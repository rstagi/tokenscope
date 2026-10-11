"""Before/after comparison of two analyzed sessions (text table + self-contained HTML)."""
import difflib
import os
import re
import statistics

from . import fixes as fixes_mod
from . import pricing
from . import goals as gl
from . import report as R
from . import understand as un
from .analyze import analyze_session
from .cli import fmt_dur, fmt_tok

TOP_CAUSES = 10
PROMPT_DIFF_CHARS = 300   # per side, in the "Prompts differ" notice
TIMELINE_PH = 200         # plot height of each compare timeline (both must fit a 1280x800 recording)

# (key, label, getter, formatter); lower is better for all of them
def _usd(a):
    m = R.metrics(a["totals"]["all"])
    return None if m["usd"] is None or (m["calls"] and m["calls"] <= m["unpriced_calls"]) else m["usd"]


METRICS = [
    ("cost_weighted", "cost-weighted tokens", lambda a: R.metrics(a["totals"]["all"])["cost_weighted"], "tok"),
    ("api_cost_usd", "API-equivalent $", _usd, "usd"),
    ("new", "new (uncached input + cache write)", lambda a: R.metrics(a["totals"]["all"])["new"], "tok"),
    ("output", "output tokens", lambda a: a["totals"]["all"]["output"], "tok"),
    ("cache_read", "cache reads", lambda a: a["totals"]["all"]["cache_read"], "tok"),
    ("all_in", "all-in tokens (unweighted)", lambda a: a["totals"]["all"]["all_in"], "tok"),
    ("calls", "API calls (incl. subagents)", lambda a: a["totals"]["all"]["calls"], "int"),
    ("peak_context", "peak context", lambda a: a["peak_context"], "tok"),
    ("duration", "duration", lambda a: a["duration_seconds"], "dur"),
    ("cost", "reported cost (Claude Code)", lambda a: (float(a["reported"]["total_cost_usd"])
                                               if a.get("reported") and a["reported"].get("total_cost_usd")
                                               is not None else None), "usd"),
]


def fmt_val(v, kind):
    if v is None:
        return "-"
    if kind == "tok":
        return fmt_tok(v)
    if kind == "dur":
        return fmt_dur(v)
    if kind == "usd":
        return R.fmt_usd(v)
    return "%d" % v


def pct(a, b):
    if a is None or b is None or not a:
        return None
    return 100.0 * (b - a) / a


def fmt_pair(x, y):
    """Both values in the same unit, e.g. ('1.10M', '0.42M')."""
    m = max(x, y)
    if m >= 1_000_000:
        return "%.2fM" % (x / 1e6), "%.2fM" % (y / 1e6)
    if m >= 100_000:
        return "%dk" % round(x / 1000), "%dk" % round(y / 1000)
    if m >= 10_000:
        return "%.1fk" % (x / 1000), "%.1fk" % (y / 1000)
    return format(x, ","), format(y, ",")


def cause_key(x):
    if x["cause"] == "tool_result":
        return x["label"]
    if x["cause"] == "system_overhead":
        return "system: " + x["label"]
    return {"cache_miss": "Cache misses (re-written context)", "compaction": "Compaction rebuilds",
            "assistant_output": "Previous assistant output", "user_prompt": "User prompts",
            "other": "Other (reminders, framing)"}.get(x["cause"], x["label"])


def causes(a):
    agg = {}
    for c in R.all_calls(a):
        for x in c.get("attribution") or []:
            k = cause_key(x)
            agg[k] = agg.get(k, 0) + x["tokens"]
    return agg


def compare_data(a, b):
    rows = []
    for key, label, get, kind in METRICS:
        va, vb = get(a), get(b)
        if key == "cost" and (va is None or vb is None):
            continue
        rows.append({"key": key, "label": label, "a": va, "b": vb, "kind": kind,
                     "delta": (vb - va) if va is not None and vb is not None else None, "pct": pct(va, vb)})
    ca, cb = causes(a), causes(b)
    keys = sorted(set(ca) | set(cb), key=lambda k: -max(ca.get(k, 0), cb.get(k, 0)))[:TOP_CAUSES]
    top = []
    for k in keys:
        x, y = ca.get(k, 0), cb.get(k, 0)
        status = "gone" if x and not y else "new" if y and not x else ""
        top.append({"cause": k, "a": x, "b": y, "delta": y - x, "status": status})
    fa, fb = fixes_mod.session_fixes(a), fixes_mod.session_fixes(b)
    ids_b = {f["id"] for f in fb}
    resolved = [f for f in fa if f["id"] not in ids_b]
    allin = next(r for r in rows if r["key"] == "all_in")
    head = headline(a, b)
    big = max(1000, 0.02 * max(allin["a"], allin["b"]))
    gone = [t for t in top if t["status"] == "gone" and t["a"] >= big]
    new = [t for t in top if t["status"] == "new" and t["b"] >= big]
    if gone:
        head += "; gone in B: %s" % ", ".join("%s (%s)" % (R.short(t["cause"], 40), fmt_tok(t["a"])) for t in gone[:2])
    if new:
        head += "; new in B: %s" % ", ".join("%s (%s)" % (R.short(t["cause"], 40), fmt_tok(t["b"])) for t in new[:2])
    return {"a": a, "b": b, "rows": rows, "top_causes": top, "fixes_a": fa, "fixes_b": fb, "fixes_resolved": resolved,
            "headline": head, "prompt_diff": prompt_diff(a["first_prompt"], b["first_prompt"])}


def headline(a, b):
    """'B used 41% fewer cost-weighted tokens (120k -> 70.5k), API-equivalent $0.40 -> $0.21; new 60k -> 30k,
    output 2k -> 1k, cache reads 1.1M -> 0.4M (all-in 1.2M -> 0.5M)'."""
    ma, mb = R.metrics(a["totals"]["all"]), R.metrics(b["totals"]["all"])
    p = pct(ma["cost_weighted"], mb["cost_weighted"])
    xa, xb = fmt_pair(int(round(ma["cost_weighted"])), int(round(mb["cost_weighted"])))
    if p is None:
        head = "B used %s cost-weighted tokens (A used none)" % xb
    elif abs(p) < 0.5:
        head = "B used about the same cost-weighted tokens as A (%s -> %s)" % (xa, xb)
    else:
        head = "B used %.0f%% %s cost-weighted tokens (%s -> %s)" % (abs(p), "fewer" if p < 0 else "more", xa, xb)
    ua, ub = _usd(a), _usd(b)
    if ua is not None or ub is not None:
        head += ", API-equivalent %s -> %s" % (R.fmt_usd(ua), R.fmt_usd(ub))
    unp = sorted(set(ma["unpriced_models"]) | set(mb["unpriced_models"]))
    n_unp = ma["unpriced_calls"] + mb["unpriced_calls"]
    if n_unp:
        head += " " + R.unpriced_note({"unpriced_calls": n_unp, "unpriced_models": unp})
    head += "; new %s, output %s, cache reads %s (all-in %s)" % tuple(
        "%s -> %s" % fmt_pair(ma[k], mb[k]) for k in ("new", "output", "cache_read", "all_in"))
    return head


def prompt_diff(pa, pb, cap=PROMPT_DIFF_CHARS):
    """None if the first prompts match (ignoring whitespace), else {'a': only-in-A text, 'b': only-in-B text}."""
    wa, wb = (pa or "").split(), (pb or "").split()
    if wa == wb:
        return None
    only_a, only_b = [], []
    for op, i1, i2, j1, j2 in difflib.SequenceMatcher(None, wa, wb, autojunk=False).get_opcodes():
        if op in ("replace", "delete"):
            only_a.append(" ".join(wa[i1:i2]))
        if op in ("replace", "insert"):
            only_b.append(" ".join(wb[j1:j2]))
    return {"a": R.short(" … ".join(only_a), cap), "b": R.short(" … ".join(only_b), cap)}


def _delta_txt(r):
    if r["delta"] is None:
        return "-"
    d = r["delta"]
    sign = "+" if d > 0 else "-" if d < 0 else "±"
    v = fmt_val(abs(d), r["kind"]) if r["kind"] != "int" else "%d" % abs(d)
    return "%s%s" % (sign, v) + ("" if r["pct"] is None else " (%+.0f%%)" % r["pct"])


def to_text(d):
    a, b = d["a"], d["b"]
    out = [d["headline"], "",
           "A (before) %s  %s  %s" % (a["session_id"][:8], R.t_full(a["start"]), R.short(a["first_prompt"], 60)),
           "B (after)  %s  %s  %s" % (b["session_id"][:8], R.t_full(b["start"]), R.short(b["first_prompt"], 60))]
    pd = d.get("prompt_diff")
    if pd:
        out += ["", "PROMPTS DIFFER", "  only in A: %s" % (pd["a"] or "-"), "  only in B: %s" % (pd["b"] or "-")]
    out += ["", "%-36s %12s %12s %18s" % ("metric", "A", "B", "delta")]
    for r in d["rows"]:
        out.append("%-36s %12s %12s %18s" % (r["label"], fmt_val(r["a"], r["kind"]), fmt_val(r["b"], r["kind"]),
                                             _delta_txt(r)))
    out += ["note: " + R.COST_NOTE]
    out += ["", "%-44s %10s %10s %10s" % ("top causes (new tokens)", "A", "B", "")]
    for t in d["top_causes"]:
        out.append("%-44s %10s %10s %10s" % (R.short(t["cause"], 44), fmt_tok(t["a"]), fmt_tok(t["b"]), t["status"]))
    sev = lambda fs: "%d (%s)" % (len(fs), ", ".join("%d %s" % (sum(1 for f in fs if f["severity"] == s), s)
                                                      for s in ("high", "med", "low")))
    out += ["", "fixes triggered: A %s  B %s" % (sev(d["fixes_a"]), sev(d["fixes_b"]))]
    if d["fixes_resolved"]:
        out.append("fixes from A that no longer trigger in B:")
        out += ["  [%s] %s" % (f["severity"], f["title"]) for f in d["fixes_resolved"]]
    return "\n".join(out)


# ---------------------------------------------------------------- HTML

A_COL, B_COL = "#d95926", "#3987e5"


def _kpi(r):
    good = r["delta"] is not None and r["delta"] < 0
    bad = r["delta"] is not None and r["delta"] > 0
    cls = "ok" if good else "hot" if bad else "muted"
    pc = "" if r["pct"] is None else "%+.0f%%" % r["pct"]
    cls_k = " head" if r["key"] in ("cost_weighted", "api_cost_usd") else " dim" if r["key"] in ("all_in", "cost") else ""
    return ('<div class="kpi%s" title="%s: %s"><div class="l">%s</div><div class="v"><span class="n">%s</span>'
            '<span class="d %s">%s</span></div><div class="s">A <span class="n">%s</span> → B <span class="n">%s</span>'
            '</div></div>') % (
        cls_k, R.esc(r["label"]), R.esc(_delta_txt(r)), R.esc(r["label"].split(" (")[0]), fmt_val(r["b"], r["kind"]), cls, pc,
        fmt_val(r["a"], r["kind"]), fmt_val(r["b"], r["kind"]))


def context_overlay_svg(a, b, W=1180, H=210):
    ca = [c["context"] for c in a["calls"]]
    cb = [c["context"] for c in b["calls"]]
    n = max(len(ca), len(cb), 2)
    L, Rr, T, B = 62, 14, 12, 30
    pw, ph = W - L - Rr, H - T - B
    vmax = max(ca + cb + [1])
    step = R._nice(vmax / 4)
    ymax = step * -(-vmax // step)
    X = lambda i: L + pw * i / (n - 1)
    Y = lambda v: T + ph - v / ymax * ph
    o = ['<svg class="chart" viewBox="0 0 %d %d" width="100%%" role="img">' % (W, H)]
    v = 0
    while v <= ymax + 1e-9:
        o.append('<line x1="%d" x2="%d" y1="%.1f" y2="%.1f" class="grid"/>' % (L, W - Rr, Y(v), Y(v)))
        o.append('<text x="%d" y="%.1f" class="axl" text-anchor="end">%s</text>' % (L - 8, Y(v) + 4, R.tick(v)))
        v += step
    k = max(1, -(-n // 20))
    for i in range(0, n, k):
        o.append('<text x="%.1f" y="%d" class="axl" text-anchor="middle">%d</text>' % (X(i), H - 10, i))
    o.append('<text x="%d" y="%d" class="axt" text-anchor="end">call #</text>' % (L - 8, H - 10))
    for vals, col, name in ((ca, A_COL, "A"), (cb, B_COL, "B")):
        if not vals:
            continue
        pts = " ".join("%.1f,%.1f" % (X(i), Y(v)) for i, v in enumerate(vals))
        o.append('<polyline fill="none" stroke="%s" stroke-width="2.5" points="%s"/>' % (col, pts))
        for i, v in enumerate(vals):
            o.append('<circle cx="%.1f" cy="%.1f" r="3.5" fill="%s"><title>%s call #%d: context %s</title></circle>'
                     % (X(i), Y(v), col, name, i, format(v, ",")))
        o.append('<text x="%.1f" y="%.1f" class="ann" fill="%s" style="fill:%s">%s %s</text>' % (
            min(X(len(vals) - 1) + 8, W - Rr - 60), Y(vals[-1]) - 8, col, col, name, fmt_tok(vals[-1])))
    o.append("</svg>")
    return "".join(o)


def _head(a, tag):
    t = a["totals"]["all"]
    m = R.metrics(t)
    return ('<div class="cmph"><span class="ab">%s</span> <span class="mono">%s</span> &middot; %s &middot; %s '
            '&middot; <span class="n">%s</span> cost-weighted &middot; %s &middot; %d calls '
            '<span class="muted">(all-in %s)</span><div class="prompt muted">“%s”</div></div>') % (
        tag, R.esc(a["session_id"][:8]), R.esc(a["project"]), R.t_full(a["start"]), fmt_tok(m["cost_weighted"]),
        R.esc(R.usd_text(m)), t["calls"], fmt_tok(m["all_in"]), R.esc(R.short(a["first_prompt"], 160)))


def render_html(d, report_link=None):
    from . import understand_html as uh
    a, b = d["a"], d["b"]
    ea, eb = R.entries(a), R.entries(b)
    vmax = max(R.timeline_vmax(ea), R.timeline_vmax(eb))
    slots = max(len(ea), len(eb))
    body = ['<div class="headline">%s</div>' % R.esc(d["headline"]),
            '<div class="grid2">%s%s</div>' % (_head(a, "A · before"), _head(b, "B · after"))]
    pd = d.get("prompt_diff")
    if pd:
        body.append('<div class="pdiff"><b>Prompts differ</b> <span class="muted">– the first user prompts of A and B '
                    'are not identical, so part of the difference may come from the task itself.</span>'
                    '<div><span class="ab">only in A</span> <span class="pa">%s</span></div>'
                    '<div><span class="ab">only in B</span> <span class="pb">%s</span></div></div>'
                    % (R.esc(pd["a"]) or '<span class="muted">–</span>', R.esc(pd["b"]) or '<span class="muted">–</span>'))
    body.append('<div class="kpis">%s</div>%s' % ("".join(_kpi(r) for r in d["rows"]), R.cost_note()))
    body.append('<div class="panel tlp"><div class="tlh"><h2 title="same y-scale and bar width; one bar per API call">'
                'Timelines</h2>%s</div><h3 class="tl">A · before</h3>%s<h3 class="tl">B · after</h3>%s</div>' % (
                    R.legend(), R.timeline_svg(ea, "all", vmax=vmax, n_slots=slots, ph=TIMELINE_PH),
                    R.timeline_svg(eb, "all", vmax=vmax, n_slots=slots, ph=TIMELINE_PH)))
    body.append('<div class="panel"><h2>Context size per main call <span class="muted">'
                '<i class="sw" style="background:%s"></i>A · before &nbsp; <i class="sw" style="background:%s"></i>'
                'B · after</span></h2>%s</div>' % (A_COL, B_COL, context_overlay_svg(a, b)))
    rows = []
    for t in d["top_causes"]:
        st = ('<span class="tag ok">gone in B</span>' if t["status"] == "gone" else
              '<span class="tag bad">new in B</span>' if t["status"] == "new" else "")
        dc = "ok" if t["delta"] < 0 else "hot" if t["delta"] > 0 else "muted"
        rows.append([(R.esc(R.short(t["cause"], 70)), None), (R.num(t["a"]), t["a"]), (R.num(t["b"]), t["b"]),
                     ('<span class="%s">%s%s</span>' % (dc, "+" if t["delta"] > 0 else "-" if t["delta"] < 0 else "",
                                                        fmt_tok(abs(t["delta"]))), t["delta"]), (st, None)])
    body.append('<div class="panel"><h2>Top causes, A vs B <span class="muted">tokens attributed per cause, summed '
                'over all calls (incl. subagents); ~ estimates</span></h2>%s</div>' % R.table(
                    [("cause", False), ("A", True), ("B", True), ("delta", True), ("", False)], rows, "compact"))

    def flist(fs):
        if not fs:
            return '<p class="muted">none triggered</p>'
        return "<ul class=\"fl\">%s</ul>" % "".join(
            '<li><span class="sev %s">%s</span> %s <span class="muted n">%s</span></li>' % (
                f["severity"], R.SEV_LABEL[f["severity"]], R.esc(f["title"]),
                _fix_sav(f))
            for f in fs)
    if d["fixes_resolved"]:
        body.append('<div class="panel"><h3>Fixes from A that no longer trigger in B <span class="muted">%d of %d'
                    '</span></h3>%s</div>' % (len(d["fixes_resolved"]), len(d["fixes_a"]), flist(d["fixes_resolved"])))
    body.append('<div class="grid2"><div class="panel"><h3>Fixes triggered in A · before <span class="muted">%d</span>'
                '</h3>%s</div><div class="panel"><h3>Fixes triggered in B · after <span class="muted">%d</span></h3>%s'
                '</div></div>' % (len(d["fixes_a"]), flist(d["fixes_a"]), len(d["fixes_b"]), flist(d["fixes_b"])))
    extra = (".headline{font-size:22px;font-weight:600;margin:4px 0 6px}"
             ".cmph{min-width:0;background:var(--panel);border:1px solid var(--border);border-radius:8px;padding:10px 14px;"
             "margin:6px 0;font-size:14.5px}.cmph .prompt{font-style:italic;font-size:13.5px;margin-top:3px}"
             ".ab{font-weight:700;font-family:var(--mono)}h3.tl{margin:14px 0 2px;font-family:var(--mono)}"
             "ul.fl{list-style:none;padding:0;margin:0;font-size:14px}ul.fl li{margin:5px 0}"
             ".pdiff{border:1px solid #bb8009;background:#bb800914;border-radius:8px;padding:7px 14px;margin:6px 0;"
             "font-size:13.5px}.pdiff>b{color:#e3b341}.pdiff .ab{display:inline-block;min-width:78px;font-size:12.5px;"
             "color:var(--muted)}.pdiff .pa{color:#ffb38a}.pdiff .pb{color:#9cc4ff}"
             ".cmph .prompt{white-space:nowrap;overflow:hidden;text-overflow:ellipsis}"
             ".kpis .kpi .v{font-size:21px}.kpis{margin:8px 0}"
             ".tlp{padding-top:10px}.tlh{display:flex;align-items:baseline;gap:6px 22px;flex-wrap:wrap}"
             ".tlh h2{margin:0}.tlh .legend{margin:0;font-size:13px;gap:4px 13px}.tlp h3.tl{margin:4px 0 0}")
    html = R.page('<section id="compare" class="part">%s</section>' % "".join(body),
                  "compare %s vs %s" % (a["session_id"][:8], b["session_id"][:8]),
                  nav=uh.nav(report_link=report_link, active="compare"))
    return html.replace("</style>", extra + "nav.steps a.on{border-color:#8b949e;background:#21262d;color:#fff}"
                        "</style>", 1)


# ---------------------------------------------------------------- sets of sessions (A = before, B = after)

def read_ids(spec):
    """Session ids from a file (one per line / whitespace or comma separated, # comments) or 'ID,ID,...'."""
    text = spec
    if os.path.isfile(spec):
        with open(spec, encoding="utf-8") as fh:
            text = "\n".join(ln.split("#", 1)[0] for ln in fh)
    return [x for x in re.split(r"[\s,]+", text) if x]


def _norm_prompt(p):
    return " ".join((p or "").split())


def _stat(xs):
    return {"median": statistics.median(xs) if xs else 0, "min": min(xs) if xs else 0, "max": max(xs) if xs else 0}


def _req_sources(u):
    """{request id: {source: context tokens summed over its main calls}} (MCP listings folded into 'mcp')."""
    from .understand_html import _fold
    out = {}
    for s in u["sessions"]:
        by_idx = {c["index"]: c for c in s["context"]["main"]}
        for r in s["requests"]:
            src = {}
            for i in r["call_indexes"]:
                for k, v in (by_idx[i]["sources"] if i in by_idx else {}).items():
                    src[k] = src.get(k, 0) + v
            out[r["id"]] = _fold(src)
    return out


def _t(r, k):
    return r["tokens"].get(k, 0) or 0


def _cw(r):
    """Cost-weighted tokens of one request (pricing.COST_WEIGHTED_FORMULA over its token types)."""
    return round(pricing.cost_weighted_of(_t(r, "fresh_input"), _t(r, "cache_write"), _t(r, "cache_read"),
                                          _t(r, "output")), 1)


def _side(rs):
    """Aggregates for one side of one goal."""
    hooks = [sum(1 for st in r["steps"] if un.is_hook(st)) for r in rs]
    chains = un.chain_stats(rs, top=1) if rs else []
    return {"runs": len(rs), "finish_rate": (sum(r["outcome"] == "success" for r in rs) / len(rs)) if rs else None,
            "all_in": _stat([r["tokens"]["all_in"] for r in rs]), "new": _stat([r["tokens"]["new"] for r in rs]),
            "cost_weighted": _stat([_cw(r) for r in rs]),
            "new_input": _stat([_t(r, "fresh_input") + _t(r, "cache_write") for r in rs]),
            "output": _stat([_t(r, "output") for r in rs]), "cache_read": _stat([_t(r, "cache_read") for r in rs]),
            "peak_context": _stat([r["peak_context"] for r in rs]), "calls": _stat([r["calls"] for r in rs]),
            "top_chain": chains[0] if chains else None, "chains": len({r["chain"] for r in rs}),
            "paths": {c["chain"]: c["count"] for c in (un.chain_stats(rs) if rs else [])},
            "hook_fires_per_run": (sum(hooks) / len(rs)) if rs else 0.0,
            "prompts": sorted({_norm_prompt(r["prompt"]) for r in rs if r["prompt"]}),
            "request_ids": [r["id"] for r in rs]}


def compare_sets(sessions_a, sessions_b, analyses=None, name_with_claude=False, namer_opts=None, now=None,
                 goal_threshold=None, area_threshold=None, label_with_claude=False):
    """Per-goal comparison of two sets of sessions. Goals are discovered on the UNION of A and B requests, so the
    same prompt lands in the same goal on both sides. label_with_claude: deprecated name_with_claude."""
    from . import fixes as fx
    analyses = dict(analyses or {})
    for s in sessions_a + sessions_b:
        if s.id not in analyses:
            analyses[s.id] = analyze_session(s)
    u = un.understand(sessions_a + sessions_b, name_with_claude=name_with_claude or label_with_claude,
                      namer_opts=namer_opts, goal_threshold=goal_threshold, area_threshold=area_threshold, now=now,
                      analyses=analyses)
    ids = {"a": [s.id for s in sessions_a], "b": [s.id for s in sessions_b]}
    side_of = {sid: k for k in ("a", "b") for sid in ids[k]}
    reqs = {"a": [], "b": []}
    for s in u["sessions"]:
        reqs[side_of[s["session_id"]]] += s["requests"]
    goals = []
    for g in u["goals"]:
        ra = [r for r in reqs["a"] if r["goal"]["id"] == g["id"]]
        rb = [r for r in reqs["b"] if r["goal"]["id"] == g["id"]]
        a, b = _side(ra), _side(rb)
        pa, pb = set(a["prompts"]), set(b["prompts"])
        goals.append({
            "id": g["id"], "goal": g["goal"], "area": g["area_name"], "area_id": g["area"],
            "name_source": g["name_source"], "request_ids": g["request_ids"],
            "a": a, "b": b, "same_prompts": bool(ra and rb and pa == pb),
            "only_a": sorted(pa - pb), "only_b": sorted(pb - pa),
            "delta_pct": {k: pct(a[k]["median"], b[k]["median"]) if ra and rb else None
                          for k in ("cost_weighted", "new_input", "output", "cache_read", "all_in", "new",
                                    "peak_context", "calls")}})
    both = [g for g in goals if g["a"]["runs"] and g["b"]["runs"]]
    common = {g["id"] for g in both}
    run_a = [r for r in reqs["a"] if r["goal"]["id"] in common]
    run_b = [r for r in reqs["b"] if r["goal"]["id"] in common]
    checks = [{"goal_id": g["id"], "goal": g["goal"], "side": "A" if g["a"]["runs"] else "B",
               "runs": g["a"]["runs"] or g["b"]["runs"],
               "text": "goal %s \"%s\" has members only in %s (%s): not compared, left out of the headline "
                       "median" % (g["id"], g["goal"], "A" if g["a"]["runs"] else "B",
                                  _pl(g["a"]["runs"] or g["b"]["runs"], "run"))}
              for g in goals if not (g["a"]["runs"] and g["b"]["runs"])]
    overall = {"a": _side(reqs["a"]), "b": _side(reqs["b"])}
    med_a = statistics.median([r["tokens"]["all_in"] for r in run_a]) if run_a else 0
    med_b = statistics.median([r["tokens"]["all_in"] for r in run_b]) if run_b else 0
    cw_a = statistics.median([_cw(r) for r in run_a]) if run_a else 0
    cw_b = statistics.median([_cw(r) for r in run_b]) if run_b else 0
    # context by source: per-run median of each source
    src = _req_sources(u)
    ctx = {}
    for k in ("a", "b"):
        rs = reqs[k]
        keys = {key for r in rs for key in src[r["id"]]}
        ctx[k] = {key: statistics.median([src[r["id"]].get(key, 0) for r in rs]) for key in keys} if rs else {}
    # fixes: same merge as the report, per side
    fixes = {}
    for k in ("a", "b"):
        an = [analyses[i] for i in ids[k]]
        fixes[k] = fx.aggregate({a["session_id"]: fx.session_fixes(a) for a in an}, an)
    ids_b = {f["id"] for f in fixes["b"]}
    resolved = [f for f in fixes["a"] if f["id"] not in ids_b]
    d = {"ids": ids, "goals": goals, "checks": checks, "areas": u["areas"], "goal_method": u["goal_method"],
         "overall": overall, "median_all_in": {"a": med_a, "b": med_b},
         "median_all_in_pct": pct(med_a, med_b), "median_cost_weighted": {"a": cw_a, "b": cw_b},
         "median_cost_weighted_pct": pct(cw_a, cw_b),
         "totals": {k: R.metrics(R._sum_totals([analyses[i] for i in ids[k]])) for k in ("a", "b")},
         "context": ctx, "fixes_a": fixes["a"], "fixes_b": fixes["b"],
         "fixes_resolved": resolved, "namer": u["namer"],
         "range": {k: _range([analyses[i] for i in ids[k]]) for k in ("a", "b")},
         "projects": sorted({analyses[i]["project"] for i in ids["a"] + ids["b"]})}
    d["headline"] = sets_headline(d)
    return d


def _range(an):
    starts = sorted(a["start"] for a in an if a["start"])
    ends = sorted((a["end"] or a["start"]) for a in an if a["end"] or a["start"])
    return (starts[0], ends[-1]) if starts else (None, None)


def sets_headline(d):
    both = [g for g in d["goals"] if g["a"]["runs"] and g["b"]["runs"]]
    p = d["median_cost_weighted_pct"]
    if p is None:
        lead = "B: no comparable runs"
    else:
        lead = "B: %s%.0f%% median cost-weighted per run" % ("−" if p < 0 else "+", abs(p))
    lead += " across %s" % _pl(len(both), "goal")
    diff = [g for g in both if not g["same_prompts"]]
    lead += ", same prompts" if both and not diff else ", prompts differ in %d of %d goals" % (len(diff), len(both))
    fa, fb = d["overall"]["a"]["finish_rate"], d["overall"]["b"]["finish_rate"]
    lead += ", finish rate A %s -> B %s" % (_pc(fa), _pc(fb))
    only = [g["id"] for g in d["goals"] if not (g["a"]["runs"] and g["b"]["runs"])]
    if only:
        lead += " (%s only on one side)" % _pl(len(only), "goal")
    return lead


def sets_totals_line(d):
    """'API-equivalent A $1.20 -> B $0.80 (all sessions); new ..., output ..., cache reads ... (all-in ...)'."""
    ta, tb = d["totals"]["a"], d["totals"]["b"]
    unp = {"unpriced_calls": ta["unpriced_calls"] + tb["unpriced_calls"],
           "unpriced_models": sorted(set(ta["unpriced_models"]) | set(tb["unpriced_models"]))}
    usd = lambda m: R.fmt_usd(m["usd"]) if m["usd"] is not None and m["calls"] > m["unpriced_calls"] else None
    ua, ub = usd(ta), usd(tb)
    money = "API-equivalent %s -> %s" % (ua or "-", ub or "-") if ua or ub else "API-equivalent $ unknown"
    return ("all sessions: cost-weighted %s -> %s, %s %s; new %s -> %s, output %s -> %s, "
            "cache reads %s -> %s (all-in %s -> %s)" % (
                fmt_tok(ta["cost_weighted"]), fmt_tok(tb["cost_weighted"]), money, R.unpriced_note(unp),
                fmt_tok(ta["new"]), fmt_tok(tb["new"]), fmt_tok(ta["output"]), fmt_tok(tb["output"]),
                fmt_tok(ta["cache_read"]), fmt_tok(tb["cache_read"]), fmt_tok(ta["all_in"]),
                fmt_tok(tb["all_in"]))).replace("  ", " ")


def _pl(n, w):
    return "%d %s%s" % (n, w, "" if n == 1 else "s")


def _pc(x):
    return "-" if x is None else "%d%%" % round(100 * x)


def _pct_txt(p):
    return "" if p is None else ("±0%" if abs(p) < 0.5 else "%s%.0f%%" % ("−" if p < 0 else "+", abs(p)))


def _tok(v):
    return fmt_tok(int(round(v or 0)))


def _rng(s, fmt=_tok):
    return "%s–%s" % (fmt(s["min"]), fmt(s["max"]))


def _n1(x):
    return ("%d" % x) if float(x).is_integer() else ("%.1f" % x)


def sets_to_text(d):
    out = [d["headline"], sets_totals_line(d), "note: " + R.COST_NOTE, ""]
    for k, tag in (("a", "A (before)"), ("b", "B (after)")):
        lo, hi = d["range"][k]
        out.append("%s %s in %s, %s .. %s" % (tag, _pl(d["overall"][k]["runs"], "run"), _pl(len(d["ids"][k]), "session"),
                                             R.t_full(lo), R.t_full(hi)))
    out.append("goals: discovered on A + B together, %s" % gl.name_note(d["namer"]))
    for c in d["checks"]:
        out.append("WARNING: " + c["text"])
    out.append("")
    rows = [["goal", "prompts", "runs A/B", "finish A/B", "med cost-wtd A -> B", "med new", "med output",
             "med cache rd", "med all-in", "med peak ctx", "med calls"]]
    for g in d["goals"]:
        a, b = g["a"], g["b"]

        def cell(key, fmt=_tok):
            return "%s -> %s %s" % (fmt(a[key]["median"]), fmt(b[key]["median"]), _pct_txt(g["delta_pct"][key]))

        rows.append(["%s %s" % (g["id"], R.short(g["goal"], 50)), "same" if g["same_prompts"] else "DIFFER", "%d/%d" % (a["runs"], b["runs"]),
                     "%s/%s" % (_pc(a["finish_rate"]), _pc(b["finish_rate"])), cell("cost_weighted"),
                     cell("new_input"), cell("output"), cell("cache_read"), cell("all_in"),
                     cell("peak_context"), cell("calls", _n1)])
        rows.append(["", "", "", "  range", "%s | %s" % (_rng(a["cost_weighted"]), _rng(b["cost_weighted"])),
                     "%s | %s" % (_rng(a["new_input"]), _rng(b["new_input"])),
                     "%s | %s" % (_rng(a["output"]), _rng(b["output"])),
                     "%s | %s" % (_rng(a["cache_read"]), _rng(b["cache_read"])),
                     "%s | %s" % (_rng(a["all_in"]), _rng(b["all_in"])),
                     "%s | %s" % (_rng(a["peak_context"]), _rng(b["peak_context"])),
                     "%s | %s" % (_rng(a["calls"], _n1), _rng(b["calls"], _n1))])
    widths = [max(len(r[i]) for r in rows) for i in range(len(rows[0]))]
    out += ["  ".join(c.ljust(w) for c, w in zip(r, widths)).rstrip() for r in rows]
    diff = [g for g in d["goals"] if not g["same_prompts"]]
    for g in diff:
        out.append("")
        out.append("PROMPTS DIFFER in %s %s:" % (g["id"], g["goal"]))
        out += ["  only in A: %s" % R.short(p, 150) for p in g["only_a"]]
        out += ["  only in B: %s" % R.short(p, 150) for p in g["only_b"]]
    out += ["", "Paths per goal (top chain, hook fires per run):"]
    for g in d["goals"]:
        out.append("  %s %s   hooks/run %.1f -> %.1f" % (g["id"], g["goal"], g["a"]["hook_fires_per_run"],
                                                     g["b"]["hook_fires_per_run"]))
        for k in ("a", "b"):
            tc = g[k]["top_chain"]
            out.append("    %s %s" % (k.upper(), "%d%%  %s" % (round(tc["pct"]), tc["chain"]) if tc else "-"))
    out += ["", "Context by source (median per run, est.)    A          B"]
    for key, lab in _ctx_keys(d):
        va, vb = d["context"]["a"].get(key, 0), d["context"]["b"].get(key, 0)
        out.append("  %-40s %8s %10s  %s" % (lab, fmt_tok(int(va)), fmt_tok(int(vb)), _gone(va, vb)))
    out += ["", "fixes: A %d, B %d; %d of A's no longer trigger in B" % (
        len(d["fixes_a"]), len(d["fixes_b"]), len(d["fixes_resolved"]))]
    out += ["  [%s] %s" % (f["severity"], f["title"]) for f in d["fixes_resolved"]]
    return "\n".join(out)


def _ctx_keys(d):
    from .understand_html import CTX_SOURCES
    have = set(d["context"]["a"]) | set(d["context"]["b"])
    return [(k, lab) for k, lab, _ in CTX_SOURCES if k in have and (d["context"]["a"].get(k) or d["context"]["b"].get(k))]


def _gone(va, vb):
    if va and not vb:
        return "gone"
    if vb and not va:
        return "new in B"
    p = pct(va, vb)
    return _pct_txt(p) if p is not None else ""


def _ab(a, b, p, fmt=fmt_tok, lower_better=True):
    cls = "muted" if p is None or abs(p) < 0.5 else ("ok" if (p < 0) == lower_better else "hot")
    return ('<span class="va">%s</span><span class="arr">→</span><span class="vb">%s</span> '
            '<span class="dl %s">%s</span>' % (fmt(a), fmt(b), cls, _pct_txt(p)))


def _abr(sa, sb, fmt=fmt_tok):
    return '<div class="rng">%s <span class="sep">|</span> %s</div>' % (_rng(sa, fmt), _rng(sb, fmt))


def _area_groups(d):
    """[(area, [compare goal, ...])] in the report's area order (goals the areas don't list come last)."""
    by_area = {}
    for g in d["goals"]:
        by_area.setdefault(g.get("area_id"), []).append(g)
    out = []
    for a in d.get("areas") or []:
        gs = [g for gid in a["goals"] for g in by_area.get(a["id"], []) if g["id"] == gid]
        if gs:
            out.append((a, gs))
    listed = {g["id"] for _, gs in out for g in gs}
    rest = [g for g in d["goals"] if g["id"] not in listed]
    if rest:
        out.append(({"id": "-", "name": "other", "goals": [g["id"] for g in rest]}, rest))
    return out


def _anchor(g, prefix=""):
    from .understand_html import goal_anchor
    return prefix + goal_anchor(g["goal"], g.get("request_ids") or [g["id"]])


def _goal_table(d):
    head = ("<tr><th>goal</th><th>prompts</th><th class=\"num\">runs A/B</th><th class=\"num\">finish</th>"
            "<th class=\"num\" title=\"%s\">median cost-weighted / run</th><th class=\"num\">median new</th>"
            "<th class=\"num\">median output</th><th class=\"num\">median cache reads</th>"
            "<th class=\"num muted\">median all-in</th>"
            "<th class=\"num\">median peak context</th><th class=\"num\">median calls</th></tr>") % R.esc(R.COST_NOTE)
    rows = []
    for ar, gs in _area_groups(d):
        ra, rb = sum(g["a"]["runs"] for g in gs), sum(g["b"]["runs"] for g in gs)
        rows.append('<tr class="arow"><td colspan="11"><span class="ar-n">%s</span> <span class="muted">%s · runs '
                    '<span class="va">%d</span>/<span class="vb">%d</span></span></td></tr>'
                    % (R.esc(ar["name"]), _pl(len(gs), "goal"), ra, rb))
        for g in gs:
            a, b = g["a"], g["b"]
            one = not (a["runs"] and b["runs"])
            if one:
                chk = '<a class="tag warnt" href="#%s" title="members only on one side: not compared">⚠ %s only</a>' % (
                    _anchor(g, "pd-"), "A" if a["runs"] else "B")
            elif g["same_prompts"]:
                chk = '<span class="tag ok" title="identical prompt texts in A and B">✓ same</span>'
            else:
                chk = '<a class="tag warnt" href="#%s" title="prompt texts differ: see below">⚠ differ</a>' % (
                    _anchor(g, "pd-"))
            fin = '<span class="va">%s</span><span class="arr">→</span><span class="vb">%s</span>' % (
                _pc(a["finish_rate"]), _pc(b["finish_rate"]))
            cells = ['<td id="%s">%s</td>' % (_anchor(g, "cmp-"), R.esc(g["goal"])), "<td>%s</td>" % chk,
                     '<td class="num"><span class="va">%d</span><span class="arr">/</span><span class="vb">%d</span></td>'
                     % (a["runs"], b["runs"]), '<td class="num">%s</td>' % fin]
            for key, fmt in (("cost_weighted", fmt_tok), ("new_input", fmt_tok), ("output", fmt_tok),
                             ("cache_read", fmt_tok), ("all_in", fmt_tok), ("peak_context", fmt_tok), ("calls", _n1)):
                if one:
                    side = a if a["runs"] else b
                    cells.append('<td class="num"><span class="%s">%s</span>%s</td>' % (
                        "va" if a["runs"] else "vb", fmt(side[key]["median"]),
                        '<div class="rng">%s</div>' % _rng(side[key], fmt)))
                else:
                    cells.append('<td class="num">%s%s</td>' % (
                        _ab(a[key]["median"], b[key]["median"], g["delta_pct"][key], fmt), _abr(a[key], b[key], fmt)))
            rows.append('<tr class="%s">%s</tr>' % ("one" if one else "", "".join(cells)))
    return ('<div class="tscroll"><table class="compact sets"><thead>%s</thead><tbody>%s</tbody></table></div>'
            % (head, "".join(rows)))


def _prompt_warnings(d):
    diff = [g for g in d["goals"] if not g["same_prompts"]]
    if not diff:
        return ('<p class="muted small">Same prompts: every goal has the identical set of prompt texts in A and B, '
                'so differences come from the setup, not the task.</p>')
    out = []
    for g in diff:
        why = ("only one side has runs" if not (g["a"]["runs"] and g["b"]["runs"]) else
               "the prompt texts are not identical")
        items = "".join('<div><span class="ab">only in A</span> <span class="pa">%s</span></div>' % R.esc(R.short(p, 220))
                        for p in g["only_a"])
        items += "".join('<div><span class="ab">only in B</span> <span class="pb">%s</span></div>' % R.esc(R.short(p, 220))
                         for p in g["only_b"])
        out.append('<div class="pdiff" id="%s"><b>⚠ %s: prompts differ</b> <span class="muted">– %s, so part of '
                   'the difference may come from the task itself.</span>%s</div>' % (_anchor(g, "pd-"), R.esc(g["goal"]),
                                                                                    why, items))
    return "".join(out)


PATHS_PER_GOAL = 4   # paths listed per goal in the compare path diff


def path_diff(g, top=PATHS_PER_GOAL):
    """[{chain, a, b, a_pct, b_pct, status}] for the busiest paths of one goal on either side, + count of the rest.
    status: 'gone in B' / 'new in B' / ''."""
    pa, pb = g["a"].get("paths") or {}, g["b"].get("paths") or {}
    na, nb = g["a"]["runs"], g["b"]["runs"]
    keys = sorted(set(pa) | set(pb), key=lambda c: (-max(pa.get(c, 0) / (na or 1), pb.get(c, 0) / (nb or 1)), c))
    out = []
    for c in keys[:top]:
        x, y = pa.get(c, 0), pb.get(c, 0)
        st = "gone in B" if x and not y and nb else "new in B" if y and not x and na else ""
        out.append({"chain": c, "a": x, "b": y, "a_pct": 100.0 * x / na if na else None,
                    "b_pct": 100.0 * y / nb if nb else None, "status": st})
    return out, max(0, len(keys) - top)


def _chain_table(d):
    rows = []
    for ar, gs in _area_groups(d):
        rows.append('<tr class="arow"><td colspan="4"><span class="ar-n">%s</span></td></tr>' % R.esc(ar["name"]))
        for g in gs:
            a, b = g["a"], g["b"]
            ha, hb = a["hook_fires_per_run"], b["hook_fires_per_run"]
            hooks = _ab(ha, hb, pct(ha, hb) if ha else (None if not hb else 100.0), lambda x: "%.1f" % x)
            rows.append('<tr class="pg"><td colspan="3"><a href="#%s">%s</a> <span class="muted small">distinct paths '
                        '<span class="va">%d</span> → <span class="vb">%d</span></span></td>'
                        '<td class="num"><span class="muted small">hooks/run</span> %s</td></tr>'
                        % (_anchor(g, "cmp-"), R.esc(g["goal"]), a["chains"], b["chains"], hooks))
            ps, more = path_diff(g)
            for p in ps:
                tag = ('<span class="tag ok">gone in B</span>' if p["status"] == "gone in B" else
                       '<span class="tag bad">new in B</span>' if p["status"] == "new in B" else "")
                cell = lambda v, n: '<span class="muted">–</span>' if v is None else \
                    '%d%% <span class="muted small">(%d)</span>' % (round(v), n)
                rows.append('<tr><td class="mono ch" title="%s">%s</td><td class="num va">%s</td>'
                            '<td class="num vb">%s</td><td>%s</td></tr>'
                            % (R.esc(p["chain"]), R.esc(R.short(p["chain"], 110)), cell(p["a_pct"], p["a"]),
                               cell(p["b_pct"], p["b"]), tag))
            if more:
                rows.append('<tr><td colspan="4" class="muted small">+%s</td></tr>' % _pl(more, "more path"))
    return ('<div class="tscroll"><table class="compact chains-t"><thead><tr><th>path (tool chain)</th>'
            '<th class="num">A · share of runs</th><th class="num">B · share of runs</th><th></th></tr></thead>'
            '<tbody>%s</tbody></table></div>' % "".join(rows))


def _ctx_bars(d):
    from .understand_html import CTX_SOURCES
    colors = {k: c for k, _, c in CTX_SOURCES}
    keys = _ctx_keys(d)
    tot = {k: sum(d["context"][k].get(key, 0) for key, _ in keys) for k in ("a", "b")}
    scale = max(tot.values()) or 1
    bars = []
    for k, tag in (("a", "A · before"), ("b", "B · after")):
        segs = "".join('<span style="width:%.3f%%;background:%s" title="%s ~%s"></span>' % (
            100.0 * d["context"][k].get(key, 0) / scale, colors[key], R.esc(lab), fmt_tok(int(d["context"][k].get(key, 0))))
            for key, lab in keys if d["context"][k].get(key, 0) > 0)
        bars.append('<div class="cbrow"><div class="cbl"><b>%s</b></div><div class="cbt"><div class="hbar cb">%s</div>'
                    '</div><div class="cbn n">~%s</div></div>' % (tag, segs, fmt_tok(int(tot[k]))))
    rows = []
    for key, lab in keys:
        va, vb = d["context"]["a"].get(key, 0), d["context"]["b"].get(key, 0)
        g = _gone(va, vb)
        tag = ('<span class="tag ok">gone in B</span>' if g == "gone" else
               '<span class="tag bad">new in B</span>' if g == "new in B" else
               '<span class="%s">%s</span>' % ("ok" if vb < va * 0.995 else "hot" if vb > va * 1.005 else "muted", g))
        rows.append([('<i class="sw" style="background:%s"></i>%s' % (colors[key], R.esc(lab)), None),
                     (R.num(va, est=True), va), (R.num(vb, est=True), vb), (tag, None)])
    leg = "".join('<span><i class="sw" style="background:%s"></i>%s</span>' % (colors[key], R.esc(lab)) for key, lab in keys)
    return ('<div class="legend">%s</div>%s<div style="height:8px"></div>%s' % (
        leg, "".join(bars), R.table([("source", False), ("A median / run", True), ("B median / run", True),
                                      ("change", False)], rows, "compact")))


def _fix_sav(f):
    """'~26.3k cost-weighted · ~15.0k new · $0.08' (estimates), or '–'."""
    if not (f.get("savings_new_tokens") or f.get("savings_all_in_tokens")):
        return "–"
    out = "~%s cost-weighted · ~%s new" % (fmt_tok(int(round(fixes_mod.cw_of(f)))), fmt_tok(f["savings_new_tokens"]))
    if f.get("savings_usd") is not None:
        out += " · %s%s" % ("≥" if f.get("savings_usd_partial") else "", R.fmt_usd(f["savings_usd"]))
    return out


def _fix_list(fs):
    if not fs:
        return '<p class="muted">none</p>'
    return "<ul class=\"fl\">%s</ul>" % "".join(
        '<li><span class="sev %s">%s</span> %s <span class="muted n" title="all-in incl. carry ~%s">%s</span></li>' % (
            f["severity"], R.SEV_LABEL[f["severity"]], R.esc(f["title"]), fmt_tok(f.get("savings_all_in_tokens", 0)),
            R.esc(_fix_sav(f))) for f in fs)


def render_sets_html(d, report_link=None):
    from . import understand_html as uh
    oa, ob = d["overall"]["a"], d["overall"]["b"]
    rng = lambda k: "%s – %s" % (R.t_full(d["range"][k][0]), R.t_hms(d["range"][k][1])[:5])
    body = ['<section id="compare" class="part"><h2 class="part-h"><span class="pn">4</span> Compare '
            '<span class="muted">same prompts, before (A) vs after (B)</span></h2>',
            '<div class="headline">%s</div>' % R.esc(d["headline"]).replace("-&gt;", "→"),
            '<p class="muted small">%s</p>' % R.esc(sets_totals_line(d)).replace("-&gt;", "→"),
            '<p class="muted small">Median cost-weighted tokens per run over all runs of the goals present on both '
            'sides; one run = one request. %s Projects: %s. Goals discovered on A + B together (same prompt, same '
            'goal on both sides), %s.</p>%s' % (R.esc(R.COST_NOTE), R.esc(", ".join(d["projects"])),
                                                R.esc(gl.name_note(d["namer"])),
                           "".join('<div class="pdiff"><b>⚠ one side only</b> <span class="muted">%s</span></div>'
                                   % R.esc(c["text"]) for c in d["checks"])),
            '<div class="grid2">%s%s</div>' % (
                '<div class="cmph"><span class="ab" style="color:%s">A · before</span> %s in %s &middot; %s</div>' % (
                    A_COL, _pl(oa["runs"], "run"), _pl(len(d["ids"]["a"]), "session"), rng("a")),
                '<div class="cmph"><span class="ab" style="color:%s">B · after</span> %s in %s &middot; %s</div>' % (
                    B_COL, _pl(ob["runs"], "run"), _pl(len(d["ids"]["b"]), "session"), rng("b")))]
    ta, tb = d["totals"]["a"], d["totals"]["b"]
    usd_of = lambda m: m["usd"] if m["usd"] is not None and m["calls"] > m["unpriced_calls"] else None
    tiles = [("median cost-weighted / run", d["median_cost_weighted"]["a"], d["median_cost_weighted"]["b"], fmt_tok),
             ("API-equivalent $ (all sessions)", usd_of(ta), usd_of(tb), R.fmt_usd),
             ("median new / run", oa["new_input"]["median"], ob["new_input"]["median"], fmt_tok),
             ("median output / run", oa["output"]["median"], ob["output"]["median"], fmt_tok),
             ("median cache reads / run", oa["cache_read"]["median"], ob["cache_read"]["median"], fmt_tok),
             ("median all-in / run", d["median_all_in"]["a"], d["median_all_in"]["b"], fmt_tok),
             ("median peak context", oa["peak_context"]["median"], ob["peak_context"]["median"], fmt_tok),
             ("median calls / run", oa["calls"]["median"], ob["calls"]["median"], _n1),
             ("hook fires / run", oa["hook_fires_per_run"], ob["hook_fires_per_run"], lambda x: "%.1f" % x)]
    cells = []
    for lab, va, vb, fmt in tiles:
        p = pct(va, vb)
        cls = "muted" if p is None or abs(p) < 0.5 else "ok" if p < 0 else "hot"
        kc = " head" if lab.startswith(("median cost-weighted", "API-equivalent")) else \
            " dim" if lab.startswith("median all-in") else ""
        cells.append('<div class="kpi%s"><div class="l">%s</div><div class="v"><span class="n">%s</span>'
                     '<span class="d %s">%s</span></div><div class="s">A <span class="n">%s</span> → B '
                     '<span class="n">%s</span></div></div>' % (kc, lab, fmt(vb), cls, _pct_txt(p), fmt(va), fmt(vb)))
    cells.append('<div class="kpi"><div class="l">finish rate</div><div class="v"><span class="n">%s</span></div>'
                 '<div class="s">A <span class="n">%s</span> → B <span class="n">%s</span></div></div>' % (
                     _pc(ob["finish_rate"]), _pc(oa["finish_rate"]), _pc(ob["finish_rate"])))
    cells.append('<div class="kpi"><div class="l">fixes triggered</div><div class="v"><span class="n">%d</span>'
                 '<span class="d ok">%s</span></div><div class="s">A <span class="n">%d</span> → B <span class="n">%d'
                 '</span></div></div>' % (len(d["fixes_b"]), "−%d" % len(d["fixes_resolved"]) if d["fixes_resolved"]
                                         else "", len(d["fixes_a"]), len(d["fixes_b"])))
    body.append('<div class="kpis">%s</div>' % "".join(cells))
    n_req = d["overall"]["a"]["runs"] + d["overall"]["b"]["runs"]
    body.append('<div class="panel"><div class="u-head"><h2>Per goal <span class="muted">A → B medians per run; '
                'second line: min–max A | B; green = lower in B</span></h2><div class="cap">%s IN %s · DISCOVERED '
                'FROM %s (A ∪ B)</div></div>%s%s</div>'
                % (_pl(len(d["goals"]), "goal").upper(), _pl(len(_area_groups(d)), "area").upper(),
                   _pl(n_req, "request").upper(), _goal_table(d), _prompt_warnings(d)))
    body.append('<div class="panel"><h2>Paths per goal <span class="muted">the tool chains of each goal: share of '
                'its A runs vs its B runs; hook fires per run</span></h2>%s</div>' % _chain_table(d))
    body.append('<div class="panel"><h2>Context by source <span class="muted">median per run of the context sent, by '
                'source (sum over the run\'s API calls, incl. cache reads); split estimated (~), scaled to measured'
                '</span></h2>%s</div>' % _ctx_bars(d))
    body.append('<div class="grid2"><div class="panel"><h3>Fixes from A that no longer trigger in B <span class="muted">'
                '%d of %d</span></h3>%s</div><div class="panel"><h3>Still triggering in B <span class="muted">%d</span>'
                '</h3>%s</div></div>' % (len(d["fixes_resolved"]), len(d["fixes_a"]), _fix_list(d["fixes_resolved"]),
                                         len(d["fixes_b"]), _fix_list(d["fixes_b"])))
    body.append("</section>")
    html = R.page("".join(body), "compare %d vs %d sessions" % (len(d["ids"]["a"]), len(d["ids"]["b"])),
                  nav=uh.nav(report_link=report_link, active="compare"))
    return html.replace("</style>", SETS_CSS + "</style>", 1)


SETS_CSS = (
    ".headline{font-size:23px;font-weight:600;margin:6px 0 4px}"
    ".cmph{background:var(--panel);border:1px solid var(--border);border-radius:8px;padding:9px 14px;margin:6px 0;"
    "font-size:14.5px}.ab{font-weight:700;font-family:var(--mono)}"
    ".kpis .kpi .v{font-size:21px}"
    "table.sets td{vertical-align:top;padding:7px 6px}table.sets td:first-child{font-weight:600;min-width:190px}"
    "table.sets td.num{font-size:13px}table.sets .rng{font-size:11.5px}table.sets th{white-space:normal}"
    "tr.arow td{padding:14px 8px 5px!important;border-bottom:1px solid #30363d;background:none!important}"
    "tr.arow .ar-n{font:600 13px var(--mono);letter-spacing:1.2px;text-transform:uppercase;color:#e6edf3;"
    "white-space:nowrap}"
    "table.sets tr.one td{color:#8b949e}table.sets tr.one td:first-child{color:#c9d1d9}"
    "table.chains-t tr.pg td{padding-top:9px;font-weight:600}table.chains-t tr.pg a{color:#e6edf3}"
    "table.chains-t td.ch{padding-left:22px;font-weight:400!important;white-space:normal!important}"
    "table.chains-t td.va{color:#ffb38a}table.chains-t td.vb{color:#9cc4ff}"
    ".u-head{display:flex;justify-content:space-between;gap:16px;align-items:flex-start}"
    ".u-head .cap{font:12px var(--mono);letter-spacing:1.2px;color:#8b949e;white-space:nowrap;padding-top:4px}"
    "table.sets .va,table.chains-t .va{color:#ffb38a}table.sets .vb,table.chains-t .vb{color:#9cc4ff}"
    ".arr{color:#6e7681;margin:0 4px}.dl{font-weight:600;font-size:13px;margin-left:2px}"
    ".rng{font-size:12.5px;color:var(--muted);margin-top:2px}.rng .sep{color:#484f58}"
    ".tag.warnt{border-color:#bb8009;color:#e3b341}"
    ".pdiff{border:1px solid #bb8009;background:#bb800914;border-radius:8px;padding:7px 14px;margin:8px 0;"
    "font-size:13.5px}.pdiff>b{color:#e3b341}.pdiff .ab{display:inline-block;min-width:78px;font-size:12.5px;"
    "color:var(--muted)}.pdiff .pa{color:#ffb38a}.pdiff .pb{color:#9cc4ff}"
    "table.chains-t td{vertical-align:top;padding:7px 8px}table.chains-t td:first-child{font-weight:600;white-space:nowrap}"
    "table.chains-t .ch{font-size:12.5px;color:#c9d1d9}table.chains-t .pc{font-family:var(--mono);color:#e3b341;"
    "font-weight:600}"
    ".cbrow{display:grid;grid-template-columns:110px 1fr 70px;gap:10px;align-items:center;margin:6px 0}"
    ".hbar.cb{height:22px;margin:0}.cbn{text-align:right}"
    "ul.fl{list-style:none;padding:0;margin:0;font-size:14px}ul.fl li{margin:5px 0}"
    ".small{font-size:13px}"
    "nav.steps a.on{border-color:#8b949e;background:#21262d;color:#fff}")
