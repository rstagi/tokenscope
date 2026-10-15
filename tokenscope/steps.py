"""Steps and intent groups (docs/intents-spec.md sections 1, 2 and 5): pure functions, no I/O.

Input format of `segment(turns, events, open_last=True)`:

    turns  = [{"ts": float|None, "cw": float, "usd": float,
               "ts_end": float|None,     # when its last call's result arrived (step durations); defaults to ts
               "tools": [{"name": raw tool_use name, "input": dict}],   # inputs may be slimmed (see slim_input)
               "thinking": int,          # thinking blocks in / before this turn
               "text": str,              # visible text / reasoning in / before this turn (labelling only)
               "test": None|"pass"|"fail",   # outcome of a test run in this turn
               "sub_cw": float, "sub_usd": float,    # cost of the subagents / workflow agents this turn launched
               "wf_agents": int}]                    # workflow agents its Workflow calls launched
    events = [{"kind": "prompt", "at": k},   # a user prompt (incl. a queued follow-up) arrived before turns[k]
              {"kind": "skill", "at": k, "name": str, "user": bool}]   # a slash-command skill was invoked before
                                                                        # turns[k]; `user`: typed (a boundary)

One turn = one assistant API message of the main thread that called tools; a turn mixing ToolSearch with other
calls is split into parts (see call_parts), and a step records which part of its first / last turn it holds
(`part_first` / `part_last`: None for the whole turn, else the part index). Boundaries depend only on a turn and
what came before it, so appending turns never moves an earlier boundary (incremental-stable); step i keeps its
index forever. `open_last=False` (offline, or a session waiting for the user) closes the last step too.
"""
import os
import re
from collections import Counter

from . import intents
from .skills import tool_skill
from .understand import test_runner

TEXT_CHARS = 300
MAX_WORDS = 7
NEUTRAL = {"TodoWrite", "TodoRead", "TaskCreate", "TaskUpdate", "TaskList"}   # bookkeeping: no category of its own
SUB_PROGS = {"git", "npm", "pnpm", "yarn", "cargo", "go", "docker", "uv", "make", "gh", "kubectl", "bun", "pip", "npx"}
INTERPRETERS = {"python", "python3", "node", "bash", "sh", "zsh", "deno", "bun", "ruby", "perl"}
WRAPPERS = {"env", "time", "sudo", "nohup", "exec", "timeout"}
SHELL_WORDS = {"while", "until", "if", "then", "else", "elif", "fi", "do", "done", "esac", "in", "{", "}", "!", "[[",
               "]]"}   # syntax, not programs: what runs is the next word
SHELL_HEADERS = {"for", "select", "case", "function"}   # "for f in *": the body (next segment) holds the program
KIND_CAT = {"investigate": "investigate", "plan": "plan", "change": "change", "verify": "verify"}   # declared kind
PROG_NAME = re.compile(r"[A-Za-z_][\w.+-]*")   # a program name: never a bare number, flag, "T()" or "["
SLIM_KEYS = ("file_path", "notebook_path", "pattern", "skill", "name", "command", "description", "subagent_type",
             "url", "query", "label", "kind", "offset", "limit")


def is_declare(name):
    return str(name or "").endswith("declare_intent")


def slim_input(name, inp):
    """The parts of a tool input the steps read (keeps memory small in the live server)."""
    inp = inp if isinstance(inp, dict) else {}
    out = {k: (v[:4000] if isinstance(v, str) else v) for k, v in inp.items() if k in SLIM_KEYS}
    if name == "TodoWrite" and isinstance(inp.get("todos"), list):
        out["todos"] = [{k: str(t.get(k) or "")[:200] for k in ("content", "status", "activeForm")}
                        for t in inp["todos"] if isinstance(t, dict)]
    if name == "ExitPlanMode":
        out["plan"] = str(inp.get("plan") or "")[:4000]
    return out


def _words(cmd_seg):
    w = [x.lstrip("({!`") for x in cmd_seg.split()]
    w = [x for x in w if x]
    while w and (re.fullmatch(r"\w+=\S*", w[0]) or w[0] in WRAPPERS or w[0] in SHELL_WORDS | SHELL_HEADERS):
        if w[0] in SHELL_HEADERS:
            return []   # "for f in *", "do for x in ...": a loop header, no program
        m = re.fullmatch(r"\w+=[\"']?\$\((\S+)", w[0])   # x=$(date +%s): what runs is date
        wrapper = w[0] in WRAPPERS
        w = ([m.group(1)] if m else []) + w[1:]
        while wrapper and w and re.fullmatch(r"-\S*|\d[\d.]*[smhd]?|[A-Z][A-Z0-9]*", w[0]):
            w = w[1:]   # the wrapper's own options / duration (timeout 100 zsh ...): what runs is zsh
    return w


def program(cmd):
    """What a shell command runs: the test runner, "git status", else the first segment's program."""
    tr = test_runner(cmd)
    if tr:
        return tr
    w = last = []
    for seg in re.split(r"&&|\|\||[;|\n]", cmd):
        ws = _words(seg)
        if not ws or not PROG_NAME.fullmatch(os.path.basename(ws[0])):
            continue   # no program here (a number, a flag, a function definition, "[ ... ]")
        last = ws
        if os.path.basename(ws[0]) not in intents.SKIP:
            w = ws
            break
    w = w or last
    if not w:
        return None
    prog = os.path.basename(w[0])
    if prog in SUB_PROGS and len(w) > 1 and re.fullmatch(r"[a-z][\w-]*", w[1]):
        return "%s %s" % (prog, w[1])
    return prog


def display_name(name, inp):
    """Tool display name: "Bash <program>", "✦ <skill name>", "<server>.<tool>" for MCP, else the tool name."""
    inp = inp if isinstance(inp, dict) else {}
    if name == "Bash":
        return ("Bash " + (program(" ".join(str(inp.get("command") or "").split())) or "")).strip()
    if name == "Skill":
        return ("✦ " + (tool_skill(name, inp) or "skill")).strip()
    if name.startswith("mcp__"):
        parts = name.split("__")
        return "%s.%s" % (parts[1] if len(parts) > 1 else "?", parts[-1] if len(parts) > 2 else "?")
    return name


def _digest(turn):
    """Pure per-turn facts, cached on the turn dict (recomputed when its tool list grows)."""
    tools = turn.get("tools") or []
    hit = turn.get("_d")
    if hit and hit[0] == len(tools):
        return hit[1]
    recs, names, files, cmds, agents, todos, declared, plan, runners, loads = [], [], [], [], [], [], None, None, [], []
    skills = []
    kind = None
    for t in tools:
        name, inp = str(t.get("name") or "?"), t.get("input") if isinstance(t.get("input"), dict) else {}
        if is_declare(name):
            lab = " ".join(str(inp.get("label") or "").split())[:120]
            if lab:
                declared, kind = lab, str(inp.get("kind") or "").strip().lower() or None
            continue
        if name == "TodoWrite" and isinstance(inp.get("todos"), list):
            todos.append(inp["todos"])
        if name == "ToolSearch":
            loads.append(loaded_tools(inp.get("query")))
        if tool_skill(name, inp):
            skills.append(tool_skill(name, inp))
        if name == "ExitPlanMode":
            n = len(re.findall(r"(?m)^\s*(?:[-*]|\d+[.)])\s+\S", str(inp.get("plan") or "")))
            plan = "Plan %d steps" % n if n >= 2 else "Write a plan"
        rec = intents.record(name, inp)
        names.append(display_name(name, inp))
        if name not in NEUTRAL:
            recs.append(rec)
        base = os.path.basename(rec.get("file") or "")
        if re.fullmatch(r"[\w.@+-]+", base):
            files.append(base)
        if name == "Bash":
            p = program(rec.get("cmd") or "")
            if p:
                cmds.append(p)
            tr = test_runner(rec.get("cmd") or "")
            if tr:
                runners.append(names[-1])
        if name in ("Agent", "Task"):
            agents.append(" ".join(str(inp.get("description") or inp.get("subagent_type") or "subagent").split()))
    intent = intents.turn_intent(recs)[0] if recs else None
    d = {"intent": intent, "group": intents.GROUP.get(intent, "other") if intent else None, "names": names,
         "files": files, "cmds": cmds, "agents": agents, "todos": todos, "declared": declared, "plan": plan,
         "runners": runners, "kind": kind, "loads": loads, "skills": skills}
    turn["_d"] = (len(tools), d)
    return d


def loaded_tools(query):
    """The tools a ToolSearch call loads: ["declare_intent"] for "select:mcp__srv__declare_intent", [] for a
    keyword search (what it finds is not known from the input)."""
    q = str(query or "").strip()
    if not q.lower().startswith("select:"):
        return []
    return [n.split("__")[-1] for n in (x.strip() for x in q[7:].split(",")) if n]


def _todo_label(item):
    return " ".join(str(item.get("activeForm") or item.get("content") or "").split())[:120] or None


def _is_search(name):
    return name == "ToolSearch"


def call_parts(names):
    """Part index of each call of a turn ([None] * n when the turn is not split): a turn mixing ToolSearch with
    other calls splits, in call order, into runs of ToolSearch calls and runs of the other calls (usually "load
    the tool" then "use it": two parts). Calls only append, so parts only append (incremental-stable)."""
    search = [_is_search(n) for n in names]
    if not any(search) or all(search):
        return [None] * len(names)
    out = []
    for k, x in enumerate(search):
        out.append(0 if k == 0 else out[-1] + (x != search[k - 1]))
    return out


def _parts(turn):
    """[(part, turn)]: the parts of a turn (see call_parts), else [(None, turn)]. The turn's CW / $ split evenly per
    call (as the live ops list does: declare_intent calls not counted); thinking and
    text go to the first part that is not ToolSearch, the test outcome and subagent / workflow cost to the last."""
    tools = turn.get("tools") or []
    idx = call_parts([t.get("name") for t in tools])
    if not idx or idx[0] is None:
        return [(None, turn)]
    hit = turn.get("_p")
    if not hit or hit[0] != len(tools):
        subs = [{"tools": []} for _ in range(idx[-1] + 1)]
        for t, j in zip(tools, idx):
            subs[j]["tools"].append(t)
        hit = turn["_p"] = (len(tools), subs)
    subs = hit[1]
    n = len([t for t in tools if not is_declare(t.get("name"))])
    cw, usd = float(turn.get("cw") or 0), float(turn.get("usd") or 0)
    work = [j for j, x in enumerate(subs) if not _is_search(x["tools"][0].get("name"))]
    for x in subs:
        share = len([t for t in x["tools"] if not is_declare(t.get("name"))]) / n   # n > 0: a part is ToolSearch
        x.update(ts=turn.get("ts"), ts_end=turn.get("ts_end"), cw=cw * share, usd=usd * share, thinking=0, text="", test=None, sub_cw=0,
                 sub_usd=0, wf_agents=0)
    subs[work[0]].update(thinking=turn.get("thinking") or 0, text=turn.get("text") or "")
    subs[work[-1]].update({k: turn.get(k) for k in ("test", "sub_cw", "sub_usd", "wf_agents")})
    return list(enumerate(subs))


def _new_step(i, k, turn, cat, prompt_before, part=None):
    return {"i": i, "cat": cat, "turn_first": k, "turn_last": k, "part_first": part, "part_last": part,
            "ts0": turn.get("ts"), "ts1": turn.get("ts"),
            "cw": 0.0, "usd": 0.0, "turns": 0, "tools": Counter(), "thinking": 0, "files": [], "cmds": [],
            "tested": None, "todo": None, "declared": None, "kind": None, "agents": [], "prompt_before": prompt_before,
            "closed": False, "text": "", "intents": Counter(), "plan": None, "runners": [], "wf_agents": 0,
            "loads": [], "skills": []}


def segment(turns, events=(), open_last=True):
    """Steps of a main thread (rules 1-6 of the spec, see module doc for the input format)."""
    prompts = {e.get("at") for e in events or () if e.get("kind") == "prompt" or (e.get("kind") == "skill"
                                                                                   and e.get("user"))}
    slash = {}
    for e in events or ():
        if e.get("kind") == "skill" and e.get("name"):
            slash.setdefault(e.get("at"), []).append(str(e["name"]))
    steps, cur = [], None
    todo_state, active, declared, kind = {}, None, None, None
    prev_test = prev_moved = prev_launch = prev_search = False
    for k, part, turn in ((k, p, t) for k, whole in enumerate(turns) for p, t in _parts(whole)):
        d = _digest(turn)
        prompt = k in prompts and not part
        search = set(d["names"]) - NEUTRAL == {"ToolSearch"}   # loading deferred tools: always a step of its own
        if prompt:
            declared = kind = None   # a declared intent does not outlive the request it was declared for
        if d["declared"]:
            declared, kind = d["declared"], d["kind"]
        g = "delegate" if d["agents"] else ("plan" if d["plan"] and not d["group"] else d["group"])
        carried = prev_test or prev_moved or prev_launch or prev_search
        new = cur is None or prompt or carried or search or bool(d["agents"]) or bool(d["declared"]) \
            or (g is not None and cur["cat"] is not None and g != cur["cat"])
        defer = new and cur is not None and not prompt and not search and g is None and not d["declared"]
        if defer:
            new = False   # a bookkeeping-only turn (todo update) stays with the step it ends; the boundary waits
        if new:
            if cur is not None:
                cur["closed"] = True
            cur = _new_step(len(steps), k, turn, g, prompt, part)
            cur["todo"], cur["declared"], cur["kind"] = active, declared, kind
            steps.append(cur)
        elif cur["cat"] is None and g is not None:
            cur["cat"] = g   # a step opened by a bookkeeping turn (declare_intent / todo update) takes the next category
        # todo bookkeeping (rule 5 + PLAN evidence)
        moved = False
        for todos in d["todos"]:
            prev = dict(todo_state)
            todo_state = {str(t.get("content") or ""): str(t.get("status") or "") for t in todos}
            moved = moved or any(st in ("in_progress", "completed") and prev.get(c) != st
                                 for c, st in todo_state.items())
            if not (set(prev) & set(todo_state)) and len(todos) >= 2 and d["group"] in (None, "plan") \
                    and not d["agents"]:
                cur["plan"] = "Plan %d tasks" % len(todos)
                if cur["cat"] in (None, "plan"):
                    cur["cat"] = "plan"
            act = next((t for t in todos if t.get("status") == "in_progress"), None)
            active = _todo_label(act) if act else None
        if d["plan"] and d["group"] in (None, "plan"):
            cur["plan"] = cur["plan"] or d["plan"]
        # accumulate
        cur["turn_last"], cur["part_last"] = k, part
        cur["ts0"] = cur["ts0"] or turn.get("ts")
        cur["ts1"] = max([x for x in (cur["ts1"], turn.get("ts"), turn.get("ts_end")) if x] or [None])
        cur["turns"] += 1
        cur["cw"] += float(turn.get("cw") or 0) + float(turn.get("sub_cw") or 0)
        cur["usd"] += float(turn.get("usd") or 0) + float(turn.get("sub_usd") or 0)
        cur["tools"].update(d["names"])
        cur["thinking"] += int(turn.get("thinking") or 0)
        cur["wf_agents"] += int(turn.get("wf_agents") or 0)
        for f in d["files"]:
            if f not in cur["files"]:
                cur["files"].append(f)
        for c in d["cmds"]:
            if c not in cur["cmds"]:
                cur["cmds"].append(c)
        cur["runners"] += [r for r in d["runners"] if r not in cur["runners"]]
        cur["agents"] += d["agents"]
        cur["loads"] += d["loads"]
        for n in (slash.get(k, []) if not part else []) + d["skills"]:
            if n not in cur["skills"]:
                cur["skills"].append(n)
        if d["intent"]:
            cur["intents"][d["intent"]] += 1
        if turn.get("test"):
            cur["tested"] = "fail" if "fail" in (turn["test"], cur["tested"]) else "pass"
        text = " ".join(str(turn.get("text") or "").split())
        if text and len(cur["text"]) < TEXT_CHARS:
            cur["text"] = (cur["text"] + " " + text).strip()[:TEXT_CHARS]
        if defer:
            prev_moved = prev_moved or moved
        else:
            prev_test, prev_moved, prev_launch = bool(turn.get("test") or d["runners"]), moved, bool(d["agents"])
            prev_search = search
    if cur is not None:
        # a step that ran tests or moved a todo item is already over (rules 2 and 5)
        cur["closed"] = not open_last or prev_test or prev_moved
    for s in steps:
        s["cw"], s["usd"] = round(s["cw"], 1), round(s["usd"], 6)
        s["dur_s"] = duration(s.get("ts0"), s.get("ts1"))
        s["cat"] = "overhead" if overhead_label(s) else s["cat"] or "other"
    skill_spans(steps)
    # a todo / declared span of >= 2 steps labels its group, not each step (see groups); the last step's span may
    # still grow while the session goes on, so a todo / declared label is not locked on it yet
    for key in ("todo", "declared"):
        for a, b, _ in _runs(steps, key):
            for i in range(a, b + 1):
                steps[i][key + "_span"] = b - a + 1
    if steps and open_last:
        steps[-1]["span_open"] = True
    return steps


def skill_spans(steps):
    """A step that only loads a skill (skill_label) takes ~0s itself (its Skill call returns at once), so its wall
    time is the work the skill drove: from the skill load to the end of the last following step before the next
    user prompt or the next step that loads a skill. Sets `drive_last` (that step's index), `drive_ts1` (the latest
    end over the span) and `dur_s` on such steps; other steps keep their own first-to-last-event time. Groups take
    `drive_ts1` into their span (_stats), so a group is never shorter than a skill step in it; ops keep each call's
    own tool_use -> tool_result time."""
    for n, s in enumerate(steps):
        if not skill_label(s):
            continue
        j = n
        while j + 1 < len(steps) and not steps[j + 1]["prompt_before"] and not steps[j + 1]["skills"]:
            j += 1
        ends = [x["ts1"] for x in steps[n:j + 1] if x.get("ts1")]
        s["drive_last"], s["drive_ts1"] = j, max(ends) if ends else s.get("ts1")
        s["dur_s"] = duration(s.get("ts0"), s["drive_ts1"])
    return steps


# ---------------------------------------------------------------- summaries and labels

def duration(ts0, ts1):
    """Seconds from ts0 to ts1 (rounded to 0.1), None when either is unknown."""
    return round(max(0.0, ts1 - ts0), 1) if ts0 and ts1 else None


def fmt_dur(s):
    """Compact wall time: "4s", "1m12s", "1h3m" ("" for None)."""
    if s is None:
        return ""
    v = int(max(0, s))
    h, m = v // 3600, v % 3600 // 60
    return "%dh%dm" % (h, m) if h else "%dm%ds" % (m, v % 60) if m else "%ds" % v


def summary(step):
    """"✦ pr-review · Read ×6 · Grep ×2 · Bash unittest ✓ · 1 thinking": the skills that drove the step first (Skill
    tool calls and slash-command skills), then the top 4 other tools by count, then thinking. Workflow calls that
    launched agents are always shown, with the agent count: "Workflow ×1 (12 agents)"."""
    skills = ["✦ " + n for n in step.get("skills") or ()]
    tools = {k: v for k, v in (step.get("tools") or {}).items() if k not in skills}
    order = list(tools)
    wf = int(step.get("wf_agents") or 0)
    top = sorted(tools.items(), key=lambda kv: (not (wf and kv[0] == "Workflow"), -kv[1], order.index(kv[0])))[:4]
    top.sort(key=lambda kv: (-kv[1], order.index(kv[0])))
    parts = skills[:2] + (["✦ +%d" % (len(skills) - 2)] if len(skills) > 2 else [])
    for name, n in top:
        s = name + (" ×%d" % n if n > 1 else "")
        if wf and name == "Workflow":
            s = "Workflow ×%d (%d agent%s)" % (n, wf, "s" if wf != 1 else "")
        if step.get("tested") and name in (step.get("runners") or ()):
            s += " ✓" if step["tested"] == "pass" else " ✗"
        parts.append(s)
    if step.get("thinking"):
        parts.append("%d thinking" % step["thinking"])
    return " · ".join(parts)


def clip_words(text, n=MAX_WORDS, chars=60):
    w = " ".join(str(text or "").split()).strip(" .:;,-")
    words = w.split()
    if len(words) > n:
        w = " ".join(words[:n])
    if len(w) > chars:
        w = w[:chars - 1].rsplit(" ", 1)[0].rstrip(" ,;:-") + "…"
    return w


def subagent_label(descs):
    descs = [d for d in descs if d]
    if not descs:
        return None
    if len(descs) == 1:
        return clip_words(descs[0])
    firsts = {d.split()[0].lower() for d in descs}
    if len(firsts) == 1 and all(len(d.split()) > 1 for d in descs):
        verb = descs[0].split()[0]
        return clip_words("%s ×%d: %s" % (verb, len(descs), ", ".join(" ".join(d.split()[1:]) for d in descs)))
    return clip_words("%d subagents: %s" % (len(descs), ", ".join(descs)))


def overhead_label(step):
    """"Load declare_intent tool" / "Load 3 deferred tools" for a step whose only tool is ToolSearch (loading
    deferred tool schemas: overhead, not work), else None."""
    tools = set(step.get("tools") or ()) - NEUTRAL
    if tools != {"ToolSearch"}:
        return None
    names = list(dict.fromkeys(n for q in step.get("loads") or () for n in q))
    if len(names) == 1:
        return clip_words("Load %s tool" % names[0])
    return "Load %d deferred tools" % len(names) if names else "Load deferred tools"


def skill_label(step):
    """"Use skill tdd" / "Use skills a, b" for a step that only loads skills (Skill tool calls and / or a
    slash-command skill, no other tool), else None. The step keeps its category."""
    tools = set(step.get("tools") or ()) - NEUTRAL
    names = step.get("skills") or []
    if not names or any(not t.startswith("✦ ") for t in tools):
        return None
    return clip_words(("Use skill %s" if len(names) == 1 else "Use skills %s") % ", ".join(names))


def label_from_signals(step):
    """(label, source) from the agent's own signals: a ToolSearch-only or skill-only step (a deterministic "rule"
    label, never sent to the model), plan evidence, todo item, subagent descriptions, declared intent; (None, None) when there
    is none. A todo item / declared label spanning >= 2 steps belongs to their
    group, so those steps fall through to the next source (in the end a model or rule label of their own)."""
    lab = overhead_label(step) or skill_label(step)
    if lab:
        return lab, "rule"
    if step.get("plan") and step.get("cat") == "plan":
        return step["plan"], "plan"
    if step.get("todo") and step.get("todo_span", 1) < 2:
        return clip_words(step["todo"]), "todo"
    if step.get("agents"):
        return subagent_label(step["agents"]), "subagent"
    if step.get("declared") and step.get("declared_span", 1) < 2:
        return clip_words(step["declared"]), "declared"
    return None, None


def rule_label(step):
    """Provisional label "<intent label> · <top targets>", e.g. "editing files · store.py, cli.py"."""
    its = step.get("intents") or Counter()
    intent = its.most_common(1)[0][0] if its else None
    base = intents.label(intent) if intent else {"plan": "planning"}.get(step.get("cat"), "working")
    targets = (step.get("files") or [])[:2] or [c for c in step.get("cmds") or () if c not in INTERPRETERS][:2]
    return base + (" · " + ", ".join(targets) if targets else "")


# ---------------------------------------------------------------- groups

def _runs(steps, key):
    """Maximal runs (first, last, value) of consecutive steps with the same non-empty key, split at prompts."""
    out, cur = [], None
    for s in steps:
        v = s.get(key)
        if cur and v and v == cur[2] and not s.get("prompt_before"):
            cur[1] = s["i"]
            continue
        if cur:
            out.append(tuple(cur))
        cur = [s["i"], s["i"], v] if v else None
    if cur:
        out.append(tuple(cur))
    return out


def _stats(g, steps):
    sub = steps[g["first"]:g["last"] + 1]
    cat = Counter()
    tools = Counter()
    skills = []
    for s in sub:
        cat[s["cat"]] += s.get("cw") or 0
        tools.update(s.get("tools") or {})
        skills += [n for n in s.get("skills") or () if n not in skills]
    ts = [t for s in sub for t in (s.get("ts0"), s.get("ts1"), s.get("drive_ts1")) if t]
    out = dict(g)
    out.update(cat=KIND_CAT.get(g.get("kind")) or (max(cat, key=cat.get) if cat else "other"), cw=round(sum(s["cw"] for s in sub), 1),
               usd=round(sum(s["usd"] for s in sub), 6), steps=len(sub), skills=skills,
               ts0=min(ts) if ts else None, ts1=max(ts) if ts else None,
               dur_s=duration(min(ts), max(ts)) if ts else None,
               summary=summary({"tools": tools, "thinking": sum(s.get("thinking") or 0 for s in sub),
                                "wf_agents": sum(s.get("wf_agents") or 0 for s in sub), "skills": skills}))
    return out


def groups(steps, model_groups=None, prior=None):
    """Intent groups over steps: todo runs, declared runs, then validated model proposals ([{"label", "first",
    "last"}]: contiguous, >= 2 steps, only ungrouped closed steps, never across a prompt boundary). `prior` are
    already locked groups (kept as they are; a todo/declared one grows while its item stays active)."""
    out = [dict(g) for g in prior or ()]
    taken = {i for g in out for i in range(g["first"], g["last"] + 1)}
    for key in ("todo", "declared"):
        for a, b, v in _runs(steps, key):
            old = next((g for g in out if g["source"] == key and g["first"] == a and g["label"] == clip_words(v)), None)
            if old:
                taken.update(range(old["last"] + 1, b + 1))
                old["last"] = max(old["last"], b)
            elif b > a and not taken & set(range(a, b + 1)):
                g = {"label": clip_words(v), "source": key, "first": a, "last": b}
                if key == "declared" and steps[a].get("kind"):
                    g["kind"] = steps[a]["kind"]
                out.append(g)
                taken.update(range(a, b + 1))
    n = len(steps)
    for g in model_groups or ():
        try:
            a, b, label = int(g["first"]), int(g["last"]), clip_words(g["label"])
        except (KeyError, TypeError, ValueError):
            continue
        if not label or a < 0 or b >= n or b - a < 1:
            continue
        rng = range(a, b + 1)
        if taken & set(rng) or not all(steps[i]["closed"] for i in rng) \
                or any(steps[i].get("prompt_before") for i in range(a + 1, b + 1)):
            continue
        out.append({"label": label, "source": g.get("source") or "haiku", "first": a, "last": b})
        taken.update(rng)
    return [_stats(g, steps) for g in sorted(out, key=lambda g: g["first"]) if g["last"] < n]


PENDING_LABEL = "pending intent"


def intent_blocks(steps, gs):
    """The intents a flow view lists, in step order, each with its steps: an intent group; a run of steps whose
    label is not settled yet (not locked: open, or waiting for the model) outside any group, as one "pending" block
    (its intent is not known yet); a settled step outside any group, as its own one-step intent. Every step is in
    exactly one block. [{"kind": "group" | "pending" | "step", "group" (index in gs, else None), "first", "last",
    "label", "cat", "source", "steps" (count), "cw", "usd", "dur_s", "skills"}]."""
    gi = {i: n for n, g in enumerate(gs) for i in range(g["first"], g["last"] + 1)}
    out, cur = [], None
    for x in steps:
        n = gi.get(x["i"])
        key = ("group", n) if n is not None else ("pending", None) if not x.get("locked") else ("step", x["i"])
        if cur is not None and cur[0] == key and cur[1][-1]["i"] == x["i"] - 1:
            cur[1].append(x)
            continue
        cur = (key, [x])
        out.append(cur)
    blocks = []
    for (kind, ref), xs in out:
        if kind == "group":
            g = gs[ref]
            b = {"label": g["label"], "cat": g["cat"], "source": g["source"], "steps": g["steps"], "cw": g["cw"],
                 "usd": g["usd"], "dur_s": g.get("dur_s"), "skills": g.get("skills") or [], "first": g["first"],
                 "last": g["last"]}
        else:
            last = xs[-1]
            ts = [t for x in xs for t in (x.get("ts0"), x.get("ts1"), x.get("drive_ts1")) if t]
            skills = []
            for x in xs:
                skills += [k for k in x.get("skills") or () if k not in skills]
            b = {"label": PENDING_LABEL if kind == "pending" else last["label"], "cat": last["cat"],
                 "source": None if kind == "pending" else last.get("source"), "steps": len(xs),
                 "cw": round(sum(x["cw"] for x in xs), 1), "usd": round(sum(x["usd"] for x in xs), 6),
                 "dur_s": duration(min(ts), max(ts)) if ts else None, "skills": skills, "first": xs[0]["i"],
                 "last": last["i"]}
        b.update(kind=kind, group=ref if kind == "group" else None)
        blocks.append(b)
    return blocks


class Labeller:
    """Locked step and group labels. A closed step is labelled once (signals, else a model label, else -- after
    finalize(no_model=True) -- its rule label) and never renamed; open / pending steps show the rule label."""

    def __init__(self):
        self.locked = {}          # step i -> (label, source)
        self.model = {}           # step i -> model label not applied yet
        self.proposals = []       # model group proposals not tried yet
        self.groups = []          # locked groups [{"label", "source", "first", "last"}]
        self.no_model = False
        self.inflight = set()     # step indices sent to the model, answer not back yet
        self.stats = {"haiku_calls": 0, "haiku_tokens": 0, "haiku_usd": 0.0}

    def add_model_labels(self, labels, groups_=None, usage=None):
        for i, lab in (labels or {}).items():
            try:
                i = int(i)
            except (TypeError, ValueError):
                continue
            self.inflight.discard(i)
            if i not in self.locked and lab:
                self.model[i] = clip_words(lab)
        self.proposals.extend(g for g in groups_ or () if isinstance(g, dict))
        if usage:
            self.stats["haiku_calls"] += int(usage.get("calls") or 0)
            self.stats["haiku_tokens"] += int(usage.get("tokens") or 0)
            self.stats["haiku_usd"] = round(self.stats["haiku_usd"] + float(usage.get("usd") or 0), 6)

    def give_up(self, idx):
        """No model label will come for these steps: lock their rule labels when they are assigned."""
        for i in idx:
            if i not in self.locked:
                self.model[int(i)] = None

    def release(self, idx):
        """The model did not answer for these steps: they may be asked again."""
        self.inflight.difference_update(idx)

    def finalize(self, steps=None, no_model=True):
        self.no_model = no_model
        return self.assign(steps) if steps is not None else None

    def pending(self, steps):
        """Closed steps still waiting for a model label (not in flight)."""
        return [s for s in steps if s["closed"] and s["i"] not in self.locked and s["i"] not in self.inflight
                and label_from_signals(s)[0] is None]

    def assign(self, steps):
        for s in steps:
            i = s["i"]
            if i in self.locked:
                s["label"], s["source"] = self.locked[i]
                s["locked"] = True
                continue
            lab, src = label_from_signals(s)
            if lab is None and s["closed"]:
                if i in self.model:
                    lab = self.model.pop(i)
                    lab, src = (lab, "haiku") if lab else (rule_label(s), "rule")
                elif self.no_model:
                    lab, src = rule_label(s), "rule"
            if lab is not None and s["closed"] and not (src in ("todo", "declared") and s.get("span_open")):
                self.locked[i] = (lab, src)
                s["label"], s["source"], s["locked"] = lab, src, True
            else:
                s["label"], s["source"] = (lab, src) if lab else (rule_label(s), "rule")
                s["locked"] = False
        tried, self.proposals = self.proposals, []   # a proposal is validated once: accepted (locked) or dropped
        gs = groups(steps, tried, prior=self.groups)
        self.groups = [{k: g[k] for k in ("label", "source", "first", "last", "kind") if k in g} for g in gs]
        return gs
