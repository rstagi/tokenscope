"""Top spenders by time window: the sessions, goals, turns, intents and steps that cost most in the last 5h / 24h / 7d /
30d.

Data: the cross-session index (tokenscope/xindex.py, cached per transcript by path + size + mtime under
<cache dir>/xindex/). Each indexed session carries "spend" (spend_facts): every API call (main thread and subagents)
as [ts, step, cw, usd, input, cache_write, cache_read, output, prompt], sorted by time, plus its steps, intent groups
and real user prompts.
A call is charged to the step holding its turn (a turn split over steps is split evenly), a subagent's call to the
step that launched it, any other call (a final answer, an agent we cannot place) to the latest step started at or
before it. Its prompt: the one holding its turn (the launching turn for a subagent), else the latest prompt at or
before it (-1: before the first prompt).

Grouping (`by`): "intent" (default) ranks sessions, intents and steps; "session", "goal" or "turn" rank just those.
Turns: real user prompts (not tool results or meta records), id = the prompt record's transcript uuid, label = its
text cut to TURN_CHARS. Goals: labelled user goals (the keyword name of the prompt that opened them); a follow-up
prompt (goals.is_follow_up: "yes", "now run the tests", "continue") stays in the goal before it, one opening a session
continues its project's latest goal from an earlier session (a resumed task), and prompts of one project with the same
goal name share one goal. A goal row links to the session holding most of its in-window $.

Window: [now - length, now], both ends included. Only calls whose own timestamp falls inside count, so a session
(intent, step) straddling the start contributes just its calls inside. Per row: $ (API-equivalent, pricing.py),
CW, tokens (input + cache write + cache read + output), share of the window's $ (of its CW when nothing is
priced), wall time (the row's span clipped to the window) and active time (gaps of <= ACTIVE_GAP_S between its
calls inside the window). Every row names its session's goal ("session_label"). Intents: intent groups; a step outside any group is its own intent (as the pane names the
current intent).
"""
import bisect
from collections import defaultdict
from datetime import datetime, timezone

WINDOWS = {"5h": 5 * 3600, "24h": 24 * 3600, "7d": 7 * 86400, "30d": 30 * 86400}
ALIASES = {"day": "24h", "1d": "24h", "week": "7d", "1w": "7d", "month": "30d"}
ACTIVE_GAP_S = 300          # a longer pause between two calls is idle time (waiting for the user, a break)
LIMIT = 10
TURN_CHARS = 80             # a turn's label: its prompt text cut to this many characters
BY = ("intent", "session", "goal", "turn")
BY_KEY = {"intent": "intents", "session": "sessions", "goal": "goals", "turn": "turns"}   # by -> its rows' key
FIELDS = ("cw", "usd", "input", "cache_write", "cache_read", "output")


def window_seconds(name):
    """Length in seconds of a window name (5h, 24h, 7d, 30d; day, week, month); ValueError otherwise."""
    key = ALIASES.get(str(name).strip().lower(), str(name).strip().lower())
    if key not in WINDOWS:
        raise ValueError("bad window %r (use %s)" % (name, ", ".join(WINDOWS)))
    return key, WINDOWS[key]


def parse_by(name):
    """A grouping name (session, goal, turn, intent; plural accepted; empty = intent); ValueError otherwise."""
    key = str(name or "intent").strip().lower()
    key = key[:-1] if key.endswith("s") and key[:-1] in BY else key
    if key not in BY:
        raise ValueError("bad grouping %r (use %s)" % (name, ", ".join(BY)))
    return key


def parse_now(s):
    """An ISO timestamp (naive = UTC) or epoch seconds -> epoch seconds; ValueError otherwise."""
    if isinstance(s, (int, float)):
        return float(s)
    try:
        return float(s)
    except (TypeError, ValueError):
        pass
    try:
        t = datetime.fromisoformat(str(s).strip().replace("Z", "+00:00"))
    except ValueError:
        raise ValueError("bad --now %r (use an ISO timestamp, e.g. 2026-10-12T09:00:00Z)" % s) from None
    return (t if t.tzinfo else t.replace(tzinfo=timezone.utc)).timestamp()


def iso(t):
    return datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------- per-session facts (indexed, cached)

def spend_facts(state, s, steps, groups):
    """{"goal", "ev", "steps", "groups", "prompts"} of a replayed session (see the module doc); prompts:
    [[uuid, ts, text (TURN_CHARS + 1 at most), goal name, follow-up?]]."""
    from .goals import is_follow_up
    idx = {id(t): k for k, t in enumerate(s.main.turn_steps)}
    by_turn = defaultdict(list)
    for x in steps:
        if x.get("turn_first") is not None:
            for k in range(x["turn_first"], x["turn_last"] + 1):
                by_turn[k].append(x["i"])
    starts = [x.get("ts0") or 0.0 for x in steps]
    prompt_at, prompt_ts = list(s.prompt_at), [p.get("ts") or 0.0 for p in s.prompts]

    def prompt_of(k, ts):
        if k is not None:
            return bisect.bisect_right(prompt_at, k) - 1
        return bisect.bisect_right(prompt_ts, ts or 0.0) - 1

    def at(ts):
        return [steps[max(0, bisect.bisect_right(starts, ts or 0.0) - 1)]["i"]] if steps else [-1]

    def turn_of(tid):
        hit = s.tools.get(tid)
        return idx.get(id(hit[0])) if hit and hit[2] is s.main else None

    launch = {}
    for tid, agents in state.workflow_agents(s).items():
        for a in agents:
            launch[a["id"]] = turn_of(tid)
    for aid, a in s.agents.items():
        if aid not in launch:
            launch[aid] = turn_of(a.get("tool_use_id"))

    def top_turn(aid):
        seen = set()
        while aid in s.agents and aid not in seen:
            seen.add(aid)
            if launch.get(aid) is not None:
                return launch[aid]
            aid = s.agents[aid].get("parent_id")
        return None

    ev = []

    def add(m, k):
        u = m["usage"]
        ts = m.get("ts") or s.started
        if u is None or ts is None:
            return
        p = prompt_of(k, ts)
        sids = (by_turn.get(k) if k is not None else None) or at(ts)
        n = len(sids)
        vals = [m["cw"], m["usd"], u["input"], u["cache_write"], u["cache_read"], u["output"]]
        for i in sids:
            ev.append([ts, i] + [round(v / n, 6 if j == 1 else 1) for j, v in enumerate(vals)] + [p])

    for m in s.main.msgs.values():
        k = idx.get(id(m["step"])) if m["step"] is not None else None
        add(m, k)
    for aid, br in s.branches.items():
        k = top_turn(aid)
        for m in br.msgs.values():
            add(m, k)
    ev.sort(key=lambda e: (e[0], e[1]))
    return {"goal": (s.goal or {}).get("name"), "ev": ev,
            "steps": [[str(x.get("label") or ""), x.get("cat"), x.get("ts0"), x.get("drive_ts1") or x.get("ts1")]
                      for x in steps],
            "groups": [[str(g.get("label") or ""), g.get("cat"), g["first"], g["last"], g.get("ts0"), g.get("ts1")]
                       for g in groups],
            "prompts": [[uuid, p.get("ts"), " ".join(str(p.get("text") or "").split())[:TURN_CHARS + 1],
                         str(p.get("name") or ""), is_follow_up(p.get("text"))]
                        for uuid, p in zip(s.prompt_uuid, s.prompts)]}


def goal_of_prompts(sessions):
    """{(sid, prompt index): (goal key, goal name, goal id)} over index entries `sessions` (see the module doc:
    follow-ups stay in their goal, a session opened by a follow-up continues its project's latest goal, same name =
    same goal); goal id: the uuid of the goal's first prompt."""
    out, latest, ids = {}, {}, {}
    for sid, e in sorted(sessions.items(), key=lambda kv: (kv[1].get("ts0") or 0.0, kv[0])):
        project = e.get("project")
        cur = None
        for n, p in enumerate((e.get("spend") or {}).get("prompts") or ()):
            if p[4] and cur is None:
                cur = latest.get(project)                     # a resumed task
            if not (p[4] and cur is not None):
                name = p[3] or _raw_goal(p[2]) or "(prompt)"
                key = (project, name.strip().lower())
                cur = (key, name, ids.setdefault(key, p[0] or "%s:%d" % (sid, n + 1)))
            out[(sid, n)] = cur
        if cur is not None:
            latest[project] = cur
    return out


def _raw_goal(text):
    """A goal named by its prompt text (an entry without a name): cleaned up when that text reads oddly."""
    from .live.goalname import goal_label, reads_oddly
    return goal_label(text) if text and reads_oddly(text) else text


def turn_label(text):
    text = str(text or "")
    return text if len(text) <= TURN_CHARS else text[:TURN_CHARS - 1].rstrip() + "…"


# ---------------------------------------------------------------- the window

def _row(**kw):
    return dict(kw, calls=0, ts=[], **{f: 0.0 for f in FIELDS})


def _clip(t0, t1, start, end):
    return max(0.0, min(t1, end) - max(t0, start))


def _finish(r, total, start, end):
    ts = sorted(r.pop("ts"))
    r["active_s"] = round(sum(b - a for a, b in zip(ts, ts[1:]) if b - a <= ACTIVE_GAP_S), 1)
    span = [t for t in (r.pop("span", None) or ()) if t is not None] + ts[:1] + ts[-1:]   # calls charged by time
    r["wall_s"] = round(_clip(min(span), max(span), start, end), 1) if span else 0.0      # may fall past its end
    r["tokens"] = int(round(r["input"] + r["cache_write"] + r["cache_read"] + r["output"]))
    for f in ("input", "cache_write", "cache_read", "output"):
        r[f] = int(round(r[f]))
    r["cw"], r["usd"] = round(r["cw"], 1), round(r["usd"], 6)
    key = "usd" if total["usd"] > 0 else "cw"
    r["share"] = round(100.0 * r[key] / total[key], 2) if total[key] else 0.0
    return r


def _add(r, e):
    r["calls"] += 1
    r["ts"].append(e[0])
    for k, f in enumerate(FIELDS):
        r[f] += e[2 + k]


def _step_rows(sr, base, ev, steps, groups):
    """Charge a session's in-window calls `ev` to its session row `sr`, steps and intents: (step rows, intent rows)."""
    step_rows, group_of, intent_rows = {}, {}, {}
    for gi, g in enumerate(groups):
        for i in range(g[2], g[3] + 1):
            group_of[i] = gi
    for x in ev:
        _add(sr, x)
        i = x[1]
        if i < 0 or i >= len(steps):
            continue
        st = steps[i]
        if i not in step_rows:
            step_rows[i] = _row(**base, label=st[0], cat=st[1], i=i, span=(st[2], st[3]))
        _add(step_rows[i], x)
        gi = group_of.get(i)
        key = ("g", gi) if gi is not None else ("s", i)
        if key not in intent_rows:
            if gi is not None:
                g = groups[gi]
                intent_rows[key] = _row(**base, label=g[0], cat=g[1], steps=g[3] - g[2] + 1, span=(g[4], g[5]))
            else:
                intent_rows[key] = _row(**base, label=st[0], cat=st[1], steps=1, span=(st[2], st[3]))
        _add(intent_rows[key], x)
    return list(step_rows.values()), list(intent_rows.values())


def _prompt_rows(sr, base, ev, prompts, goal_of, goals):
    """Charge a session's in-window calls `ev` to its session row `sr` and its turns: the turn rows; with `goal_of`
    (goal_of_prompts) also to the goal rows in `goals` (goal key -> row, shared across sessions)."""
    sid, turn_rows = base["sid"], {}
    for x in ev:
        _add(sr, x)
        n = x[8] if len(x) > 8 else -1
        if not 0 <= n < len(prompts):
            continue
        p = prompts[n]
        if n not in turn_rows:
            turn_rows[n] = _row(**base, id=p[0] or "%s:%d" % (sid, n + 1), n=n + 1, label=turn_label(p[2]), goal=p[3],
                                span=[p[1]])
        _add(turn_rows[n], x)
        if goal_of is None:
            continue
        key, name, gid = goal_of[(sid, n)]
        g = goals.get(key)
        if g is None:
            g = goals[key] = _row(**base, id=gid, label=name, span=[], by_sid={}, turns=set())
        _add(g, x)
        g["span"].append(p[1])
        g["turns"].add((sid, n))
        sb = g["by_sid"].setdefault(sid, [0.0, 0.0, base])
        sb[0] += x[3]
        sb[1] += x[2]
    return list(turn_rows.values())


def top(sessions, end, seconds, limit=LIMIT, by="intent"):
    """Top spenders of index entries `sessions` ({sid: xindex entry}) in [end - seconds, end]: {"by", "start", "end",
    "total", ...} with "sessions", "intents", "steps" (by intent) or the BY_KEY rows of `by` (rows sorted by $, then
    CW; `limit` each, None = all)."""
    start = end - seconds
    total = {f: 0.0 for f in FIELDS}
    total.update(calls=0, sessions=0)
    srows, irows, xrows, trows, grows = [], [], [], [], {}
    goal_of = goal_of_prompts(sessions) if by == "goal" else {}
    for sid, e in sessions.items():
        sp = e.get("spend") or {}
        ev = sp.get("ev") or ()
        times = [x[0] for x in ev]
        lo, hi = bisect.bisect_left(times, start), bisect.bisect_right(times, end)
        if lo >= hi:
            continue
        base = {"sid": sid, "short": sid[:8], "project": e.get("project"), "session_label": sp.get("goal") or ""}
        steps, groups, prompts = sp.get("steps") or [], sp.get("groups") or [], sp.get("prompts") or []
        ends = [x[3] for x in steps if x[3]]
        sr = _row(**base, label=sp.get("goal") or "", span=(times[0], max([times[-1]] + ends)))
        if by in ("goal", "turn"):
            trows += _prompt_rows(sr, base, ev[lo:hi], prompts, goal_of if by == "goal" else None, grows)
        else:
            step_rows, intent_rows = _step_rows(sr, base, ev[lo:hi], steps, groups)
            xrows += step_rows
            irows += intent_rows
        for f in FIELDS:
            total[f] += sr[f]
        total["calls"] += sr["calls"]
        total["sessions"] += 1
        srows.append(sr)

    def best(rows):
        rows = [_finish(r, total, start, end) for r in rows]
        rows.sort(key=lambda r: (-r["usd"], -r["cw"], r["sid"], r.get("i", 0), r.get("n", 0)))
        return rows if limit is None else rows[:limit]
    for g in grows.values():   # link a goal to the session holding most of its in-window $ (then CW)
        by_sid = g.pop("by_sid")
        g.update(max(by_sid.values(), key=lambda v: (v[0], v[1], v[2]["sid"]))[2])
        g["sessions"], g["turns"] = len(by_sid), len(g["turns"])
    out = {"by": by, "start": start, "end": end, "start_iso": iso(start), "end_iso": iso(end),
           "share_of": "usd" if total["usd"] > 0 else "cw"}
    if by == "intent":
        out.update(sessions=best(srows), intents=best(irows), steps=best(xrows))
    elif by == "session":
        out.update(sessions=best(srows))
    elif by == "goal":
        out.update(goals=best(grows.values()))
    else:
        out.update(turns=best(trows))
    total["tokens"] = int(round(sum(total[f] for f in ("input", "cache_write", "cache_read", "output"))))
    total["cw"], total["usd"] = round(total["cw"], 1), round(total["usd"], 6)
    out["total"] = total
    return out


def top_spenders(window="5h", now=None, root=None, limit=LIMIT, cache=None, jobs=None, index=None, by="intent"):
    """Build (incrementally) the cross-session index under `root` and rank the window ending at `now` (epoch s,
    default the current time) grouped `by` (see BY). `index`: an already built xindex.XIndex. Adds "window" and
    "index" (build stats)."""
    import time
    from .xindex import XIndex
    name, seconds = window_seconds(window)
    by = parse_by(by)
    if index is None:
        index = XIndex(root, cache)
        index.build(jobs=jobs)
    out = top(index.sessions, time.time() if now is None else float(now), seconds, limit, by)
    out.update(window=name, index=dict(index.stats))
    return out
