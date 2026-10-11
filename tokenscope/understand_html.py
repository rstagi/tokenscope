"""HTML for the Understand part of the report: what the agent did, before where the tokens went.

Built only from `understand.understand()` data plus the per-session analyses (for offered-vs-used). Everything is
server-side HTML/SVG; chip filters and the per-run/cumulative toggle are pure CSS (radio + :checked).
"""
import hashlib
import math
import os
import re
import statistics
from collections import Counter, OrderedDict

from . import goals as gl
from . import understand as un
from .cli import fmt_tok
from .report import _nice, esc, fix_anchor, num, short, t_hms, tick

FLOW_W = 1180
FLOW_NODES = 4          # nodes per flow column before the rest folds into "other"
FLOW_DIM = 0.20         # nodes/edges below this share of their column are dimmed
CHIPS = 8               # chain chips before the rest folds into one "other" chip
EXAMPLES = 20           # example rows per goal
REPLAY_STEPS = 30       # steps shown in one replay before the middle is elided
CTX_W, CTX_PH = 1180, 230

STATUS_CLS = {"Healthy": "st-ok", "Costly": "st-cost", "Failing": "st-fail", "Looping": "st-loop"}
OUTCOME_ICON = {"success": ("✓", "oc-ok", "success"), "failed": ("✗", "oc-bad", "failed"),
                "looping": ("↻", "oc-loop", "looping"), "abandoned": ("…", "oc-ab", "abandoned")}
ACCENT = "#d4a72c"      # main path in the flow diagram

# Context sources, bottom of the stack first. Colored sources take the validated 8-slot dark categorical order in
# stack order (adjacent pairs pass CVD / normal-vision separation); system prompt and other are neutral greys.
CTX_SOURCES = [
    ("system_prompt", "System prompt", "#8b949e"),
    ("tool_definitions", "Tool definitions", "#3987e5"),
    ("skills", "Skills", "#d95926"),
    ("mcp", "MCP", "#199e70"),
    ("instructions", "Instructions", "#c98500"),
    ("hook_output", "Hook output", "#d55181"),
    ("tool_results", "Tool results", "#008300"),
    ("user_input", "User input", "#9085e9"),
    ("assistant_history", "Assistant history", "#e66767"),
    ("other", "Other", "#484f58"),
]


def _ordinal(n):
    return "%d%s" % (n, "TH" if 10 <= n % 100 <= 20 else {1: "ST", 2: "ND", 3: "RD"}.get(n % 10, "TH"))


def _pct(a, b):
    return round(100.0 * a / b) if b else 0


def _med(xs):
    return statistics.median(xs) if xs else 0


def _n(x):
    return ("%d" % x) if float(x).is_integer() else ("%.1f" % x)


def _plural(n, word, pl=None):
    return "%d %s" % (n, word if n == 1 else (pl or word + "s"))


# ---------------------------------------------------------------- narration

def _phrase(s):
    """(verb, object) for one tool step; consecutive steps with the same verb share it in the summary."""
    k, t = s["kind"], s["target"] or ""
    if k.startswith("Bash("):
        return "ran", k
    if k in ("Edit", "MultiEdit"):
        return "edited", t or "a file"
    if k == "Write":
        return "wrote", t or "a file"
    if k == "Read":
        return "read", t or "a file"
    if k == "Grep":
        return "searched", '"%s"' % short(t, 30)
    if k == "Glob":
        return "listed", short(t, 30)
    if k.startswith("Agent("):
        return "delegated to", "a %s subagent" % k[6:-1]
    if k.startswith("Skill("):
        return "used skill", k[6:-1]
    if k.startswith("MCP("):
        return "called", k
    if k == "Todo":
        return "updated", "the todo list"
    return "used", k


def narrate(req, max_parts=6):
    """'The agent ran Bash(ls), then Bash(cat), then edited src/pricing.js 3× …' from the request's steps."""
    tools = [s for s in req["steps"] if not un.is_hook(s)]
    if not tools:
        return "The agent answered without using tools." if req["outcome"] != "abandoned" else \
            "No tool steps and no final answer."
    groups = []
    for s in tools:
        p = _phrase(s)
        if groups and groups[-1][0] == p:
            groups[-1][1] += 1
        else:
            groups.append([p, 1])
    parts, prev_verb = [], None
    for (verb, obj), n in groups[:max_parts]:
        txt = obj if verb == prev_verb else "%s %s" % (verb, obj)
        parts.append(txt + (" %d×" % n if n > 1 else ""))
        prev_verb = verb
    out = "The agent " + ", then ".join(parts)
    if len(groups) > max_parts:
        out += ", … (+%d more)" % sum(n for _, n in groups[max_parts:])
    hooks = [s for s in req["steps"] if un.is_hook(s)]
    if hooks:
        out += "; hooks fired %d×" % len(hooks)
    lt = un.last_test(req["steps"])
    if lt:
        out += "; last tests: %s" % un.test_text(lt["tests"])
    return out + "."


def outcome_icon(req):
    key = "looping" if "looping" in req["flags"] else req["outcome"]
    ic, cls, lab = OUTCOME_ICON.get(key, OUTCOME_ICON["abandoned"])
    if key == "looping":
        lab = "looping (outcome: %s)" % req["outcome"]
    if "maybe_in_progress" in req["flags"]:
        lab += ", may still be running"
    return '<span class="oc %s" title="%s">%s</span>' % (cls, esc(lab), ic)


STEP_ICON = [("Bash(", "$"), ("Read", "≡"), ("Edit", "✎"), ("MultiEdit", "✎"), ("Write", "✎"), ("Grep", "/"),
             ("Glob", "/"), ("Agent(", "@"), ("Skill(", "*"), ("MCP(", "M"), ("Hook(", "↳"), ("Todo", "☐")]


def _step_icon(k):
    return next((g for p, g in STEP_ICON if k.startswith(p)), "•")


def step_text(s):
    k, t = s["kind"], s["target"] or ""
    tests = (" — " + un.test_text(s["tests"])) if s.get("tests") else (" — error" if s["ok"] is False else "")
    if un.is_hook(s):
        return 'hook <span class="mono">%s</span>%s' % (esc(s["hook_name"]), esc(tests.replace(" — ", " → ")))
    if k.startswith("Bash("):
        return 'ran <span class="mono">%s</span>%s' % (esc(short(k, 60)), esc(tests))
    verb, obj = _phrase(s)
    extra = (" — " + s["detail"]) if s.get("detail") and k.startswith("Agent(") else ""
    return '%s <span class="mono">%s</span>%s' % (esc(verb), esc(short(obj, 70)), esc(tests or extra))


def replay_html(req, session_href=None):
    steps = req["steps"]
    shown = steps
    if len(steps) > REPLAY_STEPS:
        shown = steps[:REPLAY_STEPS - 6] + [None] + steps[-6:]
    vmax = max([s["tokens"] for s in steps] or [0]) or 1
    rows = []
    for s in shown:
        if s is None:
            rows.append('<li class="gap">… %d more steps …</li>' % (len(steps) - REPLAY_STEPS + 1))
            continue
        title = s["target"] if s["kind"].startswith("Bash(") or un.is_hook(s) else s.get("label", "")
        rows.append(
            '<li class="%s" title="%s"><span class="si">%s</span><span class="st">%s</span>'
            '<span class="sb"><i style="width:%.1f%%"></i></span><span class="sn">%s</span></li>'
            % ("hk" if un.is_hook(s) else "", esc(short(title, 400)), _step_icon(s["kind"]), step_text(s),
               100.0 * s["tokens"] / vmax, num(s["tokens"], est=s["est"])))
    prompt = " ".join((req["prompt"] or "(no prompt)").split())
    sig = "".join("<li>%s</li>" % esc(x) for x in req["signals"])
    link = ('<a class="open" href="%s">open in session view →</a>' % esc(session_href)) if session_href else ""
    return (
        '<div class="replay"><div class="rp-prompt" title="%s"><span class="lab">PROMPT</span> “%s”</div>'
        '<ol class="steps">%s</ol>'
        '<div class="rp-end">%s <b>%s</b> · %s calls · %s all-in · %s new%s<ul>%s</ul></div>%s</div>'
    ) % (esc(prompt[:3000]), esc(short(prompt, 200)), "".join(rows) or '<li class="gap">no tool steps</li>',
         outcome_icon(req), esc(req["outcome"]), req["calls"], num(req["tokens"]["all_in"]),
         num(req["tokens"]["new"]), (" · flags: " + esc(", ".join(req["flags"]))) if req["flags"] else "", sig, link)


# ---------------------------------------------------------------- flow diagram

def _flow_hooks(reqs, positions=un.FLOW_POSITIONS):
    """{(position, kind): hook fires} for hooks that followed a tool at that flow position."""
    out = Counter()
    for r in reqs:
        pos, prev = 0, None
        for s in r["steps"]:
            if un.is_hook(s):
                if prev is not None and pos <= positions:
                    out[(pos, prev)] += 1
                continue
            if s["kind"] != prev:
                pos += 1
                prev = s["kind"]
    return out


def flow_svg(reqs, W=FLOW_W):
    seqs = [r["flow"] for r in reqs if r["flow"]]
    if not seqs:
        return '<p class="muted">No tool calls in these requests, so there is no flow to draw.</p>'
    f = un.flow(seqs)
    hooks = _flow_hooks(reqs)
    ncol = max(n["position"] for n in f["nodes"])
    cols = {p: [n for n in f["nodes"] if n["position"] == p] for p in range(1, ncol + 1)}
    col_tot = {p: sum(n["count"] for n in ns) for p, ns in cols.items()}
    # fold low-ranked nodes into "other"
    shown, alias = {}, {}
    for p, ns in cols.items():
        keep = ns[:FLOW_NODES] if len(ns) <= FLOW_NODES + 1 else ns[:FLOW_NODES]
        rest = [n for n in ns if n not in keep]
        shown[p] = [dict(n, hooks=hooks.get((p, n["kind"]), 0)) for n in keep]
        if rest:
            shown[p].append({"position": p, "kind": "other (%d kinds)" % len(rest), "count": sum(n["count"] for n in rest),
                             "top_predecessor": None, "hooks": sum(hooks.get((p, n["kind"]), 0) for n in rest),
                             "other": True})
            for n in rest:
                alias[(p, n["kind"])] = shown[p][-1]["kind"]
    edges = Counter()
    for e in f["edges"]:
        a = alias.get((e["from_pos"], e["from_kind"]), e["from_kind"])
        b = alias.get((e["to_pos"], e["to_kind"]), e["to_kind"])
        edges[(e["from_pos"], a, b)] += e["count"]
    # main path: busiest first node, then the heaviest edge out of the current node
    path = {(1, shown[1][0]["kind"])}
    cur = shown[1][0]["kind"]
    main_edges = set()
    for p in range(1, ncol):
        outs = [(c, b) for (fp, a, b), c in edges.items() if fp == p and a == cur]
        if not outs:
            break
        c, nxt = max(outs)
        main_edges.add((p, cur, nxt))
        path.add((p + 1, nxt))
        cur = nxt

    gap = 34
    cw = min((W - gap * (ncol - 1)) / ncol, 240)
    nh, vs, top = 52, 20, 36
    rows = max(len(v) for v in shown.values())
    H = top + rows * (nh + vs) + 4
    pos = {}
    for p, ns in shown.items():
        for i, n in enumerate(ns):
            pos[(p, n["kind"])] = ((p - 1) * (cw + gap), top + i * (nh + vs))
    cmax = max(edges.values() or [1])
    chars = max(6, int((cw - 26) / 7.8))
    o = ['<svg class="flow" viewBox="0 0 %d %d" width="100%%" role="img" aria-label="tool chain flow">' % (W, H)]
    for p in range(1, ncol + 1):
        o.append('<text x="%.1f" y="14" class="fh">%s CALL · %d</text>' % ((p - 1) * (cw + gap) + 2, _ordinal(p),
                                                                          col_tot[p]))
    # edges below nodes
    for (fp, a, b), c in sorted(edges.items(), key=lambda kv: kv[1]):
        if (fp, a) not in pos or (fp + 1, b) not in pos:
            continue
        x1, y1 = pos[(fp, a)][0] + cw, pos[(fp, a)][1] + nh / 2
        x2, y2 = pos[(fp + 1, b)][0], pos[(fp + 1, b)][1] + nh / 2
        mx = (x1 + x2) / 2
        main = (fp, a, b) in main_edges
        dim = c < FLOW_DIM * col_tot[fp + 1]
        o.append('<path d="M%.1f %.1f C%.1f %.1f %.1f %.1f %.1f %.1f" class="fe%s%s" stroke-width="%.1f">'
                 '<title>%s → %s: %d of %d (%d%%)</title></path>'
                 % (x1, y1, mx, y1, mx, y2, x2, y2, " main" if main else "", " dim" if dim and not main else "",
                    1.5 + 7.0 * c / cmax, esc(a), esc(b), c, col_tot[fp], _pct(c, col_tot[fp])))
    for p, ns in shown.items():
        for n in ns:
            x, y = pos[(p, n["kind"])]
            main = (p, n["kind"]) in path
            dim = n["count"] < FLOW_DIM * col_tot[p] and not main
            tp = n.get("top_predecessor")
            line2 = ("%d of %d" % (n["count"], col_tot[p])) if p == 1 or not tp else "%d · %s" % (n["count"], tp["text"])
            tip = "%s\n%d of %d requests at call %d%s%s" % (
                n["kind"], n["count"], col_tot[p], p, ("\n" + tp["text"]) if tp else "",
                ("\nhooks fired after it: %d" % n["hooks"]) if n["hooks"] else "")
            o.append('<g class="fn%s%s"><title>%s</title>' % (" main" if main else "", " dim" if dim else "", esc(tip)))
            o.append('<rect x="%.1f" y="%.1f" width="%.1f" height="%d" rx="8"/>' % (x, y, cw, nh))
            if main:
                o.append('<rect x="%.1f" y="%.1f" width="3" height="%d" rx="1.5" class="acc"/>' % (x + 6, y + 10, nh - 20))
            o.append('<text x="%.1f" y="%.1f" class="fk">%s</text>' % (x + 14, y + 21, esc(short(n["kind"], chars))))
            o.append('<text x="%.1f" y="%.1f" class="fs">%s</text>' % (x + 14, y + 40, esc(short(line2, chars + 1))))
            if n["hooks"]:
                lab = "+hook ×%d" % n["hooks"]
                bw = len(lab) * 6.9 + 12
                o.append('<rect x="%.1f" y="%.1f" width="%.1f" height="17" rx="8.5" class="hb"/>'
                         '<text x="%.1f" y="%.1f" class="hbt" text-anchor="middle">%s</text>'
                         % (x + cw - bw - 6, y - 8, bw, x + cw - bw / 2 - 6, y + 4.5, lab))
            o.append("</g>")
    o.append("</svg>")
    return "".join(o)


# ---------------------------------------------------------------- goals

GROUP_DESC = OrderedDict([
    ("text", "prompt words (identifiers split, stopwords and generic task words dropped, light stemming)"),
    ("files_edited", "files the request edited or wrote"),
    ("path", "directory and file-name tokens of every touched path"),
    ("files_read", "files it read, grepped or cat-ed"),
    ("commands", "normalized Bash commands and test runners"),
    ("chain", "bigrams of the tool chain, e.g. Edit>Hook"),
    ("edited_path", "path tokens of edited files only"),
])
MEMBER_PROMPTS = 12     # distinct member prompts listed in the evidence before the rest folds
FILE_CHIPS = 8          # file chips per list in the evidence


def goal_anchor(name, request_ids):
    """Stable anchor for a goal: slug of its name + short hash of its member ids (not its position)."""
    slug = re.sub(r"[^a-z0-9]+", "-", (name or "").lower()).strip("-")[:40].strip("-") or "goal"
    return "goal-%s-%s" % (slug, hashlib.sha1(",".join(sorted(request_ids)).encode("utf-8")).hexdigest()[:6])


def area_order(u):
    """[(area, [goal, ...])] in display order; goals the areas don't list come last in an 'other' area."""
    by_id = {g["id"]: g for g in u["goals"]}
    out, seen = [], set()
    for a in u.get("areas") or []:
        gs = [by_id[x] for x in a["goals"] if x in by_id]
        seen.update(g["id"] for g in gs)
        if gs:
            out.append((a, gs))
    rest = [g for g in u["goals"] if g["id"] not in seen]
    if rest:
        out.append(({"id": "-", "name": "other", "goals": [g["id"] for g in rest],
                     "members": sum(g["runs"] for g in rest), "shared_files": []}, rest))
    return out


def goal_codes(u):
    """{goal id: 'G1'...} numbered in display order (areas, then goals inside each area)."""
    return {g["id"]: "G%d" % i for i, g in enumerate((g for _, gs in area_order(u) for g in gs), 1)}


def _named(src):
    return "named from terms" if src == "terms" else "named by " + src


def _file_chips(files, cls=""):
    head = files[:FILE_CHIPS]
    more = len(files) - len(head)
    return "".join('<span class="fchip mono%s" title="%s: in %s">%s <b>×%d</b></span>'
                   % (cls, esc(f["path"]), _plural(f["requests"], "request"), esc(short(f["path"], 44)),
                      f["requests"]) for f in head) + \
        ('<span class="muted small">+%d more</span>' % more if more > 0 else "")


def evidence_card(g, by_id, anchors):
    """'Why these are one goal': members verbatim (identical prompts folded), files, shared features, cohesion."""
    prompts = OrderedDict()
    for rid in g["request_ids"]:
        r = by_id[rid]
        prompts.setdefault(" ".join((r["prompt"] or "").split()), []).append(r)
    items = []
    for i, (p, rs) in enumerate(prompts.items()):
        if i >= MEMBER_PROMPTS:
            items.append('<li class="muted">+%d more distinct prompts</li>' % (len(prompts) - MEMBER_PROMPTS))
            break
        oc = Counter(r["outcome"] for r in rs)
        mix = " ".join('<span class="oc %s" title="%s">%s</span>%d' % (OUTCOME_ICON[k][1], k, OUTCOME_ICON[k][0], oc[k])
                       for k in ("success", "failed", "abandoned") if oc.get(k))
        ids = ", ".join(r["id"] for r in rs[:3]) + (" …" if len(rs) > 3 else "")
        items.append('<li><span class="xn">×%d</span><span class="mq" title="%s">“%s”</span>'
                     '<span class="mx">%s <span class="mono ids">%s</span></span></li>'
                     % (len(rs), esc(p[:3000]), esc(short(p, gl.MEMBERS_SHOWN)) if p else "(no prompt)", mix,
                        esc(ids)))
    distinct = len(prompts)
    files = ""
    for key, lab in (("files_edited", "FILES EDITED"), ("files_read", "FILES READ")):
        files += '<div class="lab ev-l">%s</div><div class="fchips">%s</div>' % (
            lab, _file_chips(g[key], " ed" if key == "files_edited" else "") if g[key] else
            '<span class="muted small">none</span>')
    if g["why"]:
        tot = g["cohesion"] or sum(x["contribution"] for x in g["why"]) or 1
        top = max(x["contribution"] for x in g["why"]) or 1
        why = "".join('<div class="wy" title="%s: adds %.3f of the %.3f mean pairwise similarity (%d%%)">'
                      '<span class="mono wl">%s</span><span class="wb"><i style="width:%.1f%%"></i></span>'
                      '<span class="mono wn">%.2f</span></div>'
                      % (esc(x["label"]), x["contribution"], tot, _pct(x["contribution"], tot),
                         esc(short(x["label"], 38)), 100.0 * x["contribution"] / top, x["contribution"])
                      for x in g["why"])
        why = '<div class="lab ev-l">WHY GROUPED · top shared features · similarity each adds</div><div class="wys">%s</div>' % why
    else:
        why = ('<div class="lab ev-l">WHY GROUPED</div><p class="muted small">A goal of one request: no other '
               'request was similar enough to join it.</p>')
    near = g.get("nearest")
    coh = ("cohesion <b class=\"mono\">%.2f</b> <span class=\"muted\">(mean pairwise similarity of its "
           "requests)</span>" % g["cohesion"]) if g["cohesion"] is not None else ""
    if near:
        href = anchors.get(near["id"])
        nm = esc(near.get("name") or near["id"])
        coh += "%snearest other goal: %s <span class=\"muted\">(similarity <b class=\"mono\">%.2f</b>)</span>" % (
            " · " if coh else "", '<a href="#%s">%s</a>' % (href, nm) if href else nm, near["similarity"])
    return (
        '<div class="ev"><div class="ev-h"><span class="lab">WHY THESE ARE ONE GOAL</span>'
        '<span class="muted small">%s, %s</span></div><div class="ev-g"><div>'
        '<div class="lab ev-l">MEMBER REQUESTS · verbatim, identical prompts folded</div><ul class="mem">%s</ul></div>'
        '<div>%s%s</div></div><div class="ev-f small">%s</div></div>'
    ) % (_plural(g["runs"], "request"), _plural(distinct, "distinct prompt"), "".join(items), files, why, coh)


def goal_rows(u, href_for):
    by_id = {r["id"]: r for s in u["sessions"] for r in s["requests"]}
    codes = goal_codes(u)
    anchors = {g["id"]: goal_anchor(g["goal"], g["request_ids"]) for g in u["goals"]}
    out = []
    for a, gs in area_order(u):
        runs = sum(g["runs"] for g in gs)
        chips = "".join('<span class="fchip mono" title="%s: touched by %s of this area">%s</span>'
                        % (esc(f["path"]), _plural(f.get("goals", 1), "goal"), esc(short(f["path"], 32)))
                        for f in a.get("shared_files", [])[:6])
        out.append('<div class="ar-row" id="area-%s"><span class="ar-n">%s</span>'
                   '<span class="muted">%s · %s <span class="hint">· %s</span></span><span class="ar-f" title="files '
                   'shared by goals of this area">%s</span></div>'
                   % (esc(a["id"]), esc(a["name"]), _plural(len(gs), "goal"), _plural(runs, "request"),
                      esc(_named(a.get("name_source", "terms"))), chips))
        for g in gs:
            reqs = [by_id[x] for x in g["request_ids"]]
            anchor = anchors[g["id"]]
            sig = next(iter(g["signatures"]), "-")
            out.append(
                '<details class="wf" id="%s"><summary class="wf-row">'
                '<span class="wf-name"><span class="caret"></span><span class="gc mono">%s</span><span class="wn">'
                '<span class="nm">%s</span><span class="hint">%s</span></span></span>'
                '<span><span class="stp %s" title="%s">%s</span></span>'
                '<span class="r">%d</span><span class="r">%d%%</span><span class="r">%s</span><span class="r">%s</span>'
                '<span class="mono sig" title="%s">%s</span></summary>'
                '<div class="wf-body">%s%s%s<div class="lab sect-lab">TOOL-CHAIN FLOW · %s</div>%s%s</div></details>'
                % (esc(anchor), codes[g["id"]], esc(g["goal"]), esc(_named(g["name_source"])),
                   STATUS_CLS.get(g["status"], ""), esc("%s: %s" % (g["status"], g["status_reason"])),
                   esc(g["status"]), g["runs"], round(100 * g["finish_rate"]), num(g["all_in"]["median"]),
                   num(g["new"]["median"]), esc(sig), esc(short(sig, 34)), numbers_strip(g),
                   evidence_card(g, by_id, anchors), fix_line(g), esc(g["goal"]), flow_svg(reqs),
                   paths_panel(g, reqs, anchor, href_for)))
    return "".join(out)


def method_note(u):
    m = u["goal_method"]
    gw = " · ".join('<span title="%s"><span class="mono">%s</span> %s</span>' % (esc(GROUP_DESC.get(k, k)), esc(k),
                                                                                _n(w))
                    for k, w in m["goal_weights"].items())
    aw = " · ".join('<span class="mono">%s</span> %s' % (esc(k), _n(w)) for k, w in m["area_weights"].items())
    groups = "".join('<li><span class="mono">%s</span> %s</li>' % (esc(k), esc(v)) for k, v in GROUP_DESC.items()
                     if k in m["goal_weights"])
    ign = m.get("ignored_files") or []
    ign_html = ("<ul>%s</ul>" % "".join('<li><span class="mono">%s</span> <span class="muted">%s</span></li>'
                                        % (esc(x["path"]), esc(x["reason"])) for x in ign[:12]) +
                ('<p class="muted">+%d more</p>' % (len(ign) - 12) if len(ign) > 12 else "")) if ign else \
        '<p class="muted">No file was read by more than %d%% of the requests, and no agent config or out-of-repo ' \
        'path was touched: nothing ignored.</p>' % round(100 * gl.OVERHEAD_SHARE)
    return (
        '<details class="note gm"><summary>How goals are discovered</summary>'
        '<p>Each request gets a signature of feature groups, each TF-IDF weighted (idf over the %d requests) and '
        'L2-normalized:</p><ul>%s</ul>'
        '<p>Group weights for goals: %s. Requests are compared by cosine and merged by %s-linkage clustering while '
        'the mean similarity stays at or above <b class="mono">%.2f</b>. Areas repeat this on the goal centroids '
        'with files and paths weighted up (%s) at <b class="mono">%.2f</b>.</p>'
        '<p><b>Ignored as setup noise</b> (files read by more than %d%% of requests once there are %d or more, the '
        'agent\'s own config, paths outside the repo):</p>%s'
        '<p class="muted">Also dropped from prompt words: %d stopwords and %d generic task words (%s).</p>'
        '<p><b>The outcome is not a feature</b>: success / failed / abandoned is kept as evidence only, so one goal '
        'is not split into its successes and its failures.</p></details>'
    ) % (u["requests"], groups, gw, esc(m.get("linkage", "average")), m["goal_threshold"], aw, m["area_threshold"],
         round(100 * gl.OVERHEAD_SHARE), gl.OVERHEAD_MIN_REQUESTS, ign_html, m.get("stopwords", 0),
         len(m.get("task_words") or []), esc(" ".join(m.get("task_words") or [])))


def goals_section(u, href_for):
    by_id = {r["id"]: r for s in u["sessions"] for r in s["requests"]}
    n_req = u["requests"]
    assigned = {rid for g in u["goals"] for rid in g["request_ids"]}
    orphans = [rid for rid in by_id if rid not in assigned]
    singles = sum(1 for g in u["goals"] if g["runs"] == 1)
    unhandled = u["unhandled_requests"]
    un_rows = "".join('<li>%s <span class="mono">%s</span> “%s” <span class="muted">%s</span></li>'
                      % (outcome_icon(by_id[x]), esc(x), esc(short(by_id[x]["prompt"], 120)),
                         esc("; ".join(by_id[x]["signals"][:2]))) for x in unhandled)
    foot = (
        '<div class="wf-foot"><span class="caret"></span><b>%d</b> requests no goal handles <span class="muted">'
        '– every request is a member of exactly one goal%s.</span></div>'
        '<details class="wf-foot"><summary><span class="caret"></span><b>%d</b> requests got no tool steps '
        '<span class="muted">and no answer</span></summary>%s</details>'
    ) % (len(orphans), "; %s of a single request" % _plural(singles, "goal") if singles else "", len(unhandled),
         ("<ul>%s</ul>" % un_rows) if unhandled else
         '<p class="muted">None: every request either used tools or got a final answer.</p>')
    rules = "".join("<li>%s</li>" % esc(r) for r in u["status_rules"])
    if u.get("fix_share_note"):
        rules += "<li>%s</li>" % esc(u["fix_share_note"])
    n_areas = len(area_order(u))
    return (
        '<div class="panel u-panel" id="goals"><div class="u-head"><div><h2>Goals</h2><div class="muted">What the '
        'requests were for, discovered from the data and grouped into areas. Open a goal for why its requests '
        'belong together, what to change, its flow, paths and replays. %s.</div></div>'
        '<div class="cap">%s IN %s · DISCOVERED FROM %s</div></div>'
        '<div class="wf-hdr"><span>GOAL</span><span>STATUS</span><span class="r">RUNS</span>'
        '<span class="r">FINISH</span><span class="r">MED ALL-IN</span><span class="r">MED NEW</span>'
        '<span>TOP PATH SIGNATURE</span></div>%s%s%s'
        '<details class="note"><summary>Status rules</summary><ul>%s</ul></details></div>'
    ) % (_cap(esc(short(gl.name_note(u["namer"]), 200))), _plural(len(u["goals"]), "goal").upper(),
         _plural(n_areas, "area").upper(), _plural(n_req, "request").upper(), goal_rows(u, href_for), foot, method_note(u), rules)


def numbers_strip(g):
    oc = g["outcomes"]
    looping = g["flags"].get("looping", 0)
    cells = [("runs", "%d" % g["runs"], "%s" % _plural(g["sessions"], "session")),
             ("finish", "%d%%" % _pct(oc.get("success", 0), g["runs"]), "share of runs with outcome success"),
             ("outcomes", '<span class="ocs"><span class="oc oc-ok">✓</span>%d <span class="oc oc-bad">✗</span>%d '
                          '<span class="oc oc-ab">…</span>%d <span class="oc oc-loop">↻</span>%d</span>'
              % (oc.get("success", 0), oc.get("failed", 0), oc.get("abandoned", 0), looping),
              "success · failed · abandoned · looping"),
             ("median all-in", num(g["all_in"]["median"]), "min %s · max %s" % (fmt_tok(g["all_in"]["min"]),
                                                                              fmt_tok(g["all_in"]["max"]))),
             ("median new", num(g["new"]["median"]), "min %s · max %s" % (fmt_tok(g["new"]["min"]),
                                                                        fmt_tok(g["new"]["max"]))),
             ("median calls", _n(g["median_calls"]), "API calls per run, incl. subagents")]
    return '<div class="strip">%s</div>' % "".join(
        '<div class="sc"><div class="lab">%s</div><div class="v">%s</div><div class="s">%s</div></div>' % c for c in cells)


def fix_line(g):
    """'What to change: <fix title> ~N new tokens/run' for a goal, once fixes were applied (else '')."""
    if "top_fix" not in g:
        return ""
    tf = g["top_fix"]
    if not tf:
        return ('<div class="wf-fix muted"><span class="lab">WHAT TO CHANGE</span> Nothing to change yet. '
                '<span class="muted">No fix touches these runs.</span></div>')
    return ('<div class="wf-fix"><span class="lab">WHAT TO CHANGE</span> <a href="#%s">%s</a> '
            '<span class="n">~%s</span> <span class="muted">new tokens/run in %s · %s of this goal\'s new tokens '
            'come from what it targets · all fixes together: %s</span></div>'
            % (esc(fix_anchor(tf["id"])), esc(tf["title"]), fmt_tok(int(tf["saves_per_run"])),
               _plural(tf["runs_affected"], "affected run"), _share_txt(tf["share"]), _share_txt(g["fix_share"])))


def _share_txt(x):
    return "<1%" if 0 < x < 0.005 else "%d%%" % round(100 * x)


def _chain_short(chain, units=3):
    parts = chain.split(" → ")
    return " → ".join(parts[:units]) + (" …" if len(parts) > units else "")


def chip_labels(chains, units=3):
    """Shortest prefix labels (>= `units` steps) that tell the given chains apart."""
    labels = {}
    for c in chains:
        n = units
        while True:
            lab = _chain_short(c, n)
            clash = any(o != c and _chain_short(o, n) == lab for o in chains)
            if not clash or n >= len(c.split(" → ")):
                labels[c] = lab
                break
            n += 1
    return labels


def paths_panel(g, reqs, anchor, href_for):
    """'N paths inside <goal>': one chip per tool chain (CSS radio filter) + replayable example runs."""
    chains = g["chains"]
    head = chains[:CHIPS]
    rest = chains[len(head):]
    chip_of = {}
    for j, ch in enumerate(head, 1):
        for rid in ch["request_ids"]:
            chip_of[rid] = j
    other_j = len(head) + 1
    for ch in rest:
        for rid in ch["request_ids"]:
            chip_of[rid] = other_j
    name = "p-" + anchor
    radios = ['<input type="radio" class="rd rd-0" name="%s" id="%s-0" checked>' % (name, name)]
    chips = ['<label class="chip lb-0" for="%s-0">All <b>%d</b></label>' % (name, g["runs"])]
    labs = chip_labels([ch["chain"] for ch in head])
    for j, ch in enumerate(head, 1):
        radios.append('<input type="radio" class="rd rd-%d" name="%s" id="%s-%d">' % (j, name, name, j))
        chips.append('<label class="chip lb-%d" for="%s-%d" title="%s"><span class="mono">%s</span> <b>%d%%</b></label>'
                     % (j, name, j, esc(ch["chain"]), esc(labs[ch["chain"]]), round(ch["pct"])))
    if rest:
        n = sum(c["count"] for c in rest)
        radios.append('<input type="radio" class="rd rd-%d" name="%s" id="%s-%d">' % (other_j, name, name, other_j))
        chips.append('<label class="chip lb-%d" for="%s-%d">+%d more paths <b>%d%%</b></label>'
                     % (other_j, name, other_j, len(rest), _pct(n, g["runs"])))
    by_chain = {c["chain"]: i for i, c in enumerate(chains)}
    ordered = sorted(reqs, key=lambda r: (by_chain.get(r["chain"], 99), r["start"] or ""))
    rows = []
    for r in ordered[:EXAMPLES]:
        href = href_for(r) if href_for else None
        rows.append(
            '<details class="exr c-%d"><summary>%s<span class="ex-t">%s</span><span class="ex-n">%s</span>'
            '<span class="ex-c">%s</span><span class="ex-s mono" title="%s">%s</span></summary>%s</details>'
            % (chip_of.get(r["id"], 0), outcome_icon(r), esc(narrate(r)), num(r["tokens"]["all_in"]),
               _plural(r["calls"], "call"), esc(r["session_id"]), esc(r["id"]), replay_html(r, href)))
    more = ('<p class="muted">Showing %d of %d runs.</p>' % (EXAMPLES, len(reqs))) if len(reqs) > EXAMPLES else ""
    sigs = list(g["signatures"])
    sig = '<span class="mono">%s</span>%s' % (esc(sigs[0]), (" and %d more" % (len(sigs) - 1)) if len(sigs) > 1 else "")
    return (
        '<div class="chains"><div class="ch-head"><div><h3>%s inside %s</h3><div class="muted">%s</div></div>'
        '<div class="ch-big"><b>%d%%</b><span class="muted">of runs finished</span></div></div>%s'
        '<div class="chips">%s</div><div class="lab ex-lab">EXAMPLES · %d · open one to replay it</div>'
        '<div class="exl">%s</div>%s</div>'
    ) % (_plural(len(chains), "path"), esc(g["goal"]), sig, round(100 * g["finish_rate"]), "".join(radios),
         "".join(chips), len(reqs), "".join(rows), more)


def _cap(s):
    return s[:1].upper() + s[1:]


def goal_new_tokens(u):
    """{goal id: new tokens summed over its runs}."""
    by_id = {r["id"]: r for s in u["sessions"] for r in s["requests"]}
    return {g["id"]: sum(by_id[x]["tokens"]["new"] for x in g["request_ids"]) for g in u["goals"]}


def fixes_by_goal(u, fixes, top=6, max_rows=15):
    """Compact matrix above the fix cards: rows = goals (by new tokens), columns = the top fixes that land in a
    goal, cells = share of the goal's new tokens that come from what the fix targets (blank when 0)."""
    from .fixes import cause_noun
    cols = [fx for fx in fixes or [] if isinstance(fx, dict) and fx.get("goals")][:top]
    if not cols or not u.get("goals"):
        return ""
    new_of = goal_new_tokens(u)
    codes = goal_codes(u)
    anchors = {g["id"]: goal_anchor(g["goal"], g["request_ids"]) for g in u["goals"]}
    share = {(fx["id"], e["goal_id"]): e for fx in cols for e in fx["goals"]}
    goals = sorted(u["goals"], key=lambda g: (-new_of[g["id"]], codes[g["id"]]))
    head = "".join('<th class="fxc" title="%s"><a href="#%s"><span class="fxn mono">F%d</span>%s</a></th>'
                   % (esc(fx["title"]), esc(fix_anchor(fx["id"])), j, esc(short(cause_noun(fx), 26)))
                   for j, fx in enumerate(cols, 1))
    rows = []
    for g in goals[:max_rows]:
        cells = []
        for fx in cols:
            e = share.get((fx["id"], g["id"]))
            x = e["share_of_goal_tokens"] if e else 0
            if not x:
                cells.append('<td class="fxv"></td>')
                continue
            cells.append('<td class="fxv" title="%s: %s of this goal\'s new tokens (%s)"><span style="background:'
                         'rgba(227,179,65,%.2f)">%s</span></td>'
                         % (esc(short(fx["title"], 80)), _share_txt(x), _plural(e["runs_affected"], "run"),
                            0.12 + 0.6 * min(1.0, x), _share_txt(x)))
        rows.append('<tr><td class="fxg"><a href="#%s"><span class="gc mono">%s</span>%s</a></td>'
                    '<td class="num">%s</td>%s</tr>'
                    % (esc(anchors[g["id"]]), codes[g["id"]], esc(short(g["goal"], 48)), num(new_of[g["id"]]),
                       "".join(cells)))
    more = ('<p class="muted small">+%d more goals with fewer new tokens.</p>' % (len(goals) - max_rows)
            if len(goals) > max_rows else "")
    return (
        '<div class="fxm-wrap"><div class="lab">FIXES BY GOAL · share of each goal\'s new tokens that the fix '
        'targets</div><div class="tscroll"><table class="fxmat"><thead><tr><th>goal</th><th class="num">new tokens'
        '</th>%s</tr></thead><tbody>%s</tbody></table></div>%s<p class="muted small">Rows sorted by the goal\'s new '
        'tokens (cache reads excluded); blank = the fix does not touch that goal. Columns are the top %d fixes '
        'below (F1 = first card). Shares of different fixes can overlap.</p></div>'
    ) % (head, "".join(rows), more, len(cols))


def group_bars_by_goal(u):
    """Token totals per goal for the 'By goal' panel (same shape as report.grouping)."""
    by_id = {r["id"]: r for s in u["sessions"] for r in s["requests"]}
    out = {}
    for g in u["goals"]:
        t = out.setdefault(g["goal"], {"fresh_input": 0, "cache_write": 0, "cache_read": 0, "output": 0,
                                       "all_in": 0, "n": 0})
        for rid in g["request_ids"]:
            for k in ("fresh_input", "cache_write", "cache_read", "output", "all_in"):
                t[k] += by_id[rid]["tokens"][k]
            t["n"] += 1
    return out


# ---------------------------------------------------------------- context spend

def _fold(src):
    d = {k: 0 for k, _, _ in CTX_SOURCES}
    for k, v in src.items():
        d["mcp" if k.startswith("mcp") else k if k in d else "other"] += v
    return d


def context_units(u, per_request):
    """([(label, title, sources)] in chronological order, {goal id: code}): one unit per session, or per request
    (session report). Labels are start time + the goal code (G1..), mapped to names in a legend under the chart."""
    ini = goal_codes(u)
    gname = {g["id"]: g["goal"] for g in u["goals"]}
    units = []
    if per_request:
        for s in u["sessions"]:
            by_idx = {c["index"]: c for c in s["context"]["main"]}
            for r in s["requests"]:
                src = Counter()
                for i in r["call_indexes"]:
                    src.update(by_idx[i]["sources"] if i in by_idx else {})
                units.append((r["start"], "%s %s" % (t_hms(r["start"])[:5], ini.get(r["goal"]["id"], "?")),
                              "%s · %s %s\n“%s”" % (r["id"], ini.get(r["goal"]["id"], "?"), r["goal"]["name"],
                                                       short(r["prompt"], 80)), _fold(src)))
    else:
        for s in u["sessions"]:
            g = Counter(r["goal"]["id"] for r in s["requests"]).most_common(1)
            grp = g[0][0] if g else "-"
            first = s["requests"][0]["prompt"] if s["requests"] else ""
            units.append((s["start"], "%s %s" % (t_hms(s["start"])[:5], ini.get(grp, "?")),
                          "%s · %s %s\n“%s”" % (s["session_id"][:8], ini.get(grp, "?"), gname.get(grp, grp),
                                                 short(first, 80)),
                          _fold(s["context"]["totals"])))
    units.sort(key=lambda x: x[0] or "")
    return [x[1:] for x in units], ini


def _ctx_svg(units, cumulative, keys):
    n = len(units)
    L, R, T, B = 62, 10, 10, 30
    W, ph = CTX_W, CTX_PH
    pw = W - L - R
    slot = pw / n
    bw = max(2.0, min(slot * 0.62, 44))
    vals = []
    acc = Counter()
    for lab, title, src in units:
        if cumulative:
            acc.update(src)
            vals.append(dict(acc))
        else:
            vals.append(src)
    vmax = max(sum(v.get(k, 0) for k in keys) for v in vals) or 1
    step = _nice(vmax / 4)
    ymax = step * math.ceil(vmax / step)
    H = T + ph + B
    o = ['<svg class="chart" viewBox="0 0 %d %d" width="100%%" role="img">' % (W, H)]
    v = 0
    while v <= ymax + 1e-9:
        y = T + ph - v / ymax * ph
        o.append('<line x1="%d" x2="%d" y1="%.1f" y2="%.1f" class="grid"/>' % (L, W - R, y, y))
        o.append('<text x="%d" y="%.1f" class="axl" text-anchor="end">%s</text>' % (L - 8, y + 4, tick(v)))
        v += step
    colors = {k: c for k, _, c in CTX_SOURCES}
    names = {k: l for k, l, _ in CTX_SOURCES}
    k_lab = max(1, math.ceil(62 / slot))
    for i, ((lab, title, _), val) in enumerate(zip(units, vals)):
        x = L + slot * i + (slot - bw) / 2
        tot = sum(val.get(k, 0) for k in keys)
        tip = "%s\n%s%s tokens of context sent%s\n" % (title, "cumulative " if cumulative else "", format(tot, ","),
                                                      "" if cumulative else " (sum over its API calls)")
        tip += "\n".join("~%s %s" % (fmt_tok(val.get(k, 0)), names[k]) for k in reversed(keys) if val.get(k))
        o.append('<g class="bar"><title>%s</title>' % esc(tip))
        a = 0
        for k in keys:
            vv = val.get(k, 0)
            if vv <= 0:
                continue
            ya, yb = T + ph - a / ymax * ph, T + ph - (a + vv) / ymax * ph
            o.append('<rect x="%.1f" y="%.1f" width="%.1f" height="%.2f" fill="%s" class="seg"/>'
                     % (x, yb, bw, max(ya - yb, 0.6), colors[k]))
            a += vv
        o.append('<rect class="hit" x="%.1f" y="%d" width="%.1f" height="%d"/></g>' % (L + slot * i, T, slot, ph))
        if i % k_lab == 0:
            o.append('<text x="%.1f" y="%d" class="axl" text-anchor="middle">%s</text>'
                     % (L + slot * (i + 0.5), T + ph + 18, esc(lab)))
    o.append('<line x1="%d" x2="%d" y1="%d" y2="%d" stroke="#484f58"/>' % (L, W - R, T + ph, T + ph))
    o.append("</svg>")
    return "".join(o)


def context_section(u, per_request):
    units, ini = context_units(u, per_request)
    if not units:
        return '<div class="panel u-panel"><div class="lab">CONTEXT SPEND OVER TIME</div><p class="muted">No API calls.</p></div>'
    tot = Counter()
    for _, _, src in units:
        tot.update(src)
    grand = sum(tot.values()) or 1
    keys = [k for k, _, _ in CTX_SOURCES if tot.get(k)]
    any_skill = any(s["context"]["skills_used"] for s in u["sessions"])
    any_mcp = any(s["context"]["mcp_used"] for s in u["sessions"])
    label = {k: l for k, l, _ in CTX_SOURCES}
    label["skills"] = "Skills" if any_skill else "Skills (unused)"
    label["mcp"] = "MCP" if any_mcp else "MCP (unused)"
    label["instructions"] = "Instructions (CLAUDE.md + imports)"
    colors = {k: c for k, _, c in CTX_SOURCES}
    scaled = {"mcp" if k.startswith("mcp") else k for s in u["sessions"]
              for k in s["context"].get("scaled_to_measured") or []}
    for k in scaled & set(label):
        label[k] += " *"
    shares = "".join('<span style="width:%.3f%%;background:%s" title="%s %d%%"></span>'
                     % (100.0 * tot[k] / grand, colors[k], esc(label[k]), _pct(tot[k], grand)) for k in keys)
    leg = "".join('<span class="li"><i class="sw" style="background:%s"></i>%s <b class="mono">%s%%</b></span>'
                  % (colors[k], esc(label[k]), _n(round(100.0 * tot[k] / grand, 1)) if tot[k] / grand < 0.01
                     else _pct(tot[k], grand)) for k in keys)
    unit = "request" if per_request else "run"
    used = {lab.split(" ")[-1] for lab, _, _ in units}
    gname = {g["id"]: g["goal"] for g in u["goals"]}
    key_ini = "".join('<span><b class="mono">%s</b> %s</span>' % (code, esc(gname[gid]))
                      for gid, code in sorted(ini.items(), key=lambda kv: int(kv[1][1:])) if code in used)
    return (
        '<div class="panel u-panel ctx"><input type="radio" name="cx" id="cx-run" checked>'
        '<input type="radio" name="cx" id="cx-cum"><div class="u-head"><div><div class="lab">CONTEXT SPEND OVER '
        'TIME</div><div class="hero n">%s</div><div class="muted">tokens of context sent (incl. cache reads) · %s · '
        'split by source is estimated (~)</div></div><div class="seg-tg"><label for="cx-run">Per %s</label>'
        '<label for="cx-cum">Cumulative</label></div></div>'
        '<div class="cx-run">%s</div><div class="cx-cum">%s</div>'
        '<div class="hbar shares">%s</div><div class="legend ctx-leg">%s</div>'
        '<div class="lab gk-l">X AXIS · start time + goal</div><div class="gkey">%s</div>'
        '<div class="muted small">Each bar sums its API calls\' input context%s; a %s is labelled with %s. Hover a bar '
        'for its split.%s</div></div>'
    ) % (fmt_tok(grand), _plural(len(units), unit), unit, _ctx_svg(units, False, keys), _ctx_svg(units, True, keys),
         shares, leg, key_ini, "" if per_request else " (subagent calls included)", unit,
         "its goal" if per_request else "the goal of most of its requests",
         " * scaled to measured: chars/4 estimates scaled so that each call's parts sum to its measured new tokens "
         "(dense tables / JSON tokenize worse than 4 chars per token). Other = what no estimate covers." if scaled
         else "")


def _referenced(file_entry, reqs):
    """True if any tool step names this file (Read/Grep/Glob target or a Bash command mentioning it)."""
    rel, path = file_entry["rel"], file_entry["path"]
    base = os.path.basename(path)
    for r in reqs:
        for s in r["steps"]:
            if un.is_hook(s):
                continue
            t = s["target"] or ""
            if t and (t == rel or rel in t or path in t or (s["kind"].startswith("Bash(") and base in t.split())):
                return True
    return False


def offered_card(u, analyses, per_request):
    runs = [a for a in analyses if a["session_id"] in {s["session_id"] for s in u["sessions"]}]
    if not runs:
        return ""
    reqs_by_sid = {s["session_id"]: s["requests"] for s in u["sessions"]}
    unit = "request" if per_request else "run"
    n_units = sum(len(v) for v in reqs_by_sid.values()) if per_request else len(runs)

    def per_unit(fn):
        """Average over units of (per-call tokens × main calls), est."""
        tot = sum(fn(a) * a["totals"]["main"]["calls"] for a in runs)
        return tot / n_units if n_units else 0

    rows = []
    have = [a for a in runs if a["offered_vs_used"].get("available")]
    for key, title in (("skills", "Skills"), ("mcp_servers", "MCP servers"), ("subagent_types", "Subagent types")):
        if not have:
            break
        off, used = set(), set()
        for a in have:
            for e in a["offered_vs_used"].get(key) or []:
                off.add(e["name"])
                if e["used"]:
                    used.add(e["name"])
        per_call = statistics.mean(a["offered_vs_used"].get(key + "_unused_est_tokens", 0) for a in have)
        names = ", ".join(sorted(used)) or "none"
        rows.append((title, len(off), len(used), "used: %s" % names, per_call,
                     per_unit(lambda a, k=key: a["offered_vs_used"].get(k + "_unused_est_tokens", 0)),
                     "unused listings"))
    t_off, t_used, t_unused = set(), set(), []
    for s in u["sessions"]:
        names = {t["name"] for t in s.get("tools_offered") or []}
        t_off |= names
        used_here = {st["tool"] for r in s["requests"] for st in r["steps"] if not un.is_hook(st)}
        t_used |= used_here & names if names else used_here
        t_unused.append(sum(t["est_tokens"] for t in s.get("tools_offered") or [] if t["name"] not in used_here))
    if t_off:
        per_call = statistics.mean(t_unused) if t_unused else 0
        rows.append(("Tools (definitions)", len(t_off), len(t_used), "used: %s" % (", ".join(sorted(t_used)) or "none"),
                     per_call, per_call * sum(a["totals"]["main"]["calls"] for a in runs) / max(n_units, 1),
                     "unused definitions"))
    elif any(a["per_tool"] for a in runs):
        rows.append(("Tools (definitions)", None, len({k for a in runs for k in a["per_tool"]}),
                     "this transcript doesn't record the tool list", 0, 0, ""))
    body = "".join(
        '<tr><td>%s</td><td class="num">%s</td><td class="num">%d</td><td class="muted">%s</td>'
        '<td class="num">%s</td><td class="num">%s</td></tr>'
        % (esc(t), "-" if off is None else off, used, esc(short(note, 90)), num(pc, est=True) if pc else "-",
           num(pu, est=True) if pu else "-") for t, off, used, note, pc, pu, _ in rows)
    # instruction files
    files = {}
    for a in runs:
        ins = a.get("instructions") or {}
        reqs = reqs_by_sid.get(a["session_id"], [])
        for e in ins.get("files") or []:
            f = files.setdefault(e["rel"], {"e": e, "loaded": 0, "ref": 0, "tok": e["est_tokens"], "calls": 0})
            f["loaded"] += 1
            f["calls"] += a["totals"]["main"]["calls"]
            f["ref"] += _referenced(e, reqs)
    frows = []
    for rel, f in sorted(files.items(), key=lambda kv: -kv[1]["tok"]):
        e = f["e"]
        kind = ("@-import of %s" % e["imported_by"]) if e["imported_by"] else (
            "CLAUDE.md" if os.path.basename(rel).upper() == "CLAUDE.MD" else "instructions")
        if f["ref"]:
            verdict = '<span class="ok">referenced</span> in %d of %d runs (agent opened / searched it)' % (
                f["ref"], f["loaded"])
        elif e["imported_by"]:
            verdict = ('<span class="warn">never opened</span> by the agent; can\'t tell from transcript whether its '
                       'content was needed')
        else:
            verdict = "always in context; can't tell from transcript which of its rules were used"
        frows.append('<tr><td><span class="mono" title="%s">%s</span> <span class="muted">%s</span></td>'
                     '<td class="num">%d</td><td>%s</td><td class="num">%s</td><td class="num">%s</td></tr>'
                     % (esc(e["path"]), esc(short(rel, 40)), esc(kind), f["loaded"], verdict,
                        num(f["tok"], est=True), num(f["tok"] * f["calls"] / max(n_units, 1), est=True)))
    ftable = ('<table class="compact"><thead><tr><th>instruction file</th><th class="num">loaded</th>'
              '<th>used?</th><th class="num">per call</th><th class="num">per %s</th></tr></thead><tbody>%s</tbody>'
              '</table>' % (unit, "".join(frows))) if frows else \
        '<p class="muted">No CLAUDE.md / instruction files recorded in these transcripts.</p>'
    return (
        '<div class="panel u-panel"><div class="lab">OFFERED VS USED</div><p class="muted">What sat in every call\'s '
        'prefix vs what the agent touched, across %s. Token figures are chars/4 estimates of the unused part: per '
        'API call, and per %s (× its calls).</p>'
        '<table class="compact"><thead><tr><th>offered</th><th class="num">offered</th><th class="num">used</th>'
        '<th></th><th class="num">unused / call</th><th class="num">unused / %s</th></tr></thead><tbody>%s</tbody>'
        '</table><div style="height:10px"></div>%s</div>'
    ) % (_plural(n_units, unit), unit, unit, body or '<tr><td colspan="6" class="muted">This transcript does not '
         'record what was offered.</td></tr>', ftable)


# ---------------------------------------------------------------- summary

def summary_paragraph(u, per_request):
    reqs = [r for s in u["sessions"] for r in s["requests"]]
    if not reqs:
        return "No requests found."
    n = len(reqs)
    oc = Counter(r["outcome"] for r in reqs)
    parts = []
    where = "" if per_request else " in %s" % _plural(len(u["sessions"]), "session")
    fin = "all finished" if oc["success"] == n else "%d of %d finished (%s)" % (
        oc["success"], n, ", ".join("%d %s" % (oc[k], k) for k in ("failed", "abandoned") if oc[k]))
    ip = sum("maybe_in_progress" in r["flags"] for r in reqs)
    never = [g for g in u["goals"] if not g["outcomes"].get("success")]
    if never:
        nf = "; %s never finished: %s" % (_plural(len(never), "goal"), ", ".join(g["goal"] for g in never[:3]) +
                                          (" (+%d more)" % (len(never) - 3) if len(never) > 3 else ""))
    else:
        nf = "; every goal finished at least once" if len(u["goals"]) > 1 else ""
    parts.append("%s%s → %s in %s; %s%s%s." % (_plural(n, "request"), where, _plural(len(u["goals"]), "goal"),
                                              _plural(len(area_order(u)), "area"), fin,
                                              " (%d may still be running)" % ip if ip else "", nf))
    st = Counter(g["status"] for g in u["goals"])
    if st and set(st) != {"Healthy"}:
        parts.append("Goal status: %s." % ", ".join("%d %s" % (st[k], k) for k in ("Failing", "Looping", "Costly",
                                                                                   "Healthy") if st.get(k)))
    edits = hooked = 0
    hook_names = Counter()
    hook_tok = 0
    for r in reqs:
        st = r["steps"]
        for i, s in enumerate(st):
            if un.is_hook(s):
                hook_names[s["hook_name"]] += 1
                hook_tok += s["tokens"]
            elif s["tool"] in un.EDIT_TOOLS:
                edits += 1
                hooked += i + 1 < len(st) and un.is_hook(st[i + 1]) and st[i + 1]["tool_use_id"] == s["tool_use_id"]
    if hook_names:
        lead = ("every edit triggered a hook" if edits and hooked == edits else
                "%d of %d edits triggered a hook" % (hooked, edits) if edits else "")
        parts.append("Hooks fired %s (%s)%s, adding ~%s tokens of hook output to context." % (
            _plural(sum(hook_names.values()), "time"), ", ".join("%s ×%d" % kv for kv in hook_names.most_common(3)),
            "; " + lead if lead else "", fmt_tok(hook_tok)))
    firsts = Counter(r["flow"][0] for r in reqs if r["flow"])
    if firsts:
        k, c = firsts.most_common(1)[0]
        parts.append("The most common first move was %s (%d/%d)." % (k, c, n))
    tests = [r for r in reqs if un.last_test(r["steps"])]
    if tests:
        parts.append("%s ran tests; the last test signal was green in %d." % (
            _plural(len(tests), "request"), sum(1 for r in tests if not un.last_test(r["steps"])["tests"]["failed"])))
    loop = sum("looping" in r["flags"] for r in reqs)
    if loop:
        parts.append("%s looped (same step repeated or fail → edit → fail cycles)." % _plural(loop, "request"))
    if u["goals"]:
        g = max(u["goals"], key=lambda g: g["all_in"]["median"])
        lo = min(u["goals"], key=lambda g: g["all_in"]["median"])
        if g is not lo:
            parts.append("%s was the most expensive goal (median %s all-in per run vs %s for %s)." % (
                g["goal"], fmt_tok(g["all_in"]["median"]), fmt_tok(lo["all_in"]["median"]), lo["goal"]))
    units, _ = context_units(u, per_request)
    tot = Counter()
    for _, _, src in units:
        tot.update(src)
    if sum(tot.values()):
        k, v = max(((k, v) for k, v in tot.items() if k != "other"), key=lambda kv: kv[1], default=(None, 0))
        if k:
            lab = dict((a, b) for a, b, _ in CTX_SOURCES)[k]
            parts.append("The largest identified context source was %s (~%d%% of everything sent)." % (
                lab.lower(), _pct(v, sum(tot.values()))))
    return " ".join(parts)


# ---------------------------------------------------------------- section

def understand_section(u, analyses, per_request=False, href_for=None):
    """The whole Understand section. href_for(request) -> link to its session view, or None."""
    reqs = [r for s in u["sessions"] for r in s["requests"]]
    return (
        '<section id="understand" class="part"><h2 class="part-h"><span class="pn">1</span> Understand '
        '<span class="muted">what the agent did</span></h2>'
        '<div class="panel u-panel summary"><div class="lab">WHAT THE AGENT DID</div><p>%s</p></div>'
        '<div class="panel u-panel"><div class="lab">TOOL-CHAIN FLOW · ALL GOALS · %s</div>%s'
        '<p class="muted small">Each column is the n-th distinct tool kind a request used (repeats collapsed); edge '
        'width = requests taking that step; hooks are badges on the tool they followed. Highlighted: the busiest '
        'path. Dimmed: &lt; %d%% of the column. Point at a node for counts.</p></div>'
        '%s%s%s</section>'
    ) % (esc(summary_paragraph(u, per_request)), _plural(len(reqs), "request").upper(), flow_svg(reqs),
         int(FLOW_DIM * 100), goals_section(u, href_for), context_section(u, per_request),
         offered_card(u, analyses, per_request))


CSS = """
nav.steps{position:sticky;top:0;z-index:20;background:#0d1117ee;backdrop-filter:blur(4px);border-bottom:1px solid #30363d;
margin:0 -20px 10px;padding:9px 20px;display:flex;gap:6px;align-items:center;font:600 14px var(--mono)}
nav.steps a,nav.steps span.off{padding:3px 12px;border-radius:14px;border:1px solid transparent;color:#c9d1d9}
nav.steps a:hover{border-color:#30363d;text-decoration:none;background:#161b22}
nav.steps span.off{color:#545d68;cursor:not-allowed}
nav.steps .sep{color:#484f58}
nav.steps .brand{color:#8b949e;font-weight:400;margin-right:10px}
section.part{scroll-margin-top:48px}
h2.part-h{font-size:21px;margin:22px 0 6px;display:flex;align-items:baseline;gap:10px}
h2.part-h .pn{font:600 13px var(--mono);border:1px solid #484f58;border-radius:50%;width:24px;height:24px;
display:inline-flex;align-items:center;justify-content:center;color:#c9d1d9;position:relative;top:-2px}
.lab{font:12px var(--mono);letter-spacing:1.2px;color:#8b949e;text-transform:uppercase}
.u-panel{background:#0f1318;border-color:#262c33;border-radius:12px;padding:16px 18px}
.u-panel h2{font-size:19px;margin:0 0 2px}
.u-head{display:flex;justify-content:space-between;gap:16px;align-items:flex-start;margin-bottom:10px}
.u-head .cap{font:12px var(--mono);letter-spacing:1.2px;color:#8b949e;white-space:nowrap;padding-top:6px}
.summary p{margin:6px 0 0;font-size:15.5px;line-height:1.6;color:#d6dde4}
.small{font-size:13px}
.wf-hdr,.wf-row{display:grid;grid-template-columns:minmax(0,1fr) 92px 56px 66px 96px 88px 230px;gap:12px;align-items:center}
.wf-hdr{font:12px var(--mono);letter-spacing:1px;color:#8b949e;padding:6px 10px;border-bottom:1px solid #21262d}
.wf-hdr .r,.wf-row .r{text-align:right}
details.wf{border-bottom:1px solid #1c2128}
details.wf>summary,details.wf-foot>summary{list-style:none;cursor:pointer}
details.wf>summary::-webkit-details-marker,details.wf-foot>summary::-webkit-details-marker{display:none}
.wf-row{padding:9px 10px;font-size:15px}
.wf-row:hover{background:#161b22}
.wf-row .r{font-family:var(--mono)}
.wf-name{display:flex;align-items:baseline;gap:6px;min-width:0;font-weight:600}
.wf-name .wn{display:flex;flex-direction:column;min-width:0}
.wf-name .hint{font:11.5px var(--mono);color:#6e7681;font-weight:400;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.caret:before{content:"\\25B8";color:#6e7681;font-size:12px;display:inline-block;width:12px}
details[open]>summary .caret:before{content:"\\25BE"}
.wf-row .sig{font-size:12.5px;color:#a5b4c8;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.stp{display:inline-block;padding:1px 10px;border-radius:12px;font-size:13px;border:1px solid}
.st-ok{color:#c9d1d9;border-color:#3d444d;background:#21262d}
.st-cost{color:#e3b341;border-color:#9e6a03;background:#bb800926}
.st-fail{color:#ff7b72;border-color:#da3633;background:#f8514926}
.st-loop{color:#d2a8ff;border-color:#8957e5;background:#a371f726}
.wf-body{padding:4px 10px 18px 30px}
.wf-named{font-size:13px;margin:0 0 6px}
.sect-lab{margin:16px 0 8px}
.wf-fix{margin:10px 0 2px;font-size:14.5px;border:1px solid #9e6a03;background:#bb800914;border-radius:10px;padding:8px 12px}
.wf-fix.muted{border-color:#262c33;background:none}
.wf-fix .lab{margin-right:6px}.wf-fix .n{color:#e3b341;font-weight:600}
.strip{display:grid;grid-template-columns:repeat(6,minmax(0,1fr));gap:8px;margin:6px 0}
.strip .sc{border:1px solid #262c33;border-radius:10px;padding:8px 11px;background:#0d1117}
.strip .v{font:600 19px var(--mono);margin:2px 0;white-space:nowrap}
.strip .ocs{font-size:15px;display:inline-flex;gap:3px;align-items:center}
.strip .ocs .oc{width:18px;height:18px;font-size:11px;margin-left:4px}
.strip .ocs .oc:first-child{margin-left:0}
.strip .s{font-size:12.5px;color:#8b949e}
.oc{display:inline-flex;width:20px;height:20px;border-radius:50%;align-items:center;justify-content:center;
font:700 12px var(--mono);border:1.5px solid;flex:none;margin-right:2px}
.oc-ok{color:#3fb950;border-color:#2e9e74}.oc-bad{color:#ff7b72;border-color:#da3633}
.oc-loop{color:#d2a8ff;border-color:#8957e5}.oc-ab{color:#8b949e;border-color:#484f58}
svg.flow{display:block;margin:4px 0 2px}
svg.flow .fh{fill:#8b949e;font:12px var(--mono);letter-spacing:1.2px}
svg.flow .fn rect{fill:#141a20;stroke:#30363d;stroke-width:1}
svg.flow .fn.main>rect:first-of-type{stroke:#59636e}
svg.flow .fn .acc{fill:#d4a72c;stroke:none}
svg.flow .fk{fill:#e6edf3;font:600 13px var(--mono)}
svg.flow .fs{fill:#a5b4c8;font:12.5px var(--mono)}
svg.flow .fn.dim{opacity:.42}
svg.flow .fe{fill:none;stroke:#59636e;stroke-opacity:.75}
svg.flow .fe.main{stroke:#d4a72c;stroke-opacity:.9}
svg.flow .fe.dim{stroke-opacity:.3}
svg.flow .hb{fill:#2a1f33;stroke:#8957e5;stroke-width:1}
svg.flow .hbt{fill:#d2a8ff;font:600 11.5px var(--mono)}
.chains{border:1px solid #262c33;border-radius:12px;padding:14px 16px;margin-top:14px;background:#0d1117}
.ch-head{display:flex;justify-content:space-between;align-items:flex-start;gap:16px}
.ch-head h3{font-size:17px;margin:0 0 2px}
.ch-big{text-align:right}.ch-big b{display:block;font:600 30px var(--mono);line-height:1.1}.ch-big span{font-size:12.5px}
.chains .rd{position:absolute;opacity:0;pointer-events:none}
.chips{display:flex;flex-wrap:wrap;gap:8px;margin:12px 0}
.chip{display:inline-flex;gap:6px;align-items:baseline;padding:5px 13px;border-radius:16px;background:#161b22;
border:1px solid #262c33;font-size:13.5px;cursor:pointer;max-width:100%;overflow:hidden;white-space:nowrap;text-overflow:ellipsis}
.chip b{color:#e3b341;font-family:var(--mono)}
.chip:hover{border-color:#484f58}
.ex-lab{margin:4px 0 6px}
.exl{border:1px solid #262c33;border-radius:10px;overflow:hidden}
details.exr{border-top:1px solid #1c2128}details.exr:first-child{border-top:0}
details.exr>summary{list-style:none;cursor:pointer;display:grid;grid-template-columns:24px minmax(0,1fr) 70px 64px 104px;
gap:10px;align-items:center;padding:9px 12px;font-size:14.5px}
details.exr>summary::-webkit-details-marker{display:none}
details.exr>summary:hover{background:#161b22}
details.exr[open]>summary{background:#141a20}
.ex-t{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
details.exr[open] .ex-t{white-space:normal}
.ex-n{text-align:right}.ex-c{text-align:right;color:#8b949e;font-size:13px}.ex-s{color:#8b949e;font-size:12.5px;text-align:right}
.replay{padding:6px 14px 14px 46px;background:#11161c}
.rp-prompt{font-size:14px;color:#c9d1d9;font-style:italic;margin:4px 0 8px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.rp-prompt .lab{font-style:normal;margin-right:6px}
ol.steps{list-style:none;margin:0;padding:0;position:relative}
ol.steps:before{content:"";position:absolute;left:11px;top:6px;bottom:6px;border-left:1px solid #30363d}
ol.steps li{display:grid;grid-template-columns:24px minmax(0,1fr) 160px 70px;gap:10px;align-items:center;padding:3px 0;
font-size:14px;position:relative}
ol.steps li.gap{display:block;color:#8b949e;padding-left:34px}
ol.steps .si{width:23px;height:23px;border-radius:50%;border:1px solid #484f58;background:#161b22;display:inline-flex;
align-items:center;justify-content:center;font:600 12px var(--mono);color:#c9d1d9}
ol.steps li.hk .si{border-color:#8957e5;color:#d2a8ff}
ol.steps li.hk .st{color:#c9b6ee}
ol.steps .st{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
ol.steps .sb{height:8px;background:#1c2128;border-radius:4px;overflow:hidden}
ol.steps .sb i{display:block;height:100%;background:#6e7681;border-radius:4px}
ol.steps li.hk .sb i{background:#8957e5}
ol.steps .sn{text-align:right;font-family:var(--mono);font-size:13px}
.rp-end{margin-top:10px;font-size:14px;display:block}
.rp-end ul{margin:4px 0 0;padding-left:26px;color:#8b949e;font-size:13px}
a.open{display:inline-block;margin-top:8px;font-size:13.5px}
details.wf-foot{padding:12px 10px 4px;font-size:15px}
details.wf-foot ul{list-style:none;padding-left:20px}details.wf-foot li{margin:4px 0;display:flex;gap:8px;align-items:center}
.hero{font:600 34px var(--mono);margin:2px 0}
.seg-tg{display:flex;align-items:flex-start}
.ctx>input{position:absolute;opacity:0;pointer-events:none}
.seg-tg label{padding:5px 14px;border:1px solid #30363d;font-size:13.5px;cursor:pointer;color:#c9d1d9;background:#0d1117}
.seg-tg label:first-child{border-radius:16px 0 0 16px}.seg-tg label:last-child{border-radius:0 16px 16px 0;border-left:0}
#cx-run:checked~.u-head label[for=cx-run],#cx-cum:checked~.u-head label[for=cx-cum]{background:#e6edf3;color:#0d1117;font-weight:600}
.ctx .cx-run,.ctx .cx-cum{display:none}
#cx-run:checked~.cx-run,#cx-cum:checked~.cx-cum{display:block}
.shares{height:10px;margin:14px 0 8px}
.ctx-leg{font-size:13.5px}.ctx-leg b{font-weight:400;color:#8b949e;font-size:12.5px}
.gk-l{margin:12px 0 4px}
.gkey{display:grid;grid-template-columns:repeat(auto-fill,minmax(370px,1fr));gap:3px 18px;font-size:13.5px;margin-bottom:8px}
.gkey span{white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.gkey b{color:#e3b341;margin-right:6px;font-weight:600}
.ar-row{display:flex;align-items:center;gap:6px 12px;flex-wrap:wrap;padding:16px 10px 7px;border-bottom:1px solid #262c33;
font-size:14px}
.ar-row .ar-n{font:600 13px var(--mono);letter-spacing:1.2px;text-transform:uppercase;color:#e6edf3}
.ar-row .ar-f{display:flex;gap:5px;flex-wrap:wrap;margin-left:auto}.ar-row .hint{font:12px var(--mono);color:#6e7681}
.gc{font-size:12px;color:#e3b341;font-weight:600;min-width:28px;flex:none}
.fchip{display:inline-block;padding:1px 8px;border-radius:10px;border:1px solid #30363d;background:#161b22;font-size:12px;
color:#c9d1d9;white-space:nowrap;max-width:100%;overflow:hidden;text-overflow:ellipsis}
.fchip b{color:#8b949e;font-weight:400;margin-left:2px}.fchip.ed{border-color:#4f7fc4;color:#cfe0ff}
.fchips{display:flex;gap:5px;flex-wrap:wrap;margin-bottom:4px}
.ev{border:1px solid #262c33;border-radius:12px;padding:12px 16px;margin-top:12px;background:#0d1117}
.ev-h{display:flex;justify-content:space-between;align-items:baseline;gap:12px}
.ev-g{display:grid;grid-template-columns:minmax(0,1.25fr) minmax(0,1fr);gap:22px;margin-top:6px}
.ev-l{margin:10px 0 5px;font-size:11.5px}
ul.mem{list-style:none;margin:0;padding:0;font-size:14px}
ul.mem li{display:grid;grid-template-columns:34px minmax(0,1fr);gap:2px 8px;padding:6px 0;border-top:1px solid #1c2128}
ul.mem li:first-child{border-top:0}ul.mem li.muted{display:block;padding-left:42px}
ul.mem .xn{font:600 13px var(--mono);color:#e3b341;text-align:right}
ul.mem .mq{color:#d6dde4;font-style:italic;overflow-wrap:anywhere}
ul.mem .mx{grid-column:2;font:12.5px var(--mono);color:#8b949e;display:flex;gap:4px;align-items:center}
ul.mem .mx .oc{width:16px;height:16px;font-size:10px;margin-left:4px}ul.mem .ids{margin-left:8px}
.wys{display:flex;flex-direction:column;gap:4px}
.wy{display:grid;grid-template-columns:minmax(0,1fr) 110px 40px;gap:8px;align-items:center;font-size:12.5px}
.wy .wl{overflow:hidden;text-overflow:ellipsis;white-space:nowrap;color:#c9d1d9}
.wy .wb{height:8px;background:#1c2128;border-radius:4px;overflow:hidden}
.wy .wb i{display:block;height:100%;background:#d4a72c;border-radius:4px}.wy .wn{text-align:right;color:#8b949e}
.ev-f{margin-top:10px;padding-top:8px;border-top:1px solid #1c2128;color:#c9d1d9}
div.wf-foot{padding:12px 10px 0;font-size:15px}div.wf-foot .caret:before{content:"·"}
details.gm p{margin:6px 0}details.gm ul{margin:4px 0;padding-left:20px}details.gm li{margin:2px 0}
.fxm-wrap{margin:4px 0 14px}.fxm-wrap .lab{margin-bottom:6px}
table.fxmat{width:auto;min-width:60%;font-size:13.5px}
table.fxmat th{vertical-align:bottom;font-weight:400}
table.fxmat th.fxc{width:104px;max-width:104px;white-space:normal;font-size:12px;line-height:1.25;text-align:center}
table.fxmat th.fxc a{color:#c9d1d9}.fxmat .fxn{display:block;color:#e3b341;font-weight:600}
table.fxmat td{padding:3px 7px;border-bottom:1px solid #1c2128}
table.fxmat td.fxg{white-space:nowrap}table.fxmat td.fxg a{color:#e6edf3}table.fxmat td.fxg .gc{margin-right:8px}
table.fxmat td.fxv{text-align:center;font-family:var(--mono);font-size:12.5px}
table.fxmat td.fxv span{display:inline-block;min-width:48px;padding:1px 6px;border-radius:5px;color:#fff}
@media (max-width:1000px){.ev-g{grid-template-columns:1fr}}
"""


def chip_css(n=CHIPS + 1):
    rules = [".chains .rd-0:checked~.chips .lb-0{border-color:#8b949e;background:#21262d}"]
    for j in range(1, n + 1):
        rules.append(".chains .rd-%d:checked~.chips .lb-%d{border-color:#8b949e;background:#21262d}" % (j, j))
        rules.append(".chains .rd-%d:checked~.exl .exr:not(.c-%d){display:none}" % (j, j))
    return "\n".join(rules)


def nav(compare_link=None, report_link=None, active="understand", patterns=True):
    """Top nav. In the report, 1/2 are anchors and 3 links to the compare page (greyed out without one); on the
    compare page (active='compare') 1/2 link back to the report (greyed out without --report-link)."""
    def item(label, href, title):
        if href is None:
            return '<span class="off" title="%s">%s</span>' % (esc(title), label)
        on = ' class="on"' if label.endswith("Compare") and active == "compare" else ""
        return '<a href="%s"%s>%s</a>' % (esc(href), on, label)
    if active == "compare":
        base = report_link
        u = item("1 Understand", base + "#understand" if base else None, "no report linked (pass --report-link FILE)")
        pt = item("2 Patterns", base + "#patterns" if base else None, "no report linked (pass --report-link FILE)")
        o = item("3 Optimize", base + "#optimize" if base else None, "no report linked (pass --report-link FILE)")
        c = item("4 Compare", "#compare", "")
    else:
        u, o = item("1 Understand", "#understand", ""), item("3 Optimize", "#optimize", "")
        pt = item("2 Patterns", "#patterns" if patterns else None, "patterns skipped (--no-patterns)")
        c = item("4 Compare", compare_link, "no compare report linked (pass --compare-link FILE)")
    return ('<nav class="steps"><span class="brand">tokenscope</span>%s<span class="sep">·</span>%s'
            '<span class="sep">·</span>%s<span class="sep">·</span>%s</nav>' % (u, pt, o, c))
