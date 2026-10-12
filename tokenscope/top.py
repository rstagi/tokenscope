"""Top spenders by time window: the sessions, intents and steps that cost most in the last 5h / 24h / 7d / 30d.

Data: the cross-session index (tokenscope/xindex.py, cached per transcript by path + size + mtime under
<cache dir>/xindex/). Each indexed session carries "spend" (spend_facts): every API call (main thread and subagents)
as [ts, step, cw, usd, input, cache_write, cache_read, output], sorted by time, plus its steps and intent groups.
A call is charged to the step holding its turn (a turn split over steps is split evenly), a subagent's call to the
step that launched it, any other call (a final answer, an agent we cannot place) to the latest step started at or
before it.

Window: [now - length, now], both ends included. Only calls whose own timestamp falls inside count, so a session
(intent, step) straddling the start contributes just its calls inside. Per row: $ (API-equivalent, pricing.py),
CW, tokens (input + cache write + cache read + output), share of the window's $ (of its CW when nothing is
priced), wall time (the row's span clipped to the window) and active time (gaps of <= ACTIVE_GAP_S between its
calls inside the window). Intents: intent groups; a step outside any group is its own intent (as the pane names the
current intent).
"""
import bisect
from collections import defaultdict
from datetime import datetime, timezone

WINDOWS = {"5h": 5 * 3600, "24h": 24 * 3600, "7d": 7 * 86400, "30d": 30 * 86400}
ALIASES = {"day": "24h", "1d": "24h", "week": "7d", "1w": "7d", "month": "30d"}
ACTIVE_GAP_S = 300          # a longer pause between two calls is idle time (waiting for the user, a break)
LIMIT = 10
FIELDS = ("cw", "usd", "input", "cache_write", "cache_read", "output")


def window_seconds(name):
    """Length in seconds of a window name (5h, 24h, 7d, 30d; day, week, month); ValueError otherwise."""
    key = ALIASES.get(str(name).strip().lower(), str(name).strip().lower())
    if key not in WINDOWS:
        raise ValueError("bad window %r (use %s)" % (name, ", ".join(WINDOWS)))
    return key, WINDOWS[key]


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
    """{"goal", "ev", "steps", "groups"} of a replayed session (see the module doc)."""
    idx = {id(t): k for k, t in enumerate(s.main.turn_steps)}
    by_turn = defaultdict(list)
    for x in steps:
        if x.get("turn_first") is not None:
            for k in range(x["turn_first"], x["turn_last"] + 1):
                by_turn[k].append(x["i"])
    starts = [x.get("ts0") or 0.0 for x in steps]

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

    def add(m, sids):
        u = m["usage"]
        ts = m.get("ts") or s.started
        if u is None or ts is None:
            return
        sids = sids or at(ts)
        n = len(sids)
        vals = [m["cw"], m["usd"], u["input"], u["cache_write"], u["cache_read"], u["output"]]
        for i in sids:
            ev.append([ts, i] + [round(v / n, 6 if k == 1 else 1) for k, v in enumerate(vals)])

    for m in s.main.msgs.values():
        k = idx.get(id(m["step"])) if m["step"] is not None else None
        add(m, by_turn.get(k) if k is not None else None)
    for aid, br in s.branches.items():
        k = top_turn(aid)
        for m in br.msgs.values():
            add(m, by_turn.get(k) if k is not None else None)
    ev.sort(key=lambda e: (e[0], e[1]))
    return {"goal": (s.goal or {}).get("name"), "ev": ev,
            "steps": [[str(x.get("label") or ""), x.get("cat"), x.get("ts0"), x.get("drive_ts1") or x.get("ts1")]
                      for x in steps],
            "groups": [[str(g.get("label") or ""), g.get("cat"), g["first"], g["last"], g.get("ts0"), g.get("ts1")]
                       for g in groups]}


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


def top(sessions, end, seconds, limit=LIMIT):
    """Top spenders of index entries `sessions` ({sid: xindex entry}) in [end - seconds, end]: {"start", "end",
    "total", "sessions", "intents", "steps"} (rows sorted by $, then CW; `limit` each, None = all)."""
    start = end - seconds
    total = {f: 0.0 for f in FIELDS}
    total.update(calls=0, sessions=0)
    srows, irows, xrows = [], [], []
    for sid, e in sessions.items():
        sp = e.get("spend") or {}
        ev = sp.get("ev") or ()
        times = [x[0] for x in ev]
        lo, hi = bisect.bisect_left(times, start), bisect.bisect_right(times, end)
        if lo >= hi:
            continue
        base = {"sid": sid, "short": sid[:8], "project": e.get("project")}
        steps, groups = sp.get("steps") or [], sp.get("groups") or []
        ends = [x[3] for x in steps if x[3]]
        sr = _row(**base, label=sp.get("goal") or "", span=(times[0], max([times[-1]] + ends)))
        step_rows, group_of = {}, {}
        for gi, g in enumerate(groups):
            for i in range(g[2], g[3] + 1):
                group_of[i] = gi
        intent_rows = {}
        for x in ev[lo:hi]:
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
        for f in FIELDS:
            total[f] += sr[f]
        total["calls"] += sr["calls"]
        total["sessions"] += 1
        srows.append(sr)
        xrows += step_rows.values()
        irows += intent_rows.values()

    def best(rows):
        rows = [_finish(r, total, start, end) for r in rows]
        rows.sort(key=lambda r: (-r["usd"], -r["cw"], r["sid"], r.get("i", 0)))
        return rows if limit is None else rows[:limit]
    out = {"start": start, "end": end, "start_iso": iso(start), "end_iso": iso(end),
           "share_of": "usd" if total["usd"] > 0 else "cw",
           "sessions": best(srows), "intents": best(irows), "steps": best(xrows)}
    total["tokens"] = int(round(sum(total[f] for f in ("input", "cache_write", "cache_read", "output"))))
    total["cw"], total["usd"] = round(total["cw"], 1), round(total["usd"], 6)
    out["total"] = total
    return out


def top_spenders(window="5h", now=None, root=None, limit=LIMIT, cache=None, jobs=None, index=None):
    """Build (incrementally) the cross-session index under `root` and rank the window ending at `now` (epoch s,
    default the current time). `index`: an already built xindex.XIndex. Adds "window" and "index" (build stats)."""
    import time
    from .xindex import XIndex
    name, seconds = window_seconds(window)
    if index is None:
        index = XIndex(root, cache)
        index.build(jobs=jobs)
    out = top(index.sessions, time.time() if now is None else float(now), seconds, limit)
    out.update(window=name, index=dict(index.stats))
    return out
