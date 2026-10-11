"""Self-contained HTML report: one file, inline CSS/JS/SVG, no network.

Charts are generated server-side as inline SVG; hover details use native <title> tooltips so the file
works offline without any script. The only JS is table sorting and opening anchored <details>.
"""
import html
import math
import os
import re
import statistics
from datetime import datetime

from . import pricing
from .analyze import BIG_BASH_TOKENS, CHARS_PER_TOKEN, INTENT_PROMPT_CHARS, INTENT_RULES, TTL_1H, TTL_5M, mcp_parts
from .cli import fmt_dur, fmt_tok
from .parse import parse_ts

CORPUS_DETAIL = 6      # session views in corpus mode
CORPUS_TOP = 30        # aggregated fixes shown in corpus mode
CORPUS_AUTO = 40       # report --corpus is automatic above this many sessions
DETAIL_CAP = 15        # sessions with a full session view in the cross-session report
TURN_ROWS_CAP = 300    # turn-table rows per session
OFFER_ROWS = 12        # rows per offered-vs-used table
SPIKE_ABS = 10_000     # new tokens in one call
SPIKE_REL = 3.0        # x median new tokens ...
SPIKE_MIN = 2_000      # ... but never below this
IDLE_MARK = TTL_5M     # gaps longer than this are marked on the timeline
MAX_TIMELINE_BARS = 400  # more calls than this are grouped into consecutive buckets (one bar each)
SPIKES_CAP = 25        # spikes explained per session view
FIX_CARDS_CAP = 30     # full fix cards per session view; the rest are one-line entries

# token-type colors (validated as a categorical set against the dark panel surface)
TOK = [("cache_read", "cache read", "#4f7fc4"), ("cache_write", "cache write", "#bf7f1c"),
       ("input", "fresh input", "#e0527a"), ("output", "output", "#2e9e74")]
TOK_COLOR = {k: c for k, _, c in TOK}
CAT = ["#3987e5", "#d95926", "#199e70", "#c98500", "#d55181", "#9085e9"]
OTHER = "#6e7681"
# causes inside one call are ranked parts of a whole: one hue (violet), light -> dark, so they never
# read as a token type
RANK = ["#c6bdf7", "#a497ee", "#8576df", "#6a5ac8", "#5446a8"]
MONO = 'ui-monospace,SFMono-Regular,Menlo,Consolas,"Liberation Mono",monospace'


def esc(s):
    return html.escape("" if s is None else str(s), quote=True)


def num(n, unit="tokens", est=False):
    n = int(n or 0)
    return '<span class="n" title="%s %s%s">%s%s</span>' % (
        format(n, ","), unit, " (estimate)" if est else "", "~" if est else "", fmt_tok(n))


def tick(v):
    """Axis tick label: like fmt_tok but without a trailing .0 (50k, not 50.0k)."""
    return re.sub(r"\.0(?=[kM]$)", "", fmt_tok(v))


def short(s, n):
    s = " ".join(str(s or "").split())
    return s if len(s) <= n else s[:n - 1] + "…"


def _local(iso):
    dt = parse_ts(iso) if isinstance(iso, str) else iso
    return dt.astimezone() if dt else None


def tz_label():
    now = datetime.now().astimezone()
    off = now.strftime("%z")
    off = "UTC%s:%s" % (off[:3], off[3:]) if off else "UTC"
    name = now.strftime("%Z")
    return "%s (%s)" % (name, off) if name and name not in ("UTC",) else off


def t_hms(iso):
    d = _local(iso)
    return d.strftime("%H:%M:%S") if d else ""


def t_full(iso):
    d = _local(iso)
    return d.strftime("%Y-%m-%d %H:%M") if d else "-"


def fmt_gap(s):
    return "" if s is None else fmt_dur(s)


def new_tokens(t):
    """'new' = everything except cache reads; t is a totals dict or a usage dict."""
    if "fresh_input" in t:
        return t["fresh_input"] + t["cache_write"] + t["output"]
    return t["input"] + t["cache_write"] + t["output"]


def metrics(t):
    """Honest headline numbers for a totals dict (analyze._sum_totals shape; older dicts without the pricing
    fields are filled in): new = uncached input + cache write, output, cache reads, cost-weighted, API $."""
    fi, cw, cr, out = t.get("fresh_input", 0), t.get("cache_write", 0), t.get("cache_read", 0), t.get("output", 0)
    cwt = t.get("cost_weighted")
    if cwt is None:
        cwt = pricing.cost_weighted_of(fi, cw, cr, out)
    return {"new": t.get("new", fi + cw), "output": out, "cache_read": cr, "cost_weighted": cwt,
            "usd": t.get("api_cost_usd"), "unpriced_calls": t.get("unpriced_calls", 0),
            "unpriced_models": list(t.get("unpriced_models") or []), "all_in": t.get("all_in", fi + cw + cr + out),
            "calls": t.get("calls", 0)}


def fmt_usd(v):
    if v is None:
        return "-"
    return "$%.2f" % v if v >= 0.01 or v == 0 else "$%.4f" % v


def unpriced_note(m):
    """'(3 calls unpriced: claude-test-1)' or ''."""
    if not m.get("unpriced_calls"):
        return ""
    names = m.get("unpriced_models") or []
    return "(%d call%s unpriced%s)" % (m["unpriced_calls"], "" if m["unpriced_calls"] == 1 else "s",
                                       ": " + ", ".join(names[:4]) + (", ..." if len(names) > 4 else "") if names else "")


def usd_text(m):
    """'$1.23 API-equivalent (2 calls unpriced: x)'; '$0.00 ...' when nothing is priced is reported as unpriced."""
    priced = m.get("calls", 0) - m.get("unpriced_calls", 0)
    if m.get("usd") is None or (m.get("calls") and priced <= 0):
        return "API-equivalent $ unknown " + unpriced_note(m) if m.get("unpriced_calls") else "API-equivalent $ unknown"
    return ("%s API-equivalent %s" % (fmt_usd(m["usd"]), unpriced_note(m))).strip()


def headline_text(t):
    """One plain-text line: '123k cost-weighted tokens, $1.23 API-equivalent; new 50k, output 3k, cache reads 1.2M
    (all-in 1.3M)'."""
    m = metrics(t)
    return "%s cost-weighted tokens, %s; new %s, output %s, cache reads %s (all-in %s)" % (
        fmt_tok(m["cost_weighted"]), usd_text(m), fmt_tok(m["new"]), fmt_tok(m["output"]), fmt_tok(m["cache_read"]),
        fmt_tok(m["all_in"]))


# the report must not contain links (self-contained, no network): show the price source without its scheme
PRICE_SRC = re.sub(r"^\w+://", "", pricing.PRICES_SOURCE)
COST_NOTE = ("cost-weighted tokens = %s (API price ratios relative to uncached input). API-equivalent $ = what the "
             "same calls would cost on the API (prices: %s); on a subscription this is not what you are billed."
             % (pricing.COST_WEIGHTED_FORMULA, PRICE_SRC))


def headline_kpis(t, calls_sub="", extra=()):
    """KPI cells: cost-weighted + API $ headline, then new / output / cache reads always as separate numbers,
    all-in demoted to a muted secondary cell."""
    m = metrics(t)
    note = unpriced_note(m)
    priced = m["usd"] is not None and not (m["calls"] and m["calls"] <= m["unpriced_calls"])
    cells = [
        kpi("cost-weighted tokens", num(m["cost_weighted"]), pricing.COST_WEIGHTED_FORMULA, title=COST_NOTE,
            cls="head"),
        kpi("API-equivalent", '<span class="n">%s</span>' % (fmt_usd(m["usd"]) if priced else "-"),
            esc(note) if note else "list prices, not your bill", title=COST_NOTE, cls="head"),
        kpi("new", num(m["new"]), "uncached input + cache write", TOK_COLOR["cache_write"]),
        kpi("output", num(m["output"]), "thinking %s" % fmt_tok(t.get("thinking", 0)), TOK_COLOR["output"]),
        kpi("cache reads", num(m["cache_read"]), "x0.1 in cost-weighted", TOK_COLOR["cache_read"]),
    ]
    cells += list(extra)
    cells.append(kpi("all-in", num(m["all_in"]), "plain sum incl. cache reads", cls="dim",
                     title="all-in = uncached input + cache write + cache read + output, unweighted"))
    return cells


def all_calls(a):
    return list(a["calls"]) + [c for s in a["subagents"] for c in s["call_records"]]


# ---------------------------------------------------------------- per-session derived data

def entries(a):
    """Main calls in order, each spawning call followed by its subagent's calls."""
    subs = {}
    for s in a["subagents"]:
        subs.setdefault(s["parent_call_index"], []).append(s)
    out = []

    def add(c, sub=None):
        u = c["usage"]
        attr = sorted((x for x in c.get("attribution") or [] if x["tokens"] > 0), key=lambda x: -x["tokens"])
        out.append({
            "c": c, "sub": sub, "u": u, "ts": c.get("timestamp"), "context": c["context"], "new": new_tokens(u),
            "ref": "#%d" % c["index"] if sub is None else "%s #%d" % (sub["type"] or "subagent", c["index"]),
            "attr": attr, "gap": c.get("gap_seconds"),
        })

    main_idx = {c["index"] for c in a["calls"]}
    for c in a["calls"]:
        add(c)
        for s in subs.get(c["index"], []):
            for sc in s["call_records"]:
                add(sc, s)
    for p, ss in subs.items():
        if p not in main_idx:
            for s in ss:
                for sc in s["call_records"]:
                    add(sc, s)
    news = [e["new"] for e in out]
    med = statistics.median(news) if news else 0
    for e in out:
        e["spike"] = e["new"] >= SPIKE_MIN and (e["new"] >= SPIKE_ABS or e["new"] > SPIKE_REL * med)
        top = e["attr"][0] if e["attr"] else None
        e["top"] = top
        # label: group causes with the same label (e.g. 2 firings of one hook) and prefer a named cause over
        # "other (unattributed)" / "Other (...)" leftovers; tokens are the sum of that cause's lines.
        groups = {}
        for a_ in e["attr"]:
            g = groups.setdefault(a_["label"], [0, 0])
            g[0] += a_["tokens"]
            g[1] += 1
        def _vague(lbl):
            l = lbl.lower()
            return "unattributed" in l or l.startswith("other")
        ranked = sorted(groups.items(), key=lambda kv: (_vague(kv[0]), -kv[1][0]))
        if ranked and ranked[0][1][0] > 0:
            lbl, (tok, n) = ranked[0]
            e["top_label"] = "+%s %s%s" % (fmt_tok(tok), short(lbl, 30), ((" (%d files)" if "instructions" in lbl.lower() else " (%dx)") % n) if n > 1 else "")
        else:
            e["top_label"] = "+" + fmt_tok(e["new"])
    return out


def top_drain(a):
    agg = {}
    for c in all_calls(a):
        for x in c.get("attribution") or []:
            if x["cause"] not in ("other", "assistant_output"):
                agg[x["label"]] = agg.get(x["label"], 0) + x["tokens"]
    if not agg:
        return None, 0
    k = max(agg, key=agg.get)
    return k, agg[k]


# ---------------------------------------------------------------- SVG helpers

def _nice(v):
    if v <= 0:
        return 1
    e = 10 ** math.floor(math.log10(v))
    for m in (1, 2, 2.5, 5, 10):
        if m * e >= v:
            return m * e
    return 10 * e


def _place(items, lo, hi, char_w=7.3, pad=8, max_rows=4):
    """items: list of (x_center, text). Returns list of (row or None, x_center_clamped)."""
    rows = []  # per row: list of (x0, x1)
    out = []
    for x, text in items:
        w = len(text) * char_w
        cx = min(max(x, lo + w / 2), hi - w / 2)
        x0, x1 = cx - w / 2 - pad / 2, cx + w / 2 + pad / 2
        placed = None
        for r, occ in enumerate(rows):
            if all(x1 <= a or x0 >= b for a, b in occ):
                occ.append((x0, x1))
                placed = r
                break
        if placed is None and len(rows) < max_rows:
            rows.append([(x0, x1)])
            placed = len(rows) - 1
        out.append((placed, cx))
    return out


def _bar_title(e):
    if e.get("title"):
        return e["title"]
    u = e["u"]
    lines = ["%s  %s" % (e["ref"], t_hms(e["ts"])),
             "cache read %s | cache write %s | fresh %s | output %s" % tuple(
                 format(u[k], ",") for k in ("cache_read", "cache_write", "input", "output")),
             "context %s | new %s" % (format(e["context"], ","), format(e["new"], ","))]
    for x in e["attr"][:4]:
        lines.append("  %s%s %s" % ("~" if x.get("est") else "", fmt_tok(x["tokens"]), x["label"]))
    tools = [t["name"] for t in e["c"]["tool_uses"]]
    if tools:
        lines.append("tools: " + ", ".join(tools))
    if e["c"].get("cache_miss"):
        lines.append("CACHE MISS (%s)" % e["c"]["cache_miss"]["reason"])
    if e["c"].get("compaction"):
        lines.append("COMPACTION")
    return "\n".join(lines)


def bucket_entries(ents, max_bars=MAX_TIMELINE_BARS):
    """Group consecutive entries into <= max_bars buckets (entries() shape, so timeline_svg can draw them):
    token types summed, context = max (of main calls when the bucket has any), markers kept (first cache miss /
    compaction, longest main gap), 'sub' only when every call is a subagent call, 'hatch' when any is."""
    n = len(ents)
    if n <= max_bars:
        return ents
    size = -(-n // max_bars)
    out = []
    for b, i in enumerate(range(0, n, size)):
        grp = ents[i:i + size]
        u = {}
        for e in grp:
            for k_, v in e["u"].items():
                if isinstance(v, (int, float)) and not isinstance(v, bool):
                    u[k_] = u.get(k_, 0) + v
        for k_ in ("input", "cache_write", "cache_read", "output"):
            u.setdefault(k_, 0)
        mains = [e for e in grp if e["sub"] is None]
        ctx_from = mains or grp
        miss = next((e["c"]["cache_miss"] for e in grp if e["c"].get("cache_miss")), None)
        comp = next((e["c"]["compaction"] for e in grp if e["c"].get("compaction")), None)
        gaps = [e["gap"] for e in mains if e["gap"] is not None]
        spikes = [e for e in grp if e["spike"]]
        attr = {}
        for e in grp:
            for x in e["attr"]:
                attr[x["label"]] = attr.get(x["label"], 0) + x["tokens"]
        top = sorted(attr.items(), key=lambda kv: -kv[1])[:4]
        new = sum(e["new"] for e in grp)
        lines = ["calls %s–%s (%d calls)  %s" % (grp[0]["ref"], grp[-1]["ref"], len(grp), t_hms(grp[0]["ts"])),
                 "cache read %s | cache write %s | fresh %s | output %s" % tuple(
                     format(u[k_], ",") for k_ in ("cache_read", "cache_write", "input", "output")),
                 "max context %s | new %s" % (format(max(e["context"] for e in ctx_from), ","), format(new, ","))]
        lines += ["  %s %s" % (fmt_tok(v), lbl) for lbl, v in top]
        if any(e["sub"] is not None for e in grp):
            lines.append("includes %d subagent calls" % sum(1 for e in grp if e["sub"] is not None))
        if miss:
            lines.append("CACHE MISS (%s)" % miss.get("reason"))
        if comp:
            lines.append("COMPACTION")
        first_main = mains[0]["c"]["index"] if mains else None
        out.append({
            "c": {"index": first_main if first_main is not None else grp[0]["c"]["index"], "tool_uses": [],
                  "cache_miss": miss, "compaction": comp, "tool_results": []},
            "sub": grp[0]["sub"] if not mains and all(e["sub"] is grp[0]["sub"] for e in grp) else None,
            "hatch": any(e["sub"] is not None for e in grp), "u": u, "ts": grp[0]["ts"],
            "context": max(e["context"] for e in ctx_from), "new": new,
            "ref": "%s–%s" % (grp[0]["ref"], grp[-1]["ref"]), "attr": [], "gap": max(gaps) if gaps else None,
            "spike": bool(spikes), "top": None,
            "top_label": max(spikes, key=lambda e: e["new"])["top_label"] if spikes else "+" + fmt_tok(new),
            "title": "\n".join(lines), "bucket": b, "bucket_calls": len(grp)})
    return out


def timeline_vmax(ents, mode="all"):
    keys = [k for k, _, _ in TOK if mode == "all" or k != "cache_read"]
    vmax = max([sum(e["u"][k] for k in keys) for e in ents] or [0]) or 1
    if mode == "all":
        vmax = max(vmax, max(e["context"] for e in ents))
    return vmax


def timeline_svg(ents, mode="all", W=1180, vmax=None, n_slots=None, ph=None):
    """mode 'all': cache read/write/fresh/output stacked + context line; mode 'new': excludes cache reads.

    vmax / n_slots force a shared y-scale / bar width, ph the plot height (used by compare)."""
    if not ents:
        return '<p class="muted">No API calls recorded.</p>'
    n_calls = len(ents)
    ents = bucket_entries(ents)
    n = len(ents)
    note = ""
    if n < n_calls:
        n_slots = min(n_slots, MAX_TIMELINE_BARS) if n_slots else None
        vmax = max(vmax, timeline_vmax(ents, mode)) if vmax else None
        note = ('<p class="muted small tlnote">%s calls grouped into %d bars (%d consecutive calls per bar; hover a '
                'bar for its range)</p>' % (format(n_calls, ","), n, ents[0]["bucket_calls"]))
    keys = [k for k, _, _ in TOK if mode == "all" or k != "cache_read"]
    L, R = 62, 14
    pw = W - L - R
    slot = pw / max(n, n_slots or 0)
    bw = max(1.0, min(slot * 0.72, 38))
    xc = [L + slot * (i + 0.5) for i in range(n)]

    # annotations above the plot
    spikes = [i for i, e in enumerate(ents) if e["spike"]]
    lab = _place([(xc[i], ents[i]["top_label"]) for i in spikes], L, W - R)
    rows = max([r + 1 for r, _ in lab if r is not None] or [0])
    top = 14 + rows * 17
    ph = ph or (250 if mode == "all" else 170)

    # event lane below the plot
    ev = []
    for i, e in enumerate(ents):
        c = e["c"]
        if c.get("compaction"):
            ev.append((i, "compaction", "#a371f7"))
        if c.get("cache_miss"):
            ev.append((i, "cache miss", "#f85149"))
        if e["sub"] is None and e["gap"] is not None and e["gap"] > IDLE_MARK:
            ev.append((i, "idle " + fmt_dur(e["gap"]), "#8b949e"))
    evl = _place([(xc[i], t) for i, t, _ in ev], L, W - R, char_w=6.8, max_rows=3)
    ev_rows = max([r + 1 for r, _ in evl if r is not None] or [0])

    y0 = top + ph
    y_ev = y0 + 8
    y_idx = y_ev + ev_rows * 15 + 15
    y_time = y_idx + 16
    y_sub = y_time + 12
    has_sub = any(e["sub"] for e in ents)
    H = y_sub + (26 if has_sub else 4)

    vmax = vmax or timeline_vmax(ents, mode)
    step = _nice(vmax / 4)
    ymax = step * math.ceil(vmax / step)

    def Y(v):
        return y0 - v / ymax * ph

    o = ['<svg class="chart" viewBox="0 0 %d %d" width="100%%" role="img">' % (W, H)]
    # subagent background bands
    i = 0
    groups = []
    while i < n:
        if ents[i]["sub"] is not None:
            j = i
            while j + 1 < n and ents[j + 1]["sub"] is ents[i]["sub"]:
                j += 1
            groups.append((i, j, ents[i]["sub"]))
            i = j + 1
        else:
            i += 1
    for i, j, s in groups:
        x0, x1 = L + slot * i, L + slot * (j + 1)
        o.append('<rect x="%.1f" y="%d" width="%.1f" height="%d" fill="#ffffff" fill-opacity="0.045"/>'
                 % (x0, top, x1 - x0, ph))
    # grid + y axis
    v = 0
    while v <= ymax + 1e-9:
        y = Y(v)
        o.append('<line x1="%d" x2="%d" y1="%.1f" y2="%.1f" class="grid"/>' % (L, W - R, y, y))
        o.append('<text x="%d" y="%.1f" class="axl" text-anchor="end">%s</text>' % (L - 8, y + 4, tick(v)))
        v += step
    # idle / compaction vertical markers
    for (i, t, col) in ev:
        if t.startswith("idle") or t == "compaction":
            x = L + slot * i
            o.append('<line x1="%.1f" x2="%.1f" y1="%d" y2="%d" stroke="%s" stroke-dasharray="4 3" '
                     'stroke-width="1.5"/>' % (x, x, top, y0 + 4, col))
    # bars
    stroke = slot >= 7
    for i, e in enumerate(ents):
        u = e["u"]
        x = xc[i] - bw / 2
        acc = 0
        o.append('<g class="bar"><title>%s</title>' % esc(_bar_title(e)))
        for k in keys:
            if u[k] <= 0:
                continue
            ya, yb = Y(acc), Y(acc + u[k])
            o.append('<rect x="%.1f" y="%.1f" width="%.1f" height="%.2f" fill="%s"%s/>' % (
                x, yb, bw, max(ya - yb, 0.6), TOK_COLOR[k], ' class="seg"' if stroke else ""))
            acc += u[k]
        if e.get("hatch", e["sub"] is not None) and acc:
            o.append('<rect x="%.1f" y="%.1f" width="%.1f" height="%.1f" fill="url(#ts-hatch)"/>'
                     % (x, Y(acc), bw, y0 - Y(acc)))
        o.append('<rect class="hit" x="%.1f" y="%d" width="%.1f" height="%d"/></g>' % (L + slot * i, top, slot, ph))
    # context step lines (main session solid, subagents dashed)
    if mode == "all":
        pts, sub_segs = [], []
        for i, e in enumerate(ents):
            x0, x1, y = L + slot * i, L + slot * (i + 1), Y(e["context"])
            if e["sub"] is None:
                pts += [(x0, y), (x1, y)]
            else:
                if pts:
                    pts.append((x1, pts[-1][1]))
                sub_segs.append((x0, x1, y))
        if pts:
            o.append('<polyline fill="none" stroke="#e6edf3" stroke-width="2" points="%s"/>'
                     % " ".join("%.1f,%.1f" % p for p in pts))
        for x0, x1, y in sub_segs:
            o.append('<line x1="%.1f" x2="%.1f" y1="%.1f" y2="%.1f" stroke="#e6edf3" stroke-width="2" '
                     'stroke-dasharray="3 3" stroke-opacity="0.75"/>' % (x0, x1, y, y))
    o.append('<line x1="%d" x2="%d" y1="%d" y2="%d" stroke="#484f58"/>' % (L, W - R, y0, y0))
    # spike annotations
    for (r, cx), i in zip(lab, spikes):
        if r is None:
            continue
        ty = top - 6 - r * 17
        e = ents[i]
        bar_top = Y(sum(e["u"][k] for k in keys))
        o.append('<line x1="%.1f" x2="%.1f" y1="%.1f" y2="%.1f" stroke="#8b949e" stroke-width="1" '
                 'stroke-dasharray="2 2"/>' % (xc[i], xc[i], ty + 4, bar_top - 2))
        o.append('<text x="%.1f" y="%.1f" class="ann" text-anchor="middle">%s</text>'
                 % (cx, ty, esc(e["top_label"])))
    # event lane labels
    for (r, cx), (i, t, col) in zip(evl, ev):
        if r is None:
            continue
        y = y_ev + 11 + r * 15
        o.append('<text x="%.1f" y="%.1f" class="evl" fill="%s" text-anchor="middle">%s</text>' % (cx, y, col, esc(t)))
        o.append('<line x1="%.1f" x2="%.1f" y1="%d" y2="%.1f" stroke="%s" stroke-width="2"/>'
                 % (xc[i], xc[i], y0, y0 + 5, col))
    # x labels: call index (main only) + local time
    k_idx = max(1, math.ceil(30 / slot))
    last_t = -10 ** 9
    for i, e in enumerate(ents):
        if e.get("bucket") is not None:
            if e["bucket"] % k_idx == 0:
                o.append('<text x="%.1f" y="%d" class="axl" text-anchor="middle">%d</text>'
                         % (xc[i], y_idx, e["c"]["index"]))
        elif e["sub"] is None and e["c"]["index"] % k_idx == 0:
            o.append('<text x="%.1f" y="%d" class="axl" text-anchor="middle">%d</text>' % (xc[i], y_idx, e["c"]["index"]))
        if e["ts"] and xc[i] - last_t >= 78:
            o.append('<text x="%.1f" y="%d" class="axt" text-anchor="middle">%s</text>' % (xc[i], y_time, t_hms(e["ts"])))
            last_t = xc[i]
    o.append('<text x="%d" y="%d" class="axt" text-anchor="end">call #</text>' % (L - 8, y_idx))
    # subagent brackets
    for i, j, s in groups:
        x0, x1 = L + slot * i + 1, L + slot * (j + 1) - 1
        yb = y_sub + 2
        o.append('<path d="M%.1f %d V%d H%.1f V%d" fill="none" stroke="#a5b4c8" stroke-width="1.2"/>'
                 % (x0, yb - 4, yb, x1, yb - 4))
        txt = "%s subagent · %d calls" % (s["type"] or "", s["calls"])
        if len(txt) * 7 > (x1 - x0) + 120:
            txt = "%s · %d" % (short(s["type"] or "sub", 12), s["calls"])
        o.append('<text x="%.1f" y="%d" class="subl" text-anchor="middle">%s</text>'
                 % (min(max((x0 + x1) / 2, L + len(txt) * 3.6), W - R - len(txt) * 3.6), yb + 15, esc(txt)))
    o.append("</svg>")
    return "".join(o) + note


def columns_svg(buckets, series, W=380, H=250):
    """Stacked columns. buckets: [(label, title, {key: value})], series: [(key, color)]."""
    if not buckets:
        return '<p class="muted">No data.</p>'
    L, R, T, B = 50, 8, 12, 38
    pw, ph = W - L - R, H - T - B
    n = len(buckets)
    slot = pw / n
    bw = max(2.0, min(slot * 0.7, 46))
    vmax = max(sum(v.values()) for _, _, v in buckets) or 1
    step = _nice(vmax / 4)
    ymax = step * math.ceil(vmax / step)
    o = ['<svg class="chart" viewBox="0 0 %d %d" width="100%%" role="img">' % (W, H)]
    v = 0
    while v <= ymax + 1e-9:
        y = T + ph - v / ymax * ph
        o.append('<line x1="%d" x2="%d" y1="%.1f" y2="%.1f" class="grid"/>' % (L, W - R, y, y))
        o.append('<text x="%d" y="%.1f" class="axl" text-anchor="end">%s</text>' % (L - 6, y + 4, tick(v)))
        v += step
    k_lab = max(1, math.ceil(50 / slot))
    for i, (label, title, vals) in enumerate(buckets):
        x = L + slot * i + (slot - bw) / 2
        acc = 0
        o.append('<g class="bar"><title>%s</title>' % esc(title + "\n" + "\n".join(
            "%s: %s" % (k, format(vals[k], ",")) for k, _ in series if vals.get(k))))
        for k, col in series:
            val = vals.get(k, 0)
            if val <= 0:
                continue
            ya = T + ph - acc / ymax * ph
            yb = T + ph - (acc + val) / ymax * ph
            o.append('<rect x="%.1f" y="%.1f" width="%.1f" height="%.2f" fill="%s" class="seg"/>'
                     % (x, yb, bw, max(ya - yb, 0.6), col))
            acc += val
        o.append('<rect class="hit" x="%.1f" y="%d" width="%.1f" height="%d"/></g>' % (L + slot * i, T, slot, ph))
        if i % k_lab == 0:
            o.append('<text x="%.1f" y="%d" class="axl" text-anchor="middle">%s</text>'
                     % (L + slot * (i + 0.5), T + ph + 18, esc(label)))
    o.append('<line x1="%d" x2="%d" y1="%d" y2="%d" stroke="#484f58"/>' % (L, W - R, T + ph, T + ph))
    o.append("</svg>")
    return "".join(o)


def hbar(segs, total=None, title=""):
    """HTML horizontal stacked bar. segs: [(value, color, title)]; scaled to `total` (default: sum)."""
    total = total or sum(v for v, _, _ in segs) or 1
    parts = "".join('<span style="width:%.3f%%;background:%s" title="%s"></span>' % (100.0 * v / total, c, esc(t))
                    for v, c, t in segs if v > 0)
    return '<div class="hbar" title="%s">%s</div>' % (esc(title), parts)


def legend(extra=True):
    items = ['<span><i class="sw" style="background:%s"></i>%s</span>' % (c, l) for _, l, c in TOK]
    if extra:
        items += ['<span><i class="ln"></i>context size</span>',
                  '<span><i class="sw hatch"></i>subagent call</span>',
                  '<span><i class="mk" style="background:#f85149"></i>cache miss</span>',
                  '<span><i class="mk" style="background:#a371f7"></i>compaction</span>',
                  '<span><i class="mk" style="background:#8b949e"></i>idle gap &gt; %s</span>' % fmt_dur(IDLE_MARK)]
    return '<div class="legend">%s</div>' % "".join(items)


def table(headers, rows, cls="", sortable=False):
    """headers: [(label, is_num)], rows: list of lists of (html, sort_value or None) or (html, sort, row_cls)."""
    h = "".join('<th class="%s">%s</th>' % ("num" if isnum else "", lbl) for lbl, isnum in headers)
    body = []
    for r in rows:
        rc = ""
        if isinstance(r, tuple):
            r, rc = r
        tds = []
        for (lbl, isnum), cell in zip(headers, r):
            if isinstance(cell, tuple):
                hv, sv = cell
            else:
                hv, sv = cell, None
            tds.append('<td class="%s"%s>%s</td>' % ("num" if isnum else "",
                                                       ' data-v="%s"' % esc(sv) if sv is not None else "", hv))
        body.append('<tr class="%s">%s</tr>' % (rc, "".join(tds)))
    return '<div class="tscroll"><table class="%s%s"><thead><tr>%s</tr></thead><tbody>%s</tbody></table></div>' % (
        cls, " sortable" if sortable else "", h, "".join(body))


# ---------------------------------------------------------------- session view sections

def kpi(label, value, sub="", color=None, title="", cls=""):
    sw = '<i class="sw" style="background:%s"></i>' % color if color else ""
    return '<div class="kpi%s" title="%s"><div class="l">%s%s</div><div class="v">%s</div><div class="s">%s</div></div>' % (
        " " + cls if cls else "", esc(title), sw, label, value, sub)


def session_header(a):
    it = a["intent"]
    hint = ' <span class="muted">(matched “%s”)</span>' % esc(it.get("match")) if it.get("match") else ""
    prompt = " ".join((a["first_prompt"] or "").split())
    return (
        '<div class="shead"><div class="sid"><span class="mono">%s</span> <span class="pill">%s</span> '
        '<span class="pill intent">%s</span>%s</div>'
        '<div class="meta">%s → %s &middot; %s &middot; branch <span class="mono">%s</span> &middot; %s</div>'
        '<div class="prompt" title="%s">“%s”</div></div>'
    ) % (esc(a["session_id"]), esc(a["project"]), esc(it["intent"]), hint,
         t_full(a["start"]), t_hms(a["end"]), fmt_dur(a["duration_seconds"]), esc(a["git_branch"] or "-"),
         esc(", ".join(a["models"]) or "-"), esc(prompt[:2000]), esc(short(prompt, 220)))


def session_kpis(a):
    t, m, s = a["totals"]["all"], a["totals"]["main"], a["totals"]["subagents"]
    cells = headline_kpis(t, extra=[
        kpi("cache writes", num(t["cache_write"]), "1h: %s &middot; 5m: %s" % (fmt_tok(t.get("cache_write_1h", 0)),
                                                                           fmt_tok(t.get("cache_write_5m", 0))),
            TOK_COLOR["cache_write"], title="part of new"),
        kpi("API calls", '<span class="n">%d</span>' % t["calls"], "%d main + %d subagent" % (m["calls"], s["calls"])),
        kpi("peak context", num(a["peak_context"]), "main session"),
        kpi("duration", '<span class="n">%s</span>' % fmt_dur(a["duration_seconds"]), "first → last event"),
    ])
    rep = a.get("reported")
    if rep and rep.get("total_cost_usd") is not None:
        cells.append(kpi("reported cost", '<span class="n">$%.2f</span>' % float(rep["total_cost_usd"]),
                         "as reported by Claude Code", cls="dim", title="API-equivalent cost, as reported by Claude "
                         "Code (cost-state). On a subscription this is not what you are billed."))
    return '<div class="kpis">%s</div>%s' % ("".join(cells), cost_note())


def cost_note():
    return '<p class="muted small cnote">%s</p>' % esc(COST_NOTE)


def spikes_section(ents):
    sp = [e for e in ents if e["spike"]]
    if not sp:
        return '<p class="muted">No spikes: no call added more than %s new tokens or %.0f× the median.</p>' % (
            fmt_tok(SPIKE_ABS), SPIKE_REL)
    out = []
    ranked = sorted(sp, key=lambda e: -e["new"])
    for e in ranked[:SPIKES_CAP]:
        attr = e["attr"]
        top5 = attr[:5]
        rest = sum(x["tokens"] for x in attr[5:])
        segs = [(x["tokens"], RANK[i], "%s %s" % (x["label"], format(x["tokens"], ","))) for i, x in enumerate(top5)]
        if rest:
            segs.append((rest, OTHER, "other causes %s" % format(rest, ",")))
        lines = []
        res_by_tu = {r.get("tool_use_id"): r for r in e["c"].get("tool_results") or []}
        for i, x in enumerate(top5):
            r = res_by_tu.get(x.get("tool_use_id"))
            size = ""
            if r:
                size = "%s chars in context" % format(r["chars"], ",")
                if r.get("persisted") and r.get("full_chars"):
                    size += " (full output %s chars persisted to disk, only a preview entered context)" % format(
                        r["full_chars"], ",")
                if r.get("is_error"):
                    size += ", error"
                if x.get("scaled_to_measured"):
                    size += "; chars/4 ≈ %s, scaled to the measured cache write" % fmt_tok(r["est_tokens"])
            elif x.get("detail"):
                size = short(x["detail"], 110)
                if x.get("scaled_to_measured") and x["cause"] == "hook":
                    size += "; scaled to the measured cache write"
            lines.append('<li><i class="sw" style="background:%s"></i><span class="n cause-t">%s</span> %s '
                         '<span class="muted">%s</span></li>' % (
                             RANK[i], num(x["tokens"], est=x.get("est")), esc(short(x["label"], 90)), esc(size)))
        if rest:
            lines.append('<li><i class="sw" style="background:%s"></i><span class="n cause-t">%s</span> '
                         '%d smaller causes</li>' % (OTHER, num(rest), len(attr) - 5))
        if e["c"]["index"] == 0 and e["sub"] is None:
            lines.append('<li class="muted">first call: the whole context is new to this session (system prompt, '
                         'tools, listings), even if part of it was a cache hit from an earlier session.</li>')
        out.append(
            '<div class="spike"><div class="sph"><span class="ref mono">%s</span> <span class="muted">%s</span> '
            '<span class="big">+%s new</span> <span class="muted">context %s</span></div>%s<ul class="causes">%s</ul></div>'
            % (esc(e["ref"]), t_hms(e["ts"]), num(e["new"]), num(e["context"]), hbar(segs), "".join(lines)))
    if len(ranked) > SPIKES_CAP:
        rest = ranked[SPIKES_CAP:]
        out.append('<p class="muted">+%d more spikes (%s new tokens in total), the largest %d are shown; see the '
                   'Turns table.</p>' % (len(rest), fmt_tok(sum(e["new"] for e in rest)), SPIKES_CAP))
    return "".join(out)


def turn_table(ents):
    rows = []
    for e in ents[:TURN_ROWS_CAP]:
        c = e["c"]
        tools = {}
        for t in c["tool_uses"]:
            tools[t["name"]] = tools.get(t["name"], 0) + 1
        ts = ", ".join("%s%s" % (k, "×%d" % v if v > 1 else "") for k, v in tools.items())
        flags = ""
        if c.get("cache_miss"):
            flags += ' <span class="tag bad">cache miss</span>'
        if c.get("compaction"):
            flags += ' <span class="tag vio">compaction</span>'
        top = e["top"]
        cause = ("%s %s" % (num(top["tokens"], est=top.get("est")), esc(short(top["label"], 60)))) if top else ""
        rows.append(([
            ('<span class="mono">%s</span>' % esc(e["ref"]), None),
            ('<span class="mono">%s</span>' % t_hms(e["ts"]), e["ts"]),
            ('<span class="%s">%s</span>' % ("warn" if (e["gap"] or 0) > IDLE_MARK else "", fmt_gap(e["gap"])),
             e["gap"] or 0),
            (num(e["context"]), e["context"]),
            (num(e["new"]) if not e["spike"] else '<b class="hot">%s</b>' % num(e["new"]), e["new"]),
            (esc(ts), None),
            (cause + flags, None),
        ], "sub" if e["sub"] else ""))
    note = ""
    if len(ents) > TURN_ROWS_CAP:
        note = '<p class="muted">Showing the first %d of %d calls.</p>' % (TURN_ROWS_CAP, len(ents))
    return table([("call", False), ("time", False), ("gap", True), ("context", True), ("new", True),
                  ("tools called", False), ("top cause", False)], rows, "compact", sortable=True) + note


def _bars_list(items, color="#8b949e"):
    """items: (label_html, value, right_html, title)."""
    if not items:
        return ""
    vmax = max(v for _, v, _, _ in items) or 1
    return '<div class="blist">%s</div>' % "".join(
        '<div class="brow" title="%s"><div class="bl">%s</div><div class="bt"><span style="width:%.2f%%;background:%s">'
        '</span></div><div class="br">%s</div></div>' % (esc(t), l, 100.0 * v / vmax, color, r) for l, v, r, t in items)


def breakdown_panels(a):
    p = []
    tools = sorted(a["per_tool"].items(), key=lambda kv: -kv[1]["result_tokens_attributed"])
    p.append(('By tool <span class="muted">(tokens attributed to tool results)</span>', _bars_list([
        ('<span class="mono">%s</span>' % esc(short(k, 34)), v["result_tokens_attributed"],
         '%s <span class="muted">%d call%s%s</span>' % (num(v["result_tokens_attributed"]), v["calls"],
                                                       "" if v["calls"] == 1 else "s",
                                                       ", %d err" % v["errors"] if v["errors"] else ""),
         "%s: %d calls, chars/4 estimate %s, attributed %s" % (k, v["calls"], format(v["result_tokens_est"], ","),
                                                               format(v["result_tokens_attributed"], ",")))
        for k, v in tools]) or '<p class="muted">No tool calls.</p>'))

    files = sorted(a["per_file"].items(), key=lambda kv: -kv[1]["result_tokens_attributed"])[:25]
    frows = []
    for path, f in files:
        rep = ' <span class="tag warn">%d× repeated</span>' % f["repeated_reads"] if f["repeated_reads"] else ""
        err = ' <span class="tag bad">error</span>' if f["errors"] else ""
        frows.append([('<span class="mono path" title="%s">%s</span>%s%s' % (esc(path), esc(short(path, 46)), rep, err), path),
                      ('<span class="%s">%d</span>' % ("warn" if f["reads"] > 1 else "", f["reads"]), f["reads"]),
                      (num(f["result_tokens_attributed"]), f["result_tokens_attributed"]),
                      (num(f["result_tokens_est"], est=True), f["result_tokens_est"])])
    p.append(("By file (Read)", table([("file", False), ("reads", True), ("attributed", True), ("chars/4", True)],
                                      frows, "compact", sortable=True) if frows else
              '<p class="muted">No files were read.</p>'))

    bb = a["big_bash_outputs"][:15]
    brows = [[('<code title="%s">%s</code>' % (esc(b["command"]), esc(short(b["command"], 60))), None),
              (num(b["chars_in_context"], "chars"), b["chars_in_context"]),
              (num(b["full_chars"], "chars") if b["persisted"] and b["full_chars"] else "-", b["full_chars"] or 0),
              (num(b["attributed_tokens"]), b["attributed_tokens"])] for b in bb]
    p.append(('Big Bash outputs <span class="muted">(&ge; %s tokens or persisted)</span>' % fmt_tok(BIG_BASH_TOKENS),
              table([("command", False), ("in context", True), ("full (persisted)", True), ("attributed", True)],
                    brows, "compact") if brows else '<p class="muted">None.</p>'))

    mrows = [[('<span class="mono">%s</span>' % esc(s), s), ('%d' % v["calls"], v["calls"]),
              (esc(", ".join("%s×%d" % kv for kv in sorted(v["tools"].items(), key=lambda kv: -kv[1]))), None),
              (num(v["result_tokens_attributed"]), v["result_tokens_attributed"])]
             for s, v in sorted(a["per_mcp_server"].items(), key=lambda kv: -kv[1]["result_tokens_attributed"])]
    p.append(("MCP servers (called)", table([("server", False), ("calls", True), ("tools", False), ("attributed", True)],
                                            mrows, "compact") if mrows else '<p class="muted">No MCP tools were called.</p>'))

    hrows = [[('<span class="mono">%s</span>' % esc(h["hook"]), h["hook"]),
              ('<code title="%s">%s</code>' % (esc("\n".join(h["commands"])), esc(short(", ".join(os.path.basename(c.strip('"\'')) for c in h["commands"]), 22))),
               None),
              ("%d" % h["fires"], h["fires"]),
              (num(h["attributed_tokens"], est=True), h["attributed_tokens"]),
              (num(h["carry_tokens"], est=True), h["carry_tokens"])]
             for h in sorted((a.get("hooks") or {}).values(), key=lambda h: -h["attributed_tokens"])]
    p.append(('Hooks <span class="muted">(hook output that entered context; carry = re-read by later calls)</span>',
              table([("hook", False), ("command", False), ("fires", True), ("attributed", True),
                     ("carry", True)], hrows, "compact", sortable=True)
              if hrows else '<p class="muted">No hook output entered context.</p>'))

    srows = [[('<span class="mono">%s</span>' % esc(s["type"] or "?"), None), (esc(short(s["description"], 50)), None),
              ("%d" % s["calls"], s["calls"]), (num(s["tokens"]["all_in"]), s["tokens"]["all_in"]),
              (num(new_tokens(s["tokens"])), new_tokens(s["tokens"])),
              ("#%s" % s["parent_call_index"] if s["parent_call_index"] is not None else "-", None)]
             for s in a["subagents"]]
    p.append(("Subagents", table([("type", False), ("description", False), ("calls", True), ("all-in", True),
                                  ("new", True), ("spawned by", True)], srows, "compact")
              if srows else '<p class="muted">No subagents.</p>'))
    return '<div class="grid2">%s</div>' % "".join(
        '<div class="panel"><h3>%s</h3>%s</div>' % (h, b) for h, b in p)


def overhead_section(a):
    """First-call components + instruction files (CLAUDE.md, its @-imports) and CLAUDE.md `## ` sections."""
    c0 = a["calls"][0] if a["calls"] else None
    comps = sorted([x for x in (c0 or {}).get("attribution") or [] if x["cause"] == "system_overhead"],
                   key=lambda x: -x["tokens"])
    p = []
    p.append(('First call <span class="muted">context %s, est. by component</span>' % fmt_tok(c0["context"] if c0 else 0),
              _bars_list([('<span class="mono">%s</span>' % esc(short(
                  "instructions: " + os.path.basename(x["detail"]) if x["label"] == "CLAUDE.md / instructions"
                  and x.get("detail") else x["label"], 34)), x["tokens"],
                           num(x["tokens"], est=x.get("est")), "%s: %s tokens%s" % (
                               x["label"], format(x["tokens"], ","), " (%s)" % x["detail"] if x.get("detail") else ""))
                          for x in comps], color="#6a5ac8") or '<p class="muted">No calls.</p>'))
    ins = a.get("instructions")
    n = a["totals"]["main"]["calls"]
    if not ins:
        p.append(("Instruction files", '<p class="muted">No CLAUDE.md / instructions attachment in this transcript.</p>'))
    else:
        frows = []
        for e in sorted(ins["files"], key=lambda e: -e["est_tokens"]):
            imp = ('<span class="tag warn" title="%s">@-imported by %s</span>' % (esc(e["import_ref"]), esc(e["imported_by"]))
                   if e["imported_by"] else "")
            frows.append([('<span class="mono path" title="%s">%s</span> %s' % (esc(e["path"]), esc(short(e["rel"], 40)), imp),
                           e["rel"]),
                          (num(e["est_tokens"], est=True), e["est_tokens"]),
                          (num(e["est_tokens"] * n, est=True), e["est_tokens"] * n)])
        body = table([("file", False), ("per call", True), ("× %d calls" % n, True)], frows, "compact")
        secs = sorted(ins["sections"], key=lambda x: -x["est_tokens"])
        if secs:
            body += '<h3 class="sub">CLAUDE.md sections</h3>' + _bars_list([
                ('<span class="mono">%s</span>' % esc(short("## " + x["heading"], 34)), x["est_tokens"],
                 num(x["est_tokens"], est=True), "%s ## %s: %s chars" % (x["file"], x["heading"], format(x["chars"], ",")))
                for x in secs[:10]])
        p.append(('Instruction files <span class="muted">~%s tokens in every call\'s prefix</span>' % fmt_tok(
            ins["total_est_tokens"]), body))
    return '<div class="grid2">%s</div>' % "".join('<div class="panel"><h3>%s</h3>%s</div>' % (h, b) for h, b in p)


def offered_section(a):
    o = a["offered_vs_used"]
    if not o.get("available"):
        return ('<p class="muted">This transcript doesn’t record what was offered (no deferred-tool, skill, '
                'agent or MCP-instruction listings), so offered vs used can’t be computed.</p>')
    n = a["totals"]["main"]["calls"]
    out = ['<p class="muted">Listings sit in the cached prompt prefix and are re-read on every call '
           '(%d main calls). Sizes are chars/4 estimates of the listing text.</p><div class="stack">' % n]
    for key, title in (("mcp_servers", "MCP servers"), ("skills", "Skills"), ("subagent_types", "Subagent types")):
        items = sorted(o.get(key) or [], key=lambda e: (not e["used"], -e["est_tokens"]))
        used = [e for e in items if e["used"]]
        rows = []
        for e in items[:OFFER_ROWS]:
            extra = ""
            if key == "mcp_servers" and e.get("tools_offered"):
                extra = ' <span class="muted">%d tools</span>' % len(e["tools_offered"])
            rows.append([('<span class="mono nm" title="%s">%s</span>%s' % (
                esc(e.get("display") or e["name"]), esc(short(e.get("display") or e["name"], 24)), extra), None),
                         ('<span class="tag ok">used</span>' if e["used"] else '<span class="muted">–</span>', None),
                         (num(e["est_tokens"], est=True), e["est_tokens"]),
                         (num(e["per_session_reread_est"], est=True), e["per_session_reread_est"])])
        more = ('<p class="muted">+%d more, ~%s/call together</p>' % (
            len(items) - OFFER_ROWS, fmt_tok(sum(e["est_tokens"] for e in items[OFFER_ROWS:])))
            if len(items) > OFFER_ROWS else "")
        if key == "mcp_servers" and o.get("mcp_deferred") and any(e.get("names_est_tokens") for e in items):
            unused = [e for e in items if not e["used"]]
            names = sum(e.get("names_est_tokens", 0) for e in (unused or items))
            more += ('<p class="muted note1">Claude Code defers MCP tool schemas: only names (~%s tokens%s) sit in '
                     'context until a tool is searched/loaded, so unused servers are cheap here.</p>'
                     % (fmt_tok(names), " across the %d unused server%s" % (len(unused), "" if len(unused) == 1 else "s")
                        if unused else ""))
        out.append('<div class="panel"><h3>%s <span class="muted">offered %d &middot; used %d &middot; unused ~%s/call'
                   '</span></h3>%s%s</div>' % (title, len(items), len(used), fmt_tok(o.get(key + "_unused_est_tokens", 0)),
                                               table([("name", False), ("used", False), ("per call", True),
                                                      ("× calls", True)], rows, "compact")
                                               if rows else '<p class="muted">none offered</p>', more))
    out.append("</div>")
    return "".join(out)


SEV_LABEL = {"high": "high", "med": "medium", "low": "low"}


def fix_anchor(fx_id):
    return "fix-" + re.sub(r"[^A-Za-z0-9_-]+", "-", fx_id)[:80]


def fix_card(fx):
    if not isinstance(fx, dict):
        return '<div class="fix"><div class="fxt">%s</div></div>' % esc(fx)
    from .fixes import cw_of, savings_headline
    alli, new = fx.get("savings_all_in_tokens", 0), fx.get("savings_new_tokens", 0)
    if alli or new:
        cw = cw_of(fx)
        usd = fx.get("savings_usd")
        usd_html = ('<div class="n2"><span class="n">%s%s</span> API-equivalent</div>'
                    % ("≥" if fx.get("savings_usd_partial") else "", fmt_usd(usd))) if usd is not None else \
            '<div class="n2 muted">$ unknown (unpriced model)</div>'
        sav = ('<div class="fxs" title="%s (estimate). Cost-weighted = new x%g (cache write) + carry x%g (cache read)">'
               '<div class="big n">~%s</div><div class="muted">cost-weighted</div>'
               '<div class="n2"><span class="n">~%s</span> new tokens</div>%s'
               '<div class="n2 muted small">all-in incl. carry ~%s</div></div>'
               % (esc(savings_headline(fx)), pricing.W_CACHE_WRITE, pricing.W_CACHE_READ, fmt_tok(cw), fmt_tok(new),
                  usd_html, fmt_tok(alli)))
    else:
        sav = ('<div class="fxs"><div class="big n muted">–</div><div class="muted">%s</div></div>'
               % ("counted in a related fix" if fx.get("overlaps") else "informational"))
    ev = "".join("<li>%s</li>" % esc(e) for e in fx.get("evidence") or [])
    sess = fx.get("sessions") or []
    from .fixes import goal_line
    il = goal_line(fx)
    lands = '<div class="fxi" title="shares of the goal\'s new tokens (cache reads excluded)">%s</div>' % esc(il) \
        if il else ""
    return (
        '<div class="fix sev-%s" id="%s">%s<div class="fxm"><div class="fxh"><span class="sev %s">%s</span> '
        '<span class="rule mono">%s</span>%s</div><div class="fxt">%s</div>%s'
        '<div class="fxf mono" title="how the savings were computed">%s</div>'
        '<div class="fxa">%s</div><div class="code"><button class="copy" type="button" title="copy">copy</button>'
        '<pre>%s</pre></div>'
        '<details class="fxe"><summary>evidence (%d)</summary><ul>%s</ul><p class="muted">%s</p></details></div></div>'
    ) % (esc(fx["severity"]), esc(fix_anchor(fx["id"])), sav, esc(fx["severity"]),
         SEV_LABEL.get(fx["severity"], fx["severity"]), esc(fx["rule"]),
         ' <span class="muted">%d sessions</span>' % len(sess) if len(sess) > 1 else "",
         esc(fx["title"]), lands, esc(fx["savings_formula"]), esc(fx["action"]), esc(fx["action_code"]),
         len(fx.get("evidence") or []), ev, esc(fx.get("confidence")))


def fix_line(fx):
    """One-line entry (keeps the anchor) for fixes beyond the card cap."""
    from .fixes import savings_headline
    if not isinstance(fx, dict):
        return "<li>%s</li>" % esc(fx)
    return '<li id="%s"><span class="sev %s">%s</span> %s <span class="muted n">%s</span></li>' % (
        esc(fix_anchor(fx["id"])), esc(fx["severity"]), SEV_LABEL.get(fx["severity"], fx["severity"]),
        esc(fx["title"]), esc(savings_headline(fx)) if fx.get("savings_new_tokens") or fx.get("savings_all_in_tokens")
        else "")


def fixes_section(fixes, understand=None, cap=None):
    """Fix cards; with understand() data (apply_fixes run) a 'Fixes by goal' matrix comes first."""
    if not fixes:
        return '<p class="muted">No fix rules triggered for this session.</p>'
    dicts = [f for f in fixes if isinstance(f, dict)]
    head = ""
    if dicts:
        n = sum(1 for f in dicts if f["severity"] == "high")
        head = ('<p class="muted">%d suggestion%s (%d high), sorted by estimated cost-weighted savings. <b>new</b> = '
                'tokens that would not have entered context; <b>cost-weighted</b> = new x%g (written once as cache) + '
                'carry x%g, where the carry is those tokens re-read as cache reads by every later call until '
                'compaction (<b>all-in</b> = new + carry, unweighted). $ = API-equivalent at the session model\'s '
                'list price. Savings of different fixes can overlap.</p>'
                % (len(dicts), "" if len(dicts) == 1 else "s", n, pricing.W_CACHE_WRITE, pricing.W_CACHE_READ))
    if understand is not None:
        from .understand_html import fixes_by_goal
        head += fixes_by_goal(understand, fixes)
    shown, rest = (fixes, []) if cap is None else (fixes[:cap], fixes[cap:])
    more = ""
    if rest:
        more = ('<details class="fxmore"><summary>+%d more fixes (smaller estimated savings)</summary>'
                '<ul class="fl">%s</ul></details>' % (len(rest), "".join(fix_line(f) for f in rest)))
    return head + '<div class="fixes">%s</div>%s' % ("".join(fix_card(f) for f in shown), more)


def session_view(a, fixes=None, anchor="", fixes_top=False, understand=None):
    ents = entries(a)
    sec = [session_header(a), session_kpis(a)]
    fx = '<div class="panel"><h2>Fixes</h2>%s</div>' % fixes_section(fixes, understand, cap=FIX_CARDS_CAP)
    if fixes_top:
        sec.append(fx)
    sec.append('<div class="panel"><h2>Timeline <span class="muted">one bar per API call, stacked by token type; '
               'line = context size</span></h2>%s%s</div>' % (legend(), timeline_svg(ents, "all")))
    sec.append('<div class="panel"><h2>New tokens per call <span class="muted">(fresh + cache write + output; '
               'cache reads excluded so spikes stand out)</span></h2>%s</div>' % timeline_svg(ents, "new"))
    sec.append('<div class="panel"><h2>Spikes explained <span class="muted">(calls adding &gt; %s new tokens or '
               '&gt; %.0f× the median; ~ = chars/4 estimate)</span></h2>%s</div>'
               % (fmt_tok(SPIKE_ABS), SPIKE_REL, spikes_section(ents)))
    sec.append('<div class="panel"><h2>Turns</h2>%s</div>' % turn_table(ents))
    sec.append('<h2 class="sect">Breakdown</h2>' + breakdown_panels(a))
    sec.append('<h2 class="sect">System overhead</h2>' + overhead_section(a))
    sec.append('<h2 class="sect">Offered vs used</h2>' + offered_section(a))
    if not fixes_top:
        sec.append(fx)
    return '<section class="session" id="%s">%s</section>' % (esc(anchor), "".join(sec))


# ---------------------------------------------------------------- cross-session

def _sum_totals(analyses):
    """Sum of a["totals"]["all"] over analyses: numbers add, lists (unpriced_models) are unioned; None skipped."""
    t = {k: 0 for k in ("fresh_input", "cache_write", "cache_read", "output", "all_in", "calls", "thinking")}
    for a in analyses:
        for k, v in a["totals"]["all"].items():
            if isinstance(v, bool) or v is None:
                continue
            if isinstance(v, (list, tuple, set)):
                t[k] = sorted(set(t.get(k) or []) | set(v))
            elif isinstance(v, (int, float)):
                t[k] = t.get(k, 0) + v
    return t


def request_sources(analyses):
    out = {}
    for a in analyses:
        for k, v in (a.get("request_sources") or {}).items():
            out[k] = out.get(k, 0) + (v or 0)
    return out


def workflow_counts(analyses):
    """(workflows, started agents, failed) over all analyses, or None if none has workflows."""
    wfs = [w for a in analyses for w in a.get("workflows") or []]
    if not wfs:
        return None
    n = lambda w, k: len(w.get(k) or []) if isinstance(w.get(k), (list, tuple, set, dict)) else int(w.get(k) or 0)
    return len(wfs), sum(n(w, "started") for w in wfs), sum(n(w, "failed") for w in wfs)


SOURCE_ORDER = ("human", "notification", "hook", "subagent", "headless")


def sources_line(analyses):
    """Small panel: request counts per source, plus workflow counts when present ('' if neither)."""
    src = request_sources(analyses)
    wf = workflow_counts(analyses)
    parts = []
    if src:
        keys = [k for k in SOURCE_ORDER if k in src] + sorted(k for k in src if k not in SOURCE_ORDER)
        parts.append('<span class="srcl"><b>Request sources</b> %s</span>' % " &middot; ".join(
            '%s <span class="n">%d</span>' % (esc(k), src[k]) for k in keys))
    if wf:
        parts.append('<span class="srcl"><b>Workflows</b> <span class="n">%d</span> &middot; started agents '
                     '<span class="n">%d</span> &middot; failed <span class="n">%d</span></span>' % wf)
    if not parts:
        return ""
    return ('<div class="panel srcs" title="who sent each request: a person, a notification, a hook, a subagent '
            'or a headless run">%s</div>' % " &nbsp;&nbsp; ".join(parts))


def cross_kpis(analyses):
    t = _sum_totals(analyses)
    main = sum(a["totals"]["main"]["calls"] for a in analyses)
    cost = [float(a["reported"]["total_cost_usd"]) for a in analyses
            if a.get("reported") and a["reported"].get("total_cost_usd") is not None]
    np_ = len({a["project"] for a in analyses})
    extra = [kpi("sessions", '<span class="n">%d</span>' % len(analyses), "%d project%s" % (np_, "" if np_ == 1 else "s")),
             kpi("API calls", '<span class="n">%d</span>' % t["calls"], "%d main + %d subagent" % (main, t["calls"] - main)),
             kpi("peak context", num(max((a["peak_context"] for a in analyses), default=0)), "largest session")]
    drains = [(dv, d, a["session_id"]) for a in analyses for d, dv in [top_drain(a)] if d]
    if drains:
        dv, d, sid = max(drains)
        extra.append(kpi("biggest single drain", num(dv),
                         '<span title="%s">%s</span> &middot; <span class="mono">%s</span>' % (
                             esc(d), esc(short(d, 34)), esc(sid[:8])),
                         title="largest single cause (tool result, file, listing...) summed within one session"))
    cells = headline_kpis(t, extra=extra)
    if cost:
        cells.append(kpi("reported cost", '<span class="n">$%.2f</span>' % sum(cost),
                         "as reported by Claude Code (%d/%d sessions)" % (len(cost), len(analyses)), cls="dim",
                         title="API-equivalent cost, as reported by Claude Code (cost-state). "
                               "On a subscription this is not what you are billed."))
    return '<div class="kpis">%s</div>%s%s' % ("".join(cells), cost_note(), sources_line(analyses))


def sessions_table(analyses, detailed, understand=None):
    """One row per session. With understand() data the 'intent' column (keyword rule) becomes the goals of the
    session's requests."""
    goals_of = {}
    if understand is not None:
        from .understand_html import goal_codes
        codes = goal_codes(understand)
        for s in understand["sessions"]:
            seen = []
            for r in s["requests"]:
                if r["goal"]["id"] not in [x[0] for x in seen]:
                    seen.append((r["goal"]["id"], r["goal"]["name"]))
            goals_of[s["session_id"]] = [(codes.get(gid, "?"), nm) for gid, nm in seen]
    rows = []
    for a in analyses:
        t = a["totals"]["all"]
        m = metrics(t)
        it = a["intent"]
        drain, dv = top_drain(a)
        sid = a["session_id"]
        link = ('<a href="#s-%s" class="mono">%s</a>' % (esc(sid[:8]), esc(sid[:8])) if sid in detailed
                else '<span class="mono" title="%s">%s</span>' % (esc(sid), esc(sid[:8])))
        rows.append([
            (link, sid),
            (esc(a["project"]), a["project"]),
            ('<span class="mono">%s</span>' % t_full(a["start"]), a["start"] or ""),
            (('%s <span class="muted kw" title="%s">%s</span>' % (
                esc(it["intent"]), esc(short(a["first_prompt"], 300)),
                "“%s”" % esc(it["match"]) if it.get("match") else ""), it["intent"]) if understand is None else
             ("; ".join('<span class="gc mono">%s</span> <span title="%s">%s</span>' % (c, esc(nm), esc(short(nm, 40)))
                        for c, nm in goals_of.get(sid, [])) or "-",
              " ".join(c for c, _ in goals_of.get(sid, [])))),
            ("%d" % t["calls"], t["calls"]),
            (num(m["cost_weighted"]), m["cost_weighted"]),
            ('<span class="n" title="%s">%s</span>' % (esc(usd_text(m)), fmt_usd(m["usd"]) if m["usd"] is not None
                                                       and m["calls"] > m["unpriced_calls"] else "-"), m["usd"] or 0),
            (num(m["new"]), m["new"]),
            (num(m["output"]), m["output"]),
            (num(m["cache_read"]), m["cache_read"]),
            ('<span class="muted">%s</span>' % num(m["all_in"]), m["all_in"]),
            (num(a["peak_context"]), a["peak_context"]),
            (("%s %s" % (num(dv), esc(short(drain, 38)))) if drain else "-", dv),
        ])
    return table([("session", False), ("project", False), ("start", False),
                  ("intent" if understand is None else "goals", False), ("calls", True),
                  ("cost-wtd", True), ("API $", True), ("new", True), ("output", True), ("cache rd", True),
                  ("all-in", True), ("peak ctx", True), ("top drain", False)], rows, "", sortable=True)


def grouping(analyses, key):
    groups = {}
    for a in analyses:
        g = key(a)
        t = groups.setdefault(g, {"fresh_input": 0, "cache_write": 0, "cache_read": 0, "output": 0, "all_in": 0, "n": 0})
        for k in ("fresh_input", "cache_write", "cache_read", "output", "all_in"):
            t[k] += a["totals"]["all"].get(k, 0)
        t["n"] += 1
    return group_bars(groups)


def group_bars(groups):
    """groups: {label: token totals + n} -> stacked bars by token type, largest first."""
    if not groups:
        return ""
    scale = max(t["all_in"] for t in groups.values()) or 1
    rows = []
    for g, t in sorted(groups.items(), key=lambda kv: (-metrics(kv[1])["cost_weighted"], -kv[1]["all_in"])):
        m = metrics(t)
        segs = [(t["fresh_input" if k == "input" else k], c, "%s %s" % (l, format(t["fresh_input" if k == "input" else k], ",")))
                for k, l, c in TOK]
        bar = hbar(segs).replace('class="hbar"', 'class="hbar" style="width:%.2f%%"' % (100.0 * t["all_in"] / scale), 1)
        rows.append('<div class="brow"><div class="bl">%s <span class="muted">%d</span></div><div class="bt2">%s</div>'
                    '<div class="br" title="cost-weighted; new / output / cache reads; all-in">%s <span class="muted">'
                    'cw &middot; new %s &middot; out %s &middot; cr %s &middot; all-in %s</span></div></div>'
                    % (esc(short(g, 26)), t["n"], bar, num(m["cost_weighted"]), fmt_tok(m["new"]),
                       fmt_tok(m["output"]), fmt_tok(m["cache_read"]), fmt_tok(m["all_in"])))
    return '<div class="blist">%s</div>' % "".join(rows)


def drains_over_time(analyses):
    days = {}
    for a in analyses:
        d = _local(a["start"])
        days.setdefault(d.strftime("%Y-%m-%d") if d else "?", []).append(a)
    by_day = len(days) > 1
    if by_day:
        buckets = [(k, k[5:], k, v) for k, v in sorted(days.items())]
    else:
        buckets = [(a["session_id"], a["session_id"][:6], "%s %s" % (a["session_id"][:8], t_full(a["start"])), [a])
                   for a in analyses]

    def dims(a):
        d = {"tool": {}, "file": {}, "mcp": {}}
        for c in all_calls(a):
            for r in c.get("tool_results") or []:
                v = r.get("attributed_tokens") or 0
                if not v:
                    continue
                d["tool"][r["tool"]] = d["tool"].get(r["tool"], 0) + v
                if r["tool"] == "Read":
                    d["file"][r["target"]] = d["file"].get(r["target"], 0) + v
                mp = mcp_parts(r["tool"])
                if mp:
                    d["mcp"][mp[0]] = d["mcp"].get(mp[0], 0) + v
        return d

    per = {a["session_id"]: dims(a) for a in analyses}
    out = []
    for dim, title in (("tool", "by tool"), ("file", "by file"), ("mcp", "by MCP server")):
        tot = {}
        for d in per.values():
            for k, v in d[dim].items():
                tot[k] = tot.get(k, 0) + v
        if not tot:
            out.append('<div class="panel"><h3>%s</h3><p class="muted">No attributed tokens.</p></div>' % title)
            continue
        top = sorted(tot, key=lambda k: -tot[k])[:6]
        series = [(k, CAT[i]) for i, k in enumerate(top)] + [("other", OTHER)]
        bk = []
        for _, lab, ttl, sess in buckets:
            vals = {}
            for a in sess:
                for k, v in per[a["session_id"]][dim].items():
                    kk = k if k in top else "other"
                    vals[kk] = vals.get(kk, 0) + v
            bk.append((lab, ttl, vals))
        leg = "".join('<div class="li" title="%s"><i class="sw" style="background:%s"></i><span class="lt">%s</span> %s</div>'
                      % (esc(k), c, esc(short(k, 30)), num(tot.get(k, sum(v for kk, v in tot.items() if kk not in top))))
                      for k, c in series if k != "other" or len(tot) > len(top))
        out.append('<div class="panel"><h3>%s</h3>%s<div class="vlegend">%s</div></div>' % (title, columns_svg(bk, series), leg))
    note = "per day" if by_day else "per session (all sessions are on one day)"
    return '<p class="muted">Tokens attributed to tool results, top 6 + other, %s.</p><div class="grid3">%s</div>' % (
        note, "".join(out))


def offered_aggregate(analyses):
    have = [a for a in analyses if a["offered_vs_used"].get("available")]
    if not have:
        return '<p class="muted">None of these transcripts record what was offered.</p>'
    out = ['<p class="muted">%d of %d sessions record their listings. “carried” = est. listing tokens '
           '× main calls, summed over sessions where it was offered but not used.</p><div class="grid3">'
           % (len(have), len(analyses))]
    out[0] = out[0].replace('class="grid3"', 'class="stack"')
    for key, title in (("mcp_servers", "MCP servers"), ("skills", "Skills"), ("subagent_types", "Subagent types")):
        agg = {}
        for a in have:
            for e in a["offered_vs_used"].get(key) or []:
                g = agg.setdefault(e.get("display") or e["name"], {"off": 0, "used": 0, "carried": 0, "per": 0})
                g["off"] += 1
                g["used"] += bool(e["used"])
                g["per"] = max(g["per"], e["est_tokens"])
                if not e["used"]:
                    g["carried"] += e["per_session_reread_est"]
        rows = [[('<span class="mono nm" title="%s">%s</span>' % (esc(k), esc(short(k, 22))), k), ("%d" % g["off"], g["off"]),
                 ('<span class="%s">%d</span>' % ("ok" if g["used"] else "muted", g["used"]), g["used"]),
                 (num(g["per"], est=True), g["per"]), (num(g["carried"], est=True), g["carried"])]
                for k, g in sorted(agg.items(), key=lambda kv: -kv[1]["carried"])[:OFFER_ROWS]]
        more = '<p class="muted">+%d more</p>' % (len(agg) - OFFER_ROWS) if len(agg) > OFFER_ROWS else ""
        out.append('<div class="panel"><h3>%s</h3>%s%s</div>' % (
            title, table([("name", False), ("offered", True), ("used", True), ("per call", True), ("carried", True)],
                         rows, "compact", sortable=True) if rows else '<p class="muted">none</p>', more))
    out.append("</div>")
    return "".join(out)


def intent_note():
    rules = "".join("<li><b>%s</b>: %s</li>" % (i, ", ".join("<code>%s</code>" % k for k in kws)) for i, kws in INTENT_RULES)
    return ('<details class="note"><summary>How intent is computed</summary><p>The first real user prompt is matched '
            '(first %d characters) against keyword rules in this order; the first rule with a match wins, otherwise '
            '<b>other</b>. Keywords match whole words only (“fixtures” is not “fix”). It is a heuristic.</p>'
            '<ul>%s</ul></details>' % (INTENT_PROMPT_CHARS, rules))


def methodology():
    return (
        '<footer><h3>Methodology &amp; caveats</h3><ul>'
        '<li><b>Measured</b>: per-call token counts come from the API <code>usage</code> block in the transcript '
        '(fresh input, cache write, cache read, output). One API response is split across several transcript lines '
        'that repeat the same usage; tokenscope counts each <code>message.id</code> once.</li>'
        '<li><b>Estimated</b> (marked ~): sizes of tool results, prompts and listings are text length / %d. When a '
        'call’s measured new tokens exceed the estimates by a lot, tool results are scaled up to the measured '
        'value (chars/4 undercounts dense logs and minified code).</li>'
        '<li><b>cost-weighted tokens</b> (headline) = %s: the four token types on one scale using API price '
        'ratios, so cheap cache reads no longer dominate. <b>API-equivalent $</b> = the same calls at API list prices '
        '(%s); calls of unknown models are counted as unpriced and named. On a subscription neither is billed.</li>'
        '<li><b>new</b> = uncached input + cache write; output and cache reads are always shown separately. The '
        'per-call timeline &ldquo;new tokens&rdquo; and spikes also include output. <b>all-in</b> (secondary) is the '
        'plain sum of all four, dominated by cache reads that grow with every call.</li>'
        '<li><b>Cache misses</b>: cache read falling below half of the previous context. If the idle gap exceeded '
        'the cache TTL (1h when the previous call wrote 1h cache, else %s) the miss is blamed on expiry.</li>'
        '<li><b>Compaction</b>: a compact boundary/summary in the transcript, or context dropping below half.</li>'
        '<li>Subagent calls are separate API calls with their own context; they are shown after the call that '
        'spawned them, hatched.</li>'
        '<li>Times are local to the machine that generated this report: %s.</li></ul></footer>'
    ) % (CHARS_PER_TOKEN, esc(pricing.COST_WEIGHTED_FORMULA), esc(PRICE_SRC), fmt_dur(TTL_5M),
         esc(tz_label()))


# ---------------------------------------------------------------- page

CSS = """
:root{--bg:#0d1117;--panel:#161b22;--border:#30363d;--line:#21262d;--text:#e6edf3;--muted:#8b949e;
--mono:%(mono)s}
*{box-sizing:border-box}
html{font-size:15px}
body{margin:0;background:var(--bg);color:var(--text);line-height:1.45;
font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","Noto Sans",Helvetica,Arial,sans-serif}
.wrap{max-width:1260px;margin:0 auto;padding:16px 20px 40px}
.n,.mono,code,td.num{font-family:var(--mono)}
code{font-size:13px;color:#d2a8ff}
a{color:#58a6ff;text-decoration:none}a:hover{text-decoration:underline}
.muted{color:var(--muted);font-weight:normal}
header.top{display:flex;align-items:baseline;gap:14px;flex-wrap:wrap;border-bottom:1px solid var(--border);
padding-bottom:10px;margin-bottom:12px}
header.top h1{font-family:var(--mono);font-size:24px;margin:0;letter-spacing:-.5px}
header.top h1 b{color:#4f7fc4}
header.top .tag{font-size:15px;color:#c9d1d9;font-style:italic}
header.top .gen{margin-left:auto;font-size:13px;color:var(--muted)}
h2{font-size:17px;margin:0 0 10px;font-weight:600}
h2 .muted,h3 .muted{font-size:13.5px}
h2.sect{margin:22px 0 4px;font-size:18px}
h3{font-size:15px;margin:0 0 8px;font-weight:600}
h3.sub{margin-top:14px}
.panel{background:var(--panel);border:1px solid var(--border);border-radius:8px;padding:14px 16px;margin:12px 0;min-width:0}
.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(106px,1fr));gap:10px;margin:12px 0}
.kpi{background:var(--panel);border:1px solid var(--border);border-radius:8px;padding:9px 11px;min-width:0}
.kpi .l{font-size:13px;color:var(--muted);white-space:normal;overflow:visible;text-overflow:clip;overflow-wrap:anywhere;hyphens:auto}
.kpi .v{font-size:23px;font-weight:600;margin:2px 0;white-space:nowrap}
.kpi .v .d{font-size:13px;font-weight:600;margin-left:6px}
.kpi .s{font-size:12.5px;color:var(--muted)}
.kpi.head{border-color:#4f7fc4}.kpi.head .v{font-size:25px}
.kpi.dim{opacity:.72}.kpi.dim .v{font-size:17px;color:var(--muted)}
p.cnote{font-size:12px;margin:-4px 0 8px}p.tlnote{font-size:12px;margin:2px 0 0}
.fxmore{margin-top:8px;font-size:14px}.fxmore ul.fl{list-style:none;padding:0}.fxmore li{margin:4px 0}
.panel.srcs{font-size:13.5px;padding:8px 14px}.srcs .srcl{white-space:nowrap}.srcs b{font-weight:600}
.sw{display:inline-block;width:12px;height:12px;border-radius:3px;vertical-align:-1px;margin-right:6px;flex:none}
.sw.hatch{background:repeating-linear-gradient(45deg,#4f7fc4 0 3px,#0d1117 3px 5px)}
.ln{display:inline-block;width:18px;height:0;border-top:2px solid #e6edf3;vertical-align:4px;margin-right:6px}
.mk{display:inline-block;width:10px;height:10px;border-radius:2px;margin-right:6px}
.legend{display:flex;flex-wrap:wrap;gap:6px 18px;font-size:13.5px;color:#c9d1d9;margin-bottom:6px}
svg.chart{display:block;overflow:visible}
svg .grid{stroke:#21262d;stroke-width:1}
svg .axl{fill:#8b949e;font:12px var(--mono)}
svg .axt{fill:#8b949e;font:12px var(--mono)}
svg .ann{fill:#e6edf3;font:600 12px var(--mono)}
svg .evl{font:600 12px var(--mono)}
svg .subl{fill:#a5b4c8;font:12px var(--mono)}
svg .seg{stroke:#161b22;stroke-width:1}
svg .hit{fill:transparent}
svg g.bar:hover .hit{fill:#ffffff;fill-opacity:.06}
table{border-collapse:collapse;width:100%%;font-size:14px}
th,td{padding:5px 8px;border-bottom:1px solid var(--line);text-align:left;vertical-align:top}
th{color:var(--muted);font-weight:600;font-size:13px;white-space:nowrap}
th.num,td.num{text-align:right;white-space:nowrap}
table.compact td,table.compact th{padding:4px 7px}
table.sortable th{cursor:pointer;user-select:none}
table.sortable th:hover{color:var(--text)}
table.sortable th[data-dir=asc]:after{content:" \\25B2";font-size:10px}
table.sortable th[data-dir=desc]:after{content:" \\25BC";font-size:10px}
tr.sub td{background:#ffffff06;color:#c9d1d9}
tr.sub td:first-child{padding-left:20px}
tbody tr:hover td{background:#ffffff0a}
.tag{display:inline-block;padding:0 6px;border-radius:9px;font-size:12px;border:1px solid #484f58;color:#c9d1d9;white-space:nowrap}
.tag.bad{border-color:#f85149;color:#ff7b72}.tag.vio{border-color:#a371f7;color:#d2a8ff}
.tag.warn{border-color:#bb8009;color:#e3b341}.tag.ok{border-color:#2e9e74;color:#56d364}
.warn{color:#e3b341}.ok{color:#56d364}.hot{color:#ff7b8f}
.pill{display:inline-block;padding:1px 9px;border-radius:10px;background:#21262d;border:1px solid var(--border);font-size:13.5px}
.pill.intent{border-color:#4f7fc4}
.shead{margin:6px 0}.shead .sid{font-size:16px}.shead .meta{color:var(--muted);font-size:14px;margin-top:3px}
.shead .prompt{color:#c9d1d9;font-size:14.5px;margin-top:4px;font-style:italic}
.hbar{display:flex;gap:2px;height:14px;border-radius:3px;overflow:hidden;margin:6px 0}
.hbar span{display:block;height:100%%;min-width:1px}
.spike{border-top:1px solid var(--line);padding:10px 0}.spike:first-child{border-top:0;padding-top:0}
.sph{font-size:15px}.sph .ref{font-weight:600;font-size:15px}.sph .big{font-weight:600;margin:0 6px}
ul.causes{list-style:none;margin:4px 0 0;padding:0;font-size:14px}
ul.causes li{padding:2px 0;display:flex;gap:6px;align-items:baseline;flex-wrap:wrap}
ul.causes .cause-t{min-width:56px;text-align:right;display:inline-block}
.grid2{display:grid;grid-template-columns:1fr 1fr;gap:0 12px}
.grid3{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:0 12px}
.stack{display:flex;flex-direction:column}
.tscroll{overflow-x:auto;max-width:100%%}
@media (max-width:1000px){.grid2,.grid3{grid-template-columns:1fr}}
.blist{display:flex;flex-direction:column;gap:5px;font-size:14px}
.brow{display:grid;grid-template-columns:minmax(120px,34%%) 1fr auto;gap:10px;align-items:center}
.bl{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.bt{height:12px;background:#21262d;border-radius:3px;overflow:hidden}.bt span{display:block;height:100%%;border-radius:3px}
.bt2 .hbar{margin:0}
.br{text-align:right;white-space:nowrap}
.vlegend{display:flex;flex-direction:column;gap:2px;font-size:13px;margin-top:6px}
.vlegend .li{display:flex;align-items:center;gap:2px;white-space:nowrap;overflow:hidden}
.vlegend .lt{overflow:hidden;text-overflow:ellipsis;flex:1;font-family:var(--mono);font-size:12.5px}
.path{word-break:break-all}
td .nm{white-space:nowrap}
.fixes{display:flex;flex-direction:column;gap:10px}
.fix{display:grid;grid-template-columns:150px minmax(0,1fr);gap:16px;background:#0f141b;border:1px solid var(--border);
border-left:4px solid #6e7681;border-radius:8px;padding:12px 14px}
.fix.sev-high{border-left-color:#f85149}.fix.sev-med{border-left-color:#d29922}.fix.sev-low{border-left-color:#6e7681}
.fxs .big{font-size:28px;font-weight:700;line-height:1.1;color:#e6edf3}.fxs .muted{font-size:12.5px}
.fxs .n2{font-size:13px;color:#c9d1d9;margin-top:4px}
.fxh{display:flex;gap:8px;align-items:center;font-size:12.5px}
.sev{display:inline-block;padding:0 8px;border-radius:9px;font-size:11.5px;font-weight:700;text-transform:uppercase;
letter-spacing:.4px}
.sev.high{background:#f8514926;color:#ff7b72;border:1px solid #f85149}
.sev.med{background:#d2992226;color:#e3b341;border:1px solid #bb8009}
.sev.low{background:#6e768126;color:#c9d1d9;border:1px solid #6e7681}
.rule{color:var(--muted);font-size:12px}
.fxt{font-size:15.5px;font-weight:600;margin:4px 0 2px}
.fxf{font-size:12px;color:var(--muted);margin-bottom:6px;overflow-wrap:anywhere}
.fxi{font-size:13.5px;color:#e3b341;margin:2px 0 4px}
.fxa{font-size:14px;color:#c9d1d9;margin-bottom:6px}
.code{position:relative}
.code pre{margin:0;background:#010409;border:1px solid var(--border);border-radius:6px;padding:8px 60px 8px 10px;
font:12.5px/1.5 var(--mono);color:#d2a8ff;white-space:pre-wrap;overflow-wrap:anywhere}
.code .copy{position:absolute;top:5px;right:5px;background:#21262d;color:#c9d1d9;border:1px solid var(--border);
border-radius:5px;font-size:12px;padding:1px 8px;cursor:pointer}
.code .copy:hover{background:#30363d}
details.fxe{margin-top:6px;font-size:13.5px}details.fxe summary{cursor:pointer;color:var(--muted)}
details.fxe ul{margin:4px 0;padding-left:18px;color:#c9d1d9}details.fxe li{margin:2px 0;overflow-wrap:anywhere}
@media (max-width:700px){.fix{grid-template-columns:1fr}}
details.sess{background:var(--panel);border:1px solid var(--border);border-radius:8px;margin:10px 0}
details.sess>summary{cursor:pointer;padding:10px 14px;font-size:15px;list-style:none}
details.sess>summary::-webkit-details-marker{display:none}
details.sess>summary:before{content:"\\25B8 ";color:var(--muted)}
details.sess[open]>summary:before{content:"\\25BE "}
details.sess>section{padding:0 14px 10px;border-top:1px solid var(--border)}
details.sess .panel{background:#0f141b}
p.note1{margin:8px 0 0;font-size:13.5px}
details.note{font-size:14px;color:#c9d1d9;margin-top:8px}details.note summary{cursor:pointer;color:var(--muted)}
footer{margin-top:26px;border-top:1px solid var(--border);padding-top:12px;font-size:13.5px;color:#c9d1d9}
footer li{margin:3px 0}
"""

JS = r"""
(function(){
function key(td){var v=td.getAttribute('data-v');if(v===null)v=td.textContent.trim().toLowerCase();
 return (v!==''&&/^-?\d+(\.\d+)?([eE][-+]?\d+)?$/.test(v))?parseFloat(v):v;}
document.querySelectorAll('table.sortable').forEach(function(t){
 t.querySelectorAll('thead th').forEach(function(th,i){th.addEventListener('click',function(){
  var tb=t.tBodies[0],rows=[].slice.call(tb.rows),asc=th.getAttribute('data-dir')!=='asc';
  t.querySelectorAll('thead th').forEach(function(h){h.removeAttribute('data-dir');});
  th.setAttribute('data-dir',asc?'asc':'desc');
  rows.sort(function(a,b){var x=key(a.cells[i]),y=key(b.cells[i]);
   if(typeof x!==typeof y){x=String(x);y=String(y);}return (x<y?-1:x>y?1:0)*(asc?1:-1);});
  rows.forEach(function(r){tb.appendChild(r);});});});});
document.querySelectorAll('.code .copy').forEach(function(b){b.addEventListener('click',function(){
 var t=b.parentNode.querySelector('pre').textContent;
 function done(){b.textContent='copied';setTimeout(function(){b.textContent='copy';},1200);}
 if(navigator.clipboard&&navigator.clipboard.writeText){navigator.clipboard.writeText(t).then(done,function(){});}
 else{var r=document.createRange();r.selectNodeContents(b.parentNode.querySelector('pre'));
  var s=window.getSelection();s.removeAllRanges();s.addRange(r);try{document.execCommand('copy');done();}catch(e){}}});});
function openHash(){var id=location.hash.slice(1);if(!id)return;var el=document.getElementById(id);
 if(el&&el.tagName==='DETAILS'){el.open=true;el.scrollIntoView();}}
window.addEventListener('hashchange',openHash);openHash();
})();
"""

DEFS = ('<svg width="0" height="0" style="position:absolute" aria-hidden="true"><defs>'
        '<pattern id="ts-hatch" width="6" height="6" patternUnits="userSpaceOnUse" patternTransform="rotate(45)">'
        '<rect width="2.5" height="6" fill="#0d1117" fill-opacity="0.6"/></pattern></defs></svg>')


def optimize_head():
    return ('<h2 class="part-h" id="optimize-h"><span class="pn">3</span> Optimize <span class="muted">where the tokens '
            'go and what to change</span></h2>')


def page(body, subtitle="", nav=""):
    from . import understand_html as uh  # imports this module's helpers, so not at module level
    from . import patterns_html as ph
    gen = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M")
    return (
        '<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1"><title>tokenscope%s</title>'
        '<style>%s</style></head><body>%s<div class="wrap">%s'
        '<header class="top"><h1>token<b>scope</b></h1><span class="tag">where did my tokens go?</span>'
        '<span class="gen">%sgenerated %s &middot; times in %s</span></header>%s%s</div><script>%s</script></body></html>'
    ) % (" – " + esc(subtitle) if subtitle else "", CSS % {"mono": MONO} + uh.CSS + uh.chip_css() + ph.CSS, DEFS, nav,
         esc(subtitle) + " &middot; " if subtitle else "", gen, esc(tz_label()), body, methodology(), JS)


def _wrap(understand_html, optimize_html, patterns_html=""):
    if understand_html is None:
        return patterns_html + optimize_html
    return understand_html + patterns_html + '<section id="optimize" class="part">%s%s</section>' % (optimize_head(), optimize_html)


def _money_in(u_html, analyses):
    from . import patterns_html as ph
    if u_html is None or not u_html.endswith("</section>"):
        return u_html
    return u_html[:-len("</section>")] + ph.money_table(analyses) + "</section>"


def render_session(a, fixes=None, understand=None, compare_link=None, patterns=None):
    """understand: optional understand() data for this session -> Understand section first, then Optimize.
    patterns: optional patterns.mine() output (with replays) -> Patterns section between the two."""
    from . import understand_html as uh_
    from . import patterns_html as ph
    anchor = "v-" + a["session_id"][:8]
    u_html = None
    if understand is not None:
        u_html = _money_in(uh_.understand_section(understand, [a], per_request=True, href_for=lambda r: "#" + anchor),
                           [a])
    body = _wrap(u_html, session_view(a, fixes, anchor=anchor, fixes_top=understand is not None,
                                      understand=understand),
                 ph.patterns_section(patterns, [a]) if patterns is not None else "")
    return page(body, "session %s" % a["session_id"][:8],
                nav=uh_.nav(compare_link, patterns=patterns is not None) if understand is not None else "")


def render_cross(analyses, fixes=None, filters="", cross_fixes=None, understand=None, compare_link=None,
                 patterns=None, corpus=False):
    """fixes: optional {session_id: [fix, ...]}; cross_fixes: aggregated fixes across sessions;
    understand: optional understand() data -> Understand section first, then Optimize;
    patterns: optional patterns.mine() output -> Patterns section; corpus: big-selection mode (top CORPUS_TOP
    fixes, session views only for the CORPUS_DETAIL largest sessions, no per-session drains timeline)."""
    from . import understand_html as uh_
    from . import patterns_html as ph
    fixes = fixes or {}
    analyses = sorted(analyses, key=lambda a: a["start"] or "")
    biggest = sorted(analyses, key=lambda a: (-metrics(a["totals"]["all"])["cost_weighted"],
                                              -a["totals"]["all"]["all_in"]))[:CORPUS_DETAIL if corpus else DETAIL_CAP]
    detailed = {a["session_id"] for a in biggest}
    if corpus and cross_fixes is not None:
        cross_fixes = cross_fixes[:CORPUS_TOP]
    b = [cross_kpis(analyses), ph.corpus_kpis(analyses)]
    fx_html = ('<div class="panel"><h2>Fixes <span class="muted">all sessions; same rule + target merged across '
               'sessions</span></h2>%s</div>' % fixes_section(cross_fixes, understand)) if cross_fixes is not None \
        else ""
    if understand is not None:
        b.append(fx_html)
    b.append('<div class="panel"><h2>Sessions <span class="muted">click a header to sort; click an id to jump to its '
             'session view</span></h2>%s%s</div>' % (sessions_table(analyses, detailed, understand),
                                                     intent_note() if understand is None else ""))
    if understand is None:
        by2 = ("By intent", grouping(analyses, lambda a: a["intent"]["intent"]))
    else:
        by2 = ('By goal <span class="muted">tokens of its requests</span>', group_bars(uh_.group_bars_by_goal(understand)))
    b.append('<div class="grid2"><div class="panel"><h3>By project</h3>%s</div><div class="panel"><h3>%s</h3>%s'
             '</div></div>' % (grouping(analyses, lambda a: a["project"]), by2[0], by2[1]))
    b.append(legend(extra=False))
    if not corpus:
        b.append('<h2 class="sect">Top token drains over time</h2>' + drains_over_time(analyses))
    if understand is None:
        b.append(fx_html)
    b.append('<h2 class="sect">Offered vs used (all sessions)</h2>' + offered_aggregate(analyses))
    b.append('<h2 class="sect">Session views</h2><p class="muted">Full detail for the %d largest sessions by '
             'cost-weighted tokens%s.</p>' % (len(biggest), "; the other %d are in the table above" % (len(analyses) - len(biggest))
                                if len(analyses) > len(biggest) else ""))
    for a in biggest:
        t = a["totals"]["all"]
        m = metrics(t)
        sid = a["session_id"]
        b.append('<details class="sess" id="s-%s"><summary><span class="mono">%s</span> &middot; %s &middot; %s '
                 '&middot; %s cost-weighted &middot; %s &middot; new %s, output %s, cache reads %s '
                 '<span class="muted">(all-in %s)</span> &middot; %d calls <span class="muted">%s</span></summary>%s'
                 '</details>'
                 % (esc(sid[:8]), esc(sid[:8]), esc(a["project"]), t_full(a["start"]), num(m["cost_weighted"]),
                    esc(usd_text(m)), num(m["new"]), num(m["output"]), num(m["cache_read"]), num(m["all_in"]),
                    t["calls"], esc(short(a["first_prompt"], 60)),
                    session_view(a, fixes.get(sid), anchor="v-" + sid[:8])))
    u_html = None
    if understand is not None:
        u_html = _money_in(uh_.understand_section(
            understand, analyses, href_for=lambda r: "#s-" + r["session_id"][:8] if r["session_id"] in detailed else None),
            analyses)
    p_html = ph.patterns_section(patterns, analyses) if patterns is not None else ""
    return page(_wrap(u_html, "".join(b), p_html), (filters or "all sessions") + (" (corpus)" if corpus else ""),
                nav=uh_.nav(compare_link, patterns=patterns is not None) if understand is not None else "")


def write(path, text):
    d = os.path.dirname(os.path.abspath(path))
    os.makedirs(d, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
