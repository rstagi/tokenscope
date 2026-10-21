"""Alerts: cost, off-task and looping detectors over one session's steps and tool calls (pure, no I/O except
reading the config file).

Every detector reads one `Context` (built by tokenscope.live.state.LiveState.alert_context from the live model, or
by hand in tests) and returns `Alert`s:

    cost      session_usd   the session's spend passed `cost.session_usd`
              intent_usd    one intent (a canvas node: an intent group, else a step) passed `cost.intent_usd`
              jump          one step cost far more than the session's own rolling baseline: median / MAD of the
                            previous `cost.jump_window` steps (after `cost.jump_warmup` of them), ratio and robust z
                            thresholds, and an absolute floor so tiny steps never fire
    off_task  unrelated     opt-in: recent steps touch files whose path words share nothing with the active intent
                            (declared intent, in-progress todo item, else the request's prompt words)
    looping   repeat_read / repeat_search / repeat_command / repeat_call
                            the same call with the same result again and again (no edit to the file / no edit at all
                            in between); polling (sleep, background task output) is ignored by default
              retry         the same call failing again and again with no edit in between
              edit_revert   an edit undone (A->B, then B->A; a file written back to an earlier content)

Privacy: an alert's message and details never carry a path, file name, command, pattern, prompt text, user name,
e-mail or key. Files and commands are named by kind and an ordinal per session ("file #2", "command #1"); file
extensions show only as ".py" (as the redacted live views do). `scrub()` runs over every string as a last guard.

Pluggable detectors: anything with a `name` and `detect(ctx) -> list[Alert]` (the `Detector` protocol) can be added
with `register(name, factory)`; `factory(config)` returns the detector. See `HaikuOffTaskDetector` for where a
model-based off-task check would go.
"""
import copy
import hashlib
import json
import math
import os
import re
from dataclasses import dataclass, field
from statistics import median
from typing import Callable, Optional, Protocol

from .xindex import STOP, _stem

KINDS = ("cost", "off_task", "looping")
SEVERITIES = ("info", "warning", "critical")

# ---------------------------------------------------------------- config

DEFAULTS = {
    "enabled": True,
    "active_steps": 3,
    "max": 50,
    "cost": {
        "enabled": True,
        "session_usd": 25.0,
        "intent_usd": 5.0,
        "jump": True,
        "jump_metric": "usd",
        "jump_window": 20,
        "jump_warmup": 6,
        "jump_ratio": 4.0,
        "jump_z": 3.5,
        "jump_max_ratio": 1.5,
        "jump_floor_usd": 0.75,
        "jump_floor_cw": 250_000,
        "jump_subagents": True,
        "jump_per_turn": True,
    },
    "off_task": {
        "enabled": False,
        "window": 4,
        "min_unrelated": 3,
        "min_keywords": 2,
        "use_prompt": False,
    },
    "looping": {
        "enabled": True,
        "window": 30,
        "read_n": 4,
        "search_n": 4,
        "command_n": 4,
        "retry_n": 3,
        "edit_revert_n": 2,
        "ignore_polling": True,
        "subagents": True,
    },
}

# every key, with what it does (README "Alerts" and `tokenscope live --help-alerts` print this)
KEYS = {
    "enabled": "master switch for all alerts",
    "active_steps": "an alert stays active (pane banner) while its last step is within this many main-thread steps "
                    "of the newest one",
    "max": "most alerts kept per session (newest first)",
    "cost.enabled": "cost alerts on/off",
    "cost.session_usd": "session spend (API-equivalent $) that fires `session_usd`; 0 disables",
    "cost.intent_usd": "spend of one intent (an intent group, else a step) that fires `intent_usd`; 0 disables",
    "cost.jump": "per-step spend jump detection on/off",
    "cost.jump_metric": "\"usd\" or \"cw\" (cost-weighted tokens); usd falls back to cw for unpriced models",
    "cost.jump_window": "previous steps in the rolling baseline",
    "cost.jump_warmup": "steps with a cost needed before a jump can fire",
    "cost.jump_ratio": "a jump costs at least this many times the baseline median",
    "cost.jump_z": "and has a robust z-score of at least this, on log costs: (ln x - median ln) / (1.4826 * MAD of "
                   "ln), the MAD floored at ln 1.25",
    "cost.jump_max_ratio": "and costs at least this many times the largest step in the baseline (a new high, not "
                           "a spike size the session already had); 0 turns this check off",
    "cost.jump_floor_usd": "and costs at least this much $ (metric usd)",
    "cost.jump_floor_cw": "and at least this many cost-weighted tokens (metric cw)",
    "cost.jump_subagents": "count the cost of the subagents a step launched as the step's cost",
    "cost.jump_per_turn": "compare cost per API call (turn) instead of per step: only expensive calls fire, not "
                          "long steps of ordinary calls (false: per-step spend)",
    "off_task.enabled": "off-task alerts on/off (off by default; also --off-task)",
    "off_task.window": "recent steps with files looked at",
    "off_task.min_unrelated": "of which at least this many touch only files unrelated to the active intent",
    "off_task.min_keywords": "the active intent needs at least this many keywords to be judged",
    "off_task.use_prompt": "without a declared intent or in-progress todo item, judge against the request's prompt "
                           "words (noisier: prompts describe behaviour more often than files)",
    "looping.enabled": "looping alerts on/off",
    "looping.window": "calls of one thread (main or one subagent) a repeat is counted in",
    "looping.read_n": "same file region read with the same result this many times, no edit to it in between",
    "looping.search_n": "same search (Grep / Glob) with the same result this many times",
    "looping.command_n": "same command (or other call) with the same result this many times, no edit in between",
    "looping.retry_n": "same call failing this many times with no edit in between",
    "looping.edit_revert_n": "edits undone (A->B then B->A, or a file written back to an earlier content) on one "
                             "file before it fires",
    "looping.ignore_polling": "ignore polling (sleep, waiting on a background task, reading its output)",
    "looping.subagents": "also look for loops inside subagents",
}


def _flat(d, prefix=""):
    out = {}
    for k, v in d.items():
        if isinstance(v, dict):
            out.update(_flat(v, prefix + k + "."))
        else:
            out[prefix + k] = v
    return out


def _coerce(key, value, default):
    """`value` (from a file or a "key=value" flag) as the type of the default."""
    if isinstance(default, bool):
        if isinstance(value, bool):
            return value
        s = str(value).strip().lower()
        if s in ("1", "true", "yes", "on"):
            return True
        if s in ("0", "false", "no", "off"):
            return False
        raise ValueError("alerts: %s wants true/false, got %r" % (key, value))
    if isinstance(default, int):
        try:
            return int(value)
        except (TypeError, ValueError):
            raise ValueError("alerts: %s wants an integer, got %r" % (key, value)) from None
    if isinstance(default, float):
        try:
            return float(value)
        except (TypeError, ValueError):
            raise ValueError("alerts: %s wants a number, got %r" % (key, value)) from None
    if key == "cost.jump_metric" and str(value) not in ("usd", "cw"):
        raise ValueError("alerts: cost.jump_metric is usd or cw, got %r" % (value,))
    return str(value)


def set_key(cfg, key, value):
    """Set one dotted key ("cost.session_usd") in a config dict, checked against DEFAULTS."""
    flat = _flat(DEFAULTS)
    if key not in flat:
        raise ValueError("alerts: unknown key %r (known: %s)" % (key, ", ".join(sorted(flat))))
    parts = key.split(".")
    d = cfg
    for p in parts[:-1]:
        d = d.setdefault(p, {})
    d[parts[-1]] = _coerce(key, value, flat[key])


def merge(cfg, data, prefix=""):
    """Merge a parsed config file (nested tables, or dotted keys) into cfg; unknown keys raise."""
    for k, v in (data or {}).items():
        key = prefix + str(k)
        if isinstance(v, dict):
            merge(cfg, v, key + ".")
        else:
            set_key(cfg, key, v)
    return cfg


def config_path():
    """$TOKENSCOPE_ALERTS_CONFIG, else ~/.config/tokenscope/alerts.toml (may not exist)."""
    return os.environ.get("TOKENSCOPE_ALERTS_CONFIG") or os.path.join(
        os.path.expanduser("~"), ".config", "tokenscope", "alerts.toml")


def read_file(path):
    """A config file as a dict: TOML (an optional [alerts] table, or the keys at the top level) or JSON (.json)."""
    with open(path, "rb") as f:
        raw = f.read()
    if path.endswith(".json"):
        data = json.loads(raw.decode("utf-8"))
    else:
        import tomllib
        data = tomllib.loads(raw.decode("utf-8"))
    if isinstance(data.get("alerts"), dict) and len(data) == 1:
        data = data["alerts"]
    return data


def load_config(path=None, overrides=(), off_task=None, enabled=None):
    """DEFAULTS, then the config file (`path`, else config_path() when it exists), then "key=value" overrides
    (--alert), then the --off-task / --no-alerts flags."""
    cfg = copy.deepcopy(DEFAULTS)
    p = path or config_path()
    if path or os.path.isfile(p):
        merge(cfg, read_file(os.path.expanduser(p)))
    for o in overrides or ():
        if "=" not in str(o):
            raise ValueError("alerts: --alert wants key=value, got %r" % (o,))
        k, v = str(o).split("=", 1)
        set_key(cfg, k.strip(), v.strip())
    if off_task is not None:
        cfg["off_task"]["enabled"] = bool(off_task)
    if enabled is not None:
        cfg["enabled"] = bool(enabled)
    return cfg


def add_arguments(p):
    """The alert flags of `tokenscope live` and `tokenscope alerts`."""
    p.add_argument("--alerts-config", metavar="FILE", default=None,
                   help="alerts config (TOML or JSON; default $TOKENSCOPE_ALERTS_CONFIG, else "
                        "~/.config/tokenscope/alerts.toml when it exists); `tokenscope live alerts-config` lists keys")
    p.add_argument("--alert", action="append", default=[], metavar="KEY=VALUE",
                   help="set one alerts key, e.g. --alert cost.session_usd=10 (repeatable; wins over the file)")
    p.add_argument("--off-task", action="store_true", help="enable off-task alerts (off by default)")
    p.add_argument("--no-alerts", action="store_true", help="no alerts at all")
    return p


def from_args(args):
    """load_config from parsed add_arguments flags."""
    return load_config(getattr(args, "alerts_config", None), getattr(args, "alert", None) or (),
                       off_task=True if getattr(args, "off_task", False) else None,
                       enabled=False if getattr(args, "no_alerts", False) else None)


def keys_doc():
    """"key = default  # what it does" lines for every config key."""
    flat = _flat(DEFAULTS)
    return ["%s = %s  # %s" % (k, json.dumps(flat[k]), KEYS[k]) for k in flat]


# ---------------------------------------------------------------- model

@dataclass
class Alert:
    kind: str                     # cost | off_task | looping
    rule: str                     # session_usd, jump, repeat_read, retry, ...
    severity: str                 # info | warning | critical
    message: str                  # short, privacy-safe
    step: Optional[int] = None    # the step it points at (the newest one involved)
    first: Optional[int] = None   # step range involved (first..step)
    agent: Optional[str] = None   # None: main thread; else the subagent whose steps these are
    details: dict = field(default_factory=dict)
    key: str = ""                 # stable identity (same episode -> same id as it grows)
    span: tuple = ()              # (first, last) call position in its thread (looping): steps resolved later

    @property
    def id(self):
        return hashlib.blake2b(("%s|%s|%s|%s" % (self.kind, self.rule, self.agent, self.key)).encode(),
                               digest_size=5).hexdigest()

    def to_dict(self):
        return {"id": self.id, "kind": self.kind, "rule": self.rule, "severity": self.severity,
                "message": scrub(self.message), "step": self.step,
                "first": self.first if self.first is not None else self.step, "agent": self.agent,
                "details": {k: scrub(v) if isinstance(v, str) else v for k, v in self.details.items()}}


@dataclass
class Call:
    """One tool call of a thread, privacy-reduced: `file` and `key` are only compared, never shown."""
    n: int                        # position in its thread
    step: Optional[int]           # the step (of its thread) it belongs to
    tool: str                     # tool name (Bash, Read, Grep, mcp__x__y, ...)
    kind: str                     # read | search | edit | command | other
    key: str = ""                 # identity of the call (tool + relevant input), hashed
    file: Optional[str] = None    # path it reads / edits (private)
    res: Optional[str] = None     # hash of its result (None: no result yet)
    err: Optional[bool] = None    # failed (tool error, or a test run with failures)
    poll: bool = False            # waiting / polling a background task
    edits: tuple = ()             # ((old hash, new hash), ...) of an Edit / MultiEdit
    write: Optional[str] = None   # content hash of a Write
    words: tuple = ()             # search pattern words (off-task matching only)
    found: tuple = ()             # file names a search / listing showed (off-task: what a related search led to)


@dataclass
class StepFacts:
    i: int
    usd: float = 0.0
    cw: float = 0.0
    closed: bool = True
    turns: int = 1                # API calls (turns) in the step
    cat: str = "other"
    group: Optional[int] = None   # intent group index (canvas node), None: the step is its own node
    delegated: bool = False       # launched subagents (their cost is in usd / cw)
    todo: Optional[str] = None    # in-progress todo item (private)
    declared: Optional[str] = None
    prompt: int = 0               # which request it belongs to (prompt number, 0: before any)


@dataclass
class Context:
    """What the detectors read. `steps` / `calls` are the main thread's; `agents` maps a subagent id to its
    (steps, calls) when looping.subagents is on (steps may be None and the calls' step None: the caller maps an
    alert's `span` to steps afterwards). `prompt_words[n]` are request n's words (1-based)."""
    config: dict
    steps: list = field(default_factory=list)
    calls: list = field(default_factory=list)
    groups: list = field(default_factory=list)       # [{"first", "last", "usd", "cw", "cat"}]
    agents: dict = field(default_factory=dict)       # agent id -> (steps, calls)
    session_usd: float = 0.0
    session_cw: float = 0.0
    prompt_words: list = field(default_factory=list)
    names: dict = field(default_factory=dict)        # private file / command -> ordinal label

    def label(self, kind, raw):
        """"file #2" / "command #1": a session-stable ordinal, never the name itself."""
        k = (kind, raw)
        if k not in self.names:
            self.names[k] = sum(1 for x in self.names if x[0] == kind) + 1
        return "%s #%d" % (kind, self.names[k])


class Detector(Protocol):
    name: str

    def detect(self, ctx: Context) -> list:
        ...


REGISTRY: dict = {}


def register(name, factory: Callable[[dict], Detector]):
    """Add a detector: factory(config) -> Detector (or None when the config turns it off)."""
    REGISTRY[name] = factory
    return factory


def h(*parts):
    """Short private hash (identity of a call, an edit, a result)."""
    return hashlib.blake2b("\x1f".join(str(p) for p in parts).encode("utf-8", "replace"), digest_size=8).hexdigest()


# ---------------------------------------------------------------- privacy guard

SCRUB = [
    (re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+"), "…"),                                   # e-mail
    (re.compile(r"\b(?:sk|pk|rk|ghp|gho|github_pat|xox[abpr]|AKIA|AIza)[-_A-Za-z0-9]{8,}"), "…"),   # keys
    (re.compile(r"\b[A-Za-z0-9+/_-]{32,}={0,2}"), "…"),                              # long tokens
    (re.compile(r"(?:~|\.{1,2})?(?:/[^\s/]+){2,}/?|[A-Za-z]:\\\S+|~/\S*"), "…"),        # paths
    (re.compile(r"\b[\w-]+\.(?:py|js|jsx|ts|tsx|go|rs|rb|java|kt|swift|c|cc|cpp|h|hpp|cs|php|md|json|ya?ml|toml|"
                r"txt|sh|sql|html|css|lock|cfg|ini|env)\b"), "…"),                    # file names
]


def scrub(text):
    """Last guard over alert strings: no e-mail, key-like token, path or file name survives."""
    s = str(text or "")
    for rx, rep in SCRUB:
        s = rx.sub(rep, s)
    return s


def ext_of(path):
    """".py" (redacted views show extensions only), "" without one."""
    e = os.path.splitext(str(path or ""))[1].lower()
    return e if re.fullmatch(r"\.[a-z0-9]{1,8}", e) else ""


def money(v):
    return "$%.2f" % v


def tok(v):
    v = float(v or 0)
    return "%.1fM" % (v / 1e6) if v >= 1e6 else "%dk" % round(v / 1e3) if v >= 1e3 else "%d" % v


# ---------------------------------------------------------------- call facts (computed at ingest)

EDIT_TOOLS = ("Edit", "Write", "MultiEdit", "NotebookEdit", "edit_file", "write_file", "write_to_file", "replace_file_content", "multi_replace_file_content")
SKIP_TOOLS = {"TodoWrite", "TodoRead", "TaskCreate", "TaskUpdate", "TaskList", "TaskOutput", "ToolSearch", "Agent",
              "Task", "Workflow", "ExitPlanMode", "Skill"}   # bookkeeping / delegation: never a "trivial" loop


def _canon(inp):
    try:
        return json.dumps(inp, sort_keys=True, default=str)[:20000]
    except (TypeError, ValueError):
        return str(inp)[:20000]


POLL_CMD = re.compile(r"\bsleep\b|\bwait\b|\buntil\b|\bwatch\b|--watch\b|\btail -f\b")
POLL_TOOL = re.compile(r"(?:^|_)(?:wait|status|poll|progress|health|sleep|ping)(?:_|$)", re.I)
POLL_DESC = re.compile(r"\b(?:wait|waiting|poll|polling|watch|watching|monitor|check(?:ing)? (?:on|status|progress))\b",
                       re.I)


def call_facts(name, inp, rec, intent):
    """Private per-call facts the looping / off-task detectors read: {"kind", "key", "edits", "write", "words",
    "poll"} (hashes only, plus search words). `rec` is intents.record(name, inp), `intent` its intents.tool_intent.
    "poll": a command that waits (sleep / wait / until loops, --watch) or says it does in its description, a read
    of a background task's output capture, a tool named like a status / wait / poll call."""
    inp = inp if isinstance(inp, dict) else {}
    f = rec.get("file") or None
    if name.endswith("declare_intent") or name in SKIP_TOOLS:
        return {"kind": "skip", "key": h(name)}
    if name in EDIT_TOOLS or (intent == "edit" and name == "Bash"):
        edits = []
        if name == "Edit":
            edits = [(h(inp.get("old_string")), h(inp.get("new_string")))]
        elif name == "MultiEdit" and isinstance(inp.get("edits"), list):
            edits = [(h(e.get("old_string")), h(e.get("new_string"))) for e in inp["edits"] if isinstance(e, dict)]
        return {"kind": "edit", "key": h(name, _canon(inp) if name != "Bash" else rec.get("cmd")),
                "edits": tuple(edits), "write": h(inp.get("content")) if name == "Write" else None}
    if rec.get("capture"):   # reading a background task's output capture: waiting on it
        return {"kind": "read" if name == "Read" else "command", "key": h(name, _canon(inp)), "poll": True}
    if name == "Read" or (name == "Bash" and f):
        return {"kind": "read", "key": h("read", f, inp.get("offset"), inp.get("limit"),
                                         rec.get("cmd") if name == "Bash" else None)}
    if name in ("Grep", "Glob"):
        return {"kind": "search", "key": h(name, _canon(inp)),
                "words": tuple(sorted(set(words_of(inp.get("pattern"))) | path_words(inp.get("path"))))}
    if name == "Bash":
        poll = bool(POLL_CMD.search(rec.get("cmd") or "") or POLL_DESC.search(str(inp.get("description") or "")))
        return {"kind": "command", "key": h("bash", rec.get("cmd")), "poll": poll}
    return {"kind": "other", "key": h(name, _canon(inp)), "poll": bool(POLL_TOOL.search(name.split("__")[-1]))}


# ---------------------------------------------------------------- cost

LOG_MAD_FLOOR = math.log(1.25)


def robust_baseline(values):
    """(median, median of ln, scaled MAD of ln) of a list of positive costs. Step costs are heavy-tailed
    (multiplicative), so the spread is measured on log costs; the MAD has a floor (ln 1.25) so a flat history does
    not turn every small wobble into a huge z."""
    logs = [math.log(v) for v in values]
    lm = median(logs)
    mad = 1.4826 * median(abs(x - lm) for x in logs)
    return median(values), lm, max(mad, LOG_MAD_FLOOR)


def cost_jumps(values, window, warmup, ratio, z, floor, max_ratio=0.0):
    """[(index, value, median, ratio, z)] for each value that jumps over the rolling baseline of the previous
    `window` positive values (once there are >= warmup of them): >= floor, >= ratio x their median, a robust log z
    >= z, and (max_ratio > 0) >= max_ratio x their largest."""
    out, hist = [], []
    for k, v in enumerate(values):
        if v is None:
            continue
        base = hist[-window:]
        if len(base) >= warmup and v >= floor:
            m, lm, mad = robust_baseline(base)
            r = v / m if m > 0 else float("inf")
            zz = (math.log(v) - lm) / mad
            if r >= ratio and zz >= z and (not max_ratio or v >= max_ratio * max(base)):
                out.append((k, v, m, r, zz))
        if v > 0:
            hist.append(v)
    return out


class CostDetector:
    name = "cost"

    def __init__(self, cfg):
        self.c = cfg["cost"]

    def detect(self, ctx):
        c, out = self.c, []
        if c["session_usd"] > 0 and ctx.session_usd >= c["session_usd"]:
            run, at = 0.0, None
            for x in ctx.steps:
                run += x.usd
                if at is None and run >= c["session_usd"]:
                    at = x.i
            if at is None and ctx.steps:   # the rest is in turns outside any step (answers, subagents not linked)
                at = ctx.steps[-1].i
            sev = "critical" if ctx.session_usd >= 2 * c["session_usd"] else "warning"
            out.append(Alert("cost", "session_usd", sev, "session spend %s passed the %s threshold" % (
                money(ctx.session_usd), money(c["session_usd"])), step=at, first=at,
                details={"usd": round(ctx.session_usd, 4), "threshold": c["session_usd"]}, key="session"))
        if c["intent_usd"] > 0:
            usd = {x.i: x.usd for x in ctx.steps}
            for node in intent_nodes(ctx):
                if node["usd"] >= c["intent_usd"]:
                    sev = "critical" if node["usd"] >= 2 * c["intent_usd"] else "warning"
                    n = node["last"] - node["first"] + 1
                    # it points at the step that took the intent over the threshold (stable as the intent grows,
                    # so it groups with that step's other alerts), else the intent's last step
                    run, at = 0.0, node["last"]
                    for i in range(node["first"], node["last"] + 1):
                        run += usd.get(i, 0.0)
                        if run >= c["intent_usd"]:
                            at = i
                            break
                    out.append(Alert("cost", "intent_usd", sev, "[%s] intent cost %s (threshold %s) over %d step%s" % (
                        node["cat"].upper(), money(node["usd"]), money(c["intent_usd"]), n, "s" if n != 1 else ""),
                        step=at, first=node["first"],
                        details={"usd": round(node["usd"], 4), "threshold": c["intent_usd"], "steps": n},
                        key="intent:%d" % node["first"]))
        if c["jump"]:
            out += self._jumps(ctx)
        return out

    def _jumps(self, ctx):
        c = self.c
        metric = c["jump_metric"]
        if metric == "usd" and not any(x.usd > 0 for x in ctx.steps):
            metric = "cw"   # unpriced model: tokens instead
        val = (lambda x: x.usd) if metric == "usd" else (lambda x: x.cw)
        if c["jump_per_turn"]:
            per = val
            val = lambda x: per(x) / max(1, x.turns)  # noqa: E731
        floor = c["jump_floor_usd"] if metric == "usd" else c["jump_floor_cw"]
        steps = [x for x in ctx.steps if c["jump_subagents"] or not x.delegated]
        out = []
        for k, v, m, r, z in cost_jumps([val(x) for x in steps], c["jump_window"], c["jump_warmup"],
                                        c["jump_ratio"], c["jump_z"], floor, c["jump_max_ratio"]):
            x = steps[k]
            sev = "critical" if r >= 4 * c["jump_ratio"] else "warning"
            fmt = money if metric == "usd" else (lambda v: tok(v) + " CW")
            what = "cost per call" if c["jump_per_turn"] else "cost"
            tail = " (incl. subagents)" if x.delegated else \
                " over %d calls" % x.turns if x.turns >= 3 and not c["jump_per_turn"] else ""
            out.append(Alert("cost", "jump", sev, "step %s %s, %.1f× the session median %s%s" % (
                what, fmt(v), r, fmt(m), tail), step=x.i, first=x.i,
                details={"metric": metric, "value": round(v, 4), "median": round(m, 4),
                         "ratio": round(r, 2), "z": round(z, 1), "baseline_n": min(c["jump_window"], k),
                         "open": not x.closed, "delegated": x.delegated, "turns": x.turns,
                         "per_turn": c["jump_per_turn"]}, key="jump:%d" % x.i))
        return out


def intent_nodes(ctx):
    """The canvas nodes: intent groups, and steps outside any group ({"first", "last", "usd", "cw", "cat"})."""
    nodes, seen = [], set()
    for x in ctx.steps:
        if x.group is not None and x.group < len(ctx.groups):
            if x.group not in seen:
                seen.add(x.group)
                g = ctx.groups[x.group]
                nodes.append({"first": g["first"], "last": g["last"], "usd": g.get("usd") or 0.0,
                              "cw": g.get("cw") or 0.0, "cat": g.get("cat") or "other"})
        else:
            nodes.append({"first": x.i, "last": x.i, "usd": x.usd, "cw": x.cw, "cat": x.cat or "other"})
    return nodes


# ---------------------------------------------------------------- off-task

PATH_GENERIC = set("""src lib libs app apps pkg pkgs cmd internal test tests spec specs index main init utils util
helpers helper common core shared misc tmp temp dist build out bin scripts script config configs conf docs doc
readme package lock json yaml yml toml txt md js ts tsx jsx py go rs rb java mod sum users home workspace node
modules vendor public static assets www html css setup claude cache local""".split())


# words that say what kind of work it is, not what it is about (the category words of xindex.GENERIC are kept:
# "report", "data" or "build" can be exactly what a request is about)
TASK_WORDS = set("""fix fixed fixing bug bugs issue issues error errors problem please make add adding update updating
change changes changing implement implementing work working task tasks step steps file files code function functions
method methods class classes thing things something look why what how help want need try sure into""".split())


def words_of(text):
    """Keywords of an intent text: lowercase, stemmed; stopwords, task words and words < 3 letters out."""
    out = []
    for w in re.findall(r"[A-Za-z]+", re.sub(r"([a-z])([A-Z])", r"\1 \2", str(text or ""))):
        w = w.lower()
        if len(w) < 3 or w in STOP or w in TASK_WORDS:
            continue
        w = _stem(w)
        if len(w) >= 3 and w not in STOP and w not in TASK_WORDS:
            out.append(w)
    return out


def path_words(path):
    """Words of a path's directories and file stem (camelCase / snake_case / kebab split), generic ones out."""
    p = re.sub(r"\.[A-Za-z0-9]{1,8}$", "", str(path or ""))
    return {w for w in words_of(p.replace("_", " ").replace("-", " ").replace(".", " ")) if w not in PATH_GENERIC}


class OffTaskDetector:
    """Heuristic: a step is unrelated when it touched files (read / edit) none of which shares a path word with the
    active intent's keywords (declared intent, else in-progress todo item, else with use_prompt the prompt words), none of which a related step of the request already touched or found (the file names a
    related search showed), and its searches share no word with the intent either. Steps without files (commands,
    searches only) are not judged. `window` recent steps with files, `min_unrelated` of them unrelated -> one alert
    per episode (it ends at the next related step)."""
    name = "off_task"

    def __init__(self, cfg):
        self.c = cfg["off_task"]

    def intent_of(self, ctx, x):
        """(source, keywords) of the intent active at step x: declared, else the todo item, else the request."""
        for src, text in (("declared", x.declared), ("todo", x.todo)):
            if text:
                return src, set(words_of(text))
        if self.c["use_prompt"] and 0 < x.prompt <= len(ctx.prompt_words):
            return "prompt", set(words_of(" ".join(w for w in ctx.prompt_words[x.prompt - 1] if w != "|")))
        return None, set()

    def detect(self, ctx):
        c, out = self.c, []
        by_step = {}
        for call in ctx.calls:
            if call.step is not None:
                by_step.setdefault(call.step, []).append(call)
        related, found, req = set(), set(), None   # files of related steps / names related searches showed
        recent, episode = [], None
        for x in ctx.steps:
            if x.prompt != req:
                req, related, found, recent, episode = x.prompt, set(), set(), [], None
            calls = [k for k in by_step.get(x.i, ()) if k.kind in ("read", "edit", "search", "command") and not k.poll]
            src, kw = self.intent_of(ctx, x)
            for k in calls:
                if k.found and (k.kind != "search" or set(k.words) & kw or len(kw) < c["min_keywords"]):
                    found |= set(k.found)
            files = {k.file for k in calls if k.file and k.kind in ("read", "edit")}
            if not files or len(kw) < c["min_keywords"]:
                related |= {k.file for k in calls if k.kind == "edit" and k.file}
                continue
            hit = bool(files & related) or any(os.path.basename(f) in found for f in files) or \
                any(path_words(f) & kw for f in files) or any(set(k.words) & kw for k in calls if k.kind == "search")
            if hit:
                related |= files
            recent = (recent + [(x.i, hit)])[-c["window"]:]
            bad = [i for i, ok in recent if not ok]
            if hit:
                episode = None
            elif len(bad) >= c["min_unrelated"]:
                if episode is None:
                    episode = Alert("off_task", "unrelated", "warning", "", key="off:%d" % bad[0])
                    out.append(episode)
                episode.first = episode.first if episode.first is not None else bad[0]
                episode.step = x.i
                n = sum(1 for _, ok in recent if not ok)
                episode.message = "%d of the last %d steps touched files unrelated to the current %s" % (
                    n, len(recent), {"declared": "declared intent", "todo": "todo item"}.get(src, "request"))
                episode.details = {"unrelated": n, "window": len(recent), "source": src, "keywords": len(kw)}
        return out


class HaikuOffTaskDetector:
    """Not registered. Where a model-based off-task check goes: build, from the Context, the active intent's text
    (declared label / todo item / masked prompt words) and a privacy-reduced digest of the recent steps (their
    categories, tool names, file extensions and path words, never paths or file contents), ask
    `claude -p --model haiku` (as tokenscope.live.goalname / stepnames do, through the user's own CLI, cached on
    disk) whether the steps serve the intent, and turn a "no" into Alert("off_task", "model", ...). Then enable it
    with register("off_task_haiku", lambda cfg: HaikuOffTaskDetector(cfg) if cfg["off_task"]["enabled"] else None)."""
    name = "off_task_haiku"

    def __init__(self, cfg):
        self.c = cfg["off_task"]

    def detect(self, ctx):
        raise NotImplementedError("model-based off-task detection is not implemented yet")


# ---------------------------------------------------------------- looping

REPEAT_RULE = {"read": "repeat_read", "search": "repeat_search", "command": "repeat_command", "other": "repeat_call"}


class LoopDetector:
    """Loops in one thread's calls (main thread, and each subagent with looping.subagents)."""
    name = "looping"

    def __init__(self, cfg):
        self.c = cfg["looping"]

    def detect(self, ctx):
        out = self.thread(ctx, None, ctx.calls)
        if self.c["subagents"]:
            for aid, (_, calls) in ctx.agents.items():
                out += self.thread(ctx, aid, calls)
        return out

    def thread(self, ctx, aid, calls):
        return self.repeats(ctx, aid, calls) + self.retries(ctx, aid, calls) + self.reverts(ctx, aid, calls)

    def _what(self, ctx, call):
        """("same .py file", "file #2") style descriptors; never the name itself."""
        if call.kind == "read":
            e = ext_of(call.file)
            return "same %sfile" % (e + " " if e else ""), ctx.label("file", call.file or call.key)
        if call.kind == "search":
            return "same search", ctx.label("search", call.key)
        if call.kind == "command":
            return "same command", ctx.label("command", call.key)
        if call.kind == "edit":
            e = ext_of(call.file)
            return "same edit to a %sfile" % (e + " " if e else ""), ctx.label("file", call.file or call.key)
        return "same %s call" % ("MCP" if call.tool.startswith("mcp__") else "tool"), ctx.label("call", call.key)

    def repeats(self, ctx, aid, calls):
        c, out = self.c, []
        need = {"read": c["read_n"], "search": c["search_n"], "command": c["command_n"], "other": c["command_n"]}
        seen = {}       # (key, result) -> [call positions] since the last reset
        eps = {}        # (key, result) -> open Alert
        for call in calls:
            if call.kind == "edit":
                # an edit makes a re-read / re-run legitimate: forget what touched that file (reads) / everything
                for k in [k for k in seen if k[2] is None or k[2] == call.file]:
                    seen.pop(k)
                    eps.pop(k, None)
                continue
            if call.res is None or call.err or (call.poll and c["ignore_polling"]) or call.kind not in need:
                continue
            k = (call.key, call.res, call.file if call.kind == "read" else None)
            pos = [n for n in seen.get(k, []) if n > call.n - c["window"]] + [call.n]
            seen[k] = pos
            if len(pos) < need[call.kind]:
                continue
            what, label = self._what(ctx, call)
            a = eps.get(k)
            if a is None:
                a = eps[k] = Alert("looping", REPEAT_RULE[call.kind], "warning", "", agent=aid,
                                   first=self._step_of(calls, pos[0]), key="%s:%d" % (label, pos[0]))
                out.append(a)
            a.step, a.span = call.step, (pos[0], call.n)
            a.severity = "critical" if len(pos) >= 2 * need[call.kind] else "warning"
            verb = {"read": "read", "search": "run", "command": "run"}.get(call.kind, "made")
            a.message = "%s %s %d× with the same result (%s)" % (what, verb, len(pos), label)
            a.details = {"count": len(pos), "calls": c["window"], "target": label, "tool": tool_kind(call.tool)}
        return out

    def retries(self, ctx, aid, calls):
        c, out = self.c, []
        fails, eps = {}, {}
        for call in calls:
            if call.kind == "edit" and call.err is False:
                fails, eps = {}, {}
                continue
            if call.err is None or (call.poll and c["ignore_polling"]):
                continue
            if not call.err:
                fails.pop(call.key, None)
                eps.pop(call.key, None)
                continue
            pos = [n for n in fails.get(call.key, []) if n > call.n - c["window"]] + [call.n]
            fails[call.key] = pos
            if len(pos) < c["retry_n"]:
                continue
            what, label = self._what(ctx, call)
            what = what.replace("same ", "", 1)
            a = eps.get(call.key)
            if a is None:
                a = eps[call.key] = Alert("looping", "retry", "warning", "", agent=aid,
                                          first=self._step_of(calls, pos[0]), key="%s:%d" % (label, pos[0]))
                out.append(a)
            a.step, a.span = call.step, (pos[0], call.n)
            a.severity = "critical" if len(pos) >= 2 * c["retry_n"] else "warning"
            a.message = "same failing %s retried %d× with no edit in between (%s)" % (what, len(pos), label)
            a.details = {"count": len(pos), "target": label, "tool": tool_kind(call.tool)}
        return out

    def reverts(self, ctx, aid, calls):
        c, out = self.c, []
        hist = {}       # file -> [(call n, kind, a, b)]: edit (old -> new hashes) or write (content hash)
        cycles = {}     # file -> [call n of each revert]
        eps = {}
        for call in calls:
            if call.kind != "edit" or not call.file or call.err:
                continue
            past = [p for p in hist.get(call.file, []) if p[0] > call.n - c["window"]]
            reverted = False
            for old, new in call.edits:
                if old != new and any(p[1] == "edit" and (p[2], p[3]) == (new, old) for p in past):
                    reverted = True
            if call.write is not None:
                writes = [p for p in past if p[1] == "write"]
                if writes and writes[-1][2] != call.write and any(p[2] == call.write for p in writes[:-1]):
                    reverted = True
            hist[call.file] = past + [(call.n, "edit", a, b) for a, b in call.edits] + \
                ([(call.n, "write", call.write, None)] if call.write is not None else [])
            if not reverted:
                continue
            cyc = cycles.setdefault(call.file, []) + [call.n]
            cycles[call.file] = cyc
            if len(cyc) < c["edit_revert_n"]:
                continue
            e = ext_of(call.file)
            label = ctx.label("file", call.file)
            a = eps.get(call.file)
            n0 = min(p[0] for p in past) if past else call.n
            if a is None:
                a = eps[call.file] = Alert("looping", "edit_revert", "warning", "", agent=aid, key=label,
                                           first=self._step_of(calls, n0))
                a.span = (n0, call.n)
                out.append(a)
            a.step, a.span = call.step, (a.span[0], call.n)
            a.severity = "critical" if len(cyc) >= 2 * c["edit_revert_n"] + 1 else "warning"
            a.message = "edits to the same %sfile undone %d\u00d7 (A\u2192B\u2192A) (%s)" % (
                e + " " if e else "", len(cyc), label)
            a.details = {"reverts": len(cyc), "target": label}
        return out

    @staticmethod
    def _step_of(calls, n):
        return next((k.step for k in calls if k.n == n), None)


def tool_kind(tool):
    """A tool name safe to show: MCP tools as "mcp" (server names can be private)."""
    return "mcp" if str(tool).startswith("mcp__") else str(tool)


register("cost", lambda cfg: CostDetector(cfg) if cfg["cost"]["enabled"] else None)
register("off_task", lambda cfg: OffTaskDetector(cfg) if cfg["off_task"]["enabled"] else None)
register("looping", lambda cfg: LoopDetector(cfg) if cfg["looping"]["enabled"] else None)


# ---------------------------------------------------------------- run

SEV_RANK = {s: i for i, s in enumerate(SEVERITIES)}


def run(ctx, names=None):
    """All enabled detectors over ctx -> [Alert], newest step first (session-level ones lead), at most
    config["max"]. A detector that raises is skipped (alerts never break the live views)."""
    cfg = ctx.config
    if not cfg.get("enabled", True):
        return []
    out = []
    for name, factory in REGISTRY.items():
        if names is not None and name not in names:
            continue
        det = factory(cfg)
        if det is None:
            continue
        try:
            out += det.detect(ctx)
        except Exception:  # noqa: BLE001 - one broken detector must not hide the others
            continue
    out.sort(key=lambda a: (a.rule != "session_usd", -(a.step if a.step is not None else -1),
                            -SEV_RANK.get(a.severity, 0), a.kind, a.rule))
    return out[:cfg.get("max", 50)]


RULE_RANK = {r: i for i, r in enumerate(("session_usd", "intent_usd", "jump"))}


def _anchor(a):
    """The step an alert fired at, for grouping: a cost alert's step (fixed once fired), else the step its episode
    began at (a loop's latest step moves as it goes on)."""
    return a.step if a.kind == "cost" else a.first if a.first is not None else a.step


def group(alerts):
    """Alerts of one kind firing at the same step of the same thread, merged: [[lead, *others]], the lead the most
    severe (then session_usd, intent_usd, jump, the rest), groups in the order of their first member. One expensive
    step that passes the session and intent thresholds and is a jump is one alert with three reasons."""
    out, at = [], {}
    for a in alerts:
        anc = _anchor(a)
        if anc is None:
            out.append([a])
            continue
        k = (a.kind, a.agent, anc)
        if k in at:
            at[k].append(a)
        else:
            at[k] = [a]
            out.append(at[k])
    for g in out:
        g.sort(key=lambda a: (-SEV_RANK.get(a.severity, 0), RULE_RANK.get(a.rule, len(RULE_RANK))))
    return out


def group_id(members):
    """A grouped alert's id: a cost group is the step it fired at (the same id whichever reasons join it later); a
    single other alert keeps its own id; others hash their kind, thread and step."""
    a = members[0]
    if a.kind == "cost" and a.step is not None:
        return hashlib.blake2b(("cost|%s|%s" % (a.agent, a.step)).encode(), digest_size=5).hexdigest()
    if len(members) == 1:
        return a.id
    return hashlib.blake2b(("group|%s|%s|%s" % (a.kind, a.agent, _anchor(a))).encode(), digest_size=5).hexdigest()


def _active(a, last_step, agents_live, active_steps):
    if a.rule == "session_usd":
        return True
    if a.agent is not None:
        return a.agent in agents_live
    return last_step is not None and a.step is not None and a.step >= last_step - active_steps + 1


def public(alerts, last_step=None, agents_live=(), active_steps=3):
    """Alerts as dicts, grouped (group: one per kind, thread and step) with "active": a session-level alert always,
    a main-thread one while its step is within `active_steps` of the newest step, a subagent one while that subagent
    works. A group carries the lead's rule, message and details, the worst severity, the widest step range, and
    "reasons": every member ({"id", "rule", "severity", "message", "details"}, the lead first); "members" are their
    ids (history keeps a group's acknowledgement when members join)."""
    out = []
    for g in group(alerts):
        lead = g[0]
        d = lead.to_dict()
        d["id"] = group_id(g)
        d["severity"] = max((a.severity for a in g), key=lambda s: SEV_RANK.get(s, 0))
        d["first"] = min(a.first if a.first is not None else a.step for a in g)
        d["step"] = max(a.step for a in g) if all(a.step is not None for a in g) else lead.step
        d["active"] = any(_active(a, last_step, agents_live, active_steps) for a in g)
        d["reasons"] = [{k: m[k] for k in ("id", "rule", "severity", "message", "details")}
                        for m in (a.to_dict() for a in g)]
        d["members"] = [a.id for a in g]
        out.append(d)
    return out


TRIGGERS_MAX = 50   # triggers listed on one collapsed alert (the newest; "count" counts them all)


def alert_id(sid, kind):
    """The stable id of a session's one alert of `kind` (collapse): the same whatever fires it, and as it fires again."""
    return hashlib.blake2b(("alert|%s|%s" % (sid, kind)).encode(), digest_size=5).hexdigest()


def collapse(pub, sid):
    """public() alerts collapsed to one per kind (at most one cost, one off-task, one looping alert per session), in
    the order of their kind's newest trigger. Each grouped alert is a trigger; the collapsed alert has the worst
    severity of them, the latest trigger's (the newest step's) rule, message, details, step range, reasons, "at" and
    "intent", "count" (triggers so far) and "triggers" ({"id", "rule", "rules", "severity", "message", "step",
    "first", "agent"}, oldest first, at most TRIGGERS_MAX), "active" when any trigger is. Id: alert_id(sid, kind)."""
    by = {}
    for a in pub:
        by.setdefault(a["kind"], []).append(a)
    out = []
    for kind, ts in by.items():
        ts.sort(key=lambda a: (a["step"] if a["step"] is not None else -1, a.get("at") or 0,
                               SEV_RANK.get(a["severity"], 0)))
        lead = ts[-1]
        d = dict(lead, id=alert_id(sid, kind), count=len(ts),
                 severity=max((a["severity"] for a in ts), key=lambda s: SEV_RANK.get(s, 0)),
                 active=any(a.get("active") for a in ts), members=[a["id"] for a in ts],
                 triggers=[{"id": a["id"], "rule": a["rule"], "rules": [m["rule"] for m in a.get("reasons") or [a]],
                            "severity": a["severity"], "message": a["message"], "step": a["step"],
                            "first": a["first"], "agent": a["agent"]} for a in ts[-TRIGGERS_MAX:]])
        out.append(d)
    out.sort(key=lambda d: (d["rule"] != "session_usd", -(d["step"] if d["step"] is not None else -1), d["kind"]))
    return out


def counts(alerts):
    """{"n", "active", "worst", "by_kind": {kind: n}} of public alert dicts (board cards)."""
    by = {}
    for a in alerts:
        by[a["kind"]] = by.get(a["kind"], 0) + 1
    act = [a for a in alerts if a.get("active")]
    worst = max((a["severity"] for a in act), key=lambda s: SEV_RANK.get(s, 0), default=None)
    return {"n": len(alerts), "active": len(act), "worst": worst, "by_kind": by}
