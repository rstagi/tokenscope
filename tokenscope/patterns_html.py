"""Patterns section of the HTML report (patterns.mine output) + drill-down replays + 'where the money goes'."""
from . import pricing
from .report import esc, num, fmt_usd, kpi, short, table

KIND_ORDER = ["recurring_error", "retry_loops", "workflow_fanout", "session_start_rediscovery", "wasted_context",
              "repeated_sequence", "tool_choice", "divergent_paths", "user_corrections", "compactions",
              "tool_overhead"]
KIND_TITLE = {"recurring_error": "Recurring errors", "retry_loops": "Retry loops",
              "workflow_fanout": "Workflow fan-out", "session_start_rediscovery": "Re-discovery at start (incl. fan-out)",
              "wasted_context": "Wasted context", "repeated_sequence": "Repeated sequences",
              "tool_choice": "Tool choice", "divergent_paths": "Divergent paths",
              "user_corrections": "User corrections", "compactions": "Compactions",
              "tool_overhead": "Tool overhead"}
ROWS = 8
REPLAY_SPAN = 6


def _did(p):
    return "".join(ch if ch.isalnum() or ch in "-_" else "-" for ch in str(p["id"]))[:60]


# ---------------------------------------------------------------- replays (built before --redact)
def attach_replays(d, records):
    """Add p['replays'] = [{session, agent, call, steps:[{label, kind, err, new, out, cr, cw, hit}]}] for each drill
    ref, from the compact per-call tool records (short heads only)."""
    trs = {(tr["sid"], tr["agent"]): tr for rec in records for tr in rec["transcripts"]}
    for p in d["patterns"]:
        out = []
        for ref in p.get("drill") or []:
            tr = trs.get((ref.get("session_id"), ref.get("transcript")))
            if tr is None:
                out.append(None)
                continue
            tools = tr["tools"]
            ci = ref.get("call_index")
            at = next((i for i, t in enumerate(tools) if ci is not None and t["call"] >= ci), 0)
            lo, hi = max(0, at - REPLAY_SPAN), min(len(tools), at + REPLAY_SPAN + 1)
            out.append({"session_id": ref["session_id"], "agent": tr["label"], "call": ci,
                        "steps": [{"label": short(t["fine"], 90), "kind": t["coarse"], "err": bool(t["err"]),
                                   "call": t["call"], "new": int(t["new"]), "out": int(round(t["out"])),
                                   "cr": int(round(t["cr"])), "cw": int(round(t["cw"])), "hit": i == at}
                                  for i, t in enumerate(tools[lo:hi], lo)]})
        p["replays"] = out
    return d


def _replay(p, i, r):
    if r is None:
        return '<p class="muted small" id="drill-%s-%d">transcript not in this selection</p>' % (_did(p), i)
    rows = "".join(
        '<tr class="%s"><td class="r mono">%s</td><td>%s</td><td class="mono">%s</td><td>%s</td>'
        '<td class="r">%s</td><td class="r">%s</td><td class="r">%s</td><td class="r">%s</td></tr>'
        % ("hit" if s["hit"] else "", s["call"], esc(s["kind"]), esc(s["label"]),
           '<span class="err">error</span>' if s["err"] else '<span class="ok">ok</span>',
           num(s["new"]), num(s["out"]), num(s["cr"]), num(s["cw"])) for s in r["steps"])
    return ('<details class="drill" id="drill-%s-%d"><summary>replay %d &middot; <span class="mono">%s</span> '
            '&middot; %s &middot; call %s</summary><table class="t small"><tr><th>call</th><th>kind</th>'
            '<th>step</th><th></th><th>new</th><th>output</th><th>cache read</th><th>cost-wtd</th></tr>%s</table>'
            '</details>' % (_did(p), i, i + 1, esc(str(r["session_id"])[:8]), esc(r["agent"]),
                            esc(r["call"]), rows))


# ---------------------------------------------------------------- cards
def _sav(s):
    sv = (s or {}).get("est_savings")
    if not sv or not (sv.get("new") or sv.get("cost_weighted") or sv.get("usd")):
        return '<span class="muted">est. savings: not estimated</span>'
    return ('est. savings: new %s &middot; cost-weighted %s &middot; %s <span class="muted">(%s)</span>'
            % (num(sv.get("new", 0)), num(sv.get("cost_weighted", 0)), esc(fmt_usd(sv.get("usd"))),
               esc(sv.get("formula") or "no formula")))


def _detail(p):
    s = p.get("suggestion") or {}
    ev = "".join("<li>%s</li>" % esc(e) for e in p.get("evidence") or [])
    ex = "".join("<li><span class=\"mono\">%s</span> %s &middot; call %s &middot; %s</li>"
                 % (esc(e.get("session")), esc(e.get("agent")), esc(e.get("call")), esc(e.get("label")))
                 for e in p.get("examples") or [])
    links = " ".join('<a href="#drill-%s-%d">replay %d</a>' % (_did(p), i, i + 1)
                     for i in range(len(p.get("replays") or [])))
    reps = "".join(_replay(p, i, r) for i, r in enumerate(p.get("replays") or []))
    return ('<div class="pdet">%s%s<p>%s</p><p class="small">%s</p>%s<p class="small">drill: %s</p>%s</div>'
            % ("<p class=\"small muted\">evidence</p><ul class=\"small\">%s</ul>" % ev if ev else "",
               "<p class=\"small muted\">examples</p><ul class=\"small\">%s</ul>" % ex if ex else "",
               esc(s.get("text") or "no suggestion"), _sav(s),
               '<p class="small muted">confidence: %s</p>' % esc(p.get("confidence")),
               links or '<span class="muted">none</span>', reps))


def _row(p):
    t = p["tokens"]
    s = p.get("suggestion") or {}
    chip = '<span class="pchip">%s</span>' % esc(s["type"]) if s.get("type") else ""
    usd = p.get("api_cost_usd") or 0
    return [('<details><summary>%s %s</summary>%s</details>' % (esc(short(p["title"], 110)), chip, _detail(p)),
             p["title"].lower()),
            (str(p["count"]), p["count"]), (str(p["sessions"]), p["sessions"]), (str(p["projects"]), p["projects"]),
            (num(t["new"]), t["new"]), (num(t["output"]), t["output"]), (num(t["cache_read"]), t["cache_read"]),
            (num(t["cost_weighted"]), t["cost_weighted"]), (esc(fmt_usd(usd)), usd)]


HEAD = [("pattern", False), ("count", True), ("sessions", True), ("projects", True), ("new", True), ("output", True),
        ("cache read", True), ("cost-wtd", True), ("$", True)]


def _card(kind, ps):
    ps = sorted(ps, key=lambda p: -p["tokens"]["cost_weighted"])
    cw = sum(p["tokens"]["cost_weighted"] for p in ps)
    usd = sum(p.get("api_cost_usd") or 0 for p in ps)
    body = table(HEAD, [_row(p) for p in ps[:ROWS]], cls="ptab", sortable=True)
    if len(ps) > ROWS:
        body += '<details class="more"><summary>+%d more</summary>%s</details>' % (
            len(ps) - ROWS, table(HEAD, [_row(p) for p in ps[ROWS:]], cls="ptab", sortable=True))
    return ('<div class="panel pcard" id="pk-%s"><h3>%s <span class="muted">%d &middot; %s cost-weighted &middot; '
            '%s</span></h3>%s</div>' % (kind, esc(KIND_TITLE.get(kind, kind)), len(ps), num(cw), esc(fmt_usd(usd)),
                                        body))


def cost_split(analyses):
    """{main|task|workflow: (cost_weighted, usd)} from the analyzer."""
    out = {"main": [0.0, 0.0], "task": [0.0, 0.0], "workflow": [0.0, 0.0]}
    for a in analyses:
        m = a["totals"]["main"]
        out["main"][0] += m["cost_weighted"]
        out["main"][1] += m["api_cost_usd"] or 0
        for sa in a["subagents"]:
            k = "workflow" if sa.get("workflow_id") else "task"
            out[k][0] += sa["tokens"]["cost_weighted"]
            out[k][1] += sa["tokens"]["api_cost_usd"] or 0
    return out


SPLIT_LABEL = {"main": "main", "task": "Task subagents", "workflow": "workflow agents"}


def patterns_section(d, analyses):
    ps = d.get("patterns") or []
    by = {}
    for p in ps:
        by.setdefault(p["kind"], []).append(p)
    cw = sum(p["tokens"]["cost_weighted"] for p in ps)
    usd = sum(p.get("api_cost_usd") or 0 for p in ps)
    kinds = " &middot; ".join('<a href="#pk-%s">%s %d</a>' % (k, esc(k), len(by[k])) for k in KIND_ORDER if k in by)
    split = cost_split(analyses)
    tot = sum(v[0] for v in split.values()) or 1
    cells = [kpi("patterns", '<span class="n">%d</span>' % len(ps), kinds or "none found"),
             kpi("involved", num(cw), "%s &middot; patterns can overlap" % esc(fmt_usd(usd)),
                 title="sum over patterns of the tokens of the tool calls each pattern covers")]
    cells += [kpi(SPLIT_LABEL[k] + " cost", num(v[0]), "%d%% &middot; %s" % (round(100 * v[0] / tot), esc(fmt_usd(v[1]))))
              for k, v in split.items()]
    cards = "".join(_card(k, by[k]) for k in KIND_ORDER if k in by)
    cards += "".join(_card(k, v) for k, v in by.items() if k not in KIND_ORDER)
    notes = "".join("<li>%s</li>" % esc(n.replace("https://", "")) for n in d.get("token_notes") or [])
    return ('<section id="patterns" class="part"><h2 class="part-h"><span class="pn">2</span> Patterns <span '
            'class="muted">what keeps repeating across sessions</span></h2><div class="kpis">%s</div>%s'
            '<details class="panel"><summary class="muted small">how tokens are counted</summary><ul class="small">'
            '%s</ul></details></section>' % ("".join(cells), cards or '<p class="muted">No patterns found.</p>', notes))


# ---------------------------------------------------------------- where the money goes
def _iter_calls(a):
    for c in a["calls"]:
        yield "main", c
    for sa in a["subagents"]:
        for c in sa["call_records"]:
            yield "workflow" if sa.get("workflow_id") else "task", c


def money(analyses):
    """$ and cost-weighted by token type, and $ per model (priced per call with pricing.PRICES)."""
    types = {k: [0, 0.0, 0.0] for k in ("input", "cache write 5m", "cache write 1h", "cache read", "output")}
    models = {}
    for a in analyses:
        for _, c in _iter_calls(a):
            u, p = c["usage"], pricing.price_of(c.get("model"))
            w1h = u.get("cache_write_1h", 0)
            w5m = max(u.get("cache_write", 0) - w1h, 0)
            for k, n, w, i in (("input", u.get("input", 0), pricing.W_INPUT, 0),
                               ("cache write 5m", w5m, pricing.W_CACHE_WRITE, 1),
                               ("cache write 1h", w1h, pricing.W_CACHE_WRITE, 2),
                               ("cache read", u.get("cache_read", 0), pricing.W_CACHE_READ, 3),
                               ("output", u.get("output", 0), pricing.W_OUTPUT, 4)):
                types[k][0] += n
                types[k][1] += n * w
                types[k][2] += n * p[i] / 1e6 if p else 0
            m = models.setdefault(str(c.get("model")), [0, 0.0, p is not None])
            m[0] += 1
            m[1] += pricing.cost_usd(c.get("model"), u) or 0
    return types, models


def money_table(analyses):
    split = cost_split(analyses)
    types, _ = money(analyses)
    r1 = "".join('<tr><td>%s</td><td class="r">%s</td><td class="r">%s</td></tr>'
                 % (SPLIT_LABEL[k], num(v[0]), esc(fmt_usd(v[1]))) for k, v in split.items())
    r2 = "".join('<tr><td>%s</td><td class="r">%s</td><td class="r">%s</td><td class="r">%s</td></tr>'
                 % (k, num(v[0]), num(v[1]), esc(fmt_usd(v[2]))) for k, v in types.items())
    return ('<div class="panel u-panel money"><div class="lab">WHERE THE MONEY GOES</div><div class="grid2"><table '
            'class="t small"><tr><th>transcript kind</th><th class="r">cost-weighted</th><th class="r">$</th></tr>%s'
            '</table><table class="t small"><tr><th>token type</th><th class="r">tokens</th><th class="r">'
            'cost-weighted</th><th class="r">$</th></tr>%s</table></div><p class="muted small">$ = API-equivalent '
            '(%s); cost-weighted = %s</p></div>' % (r1, r2, esc(pricing.PRICES_SOURCE.replace("https://", "")),
                                                   esc(pricing.COST_WEIGHTED_FORMULA)))


def corpus_kpis(analyses):
    """Extra KPI line: transcripts by kind, date range, models with $."""
    n_task = sum(1 for a in analyses for sa in a["subagents"] if not sa.get("workflow_id"))
    n_wf = sum(1 for a in analyses for sa in a["subagents"] if sa.get("workflow_id"))
    starts = sorted(a["start"] for a in analyses if a.get("start"))
    _, models = money(analyses)
    ms = sorted(models.items(), key=lambda kv: -kv[1][1])
    mtxt = " &middot; ".join("<span class=\"mono\">%s</span> %s%s" % (esc(k), esc(fmt_usd(v[1])),
                                                                      "" if v[2] else " (unpriced)")
                             for k, v in ms[:8])
    return ('<div class="panel srcs"><span class="srcl"><b>Transcripts</b> <span class="n">%d</span> (main %d / Task '
            'subagents %d / workflow agents %d)</span> &nbsp;&nbsp; <span class="srcl"><b>Dates</b> %s &rarr; %s</span>'
            ' &nbsp;&nbsp; <span class="srcl"><b>Models</b> %s</span></div>'
            % (len(analyses) + n_task + n_wf, len(analyses), n_task, n_wf, esc(starts[0][:10] if starts else "?"),
               esc(starts[-1][:10] if starts else "?"), mtxt or "none"))


CSS = """
.pchip{font:11px var(--mono);border:1px solid #30363d;border-radius:10px;padding:0 6px;margin-left:6px;color:#d2a8ff}
.pcard h3{margin:4px 0 8px}.ptab td{vertical-align:top}.ptab summary{cursor:pointer}
.pdet{padding:6px 10px;border-left:2px solid #30363d;margin:6px 0}.pdet ul{margin:2px 0 6px 16px;padding:0}
.drill table{margin:4px 0}.drill tr.hit td{background:#1f2a3a}.drill .err{color:#f85149}.drill .ok{color:#3fb950}
"""
