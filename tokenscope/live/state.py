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
           (stop_reason end_turn / Stop hook) and the session is < 30 min old -- unless a subagent is still
           active (see agent_active: it wrote within the last 90 s, has a call running, or a SubagentStart hook
           without its SubagentStop), or one was launched < 90 s ago and has not written yet
  working  the last event is a prompt / tool_result / tool_use (or a PreToolUse / UserPromptSubmit hook came after
           the last Stop) and it is < 90 s old; a PreToolUse hook without its PostToolUse keeps a long tool working
  idle     otherwise
"""
import bisect
import copy
import os
import re
import sys
import time
from collections import Counter
from datetime import datetime
from urllib.parse import urlparse

from .. import alerts as alertsmod, intents, pricing, steps as stepsmod
from .alertstore import AlertStore
from ..xindex import named_files, prompt_words
from ..analyze import est_tokens
from ..parse import prompt_source, real_prompt_text
from ..understand import parse_test_output, test_runner
from ..redact import Redactor
from ..skills import SkillScanner, tool_skill
from . import goalname, recap as recapmod

WORKING_S = 90
WAITING_S = 30 * 60
REASON_TEXT_CHARS = 160
LABEL_BATCH = 12   # steps per model labelling call
BOARD_SPARK_N = 40   # points in a board card's cost sparkline
FOLLOW_STEPS = 40    # steps in a follow-view tile (the latest ones)
FOLLOW_MAX = 4       # sessions one follow view shows
FOLLOW_STATUS = {"working": "running"}   # follow-view tile status: running / waiting / idle / ended
FOLLOW_AGENTS = 12   # subagent branches one follow tile shows (active ones first; the rest counted per step)
FOLLOW_AGENT_STEPS = 6   # latest steps shown in a subagent branch
FOLLOW_DEPTH = 3     # nesting levels of subagent branches in a follow tile
TILE_KINDS = ("cost", "off_task", "looping")   # order of the alert rows at the top of a follow tile
TILE_TRIGGERS = 4    # triggers carried per tile alert (the latest one and up to 3 earlier)
AGENT_CALL_S = 10 * 60   # a subagent call without its result keeps the subagent active this long (a long test run)


def follow_branches(rows):
    """Mark the subagent branches of a follow tile's steps (recursively, nested ones too): "live" when it or one of
    its subagents is running or starting; "collapsed" (header line only) when it finished (done / failed) and is
    not the latest branch of its level, i.e. the last one launched by the newest step that launched any. Returns
    whether any branch is live."""
    live_any, latest = False, None
    for r in rows:
        for a in r.get("agents", ()):
            a["live"] = follow_branches(a["steps"]) or a["status"] in ("running", "starting")
            live_any = live_any or a["live"]
            latest = a
    for r in rows:
        for a in r.get("agents", ()):
            a["collapsed"] = a["status"] in ("done", "error") and not a["live"] and a is not latest
    return live_any


def board_order(rows):
    """Stable board card order: sessions that have not ended in order of first appearance (start time, then id; a
    session without a timestamp yet last), then the ended ones in the same order. Cost and activity never move a
    card: one moves only when its session ends (or resumes), and cards come and go as sessions do."""
    return sorted(rows, key=lambda r: (bool(r.get("ended")), r.get("started") is None, r.get("started") or 0.0,
                                       r["id"]))


def hook_agent_id(v):
    """The transcript agent id a hook event names ("agent-<id>" or "<id>"), or None."""
    v = str(v or "").strip()
    v = v[len("agent-"):] if v.startswith("agent-") else v
    return v if re.fullmatch(r"[\w-]{1,64}", v) else None


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
NAMES_CHARS = 50_000   # of a search output scanned for the file names it shows (xindex: which files it led to)
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

GENERIC_SUB = {"git", "npm", "pnpm", "yarn", "cargo", "go", "docker", "uv", "make", "gh", "kubectl", "bun"}


def bash_what(rec, redact=True):
    """What a shell command runs: the test runner ("unittest", "./run_tests.sh"), "git status", "python3 -m mod"
    (module only when not redacting), else the program of the first segment."""
    cmd = rec.get("cmd") or ""
    tr = test_runner(cmd)
    if tr:
        return tr if not redact or not tr.startswith(("./", "../", "/")) else "test script"
    # first segment with a program: skips FOO=1, time/sudo/..., "(cd x", shell syntax ("for f in *; do cat")
    words = next((w for w in map(stepsmod._words, rec.get("segs") or []) if w), [])
    if not words:
        return None
    prog = os.path.basename(words[0])
    if prog in GENERIC_SUB and len(words) > 1 and re.fullmatch(r"[a-z][\w-]*", words[1]):
        return "%s %s" % (prog, words[1])
    if re.fullmatch(r"python[\d.]*", prog) and len(words) > 2 and words[1] == "-m" and not redact:
        return "%s -m %s" % (prog, words[2])
    return prog


def tool_label(name, inp, rec, redactor=None):
    """Short tool label: "Bash git", "mcp:srv.op", "skill:<name>", "Agent(<type>)", else the tool name."""
    if name == "Bash":
        return ("Bash " + (bash_what(rec, redactor is not None) or "")).strip()
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
        return bash_what(rec, redact)
    f = rec.get("file")
    if f:
        return (os.path.splitext(f)[1] or "…") if redact else os.path.basename(f)
    if name == "WebFetch" and inp.get("url"):
        return "…" if redact else (urlparse(str(inp["url"])).hostname or "…")
    if name in ("Agent", "Task"):   # the label already names the type: the target is what it was asked to do
        return None if redact else (str(inp.get("description") or "")[:48] or None)
    if name == "Skill":
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


def _result_chars(b):
    c = b.get("content")
    if isinstance(c, list):
        return sum(len(str(x.get("text", ""))) for x in c if isinstance(x, dict))
    return len(c) if isinstance(c, str) else 0


def _media_key(b):
    """Hash of the images / documents in a tool result (their text is empty: two screenshots must not look alike)."""
    c = b.get("content")
    if not isinstance(c, list):
        return ""
    data = [str((x.get("source") or {}).get("data") or "") for x in c
            if isinstance(x, dict) and x.get("type") in ("image", "document") and isinstance(x.get("source"), dict)]
    return alertsmod.h(*data) if data else ""


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
        self.msgs = {}            # message.id -> {"usage", "cw", "usd", "priced", "step", "ts" (its first record)}
        self.pending_thinking = 0  # thinking blocks / text seen since the last turn step (main thread: step labels)
        self.pending_text = ""


class Session:
    def __init__(self, sid, path, project_dir):
        self.id = sid
        self.path = path
        self.project_raw = project_dir   # transcript dir slug until a record's cwd names the project
        self.project_from_cwd = False
        self.cwd = None                  # the main thread's working directory (project skills: <cwd>/.claude/skills)
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
        self.finished = {}        # tool_use_id -> "done"|"error" from task-notifications
        self.wf_runs = {}         # workflow run id (wf_...) -> tool_use_id of the main-thread Workflow call
        self.wf_calls = []        # (ts, tool_use_id) of main-thread Workflow calls, in order
        self.edited = set()       # paths of edited files
        self.recs = {}            # tool_use_id -> intents.record until its result arrives
        self.compactions = 0
        self.seq = 0
        self.hook = {"last": (None, 0), "stop_ts": 0, "active_ts": 0, "notify_ts": 0, "end_ts": 0, "open_tools": 0}
        self.prompt_at = []       # main-thread turn count when each prompt (incl. queued) arrived: step boundaries
        self.ctx_at_prompt = []   # context tokens when each prompt arrived (idle recap)
        self.prompt_words = []    # xindex.prompt_words of each prompt (cross-session matching only; never emitted)
        self.prompt_uuid = []     # transcript uuid of each prompt record (tokenscope top --by turn; never emitted)
        self.headless = False     # a prompt came from `claude -p` / an SDK program (parse.prompt_source): no next task
        self.compact_at = []      # main-thread turn count at each compaction (idle recap)
        self.recap_cache = None   # ((steps key, index version), recap)
        self.labeller = stepsmod.Labeller()
        self.intents_cache = None  # (key, payload)
        self.agent_labellers = {}  # agent id -> Labeller (subagent steps: signal / rule labels, never the model)
        self.agent_steps_cache = {}   # agent id -> (key, (steps, groups))
        self.branch_ids = {}      # id(Branch) -> agent id (which subagent a call was made in)
        self.agent_rev = {}       # agent id -> seq of its (or a descendant's) last ingested record: agent_steps cache
        self.agent_open = {}      # agent id -> tool_use ids of its calls without a result yet
        self.tool_agent = {}      # tool_use_id of a subagent's call -> that agent id (until the result)
        self.hook_agents = {}     # agent id -> {"start", "stop", "open", "ts"} from hooks that name the agent
        self.skills = Counter()   # skill name -> invocations (Skill tool in any thread + slash-command skills)
        self.skill_scan = SkillScanner()   # main thread: slash-command skills
        self.slash_skills = []    # {"at": main-thread turn count, "name", "user": typed} per slash-command skill
        self.alerts_cache = None  # (steps key, [alerts.Alert]) (see LiveState.alerts)
        self.alert_names = {}     # (kind, private file / call key) -> ordinal: "file #2" in alert messages

    def next_seq(self):
        self.seq += 1
        return self.seq


class LiveState:
    def __init__(self, redact=True, on_goal=None, on_prompt=None, clock=time.time, model_labels=False,
                 alerts_config=None, alert_store=None):
        self.redact = redact
        self.alerts_config = alerts_config or copy.deepcopy(alertsmod.DEFAULTS)   # alerts.load_config()
        # alert history with acknowledgement state (alertstore.py): on disk for the server, else in memory
        self.alert_store = alert_store if alert_store is not None else AlertStore(clock=clock)
        self.alert_synced = {}    # session id -> seq its alerts were last synced to the history at
        self.model_labels = model_labels   # True: closed steps wait for a model label (see label_requests)
        self.on_prompt = on_prompt   # callback(session_id, prompt n, text) for later prompts (Haiku naming)
        self.clock = clock
        self.sessions = {}
        self.redactor = Redactor() if redact else None
        self.project_names = {}
        self.on_goal = on_goal    # callback(session_id, first prompt text) for optional Haiku naming
        self.new_steps = []       # (session_id, step) emitted since the last drain()
        self.xhints = None        # cross-session hints: callable(sid, project, cwd, steps, groups, used skills) -> hint
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
            s.labeller.no_model = not self.model_labels   # no model: closed steps lock their rule label
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
            if s.agents[agent_id].get("workflow") is None:
                wf = [p for p in os.path.normpath(tf.path).split(os.sep)[:-1] if p.startswith("wf_")]
                s.agents[agent_id]["workflow"] = wf[-1] if wf else ""
        else:
            branch = s.main
        for r in records:
            self._record(s, branch, agent_id, r)
        if agent_id:   # its steps (and its ancestors': their steps carry its cost) need a new segmentation
            rev, seen, aid = s.next_seq(), set(), agent_id   # a fresh seq: records that emitted no step count too
            while aid and aid not in seen and aid in s.agents:
                seen.add(aid)
                s.agent_rev[aid] = rev
                aid = self._parent(s, s.agents[aid])
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
            s.branch_ids[id(s.branches[agent_id])] = agent_id
        if meta and a["tool_use_id"] is None:
            a["type"] = meta.get("agentType") or a["type"]
            if not self.redact:
                a["description"] = meta.get("description")
            a["tool_use_id"] = meta.get("toolUseId")
            a["parent_id"] = meta.get("parentAgentId")
            if a["tool_use_id"] in s.finished:
                a["status"] = s.finished[a["tool_use_id"]]
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
        if not agent_id and r.get("cwd") and s.cwd is None:
            s.cwd = str(r["cwd"])
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
                s.agents[agent_id].setdefault("first_ts", ts)
        if r.get("isCompactSummary") or (typ == "system" and "compact" in str(r.get("subtype") or "")):
            if not agent_id:
                s.compactions += 1
                s.compact_at.append(len(s.main.turn_steps))
                s.main.file_events.append(("compact", None))
                self._emit(s, branch, {"k": "compact", "ts": ts})
            return
        self._notifications(s, r)
        if typ == "attachment" and not agent_id:
            att = r.get("attachment") if isinstance(r.get("attachment"), dict) else {}
            text = att.get("prompt") if att.get("type") == "queued_command" else None
            if isinstance(text, str) and text.strip() and not text.lstrip().startswith("<"):
                s.last_kind, s.last_active_ts = "prompt", ts
                self._new_prompt(s, branch, text.strip(), ts, queued=True, src=prompt_source(r, text), uuid=r.get("uuid"))   # a follow-up typed mid-turn
            return
        if not agent_id and typ in ("user", "assistant"):
            self._slash_skills(s, r, ts)
        if typ == "assistant":
            self._assistant(s, branch, agent_id, r, ts)
        elif typ == "user":
            self._user(s, branch, agent_id, r, ts)

    def _slash_skills(self, s, r, ts):
        """Slash-command skills of the main thread (skills.SkillScanner): counted, and a step boundary when typed."""
        for kind, name in s.skill_scan.feed(r):
            if kind != "slash":
                continue
            s.skills[name] += 1
            s.slash_skills.append({"at": len(s.main.turn_steps), "name": name, "user": s.skill_scan.typed})
            if s.skill_scan.typed:
                s.last_kind, s.last_active_ts = "prompt", ts   # a request: the agent is about to work

    def _notifications(self, s, r):
        """<task-notification> for a background agent, in any thread (a subagent's own background children report
        back inside the subagent transcript, as a queued_command attachment): mark that agent finished. Kept in
        s.finished so an agent whose transcript is read later still starts out finished."""
        typ = r.get("type")
        if typ == "attachment":
            att = r.get("attachment") if isinstance(r.get("attachment"), dict) else {}
            text = att.get("prompt") if isinstance(att.get("prompt"), str) else ""
        elif typ == "user":
            msg = r.get("message") if isinstance(r.get("message"), dict) else {}
            c = msg.get("content")
            text = c if isinstance(c, str) else " ".join(str(b.get("text", "")) for b in c or [] if isinstance(b, dict)
                                                           and b.get("type") == "text")
        else:
            return
        if "<task-notification>" not in (text or ""):
            return
        tid = re.search(r"<tool-use-id>([^<]+)</tool-use-id>", text)
        st = re.search(r"<status>([^<]+)</status>", text)
        if not tid:
            return
        status = "error" if st and st.group(1).strip() in ("failed", "error", "killed") else "done"
        s.finished[tid.group(1).strip()] = status
        for a in s.agents.values():
            if a["tool_use_id"] == tid.group(1).strip() and a["status"] != status:
                a["status"] = status
                self.version += 1

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
        self._new_prompt(s, branch, text, ts, src=prompt_source(r, text), uuid=r.get("uuid"))

    def _new_prompt(self, s, branch, text, ts, queued=False, src="human", uuid=None):
        s.headless = s.headless or src == "headless"
        n = len(s.prompts) + 1
        p = {"ts": ts, "chars": len(text), "name": goalname.goal_label(text, redact=self.redact), "source": "keyword"}
        step = {"k": "prompt", "ts": ts, "chars": len(text), "n": n}
        if queued:
            step["queued"] = True   # typed while the agent was working, absorbed mid-turn
        if not self.redact:
            p["text"] = step["text"] = text
        s.prompts.append(p)
        s.prompt_at.append(len(s.main.turn_steps))
        s.prompt_words.append(prompt_words(text))
        s.prompt_uuid.append(uuid)
        s.ctx_at_prompt.append(s.context_tokens)
        self._emit(s, branch, step)
        if s.goal is None:
            s.goal = {"name": p["name"], "source": "keyword",
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
        aid = s.tool_agent.pop(tool["id"], None)
        if aid:
            s.agent_open.get(aid, set()).discard(tool["id"])
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
        facts = step.get("_facts") or []
        k = next((i for i, x in enumerate(step["tools"]) if x is tool), None)
        if k is not None and k < len(facts) and facts[k].get("full"):
            facts[k]["chars"] = _result_chars(b)   # what the read put into the context (recap: shared_reads)
        if k is not None and k < len(facts) and br is s.main and not err \
                and (facts[k].get("search") or tool["name"] == "Bash"):
            facts[k]["names"] = named_files(_result_text(b, NAMES_CHARS))   # xindex: which files a search led to
        if err:
            br.err_events.append(("err", error_signature(text)))
        fact = facts[k] if k is not None and k < len(facts) else None
        if fact is not None:   # alerts: same call + same result = a repeat; a failure = a retry
            fact["res"], fact["fail"] = alertsmod.h(_result_chars(b), text, _media_key(b)), err
        rec = s.recs.pop(tool["id"], None) or {}
        if rec.get("cmd") and test_runner(rec["cmd"]):
            tr = r.get("toolUseResult") if isinstance(r, dict) and isinstance(r.get("toolUseResult"), dict) else {}
            out = keep_ends("\n".join(str(tr.get(k) or "") for k in ("stdout", "stderr")), KEEP_CHARS) if tr else ""
            got = parse_test_output(out) or parse_test_output(text)
            if got:
                s.tests.append({"passed": got.get("passed"), "failed": got.get("failed"),
                                "ok": not got.get("failed") and not err, "ts": ts, "main": br is s.main})
            elif err:
                s.tests.append({"passed": None, "failed": None, "ok": False, "ts": ts, "main": br is s.main})
            if (got or err) and br is s.main:
                step["_test"] = "pass" if s.tests[-1]["ok"] else "fail"
                # private, for the idle recap: the failure's signature tells a repeat from a new failure
                step.setdefault("_tests", []).append({
                    "ok": s.tests[-1]["ok"], "failed": s.tests[-1]["failed"], "runner": bash_what(rec, self.redact),
                    "sig": None if s.tests[-1]["ok"] else error_signature(out or text)})
                t = s.tests[-1]
                tool["test"] = {"ok": t["ok"], "passed": t["passed"], "failed": t["failed"]}
            if fact is not None and s.tests and (got or err):
                fact["fail"] = not s.tests[-1]["ok"]
        if tool["name"] == "Workflow" and br is s.main and isinstance(r, dict) \
                and isinstance(r.get("toolUseResult"), dict) and r["toolUseResult"].get("runId"):
            s.wf_runs[str(r["toolUseResult"]["runId"])] = tool["id"]
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
            m = branch.msgs[mid] = {"usage": None, "cw": 0.0, "usd": 0.0, "priced": True, "step": None, "ts": ts}
            s.totals["turns"] += 1
            if agent_id:
                s.agents[agent_id]["turns"] += 1
        if msg.get("usage"):
            self._usage(s, agent_id, m, model, _usage(msg))
        stop = msg.get("stop_reason")
        if agent_id and stop:
            a = s.agents[agent_id]
            if stop == "end_turn" and a["status"] in ("running", "starting"):
                a["status"], a["ended_by_turn"] = "done", True   # its final answer: done even if no notification comes
                self.version += 1
            elif stop != "end_turn" and a.get("ended_by_turn"):
                a["status"], a["ended_by_turn"] = "running", False   # resumed (e.g. a follow-up message to it)
                self.version += 1
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
                # kept privately for step labels (masked before any model sees it)
                branch.pending_thinking += bt != "text"
                if body and len(branch.pending_text) < stepsmod.TEXT_CHARS:
                    branch.pending_text = (branch.pending_text + " " + " ".join(body.split()))[:stepsmod.TEXT_CHARS]
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
            step.update(_raw=[], _thinking=branch.pending_thinking, _text=branch.pending_text.strip())
            branch.pending_thinking, branch.pending_text = 0, ""
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
            if "_raw" in step:
                step["_raw"].append({"name": name, "input": stepsmod.slim_input(name, inp)})
            tool = {"name": name, "label": tool_label(name, inp, rec, self.redactor), "ts": ts,
                    "intent": intents.tool_intent(rec),
                    "target": tool_target(name, inp, rec, self.redact), "id": tid, "err": None}
            s.recs[tid] = rec
            if tool_skill(name, inp):
                s.skills[tool_skill(name, inp)] += 1
            step["tools"].append(tool)
            s.tools[tid] = (step, tool, branch, ts)
            s.totals["tool_calls"] += 1
            if agent_id:
                s.agent_open.setdefault(agent_id, set()).add(tid)
                s.tool_agent[tid] = agent_id
            f = rec.get("file")
            it = intents.tool_intent(rec)
            af = alertsmod.call_facts(name, inp, rec, it)   # hashes: looping / off-task alerts
            step.setdefault("_facts", []).append({   # private, for the idle recap (see recap.py)
                "tool": name, "file": f or None, "full": bool(f) and it != "edit" and _full_read(name, inp, rec),
                "edit": it == "edit" and name in EDIT_TOOLS and bool(f), "search": it == "locate",
                # waiting, or reading a background task's output file (<id>.output): polling
                "poll": it == "wait" or (name == "Bash" and stepsmod.program(rec.get("cmd") or "") == "sleep")
                or (bool(f) and it != "edit" and str(f).endswith(".output")),
                "apoll": af.pop("poll", False), **af})
            if intents.tool_intent(rec) == "edit":
                branch.err_events.append(("edit", None))
                if name in EDIT_TOOLS and f:
                    branch.file_events.append(("edit", f))
                    s.edited.add(f)
            elif f and _full_read(name, inp, rec):
                branch.file_events.append(("read", f))
            elif intents.tool_intent(rec) not in READONLY_INTENTS:
                branch.file_events.append(("mutate", None))
            if name == "Workflow" and not agent_id:
                s.wf_calls.append((ts or 0, tid))
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
        aid = hook_agent_id(ev.get("agent_id"))
        if aid and name in ("SubagentStart", "SubagentStop", "PreToolUse", "PostToolUse", "PostToolUseFailure"):
            # a subagent's own event (hooks name the agent): its activity, not the main thread's
            a = s.hook_agents.setdefault(aid, {"start": 0, "stop": 0, "open": 0, "ts": 0})
            a["ts"] = ts
            if name == "SubagentStart":
                a["start"] = ts
            elif name == "SubagentStop":
                a["stop"], a["open"] = ts, 0
            elif name == "PreToolUse":
                a["open"] += 1
            else:
                a["open"] = max(0, a["open"] - 1)
            self.version += 1
            return
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
    @staticmethod
    def agent_active(s, a, now):
        """A subagent still working: not finished (end_turn / result / task-notification) and not stopped by a
        SubagentStop hook, and it wrote within WORKING_S, or a call of it has no result yet (< AGENT_CALL_S: a long
        test run), or a SubagentStart / PreToolUse hook naming it has no SubagentStop / PostToolUse yet."""
        if a["status"] not in ("running", "starting"):
            return False
        last = a.get("last_ts") or 0
        hk = s.hook_agents.get(a["id"])
        if hk:
            if hk["stop"] and hk["stop"] >= max(hk["start"], last):
                return False
            if (hk["start"] or hk["open"]) and now - max(last, hk["ts"]) < AGENT_CALL_S:
                return True
        if s.agent_open.get(a["id"]) and now - last < AGENT_CALL_S:
            return True
        return bool(last) and now - last < WORKING_S

    def _pending_launches(self, s, now):
        """Agent/Task calls launched < WORKING_S ago whose subagent has not written yet (and did not fail)."""
        linked = {a["tool_use_id"] for a in s.agents.values()}
        out = []
        for tid, f in s.fanouts.items():
            if tid in linked or f.get("agent_id") or not f.get("ts") or now - f["ts"] >= WORKING_S:
                continue
            hit = s.tools.get(tid)
            if hit and hit[1].get("err"):
                continue
            out.append(f)
        return out

    def agents_active(self, s, now):
        """Number of subagents still working (agent_active), counting launches that have not written yet."""
        return sum(1 for a in s.agents.values() if self.agent_active(s, a, now)) + \
            (len(self._pending_launches(s, now)) if s.fanouts else 0)

    def status(self, s, now=None):
        now = self.clock() if now is None else now
        st = status_of(now, s.last_ts, s.last_kind, s.last_active_ts, s.hook)
        if st != "working" and (s.agents or s.fanouts) and self.agents_active(s, now):
            return "working"   # the main turn ended (or waits on a foreground agent) but subagents still work
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
                "intent_mix": sorted([[g, round(c, 1)] for g, c in mix.items()], key=lambda x: -x[1]),
                "ended": self.ended(s, now),
                "asked": self._asked(s), "now": self._now(s, now),
                "cw_series": self.cw_series(turns), **self._board_intent(s, now), **self._board_alerts(s, now)}

    def _board_alerts(self, s, now):
        """A board card's alerts: alert_counts, and the messages of the active ones not acknowledged (at most 3)."""
        al = self.alerts_public(s, now)
        keys = ("id", "kind", "rule", "severity", "message", "count")
        return {"alerts": dict(self.alert_counts(al), top=[{k: a[k] for k in keys} for a in al
                                                           if a["active"] and a["state"] == "new"][:3])}

    @staticmethod
    def alert_counts(al):
        """alerts.counts of a session's alert history, plus what still needs attention: "new" (not acknowledged),
        "new_by_kind" ({kind: n}) and "new_worst" (their worst severity)."""
        new = [a for a in al if a.get("state", "new") == "new"]
        by = {}
        for a in new:
            by[a["kind"]] = by.get(a["kind"], 0) + 1
        worst = max((a["severity"] for a in new), key=lambda v: alertsmod.SEV_RANK.get(v, 0), default=None)
        return dict(alertsmod.counts(al), new=len(new), new_by_kind=by, new_worst=worst)

    @staticmethod
    def cw_series(turns, n=BOARD_SPARK_N):
        """CW per turn (main + subagents, time order) for a board sparkline: at most `n` points, older turns summed
        into buckets so the shape covers the whole session."""
        cw = [t.get("cw") or 0 for t in turns]
        if len(cw) > n:
            k = -(-len(cw) // n)
            cw = [sum(cw[i:i + k]) for i in range(0, len(cw), k)]
        return [round(x) for x in cw]

    def _board_intent(self, s, now):
        """The current intent as the pane names it (the last step's group, else the step: label + category) and
        the session's skills, for a board card."""
        steps, gs = self.steps(s, now) if s.main.turn_steps else ([], [])
        cur = None
        if steps:
            x = steps[-1]
            g = next((g for g in gs if g["first"] <= x["i"] <= g["last"]), None)
            cur = {"label": self._lab((g or x)["label"]), "cat": (g or x)["cat"], "open": not x["closed"]}
        return {"intent": cur, "skills": [n for n, _ in sorted(s.skills.items(), key=lambda kv: (-kv[1], kv[0]))]}

    def ended(self, s, now=None):
        """A SessionEnd hook event after the session's last transcript event (a resumed session is not ended), and
        no subagent still active (agent_active): a SessionEnd while subagents work does not end the view."""
        if not (s.hook["end_ts"] and s.hook["end_ts"] >= (s.last_ts or 0)):
            return False
        now = self.clock() if now is None else now
        return not any(self.agent_active(s, a, now) for a in s.agents.values())

    def board(self, now=None, max_idle_s=6 * 3600):
        now = self.clock() if now is None else now
        rows = [self.board_row(s, now) for s in self.sessions.values()
                if s.last_ts is None or now - s.last_ts <= max_idle_s]
        return {"now": now, "redacted": self.redact, "sessions": board_order(rows)}

    def follow_tile(self, s, now=None, last_n=FOLLOW_STEPS):
        """One tile of the follow view (/api/follow): the session's latest main-thread steps (label, category, $,
        CW, wall time, open) with its running totals, status (running / waiting / idle / ended) and the call running
        now. A step that launched subagents carries them as branches ("agents", see _follow_agents): their latest
        steps, nested subagents, status and cost; "agents_running" counts the active ones. Steps come from cached
        segmentations (steps(), agent_steps() keyed on the agent's own records), so polling stays cheap."""
        now = self.clock() if now is None else now
        status = self.status(s, now)
        steps, gs = self.steps(s, now) if s.main.turn_steps else ([], [])
        ctx = self._follow_ctx(s, now) if s.agents or s.fanouts else None
        w = self._follow_now(s, now, ctx) if status == "working" else None
        tail = steps[-last_n:]
        shown = (self._follow_early(s, steps[:len(steps) - len(tail)], ctx) if ctx else []) + tail
        out = [self._follow_step(x, now, status == "working") for x in shown]
        if ctx:
            self._follow_agents(s, None, shown, out, now, ctx, 0, tail[0]["ts0"])
            follow_branches(out)
        intents, first_n, total_n = self._follow_intents(steps, gs, shown, out)
        return {"id": s.id, "short": s.id[:8], "project": self._project(s.project_raw),
                "goal": s.goal["name"] if s.goal else None, "goal_source": s.goal["source"] if s.goal else None,
                "status": "ended" if self.ended(s, now) else FOLLOW_STATUS.get(status, status),
                "cw": round(s.totals["cw"], 1), "usd": round(s.totals["usd"], 6), "turns": s.totals["turns"],
                "started": s.started, "last_ts": s.last_ts, "seq": s.seq, "steps_n": len(steps),
                "agents_running": sum(ctx["active"].values()) + len(ctx["pending"]) if ctx else 0,
                "now": w, "steps": out, "intents": intents, "intents_before": first_n, "intents_n": total_n,
                **self._tile_alerts(s, now)}

    def _follow_intents(self, steps, gs, shown, rows):
        """The intents (steps.intent_blocks) the shown steps of a tile or branch belong to, oldest first, each row
        tagged with its block ("intent": index in that list): the tile shows each intent's header, "+N other steps"
        and only its latest step. Returns (blocks, blocks before the first shown one, all blocks)."""
        blocks = stepsmod.intent_blocks(steps, gs)
        at = {i: k for k, b in enumerate(blocks) for i in range(b["first"], b["last"] + 1)}
        used, idx = [], {}
        for x, r in zip(shown, rows):
            k = at.get(x["i"])
            if k is None:
                continue
            if k not in idx:
                idx[k] = len(used)
                used.append(k)
            r["intent"] = idx[k]
        out = [dict(blocks[k], label=self._lab(blocks[k]["label"]) if blocks[k]["kind"] != "pending"
                    else blocks[k]["label"]) for k in used]
        return out, used[0] if used else 0, len(blocks)

    def _tile_alerts(self, s, now):
        """A follow tile's alerts: alert_counts, and "alert_steps": {main step: worst severity of its alerts not
        acknowledged} (a ▲ on the step, and where a jump to the alert scrolls), and "items": the alerts themselves
        (at most one per kind, cost / off-task / looping order; acknowledged ones too, with their state), drawn as a
        block at the top of the tile: severity, rule, message, count, latest step and the earlier triggers (the
        last TILE_TRIGGERS)."""
        al = self.alerts_public(s, now)
        marks = {}
        for a in al:
            if a["state"] == "new" and a["agent"] is None and a["step"] is not None:
                k = str(a["step"])
                if alertsmod.SEV_RANK.get(a["severity"], 0) >= alertsmod.SEV_RANK.get(marks.get(k), -1):
                    marks[k] = a["severity"]
        keys = ("id", "kind", "rule", "severity", "message", "count", "step", "first", "agent", "state", "raised",
                "active")
        order = {k: n for n, k in enumerate(TILE_KINDS)}
        items = [dict({k: a.get(k) for k in keys},
                      triggers=[{k: t.get(k) for k in ("rule", "severity", "step", "first", "agent", "message")}
                                for t in (a.get("triggers") or [])[-TILE_TRIGGERS:]])
                 for a in sorted(al, key=lambda a: order.get(a["kind"], len(order)))]
        return {"alerts": dict(self.alert_counts(al), items=items), "alert_steps": marks}

    def _follow_early(self, s, early, ctx):
        """The steps before a tile's latest ones that launched a subagent still active (a background agent the main
        thread kept working past): shown too, so its branch stays pinned in the tile."""
        ids = {f["tool_use_id"] for f in ctx["pending_by"].get(None, ())}
        for a in ctx["kids"].get(None, ()):
            if ctx["active"].get(a["id"]):
                ids.update(x for x in (a.get("tool_use_id"), ctx["wf"].get(a["id"])) if x)
        if not early or not ids:
            return []
        hit = []
        for x in reversed(early):
            tids = {t.get("id") for k in range(x["turn_first"], x["turn_last"] + 1)
                    for t in (s.main.turn_steps[k]["tools"] if 0 <= k < len(s.main.turn_steps) else ())}
            if tids & ids:
                hit.append(x)
                ids -= tids
                if not ids:
                    break
        return hit[::-1]

    def _follow_step(self, x, now, ticking):
        return {"i": x["i"], "cat": x["cat"], "label": self._lab(x["label"]), "cw": x["cw"], "usd": x["usd"],
                "open": not x["closed"], "tested": x["tested"],
                "dur_s": stepsmod.duration(x["ts0"], now if not x["closed"] and ticking else x["ts1"])}

    def _follow_ctx(self, s, now):
        """What one follow tile's subagent branches need, computed once per tile: who launched whom, which agents
        are active, subtree costs, workflow runs and launches without a transcript yet."""
        kids = {}
        for a in s.agents.values():
            kids.setdefault(self._parent(s, a), []).append(a)
        pending = {}
        for f in self._pending_launches(s, now):
            hit = s.tools.get(f["tool_use_id"])
            pending.setdefault(None if not hit or hit[2] is s.main else s.branch_ids.get(id(hit[2])), []).append(f)
        wf = {a["id"]: tid for tid, ags in self.workflow_agents(s).items() for a in ags} if s.wf_calls else {}
        return {"kids": kids, "pending": [f for fs in pending.values() for f in fs], "pending_by": pending, "wf": wf,
                "active": {a["id"]: self.agent_active(s, a, now) for a in s.agents.values()},
                "cost": self.tree_cost(s), "budget": FOLLOW_AGENTS}

    def _follow_now(self, s, now, ctx):
        """The call running now: the main thread's, or while it only waits on subagents (its turn ended, or its
        latest call is an Agent call), the latest call of the most recently active subagent ({"agent": its type})."""
        w = self._now(s, now)
        main = {"tool": w["tool"], "target": w["target"], "since_s": w["since_s"]} if w else None
        live = [a for a in s.agents.values() if ctx and ctx["active"].get(a["id"])]
        tool = s.main.turn_steps[-1]["tools"][-1]["name"] if s.main.turn_steps and s.main.turn_steps[-1]["tools"] else None
        if not live or (status_of(now, s.last_ts, s.last_kind, s.last_active_ts, s.hook) == "working"
                        and tool not in ("Agent", "Task", "Workflow")):
            return main
        a = max(live, key=lambda a: (a.get("last_ts") or 0, a["id"]))
        turns = s.branches[a["id"]].turn_steps
        x = turns[-1]["tools"][-1] if turns and turns[-1]["tools"] else None
        if x is None:
            return main
        return {"tool": x["label"], "target": x.get("target"), "agent": a["type"] or "agent",
                "since_s": round(max(0.0, now - x["ts"]), 1) if x.get("ts") else None}

    def _follow_agents(self, s, aid, steps, out, now, ctx, depth, since=None):
        """Attach to each shown step of a branch (main: aid None) the subagents it launched ("agents": active ones
        first within the tile's budget, shown in launch order; "agents_more": the rest; steps launching active
        ones get the budget first, then the newest). A subagent is matched to a step by its Agent/Task call
        (workflow agents: the Workflow call), else to the last step started before its first record (not before
        `since`, the first of the latest steps shown)."""
        children = ctx["kids"].get(aid, ())
        pend = ctx["pending_by"].get(aid, [])
        if not steps or not (children or pend):
            return
        br = self._branch(s, aid)
        at = {}
        for x in steps:
            for k in range(x["turn_first"], x["turn_last"] + 1):
                for t in br.turn_steps[k]["tools"] if 0 <= k < len(br.turn_steps) else ():
                    if t.get("id"):
                        at.setdefault(t["id"], x["i"])
        first_i = steps[0]["i"]
        by_step = {}
        for a in children:
            i = at.get(a["tool_use_id"])
            i = at.get(ctx["wf"].get(a["id"])) if i is None else i
            if i is None:
                t0 = a.get("first_ts") or 0
                since = steps[0]["ts0"] if since is None else since
                if t0 and since and t0 < since:
                    continue   # launched by an earlier step than the ones shown
                i = next((x["i"] for x in reversed(steps) if x["ts0"] and x["ts0"] <= t0), steps[-1]["i"])
            by_step.setdefault(i, []).append(("a", a))
        for f in pend:
            i = at.get(f["tool_use_id"])
            if i is not None and i >= first_i:
                by_step.setdefault(i, []).append(("f", f))
        rows = {x["i"]: r for x, r in zip(steps, out)}
        def busy(i):
            return any(c[0] == "f" or ctx["active"].get(c[1]["id"]) for c in by_step[i])
        for i in sorted(by_step, key=lambda i: (busy(i), i), reverse=True):   # active, then newest: the budget
            cands = by_step[i]
            rank = sorted(cands, key=lambda c: (c[0] == "a" and not ctx["active"].get(c[1]["id"]),
                                                -((c[1].get("first_ts") if c[0] == "a" else c[1].get("ts")) or 0)))
            take = rank[:max(0, ctx["budget"])]
            ctx["budget"] -= len(take)
            take.sort(key=lambda c: ((c[1].get("first_ts") if c[0] == "a" else c[1].get("ts")) or 0,
                                     str(c[1].get("id") or c[1].get("tool_use_id"))))
            row = rows[i]
            if take:
                row["agents"] = [self._follow_agent(s, c[1], now, ctx, depth) if c[0] == "a" else self._follow_pending(c[1])
                                 for c in take]
            if len(cands) > len(take):
                row["agents_more"] = len(cands) - len(take)

    def _follow_agent(self, s, a, now, ctx, depth):
        active = ctx["active"].get(a["id"], False)
        steps, gs = self.agent_steps(s, a["id"], now)
        shown = steps[-FOLLOW_AGENT_STEPS:]
        out = [self._follow_step(x, now, active) for x in shown]
        intents, _, intents_n = self._follow_intents(steps, gs, shown, out)
        st = a["status"]
        if st in ("running", "starting") and not active:
            st = "idle"   # not finished, but quiet (no write, no open call) for a while
        f = s.fanouts.get(a["tool_use_id"]) or {}
        cw, usd = ctx["cost"].get(a["id"], (a["cw"], a["usd"]))
        node = {"id": a["id"], "type": a["type"] or f.get("agent_type") or "agent",
                "description": None if self.redact else (a.get("description") or f.get("description")),
                "workflow": bool(a.get("workflow")), "status": "running" if active else st,
                "cw": round(cw, 1), "usd": round(usd, 6), "turns": a["turns"], "steps_n": len(steps), "steps": out,
                "intents": intents, "intents_n": intents_n}
        if depth + 1 < FOLLOW_DEPTH:
            self._follow_agents(s, a["id"], shown, out, now, ctx, depth + 1)
        return node

    def _follow_pending(self, f):
        """A launched subagent that has not written its transcript yet."""
        return {"id": None, "type": f.get("agent_type") or "agent",
                "description": None if self.redact else f.get("description"), "workflow": False,
                "status": "starting", "cw": 0.0, "usd": 0.0, "turns": 0, "steps_n": 0, "steps": [], "intents": [],
                "intents_n": 0}

    def follow(self, ids, now=None):
        """The follow view's payload for up to FOLLOW_MAX session ids (or unique prefixes): a tile each, in the
        order asked; an unknown id gets {"id", "missing": true}."""
        now = self.clock() if now is None else now
        out, seen = [], set()
        for q in ids:
            q = str(q or "").strip()
            if not q or q in seen:
                continue
            seen.add(q)
            s = self.find(q)
            out.append(self.follow_tile(s, now) if s is not None else {"id": q, "missing": True})
            if len(out) >= FOLLOW_MAX:
                break
        return {"now": now, "redacted": self.redact, "sessions": out}

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
            out.append(dict(self._agent_row(s, a, f), children=[self._agent_row(s, c, None) for c in self._children(s, a)]))
        return out

    def _agent_row(self, s, a, f):
        f = f or (s.fanouts.get(a["tool_use_id"]) if a else None) or {}
        return {"id": a["id"] if a else None, "type": (a and a["type"]) or f.get("agent_type") or "agent",
                "description": (a and a.get("description")) or f.get("description"),
                "status": a["status"] if a else "starting", "turns": a["turns"] if a else 0,
                "cw": round(a["cw"]) if a else 0, "parent": self._parent(s, a) if a else None}

    @staticmethod
    def _parent(s, a):
        """Agent id of the subagent whose Agent/Task call launched `a` (None when the main thread did)."""
        if a.get("parent_id"):
            return a["parent_id"]
        hit = s.tools.get(a.get("tool_use_id"))
        if not hit or hit[2] is s.main:
            return None
        return s.branch_ids.get(id(hit[2]))

    def _children(self, s, a):
        if not a:
            return []
        return [c for c in s.agents.values() if c is not a and self._parent(s, c) == a["id"]]

    def session_compact(self, s, now=None, recent_n=60, all_ops=False):
        """Small payload for the in-terminal mod / status line and the web session page (see docs/live-spec.md);
        `all_ops`: every intent step with its calls (?ops=all)."""
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
                "agents": [self._agent_row(s, a, None) for a in s.agents.values()],
                "main_tool_calls": sum(len(t["tools"]) for t in s.main.turn_steps),
                "intents": self.intents(s, now, all_ops=all_ops),
                "skills": [{"name": n, "count": c} for n, c in sorted(s.skills.items(), key=lambda kv: (-kv[1], kv[0]))],
                "alerts": self.alerts_public(s, now),
                **self._edited(s), **self._recap_key(s, now)}

    def session_flow(self, s, now=None):
        """The web canvas payload (/api/session/<id>/flow): the compact payload with every step's calls, the header
        (totals, flags, model), each main step's times, and "flow": one entry per subagent with its own intents and
        the step that launched it ({"agent": launching agent id or None for the main thread, "step": step index})."""
        now = self.clock() if now is None else now
        d = self.session_compact(s, now, all_ops=True)
        d["intents"] = self.intents(s, now, all_ops=True, flow=True)
        d["header"] = self._header(s, now)
        d["now_ts"] = now
        wf = {a["id"]: tid for tid, ags in self.workflow_agents(s).items() for a in ags} if s.wf_calls else {}
        out = []
        for a in sorted(s.agents.values(), key=lambda a: (a.get("first_ts") or 0, a["id"])):
            row = self._agent_row(s, a, None)
            f = s.fanouts.get(a["tool_use_id"]) or {}
            row.update(usd=round(a["usd"], 6), first_ts=a.get("first_ts"), last_ts=a.get("last_ts"),
                       launch=self._launch(s, a["tool_use_id"] or wf.get(a["id"]), now), launch_ts=f.get("ts"),
                       intents=self.intents(s, now, all_ops=True, aid=a["id"], flow=True))
            out.append(row)
        linked = {a["tool_use_id"] for a in s.agents.values()}
        for tid, f in s.fanouts.items():   # launched, no transcript yet
            if tid not in linked and not f.get("agent_id"):
                out.append({"id": "pending:" + str(tid)[-12:], "type": f.get("agent_type") or "agent",
                            "description": None if self.redact else f.get("description"), "status": "starting",
                            "turns": 0, "cw": 0, "usd": 0, "parent": None, "first_ts": f.get("ts"), "last_ts": f.get("ts"),
                            "launch": self._launch(s, tid, now), "launch_ts": f.get("ts"), "intents": None})
        d["flow"] = {"agents": out}
        return d

    def _launch(self, s, tid, now):
        """{"agent": id of the branch whose call `tid` was (None: main thread), "step": that branch's step index}."""
        hit = s.tools.get(tid)
        if not hit:
            return None
        turn, _, br = hit[0], hit[1], hit[2]
        aid = None if br is s.main else s.branch_ids.get(id(br))
        try:
            k = br.turn_steps.index(turn)
        except ValueError:
            return {"agent": aid, "step": None}
        steps, _ = self.steps(s, now) if aid is None else self.agent_steps(s, aid, now)
        st = next((x for x in steps if x["turn_first"] <= k <= x["turn_last"]), None)
        return {"agent": aid, "step": st["i"] if st else None}

    # ------------------------------------------------------------ alerts (tokenscope/alerts.py)
    @staticmethod
    def _thread_calls(branch, steps=None):
        """[alerts.Call] of one thread in order, each mapped to the step (of `steps`) it belongs to (step None
        without `steps`; .turn keeps (turn index, part) so the step can be found later, see _call_steps)."""
        at = {}
        for x in steps or ():
            for k in range(x["turn_first"], x["turn_last"] + 1):
                at.setdefault(k, []).append((x["i"], x.get("part_first") if k == x["turn_first"] else None,
                                             x.get("part_last") if k == x["turn_last"] else None))
        out = []
        for k, t in enumerate(branch.turn_steps):
            facts = t.get("_facts") or []
            parts = stepsmod.call_parts([x["name"] for x in t["tools"]])
            for f, j in zip(facts, parts):
                if f.get("kind", "skip") == "skip":
                    continue
                i = next((i for i, lo, hi in at.get(k, ()) if j is None or ((lo is None or j >= lo)
                                                                             and (hi is None or j <= hi))), None)
                out.append(alertsmod.Call(
                    n=len(out), step=i, tool=f["tool"], kind=f["kind"], key=f["key"], file=f.get("file"),
                    res=f.get("res"), err=f.get("fail"), poll=bool(f.get("poll") or f.get("apoll")), edits=f.get("edits") or (),
                    write=f.get("write"), words=f.get("words") or (), found=tuple(f.get("names") or ())))
        return out

    def _agent_alert_steps(self, s, got, now):
        """Subagent alerts point at call positions (their threads are not segmented for detection): segment only
        the subagents that have an alert and set first / step from the alert's call span."""
        for aid in {a.agent for a in got if a.agent is not None and a.span}:
            if aid not in s.branches:
                continue
            ast, _ = self.agent_steps(s, aid, now)
            calls = self._thread_calls(s.branches[aid], ast)
            for a in got:
                if a.agent == aid and a.span:
                    a.first = calls[a.span[0]].step if a.span[0] < len(calls) else None
                    a.step = calls[a.span[1]].step if a.span[1] < len(calls) else None

    def alert_context(self, s, now=None):
        """The alerts.Context of a session: main-thread steps (cost, todo / declared intent, request) and calls, the
        intent groups, and with looping.subagents each subagent's steps and calls."""
        now = self.clock() if now is None else now
        cfg = self.alerts_config
        steps, gs = self.steps(s, now) if s.main.turn_steps else ([], [])
        facts = []
        for x in steps:
            facts.append(alertsmod.StepFacts(
                i=x["i"], usd=x["usd"] or 0.0, cw=x["cw"] or 0.0, closed=x["closed"], turns=x["turns"] or 1,
                cat=x["cat"] or "other",
                group=next((n for n, g in enumerate(gs) if g["first"] <= x["i"] <= g["last"]), None),
                delegated=bool(x.get("agents") or x.get("wf_agents")), todo=x.get("todo"), declared=x.get("declared"),
                prompt=bisect.bisect_right(s.prompt_at, x["turn_first"])))
        ctx = alertsmod.Context(config=cfg, steps=facts, calls=self._thread_calls(s.main, steps),
                                groups=[{k: g[k] for k in ("first", "last", "usd", "cw", "cat")} for g in gs],
                                session_usd=s.totals["usd"], session_cw=s.totals["cw"],
                                prompt_words=list(s.prompt_words), names=s.alert_names)
        if cfg["looping"]["enabled"] and cfg["looping"]["subagents"]:
            for aid in s.agents:
                if s.branches[aid].turn_steps:
                    ctx.agents[aid] = (None, self._thread_calls(s.branches[aid]))
        return ctx

    def alerts(self, s, now=None):
        """[alerts.Alert] of a session, cached until its steps change."""
        now = self.clock() if now is None else now
        if s.main.turn_steps:
            self.steps(s, now)
        key = (s.seq, s.intents_cache[0][1] if s.intents_cache else None)   # new records, the open step closing
        if s.alerts_cache and s.alerts_cache[0] == key:
            return s.alerts_cache[1]
        got = alertsmod.run(self.alert_context(s, now)) if self.alerts_config.get("enabled", True) else []
        if s.agents:
            self._agent_alert_steps(s, got, now)
        s.alerts_cache = (key, got)
        return got

    def alerts_public(self, s, now=None, limit=100):
        """The "alerts" payload key: the session's alert history (alertstore records not dismissed, newest first, at
        most `limit`; one per kind), each the alert as alerts.collapse last made it (the latest trigger's rule,
        message, step and "reasons"; the worst severity; "count" and "triggers") with "active" (in the
        current run and active), "live" (in the current run), "state" (new / acked), "n", "ts" (first seen), "at"
        (its step's time) and "intent" ({"cat", "label"} of its step's intent). The current run is synced into the
        history first."""
        now = self.clock() if now is None else now
        cur = self._sync_alerts(s, now)
        live = {a["id"]: a for a in cur}
        out = []
        for r in self.alert_store.records([s.id])[:limit]:
            a = live.get(r["id"])
            d = {k: r.get(k) for k in ("id", "kind", "rule", "severity", "message", "step", "first", "agent",
                                       "reasons", "n", "ts", "at", "intent", "state", "raised", "triggers")}
            d["count"] = r.get("count") or 1
            d["details"] = (a or {}).get("details") or {}
            d["active"] = bool(a and a["active"])
            d["live"] = a is not None
            out.append(d)
        return out

    def _sync_alerts(self, s, now):
        """The session's current alerts (alerts.public, with "at" / "intent", collapsed to one per kind:
        alerts.collapse), synced into the alert history."""
        got = self.alerts(s, now)
        if not got:
            return []
        steps, gs = self.steps(s, now) if s.main.turn_steps else ([], [])
        live = {a["id"] for a in s.agents.values() if self.agent_active(s, a, now)}
        pub = alertsmod.public(got, steps[-1]["i"] if steps else None, live,
                               self.alerts_config.get("active_steps", 3))
        by_i = {x["i"]: x for x in steps}
        for a in pub:
            if a["agent"] is not None:
                ag = s.agents.get(a["agent"]) or {}
                a["at"] = ag.get("last_ts")
                a["intent"] = {"cat": "delegate", "label": "subagent " + str(ag.get("type") or "agent")}
                continue
            x = by_i.get(a["step"])
            if x is None:
                continue
            g = next((g for g in gs if g["first"] <= x["i"] <= g["last"]), None)
            a["at"] = x.get("ts1") or x.get("ts0")
            a["intent"] = {"cat": (g or x)["cat"], "label": alertsmod.scrub(self._lab((g or x)["label"]))}
        pub = alertsmod.collapse(pub, s.id)
        key = (s.seq, s.alerts_cache[0] if s.alerts_cache else None)
        if self.alert_synced.get(s.id) != key:
            self.alert_store.sync(s.id, pub, {"project": self._project(s.project_raw),
                                              "goal": s.goal["name"] if s.goal else None})
            self.alert_synced[s.id] = key
        return pub

    def sync_alerts(self, now=None):
        """Bring every session that changed since its last sync into the alert history (the server's poll loop:
        alerts are kept even while nobody watches)."""
        now = self.clock() if now is None else now
        for s in list(self.sessions.values()):
            k = self.alert_synced.get(s.id)
            if k is None or k[0] != s.seq:
                self._sync_alerts(s, now)
                self.alert_synced.setdefault(s.id, (s.seq, None))

    def alert_history(self, sids=None, states=("new", "acked"), limit=500, now=None):
        """GET /api/alerts: alert records across sessions (or `sids`), newest first, with the session's current
        project / goal labels when it is loaded, plus "sessions": per session with alerts, its alert_counts-like
        summary ({"new", "acked", "total", "by_kind" (new ones), "worst" (of the new ones)}), sessions needing
        attention (new alerts, worst first) first."""
        now = self.clock() if now is None else now
        self.sync_alerts(now)
        st = self.alert_store
        recs = st.records(sids, states)
        sess, out = {}, []
        for r in recs[:limit]:
            out.append(self._alert_row(r, sess))
        rows = []
        for sid in sorted({r["sid"] for r in st.records(sids, ("new", "acked"))}):
            s = self.sessions.get(sid)
            last = next((r for r in st.records([sid], ("new", "acked"))), None)
            rows.append(dict(st.summary(sid), id=sid, short=sid[:8],
                             project=self._project(s.project_raw) if s else (last or {}).get("project"),
                             goal=(s.goal["name"] if s and s.goal else None) if s else (last or {}).get("goal"),
                             loaded=s is not None, ended=self.ended(s, now) if s else True,
                             last_ts=(last or {}).get("ts")))
        rows.sort(key=lambda x: (-(x["new"] > 0), -alertsmod.SEV_RANK.get(x["worst"], -1), -x["new"],
                                 -(x["last_ts"] or 0)))
        return {"now": now, "redacted": self.redact, "alerts": out, "sessions": rows,
                "new": sum(x["new"] for x in rows), "version": st.version}

    def _alert_row(self, r, cache):
        sid = r["sid"]
        if sid not in cache:
            s = self.sessions.get(sid)
            cache[sid] = {"project": self._project(s.project_raw), "goal": s.goal["name"] if s.goal else None} \
                if s else {"project": r.get("project"), "goal": r.get("goal")}
        d = {k: r.get(k) for k in ("id", "sid", "n", "kind", "rule", "severity", "message", "step", "first", "agent",
                                   "reasons", "ts", "at", "intent", "state", "raised", "acked_ts", "updated",
                                   "triggers")}
        d.update(short=sid[:8], count=r.get("count") or 1, **cache[sid])
        return d

    # ------------------------------------------------------------ idle recap (recap.py)
    def _recap_key(self, s, now):
        """{"recap": ...} while the session waits for the user (or is idle) and there is work to recap, else {}."""
        if self.status(s, now) not in ("waiting", "idle"):
            return {}
        r = self.recap(s, now)
        return {"recap": r} if r else {}

    def recap_input(self, s):
        """Per-turn facts of the main thread and the subagents' full reads, in the shape recap.recap reads."""
        turns = [{"ts": t["ts"], "cw": t.get("cw") or 0, "calls": t.get("_facts") or [],
                  "tests": t.get("_tests") or []} for t in s.main.turn_steps]
        agents = []
        for aid, br in s.branches.items():
            reads, n = [], len(br.turn_steps)
            for k, t in enumerate(br.turn_steps):
                reads += [(c["file"], est_tokens(c.get("chars") or 0), n - 1 - k)
                          for c in t.get("_facts") or () if c.get("full")]
            agents.append({"id": aid, "ts": s.agents[aid].get("first_ts"), "reads": reads})
        ctx = {"now": s.context_tokens, "start": s.ctx_at_prompt[-1] if s.ctx_at_prompt else 0,
               "window": context_window(s.model, s.max_context), "headless": s.headless}
        return turns, agents, ctx

    def recap(self, s, now=None):
        """The idle recap of the work since the last user prompt (+ a session variant), redaction-aware; None
        before any work. Cached until the session, its step labels (model labels arrive late) or the cross-session
        index change."""
        steps, gs = self.steps(s, now)
        key = (s.intents_cache[0], getattr(self.xhints, "version", 0))
        if s.recap_cache and s.recap_cache[0] == key:
            return s.recap_cache[1]
        turns, agents, ctx = self.recap_input(s)
        name = (lambda p: self.redactor.path(p)) if self.redact else os.path.basename
        r = recapmod.recap(steps, gs, turns, s.prompt_at, agents, ctx, s.compact_at, name=name, lab=self._lab)
        if r and self.xhints is not None:
            try:
                h = self.xhints(s.id, s.project_raw, s.cwd, steps, gs, tuple(s.skills),
                                prompts=list(zip(s.prompt_at, s.prompt_words)))
            except Exception as e:  # noqa: BLE001 - the recap works without cross-session hints
                print("tokenscope live: cross-session hint: %r" % (e,), file=sys.stderr)
                h = None
            if h:
                r["hints"] = recapmod.with_cross(r["hints"], self._redact_hint(h))
        s.recap_cache = (key, r)
        return r

    def _redact_hint(self, h):
        """A cross-session hint's free text through the session redactor (file names, labels, paths)."""
        if not self.redact:
            return h
        h = dict(h)
        for k in ("action", "text", "evidence", "estimate", "seq", "stub_path"):
            if h.get(k):
                h[k] = self._lab(h[k])
        h["matches"] = [dict(m, label=self._lab(m.get("label") or "")) for m in h.get("matches") or ()]
        return h

    # ------------------------------------------------------------ steps and intent groups (docs/intents-spec.md)
    @staticmethod
    def workflow_agents(s):
        """tool_use_id of a main-thread Workflow call -> the workflow agents it launched. An agent belongs to the
        workflow run named by its transcript dir (subagents/workflows/wf_<id>/...); the run is matched to the call
        whose tool result reported that runId, else (no result yet / older transcripts) to the latest Workflow call
        at or before the agent's first record."""
        out = {}
        for a in s.agents.values():
            wf = a.get("workflow")
            if not wf:
                continue
            tid = s.wf_runs.get(wf)
            if tid is None and s.wf_calls:
                t0 = a.get("first_ts")
                before = [c for c in s.wf_calls if t0 is None or c[0] <= t0]
                tid = before[-1][1] if before else None
            if tid is not None:
                out.setdefault(tid, []).append(a)
        return out

    def segment_input(self, s, aid=None):
        """(turns, events) for steps.segment from the main thread, or the subagent `aid` (see steps.py)."""
        by_tool = {a["tool_use_id"]: a for a in s.agents.values() if a["tool_use_id"]}
        by_wf = self.workflow_agents(s) if s.wf_calls and aid is None else {}
        tree = self.tree_cost(s) if s.agents else {}
        turns = []
        for t in self._branch(s, aid).turn_steps:
            seg = t.get("_seg")
            if seg is None:
                seg = t["_seg"] = {"ts": t["ts"], "tools": t.get("_raw") or [], "thinking": t.get("_thinking", 0),
                                   "text": t.get("_text", "")}
            subs = [by_tool[x["id"]] for x in t["tools"] if x.get("id") in by_tool]
            wfs = [a for x in t["tools"] for a in by_wf.get(x.get("id"), ())]
            ends = [x["ts"] + x["dur_ms"] / 1000.0 for x in t["tools"] if x.get("ts") and x.get("dur_ms") is not None]
            seg.update(ts_end=max(ends) if ends else None, cw=t.get("cw") or 0, usd=t.get("usd") or 0, test=t.get("_test"),
                       sub_cw=sum(tree[a["id"]][0] for a in subs + wfs),
                       sub_usd=sum(tree[a["id"]][1] for a in subs + wfs),
                       wf_agents=len(wfs))
            turns.append(seg)
        if aid is not None:
            return turns, []
        events = [{"kind": "prompt", "at": k} for k in s.prompt_at]
        return turns, events + [dict(e, kind="skill") for e in s.slash_skills]

    def tree_cost(self, s):
        """agent id -> (cw, usd) of the agent and every subagent it launched (nested ones roll up into the step that
        launched the outermost)."""
        kids = {}
        for a in s.agents.values():
            kids.setdefault(self._parent(s, a), []).append(a)
        out = {}

        def walk(a, seen):
            if a["id"] in out:
                return out[a["id"]]
            cw, usd = a["cw"], a["usd"]
            for c in kids.get(a["id"], ()):
                if c["id"] not in seen:
                    x = walk(c, seen | {c["id"]})
                    cw, usd = cw + x[0], usd + x[1]
            out[a["id"]] = (cw, usd)
            return out[a["id"]]
        for a in s.agents.values():
            walk(a, {a["id"]})
        return out

    @staticmethod
    def _branch(s, aid):
        return s.main if aid is None else s.branches[aid]

    def agent_steps(self, s, aid, now=None):
        """Labelled steps + groups of one subagent's transcript (signal / rule labels: no model calls). Its last
        step stays open while the subagent runs."""
        a = s.agents[aid]
        lab = s.agent_labellers.get(aid)
        if lab is None:
            lab = s.agent_labellers[aid] = stepsmod.Labeller()
            lab.no_model = True
        open_last = a["status"] in ("running", "starting")
        key = (s.agent_rev.get(aid), open_last, len(lab.locked))   # its own records only: cheap to poll
        hit = s.agent_steps_cache.get(aid)
        if hit and hit[0] == key:
            return hit[1]
        turns, events = self.segment_input(s, aid)
        steps = stepsmod.segment(turns, events, open_last=open_last)
        gs = lab.assign(steps)
        s.agent_steps_cache[aid] = (key, (steps, gs))
        return steps, gs

    def steps(self, s, now=None, final=False):
        """Labelled steps + groups of the main thread. The last step stays open while the session works."""
        now = self.clock() if now is None else now
        st = self.status(s, now)
        open_last = not final and (st == "working" or (st == "idle" and s.last_ts and now - s.last_ts < WAITING_S
                                                         and s.last_kind != "end_turn"))
        key = (s.seq, open_last, len(s.labeller.locked),
               len(s.labeller.model), len(s.labeller.proposals), s.labeller.no_model)
        if s.intents_cache and s.intents_cache[0] == key:
            return s.intents_cache[1]
        turns, events = self.segment_input(s)
        steps = stepsmod.segment(turns, events, open_last=open_last)
        gs = s.labeller.assign(steps)
        s.intents_cache = (key, (steps, gs))
        return steps, gs

    def _summary(self, step):
        if not self.redact:
            return stepsmod.summary(step)
        tools = {}
        for name, n in (step.get("tools") or {}).items():
            if "." in name and " " not in name:   # "<server>.<tool>": private servers get a stable alias
                srv, op = name.split(".", 1)
                name = "%s.%s" % (self.redactor.server(srv), op)
            name = self.redactor.string(name)
            tools[name] = tools.get(name, 0) + n
        return stepsmod.summary(dict(step, tools=tools, runners=[self.redactor.string(r) for r in
                                                                  step.get("runners") or ()]))

    @staticmethod
    def _group_tools(steps, g):
        out = {}
        for x in steps[g["first"]:g["last"] + 1]:
            for k, n in x["tools"].items():
                out[k] = out.get(k, 0) + n
        return out

    def _lab(self, text):
        return self.redactor.string(text) if self.redact and text else text

    @staticmethod
    def _ops(s, step, now, working, last_n=12, branch=None):
        """The live step's calls, newest last (at most `last_n`): tool label, target (both redaction-aware, as in
        "now"), CW (the turn's, split over its calls), err (None while running), the test outcome of a run, and
        dur_s: tool_use to tool_result, or for a running call (while the session works) the time since it started.
        declare_intent calls are the label source, not work: never an op, their CW share goes to the turn's other
        calls."""
        out = []
        branch = branch or s.main
        for k in range(step["turn_first"], step["turn_last"] + 1):
            t = branch.turn_steps[k]
            tools = [x for x in t["tools"] if not stepsmod.is_declare(x["name"])]
            part = stepsmod.call_parts([x["name"] for x in t["tools"]])   # a turn split at its ToolSearch calls
            lo = step.get("part_first") if k == step["turn_first"] else None
            hi = step.get("part_last") if k == step["turn_last"] else None
            for x, j in zip(t["tools"], part):
                if x not in tools or (lo is not None and j < lo) or (hi is not None and j > hi):
                    continue
                op = {"tool": x["label"], "target": x.get("target"), "cw": round((t.get("cw") or 0) / len(tools), 1),
                      "err": x.get("err"), "dur_s": None}
                if x.get("dur_ms") is not None:
                    op["dur_s"] = round(x["dur_ms"] / 1000.0, 1)
                elif x.get("err") is None and working and x.get("ts"):
                    op["dur_s"] = round(max(0.0, now - x["ts"]), 1)
                if tool_skill(x["name"], {"skill": x.get("target")}):
                    op["skill"] = x.get("target")
                if x.get("test"):
                    op["test"] = x["test"]
                out.append(op)
        return out[-last_n:] if last_n else out

    def intents(self, s, now=None, last_n=40, all_ops=False, aid=None, flow=False):
        """The compact payload's "intents" key; `all_ops`: every step carries its calls (the web page shows them on
        an older step it expands), not only the live one. `aid`: a subagent's steps instead of the main thread's;
        `flow` (the web canvas): every step and call, plus each step's first / last event time (ts0 / ts1)."""
        now = self.clock() if now is None else now
        if aid is None:
            steps, gs = self.steps(s, now)
            working = self.status(s, now) == "working"
        else:
            steps, gs = self.agent_steps(s, aid, now)
            working = s.agents[aid]["status"] in ("running", "starting") and self.status(s, now) == "working"
        branch = self._branch(s, aid)
        if flow:
            last_n = len(steps) or 1
        # the live step, with its calls: the open one, or the last while the session works (a test run closes a
        # step before the next turn opens one)
        live = steps[-1] if steps and (not steps[-1]["closed"] or working) else None

        def dur(ts0, ts1, ticking):
            """Wall time first to last event (a skill-only step: to the end of the work it drove, steps.skill_spans);
            the live step, its group and a skill step driving it tick up to now while the session works."""
            return stepsmod.duration(ts0, now if ticking and ts0 else ts1)

        def drives(x):
            """x is the live step, or a skill step whose driven span (steps.skill_spans) reaches it."""
            return live is not None and (x is live or x["i"] <= live["i"] <= x.get("drive_last", -1))
        gi = {}
        for n, g in enumerate(gs):
            for i in range(g["first"], g["last"] + 1):
                gi[i] = n
        decl = [x for t in branch.turn_steps for x in t.get("_raw") or () if stepsmod.is_declare(x["name"])]
        return {"steps": [{"i": x["i"], "cat": x["cat"], "label": self._lab(x["label"]), "source": x["source"],
                           "locked": x["locked"], "open": not x["closed"], "cw": x["cw"], "usd": x["usd"],
                           "turns": x["turns"], "summary": self._summary(x), "tested": x["tested"],
                           "group": gi.get(x["i"]), "skills": x["skills"],
                           "dur_s": dur(x["ts0"], x.get("drive_ts1") or x["ts1"], working and drives(x)),
                           **({"ops": self._ops(s, x, now, working and x is live, last_n=0 if flow else 12,
                                                branch=branch)} if x is live or all_ops else {}),
                           **({"ts0": x["ts0"], "ts1": x.get("drive_ts1") or x["ts1"]} if flow else {})}
                          for x in steps[-last_n:]],
                "groups": [{"label": self._lab(g["label"]), "source": g["source"], "first": g["first"],
                            "last": g["last"], "cat": g["cat"], "cw": g["cw"], "usd": g["usd"], "steps": g["steps"],
                            "skills": g["skills"],
                            "dur_s": dur(g["ts0"], g["ts1"], working and any(
                                drives(x) for x in steps[g["first"]:g["last"] + 1])),
                            "summary": self._lab(g["summary"]) if not self.redact else
                            self._summary({"tools": self._group_tools(steps, g), "thinking": 0, "skills": g["skills"],
                                           "wf_agents": sum(x.get("wf_agents") or 0
                                                            for x in steps[g["first"]:g["last"] + 1])})} for g in gs],
                "declared": {"calls": len(decl), "steps": sum(1 for x in steps if x["declared"])},
                "labeller": dict(s.labeller.stats, pending=len(s.labeller.pending(steps))) if aid is None else {}}

    def label_requests(self, now=None, min_pending=3):
        """[(session id, steps batch, goal)] for the model labeller: >= min_pending closed unlabelled steps, or
        any when the session is waiting for the user or idle. Marks the batch in flight."""
        now = self.clock() if now is None else now
        out = []
        for s in self.sessions.values():
            if not s.main.turn_steps:
                continue
            steps, _ = self.steps(s, now)
            pend = s.labeller.pending(steps)
            # a session that waits or went idle gets its last few steps labelled too (a server started on a finished
            # session would otherwise lock rule labels the live pane's server replaced with model labels)
            if pend and (len(pend) >= min_pending or self.status(s, now) in ("waiting", "idle")):
                batch = pend[:LABEL_BATCH]
                s.labeller.inflight.update(x["i"] for x in batch)
                out.append((s.id, batch, s.goal["name"] if s.goal else None))
        return out

    def add_step_labels(self, sid, idx, res):
        s = self.sessions.get(sid)
        if s is None:
            return
        s.labeller.release(idx)
        s.labeller.add_model_labels(res.get("labels"), res.get("groups"), res.get("usage"))
        if not res.get("labels"):
            s.labeller.give_up(idx)   # no answer: lock their rule labels rather than asking again forever
        self.version += 1

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
        # the latest call says what is happening now ("Edit + run tests" in one turn is running tests by now)
        it = tool.get("intent") or last["intent"]
        t0 = tool.get("ts") or last["ts"]
        return {"intent": it, "label": intents.label(it), "group": intents.GROUP.get(it, last["group"]),
                "tool": tool.get("label"), "target": tool.get("target"),
                "since_s": round(max(0.0, now - t0), 1) if t0 else None,
                "phase_s": round(max(0.0, now - turns[i]["ts"]), 1) if turns[i]["ts"] else None,
                "status": self.status(s, now)}

    @staticmethod
    def _tests(s):
        """Main-thread test runs (subagents often run only their own new test file, often red on purpose);
        subagent runs only when the main thread has run none, marked scope="subagents"."""
        main = [t for t in s.tests if t.get("main", True)]
        runs, scope = (main, "main") if main else (s.tests, "subagents")
        if not runs:
            return None
        out = LiveState._tests_of(runs)
        out["scope"] = scope
        return out

    @staticmethod
    def _tests_of(tests):
        last = tests[-1]
        if last["passed"] is None and last["failed"] is None:
            return {"passed": None, "failed": None, "ok": False, "runs": len(tests), "last_ts": last["ts"],
                    "trend": [t["failed"] for t in tests[-8:]]}
        return {"passed": last["passed"], "failed": last["failed"], "ok": last["ok"], "runs": len(tests),
                "last_ts": last["ts"], "trend": [t["failed"] for t in tests[-8:]],
                "passed_trend": [t["passed"] for t in tests[-8:]]}

    def _edited(self, s):
        if self.redact:
            return {"files_edited_n": len(s.edited)}
        return {"files_edited": sorted({os.path.basename(p) for p in s.edited}), "files_edited_n": len(s.edited)}
