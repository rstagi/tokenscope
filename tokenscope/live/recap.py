"""Idle recap for the live pane: what the work since the last user prompt cost, and evidence-backed hints.

Pure functions, no I/O. `recap(...)` takes the labelled steps and groups of the main thread (steps.segment +
Labeller) and per-turn facts the live state keeps (see `LiveState.recap_input`):

    turns      = [{"ts": float|None, "cw": float,
                   "calls": [{"tool": raw name, "file": path|None, "full": bool (read in full), "edit": bool,
                              "search": bool (Grep / Glob / find ...), "poll": bool (only waits: sleep, Monitor ...)}],
                   "tests": [{"ok": bool, "failed": int|None, "sig": str|None, "runner": str}]}]   # main thread
    prompt_at  = [turn index at which each user prompt arrived]
    agents     = [{"id", "ts": first record ts, "reads": [(path, tokens of the read result, later turns of that
                   subagent)]}]   # full reads
    ctx        = {"now": tokens, "start": tokens when the last prompt arrived, "window": 200k | 1M,
                  "headless": bool (a prompt came from `claude -p` / an SDK program: no next task to prepare for)}
    compact_at = [turn index at which each compaction happened]

An "intent" is a group, or a step outside any group. The window is the work since the last user prompt; the
"session" variant covers every intent (costliest + slowest only). A hint is only returned when its rule's evidence
crosses conservative thresholds (module constants below); nothing rather than filler.
"""
import statistics
from collections import Counter

from .. import pricing

MAX_HINTS = 3
HINT_MIN_CW = 15_000          # a hint whose estimated CW at stake is below this is not worth a line

# (a) re-discovery: the same file read in full again and again across intents, unchanged in between
A_READS = 4                   # full reads of the file in the session
A_REREADS = 3                 # of which re-reads with no edit to it / compaction since the previous read
A_INTENTS = 3                 # distinct intents reading it
A_SKIP_EXT = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".pdf")   # screenshots / renders: re-read after regenerating
# (b) one intent taking a large share of the request, mostly re-reads and searches
B_SHARE = 0.40
B_MIN_CW = 100_000
B_MIN_CALLS = 10
B_FRAC = 0.60                 # share of its calls that are searches or reads of a file already read in full
# (c) tests run again and again with the same failure
C_RUNS = 4                    # consecutive failing runs (no pass between) with the same failure signature
# (d) ToolSearch / deferred tool loading overhead
D_CALLS = 3
D_MIN_CW = 30_000
D_SHARE = 0.15
# (e) near autocompaction: this request pushed the context past E_NEAR of the window (growth alone is normal work,
# and a context that was already that full before the request was flagged at an earlier idle point / by the
# warning); never in headless / SDK sessions, which have no next task
E_NEAR = 0.85
E_BASE = 20_000               # what a fresh / compacted context would still hold
E_CACHE_READ = 0.1            # CW of a cache-read token
# (f) several subagents reading the same files
F_AGENTS = 3                  # distinct subagents reading one file in full
F_SHARE = 0.20                # ... and at least this share of the request's subagents
F_FILES = 2                   # files read that way
F_MIN_CW = 100_000
F_SKIP_EXT = A_SKIP_EXT + (".bmp", ".ico", ".tiff", ".zip", ".gz", ".tar", ".whl", ".jar", ".so", ".dylib",
                           ".dll", ".exe", ".bin", ".wasm", ".pyc", ".sqlite", ".db")   # images / PDFs / binaries
# (g) polling: wake-ups that only wait again
G_RUN = 5                     # consecutive wait-only main turns
# (h) one intent far above the session median, mostly code exploration (reads of files, searches)
H_MIN_INTENTS = 5
H_RATIO = 8.0
H_MIN_CW = 200_000
H_MIN_CALLS = 8
H_EXPLORE = 0.70              # share of its calls reading files (not polling output) or searching


def _ident(x):
    return x


def intents_of(steps, groups):
    """Groups and ungrouped steps in step order: [{"label", "cat", "cw", "usd", "steps", "ts0", "ts1",
    "turn_first", "turn_last"}]."""
    by_step = {}
    for g in groups or ():
        for i in range(g["first"], g["last"] + 1):
            by_step[i] = g
    out, done = [], set()
    for s in steps:
        g = by_step.get(s["i"])
        if g is not None:
            if id(g) in done:
                continue
            done.add(id(g))
            members = steps[g["first"]:g["last"] + 1]   # steps[i]["i"] == i
            it = {"label": g["label"], "cat": g["cat"], "cw": g["cw"], "usd": g["usd"], "steps": members}
        else:
            it = {"label": s.get("label") or "step", "cat": s["cat"], "cw": s["cw"], "usd": s["usd"], "steps": [s]}
        ts = [t for x in it["steps"] for t in (x.get("ts0"), x.get("ts1")) if t]
        it.update(ts0=min(ts) if ts else None, ts1=max(ts) if ts else None,
                  turn_first=it["steps"][0]["turn_first"], turn_last=it["steps"][-1]["turn_last"])
        out.append(it)
    return out


def _turn_owner(intents):
    """turn index -> index of the intent holding it (a split turn goes to the first)."""
    own = {}
    for n, it in enumerate(intents):
        for k in range(it["turn_first"], it["turn_last"] + 1):
            own.setdefault(k, n)
    return own


def _share(turn):
    calls = turn.get("calls") or ()
    return float(turn.get("cw") or 0) / len(calls) if calls else 0.0


def _brief(it, total, lab):
    if it is None:
        return None
    out = {"label": lab(it["label"]), "cat": it["cat"], "cw": round(it["cw"]), "usd": round(it["usd"], 4)}
    if total:
        out["pct"] = int(round(100.0 * it["cw"] / total))
    return out


def _slowest(intents, lab):
    timed = [it for it in intents if it["ts0"] and it["ts1"] and it["ts1"] > it["ts0"]]
    if not timed:
        return None
    it = max(timed, key=lambda x: x["ts1"] - x["ts0"])
    return {"label": lab(it["label"]), "cat": it["cat"], "s": round(it["ts1"] - it["ts0"], 1)}


def fmt_k(n):
    n = float(n or 0)
    return "%.1fM" % (n / 1e6) if n >= 1e6 else "%dk" % round(n / 1e3) if n >= 1e3 else "%d" % n


def _hint(rule, text, evidence, cw, estimate):
    return {"rule": rule, "text": text, "evidence": evidence, "cw_at_stake": int(round(cw)), "estimate": estimate}


# ---------------------------------------------------------------- rules (each returns a hint or None)

def rule_reread(turns, own, k0, compact_at, name):
    """(a) the same file read in full >= A_READS times, >= A_REREADS of them unchanged since the previous read,
    across >= A_INTENTS intents, at least one re-read in this request."""
    compacts = sorted(compact_at or ())
    files = {}
    edited_since = {}
    for k, t in enumerate(turns):
        for c in t.get("calls") or ():
            f = c.get("file")
            if not f:
                continue
            if c.get("edit"):
                edited_since[f] = True
                continue
            if not c.get("full") or c.get("poll") or f.lower().endswith(A_SKIP_EXT):
                continue
            st = files.setdefault(f, {"reads": 0, "rereads": 0, "intents": set(), "cw": 0.0, "last": None,
                                      "in_window": False})
            fresh = st["last"] is None or edited_since.get(f) or any(st["last"] < x <= k for x in compacts)
            st["reads"] += 1
            st["intents"].add(own.get(k))
            if not fresh:
                st["rereads"] += 1
                st["cw"] += _share(t)
                st["in_window"] = st["in_window"] or k >= k0
            st["last"], edited_since[f] = k, False
    hits = [(f, st) for f, st in files.items() if st["reads"] >= A_READS and st["rereads"] >= A_REREADS
            and len(st["intents"]) >= A_INTENTS and st["in_window"]]
    if not hits:
        return None
    hits.sort(key=lambda x: -x[1]["cw"])
    f, st = hits[0]
    more = " (+%d more file%s)" % (len(hits) - 1, "s" if len(hits) > 2 else "") if len(hits) > 1 else ""
    return _hint("reread", "Note what %s holds in CLAUDE.md (or keep a digest) instead of re-reading it" % name(f),
                 "%s read in full %d× across %d intents, %d× unchanged%s" % (name(f), st["reads"], len(st["intents"]),
                                                                            st["rereads"], more),
                 sum(x["cw"] for _, x in hits), "CW of the unchanged re-reads")


def rule_search_heavy(intents, turns, win, total, lab):
    """(b) one intent with >= B_SHARE of the request's CW, >= B_MIN_CW, made mostly (>= B_FRAC of >= B_MIN_CALLS
    calls) of searches and reads of files already read in full."""
    if not total:
        return None
    seen = set()
    best = None
    for n, it in enumerate(intents):
        calls = searchy = 0
        for k in range(it["turn_first"], it["turn_last"] + 1):
            if k >= len(turns):
                break
            for c in turns[k].get("calls") or ():
                calls += 1
                f = c.get("file")
                if c.get("search") or (f and not c.get("edit") and f in seen and c["tool"] in ("Read", "Bash")):
                    searchy += 1
                if f and c.get("full"):
                    seen.add(f)
                elif f and c.get("edit"):
                    seen.discard(f)
        if n not in win or it["cw"] < B_MIN_CW or it["cw"] < B_SHARE * total or calls < B_MIN_CALLS \
                or searchy < B_FRAC * calls:
            continue
        if best is None or it["cw"] > best[0]["cw"]:
            best = (it, n, calls, searchy)
    if best is None:
        return None
    it, n, calls, searchy = best
    h = _hint("search_heavy", "Add a CLAUDE.md note on where things live, so the next task starts there",
              "'%s': %d%% of this request's CW (%s), %d of %d calls searches or re-reads"
              % (lab(it["label"]), round(100.0 * it["cw"] / total), fmt_k(it["cw"]), searchy, calls),
              it["cw"] * searchy / calls, "the intent's CW × its share of searches / re-reads")
    h["_intent"] = n
    return h


def rule_test_repeat(turns, k0):
    """(c) >= C_RUNS consecutive failing test runs (no pass between) of one runner with the same failure."""
    runs = [(k, x) for k, t in enumerate(turns) if k >= k0 for x in t.get("tests") or ()]
    best, cur = None, []
    for k, x in runs + [(None, None)]:
        same = x is not None and not x["ok"] and cur and \
            (x["runner"], x.get("failed"), x.get("sig")) == (cur[0][1]["runner"], cur[0][1].get("failed"),
                                                            cur[0][1].get("sig"))
        if same:
            cur.append((k, x))
            continue
        if len(cur) >= C_RUNS and (best is None or len(cur) > len(best)):
            best = cur
        cur = [(k, x)] if x is not None and not x["ok"] else []
    if not best:
        return None
    a, b = best[1][0], best[-1][0]
    cw = sum(float(turns[k].get("cw") or 0) for k in range(a, b + 1))
    edits = sum(1 for k in range(best[0][0], b + 1) for c in turns[k].get("calls") or () if c.get("edit"))
    x = best[0][1]
    what = "%d failed" % x["failed"] if x.get("failed") else "same error"
    return _hint("test_repeat", "Run only the failing test and read its full output before the next fix",
                 "%s failed %d× in a row with the same failure (%s), %d edit%s between"
                 % (x["runner"], len(best), what, edits, "" if edits == 1 else "s"),
                 cw, "CW from the 2nd failing run to the last")


def rule_toolsearch(turns, win_intents, k0, total):
    """(d) >= D_CALLS ToolSearch calls in this request whose OVERHEAD steps cost >= D_MIN_CW and >= D_SHARE."""
    n = sum(1 for t in turns[k0:] for c in t.get("calls") or () if c["tool"] == "ToolSearch")
    over = sum(s["cw"] for it in win_intents for s in it["steps"] if s["cat"] == "overhead")
    if n < D_CALLS or over < D_MIN_CW or not total or over < D_SHARE * total:
        return None
    return _hint("toolsearch", "Load the deferred tools once (one select: query naming them all) or allow them up front",
                 "%d ToolSearch calls: %s CW, %d%% of this request" % (n, fmt_k(over), round(100.0 * over / total)),
                 over * (n - 1) / n, "OVERHEAD CW beyond one load")


def rule_context(ctx, turns, prompt_at):
    """(e) this request took the context from below E_NEAR of the window to at least E_NEAR of it, in a session
    with a person at the keyboard. The estimate: cache reads of the context beyond a fresh one over the session's
    median turns per request (the next task, if it is like the ones so far)."""
    ctx = ctx or {}
    now, start, window = ctx.get("now") or 0, ctx.get("start") or 0, ctx.get("window") or 0
    if not window or not now or ctx.get("headless"):
        return None
    grew = now - start
    if now < E_NEAR * window or start >= E_NEAR * window:
        return None
    bounds = sorted(set(prompt_at or ())) or [0]
    per = [b - a for a, b in zip(bounds, bounds[1:] + [len(turns)]) if b > a]
    med = statistics.median(per) if per else 0
    if not med:
        return None
    ev = "context %s / %s (%d%%)" % (fmt_k(now), fmt_k(window), round(100.0 * now / window))
    if grew > 0:
        ev += ", +%s during this request" % fmt_k(grew)
    return _hint("context", "/compact (or start a fresh session with a summary) before the next task", ev,
                 E_CACHE_READ * max(0, now - E_BASE) * med,
                 "cache reads of the extra context over this session's median %s turns per request" % ("%g" % med))


def rule_shared_reads(agents, ts0, name):
    """(f) >= F_FILES files (not images / PDFs) each read in full by >= F_AGENTS of this request's subagents and
    >= F_SHARE of them, the duplicate reads costing >= F_MIN_CW. A read's cost is its own size, not its turn's CW:
    its tokens written to the cache once, then read from the cache on every later turn of that subagent."""
    agents = [a for a in agents or () if ts0 is None or (a.get("ts") or 0) >= ts0]
    need = max(F_AGENTS, F_SHARE * len(agents))
    by_file = {}
    for a in agents:
        for f, tok, later in a.get("reads") or ():
            if f.lower().endswith(F_SKIP_EXT):
                continue
            d = by_file.setdefault(f, {})
            d.setdefault(a["id"], []).append(tok * (pricing.W_CACHE_WRITE + pricing.W_CACHE_READ * later))
    hits = [(f, d) for f, d in by_file.items() if len(d) >= need]
    if len(hits) < F_FILES:
        return None
    hits.sort(key=lambda x: (-len(x[1]), name(x[0])))
    cw = 0.0
    for _, d in hits:
        firsts = sorted(min(v) for v in d.values())
        cw += sum(firsts[1:])   # every agent but one could have had it from a digest
    if cw < F_MIN_CW:
        return None
    names = ", ".join(name(f) for f, _ in hits[:2]) + (" (+%d)" % (len(hits) - 2) if len(hits) > 2 else "")
    return _hint("shared_reads", "Give the subagents one shared digest of these files in their prompts",
                 "%s each read in full by %d of %d subagents" % (names, min(len(d) for _, d in hits[:2]), len(agents)),
                 cw, "size of the duplicate subagent reads (result chars/4) x (1.25 cache write + 0.1 cache read "
                     "per later turn of that subagent)")


def rule_polling(turns, k0):
    """(g) >= G_RUN consecutive main turns in this request that only waited (sleep / Monitor / TaskOutput ...)."""
    best, cur = [], []
    for k in range(k0, len(turns) + 1):
        t = turns[k] if k < len(turns) else None
        calls = (t or {}).get("calls") or ()
        if t is not None and calls and all(c.get("poll") for c in calls):
            cur.append(k)
            continue
        if len(cur) > len(best):
            best = cur
        cur = []
    if len(best) < G_RUN:
        return None
    tools = Counter("sleep" if c["tool"] == "Bash" else c["tool"] for k in best for c in turns[k]["calls"])
    cw = sum(float(turns[k].get("cw") or 0) for k in best[1:])
    return _hint("polling", "Wait in one call (a longer sleep, Monitor with an until-condition, or a background "
                            "run and its notification) instead of polling",
                 "%d wake-ups in a row only waited (%s): %s CW"
                 % (len(best), ", ".join("%s ×%d" % kv for kv in tools.most_common(2)), fmt_k(cw + float(turns[best[0]].get("cw") or 0))),
                 cw, "CW of the wake-ups after the first")


def _explore(it, turns):
    """(calls, exploring calls) of an intent: reads of files that are not background-task output, and searches."""
    calls = explore = 0
    for k in range(it["turn_first"], min(it["turn_last"] + 1, len(turns))):
        for c in turns[k].get("calls") or ():
            calls += 1
            explore += bool(c.get("search") or (c.get("file") and not c.get("edit") and not c.get("poll")))
    return calls, explore


def rule_outlier(intents, turns, win, skip, lab):
    """(h) an intent of this request costing >= H_RATIO × the session's median intent (>= H_MIN_INTENTS intents),
    >= H_MIN_CW, with >= H_EXPLORE of its (>= H_MIN_CALLS) calls reading files or searching."""
    if len(intents) < H_MIN_INTENTS:
        return None
    med = statistics.median(it["cw"] for it in intents)
    if med <= 0:
        return None
    cands = []
    for n, it in enumerate(intents):
        if n not in win or n in skip or it["cw"] < H_MIN_CW or it["cw"] < H_RATIO * med:
            continue
        calls, explore = _explore(it, turns)
        if calls >= H_MIN_CALLS and explore >= H_EXPLORE * calls:
            cands.append((it, calls, explore))
    if not cands:
        return None
    it, calls, explore = max(cands, key=lambda x: x[0]["cw"])
    return _hint("outlier", "Point the agent at the right files up front (or ask for a short plan first)",
                 "'%s': %s CW = %d× the median intent (%s), %d of %d calls reads or searches"
                 % (lab(it["label"]), fmt_k(it["cw"]), round(it["cw"] / med), fmt_k(med), explore, calls),
                 it["cw"] - 2 * med, "CW above twice the median intent")


# ---------------------------------------------------------------- the recap

def hints(intents, turns, k0, win, total, agents=(), ctx=None, compact_at=(), name=_ident, lab=_ident, prompt_at=()):
    """Up to MAX_HINTS hints for the window (intent indices `win`, turns from k0), ranked by CW at stake."""
    own = _turn_owner(intents)
    win_intents = [intents[n] for n in sorted(win)]
    ts0 = min((it["ts0"] for it in win_intents if it["ts0"]), default=None)
    b = rule_search_heavy(intents, turns, win, total, lab)
    out = [rule_reread(turns, own, k0, compact_at, name), b, rule_test_repeat(turns, k0),
           rule_toolsearch(turns, win_intents, k0, total), rule_context(ctx, turns, prompt_at),
           rule_shared_reads(agents, ts0, name),
           rule_polling(turns, k0), rule_outlier(intents, turns, win, {b["_intent"]} if b else set(), lab)]
    out = [h for h in out if h and h["cw_at_stake"] >= HINT_MIN_CW]
    for h in out:
        h.pop("_intent", None)
    out.sort(key=lambda h: -h["cw_at_stake"])
    return out[:MAX_HINTS]


def recap(steps, groups, turns, prompt_at=(), agents=(), ctx=None, compact_at=(), name=_ident, lab=_ident):
    """{"cw", "usd", "intents", "costliest", "slowest", "hints", "session": {...}} for the work since the last user
    prompt, or None when there is none. `name` / `lab` display a file path / a label (redaction-aware)."""
    intents = intents_of(steps, groups)
    k0 = max(prompt_at) if prompt_at else 0
    win = {n for n, it in enumerate(intents) if it["turn_first"] >= k0}
    if not win:
        return None
    w = [intents[n] for n in sorted(win)]
    total = sum(it["cw"] for it in w)
    s_total = sum(it["cw"] for it in intents)
    return {"cw": round(total), "usd": round(sum(it["usd"] for it in w), 4), "intents": len(w),
            "costliest": _brief(max(w, key=lambda x: x["cw"]), total, lab), "slowest": _slowest(w, lab),
            "hints": hints(intents, turns, k0, win, total, agents, ctx, compact_at, name, lab, prompt_at),
            "session": {"cw": round(s_total), "usd": round(sum(it["usd"] for it in intents), 4),
                        "intents": len(intents), "costliest": _brief(max(intents, key=lambda x: x["cw"]), s_total, lab),
                        "slowest": _slowest(intents, lab)}}
