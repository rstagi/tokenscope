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
from ..redact import Redactor
from . import goalname

CONTEXT_WARN = 150_000
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


# ---------------------------------------------------------------- pattern rules (small pure functions)

def retry_loop(turns, n=3):
    """Number of runs of >= n consecutive turns with the same intent where every turn had a tool error.
    turns: [(intent, had_error)]."""
    runs, cur, prev = 0, 0, None
    for intent, err in turns:
        cur = cur + 1 if err and intent == prev and cur else (1 if err else 0)
        prev = intent
        if cur == n:
            runs += 1
    return runs


def same_intent_after_error(turns):
    """How often a turn repeated the intent of the errored turn right before it."""
    return sum(1 for a, b in zip(turns, turns[1:]) if a[1] and b[0] == a[0])


def browser_loop(intents_seq, n=10):
    """Number of runs of >= n consecutive browse_ui turns."""
    runs = cur = 0
    for it in intents_seq:
        cur = cur + 1 if it == "browse_ui" else 0
        if cur == n:
            runs += 1
    return runs


def reread_files(file_events, n=3):
    """{path: reads} for files read >= n times with no Edit/Write in between. file_events: [("read"|"write", path)]."""
    count, out = {}, {}
    for kind, path in file_events:
        if kind == "write":
            count[path] = 0
            continue
        count[path] = count.get(path, 0) + 1
        if count[path] >= n:
            out[path] = max(out.get(path, 0), count[path])
    return out


def dead_tools(results, n=3):
    """{label: failures} for tool labels that failed >= n times with no success in between. results: [(label, err)]."""
    streak, out = {}, {}
    for label, err in results:
        if err:
            streak[label] = streak.get(label, 0) + 1
            if streak[label] >= n:
                out[label] = max(out.get(label, 0), streak[label])
        elif err is False:
            streak[label] = 0
    return out


def context_large(tokens, warn=CONTEXT_WARN):
    return bool(tokens and tokens >= warn)


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


def _usage(msg):
    u = msg.get("usage") if isinstance(msg.get("usage"), dict) else {}
    g = lambda k: int(u.get(k) or 0)  # noqa: E731
    return {"input": g("input_tokens"), "cache_write": g("cache_creation_input_tokens"),
            "cache_read": g("cache_read_input_tokens"), "output": g("output_tokens")}


def _new_totals():
    return {"cw": 0.0, "usd": 0.0, "input": 0, "cache_write": 0, "cache_read": 0, "output": 0, "turns": 0,
            "tool_calls": 0, "errors": 0, "unpriced": 0}


def _result_text(b):
    c = b.get("content")
    if isinstance(c, list):
        c = " ".join(str(x.get("text", "")) for x in c if isinstance(x, dict))
    return str(c or "")[:200]


class Branch:
    """Main thread or one subagent: its steps and the inputs of the per-branch pattern rules."""

    def __init__(self):
        self.steps = []
        self.turn_steps = []      # turn steps in order (intent + err feed retry/same-intent/browser rules)
        self.file_events = []     # ("read"|"write", path)
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
        self.results = []         # (label, err) in arrival order (dead_tool)
        self.totals = _new_totals()
        self.context_tokens = 0
        self.flags = {}           # flag -> {"flag", "detail", "count", "ts"}
        self.compactions = 0
        self.seq = 0
        self.hook = {"last": (None, 0), "stop_ts": 0, "active_ts": 0, "notify_ts": 0, "end_ts": 0, "open_tools": 0}

    def next_seq(self):
        self.seq += 1
        return self.seq


class LiveState:
    def __init__(self, redact=True, context_warn=CONTEXT_WARN, on_goal=None, clock=time.time):
        self.redact = redact
        self.context_warn = context_warn
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
        self._flags(s)
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
                    self._tool_result(s, b, ts)
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
        p = {"ts": ts, "chars": len(text)}
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

    def set_goal(self, sid, name, source="haiku"):
        s = self.sessions.get(sid)
        if s and s.goal:
            s.goal = dict(s.goal, name=name, source=source)
            self.version += 1

    def _tool_result(self, s, b, ts):
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
        s.results.append((tool["label"], err))
        self._touch(s, step)
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
            step["tools"].append(tool)
            s.tools[tid] = (step, tool, branch, ts)
            s.totals["tool_calls"] += 1
            f = rec.get("file")
            if f and name in ("Read", "Bash"):
                branch.file_events.append(("read", f))
            elif f:
                branch.file_events.append(("write", f))
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

    # ------------------------------------------------------------ flags
    def _flag(self, s, flag, count, detail, ts):
        if count <= 0:
            return
        f = s.flags.get(flag)
        if f is None:
            s.flags[flag] = {"flag": flag, "detail": detail, "count": count, "ts": ts}
            self._emit(s, s.main, {"k": "flag", "ts": ts, "flag": flag, "detail": detail})
        elif f["count"] != count or f["detail"] != detail:
            f["count"], f["detail"] = count, detail

    def _flags(self, s):
        ts = s.last_ts
        retry = same = browser = 0
        rereads, last_reread = 0, None
        for br in [s.main] + list(s.branches.values()):
            turns = [(t["intent"], bool(t.get("err"))) for t in br.turn_steps]
            retry += retry_loop(turns)
            same += same_intent_after_error(turns)
            browser += browser_loop([t[0] for t in turns])
            rr = reread_files(br.file_events)
            rereads += len(rr)
            if rr:
                last_reread = max(rr.items(), key=lambda kv: kv[1])
        self._flag(s, "retry_loop", retry, retry, ts)
        if same >= 3:
            self._flag(s, "same_intent_after_error", same, same, ts)
        self._flag(s, "browser_loop", browser, browser, ts)
        if last_reread:
            path, n = last_reread
            name = (os.path.splitext(path)[1] or "…") if self.redact else os.path.basename(path)
            self._flag(s, "reread_file", rereads, "%s x%d" % (name, n), ts)
        dead = dead_tools(s.results)
        if dead:
            label, n = max(dead.items(), key=lambda kv: kv[1])
            self._flag(s, "dead_tool", len(dead), "%s x%d" % (label, n), ts)
        if context_large(s.context_tokens, self.context_warn):
            self._flag(s, "context_large", 1, s.context_tokens, ts)
            s.flags["context_large"]["detail"] = s.context_tokens
        self._flag(s, "compaction", s.compactions, s.compactions, ts)

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

    def flags_list(self, s):
        return [dict(f) for f in s.flags.values()]

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
                "flags": [{"flag": f["flag"], "count": f["count"]} for f in s.flags.values()],
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
                "flags": self.flags_list(s), "seq": s.seq, "redacted": self.redact}

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

    def session_compact(self, s, now=None):
        now = self.clock() if now is None else now
        recent = s.main.turn_steps[-12:]
        last = recent[-1] if recent else None
        return {"goal": s.goal["name"] if s.goal else None, "status": self.status(s, now),
                "cw": round(s.totals["cw"], 1), "usd": round(s.totals["usd"], 6), "context_tokens": s.context_tokens,
                "current_intent": last["intent"] if last else None,
                "flags": [{"flag": f["flag"], "count": f["count"], "detail": f["detail"]} for f in s.flags.values()],
                "recent": [{"intent": t["intent"], "group": t["group"], "tools": [x["label"] for x in t["tools"]],
                            "err": bool(t.get("err"))} for t in recent],
                "agents": [{"type": a["type"], "status": a["status"], "turns": a["turns"]} for a in s.agents.values()]}
