"""Per-session live model built incrementally from transcript records (+ optional hook events).

A session holds an ordered, append-only list of steps (prompt / reason / turn / fanout / compact / flag) for the
main thread, and one list per subagent branch. Every step gets a session-wide monotonically increasing `seq`.
Steps can still change after they were emitted (a later line of the same assistant message adds tools, a tool
result sets `err`); each change bumps the step's `useq` to a fresh seq, so `steps_since(n)` returns new and updated
steps alike.

Usage is counted once per `message.id` (the last line seen for that id wins), into the session totals and, for
subagent messages, into the agent's totals as well.

Status heuristic (hooks make it exact):
  idle     SessionEnd hook, or no activity for > 30 min
  waiting  a Notification hook newer than the last tool/prompt event, or the last assistant message ended the turn
           (stop_reason end_turn / Stop hook) and the session is < 30 min old -- unless a subagent wrote
           within the last 90 s (background agents still working)
  working  the last event is a prompt / tool_result / tool_use (or a PreToolUse / UserPromptSubmit hook came after
           the last Stop) and it is < 90 s old; a PreToolUse hook without its PostToolUse keeps a long tool working
  idle     otherwise
"""
import os
import re
import time
from datetime import datetime
from urllib.parse import urlparse

from .. import intents, pricing
from ..parse import real_prompt_text
from ..understand import parse_test_output, test_runner
from ..redact import Redactor
from . import goalname

WORKING_S = 90
WAITING_S = 30 * 60
REASON_TEXT_CHARS = 160


def parse_ts(s):
    """ISO timestamp (or epoch number) -> epoch seconds, or None."""
    if isinstance(s, (int, float)):
        return float(s)
    if not isinstance(s, str) or not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


# ---------------------------------------------------------------- warning rules (small pure functions)

STUCK_N = 3
REREAD_N = 4
BROWSER_N = 20
CONTEXT_PCT = 0.85
EDIT_TOOLS = ("Edit", "Write", "MultiEdit", "NotebookEdit")
READONLY_INTENTS = {"locate", "read_code", "read_docs", "review_changes", "research", "gather_context",
                    "discover_tools", "plan", "wait", "communicate", "report", "query_data"}
BROWSER_ERR_N = 5   # a long browser run is only a loop when it keeps failing
HINTS = {
    "stuck_error": "try a different approach (read the error, change the code or the command)",
    "reread_loop": "it is already in context; reference it or read only the lines you need",
    "browser_loop": "script the flow or check the selector/page state",
    "context_near_full": "/compact or start a fresh session with a summary",
}
KEEP_CHARS = 400   # a tool result is kept as its first + last KEEP_CHARS chars only
ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
NORMS = [
    (re.compile(r"\d{4}-\d\d-\d\d[t ]\d\d:\d\d(:\d\d)?(\.\d+)?z?|\b\d\d:\d\d:\d\d(\.\d+)?\b"), "<ts>"),
    # paths stay literal: the same error on a different file is exploration, not a stuck loop
    (re.compile(r"\b0x[0-9a-f]+\b|\b[0-9a-f]{7,}\b"), "<hex>"),
    (re.compile(r"\b\d+(?:\.\d+)?\s?(?:ms|s|sec|seconds|m|min|minutes)\b"), "<dur>"),
    (re.compile(r":\d+(?::\d+)?\b"), ":<n>"),
    (re.compile(r"\b\d+(?:\.\d+)?\b"), "<n>"),
]
ERRISH_RE = re.compile(r"error|exception|failed|failure|denied|not found|no such|traceback|refused|timed out|fatal|"
                       r"cannot|can't|invalid|panic")


def keep_ends(text, n=KEEP_CHARS):
    """First + last n chars of a tool result (never keep the full text)."""
    text = str(text or "")
    return text if len(text) <= 2 * n else text[:n] + "\n" + text[-n:]


def normalise(line):
    s = ANSI_RE.sub("", line).lower()
    for rx, rep in NORMS:
        s = rx.sub(rep, s)
    return " ".join(s.split())


MSG_RE = re.compile(r'\\*"(?:message|error|detail)\\*"\s*:\s*\\*"((?:[^"\\]|\\[^"])+)')


def error_signature(text):
    """Signature of the last error-ish line(s) of a failed tool result (normalised), or None.
    JSON-ish errors (MCP / API bodies) are reduced to their message fields, so two different messages wrapped in
    the same envelope do not look like the same error."""
    text = str(text or "")
    msgs = [m for m in MSG_RE.findall(text) if m.strip()]
    if msgs:
        return normalise(" | ".join(msgs[-2:]))[:200]
    lines = [normalise(x) for x in ANSI_RE.sub("", text).splitlines()]
    lines = [x for x in lines if x.strip("<>n:. ")]
    if not lines:
        return None
    errish = [x for x in lines if ERRISH_RE.search(x)]
    if errish and len(lines) - len(errish) >= 3 and len(errish) < 0.3 * len(lines):
        return None   # mostly useful output with an incidental error line (e.g. a trailing shell warning): not stuck
    return " | ".join(errish[-2:] if errish else lines[-1:])[:200]


def stuck_errors(events, n=STUCK_N):
    """Episodes [(signature, repeats)] where one error signature repeated >= n times with no edit in between.
    events: [("edit", None) | ("err", signature)] in thread order."""
    counts, eps, cur = {}, [], {}
    for kind, sig in events:
        if kind == "edit":
            counts, cur = {}, {}
            continue
        if not sig:
            continue
        counts[sig] = counts.get(sig, 0) + 1
        if counts[sig] == n:
            cur[sig] = len(eps)
            eps.append([sig, n])
        elif counts[sig] > n:
            eps[cur[sig]][1] = counts[sig]
    return [tuple(e) for e in eps]


def reread_loops(events, n=REREAD_N):
    """Episodes [(path, reads)] of a file read in full >= n times with no edit to it and no compaction in between.
    events: [("read", path) | ("edit", path) | ("compact", None) | ("mutate", None)]; "mutate" = any tool call
    that may change files (a build, a test run, a script, a shell command), so the next read may see new content."""
    counts, eps, cur = {}, [], {}
    for kind, path in events:
        if kind in ("compact", "mutate"):
            counts, cur = {}, {}
        elif kind == "edit":
            counts.pop(path, None)
            cur.pop(path, None)
        elif kind == "read":
            counts[path] = counts.get(path, 0) + 1
            if counts[path] == n:
                cur[path] = len(eps)
                eps.append([path, n])
            elif counts[path] > n:
                eps[cur[path]][1] = counts[path]
    return [tuple(e) for e in eps]


def browser_loops(turns, n=BROWSER_N, min_err=BROWSER_ERR_N):
    """Run lengths of >= n consecutive browse_ui turns of which >= min_err had a tool error.
    turns: [(intent, err)] (a bare intent string counts as err=False)."""
    runs, cur, errs = [], 0, 0
    for t in list(turns) + [(None, False)]:
        it, err = t if isinstance(t, tuple) else (t, False)
        if it == "browse_ui":
            cur += 1
            errs += bool(err)
            continue
        if cur >= n and errs >= min_err:
            runs.append(cur)
        cur, errs = 0, 0
    return runs


def context_window(model, max_seen=0):
    return 1_000_000 if "[1m]" in str(model or "") or (max_seen or 0) > 200_000 else 200_000


def context_near_full(tokens, window, pct=CONTEXT_PCT):
    """Percent full when tokens >= pct of the window, else None."""
    if not tokens or not window or tokens < pct * window:
        return None
    return int(round(100.0 * tokens / window))


def status_of(now, last_ts, last_kind, last_active_ts=None, hook=None):
    """working / waiting / idle (see module doc).
    last_ts: last transcript event; last_kind: prompt | tool_result | tool_use | end_turn | other;
    last_active_ts: last prompt / tool event; hook: {"last": (event, ts), "active_ts", "stop_ts", "notify_ts",
    "end_ts", "open_tools"} from apply_hook (all 0 without hooks)."""
    hook = hook or {}
    last_ts, last_active_ts = last_ts or 0, last_active_ts or 0
    active, stop = hook.get("active_ts", 0), hook.get("stop_ts", 0)
    activity = max(last_ts, hook.get("last", (None, 0))[1])
    if hook.get("end_ts") and hook["end_ts"] >= activity:
        return "idle"
    age = now - activity
    if age > WAITING_S:
        return "idle"
    if hook.get("notify_ts", 0) > max(last_active_ts, active):
        return "waiting"
    if hook.get("open_tools", 0) > 0 and active > stop:
        return "working"          # a long-running tool: PreToolUse without its PostToolUse yet
    stopped = stop > 0 and stop >= max(last_active_ts, active)
    busy = last_kind in ("prompt", "tool_result", "tool_use") or (active > stop and active >= last_ts)
    if busy and not stopped and age < WORKING_S:
        return "working"
    if last_kind == "end_turn" or stopped:
        return "waiting"
    return "idle"


# ---------------------------------------------------------------- labels & targets

def tool_label(name, inp, rec, redactor=None):
    """Short tool label: "Bash git", "mcp:srv.op", "skill:<name>", "Agent(<type>)", else the tool name."""
    if name == "Bash":
        segs = rec.get("segs") or []
        prog = segs[0].split()[0] if segs and segs[0].split() else ""
        return ("Bash " + os.path.basename(prog)).strip()
    if name.startswith("mcp__"):
        parts = name.split("__")
        srv = parts[1] if len(parts) > 1 else "?"
        if redactor:
            srv = redactor.server(srv)
        return "mcp:%s.%s" % (srv, parts[-1] if len(parts) > 2 else "?")
    if name == "Skill":
        return "skill:" + (rec.get("target") or "?")
    if name in ("Agent", "Task"):
        return "Agent(%s)" % (rec.get("target") or "general-purpose")
    return name


def tool_target(name, inp, rec, redact):
    """File basename / bash program / url host / pattern; redacted: extension only / program only / "…"."""
    inp = inp if isinstance(inp, dict) else {}
    if name == "Bash":
        segs = rec.get("segs") or []
        return os.path.basename(segs[0].split()[0]) if segs and segs[0].split() else None
    f = rec.get("file")
    if f:
        return (os.path.splitext(f)[1] or "…") if redact else os.path.basename(f)
    if name == "WebFetch" and inp.get("url"):
        return "…" if redact else (urlparse(str(inp["url"])).hostname or "…")
    if name in ("Agent", "Task", "Skill"):
        return rec.get("target") or None
    t = rec.get("target")
    if t:
        return "…" if redact else t[:60]
    return None


def _full_read(name, inp, rec):
    """Read without offset/limit, or a plain `cat <file>`."""
    if name == "Read":
        inp = inp if isinstance(inp, dict) else {}
        return inp.get("offset") is None and inp.get("limit") is None
    w = (rec.get("cmd") or "").split()
    return name == "Bash" and len(w) == 2 and w[0] == "cat"


def _usage(msg):
    u = msg.get("usage") if isinstance(msg.get("usage"), dict) else {}
    g = lambda k: int(u.get(k) or 0)  # noqa: E731
    return {"input": g("input_tokens"), "cache_write": g("cache_creation_input_tokens"),
            "cache_read": g("cache_read_input_tokens"), "output": g("output_tokens")}


def _new_totals():
    return {"cw": 0.0, "usd": 0.0, "input": 0, "cache_write": 0, "cache_read": 0, "output": 0, "turns": 0,
            "tool_calls": 0, "errors": 0, "unpriced": 0}


def _result_text(b, n=200):
    c = b.get("content")
    if isinstance(c, list):
        c = "\n".join(str(x.get("text", "")) for x in c if isinstance(x, dict))
    return keep_ends(c, n)


class Branch:
    """Main thread or one subagent: its steps and the inputs of the per-branch pattern rules."""

    def __init__(self):
        self.steps = []
        self.turn_steps = []      # turn steps in order (intents feed browser_loop)
        self.err_events = []      # ("edit", None) | ("err", signature) -> stuck_error
        self.file_events = []     # ("read", path) full reads | ("edit", path) | ("compact", None) -> reread_loop
        self.msgs = {}            # message.id -> {"usage", "cw", "usd", "priced", "step"}


class Session:
    def __init__(self, sid, path, project_dir):
        self.id = sid
        self.path = path
        self.project_raw = project_dir   # transcript dir slug until a record's cwd names the project
        self.project_from_cwd = False
        self.started = None
        self.last_ts = None
        self.last_kind = "other"
        self.last_active_ts = None   # last prompt / tool_use / tool_result in the main thread
        self.model = None
        self.goal = None
        self.prompts = []
        self.main = Branch()
        self.agents = {}          # agent_id -> dict (public) ; branch kept in self.branches
        self.branches = {}
        self.fanouts = {}         # tool_use_id -> fanout step
        self.tools = {}           # tool_use_id -> (step, tool dict, branch, ts)
        self.totals = _new_totals()
        self.context_tokens = 0
        self.max_context = 0
        self.warnings = {}        # rule -> {"rule", "text", "hint", "count", "ts", "detail"}
        self.tests = []           # test runs: {"passed", "failed", "ok", "ts"}
        self.edited = set()       # paths of edited files
        self.recs = {}            # tool_use_id -> intents.record until its result arrives
        self.compactions = 0
        self.seq = 0
        self.hook = {"last": (None, 0), "stop_ts": 0, "active_ts": 0, "notify_ts": 0, "end_ts": 0, "open_tools": 0}

    def next_seq(self):
        self.seq += 1
        return self.seq


class LiveState:
    def __init__(self, redact=True, on_goal=None, on_prompt=None, clock=time.time):
        self.redact = redact
        self.on_prompt = on_prompt   # callback(session_id, prompt n, text) for later prompts (Haiku naming)
        self.clock = clock
        self.sessions = {}
        self.redactor = Redactor() if redact else None
        self.project_names = {}
        self.on_goal = on_goal    # callback(session_id, first prompt text) for optional Haiku naming
        self.new_steps = []       # (session_id, step) emitted since the last drain()
        self.version = 0

    # ------------------------------------------------------------ helpers
    def _project(self, name):
        if not self.redact:
            return name
        if name not in self.project_names:
            n = len(self.project_names)
            label = ""
            while True:
                label = chr(ord("A") + n % 26) + label
                n = n // 26 - 1
                if n < 0:
                    break
            self.project_names[name] = "project-" + label
        return self.project_names[name]

    def _emit(self, s, branch, step, agent_id=None):
        step["seq"] = step["useq"] = s.next_seq()
        if agent_id:
            step["agent"] = agent_id
        branch.steps.append(step)
        self.new_steps.append((s.id, step))
        return step

    def _touch(self, s, step):
        step["useq"] = s.next_seq()
        self.new_steps.append((s.id, step))

    def drain(self):
        out, self.new_steps = self.new_steps, []
        return out

    def session(self, sid, path=None, project_dir=None):
        s = self.sessions.get(sid)
        if s is None:
            s = self.sessions[sid] = Session(sid, path, project_dir or "?")
        return s

    def reset_session(self, sid):
        """Forget a session (its main transcript was truncated / rotated)."""
        self.sessions.pop(sid, None)
        self.version += 1

    # ------------------------------------------------------------ ingestion
    def ingest(self, tf, records, reset=False):
        """Apply new records of one tracked transcript (tail.TrackedFile)."""
        if reset and not tf.agent_id:
            self.reset_session(tf.session_id)
        s = self.session(tf.session_id, None if tf.agent_id else tf.path, tf.project_dir)
        if not tf.agent_id:
            s.path = tf.path
        agent_id = tf.agent_id
        if agent_id:
            branch = self._agent_branch(s, agent_id, tf.load_meta() or {})
        else:
            branch = s.main
        for r in records:
            self._record(s, branch, agent_id, r)
        self._warnings(s)
        self.version += 1
        return s

    def _agent_branch(self, s, agent_id, meta):
        a = s.agents.get(agent_id)
        if a is None:
            a = s.agents[agent_id] = {"id": agent_id, "type": None, "description": None, "tool_use_id": None,
                                     "steps": [], "cw": 0.0, "usd": 0.0, "turns": 0, "status": "running",
                                     "last_ts": None}
            s.branches[agent_id] = Branch()
            s.branches[agent_id].steps = a["steps"]
        if meta and a["tool_use_id"] is None:
            a["type"] = meta.get("agentType") or a["type"]
            if not self.redact:
                a["description"] = meta.get("description")
            a["tool_use_id"] = meta.get("toolUseId")
            f = s.fanouts.get(a["tool_use_id"])
            if f is not None:
                f["agent_id"] = agent_id
                a["type"] = a["type"] or f.get("agent_type")
                self._touch(s, f)
                res = s.tools.get(a["tool_use_id"])
                if res and res[1].get("err") is not None and not res[1].get("async"):
                    a["status"] = "error" if res[1]["err"] else "done"
        return s.branches[agent_id]

    def _record(self, s, branch, agent_id, r):
        ts = parse_ts(r.get("timestamp"))
        typ = r.get("type")
        if not agent_id and r.get("cwd") and not s.project_from_cwd:
            s.project_raw = os.path.basename(str(r["cwd"]).rstrip("/")) or "?"
            s.project_from_cwd = True
        if ts:
            if s.started is None or ts < s.started:
                s.started = ts
            if s.last_ts is None or ts >= s.last_ts:
                s.last_ts = ts
            if agent_id:
                s.agents[agent_id]["last_ts"] = ts
        if r.get("isCompactSummary") or (typ == "system" and "compact" in str(r.get("subtype") or "")):
            if not agent_id:
                s.compactions += 1
                s.main.file_events.append(("compact", None))
                self._emit(s, branch, {"k": "compact", "ts": ts})
            return
        if typ == "assistant":
            self._assistant(s, branch, agent_id, r, ts)
        elif typ == "user":
            self._user(s, branch, agent_id, r, ts)

    def _user(self, s, branch, agent_id, r, ts):
        msg = r.get("message") if isinstance(r.get("message"), dict) else {}
        content = msg.get("content")
        if isinstance(content, list):
            for b in content:
                if isinstance(b, dict) and b.get("type") == "tool_result":
                    self._tool_result(s, b, ts, r)
                    if not agent_id:
                        s.last_kind, s.last_active_ts = "tool_result", ts
        if agent_id:
            return
        text = real_prompt_text(r)
        if text is None:
            return
        s.last_kind, s.last_active_ts = "prompt", ts
        origin = r.get("origin") if isinstance(r.get("origin"), dict) else {}
        if text.lstrip().startswith("<task-notification>") or origin.get("kind") not in (None, "human"):
            # a background subagent / task reporting back, not a person: finish the matching agent branch
            m = re.search(r"<tool-use-id>([^<]+)</tool-use-id>", text)
            for a in s.agents.values():
                if m and a["tool_use_id"] == m.group(1).strip():
                    a["status"] = "done"
                    self.version += 1
            return
        n = len(s.prompts) + 1
        p = {"ts": ts, "chars": len(text), "name": goalname.keyword_name(text, redact=self.redact), "source": "keyword"}
        step = {"k": "prompt", "ts": ts, "chars": len(text), "n": n}
        if not self.redact:
            p["text"] = step["text"] = text
        s.prompts.append(p)
        self._emit(s, branch, step)
        if s.goal is None:
            s.goal = {"name": goalname.keyword_name(text, redact=self.redact), "source": "keyword",
                      "prompt_chars": len(text)}
            if self.on_goal:
                self.on_goal(s.id, text)
        elif self.on_prompt:
            self.on_prompt(s.id, n, text)

    def set_goal(self, sid, name, source="haiku"):
        s = self.sessions.get(sid)
        if s and s.goal:
            s.goal = dict(s.goal, name=name, source=source)
            self.version += 1

    def set_prompt_name(self, sid, n, name, source="haiku"):
        s = self.sessions.get(sid)
        if s and 0 < n <= len(s.prompts):
            s.prompts[n - 1].update(name=name, source=source)
            self.version += 1

    def _tool_result(self, s, b, ts, r=None):
        hit = s.tools.get(b.get("tool_use_id"))
        if not hit:
            return
        step, tool, br, t0 = hit
        if tool.get("err") is not None:
            return
        err = bool(b.get("is_error"))
        tool["err"] = err
        if ts and t0:
            tool["dur_ms"] = int(max(0, ts - t0) * 1000)
        if err:
            s.totals["errors"] += 1
            step["err"] = True
        elif "err" not in step:
            step["err"] = False
        self._touch(s, step)
        text = _result_text(b, KEEP_CHARS)
        if err:
            br.err_events.append(("err", error_signature(text)))
        rec = s.recs.pop(tool["id"], None) or {}
        if rec.get("cmd") and test_runner(rec["cmd"]):
            tr = r.get("toolUseResult") if isinstance(r, dict) and isinstance(r.get("toolUseResult"), dict) else {}
            out = keep_ends("\n".join(str(tr.get(k) or "") for k in ("stdout", "stderr")), KEEP_CHARS) if tr else ""
            got = parse_test_output(out) or parse_test_output(text)
            if got:
                s.tests.append({"passed": got.get("passed"), "failed": got.get("failed"),
                                "ok": not got.get("failed") and not err, "ts": ts})
            elif err:
                s.tests.append({"passed": None, "failed": None, "ok": False, "ts": ts})
        if not err and tool["name"] in ("Agent", "Task") and _result_text(b).lstrip().startswith("Async agent launched"):
            tool["async"] = True
        a = next((a for a in s.agents.values() if a["tool_use_id"] == tool["id"]), None)
        if a is not None and not tool.get("async"):
            a["status"] = "error" if err else "done"   # async agents finish later, via a task-notification

    def _assistant(self, s, branch, agent_id, r, ts):
        msg = r.get("message") if isinstance(r.get("message"), dict) else {}
        mid = msg.get("id") or r.get("uuid")
        model = msg.get("model")
        if model and not model.startswith("<") and not agent_id:
            s.model = model
        m = branch.msgs.get(mid)
        if m is None:
            m = branch.msgs[mid] = {"usage": None, "cw": 0.0, "usd": 0.0, "priced": True, "step": None}
            s.totals["turns"] += 1
            if agent_id:
                s.agents[agent_id]["turns"] += 1
        if msg.get("usage"):
            self._usage(s, agent_id, m, model, _usage(msg))
        stop = msg.get("stop_reason")
        tool_blocks = []
        for b in msg.get("content") or []:
            if not isinstance(b, dict):
                continue
            bt = b.get("type")
            if bt in ("thinking", "redacted_thinking", "text"):
                body = b.get("thinking") if bt != "text" else b.get("text")
                body = str(body or "")
                if bt == "text" and not body.strip():
                    continue
                step = {"k": "reason", "ts": ts, "kind": "text" if bt == "text" else "thinking",
                        "chars": len(body)}
                if bt != "text" and not body and b.get("signature"):
                    step["signed"] = True
                if bt == "text" and not self.redact:
                    step["text"] = body[:REASON_TEXT_CHARS]
                self._emit(s, branch, step, agent_id)
            elif bt == "tool_use":
                tool_blocks.append(b)
        if tool_blocks:
            self._tools(s, branch, agent_id, m, tool_blocks, ts)
            if not agent_id:
                s.last_kind, s.last_active_ts = "tool_use", ts
        elif not agent_id and stop == "end_turn":
            s.last_kind = "end_turn"
        elif not agent_id and stop:
            s.last_kind = "other"

    def _usage(self, s, agent_id, m, model, u):
        cw = pricing.cost_weighted(u)
        usd = pricing.cost_usd(model, u)
        old = m["usage"]
        t = s.totals
        if old is not None:   # a later line for the same message.id replaces the earlier one
            for k in ("input", "cache_write", "cache_read", "output"):
                t[k] -= old[k]
            t["cw"] -= m["cw"]
            t["usd"] -= m["usd"]
            if not m["priced"]:
                t["unpriced"] -= 1
            if agent_id:
                s.agents[agent_id]["cw"] -= m["cw"]
                s.agents[agent_id]["usd"] -= m["usd"]
        m.update(usage=u, cw=cw, usd=usd or 0.0, priced=usd is not None)
        for k in ("input", "cache_write", "cache_read", "output"):
            t[k] += u[k]
        t["cw"] += cw
        t["usd"] += m["usd"]
        if usd is None:
            t["unpriced"] += 1
        if agent_id:
            s.agents[agent_id]["cw"] += cw
            s.agents[agent_id]["usd"] += m["usd"]
        else:
            s.context_tokens = u["input"] + u["cache_write"] + u["cache_read"]
            s.max_context = max(s.max_context, s.context_tokens)
        if m["step"] is not None:
            m["step"]["cw"], m["step"]["usd"] = round(cw, 1), round(m["usd"], 6)
            self._touch(s, m["step"])

    def _tools(self, s, branch, agent_id, m, blocks, ts):
        step = m["step"]
        if step is None:
            step = {"k": "turn", "ts": ts, "intent": None, "group": None, "tools": [],
                    "cw": round(m["cw"], 1), "usd": round(m["usd"], 6), "_recs": []}
            m["step"] = step
            self._emit(s, branch, step, agent_id)
            branch.turn_steps.append(step)
        else:
            self._touch(s, step)
        for b in blocks:
            name, inp, tid = str(b.get("name") or "?"), b.get("input"), b.get("id")
            if tid in s.tools:
                continue
            rec = intents.record(name, inp)
            step["_recs"].append(rec)
            tool = {"name": name, "label": tool_label(name, inp, rec, self.redactor),
                    "target": tool_target(name, inp, rec, self.redact), "id": tid, "err": None}
            s.recs[tid] = rec
            step["tools"].append(tool)
            s.tools[tid] = (step, tool, branch, ts)
            s.totals["tool_calls"] += 1
            f = rec.get("file")
            if intents.tool_intent(rec) == "edit":
                branch.err_events.append(("edit", None))
                if name in EDIT_TOOLS and f:
                    branch.file_events.append(("edit", f))
                    s.edited.add(f)
            elif f and _full_read(name, inp, rec):
                branch.file_events.append(("read", f))
            elif intents.tool_intent(rec) not in READONLY_INTENTS:
                branch.file_events.append(("mutate", None))
            if name in ("Agent", "Task"):
                fo = {"k": "fanout", "ts": ts, "tool_use_id": tid, "agent_type": rec.get("target"), "agent_id": None}
                if not self.redact and isinstance(inp, dict):
                    fo["description"] = str(inp.get("description") or "")[:200]
                s.fanouts[tid] = fo
                self._emit(s, branch, fo, agent_id)
                linked = next((a for a in s.agents.values() if a["tool_use_id"] == tid), None)
                if linked:
                    fo["agent_id"] = linked["id"]
        intent, _ = intents.turn_intent(step["_recs"])
        step["intent"], step["group"] = intent, intents.GROUP.get(intent, "other")

    # ------------------------------------------------------------ warnings
    def _warn(self, s, rule, count, text, detail, ts):
        w = s.warnings.get(rule)
        if w is None:
            s.warnings[rule] = {"rule": rule, "text": text, "hint": HINTS[rule], "count": count, "ts": ts,
                                "detail": detail}
            self._emit(s, s.main, {"k": "flag", "ts": ts, "flag": rule, "rule": rule, "detail": text, "text": text,
                                   "hint": HINTS[rule]})
        elif (w["count"], w["text"]) != (count, text):
            w.update(count=count, text=text, detail=detail)

    def _warnings(self, s):
        ts = s.last_ts
        stuck, rereads, browser = [], [], []
        for br in [s.main] + list(s.branches.values()):
            stuck += stuck_errors(br.err_events)
            rereads += reread_loops(br.file_events)
            browser += browser_loops((t["intent"], bool(t.get("err"))) for t in br.turn_steps)
        if stuck:
            sig, n = stuck[-1]
            self._warn(s, "stuck_error", len(stuck), "same error \u00d7%d, no edits since" % n,
                       None if self.redact else sig, ts)
        if rereads:
            path, n = rereads[-1]
            name = (os.path.splitext(path)[1] or "…") if self.redact else os.path.basename(path)
            self._warn(s, "reread_loop", len(rereads), "re-read %s \u00d7%d" % (name, n), name, ts)
        if browser:
            self._warn(s, "browser_loop", len(browser), "%d browser turns in a row" % browser[-1], None, ts)
        pct = context_near_full(s.context_tokens, context_window(s.model, s.max_context))
        if pct:
            self._warn(s, "context_near_full", 1, "context %d%% full" % pct, s.context_tokens, ts)
        else:
            s.warnings.pop("context_near_full", None)   # live: goes away after a compaction

    # ------------------------------------------------------------ hooks
    def apply_hook(self, ev):
        s = self.sessions.get(ev.get("session_id"))
        if s is None:
            return
        name, ts = ev.get("event"), parse_ts(ev.get("ts")) or self.clock()
        h = s.hook
        h["last"] = (name, ts)
        if name in ("UserPromptSubmit", "PreToolUse", "PostToolUse", "PostToolUseFailure", "SubagentStart",
                    "SessionStart"):
            h["active_ts"] = ts
        if name == "PreToolUse":
            h["open_tools"] += 1
        elif name in ("PostToolUse", "PostToolUseFailure"):
            h["open_tools"] = max(0, h["open_tools"] - 1)
        elif name == "Stop":
            h["stop_ts"], h["open_tools"] = ts, 0
        elif name == "Notification":
            h["notify_ts"] = ts
        elif name == "SessionEnd":
            h["end_ts"] = ts
        self.version += 1

    # ------------------------------------------------------------ views
    def status(self, s, now=None):
        now = self.clock() if now is None else now
        st = status_of(now, s.last_ts, s.last_kind, s.last_active_ts, s.hook)
        if st != "working" and any(a["status"] == "running" and a.get("last_ts") and now - a["last_ts"] < 90
                                   for a in s.agents.values()):
            return "working"   # main turn ended but background subagents are still active
        return st

    @staticmethod
    def public_step(step):
        return {k: v for k, v in step.items() if not k.startswith("_")}

    def all_turns(self, s):
        out = list(s.main.turn_steps)
        for br in s.branches.values():
            out.extend(br.turn_steps)
        return sorted(out, key=lambda t: t["seq"])

    def agents_summary(self, s):
        return [{"id": a["id"], "type": a["type"], "description": a["description"], "tool_use_id": a["tool_use_id"],
                 "cw": round(a["cw"], 1), "usd": round(a["usd"], 6), "turns": a["turns"], "status": a["status"],
                 "steps": len(a["steps"])} for a in s.agents.values()]

    def warnings_list(self, s):
        return [{k: w[k] for k in ("rule", "text", "hint", "count", "ts")} for w in s.warnings.values()]

    @staticmethod
    def flags_alias(s):
        """Old `flags` shape, kept for compatibility: [{"flag", "count", "detail"}]."""
        return [{"flag": w["rule"], "count": w["count"], "detail": w["text"]} for w in s.warnings.values()]

    def totals(self, s):
        t = dict(s.totals)
        t["cw"], t["usd"] = round(t["cw"], 1), round(t["usd"], 6)
        return t

    def board_row(self, s, now):
        turns = self.all_turns(s)
        last = s.main.turn_steps[-1] if s.main.turn_steps else (turns[-1] if turns else None)
        mix = {}
        for t in turns:
            mix[t["group"]] = mix.get(t["group"], 0.0) + (t.get("cw") or 0)
        last_step = None
        if last:
            tool = last["tools"][-1] if last["tools"] else {}
            last_step = {"label": tool.get("label"), "ts": last["ts"], "err": tool.get("err")}
        return {"id": s.id, "short": s.id[:8], "project": self._project(s.project_raw), "goal": s.goal, "status": self.status(s, now),
                "current_intent": last["intent"] if last else None, "current_group": last["group"] if last else None,
                "last_step": last_step, "started": s.started, "last_ts": s.last_ts,
                "elapsed_s": round((s.last_ts or now) - (s.started or now), 1),
                "cw": round(s.totals["cw"], 1), "usd": round(s.totals["usd"], 6), "turns": s.totals["turns"],
                "tool_calls": s.totals["tool_calls"], "errors": s.totals["errors"],
                "agents_running": sum(1 for a in s.agents.values() if a["status"] == "running"),
                "agents_total": len(s.agents), "context_tokens": s.context_tokens,
                "flags": [{"flag": w["rule"], "count": w["count"]} for w in s.warnings.values()],
                "warnings": self.warnings_list(s),
                "intent_mix": sorted([[g, round(c, 1)] for g, c in mix.items()], key=lambda x: -x[1])}

    def board(self, now=None, max_idle_s=6 * 3600):
        now = self.clock() if now is None else now
        rows = [self.board_row(s, now) for s in self.sessions.values()
                if s.last_ts is None or now - s.last_ts <= max_idle_s]
        rows.sort(key=lambda r: r["last_ts"] or 0, reverse=True)
        return {"now": now, "redacted": self.redact, "sessions": rows}

    def find(self, sid):
        s = self.sessions.get(sid)
        if s is None:
            hits = [x for x in self.sessions.values() if x.id.startswith(sid)]
            s = hits[0] if len(hits) == 1 else None
        return s

    def _header(self, s, now):
        return {"id": s.id, "short": s.id[:8], "project": self._project(s.project_raw), "file": None if self.redact else s.path,
                "started": s.started, "last_event_ts": s.last_ts, "model": s.model, "goal": s.goal,
                "totals": self.totals(s), "context_tokens": s.context_tokens, "status": self.status(s, now),
                "flags": self.flags_alias(s), "warnings": self.warnings_list(s), "seq": s.seq, "redacted": self.redact}

    def session_full(self, s, now=None):
        now = self.clock() if now is None else now
        d = self._header(s, now)
        d["prompts"] = [dict(p) for p in s.prompts]
        d["steps"] = [self.public_step(x) for x in s.main.steps]
        d["agents"] = [dict(a, steps=[self.public_step(x) for x in a["steps"]], cw=round(a["cw"], 1),
                            usd=round(a["usd"], 6)) for a in s.agents.values()]
        return d

    def session_since(self, s, since, now=None):
        """Steps (main + agents) created or updated after `since`, ordered by seq, plus header + agents summary."""
        now = self.clock() if now is None else now
        d = self._header(s, now)
        steps = [x for x in s.main.steps if x["useq"] > since]
        for a in s.agents.values():
            steps.extend(x for x in a["steps"] if x["useq"] > since)
        d["since"] = since
        d["steps"] = [self.public_step(x) for x in sorted(steps, key=lambda x: x["seq"])]
        d["agents"] = self.agents_summary(s)
        return d

    def _turn_agents(self, s, turn):
        """Subagents launched by one turn's Agent/Task calls: [{type, status, turns, cw}]."""
        out = []
        for x in turn["tools"]:
            tid = x.get("id")
            if not tid or tid not in s.fanouts:
                continue
            a = next((a for a in s.agents.values() if a["tool_use_id"] == tid), None)
            f = s.fanouts[tid]
            out.append({"type": (a and a["type"]) or f.get("agent_type") or "agent",
                        "status": a["status"] if a else "starting", "turns": a["turns"] if a else 0,
                        "cw": round(a["cw"]) if a else 0})
        return out

    def session_compact(self, s, now=None, recent_n=60):
        """Small payload for the in-terminal mod / status line (see docs/live-spec.md)."""
        now = self.clock() if now is None else now
        turns = s.main.turn_steps
        recent = turns[-recent_n:]
        last = recent[-1] if recent else None
        started = s.started or (turns[0]["ts"] if turns else None)
        return {"goal": s.goal["name"] if s.goal else None, "status": self.status(s, now),
                "cw": round(s.totals["cw"], 1), "usd": round(s.totals["usd"], 6), "context_tokens": s.context_tokens,
                "elapsed_s": round(now - started, 1) if started else None,
                "turns": s.totals["turns"], "tool_calls": s.totals["tool_calls"], "errors": s.totals["errors"],
                "current_intent": last["intent"] if last else None,
                "flags": self.flags_alias(s), "warnings": self.warnings_list(s),
                "asked": self._asked(s), "now": self._now(s, now), "tests": self._tests(s),
                "recent": [{"intent": t["intent"], "group": t["group"], "label": intents.label(t["intent"]),
                            "cw": round(t.get("cw") or 0),
                            "tools": [x["label"] for x in t["tools"]],
                            "targets": [x.get("target") for x in t["tools"]],
                            "tool_err": [bool(x.get("err")) for x in t["tools"]],
                            "agents": self._turn_agents(s, t),
                            "err": bool(t.get("err"))} for t in recent],
                "cw_series": [round(t.get("cw") or 0) for t in turns[-120:]],
                "agents": [{"type": a["type"], "status": a["status"], "turns": a["turns"]} for a in s.agents.values()],
                **self._edited(s)}

    def _asked(self, s):
        n = len(s.prompts)
        latest = s.prompts[-1] if n > 1 else None
        return {"goal": s.goal["name"] if s.goal else None, "source": s.goal["source"] if s.goal else None,
                "prompt_n": n, "latest": latest["name"] if latest else None,
                "latest_ts": s.prompts[-1]["ts"] if n else None}

    def _now(self, s, now):
        turns = s.main.turn_steps
        if not turns:
            return None
        last = turns[-1]
        i = len(turns) - 1
        while i > 0 and turns[i - 1]["group"] == last["group"]:
            i -= 1
        tool = last["tools"][-1] if last["tools"] else {}
        t0 = turns[i]["ts"]
        return {"intent": last["intent"], "label": intents.label(last["intent"]), "group": last["group"],
                "tool": tool.get("label"), "target": tool.get("target"),
                "since_s": round(max(0.0, now - t0), 1) if t0 else None, "status": self.status(s, now)}

    @staticmethod
    def _tests(s):
        if not s.tests:
            return None
        last = s.tests[-1]
        if last["passed"] is None and last["failed"] is None:
            return {"passed": None, "failed": None, "ok": False, "runs": len(s.tests), "last_ts": last["ts"],
                    "trend": [t["failed"] for t in s.tests[-8:]]}
        return {"passed": last["passed"], "failed": last["failed"], "ok": last["ok"], "runs": len(s.tests),
                "last_ts": last["ts"], "trend": [t["failed"] for t in s.tests[-8:]]}

    def _edited(self, s):
        if self.redact:
            return {"files_edited_n": len(s.edited)}
        return {"files_edited": sorted({os.path.basename(p) for p in s.edited}), "files_edited_n": len(s.edited)}
