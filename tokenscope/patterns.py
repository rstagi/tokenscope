"""Patterns: mine recurring real behavior (sequences, re-discovery, retries, wasted context, ...) from transcripts.

Two stages, so the miners stay fast on thousands of transcripts:
  1. session_records(session, analysis, understood) runs in the per-session worker and reduces every transcript
     (main, Task subagents, workflow agents) to compact per-tool-call records carrying the analyzer's tokens;
  2. mine(records, understood) streams over those records with one function per miner.

Tokens of a tool call (all from the analyzer's per-call usage and attribution, never guessed):
  new        = tokens the analyzer attributed to its result in the next call (chars/4 estimate unless scaled to the
               measured delta; flagged `est`)
  output     = the emitting API call's output tokens / tool calls in that call (split is an estimate)
  cache_read = the emitting call's cache read / tool calls in that call (split is an estimate)
  cost-weighted and $ use the same split of the measured per-call usage (pricing.py).
"""
import difflib
import json
import os
import re
import shlex
import statistics
from collections import Counter, defaultdict
from urllib.parse import urlparse

from . import pricing
from . import understand as un

# ---------------------------------------------------------------- thresholds (minimum support etc.)
NGRAM_SIZES = (3, 4, 5)
SEQ_MIN_SESSIONS = 3          # n-gram must recur in this many sessions...
SEQ_MIN_PROJECTS = 2          # ...or this many projects
CHAIN_MIN_RUN = 5             # identical calls in a row worth reporting
SEQ_CANDIDATES = 300          # top n-grams (by cost-weighted) considered before overlap de-duplication
SEQ_OVERLAP = 0.8             # drop an n-gram whose calls overlap a better one by this share
START_K = 8                   # first K tool calls of a session / subagent = its "start"
START_MIN_SESSIONS = 3
FANOUT_MIN_SIBLINGS = 3
EDIT_FAIL_MIN_CYCLES = 2
RETRY_MIN_RUN = 3
RETRY_SIMILARITY = 0.6        # difflib ratio between consecutive commands = "small variation"
ERR_MIN_COUNT = 20
ERR_MIN_RATE = 0.20
ERR_MIN_CALLS = 5
DENIED_LOOKAHEAD = 3
BIG_RESULT_TOKENS = 5000
DISTINCT_MAX = 40             # distinctive tokens checked per result
DIVERGENT_MIN_RUNS = 4
DIVERGENT_SAVING = 0.30
PATH_KINDS = 4
CORRECTION_MAX_CHARS = 200
COMPACT_BEFORE = 10
PHASE_COST_X = 2.0            # workflow phase cost per agent above this x the median agent => flagged
PHASE_FAIL_RATE = 0.10
TOOLSEARCH_MIN = 3
MCP_UNUSED_MIN_SESSIONS = 3
EXAMPLES = 5

SUGGESTION_TYPES = ("skill", "claude_md", "script", "hook", "permission", "mcp_config", "workflow", "none")
EDIT_TOOLS = ("Edit", "MultiEdit", "Write", "NotebookEdit", "edit_file", "write_file", "write_to_file", "replace_file_content", "multi_replace_file_content")
FILE_TOOLS = ("Read", "read_file", "view_file") + EDIT_TOOLS
SUBCMD = ("npm", "pnpm", "yarn", "git", "gh", "uv", "npx", "bunx", "pnpx", "docker", "docker-compose", "make",
          "cargo", "go", "kubectl", "bun", "pip", "pip3", "poetry", "terraform", "brew", "apt", "apt-get")
WRAPPERS = ("sudo", "time", "env", "nice", "nohup", "command", "exec", "xargs")
BASH_READERS = ("cat", "sed", "head", "tail")
BASH_SEARCH = ("grep", "rg", "find", "egrep", "fgrep", "ag", "fd")
REC_ERR_MIN_COUNT = 10
REC_ERR_MIN_SESSIONS = 3
REC_ERR_CHARS = 80
CHAIN_MAX = 10                # identical-arg chain patterns kept (recurring: >= 2 sessions or >= 3 runs)
SEQ_MAX = 30                  # ranked sequence patterns kept (identical-arg chains come on top)
SEQ_CONTAINED = 0.8           # drop an n-gram inside a longer kept one that has >= this share of its count
TOOL_CHOICE_MIN = 5
TOOL_CHOICE_SHARE = 0.5
PAGERS = ("sed", "cat", "head", "tail", "nl", "awk", "less", "more", "bat")
DENIED_RE = re.compile(r"permission (?:was |has been )?denied|not allowed|user (?:rejected|denied|declined)|"
                       r"doesn't want to proceed|requires approval|was blocked by|denied by (?:the )?user", re.I)
CORRECTION_RE = re.compile(r"^\W*(?:no\b|nope\b|that'?s wrong|wrong\b|revert\b|undo\b|i meant\b|not what i\b|stop\b|"
                           r"don'?t\b|why did you\b|actually\b|wait\b|you broke\b|still (?:failing|broken|not)\b)", re.I)
CORRECTION_ANY_RE = re.compile(r"\b(?:nope|that'?s wrong|revert|undo|i meant|not what i|why did you|you broke|"
                               r"still (?:failing|broken|not))\b", re.I)
CORRECTION_RULES = ("prompt starts with: no, nope, that's wrong, wrong, revert, undo, I meant, not what I, stop, "
                    "don't, why did you, actually, wait, you broke, still failing|broken|not",
                    "or the prompt is <= %d chars and contains nope / that's wrong / revert / undo / I meant / not "
                    "what I / why did you / you broke / still failing|broken|not" % CORRECTION_MAX_CHARS)
_TOKEN_RE = re.compile(r"[A-Za-z0-9_./:-]{6,}")
_WORKTREE_RE = re.compile(r"-(?:integration|review(?:-[\w.]+)?|issue-\d+|feat(?:ure)?-[\w.-]+|fix-[\w.-]+|wt-[\w.-]+)$")


# ---------------------------------------------------------------- normalization
def _seg_norm(toks):
    """'prog [sub]' of one command segment (None for cd/export/... and loop headers)."""
    toks = list(toks)
    while toks and toks[0] in un.SHELL_KEYWORDS:
        toks.pop(0)
    if not toks or toks[0] in un.COMPOUND_HEADS:
        return None
    while toks and (re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", toks[0]) or toks[0] in WRAPPERS):
        toks.pop(0)
    if toks and toks[0] == "timeout":
        toks = toks[1:]
        while toks and (toks[0].startswith("-") or re.match(r"^\d", toks[0])):
            toks.pop(0)
    if not toks or toks[0] in ("cd", "pushd", "popd", "export", "source", ".", "set", "true", ":"):
        return None
    prog = toks[0]
    base = prog if prog.startswith(("./", "../")) else (os.path.basename(prog.rstrip("/")) or prog)
    args = [t for t in toks[1:] if not t.startswith(("<", ">")) and not re.match(r"^\d?>", t)]
    if base in ("python", "python3") and len(args) >= 2 and args[0] == "-m":
        return "%s -m %s" % (base, args[1])
    if base in SUBCMD:
        if base == "git":
            while len(args) >= 2 and args[0] in ("-C", "-c", "--git-dir", "--work-tree"):
                args = args[2:]
        sub = next((a for a in args if not a.startswith("-")), None)
        if sub and base in ("npx", "bunx", "pnpx"):
            sub = os.path.basename(sub)
        if not sub or re.search(r"[/\s=]", sub):
            return base
        if base in ("npm", "pnpm", "yarn") and sub in ("run", "exec"):
            nxt = next((a for a in args[args.index(sub) + 1:] if not a.startswith("-")), "")
            if nxt and not re.search(r"[/\s=]", nxt):
                sub = "%s %s" % (sub, nxt)
        return "%s %s" % (base, sub)
    return base


def bash_segments(cmd):
    out = []
    for seg in un._split_segments(str(cmd or "").strip()):
        n = (_seg_norm(seg) or "").strip()
        if n:
            out.append(n)
    return out or ["sh"]


def bash_file_arg(cmd):
    """Last path-like argument of the primary segment (for `sed -n 1,80p f` paging detection)."""
    for seg in un._split_segments(str(cmd or "")):
        if _seg_norm(seg) is None:
            continue
        cands = [t for t in seg[1:] if not t.startswith("-") and ("/" in t or "." in t) and not re.match(r"^\d", t)]
        return cands[-1] if cands else None
    return None


def kinds(name, inp, target):
    """(coarse kind, fine kind) of a tool call; fine carries the normalized argument."""
    inp = inp if isinstance(inp, dict) else {}
    mp = un.mcp_parts(name)
    if name == "Bash":
        return "Bash", "Bash(%s)" % bash_segments(inp.get("command"))[0]
    if name in FILE_TOOLS:
        return ("Edit" if name == "MultiEdit" else name), "%s(%s)" % ("Edit" if name == "MultiEdit" else name, target)
    if name in ("Grep", "Glob"):
        return name, '%s("%s")' % (name, str(inp.get("pattern", ""))[:60])
    if mp:
        return "MCP", "MCP(%s.%s)" % mp
    if name in ("Agent", "Task"):
        return "Agent", "Agent(%s)" % (inp.get("subagent_type") or "general-purpose")
    if name == "Skill":
        return "Skill", "Skill(%s)" % (inp.get("skill") or inp.get("name") or inp.get("command") or "?")
    if name == "WebFetch":
        return name, "WebFetch(%s)" % (urlparse(str(inp.get("url") or "")).netloc or "?")
    if name in ("TodoWrite", "TodoRead", "TaskCreate", "TaskUpdate", "TaskList"):
        return "Todo", "Todo"
    return name, name


def family(cwd, slug):
    """Repo name of a session: git root basename when it exists here, else the cwd basename with worktree /
    conductor suffixes removed, else the slug."""
    if cwd:
        p = cwd.rstrip("/")
        for marker in ("/.claude/worktrees/", "/.worktrees/"):   # in-repo worktrees: the repo is before the marker
            if marker in p:
                p = p.split(marker)[0]
        if "/worktrees/" in p:   # worktree managers (~/.ws/worktrees/<repo>/<branch>, ~/.loop/worktrees/loop-<plan>/<user>--<repo>/<role>)
            segs = [x for x in p.split("/worktrees/", 1)[1].split("/") if x]
            if len(segs) >= 2 and segs[0].startswith("loop-"):
                return segs[1].split("--")[-1] or slug
            if len(segs) >= 2:
                return segs[0]
            if segs:
                return _WORKTREE_RE.sub("", segs[0]) or slug
        parts = p.split("/")
        if "conductor" in parts and "workspaces" in parts:
            i = parts.index("workspaces")
            if i + 1 < len(parts):
                return parts[i + 1]
        d = p
        while d and d != "/":
            if os.path.exists(os.path.join(d, ".git")):
                return os.path.basename(d)
            d = os.path.dirname(d)
        return _WORKTREE_RE.sub("", os.path.basename(p)) or slug
    return slug


def distinctive(text):
    """Identifiers / paths / numbers >= 6 chars that are not plain lowercase words."""
    out = []
    for t in _TOKEN_RE.findall(text or ""):
        t = t.strip(".:/-")
        if len(t) >= 6 and (re.search(r"[\d_/.]", t) or re.search(r"[a-z][A-Z]", t)) and t not in out:
            out.append(t)
            if len(out) >= DISTINCT_MAX:
                break
    return out


# ---------------------------------------------------------------- stage 1: records (worker side)
def _rates(c):
    """(cost-weighted per new token, $ per new token) of a consuming call's input side."""
    u = c["usage"]
    new = u["input"] + u["cache_write"]
    if not new:
        return 1.0, 0.0
    cw = (u["input"] * pricing.W_INPUT + u["cache_write"] * pricing.W_CACHE_WRITE) / new
    usd = pricing.cost_usd(c.get("model"), {"input": u["input"], "cache_write": u["cache_write"],
                                            "cache_write_1h": u["cache_write_1h"]}) or 0.0
    return cw, usd / new


def _emit_share(c, n):
    u = c["usage"]
    out, cr = u["output"] / n, u["cache_read"] / n
    usd = pricing.cost_usd(c.get("model"), {"output": u["output"], "cache_read": u["cache_read"]})
    return out, cr, (usd or 0.0) / n


def _transcript(events, calls, cwd):
    """Tool records of one transcript (calls = analyzer call json, in order)."""
    inputs, res_text, res_pos, stream, res_err, ts = {}, {}, {}, [], {}, {}
    for ev in events:
        typ = ev.get("type")
        msg = ev.get("message") if isinstance(ev.get("message"), dict) else {}
        if typ == "assistant":
            for b in msg.get("content") or []:
                if not isinstance(b, dict):
                    continue
                if b.get("type") == "text":
                    stream.append(str(b.get("text", "")))
                elif b.get("type") == "tool_use":
                    inputs[b.get("id")] = b.get("input")
                    ts[b.get("id")] = ev.get("timestamp")
                    stream.append(json.dumps(b.get("input"), default=str))
        elif typ == "user" and isinstance(msg.get("content"), list):
            for b in msg["content"]:
                if isinstance(b, dict) and b.get("type") == "tool_result":
                    res_text[b.get("tool_use_id")] = un._text_of(b.get("content"))
                    res_err[b.get("tool_use_id")] = b.get("is_error") is True
                    res_pos[b.get("tool_use_id")] = len(stream)
    results = {}
    for c in calls:
        for r in c["tool_results"]:
            scaled = any(i.get("scaled_to_measured") for i in c["attribution"]
                         if i.get("tool_use_id") == r["tool_use_id"])
            results[r["tool_use_id"]] = (r, c, scaled)
    tools, compactions = [], []
    for c in calls:
        n = len(c["tool_uses"]) or 1
        out, cr, eusd = _emit_share(c, n)
        if c.get("compaction") and tools:
            compactions.append({"call": c["index"], "context_before": c["compaction"].get("context_before"),
                                "source": c["compaction"].get("source"), "before": len(tools)})
        for tu in c["tool_uses"]:
            name, inp = tu["name"], inputs.get(tu["id"])
            inp = inp if isinstance(inp, dict) else {}
            coarse, fine = kinds(name, inp, tu["target"])
            r, rc, scaled = results.get(tu["id"], (None, None, False))
            new = r["attributed_tokens"] if r else 0
            rcw, rusd = _rates(rc) if rc else (1.0, 0.0)
            text = res_text.get(tu["id"], "")
            t = {"n": len(tools), "call": c["index"], "id": tu["id"], "name": name, "coarse": coarse, "fine": fine,
                 "target": tu["target"], "err": bool(r) and res_err.get(tu["id"], False), "new": new, "res_est": r["est_tokens"] if r else 0,
                 "scaled": scaled, "out": out, "cr": cr,
                 "cw": new * rcw + out * pricing.W_OUTPUT + cr * pricing.W_CACHE_READ, "usd": new * rusd + eusd,
                 "ts": ts.get(tu["id"])}
            if t["err"]:
                t["etext"] = text[:240]
            if name == "Bash":
                cmd = " ".join(str(inp.get("command", "")).split())
                t["cmd"] = cmd[:300]
                t["segs"] = bash_segments(cmd)
                prog = t["segs"][0].split()[0]
                if prog in PAGERS:
                    t["file"] = bash_file_arg(cmd)
                if prog in BASH_READERS and t.get("file"):
                    t["via"] = "read"
                elif prog in BASH_SEARCH:
                    t["via"] = "search"
                if un.test_runner(cmd):
                    tests = un.parse_test_output(text)
                    t["test_fail"] = bool((tests and tests["failed"]) or (not tests and t["err"]))
            if name in FILE_TOOLS:
                t["file"] = tu["target"]
            if name == "Read":
                t["full"] = not (inp.get("offset") or inp.get("limit"))
                t["range"] = (inp.get("offset"), inp.get("limit"))
            if r and (t["err"] or len(text) < 2000) and DENIED_RE.search(text[:600]):
                t["denied"] = True
            pos = res_pos.get(tu["id"])
            later = None
            if r and pos is not None and (r["est_tokens"] >= BIG_RESULT_TOKENS or name == "Read"
                                          or name in ("Agent", "Task")):
                later = "\n".join(stream[pos:])
            if later is not None and r["est_tokens"] >= BIG_RESULT_TOKENS and not t["err"]:
                toks = distinctive(text)
                t["big_ref"] = (any(x in later for x in toks)) if toks else None
            if later is not None and name == "Read":
                base = os.path.basename(tu["target"] or "")
                t["mentioned_later"] = bool(base) and base in later
            if later is not None and name in ("Agent", "Task") and not t["err"]:
                toks = distinctive(text)
                t["sub_ref"] = (any(x in later for x in toks)) if toks else None
            tools.append(t)
    return tools, compactions


def _tot(calls):
    t = {"new": 0, "output": 0, "cache_read": 0, "cost_weighted": 0.0, "usd": 0.0}
    for c in calls:
        u = c["usage"]
        t["new"] += u["input"] + u["cache_write"]
        t["output"] += u["output"]
        t["cache_read"] += u["cache_read"]
        t["cost_weighted"] += c["cost_weighted"]
        t["usd"] += c["api_cost_usd"] or 0.0
    return t


def session_records(session, analysis, understood=None):
    """Compact per-transcript records for one session (main + every subagent / workflow agent)."""
    a = analysis
    fam = family(session.cwd, session.slug)
    trs = []
    tools, comps = _transcript(session.events, a["calls"], session.cwd)
    trs.append({"sid": session.id, "family": fam, "where": "main", "agent": "main", "label": "main",
                "group": None, "workflow": None, "phase": None, "tools": tools, "compactions": comps,
                "tot": _tot(a["calls"])})
    failed = {x for w in a.get("workflows") or [] for x in w.get("failed_agents") or []}
    by_agent = {sa["agent_id"]: sa for sa in session.subagents}
    for sa in a["subagents"]:
        ev = (by_agent.get(sa["agent_id"]) or {}).get("events") or []
        tools, comps = _transcript(ev, sa["call_records"], session.cwd)
        wf = sa.get("workflow_id")
        trs.append({"sid": session.id, "family": fam, "where": "workflow" if wf else "subagent",
                    "agent": sa["agent_id"], "label": "%s %s (%s)" % ("workflow agent" if wf else "subagent",
                                                                       (sa["agent_id"] or "")[:8],
                                                                       (sa.get("description") or sa.get("type") or "")[:40]),
                    "group": wf or session.id, "workflow": wf, "phase": sa.get("workflow_phase"),
                    "type": sa.get("type"), "tool_use_id": sa.get("tool_use_id"),
                    "failed": sa["agent_id"] in failed, "tools": tools, "compactions": comps,
                    "tot": _tot(sa["call_records"])})
    if understood:
        cw_by = {c["index"]: (c["cost_weighted"], c["api_cost_usd"] or 0.0) for c in a["calls"]}
        sub_by = {}
        for sa in a["subagents"]:
            t = sa["tokens"]
            sub_by.setdefault(sa["tool_use_id"], []).append((t["cost_weighted"], t["api_cost_usd"]))
        for r in understood["requests"]:
            cw = sum(cw_by.get(i, (0, 0))[0] for i in r["call_indexes"])
            usd = sum(cw_by.get(i, (0, 0))[1] for i in r["call_indexes"])
            for st in r["steps"]:
                for x in sub_by.get(st.get("tool_use_id"), []):
                    cw += x[0]
                    usd += x[1]
            r["cost_weighted"], r["usd"] = round(cw, 1), usd
    mcp = []
    o = a.get("offered_vs_used") or {}
    for e in o.get("mcp_servers") or []:
        mcp.append({"name": e["name"], "used": e["used"], "est_tokens": e["est_tokens"],
                    "reread": e["per_session_reread_est"]})
    return {"session_id": session.id, "family": fam, "totals": a["totals"], "transcripts": trs,
            "workflows": a.get("workflows") or [], "mcp": mcp}


# ---------------------------------------------------------------- stage 2: miners
class Tok:
    __slots__ = ("new", "output", "cache_read", "cw", "usd", "est")

    def __init__(self):
        self.new = self.output = self.cache_read = self.cw = self.usd = 0.0
        self.est = False

    def add(self, t):
        self.new += t["new"]
        self.output += t["out"]
        self.cache_read += t["cr"]
        self.cw += t["cw"]
        self.usd += t["usd"]
        if not t.get("scaled"):
            self.est = True
        return self

    def add_tot(self, x):
        self.new += x["new"]
        self.output += x["output"]
        self.cache_read += x["cache_read"]
        self.cw += x["cost_weighted"]
        self.usd += x["usd"]
        return self

    def json(self):
        return {"new": int(round(self.new)), "output": int(round(self.output)),
                "cache_read": int(round(self.cache_read)), "cost_weighted": int(round(self.cw))}


def _example(tr, t, label=None):
    return {"session": tr["sid"][:8], "agent": tr["label"], "where": tr["where"], "call": t["call"] if t else None,
            "label": (label or (t["fine"] if t else ""))[:120],
            "_ref": {"session_id": tr["sid"], "transcript": tr["agent"], "call_index": t["call"] if t else None,
                     "ts": t.get("ts") if t else None}}


def _savings(new=0.0, cw=0.0, usd=0.0, formula=""):
    return {"new": int(round(new)), "cost_weighted": int(round(cw)), "usd": round(usd, 4), "formula": formula,
            "est": True}


def pattern(pid, kind, title, occ, tok, examples, suggestion=None, confidence="medium", evidence=(), extra=None):
    """occ: list of transcript dicts (one per occurrence; duplicates allowed)."""
    where = Counter(tr["where"] for tr in occ)
    p = {"id": pid, "kind": kind, "title": title, "count": len(occ),
         "transcripts": len({(tr["sid"], tr["agent"]) for tr in occ}), "sessions": len({tr["sid"] for tr in occ}),
         "projects": len({tr["family"] for tr in occ}), "where": dict(where), "tokens": tok.json(),
         "api_cost_usd": round(tok.usd, 4), "tokens_est": tok.est,
         "examples": [{k: v for k, v in e.items() if k != "_ref"} for e in examples[:EXAMPLES]],
         "drill": [e["_ref"] for e in examples[:EXAMPLES] if e.get("_ref")],
         "suggestion": suggestion, "confidence": confidence, "evidence": list(evidence)}
    if extra:
        p.update(extra)
    return p


def _sugg(typ, text, sav):
    assert typ in SUGGESTION_TYPES
    return {"type": typ, "text": text, "est_savings": sav}


def _all(records):
    for rec in records:
        for tr in rec["transcripts"]:
            yield tr


def _fmt(n):
    n = n or 0
    return "%.1fM" % (n / 1e6) if n >= 1e6 else "%.1fk" % (n / 1e3) if n >= 1e3 else "%d" % n


# 1 ---------------------------------------------------------------- repeated sequences
def _contains(big, small):
    n = len(small)
    return any(big[i:i + n] == small for i in range(len(big) - n + 1))


def mine_sequences(records, ms, side=None):
    min_sess = max(1, ms.get("sessions", SEQ_MIN_SESSIONS))
    min_proj = max(1, ms.get("projects", SEQ_MIN_PROJECTS))
    stat = {}  # key -> [last_sid, sessions, projects set, cw]
    trs = list(_all(records))
    for tr in trs:
        tl = tr["tools"]
        for level in ("coarse", "fine"):
            seq = [t[level] for t in tl]
            for n in NGRAM_SIZES:
                for i in range(len(seq) - n + 1):
                    g = tuple(seq[i:i + n])
                    if len(set(g)) == 1:
                        continue
                    key = (level, g)
                    s = stat.get(key)
                    if s is None:
                        s = stat[key] = [None, 0, set(), 0.0, 0, tuple(t["coarse"] for t in tl[i:i + n])]
                    s[4] += 1
                    if s[0] != tr["sid"]:
                        s[0] = tr["sid"]
                        s[1] += 1
                    s[2].add(tr["family"])
                    s[3] += sum(t["cw"] for t in tl[i:i + n])
    import math
    score = lambda k: stat[k][3] * math.log(1 + stat[k][1])  # noqa: E731
    sup = [k for k, s in stat.items() if s[1] >= min_sess or len(s[2]) >= min_proj]
    fine = sorted((k for k in sup if k[0] == "fine"), key=score, reverse=True)[:SEQ_CANDIDATES]
    # (a) kind-only n-grams an args-level one covers go to `coarse_sequences`, not the ranked list
    covered = {stat[k][5] for k in sup if k[0] == "fine"}
    coarse_all = sorted((k for k in sup if k[0] == "coarse"), key=score, reverse=True)
    if side is not None:
        side["coarse_sequences"] = [{"sequence": list(k[1]), "count": stat[k][4], "sessions": stat[k][1],
                                     "projects": len(stat[k][2]), "cost_weighted": int(round(stat[k][3])),
                                     "covered_by_args_level": k[1] in covered} for k in coarse_all[:SEQ_CANDIDATES]]
    cands = fine + [k for k in coarse_all if k[1] not in covered][:SEQ_CANDIDATES]
    # (c) drop n-grams inside a longer kept n-gram (same level) that carries >= SEQ_CONTAINED of their count
    kept = []
    for k in sorted(cands, key=lambda k: -len(k[1])):
        if not any(b[0] == k[0] and len(b[1]) > len(k[1]) and _contains(b[1], k[1]) and
                   stat[b][4] >= SEQ_CONTAINED * stat[k][4] for b in kept):
            kept.append(k)
    # (b) rotations / overlaps: one representative per (level, sorted multiset of kinds, length bucket)
    fams = {}
    for k in sorted(kept, key=score, reverse=True):
        fk = (k[0], tuple(sorted(k[1])), "3" if len(k[1]) <= 3 else "4-5")
        fams.setdefault(fk, []).append(k)
    members = {v[0]: len(v) for v in fams.values()}
    # (d) rank by cost-weighted x log(1 + sessions)
    cands = sorted(members, key=score, reverse=True)
    want = set(cands)
    occ = defaultdict(list)  # key -> [(tr, start)]
    for tr in trs:
        tl = tr["tools"]
        for level in ("coarse", "fine"):
            seq = [t[level] for t in tl]
            for n in NGRAM_SIZES:
                for i in range(len(seq) - n + 1):
                    key = (level, tuple(seq[i:i + n]))
                    if key in want:
                        occ[key].append((tr, i))
    out, taken = [], []
    for key in cands:
        if len(out) >= SEQ_MAX:
            break
        level, g = key
        calls = {(tr["sid"], tr["agent"], tr["tools"][i + j]["n"]) for tr, i in occ[key] for j in range(len(g))}
        if any(len(calls & prev) >= SEQ_OVERLAP * len(calls) for prev in taken):
            continue
        taken.append(calls)
        tok = Tok()
        by_tr = {}
        for tr, i in occ[key]:
            by_tr.setdefault((tr["sid"], tr["agent"]), (tr, set()))[1].update(range(i, i + len(g)))
        turn = Tok()
        for tr, idx in by_tr.values():
            for k in idx:
                tok.add(tr["tools"][k])
        for tr, i in occ[key]:
            for k in range(i + 1, i + len(g)):
                t = tr["tools"][k]
                turn.add({"new": 0, "out": t["out"], "cr": t["cr"], "cw": t["out"] * pricing.W_OUTPUT +
                          t["cr"] * pricing.W_CACHE_READ, "usd": 0.0, "scaled": True})
        sessions = {tr["sid"] for tr, _ in occ[key]}
        label = " → ".join(g)
        identical = level == "fine" and all(x.startswith(("Bash(", "Skill(", "MCP(")) or "(" in x for x in g)
        sug = None
        sav = _savings(cw=turn.cw, formula="per occurrence, the output + cache-read share of all but one call "
                       "(one script/skill call replaces %d turns; result tokens kept)" % len(g))
        if level == "fine" and any(x.startswith("Bash(") for x in g):
            sug = _sugg("script" if identical else "skill", "The same %d-step command sequence ran %d times in %d "
                        "sessions: wrap it in one script (or a skill that runs it): %s" % (len(g), len(occ[key]),
                                                                                           len(sessions), label), sav)
        elif level == "fine":
            sug = _sugg("skill", "Recurring step sequence; a skill or CLAUDE.md note describing it could shortcut "
                        "it: %s" % label, sav)
        exs = [_example(tr, tr["tools"][i], label) for tr, i in occ[key][:EXAMPLES]]
        out.append(pattern("seq:%s:%s" % (level, label), "repeated_sequence",
                           "%s (%s) ran %d times in %d sessions / %d projects" % (
                               label, "with args" if level == "fine" else "tool kinds", len(occ[key]),
                               len(sessions), len({tr["family"] for tr, _ in occ[key]})),
                           [tr for tr, _ in occ[key]], tok, exs, sug,
                           "high" if level == "fine" else "low",
                           ["%d-gram at %s level; support %d sessions (min %d) / %d projects (min %d)" % (
                               len(g), level, len(sessions), min_sess, len({tr["family"] for tr, _ in occ[key]}),
                               min_proj)], {"sequence": list(g), "level": level, "family_members": members[key],
                                                "score": round(score(key), 1)}))
    # chains of identical calls (same fine kind + same file) >= CHAIN_MIN_RUN in a row
    chains = defaultdict(list)
    for tr in trs:
        tl, i = tr["tools"], 0
        while i < len(tl):
            j = i
            key = (tl[i]["fine"], tl[i].get("file"))
            while j + 1 < len(tl) and (tl[j + 1]["fine"], tl[j + 1].get("file")) == key:
                j += 1
            if j - i + 1 >= ms.get("chain", CHAIN_MIN_RUN):
                chains[tl[i]["fine"]].append((tr, i, j - i + 1))
            i = j + 1
    chains = {f: r for f, r in chains.items() if len({tr["sid"] for tr, _, _ in r}) >= 2 or len(r) >= 3}
    for fine, runs in sorted(chains.items(), key=lambda kv: -sum(tr["tools"][k]["cw"] for tr, i, n in kv[1]
                                                               for k in range(i, i + n)))[:CHAIN_MAX]:
        tok, turn = Tok(), Tok()
        for tr, i, n in runs:
            for k in range(i, i + n):
                tok.add(tr["tools"][k])
                if k > i:
                    t = tr["tools"][k]
                    turn.cw += t["out"] * pricing.W_OUTPUT + t["cr"] * pricing.W_CACHE_READ
        paging = fine.startswith("Bash(") and fine[5:-1].split()[0] in PAGERS
        if paging:
            sug = _sugg("claude_md", "Paging through a file with %s: use Read with offset/limit (or Grep for the "
                        "part you need) instead of many shell calls" % fine,
                        _savings(cw=turn.cw, formula="output + cache-read share of all but one call per run"))
        else:
            sug = _sugg("script", "%s repeated %d+ times in a row: batch it (one call with several args / a loop "
                        "in one Bash call)" % (fine, ms.get("chain", CHAIN_MIN_RUN)),
                        _savings(cw=turn.cw, formula="output + cache-read share of all but one call per run"))
        longest = max(runs, key=lambda x: x[2])
        out.append(pattern("chain:%s" % fine, "repeated_sequence",
                           "Chain of identical calls: %s ×%d in a row (%d runs, longest %d)" % (
                               fine, ms.get("chain", CHAIN_MIN_RUN), len(runs), longest[2]),
                           [tr for tr, _, _ in runs], tok,
                           [_example(tr, tr["tools"][i], "%s ×%d%s" % (fine, n, " on " + str(tr["tools"][i].get("file"))
                                                                         if tr["tools"][i].get("file") else ""))
                            for tr, i, n in sorted(runs, key=lambda x: -x[2])],
                           sug, "high" if paging else "medium",
                           ["same normalized call (and same file) >= %d times in a row" % ms.get("chain", CHAIN_MIN_RUN)],
                           {"chain": True, "calls": sum(n for _, _, n in runs)}))
    return out


# 2 ---------------------------------------------------------------- start-of-session re-discovery
_START_KINDS = ("Read", "Grep", "Glob", "Bash")


def _start_items(tr, k=START_K):
    out = {}
    for t in tr["tools"][:k]:
        if t["coarse"] not in _START_KINDS or t["fine"] in _START_TRIVIAL:
            continue
        key = t["fine"]
        if t["coarse"] == "Bash" and t.get("file"):
            key = "%s %s)" % (key[:-1], t["file"])
        out.setdefault(key, []).append(t)
    return out


_START_TRIVIAL = ("Bash(echo)", "Bash(pwd)", "Bash(true)", "Bash(sleep)", "Bash(sh)", "Bash(date)", "Bash(which)")


def mine_rediscovery(records, ms):
    min_s = ms.get("sessions", START_MIN_SESSIONS)
    out = []
    fam = defaultdict(list)
    for tr in _all(records):
        if tr["where"] == "main":
            fam[tr["family"]].append(tr)
    for f, trs in fam.items():
        items = defaultdict(list)
        for tr in trs:
            for fine, ts in _start_items(tr).items():
                items[fine].append((tr, ts))
        rec = {k: v for k, v in items.items() if len({tr["sid"] for tr, _ in v}) >= min_s}
        if not rec:
            continue
        tok = Tok()
        occ, exs = [], []
        for fine, v in sorted(rec.items(), key=lambda kv: -len(kv[1])):
            for tr, ts in v:
                occ.append(tr)
                for t in ts:
                    tok.add(t)
            exs.append(_example(v[0][0], v[0][1][0], "%s (in %d sessions)" % (fine, len(v))))
        nsess = len({tr["sid"] for tr in occ})
        lines = sorted(rec, key=lambda k: -len(rec[k]))
        out.append(pattern("start:%s" % f, "session_start_rediscovery",
                           "%s: %d files/commands re-discovered at the start of %d sessions" % (f, len(rec), nsess),
                           occ, tok, exs,
                           _sugg("claude_md", "Add a 'Project map' to CLAUDE.md with what sessions look up first: "
                                 + "; ".join(lines[:8]),
                                 _savings(tok.new, tok.cw, tok.usd, "tokens of these first-%d-call lookups summed over "
                                          "the sessions that repeated them (a short map costs far less)" % START_K)),
                           "medium", ["first %d tool calls of main sessions in family %s; item kept when in >= %d "
                                      "sessions" % (START_K, f, min_s)], {"items": lines[:20], "family": f}))
    # fan-out re-discovery among sibling subagents / workflow agents
    groups = defaultdict(list)
    for tr in _all(records):
        if tr["where"] != "main" and tr["group"]:
            groups[(tr["sid"], tr["group"])].append(tr)
    for (sid, g), trs in groups.items():
        if len(trs) < FANOUT_MIN_SIBLINGS:
            continue
        items = defaultdict(list)
        for tr in trs:
            for fine, ts in _start_items(tr).items():
                items[fine].append((tr, ts))
        rec = {k: v for k, v in items.items() if len({tr["agent"] for tr, _ in v}) >= FANOUT_MIN_SIBLINGS}
        if not rec:
            continue
        tok, occ, exs = Tok(), [], []
        for fine, v in sorted(rec.items(), key=lambda kv: -len(kv[1])):
            for tr, ts in v:
                occ.append(tr)
                for t in ts:
                    tok.add(t)
            exs.append(_example(v[0][0], v[0][1][0], "%s (by %d siblings)" % (fine, len(v))))
        wf = trs[0]["workflow"]
        out.append(pattern("fanout:%s:%s" % (sid[:8], g), "session_start_rediscovery",
                           "Fan-out re-discovery: %d of %d %s re-read the same %d files/commands at start" % (
                               len({tr["agent"] for tr in occ}), len(trs),
                               "agents of workflow %s" % wf if wf else "subagents of session %s" % sid[:8], len(rec)),
                           occ, tok, exs,
                           _sugg("workflow" if wf else "claude_md",
                                 "Pass a shared brief/context file to the %s (contents of: %s)" % (
                                     "workflow agents" if wf else "subagents", "; ".join(list(rec)[:6])),
                                 _savings(tok.new, tok.cw, tok.usd, "tokens of the repeated start lookups in every "
                                          "sibling (upper bound: each agent still reads the brief)")),
                           "medium", ["sibling agents (same %s), first %d calls, item in >= %d siblings" % (
                               "workflow" if wf else "parent session", START_K, FANOUT_MIN_SIBLINGS)],
                           {"fanout": True, "items": list(rec)[:20]}))
    return out


# 3 ---------------------------------------------------------------- retry loops
def mine_retries(records, ms):
    out = []
    trs = list(_all(records))
    # (a) edit -> test fail -> edit same file
    occ, tok, exs, files = [], Tok(), [], Counter()
    for tr in trs:
        pending, edited, cycles, involved = set(), set(), Counter(), []
        for t in tr["tools"]:
            if t["name"] in EDIT_TOOLS:
                f = t.get("file")
                if f in pending:
                    cycles[f] += 1
                    involved.append(t)
                    pending.discard(f)
                edited.add(f)
            elif "test_fail" in t:
                if t["test_fail"]:
                    pending |= edited
                    involved.append(t)
                else:
                    pending.clear()
                edited = set()
        hit = {f: n for f, n in cycles.items() if n >= ms.get("cycles", EDIT_FAIL_MIN_CYCLES)}
        if hit:
            occ.append(tr)
            for t in involved:
                tok.add(t)
            for f, n in hit.items():
                files[f] += n
                exs.append(_example(tr, involved[0], "%d edit→test fail→edit cycles on %s" % (n, f)))
    if occ:
        out.append(pattern("retry:edit_test", "retry_loops",
                           "Edit → test fail → edit loops: %d cycles on %d files in %d transcripts" % (
                               sum(files.values()), len(files), len(occ)),
                           occ, tok, exs,
                           _sugg("claude_md", "Note the test command and the usual failure causes for the files that "
                                 "loop (%s) in CLAUDE.md; or a hook that runs the focused test after edits" %
                                 ", ".join(f for f, _ in files.most_common(4)), None),
                           "medium", ["per file, an edit after a failed test run that followed an edit of it; "
                                      ">= %d cycles" % EDIT_FAIL_MIN_CYCLES], {"files": dict(files.most_common(20))}))
    # (b) same command retried with small variations, failing
    occ, tok, exs, cmds = [], Tok(), [], Counter()
    for tr in trs:
        tl, i = [t for t in tr["tools"] if t["name"] == "Bash"], 0
        while i < len(tl):
            j = i
            while j + 1 < len(tl) and tl[j + 1]["fine"] == tl[i]["fine"] and tl[j + 1]["n"] == tl[j]["n"] + 1 and \
                    tl[j + 1]["cmd"] != tl[j]["cmd"] and \
                    difflib.SequenceMatcher(None, tl[j]["cmd"][:200], tl[j + 1]["cmd"][:200]).ratio() >= RETRY_SIMILARITY:
                j += 1
            run = tl[i:j + 1]
            if len(run) >= ms.get("retries", RETRY_MIN_RUN) and sum(1 for t in run[:-1] if t["err"] or t.get("test_fail")) >= len(run) - 2:
                occ.append(tr)
                cmds[run[0]["fine"]] += 1
                for t in run:
                    tok.add(t)
                exs.append(_example(tr, run[0], "%s ×%d variants, %d failed" % (
                    run[0]["fine"], len(run), sum(1 for t in run if t["err"] or t.get("test_fail")))))
            i = j + 1
    if occ:
        out.append(pattern("retry:variants", "retry_loops",
                           "Command retried with small variations while failing: %d runs (%s)" % (
                               len(occ), ", ".join("%s ×%d" % kv for kv in cmds.most_common(3))),
                           occ, tok, exs,
                           _sugg("claude_md", "Document the working invocation of %s in CLAUDE.md (or a script)" %
                                 cmds.most_common(1)[0][0],
                                 _savings(tok.new, tok.cw, tok.usd, "all failed attempts (upper bound)")),
                           "medium", ["consecutive Bash calls, same program, difflib ratio >= %.1f, all but the last "
                                      "may succeed" % RETRY_SIMILARITY], {"commands": dict(cmds)}))
    # (c) error hotspots
    stats = defaultdict(lambda: {"calls": 0, "errors": 0, "tok": Tok(), "occ": [], "ex": []})
    for tr in trs:
        tl = tr["tools"]
        for k, t in enumerate(tl):
            key = t["fine"] if t["name"] in ("Bash",) or t["coarse"] in ("MCP", "Skill", "Agent") or \
                t["name"] == "StructuredOutput" else t["coarse"]
            s = stats[key]
            s["calls"] += 1
            if t["err"]:
                s["errors"] += 1
                s["tok"].add(t)
                s["occ"].append(tr)
                if k + 1 < len(tl) and tl[k + 1]["coarse"] == t["coarse"]:
                    s["tok"].add(tl[k + 1])
                if len(s["ex"]) < EXAMPLES:
                    s["ex"].append(_example(tr, t))
    for key, s in stats.items():
        rate = s["errors"] / s["calls"] if s["calls"] else 0
        if s["errors"] >= ms.get("errors", ERR_MIN_COUNT) or (rate >= ERR_MIN_RATE and s["calls"] >= ERR_MIN_CALLS and s["errors"]):
            out.append(pattern("errors:%s" % key, "retry_loops",
                               "%s errors: %d of %d calls (%d%%)" % (key, s["errors"], s["calls"], round(100 * rate)),
                               s["occ"], s["tok"], s["ex"],
                               _sugg("claude_md", "Look at the error examples for %s; document the right usage (paths, "
                                     "flags, limits) or add a permission/hook so it stops failing" % key,
                                     _savings(s["tok"].new, s["tok"].cw, s["tok"].usd,
                                              "erroring calls + the immediate retry (upper bound)")),
                               "high", ["flag: >= %d errors or >= %d%% error rate (>= %d calls)" % (
                                   ERR_MIN_COUNT, 100 * ERR_MIN_RATE, ERR_MIN_CALLS)],
                               {"calls": s["calls"], "errors": s["errors"], "error_rate": round(rate, 3)}))
    # (d) permission denied, then another attempt
    occ, tok, exs, kinds_ = [], Tok(), [], Counter()
    for tr in trs:
        tl = tr["tools"]
        for k, t in enumerate(tl):
            if t.get("denied") and any(x["coarse"] == t["coarse"] for x in tl[k + 1:k + 1 + DENIED_LOOKAHEAD]):
                occ.append(tr)
                kinds_[t["fine"]] += 1
                tok.add(t)
                nxt = next(x for x in tl[k + 1:k + 1 + DENIED_LOOKAHEAD] if x["coarse"] == t["coarse"])
                tok.add(nxt)
                exs.append(_example(tr, t, "denied: %s, retried as %s" % (t["fine"], nxt["fine"])))
    if occ:
        out.append(pattern("retry:denied", "retry_loops",
                           "Permission denied then retried: %d times (%s)" % (
                               len(occ), ", ".join("%s ×%d" % kv for kv in kinds_.most_common(3))),
                           occ, tok, exs,
                           _sugg("permission", "Allow (or explicitly deny in CLAUDE.md) %s in .claude/settings.json "
                                 "permissions so Claude stops re-trying" % ", ".join(k for k, _ in kinds_.most_common(3)),
                                 _savings(tok.new, tok.cw, tok.usd, "denied call + its retry")),
                           "medium", ["tool_result matches %s; same tool kind within %d calls" % (
                               DENIED_RE.pattern[:60] + "...", DENIED_LOOKAHEAD)], {"kinds": dict(kinds_)}))
    return out


# 3b --------------------------------------------------------------- recurring error classes
_ERR_TAG_RE = re.compile(r"</?[a-z_]+>")
_UUID_RE = re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.I)
_PATH_RE = re.compile(r"(?:~|\.{1,2})?/(?:[\w.@+-]+/)*[\w.@+-]+|\b[\w.-]+/[\w./-]+")
_HEX_RE = re.compile(r"\b(?:0x)?[0-9a-f]{6,}\b", re.I)


def error_class(text):
    """First ~80 chars of an error result, with uuids/paths/hex/digits normalized."""
    s = _ERR_TAG_RE.sub(" ", str(text or ""))[:400]
    s = _UUID_RE.sub("<uuid>", s)
    s = _PATH_RE.sub("<path>", s)
    s = _HEX_RE.sub("<hex>", s)
    s = re.sub(r"\d+", "N", s)
    return " ".join(s.split())[:REC_ERR_CHARS]


def tool_family(t):
    mp = un.mcp_parts(t["name"])
    if mp:
        return "mcp:%s" % mp[0]
    if t["name"] == "Bash":
        return "bash:%s" % (t.get("segs") or ["sh"])[0].split()[0]
    return t["name"]


def _err_suggestion(fam, cls, tok):
    low = cls.lower()
    sav = _savings(tok.new, tok.cw, tok.usd, "all failing calls of this class + their retries")
    srv = fam.split(":", 1)[1] if fam.startswith("mcp:") else fam
    if re.search(r"auth|api[ _-]?key|unauthori[sz]ed|\b40[13]\b|forbidden", low):
        return _sugg("mcp_config", "Fix the credentials or disable server %s (it keeps failing auth)" % srv, sav)
    if re.search(r"rate.?limit|too many requests|\b429\b|overloaded", low):
        return _sugg("script", "Rate limited (%s): batch the calls or retry with backoff in a script" % srv, sav)
    if "no matches found" in low:
        return _sugg("claude_md", "Quote globs in zsh (e.g. grep --include='*.ts')", sav)
    if "structuredoutput" in fam.lower() or "does not match" in low and "schema" in low:
        return _sugg("workflow", "Tighten the output schema / instructions of the workflow agents", sav)
    return _sugg("claude_md", "Document the right usage of %s so it stops failing with: %s" % (srv, cls[:60]), sav)


def mine_recurring_errors(records, ms):
    groups = {}
    for tr in _all(records):
        tl = tr["tools"]
        for k, t in enumerate(tl):
            if not t["err"]:
                continue
            fam = tool_family(t)
            key = (fam, error_class(t.get("etext")))
            g = groups.get(key)
            if g is None:
                g = groups[key] = {"occ": [], "tok": Tok(), "tools": Counter(), "days": set(), "retries": 0, "ex": []}
            g["occ"].append(tr)
            g["tok"].add(t)
            g["tools"][t["fine"]] += 1
            if t.get("ts"):
                g["days"].add(str(t["ts"])[:10])
            nxt = next((x for x in tl[k + 1:k + 3] if x["name"] == t["name"]), None)
            if nxt is not None:
                g["retries"] += 1
                g["tok"].add(nxt)
            if len(g["ex"]) < EXAMPLES:
                g["ex"].append(_example(tr, t, "%s: %s" % (t["fine"], key[1])))
    out = []
    for (fam, cls), g in groups.items():
        n, sess = len(g["occ"]), {tr["sid"] for tr in g["occ"]}
        if n < ms.get("rec_errors", REC_ERR_MIN_COUNT) and len(sess) < ms.get("sessions", REC_ERR_MIN_SESSIONS):
            continue
        days = sorted(g["days"])
        out.append(pattern("recerr:%s:%s" % (fam, cls), "recurring_error",
                           "%s keeps failing with \"%s\": %d times in %d sessions over %d days" % (
                               fam, cls, n, len(sess), len(days)),
                           g["occ"], g["tok"], g["ex"], _err_suggestion(fam, cls, g["tok"]), "high",
                           ["tool_result is_error=true; class = first %d chars, digits/paths/uuids/hex normalized; "
                            ">= %d occurrences or >= %d sessions" % (REC_ERR_CHARS, REC_ERR_MIN_COUNT,
                                                                       REC_ERR_MIN_SESSIONS)],
                           {"tool_family": fam, "error_class": cls, "first_date": days[0] if days else None,
                            "last_date": days[-1] if days else None, "distinct_days": len(days),
                            "tools": dict(g["tools"].most_common(10)), "retries": g["retries"]}))
    return out


# 3c --------------------------------------------------------------- tool choice (revealed behavior)
def mine_tool_choice(records, ms):
    cats = {"read": ("file reads", ("Read",), "cat/sed -n/head/tail"),
            "search": ("searches", ("Grep", "Glob"), "grep/rg/find")}
    st = {c: {"bash": Counter(), "tool": Counter(), "bt": Counter(), "tt": Counter(), "occ": [], "tok": Tok(),
              "ex": []} for c in cats}
    for tr in _all(records):
        for t in tr["tools"]:
            for c, (_, tools, _) in cats.items():
                if t.get("via") == c:
                    s = st[c]
                    s["bash"][tr["where"]] += 1
                    s["bt"][tr["where"]] += t["new"]
                    s["occ"].append(tr)
                    s["tok"].add(t)
                    if len(s["ex"]) < EXAMPLES:
                        s["ex"].append(_example(tr, t, t.get("cmd", t["fine"])[:120]))
                elif t["name"] in tools:
                    st[c]["tool"][tr["where"]] += 1
                    st[c]["tt"][tr["where"]] += t["new"]
    out = []
    for c, (label, tools, progs) in cats.items():
        s = st[c]
        nb, nt = sum(s["bash"].values()), sum(s["tool"].values())
        if nb + nt < TOOL_CHOICE_MIN or not nb:
            continue
        share = nb / (nb + nt)
        split = {w: {"bash": s["bash"][w], "tool": s["tool"][w], "bash_new_tokens": s["bt"][w],
                     "tool_new_tokens": s["tt"][w],
                     "bash_share": round(s["bash"][w] / ((s["bash"][w] + s["tool"][w]) or 1), 3)}
                 for w in ("main", "subagent", "workflow")}
        bt, tt = sum(s["bt"].values()), sum(s["tt"].values())
        avg_b, avg_t = (bt / nb if nb else 0), (tt / nt if nt else 0)
        notes = ["Bash primary segment (after cd/env) is %s%s" % (progs, " on a path" if c == "read" else ""),
                 "avg new tokens per call: Bash %d vs %s %s" % (avg_b, "/".join(tools), ("%d" % avg_t) if nt else "n/a")]
        sug = None
        if nt == 0:
            notes.append("%s was never called in this selection: likely not offered by this Claude Code version, "
                         "so Bash is the only option (revealed behaviour, no fix suggested)" % "/".join(tools))
        elif share > TOOL_CHOICE_SHARE and avg_b > avg_t:
            sug = _sugg("claude_md", "Prefer Read with offset/limit over cat/sed in Bash (Bash reads here average %d "
                        "new tokens vs %d for Read)" % (avg_b, avg_t), _savings(formula="not estimated"))
        else:
            notes.append("Bash reads are smaller on average than the tool's (targeted sed -n ranges), so no fix is "
                         "suggested: this is revealed behaviour, not waste")
        out.append(pattern("toolchoice:%s" % c, "tool_choice",
                           "%d%% of %s go through Bash (%s) instead of %s: %d vs %d calls" % (
                               round(100 * share), label, progs, "/".join(tools), nb, nt),
                           s["occ"], s["tok"], s["ex"], sug, "medium", notes,
                           {"bash_share": round(share, 3), "bash_calls": nb, "tool_calls": nt, "split": split,
                            "avg_new_bash": round(avg_b), "avg_new_tool": round(avg_t) if nt else None}))
    return out


# 4 ---------------------------------------------------------------- wasted context
def mine_wasted(records, ms):
    out = []
    trs = list(_all(records))
    big, full, rere = ([], Tok(), []), ([], Tok(), []), ([], Tok(), [])
    for tr in trs:
        tl = tr["tools"]
        edited = {t.get("file") for t in tl if t["name"] in EDIT_TOOLS}
        last_read = {}
        for t in tl:
            if t.get("big_ref") is False:
                big[0].append(tr)
                big[1].add(t)
                big[2].append(_example(tr, t, "%s (~%s tokens)" % (t["fine"], _fmt(t["res_est"]))))
            if t["name"] == "Read" and t.get("full") and not t["err"] and t.get("file") not in edited \
                    and t.get("mentioned_later") is False:
                full[0].append(tr)
                full[1].add(t)
                full[2].append(_example(tr, t))
            if t["name"] in EDIT_TOOLS:
                last_read.pop(t.get("file"), None)
            if t["name"] == "Read" and not t["err"]:
                key = (t.get("file"), t.get("range"))
                if key in last_read:
                    rere[0].append(tr)
                    rere[1].add(t)
                    rere[2].append(_example(tr, t, "re-read %s" % t.get("file")))
                last_read[key] = t
    for pid, title, (occ, tok, exs), sug, conf, ev in (
            ("wasted:big_unreferenced", "Large tool results (>= %s tokens) never referenced later" % _fmt(BIG_RESULT_TOKENS),
             big, ("claude_md", "Narrow these outputs (head/tail/grep, Read offset/limit) or run them in a subagent"),
             "low", "heuristic: no identifier/path/number (>= 6 chars) from the kept head/tail of the result appears in "
                    "any later assistant text or tool input of the transcript"),
            ("wasted:full_reads", "Full-file Reads of files never edited or mentioned later", full,
             ("claude_md", "Use Grep or Read with offset/limit for files you only skim"), "low",
             "Read without offset/limit, no Edit/Write of the file, its basename never appears later"),
            ("wasted:rereads", "Same file read again (same range) with no edit in between", rere,
             ("claude_md", "Avoid re-reading unchanged files; the earlier result is still in context"), "high",
             "same file + same offset/limit, no Edit/Write of it in between")):
        if occ:
            out.append(pattern(pid, "wasted_context", "%s: %d results in %d transcripts" % (
                title, len(occ), len({(x["sid"], x["agent"]) for x in occ})), occ, tok,
                sorted(exs, key=lambda e: e["label"])[:EXAMPLES],
                _sugg(sug[0], sug[1], _savings(tok.new, tok.cw, tok.usd, "tokens of these results (upper bound)")),
                conf, [ev]))
    # (d) subagent results discarded / failed workflow agents
    occ, tok, exs = [], Tok(), []
    parent_ref = {}
    for tr in trs:
        if tr["where"] == "main" or tr["where"] == "subagent":
            for t in tr["tools"]:
                if t["name"] in ("Agent", "Task"):
                    parent_ref[(tr["sid"], t["id"])] = t.get("sub_ref")
    for tr in trs:
        why = None
        if tr["where"] == "workflow" and tr.get("failed"):
            why = "workflow agent failed (journal)"
        elif tr["where"] == "subagent" and parent_ref.get((tr["sid"], tr.get("tool_use_id"))) is False:
            why = "result never referenced by the parent"
        if why:
            occ.append(tr)
            tok.add_tot(tr["tot"])
            tok.est = True
            exs.append({"session": tr["sid"][:8], "agent": tr["label"], "where": tr["where"], "call": None,
                        "label": why})
    if occ:
        out.append(pattern("wasted:discarded_agents", "wasted_context",
                           "Subagent / workflow agent work discarded: %d agents" % len(occ), occ, tok, exs,
                           _sugg("workflow", "Check why these agents fail or why their results go unused; give them "
                                 "a narrower task or drop them", _savings(tok.new, tok.cw, tok.usd,
                                                                         "full measured cost of these agents")),
                           "low", ["failed per workflow journal, or (heuristic) no distinctive token of the "
                                   "subagent's result appears later in the parent"]))
    return out


# 5 ---------------------------------------------------------------- divergent paths
def _cw_req(r):
    if "cost_weighted" in r:
        return r["cost_weighted"]
    t = r["tokens"]
    return pricing.cost_weighted_of(t.get("fresh_input", 0), t.get("cache_write", 0), t.get("cache_read", 0),
                                    t.get("output", 0))


def mine_divergent(u, ms):
    out = []
    if not u:
        return out
    reqs = {r["id"]: r for s in u["sessions"] for r in s["requests"]}
    for g in u["goals"]:
        rs = [reqs[i] for i in g["request_ids"] if i in reqs]
        if len(rs) < ms.get("runs", DIVERGENT_MIN_RUNS):
            continue
        med = statistics.median(_cw_req(r) for r in rs)
        paths = defaultdict(list)
        for r in rs:
            paths[" → ".join(r["flow"][:PATH_KINDS]) or "(no tools)"].append(r)
        rows = []
        for p, members in paths.items():
            ok = [r for r in members if r["outcome"] == "success"]
            rows.append({"path": p, "runs": len(members), "finish_rate": round(len(ok) / len(members), 2),
                         "median_cost_weighted": round(statistics.median(_cw_req(r) for r in members)),
                         "median_success_cost_weighted": round(statistics.median(_cw_req(r) for r in ok)) if ok else None})
        good = [x for x in rows if x["median_success_cost_weighted"] is not None]
        if not good or not med:
            continue
        best = min(good, key=lambda x: x["median_success_cost_weighted"])
        if best["median_success_cost_weighted"] > (1 - DIVERGENT_SAVING) * med:
            continue
        tok = Tok()
        for r in rs:
            t = r["tokens"]
            tok.new += t.get("fresh_input", 0) + t.get("cache_write", 0)
            tok.output += t.get("output", 0)
            tok.cache_read += t.get("cache_read", 0)
            tok.cw += _cw_req(r)
            tok.usd += r.get("usd", 0.0)
        occ = [{"sid": r["session_id"], "agent": "main", "family": r["project"], "where": "main"} for r in rs]
        dearer = [r for r in rs if _cw_req(r) > best["median_success_cost_weighted"]]
        sav = sum(_cw_req(r) - best["median_success_cost_weighted"] for r in dearer)
        exs = [{"session": r["session_id"][:8], "agent": "main", "where": "main", "call": r["call_indexes"][0]
                if r["call_indexes"] else None, "label": "%s: %s (%s)" % (r["id"], " → ".join(r["flow"][:PATH_KINDS]),
                                                                        r["outcome"])} for r in rs]
        out.append(pattern("paths:%s" % g["id"], "divergent_paths",
                           "Goal '%s': cheapest successful path is %d%% cheaper than the median run (%d runs, %d paths)"
                           % (g["goal"], round(100 * (1 - best["median_success_cost_weighted"] / med)), len(rs),
                              len(rows)), occ, tok, exs,
                           _sugg("claude_md", "For '%s', steer toward the path %s (e.g. a CLAUDE.md note or skill)" % (
                               g["goal"], best["path"]),
                               _savings(cw=sav, formula="sum over runs dearer than the best path's median success: "
                                        "run cost-weighted - that median")),
                           "low" if best["runs"] < 2 else "medium",
                           ["paths = first %d tool kinds; flagged when cheapest successful path median <= %d%% of the "
                            "goal median" % (PATH_KINDS, 100 * (1 - DIVERGENT_SAVING))],
                           {"goal_id": g["id"], "paths": sorted(rows, key=lambda x: -x["runs"])}))
    return out


# 6 ---------------------------------------------------------------- user corrections
def is_correction(text):
    t = (text or "").strip()
    if CORRECTION_RE.match(t):
        return True
    return len(t) <= CORRECTION_MAX_CHARS and bool(CORRECTION_ANY_RE.search(t))


def mine_corrections(u, ms):
    if not u:
        return []
    occ, tok, exs = [], Tok(), []
    by_goal, by_tool = Counter(), Counter()
    for s in u["sessions"]:
        rs = sorted(s["requests"], key=lambda r: r["n"])
        for prev, r in zip(rs, rs[1:]):
            if r.get("source", "human") != "human" or not is_correction(r["prompt"]):
                continue
            goal = (prev.get("goal") or {}).get("name") or "?"
            last = [st["kind"] for st in prev["steps"] if not un.is_hook(st)][-3:]
            by_goal[goal] += 1
            for k in set(last):
                by_tool[k] += 1
            t = prev["tokens"]
            tok.new += t.get("fresh_input", 0) + t.get("cache_write", 0)
            tok.output += t.get("output", 0)
            tok.cache_read += t.get("cache_read", 0)
            tok.cw += _cw_req(prev)
            tok.usd += prev.get("usd", 0.0)
            occ.append({"sid": r["session_id"], "agent": "main", "family": r["project"], "where": "main"})
            exs.append({"session": r["session_id"][:8], "agent": "main", "where": "main",
                        "call": r["call_indexes"][0] if r["call_indexes"] else None,
                        "label": "after %s (goal %s; last tools %s)" % (prev["id"], goal, ", ".join(last) or "-")})
    if not occ:
        return []
    return [pattern("corrections", "user_corrections",
                    "User corrections: %d follow-ups correcting the previous request (top goal: %s)" % (
                        len(occ), by_goal.most_common(1)[0][0]), occ, tok, exs,
                    _sugg("claude_md", "Add the missing expectations for %s to CLAUDE.md (what was corrected)" %
                          ", ".join(g for g, _ in by_goal.most_common(3)),
                          _savings(tok.new, tok.cw, tok.usd, "cost of the corrected requests (wrong turns)")),
                    "medium", list(CORRECTION_RULES),
                    {"by_goal": dict(by_goal.most_common(20)), "by_last_tool": dict(by_tool.most_common(20))})]


# 7 ---------------------------------------------------------------- compactions
def mine_compactions(records, ms):
    occ, tok, exs, causes = [], Tok(), [], Counter()
    for tr in _all(records):
        for c in tr["compactions"]:
            before = [t for t in tr["tools"] if t["call"] < c["call"]][-COMPACT_BEFORE:]
            occ.append(tr)
            per = Counter()
            for t in before:
                tok.add(t)
                per[t["fine"]] += t["new"]
                causes[t["coarse"] if t["coarse"] not in ("Bash", "Read") else t["fine"].split("(")[0] + ":" +
                       (t["fine"][5:-1] if t["coarse"] == "Bash" else "file")] += t["new"]
            top = ", ".join("%s %s" % (k, _fmt(v)) for k, v in per.most_common(2))
            exs.append({"session": tr["sid"][:8], "agent": tr["label"], "where": tr["where"], "call": c["call"],
                        "label": "compaction at %s context; before it: %s" % (_fmt(c.get("context_before") or 0),
                                                                            top or "-")})
    if not occ:
        return []
    top = causes.most_common(3)
    return [pattern("compactions", "compactions", "%d compactions; context filled mostly by %s" % (
        len(occ), ", ".join("%s (%s)" % (k, _fmt(v)) for k, v in top)), occ, tok, exs,
        _sugg("claude_md", "Before long tasks, cap the biggest context fillers (%s): narrower reads/outputs, or "
              "delegate them to subagents" % ", ".join(k for k, _ in top),
              _savings(tok.new, tok.cw, tok.usd, "tokens of the %d calls before each compaction (upper bound)" %
                       COMPACT_BEFORE)), "medium",
        ["compaction = compact_boundary / isCompactSummary / context drop > 50% (analyzer)"],
        {"causes": dict(causes.most_common(10))})]


# 8 ---------------------------------------------------------------- workflow fan-out
def mine_workflows(records, ms):
    out, wfs = [], []
    agents = defaultdict(list)
    journals = {}
    for rec in records:
        for w in rec["workflows"]:
            journals[(rec["session_id"], w["id"])] = w
        for tr in rec["transcripts"]:
            if tr["where"] == "workflow":
                agents[(tr["sid"], tr["workflow"])].append(tr)
    all_costs = [tr["tot"]["cost_weighted"] for v in agents.values() for tr in v]
    med_all = statistics.median(all_costs) if all_costs else 0
    phases = defaultdict(lambda: {"agents": [], "failed": 0, "so_err": 0})
    for key, trs in agents.items():
        j = journals.get(key, {})
        tot = Tok()
        for tr in trs:
            tot.add_tot(tr["tot"])
        per_phase = defaultdict(Tok)
        for tr in trs:
            p = tr["phase"] or "(no phase)"
            per_phase[p].add_tot(tr["tot"])
            ph = phases[p]
            ph["agents"].append(tr)
            ph["failed"] += bool(tr.get("failed"))
            ph["so_err"] += sum(1 for t in tr["tools"] if t["name"] == "StructuredOutput" and t["err"])
        failed = Tok()
        for tr in trs:
            if tr.get("failed"):
                failed.add_tot(tr["tot"])
        wfs.append({"session": key[0][:8], "workflow": key[1], "agents": len(trs),
                    "phases": {p: {"agents": sum(1 for tr in trs if (tr["phase"] or "(no phase)") == p),
                                   "cost_weighted": int(t.cw), "usd": round(t.usd, 4)} for p, t in per_phase.items()},
                    "started": j.get("started", 0), "results": j.get("results", 0), "failed": j.get("failed", 0),
                    "tokens": tot.json(), "api_cost_usd": round(tot.usd, 4),
                    "failed_cost_weighted": int(failed.cw), "failed_usd": round(failed.usd, 4),
                    "median_cost_weighted_per_agent": int(statistics.median(tr["tot"]["cost_weighted"] for tr in trs)),
                    "structured_output_errors": sum(1 for tr in trs for t in tr["tools"]
                                                    if t["name"] == "StructuredOutput" and t["err"])})
    for p, ph in phases.items():
        trs = ph["agents"]
        tok = Tok()
        for tr in trs:
            tok.add_tot(tr["tot"])
        per_agent = tok.cw / len(trs)
        rate = ph["failed"] / len(trs)
        flags = []
        if med_all and per_agent > PHASE_COST_X * med_all and len(trs) >= 2:
            flags.append("cost per agent %s > %.0fx the median workflow agent (%s)" % (_fmt(per_agent), PHASE_COST_X,
                                                                                     _fmt(med_all)))
        if rate >= PHASE_FAIL_RATE and ph["failed"]:
            flags.append("%d%% of agents failed" % round(100 * rate))
        if not flags:
            continue
        exs = [{"session": tr["sid"][:8], "agent": tr["label"], "where": "workflow", "call": None,
                "label": "%s %s%s" % (tr["workflow"], _fmt(tr["tot"]["cost_weighted"]), " failed" if tr.get("failed")
                                      else "")} for tr in sorted(trs, key=lambda x: -x["tot"]["cost_weighted"])]
        out.append(pattern("workflow:%s" % p, "workflow_fanout", "Workflow phase '%s': %d agents, %s" % (
            p, len(trs), "; ".join(flags)), trs, tok, exs,
            _sugg("workflow", "Narrow the '%s' phase prompt/inputs (shared brief, smaller scope per agent) and check "
                  "its failures" % p, _savings(tok.new * 0, (per_agent - med_all) * len(trs) if per_agent > med_all
                                               else 0, 0, "(phase cost per agent - median agent) x agents")),
            "medium", ["phase from the agent's .meta.json workflowPhase; failed from journal.jsonl"],
            {"failed": ph["failed"], "structured_output_errors": ph["so_err"]}))
    return out, wfs


# 9 ---------------------------------------------------------------- tool overhead
def mine_overhead(records, ms):
    out = []
    occ, tok, exs = [], Tok(), []
    for tr in _all(records):
        for t in tr["tools"]:
            if t["name"] == "ToolSearch":
                occ.append(tr)
                tok.add(t)
                if len(exs) < EXAMPLES:
                    exs.append(_example(tr, t))
    if len(occ) >= TOOLSEARCH_MIN:
        out.append(pattern("overhead:toolsearch", "tool_overhead", "ToolSearch (deferred tool discovery): %d calls" %
                           len(occ), occ, tok, exs,
                           _sugg("mcp_config", "Tools looked up every session can be made non-deferred (or the unused "
                                 "MCP servers removed) so no discovery turn is needed",
                                 _savings(tok.new, tok.cw, tok.usd, "tokens of the ToolSearch calls")), "medium",
                           ["tool name ToolSearch"]))
    srv = defaultdict(lambda: {"offered": 0, "used": 0, "est": 0, "reread": 0, "sids": []})
    for rec in records:
        for m in rec["mcp"]:
            s = srv[m["name"]]
            s["offered"] += 1
            s["used"] += m["used"]
            if not m["used"]:
                s["est"] += m["est_tokens"]
                s["reread"] += m["reread"]
                s["sids"].append(rec["session_id"])
    for name, s in srv.items():
        unused = s["offered"] - s["used"]
        if unused < ms.get("sessions", MCP_UNUSED_MIN_SESSIONS):
            continue
        tok = Tok()
        tok.new = s["est"]
        tok.cache_read = s["reread"]
        tok.cw = s["est"] * pricing.W_CACHE_WRITE + s["reread"] * pricing.W_CACHE_READ
        tok.est = True
        occ = [{"sid": sid, "agent": "main", "family": "", "where": "main"} for sid in s["sids"]]
        out.append(pattern("overhead:mcp:%s" % name, "tool_overhead",
                           "MCP server %s offered in %d sessions, never called in %d" % (name, s["offered"], unused),
                           occ, tok, [{"session": sid[:8], "agent": "main", "where": "main", "call": 0,
                                       "label": "offered, not called"} for sid in s["sids"]],
                           _sugg("mcp_config", "Disable %s where it is not used (project .mcp.json / settings)" % name,
                                 _savings(tok.new, tok.cw, 0, "listing est tokens written once + re-read per call")),
                           "medium", ["offered = deferred tool names / MCP instructions in the transcript; listing "
                                      "tokens are chars/4 estimates"], {"offered": s["offered"], "used": s["used"]}))
    return out


# ---------------------------------------------------------------- driver
MINERS = ("repeated_sequence", "session_start_rediscovery", "retry_loops", "recurring_error", "tool_choice",
          "wasted_context", "divergent_paths", "user_corrections", "compactions", "workflow_fanout", "tool_overhead")


def mine(records, understood=None, min_support=None, top=None, timings=None):
    """All miners -> {patterns, workflows, counts, timings}. min_support: override of the session/sibling minimums."""
    import time
    ms = {}
    if min_support:
        ms = {"sessions": min_support, "projects": max(2, min_support)}
    timings = {} if timings is None else timings
    pats, wfs = [], []
    side = {}
    steps = [("repeated_sequence", lambda: mine_sequences(records, ms, side)),
             ("session_start_rediscovery", lambda: mine_rediscovery(records, ms)),
             ("retry_loops", lambda: mine_retries(records, ms)),
             ("recurring_error", lambda: mine_recurring_errors(records, ms)),
             ("tool_choice", lambda: mine_tool_choice(records, ms)),
             ("wasted_context", lambda: mine_wasted(records, ms)),
             ("divergent_paths", lambda: mine_divergent(understood, ms)),
             ("user_corrections", lambda: mine_corrections(understood, ms)),
             ("compactions", lambda: mine_compactions(records, ms)),
             ("workflow_fanout", None),
             ("tool_overhead", lambda: mine_overhead(records, ms))]
    counts = {}
    for name, fn in steps:
        t0 = time.time()
        if name == "workflow_fanout":
            got, wfs = mine_workflows(records, ms)
        else:
            got = fn()
        timings[name] = round(time.time() - t0, 3)
        counts[name] = len(got)
        pats += got
    pats.sort(key=lambda p: -p["tokens"]["cost_weighted"])
    if top:
        pats = pats[:top]
    return {"patterns": pats, "workflows": wfs, "counts": counts, "timings": timings,
            "coarse_sequences": side.get("coarse_sequences", []),
            "token_notes": ["tokens per tool call: new = result tokens attributed by the analyzer in the next call "
                            "(chars/4 estimate unless scaled to measured), output / cache read = the emitting call's "
                            "measured usage split evenly over its tool calls",
                            "cost-weighted = " + pricing.COST_WEIGHTED_FORMULA, "$ = API-equivalent (%s)" %
                            pricing.PRICES_SOURCE, "est_savings are estimates; each states its formula"]}


def redact(obj, salt="tokenscope", terms=None):
    from .redact import redact as _r
    return _r(obj, salt, terms)


def to_text(d, fmt_tok, fmt_usd):
    lines = ["Patterns (%d): mined from real calls; ranked by cost-weighted tokens" % len(d["patterns"])]
    for p in d["patterns"]:
        t = p["tokens"]
        lines.append("")
        lines.append("[%s] %s" % (p["kind"], p["title"]))
        lines.append("  %s cost-weighted, %s | new %s%s, output %s, cache read %s | %d× in %d transcripts, %d sessions, "
                     "%d projects %s | confidence %s" % (
                         fmt_tok(t["cost_weighted"]), fmt_usd(p["api_cost_usd"]), fmt_tok(t["new"]),
                         "~" if p["tokens_est"] else "", fmt_tok(t["output"]), fmt_tok(t["cache_read"]), p["count"],
                         p["transcripts"], p["sessions"], p["projects"],
                         ", ".join("%s %d" % kv for kv in sorted(p["where"].items())), p["confidence"]))
        for e in p["examples"][:3]:
            lines.append("  e.g. %s %s #%s: %s" % (e["session"], e["agent"], e["call"], e["label"]))
        s = p["suggestion"]
        if s:
            sv = s.get("est_savings")
            lines.append("  → %s: %s%s" % (s["type"], s["text"][:220], " (est. saves %s cost-weighted: %s)" % (
                fmt_tok(sv["cost_weighted"]), sv["formula"]) if sv else ""))
    lines.append("")
    lines.append("per miner: " + ", ".join("%s %d (%.2fs)" % (k, d["counts"][k], d["timings"].get(k, 0))
                                           for k in d["counts"]))
    lines.append("~ = estimate (chars/4 result tokens or split shares)")
    return "\n".join(lines)
