"""Discover and parse Claude Code transcripts (tolerant, stdlib only)."""
import hashlib
import json
import os
import pickle
import re
from collections import Counter
from datetime import datetime, timezone

from . import __version__

DEFAULT_ROOT = os.path.join(os.path.expanduser("~"), ".claude", "projects")
CACHE_FORMAT = 2  # bump when compact_event changes shape
UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
NOISE_PREFIXES = ("<command-", "<local-command", "<system-reminder>", "Caveat:", "<bash-", "<user-memory-input>",
                  "[Request interrupted", "<SYSTEM_MESSAGE>", "The following is a <SYSTEM_MESSAGE>")


def resolve_root(root=None, harness=None):
    if root:
        return os.path.expanduser(root)
    if os.environ.get("TOKENSCOPE_ROOT"):
        return os.path.expanduser(os.environ["TOKENSCOPE_ROOT"])
    from .harness import get_harness
    return get_harness(name=harness).default_root()


def parse_ts(s):
    if not isinstance(s, str) or not s:
        return None
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def read_jsonl(path):
    """Yield dicts from a jsonl file, skipping blank/bad lines (streamed, never a whole-file read)."""
    try:
        f = open(path, "rb")
    except OSError:
        return
    with f:
        for line in f:
            if _skip_line(line):
                continue
            try:
                obj = json.loads(line)
            except ValueError:
                continue
            if isinstance(obj, dict):
                yield obj


# Line types nothing downstream reads; recognised from the first bytes so the line is never json-decoded.
SKIP_TYPES = ("file-history-snapshot", "queue-operation", "last-prompt", "atis-latch", "summary", "progress")
_SKIP_PREFIXES = tuple(('{"type":"%s"' % t).encode() for t in SKIP_TYPES)
KEEP_TYPES = {"user", "assistant", "attachment", "system", "result", "cost-state"}


def _skip_line(line):
    line = line.lstrip()
    return not line or line.startswith(_SKIP_PREFIXES)


# ---------------------------------------------------------------- compaction
# Events are reduced to what analyze/understand/goals read; large text is cut to a head (+ a tail for Bash, which
# test-summary parsing needs) and the original size is kept in `_chars` so token estimates are unchanged.
HEAD_CHARS = 300
BASH_TAIL_CHARS = 3000
OTHER_TAIL_CHARS = 200
TEXT_KEEP_CHARS = 1000      # assistant text blocks
INPUT_KEEP_CHARS = 4000     # string values inside tool_use inputs (Write content, Edit strings, Agent prompts)
EVENT_KEYS = ("type", "subtype", "uuid", "timestamp", "cwd", "gitBranch", "isSidechain", "agentId", "isMeta",
              "isCompactSummary", "message", "toolUseResult", "attachment", "compactMetadata", "entrypoint",
              "userType", "promptSource", "turnOrigin", "origin", "modelUsage", "totalCostUSD", "sessionId")


def _cut(text, head, tail):
    if len(text) <= head + tail + 40:
        return text
    return text[:head] + "\n[... tokenscope: %d chars omitted ...]\n" % (len(text) - head - tail) + \
        (text[-tail:] if tail else "")


def _compact_input(inp):
    if isinstance(inp, dict):
        return {k: _compact_input(v) for k, v in inp.items()}
    if isinstance(inp, list):
        return [_compact_input(v) for v in inp]
    if isinstance(inp, str) and len(inp) > INPUT_KEEP_CHARS:
        return _cut(inp, INPUT_KEEP_CHARS - 200, 200)
    return inp


def compact_event(ev, tool_names):
    """Smaller copy of a transcript event (None if irrelevant). tool_names: tool_use id -> name, filled as we go."""
    typ = ev.get("type")
    if typ not in KEEP_TYPES:
        return None
    out = {k: ev[k] for k in EVENT_KEYS if k in ev}
    msg = ev.get("message")
    if typ == "assistant" and isinstance(msg, dict):
        blocks = []
        for b in msg.get("content") or []:
            if not isinstance(b, dict):
                continue
            bt = b.get("type")
            if bt == "text":
                t = str(b.get("text", ""))
                nb = {"type": "text", "text": t[:TEXT_KEEP_CHARS]}
                if len(t) > TEXT_KEEP_CHARS:
                    nb["_chars"] = len(t)
            elif bt == "thinking":
                nb = {"type": "thinking", "thinking": "", "_chars": len(str(b.get("thinking", "")))}
            elif bt == "tool_use":
                nb = {"type": "tool_use", "id": b.get("id"), "name": b.get("name"),
                      "input": _compact_input(b.get("input"))}
                tool_names[b.get("id")] = b.get("name")
            else:
                continue
            blocks.append(nb)
        out["message"] = {k: msg[k] for k in ("id", "model", "usage", "stop_reason", "role") if k in msg}
        out["message"]["content"] = blocks
    elif typ == "user" and isinstance(msg, dict):
        content = msg.get("content")
        nm = {"role": msg.get("role")}
        if ev.get("isCompactSummary"):
            t = content_text(content)
            nm["content"], nm["_chars"] = t[:HEAD_CHARS], len(t)
        elif isinstance(content, list):
            blocks = []
            for b in content:
                if isinstance(b, dict) and b.get("type") == "tool_result":
                    c = b.get("content")
                    t = content_text(c)
                    images = sum(1 for x in c if isinstance(x, dict) and x.get("type") == "image") \
                        if isinstance(c, list) else 0
                    tail = BASH_TAIL_CHARS if tool_names.get(b.get("tool_use_id")) == "Bash" else OTHER_TAIL_CHARS
                    nb = {"type": "tool_result", "tool_use_id": b.get("tool_use_id"), "is_error": b.get("is_error"),
                          "content": _cut(t, HEAD_CHARS, tail), "_chars": len(t)}
                    if images:
                        nb["_images"] = images
                    blocks.append(nb)
                elif isinstance(b, dict) and b.get("type") == "text":
                    blocks.append({"type": "text", "text": str(b.get("text", ""))})
                elif isinstance(b, str):
                    blocks.append(b)
            nm["content"] = blocks
        else:
            nm["content"] = content
        out["message"] = nm
        tur = ev.get("toolUseResult")
        if isinstance(tur, str):
            out["toolUseResult"] = tur[:HEAD_CHARS]
        elif isinstance(tur, dict):
            out["toolUseResult"] = {"persistedOutputSize": tur.get("persistedOutputSize"),
                                    "_stdout_chars": len(str(tur.get("stdout") or ""))}
        elif "toolUseResult" in out:
            del out["toolUseResult"]
    return out


def read_events(path):
    """Compacted events of one transcript file (see compact_event)."""
    from .harness import get_harness
    return get_harness(path=path).read_events(path)


# ---------------------------------------------------------------- disk cache
CACHE_DIR = os.path.join(os.path.expanduser("~"), ".cache", "tokenscope", "parsed")
CACHE = {"enabled": True, "dir": CACHE_DIR}
STATS = {"hits": 0, "misses": 0, "transcripts": 0}


def set_cache(enabled=True, directory=None):
    CACHE["enabled"] = enabled
    if directory:
        CACHE["dir"] = directory


def _cache_key(path):
    st = os.stat(path)
    return (os.path.abspath(path), st.st_size, st.st_mtime_ns, __version__, CACHE_FORMAT)


def load_events(path):
    """read_events(path) through the pickle cache keyed by (abs path, size, mtime_ns, version)."""
    STATS["transcripts"] += 1
    if not CACHE["enabled"]:
        return read_events(path)
    try:
        key = _cache_key(path)
    except OSError:
        return []
    cp = os.path.join(CACHE["dir"], hashlib.sha1(key[0].encode("utf-8")).hexdigest() + ".pkl")
    try:
        with open(cp, "rb") as f:
            k, events = pickle.load(f)
        if k == key:
            STATS["hits"] += 1
            return events
    except (OSError, pickle.PickleError, EOFError, ValueError, TypeError, AttributeError):
        pass
    STATS["misses"] += 1
    events = read_events(path)
    try:
        os.makedirs(CACHE["dir"], exist_ok=True)
        tmp = "%s.%d.tmp" % (cp, os.getpid())
        with open(tmp, "wb") as f:
            pickle.dump((key, events), f, protocol=pickle.HIGHEST_PROTOCOL)
        os.replace(tmp, cp)
    except OSError:
        pass
    return events


def read_json(path):
    try:
        with open(path, encoding="utf-8") as f:
            obj = json.load(f)
        return obj if isinstance(obj, dict) else {}
    except (OSError, ValueError):
        return {}


def content_text(content):
    """Flatten message/tool_result content (str or list of blocks) to text."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for b in content:
            if isinstance(b, dict) and b.get("type") == "text":
                parts.append(str(b.get("text", "")))
            elif isinstance(b, str):
                parts.append(b)
        return "".join(parts)
    return ""


def real_prompt_text(ev):
    """Return the prompt text if this is a real user prompt line, else None."""
    if ev.get("type") != "user" or ev.get("isMeta") or ev.get("isCompactSummary"):
        return None
    msg = ev.get("message")
    if not isinstance(msg, dict):
        return None
    content = msg.get("content")
    if isinstance(content, list):
        if any(isinstance(b, dict) and b.get("type") == "tool_result" for b in content):
            return None
    text = content_text(content).strip()
    if not text or text.startswith(NOISE_PREFIXES):
        return None
    return text


# ---------------------------------------------------------------- request source
# A request (real prompt) is "human" unless one of these conservative rules says it was automated:
AUTOMATED_PREFIXES = ("<task-notification>", "<system-reminder>", "[SYSTEM", "<command-", "<local-command",
                      "<bash-", "<user-memory-input>", "<scheduled-task", "<hook-",
                      "<SYSTEM_MESSAGE>", "The following is a <SYSTEM_MESSAGE>")
HEADLESS_ENTRYPOINTS = ("sdk",)  # entrypoint sdk-cli / sdk-ts / sdk-py = `claude -p` or an SDK program
SOURCES = ("human", "notification", "hook", "subagent", "headless")
SOURCE_RULES = [
    "subagent: the prompt is in a subagent transcript (spawned by Agent/Task or a workflow), or isSidechain is set",
    "notification: the text starts with %s (task/agent notifications, reminders, local command output)"
    % ", ".join(AUTOMATED_PREFIXES),
    "hook: the event carries an `origin` that is not the user (hook / scheduler / queue injected prompt)",
    "headless: entrypoint starts with 'sdk' (claude -p or an SDK program) or promptSource is 'sdk'; a person typing "
    "`claude -p` looks the same, so these count as automated only because no human REPL was involved",
    "human: everything else (interactive REPL prompts)",
    "queue-operation lines are not used: in the REPL a prompt typed while Claude is busy is queued too",
]
_HUMAN_ORIGINS = ("user", "human", "repl", "keyboard", "cli")


def prompt_source(ev, text, in_subagent=False):
    """Source of a real prompt (see SOURCE_RULES)."""
    if in_subagent or ev.get("isSidechain"):
        return "subagent"
    if (text or "").lstrip().startswith(AUTOMATED_PREFIXES):
        return "notification"
    origin = ev.get("origin")
    if isinstance(origin, dict):
        origin = origin.get("kind") or origin.get("type") or origin.get("source")
    if origin and str(origin).lower() not in _HUMAN_ORIGINS:
        return "hook"
    if str(ev.get("entrypoint") or "").startswith(HEADLESS_ENTRYPOINTS) or ev.get("promptSource") == "sdk":
        return "headless"
    return "human"


def is_automated(source):
    return source != "human"


def parse_journal(path, wf_id):
    """Counts/labels/phases of a workflow journal.jsonl (result payloads are never kept)."""
    w = {"id": wf_id, "launched": 0, "started": 0, "results": 0, "failed": 0, "phases": Counter(), "agents": []}
    for ev in read_jsonl(path):
        t = ev.get("type")
        if t == "launched":
            w["launched"] += 1
        elif t == "started":
            w["started"] += 1
            if ev.get("phase"):
                w["phases"][str(ev["phase"])] += 1
            if ev.get("agentId") and ev["agentId"] not in w["agents"]:
                w["agents"].append(ev["agentId"])
        elif t == "result":
            w["results"] += 1
        elif t == "failed":
            w["failed"] += 1
            if ev.get("agentId"):
                w.setdefault("failed_agents", []).append(ev["agentId"])
    return w


def _workflow_of(path, sub_dir):
    rel = os.path.relpath(os.path.dirname(path), sub_dir)
    wf = [p for p in rel.split(os.sep) if p.startswith("wf_")]
    return wf[-1] if wf else None


def subagent_files(sub_dir):
    """(transcripts, journals) under <session>/subagents/** at any depth, sorted."""
    files, journals = [], []
    for d, dirs, names in os.walk(sub_dir):
        dirs.sort()
        for name in sorted(names):
            if not name.endswith(".jsonl"):
                continue
            (journals if name == "journal.jsonl" else files).append(os.path.join(d, name))
    return files, journals


class Session:
    def __init__(self, path, slug, harness=None):
        self.path = path
        self.slug = slug
        self.harness = harness
        if path.endswith(".jsonl"):
            if os.path.basename(path) == "transcript.jsonl":
                parts = os.path.normpath(path).split(os.sep)
                if len(parts) >= 4 and parts[-2] == "logs" and parts[-3] == ".system_generated":
                    self.id = parts[-4]
                else:
                    self.id = os.path.basename(os.path.dirname(path))
            else:
                self.id = os.path.basename(path)[:-len(".jsonl")]
        else:
            self.id = os.path.basename(path)
        self.events = []
        self.subagents = []  # dicts: agent_id, path, meta, events, workflow_id, agent_type, description, ...
        self.workflows = []  # dicts: id, launched, started, results, failed, phases (Counter), agents
        self.cwd = None
        self.git_branch = None
        self.models = []
        self.start = None
        self.end = None
        self.first_prompt = None

    @property
    def project(self):
        if self.cwd:
            return os.path.basename(self.cwd.rstrip("/")) or self.cwd
        return self.slug

    @property
    def sub_dir(self):
        if os.path.basename(self.path) == "transcript.jsonl":
            return os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(self.path))), "subagents")
        return os.path.join(os.path.dirname(self.path), self.id, "subagents")

    def load(self):
        self.events = load_events(self.path)
        times = []
        for ev in self.events:
            if self.cwd is None and isinstance(ev.get("cwd"), str):
                self.cwd = ev["cwd"]
            if self.git_branch is None and ev.get("gitBranch"):
                self.git_branch = ev["gitBranch"]
            t = parse_ts(ev.get("timestamp"))
            if t:
                times.append(t)
            if ev.get("type") == "assistant":
                m = (ev.get("message") or {}).get("model")
                if m and m not in self.models and not m.startswith("<"):
                    self.models.append(m)
            if self.first_prompt is None:
                self.first_prompt = real_prompt_text(ev)
        if times:
            self.start, self.end = min(times), max(times)
        sub_dir = self.sub_dir
        if os.path.isdir(sub_dir):
            files, journals = subagent_files(sub_dir)
            for p in files:
                base = os.path.basename(p)[:-len(".jsonl")]
                meta = read_json(os.path.join(os.path.dirname(p), base + ".meta.json"))
                events = load_events(p)
                agent_id = next((e.get("agentId") for e in events if e.get("agentId")), None) \
                    or (base[len("agent-"):] if base.startswith("agent-") else base)
                self.subagents.append({
                    "agent_id": agent_id, "path": p, "meta": meta, "events": events,
                    "workflow_id": _workflow_of(p, sub_dir), "agent_type": meta.get("agentType"),
                    "description": meta.get("description"), "workflow_phase": meta.get("workflowPhase"),
                    "spawn_depth": meta.get("spawnDepth")})
            for p in journals:
                self.workflows.append(parse_journal(p, _workflow_of(p, sub_dir) or os.path.basename(os.path.dirname(p))))
        return self

    def prompt_sources(self):
        """Counter of request sources over main + subagent transcripts."""
        c = Counter()
        for ev in self.events:
            text = real_prompt_text(ev)
            if text:
                c[prompt_source(ev, text)] += 1
        for sa in self.subagents:
            for ev in sa["events"]:
                if real_prompt_text(ev):
                    c["subagent"] += 1
        return c


def discover(root=None, harness=None):
    """Return unloaded Session objects for every main transcript under root."""
    from .harness import get_harness
    h = get_harness(name=harness, root=root)
    resolved_root = resolve_root(root, harness=h.name)
    found = h.discover(resolved_root)
    if not found and root is None and h.name != "antigravity":
        ag = get_harness("antigravity")
        if ag.is_available():
            found = ag.discover()
    return found


def load_sessions(root=None, harness=None):
    return [s.load() for s in discover(root, harness=harness)]


def find_session(prefix, root=None, harness=None):
    """Find a session by id or unique id prefix. Raises LookupError."""
    matches = [s for s in discover(root, harness=harness) if s.id.startswith(prefix)]
    exact = [s for s in matches if s.id == prefix]
    if exact:
        matches = exact
    if not matches:
        raise LookupError("no session matches %r" % prefix)
    if len(matches) > 1:
        raise LookupError("ambiguous prefix %r: %s" % (prefix, ", ".join(s.id for s in matches[:10])))
    return matches[0].load()
