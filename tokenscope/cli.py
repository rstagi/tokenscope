"""tokenscope command line."""
import argparse
import json
import os
import random
import re
import sys
import time
from datetime import datetime, timedelta, timezone

from . import __version__
from .analyze import analyze_session, session_summary
from .goals import AREA_THRESHOLD, GOAL_THRESHOLD
from .pricing import COST_WEIGHTED_FORMULA, PRICES_SOURCE
from .parse import STATS, Session, discover, find_session, parse_ts, set_cache


SAMPLE_SEED = 8          # --sample is deterministic
WORKER_GB = 0.6          # assumed peak memory of one worker (a large session with its subagents)


def parse_since(s):
    if not s:
        return None
    m = re.fullmatch(r"(\d+)\s*([mhdw])", s.strip())
    if m:
        n, unit = int(m.group(1)), m.group(2)
        delta = {"m": timedelta(minutes=n), "h": timedelta(hours=n), "d": timedelta(days=n), "w": timedelta(weeks=n)}
        return datetime.now(timezone.utc) - delta[unit]
    t = parse_ts(s)
    if t is None:
        raise argparse.ArgumentTypeError("bad --since %r (use 7d, 24h, or 2026-10-01)" % s)
    return t


def fmt_tok(n, m_digits=1):
    """Compact token count: 950, 8.4k, 89.2k, 412k, 1.3M (one decimal below 100k everywhere)."""
    n = n or 0
    if round(n / 1000) >= 1000:
        return "%.*fM" % (m_digits, n / 1e6)
    if round(n / 100) >= 1000:
        return "%dk" % round(n / 1000)
    if n >= 1000:
        return "%.1fk" % (n / 1000)
    return str(n)


def fmt_usd(x):
    if x is None:
        return "-"
    return "$%.2f" % x if x >= 0.01 or not x else "$%.4f" % x


def headline(t):
    """One-line honest headline for a totals dict: cost-weighted tokens + API-equivalent $, then new / output /
    cache read separately; all-in last, as a muted detail."""
    unpriced = ""
    if t.get("unpriced_calls"):
        unpriced = " (%d calls unpriced: %s)" % (t["unpriced_calls"], ", ".join(t.get("unpriced_models") or []))
    return ("%s cost-weighted tokens, %s API-equivalent%s | new %s, output %s, cache read %s | all-in %s" % (
        fmt_tok(t.get("cost_weighted")), fmt_usd(t.get("api_cost_usd")), unpriced, fmt_tok(t.get("new")),
        fmt_tok(t.get("output")), fmt_tok(t.get("cache_read")), fmt_tok(t.get("all_in"))))


def sum_totals(analyses):
    out = {}
    for a in analyses:
        for k, v in a["totals"]["all"].items():
            if isinstance(v, list):
                out[k] = sorted(set(out.get(k, [])) | set(v))
            else:
                out[k] = out.get(k, 0) + v
    return out


def fmt_dur(s):
    if s is None:
        return "-"
    s = int(s)
    if s < 60:
        return "%ds" % s
    if s < 3600:
        return "%dm%02ds" % (s // 60, s % 60)
    return "%dh%02dm" % (s // 3600, s % 3600 // 60)


def _prefilter(args):
    """Discovered (unloaded) sessions after --sessions ids, then --sample / --max-sessions (deterministic)."""
    only = None
    if getattr(args, "sessions", None):
        from .compare import read_ids
        only = [i.strip() for i in read_ids(args.sessions) if i.strip()]
    found = [s for s in discover(getattr(args, "root", None), harness=getattr(args, "harness", None))
             if only is None or any(s.id.startswith(i) for i in only)]
    n = getattr(args, "sample", None)
    if n and n < len(found):
        keep = set(random.Random(SAMPLE_SEED).sample(range(len(found)), n))
        found = [s for i, s in enumerate(found) if i in keep]
    m = getattr(args, "max_sessions", None)
    if m:
        found = found[:m]
    return found


def _keep(s, args, since):
    if getattr(args, "project", None) and args.project.lower() not in (s.project + " " + s.slug).lower():
        return False
    return not (since and (s.start is None or s.start < since))


def select_sessions(args):
    since = parse_since(args.since)
    return [s for s in (x.load() for x in _prefilter(args)) if _keep(s, args, since)]


def _work(job):
    """Load + analyze (+ understand) one session; runs in a worker process with --jobs > 1."""
    path, slug, args, since, what = job
    set_cache(not args.no_cache)
    before = dict(STATS)
    s = Session(path, slug, harness=getattr(args, "harness", None)).load()
    stats = {k: STATS[k] - before[k] for k in STATS}
    if not _keep(s, args, since):
        return None, None, stats
    if what == "summary":
        return session_summary(s), None, stats
    a = analyze_session(s)
    us = None
    if what == "patterns":
        from . import patterns as pt
        from . import understand as un
        us = un.understand_session(s, analysis=a)
        return pt.session_records(s, a, us), us, stats
    if what in ("understand", "report"):
        from . import understand as un
        us = un.understand_session(s, analysis=a)
    if what == "report":  # analysis + compact pattern records (the parent mines patterns across sessions)
        from . import patterns as pt
        return (a, pt.session_records(s, a, us)), us, stats
    return a, us, stats


def default_jobs():
    return min(4, os.cpu_count() or 1)


def mem_available_gb():
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith("MemAvailable:"):
                    return int(line.split()[1]) / 1024 / 1024
    except (OSError, ValueError):
        pass
    return None


def collect(args, what="understand"):
    """[(analysis or summary, understood session or None)] for the selected sessions, possibly in parallel."""
    t0 = time.time()
    since = parse_since(args.since)
    found = _prefilter(args)
    jobs = max(1, args.jobs or default_jobs())
    avail = mem_available_gb()
    if avail is not None:  # each worker holds one session's events + analysis; keep room for the parent's results
        jobs = min(jobs, max(1, int(avail / WORKER_GB)))
    jobs = min(jobs, len(found)) or 1
    work = [(s.path, s.slug, args, since, what) for s in found]
    stats = {"hits": 0, "misses": 0, "transcripts": 0}
    out = []
    if jobs == 1:
        results = map(_work, work)
        pool = None
    else:
        import multiprocessing as mp
        pool = mp.get_context("fork" if "fork" in mp.get_all_start_methods() else "spawn").Pool(
            jobs, maxtasksperchild=50)
        results = pool.imap(_work, work, chunksize=1)
    try:
        for a, us, st in results:
            for k in stats:
                stats[k] += st[k]
            if a is not None:
                out.append((a, us))
    finally:
        if pool is not None:
            pool.close()
            pool.join()
    if args.verbose:
        calls = sum(a["calls"] if what == "summary" else (a[0] if isinstance(a, tuple) else a)["totals"]["all"]["calls"]
                    for a, _ in out)
        print("tokenscope: %d sessions, %d transcripts, %d calls, %.1fs, jobs %d, cache %d hits / %d misses%s, "
              "peak RSS %d MB" % (len(out), stats["transcripts"], calls, time.time() - t0, jobs, stats["hits"],
                                  stats["misses"], " (disabled)" if args.no_cache else "", peak_rss_mb()),
              file=sys.stderr)
    return out


def peak_rss_mb():
    import resource
    self_kb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    child_kb = resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss
    return max(self_kb, child_kb) // 1024


def cmd_sessions(args):
    rows = [r for r, _ in collect(args, "summary")]
    rows.sort(key=lambda r: r["start"] or "")
    if args.json:
        json.dump({"sessions": rows}, sys.stdout, indent=1, default=str)
        print()
        return 0
    if not rows:
        print("no sessions found")
        return 0
    print("%-8s  %-22s  %-16s  %5s  %8s  %8s  %-12s  %s" % ("id", "project", "start (UTC)", "calls", "cost-wtd",
                                                             "$ API-eq", "kind", "first prompt"))
    for r in rows:
        start = (r["start"] or "")[:16].replace("T", " ")
        calls = str(r["calls"]) + ("*" if r["subagents"] else "")
        prompt = " ".join((r["first_prompt"] or "").split())[:50]
        print("%-8s  %-22s  %-16s  %5s  %8s  %8s  %-12s  %s" % (
            r["session_id"][:8], r["project"][:22], start, calls, fmt_tok(r["cost_weighted"]),
            fmt_usd(r["api_cost_usd"]), r["intent"], prompt))
    if any(r["subagents"] for r in rows):
        print("* includes subagent calls")
    print("kind = keyword rule on the first prompt; `tokenscope goals` discovers what the requests were for")
    return 0


def print_summary(a):
    t, m = a["totals"]["all"], a["totals"]["main"]
    print("Session %s  (%s)" % (a["session_id"], a["project"]))
    print("  cwd %s  branch %s  models %s" % (a["cwd"], a["git_branch"], ", ".join(a["models"])))
    print("  %s -> %s  (%s)" % (a["start"], a["end"], fmt_dur(a["duration_seconds"])))
    it = a["intent"]
    print("  kind: %s%s" % (it["intent"], " (keyword %r)" % it["match"] if it.get("match") else ""))
    print("  prompt: %s" % " ".join((a["first_prompt"] or "").split())[:160])
    print()
    print("  %s" % headline(t))
    print()
    print("Totals (measured)       %10s %10s %10s %10s %10s %10s %6s  %s" % (
        "fresh", "cache_wr", "cache_rd", "output", "cost-wtd", "$ API-eq", "calls", "(all-in)"))
    for k in ("main", "subagents", "all"):
        x = a["totals"][k]
        print("  %-21s %10d %10d %10d %10d %10d %10s %6d  (%d)" % (
            k, x["fresh_input"], x["cache_write"], x["cache_read"], x["output"], x["cost_weighted"],
            fmt_usd(x["api_cost_usd"]), x["calls"], x["all_in"]))
    print("  cost-weighted = %s; $ = API-equivalent (%s)" % (COST_WEIGHTED_FORMULA, PRICES_SOURCE))
    print("  cache writes: 5m=%d 1h=%d   peak context %s" % (t["cache_write_5m"], t["cache_write_1h"],
                                                           fmt_tok(a["peak_context"])))
    print()
    print("Calls (main):  #  time      gap   context    new  top causes")
    for c in a["calls"]:
        causes = sorted(c["attribution"], key=lambda x: -x["tokens"])[:3]
        cs = "; ".join("%s %s%s" % (fmt_tok(x["tokens"]), x["label"][:40], "~" if x.get("est") else "")
                       for x in causes if x["tokens"])
        flags = (" [CACHE MISS]" if c.get("cache_miss") else "") + (" [COMPACT]" if c.get("compaction") else "")
        gap = "%ds" % c["gap_seconds"] if c["gap_seconds"] is not None else "-"
        print("  %13d  %s %5s %9s %6s  %s%s" % (c["index"], (c["timestamp"] or "")[11:19], gap,
                                               fmt_tok(c["context"]), fmt_tok(c["new_tokens"]), cs, flags))
    print("  (~ = estimate, chars/4)")
    if a["subagents"]:
        print()
        print("Subagents:")
        for s in a["subagents"]:
            print("  %s %-12s %-30s calls=%d all-in=%s out=%s (spawned by call #%s)" % (
                s["agent_id"][:10], s["type"], (s["description"] or "")[:30], s["calls"],
                fmt_tok(s["tokens"]["all_in"]), fmt_tok(s["tokens"]["output"]), s["parent_call_index"]))
    print()
    print("Tools: name  calls  result_est~  attributed")
    for name, e in sorted(a["per_tool"].items(), key=lambda kv: -kv[1]["result_tokens_attributed"]):
        print("  %-28s %4d %10s %10s%s" % (name, e["calls"], fmt_tok(e["result_tokens_est"]),
                                           fmt_tok(e["result_tokens_attributed"]),
                                           "  errors=%d" % e["errors"] if e["errors"] else ""))
    if a["per_file"]:
        print("Files read:")
        for p, f in sorted(a["per_file"].items(), key=lambda kv: -kv[1]["result_tokens_attributed"]):
            print("  %-40s reads=%d attributed=%s est~=%s%s%s" % (
                p[-40:], f["reads"], fmt_tok(f["result_tokens_attributed"]), fmt_tok(f["result_tokens_est"]),
                "  repeated=%d" % f["repeated_reads"] if f["repeated_reads"] else "",
                "  errors=%d" % f["errors"] if f["errors"] else ""))
    if a["big_bash_outputs"]:
        print("Big Bash outputs:")
        for b in a["big_bash_outputs"]:
            print("  %s  in-context=%d chars%s" % (b["command"][:70], b["chars_in_context"],
                                                   "  (persisted, full %d chars)" % b["full_chars"]
                                                   if b["persisted"] else ""))
    if a.get("hooks"):
        print("Hooks (output that entered context):")
        for name, h in sorted(a["hooks"].items(), key=lambda kv: -kv[1]["attributed_tokens"]):
            print("  %-28s fires=%d chars=%d attributed=%s carry=%s" % (
                name, h["fires"], h["chars"], fmt_tok(h["attributed_tokens"]), fmt_tok(h["carry_tokens"])))
    ins = a.get("instructions")
    if ins:
        print("Instruction files (est~ %s tokens):" % fmt_tok(ins["total_est_tokens"]))
        for e in ins["files"]:
            print("  %-40s ~%s%s" % (e["rel"][-40:], fmt_tok(e["est_tokens"]),
                                     "  (@-imported by %s)" % e["imported_by"] if e["imported_by"] else ""))
    for label, key in (("Cache misses", "cache_misses"), ("Compactions", "compactions"), ("Idle gaps", "idle_gaps")):
        if a[key]:
            print("%s: %s" % (label, json.dumps(a[key])))
    o = a["offered_vs_used"]
    print()
    if o["available"]:
        print("Offered vs used (est~ tokens of listing, re-read every call):")
        for k, title in (("mcp_servers", "MCP servers"), ("skills", "Skills"), ("subagent_types", "Subagent types")):
            used = [e["name"] for e in o[k] if e["used"]]
            print("  %-15s offered=%d used=%d  unused listing ~%s tok/call  used: %s" % (
                title, len(o[k]), len(used), fmt_tok(o[k + "_unused_est_tokens"]), ", ".join(used) or "-"))
    else:
        print("Offered vs used: unavailable (%s)" % o["reason"])
    if a["reported"]:
        print()
        print("Claude Code cost-state (reported): $%s  %s" % (a["reported"]["total_cost_usd"], json.dumps(
            {m: {k: v for k, v in u.items() if k.endswith("Tokens")} for m, u in a["reported"]["model_usage"].items()})))


def cmd_analyze(args):
    s = _load_one(args.session, getattr(args, "root", None), harness=getattr(args, "harness", None))
    if s is None:
        return 2
    a = analyze_session(s)
    if args.json:
        json.dump(a, sys.stdout, indent=1, default=str)
        print()
    else:
        print_summary(a)
    return 0


def _load_one(sid, root, harness=None):
    try:
        return find_session(sid, root, harness=harness)
    except LookupError as e:
        print("error: %s" % e, file=sys.stderr)
        return None


def _redact(args, *objs):
    if not getattr(args, "redact", False):
        return objs if len(objs) > 1 else objs[0]
    from .redact import redact_many
    out = redact_many(*objs)
    return out if len(objs) > 1 else out[0]


def cmd_report(args):
    from . import fixes as fx
    from . import report
    from . import understand as un
    if args.session:
        s = _load_one(args.session, getattr(args, "root", None), harness=getattr(args, "harness", None))
        if s is None:
            return 2
        a = analyze_session(s)
        u = _understand([s], {a["session_id"]: a}, args)
        fixes = fx.session_fixes(a)
        un.apply_fixes(u, fixes)
        pd = None
        if not args.no_patterns:
            from . import patterns as pt
            from . import patterns_html as ph
            recs = [pt.session_records(s, a)]
            pd = ph.attach_replays(pt.mine(recs, u), recs)
        a, fixes, u, pd = _redact(args, a, fixes, u, pd)
        html = report.render_session(a, fixes, understand=u, compare_link=args.compare_link, patterns=pd)
    else:
        t0 = time.time()
        got = collect(args, "understand" if args.no_patterns else "report")
        if not got:
            print("error: no sessions found", file=sys.stderr)
            return 1
        filters = ", ".join(x for x in ("project %s" % args.project if args.project else "",
                                        "since %s" % args.since if args.since else "",
                                        "sample %d" % args.sample if args.sample else "",
                                        "max %d sessions" % args.max_sessions if args.max_sessions else "") if x)
        if args.no_patterns:
            analyses, recs = [a for a, _ in got], None
        else:
            analyses, recs = [a for (a, _), _ in got], [r for (_, r), _ in got]
        corpus = args.corpus or len(analyses) > report.CORPUS_AUTO
        per = {a["session_id"]: fx.session_fixes(a) for a in analyses}
        u = _understand([], {a["session_id"]: a for a in analyses}, args, per_session=[us for _, us in got])
        cross = fx.aggregate(per, analyses)
        un.apply_fixes(u, [f for fs in per.values() for f in fs], update_status=False)
        un.apply_fixes(u, cross)
        pd = None
        if recs is not None:
            from . import patterns as pt
            from . import patterns_html as ph
            t1 = time.time()
            pd = ph.attach_replays(pt.mine(recs, u), recs)
            del recs
            if args.verbose:
                print("tokenscope: patterns %.1fs (collect %.1fs)" % (time.time() - t1, t1 - t0), file=sys.stderr)
        analyses, per, cross, u, pd = _redact(args, analyses, per, cross, u, pd)
        html = report.render_cross(analyses, fixes=per, filters=filters, cross_fixes=cross,
                                   understand=u, compare_link=args.compare_link, patterns=pd, corpus=corpus)
    if getattr(args, "redact", False):
        from .redact import final_pass
        html = final_pass(html)
    report.write(args.output, html)
    print("wrote %s (%s KB)" % (args.output, len(html.encode("utf-8")) // 1024))
    if args.open:
        open_in_browser(args.output)
    return 0


def goal_opts(args):
    """understand() keyword arguments from the shared goal options (--label-with-claude = deprecated alias)."""
    return {"name_with_claude": bool(getattr(args, "name_with_claude", False) or
                                     getattr(args, "label_with_claude", False)),
            "goal_threshold": getattr(args, "goal_threshold", None),
            "area_threshold": getattr(args, "area_threshold", None),
            "include_automated": bool(getattr(args, "include_automated", False)),
            "goal_unit": getattr(args, "goal_unit", None) or "auto"}


def namer_notes(args, namer):
    if getattr(args, "label_with_claude", False):
        print("note: --label-with-claude is deprecated; use --name-with-claude", file=sys.stderr)
    if namer.get("fallback_reason"):
        print("note: claude naming fell back to names from terms: %s" % namer["fallback_reason"], file=sys.stderr)


def _understand(sessions, analyses, args, per_session=None):
    from . import understand as un
    u = un.understand(sessions, analyses=analyses, per_session=per_session, **goal_opts(args))
    namer_notes(args, u["namer"])
    return u


def open_in_browser(path):
    """Best effort: open a written report in the default browser."""
    import webbrowser
    try:
        if not webbrowser.open("file://" + os.path.abspath(path)):
            print("note: could not open a browser; open %s yourself" % path, file=sys.stderr)
    except Exception as e:  # noqa: BLE001 - never fail the command because of the browser
        print("note: could not open a browser (%s)" % e, file=sys.stderr)


def cmd_fixes(args):
    from . import fixes as fx
    if args.overlay and not args.emit_config:
        print("error: --overlay needs --emit-config DIR", file=sys.stderr)
        return 2
    if args.session:
        s = _load_one(args.session, getattr(args, "root", None), harness=getattr(args, "harness", None))
        if s is None:
            return 2
        a = analyze_session(s)
        sessions, analyses, per_session = [s], [a], None
        out = fx.session_fixes(a)
        header = "Fixes for session %s (%s): %s" % (a["session_id"], a["project"], headline(a["totals"]["all"]))
    else:
        got = collect(args)
        if not got:
            print("error: no sessions found", file=sys.stderr)
            return 1
        sessions, analyses, per_session = [], [a for a, _ in got], [us for _, us in got]
        out = fx.aggregate({a["session_id"]: fx.session_fixes(a) for a in analyses}, analyses)
        header = "Fixes across %d sessions: %s" % (len(analyses), headline(sum_totals(analyses)))
    from . import understand as un
    un.apply_fixes(un.understand(sessions, analyses={a["session_id"]: a for a in analyses}, per_session=per_session),
                   out)
    if getattr(args, "redact", False):
        out, header = _redact(args, out, {"text": header, "analyses": analyses})
        header = header["text"]
    if args.json:
        json.dump(out, sys.stdout, indent=1, default=str)
        print()
    else:
        print(fx.to_text(out, header))
    if args.emit_config:
        if args.overlay:
            if len({a["cwd"] for a in analyses}) > 1:
                print("error: --overlay needs sessions from one repo (use --session or --project)", file=sys.stderr)
                return 2
            # CLAUDE.md content comes from the transcript: use the latest session that recorded it
            with_md = [a for a in analyses if any("content" in e for e in (a.get("instructions") or {}).get("files", []))]
            latest = max(with_md or analyses, key=lambda a: a["start"] or "")
            paths = fx.emit_overlay(out, latest, args.emit_config, analyses=analyses)
        else:
            paths = fx.emit_config(out, args.emit_config)
        for p in paths:
            print("wrote %s" % p, file=sys.stderr if args.json else sys.stdout)
    return 0


def cmd_compare(args):
    from . import compare, report
    sets = args.a_sessions or args.b_sessions
    if sets:
        if not (args.a_sessions and args.b_sessions) or args.a or args.b:
            print("error: use either A B (two sessions) or --a-sessions and --b-sessions", file=sys.stderr)
            return 2
        side = {}
        for k, spec in (("A", args.a_sessions), ("B", args.b_sessions)):
            ids = compare.read_ids(spec)
            if not ids:
                print("error: no session ids in %s" % spec, file=sys.stderr)
                return 2
            side[k] = [_load_one(i, getattr(args, "root", None), harness=getattr(args, "harness", None)) for i in ids]
            if any(s is None for s in side[k]):
                return 2
        d = compare.compare_sets(side["A"], side["B"], **{k: v for k, v in goal_opts(args).items()
                                                          if k not in ("include_automated", "goal_unit")})
        namer_notes(args, d["namer"])
        d = _redact(args, d)
        print(compare.sets_to_text(d))
        render = lambda: compare.render_sets_html(d, report_link=args.report_link)
    else:
        if not (args.a and args.b):
            print("error: give two sessions (A B) or --a-sessions FILE|ID,ID --b-sessions FILE|ID,ID", file=sys.stderr)
            return 2
        sa = _load_one(args.a, getattr(args, "root", None), harness=getattr(args, "harness", None))
        sb = _load_one(args.b, getattr(args, "root", None), harness=getattr(args, "harness", None))
        if sa is None or sb is None:
            return 2
        d = _redact(args, compare.compare_data(analyze_session(sa), analyze_session(sb)))
        print(compare.to_text(d))
        render = lambda: compare.render_html(d, report_link=args.report_link)
    if args.output:
        html = render()
        if getattr(args, "redact", False):
            from .redact import final_pass
            html = final_pass(html)
        report.write(args.output, html)
        print("wrote %s (%s KB)" % (args.output, len(html.encode("utf-8")) // 1024))
        if args.open:
            open_in_browser(args.output)
    elif args.open:
        print("note: --open needs -o/--output", file=sys.stderr)
    return 0


def cmd_goals(args):
    from . import understand as un
    if args.session:
        s = _load_one(args.session, getattr(args, "root", None), harness=getattr(args, "harness", None))
        if s is None:
            return 2
        sessions, analyses, per_session = [s], [analyze_session(s)], None
    else:
        got = collect(args)
        sessions, analyses, per_session = [], [a for a, _ in got], [us for _, us in got]
    if not analyses:
        print("error: no sessions found", file=sys.stderr)
        return 1
    from . import fixes as fx
    u = un.understand(sessions, analyses={a["session_id"]: a for a in analyses}, per_session=per_session,
                      **goal_opts(args))
    per = {a["session_id"]: fx.session_fixes(a) for a in analyses}
    if len(analyses) == 1:
        un.apply_fixes(u, per[analyses[0]["session_id"]])
    else:
        un.apply_fixes(u, [f for fs in per.values() for f in fs], update_status=False)
        un.apply_fixes(u, fx.aggregate(per, analyses))
    if getattr(args, "redact", False):
        u = _redact(args, u, analyses)[0]
    if args.json:
        json.dump(u, sys.stdout, indent=1, default=str, ensure_ascii=False)
        print()
    else:
        print(un.to_text(u, fmt_tok))
    namer_notes(args, u["namer"])
    return 0


def cmd_patterns(args):
    from . import patterns as pt
    from . import understand as un
    got = collect(args, "patterns")
    if not got:
        print("error: no sessions found", file=sys.stderr)
        return 1
    t0 = time.time()
    records, per_session = [r for r, _ in got], [us for _, us in got]
    u = un.understand([], per_session=per_session, **goal_opts(args))
    timings = {"goals": round(time.time() - t0, 3)}
    d = pt.mine(records, u, min_support=args.min_support, top=args.top, timings=timings)
    d["sessions"] = len(records)
    d["transcripts"] = sum(len(r["transcripts"]) for r in records)
    if getattr(args, "redact", False):
        from .redact import redact
        d = redact(d, os.environ.get("TOKENSCOPE_REDACT_SALT", "tokenscope"))
    if args.json:
        json.dump(d, sys.stdout, indent=1, default=str, ensure_ascii=False)
        print()
    else:
        print("%d sessions, %d transcripts" % (d["sessions"], d["transcripts"]))
        print(pt.to_text(d, fmt_tok, fmt_usd))
    return 0


def cmd_live(args):
    if args.action == "intents-mcp":
        from .live import intent_mcp
        return intent_mcp.serve()
    if args.action == "intents-install":
        from .live import intent_mcp
        print(intent_mcp.install_text())
        if args.write_claude_md:
            intent_mcp.write_claude_md(args.write_claude_md)
            print("appended the snippet to %s" % args.write_claude_md)
        return 0
    if args.action == "alerts-config":
        from . import alerts
        print("# tokenscope alerts config: ~/.config/tokenscope/alerts.toml (or --alerts-config FILE); "
              "every key with its default\n" + "\n".join(alerts.keys_doc()))
        return 0
    if args.action == "hooks":
        from .live.hook import settings_snippet
        print(json.dumps(settings_snippet(), indent=2))
        return 0
    from .live import server
    return server.main(args)


def intents_view(path, haiku=False, label_batch=None):
    """(steps, groups, labeller stats) of one transcript replayed offline; all steps closed."""
    from .live.replay import replay
    state, s = replay(path, redact=False, model_labels=haiku)
    if s is None:
        return [], [], {}
    lab = s.labeller
    steps, gs = state.steps(s, final=True)
    if haiku:
        from .stepnames import BATCH, label_batch as default_batch
        run = label_batch or default_batch
        goal = s.goal["name"] if s.goal else None
        while True:
            pend = lab.pending(steps)
            if not pend:
                break
            batch = pend[:BATCH]
            res = run(batch, goal)
            lab.add_model_labels(res.get("labels"), res.get("groups"), res.get("usage"))
            if not res.get("labels"):
                lab.give_up([x["i"] for x in batch])
            steps, gs = state.steps(s, final=True)
    gs = lab.finalize(steps)   # whatever the model did not label keeps its rule label
    return steps, gs, dict(lab.stats, declared_calls=sum(
        1 for t in s.main.turn_steps for x in t.get("_raw") or () if x["name"].endswith("declare_intent")))


def cmd_alerts(args):
    """Replay one session through the live model and print its alerts (privacy-safe messages)."""
    from . import alerts
    from .parse import resolve_root
    from .live.replay import find_transcript, replay
    try:
        cfg = alerts.from_args(args)
        path = find_transcript(args.session, resolve_root(getattr(args, "root", None), harness=getattr(args, "harness", None)))
    except (LookupError, OSError, ValueError) as e:
        print(str(e), file=sys.stderr)
        return 1
    state, s = replay(path, redact=True, alerts_config=cfg)
    got = state.alerts_public(s) if s is not None else []
    if args.json:
        print(json.dumps({"session": s.id if s else None, "alerts": got}, indent=2))
        return 0
    trig = sum(a.get("count") or 1 for a in got)
    print("%s: %d alert%s%s" % (s.id[:8] if s else "?", len(got), "" if len(got) == 1 else "s",
                                " (one per kind; %d triggers)" % trig if trig > len(got) else ""))

    def where(a):
        if a["step"] is None:
            return ""
        return " step %s%s" % ("%s-%s" % (a["first"], a["step"]) if a["first"] != a["step"] else a["step"],
                               " (subagent)" if a["agent"] else "")
    for a in got:   # one per kind (alerts.collapse): the latest trigger leads
        n = a.get("count") or 1
        print("  [%s] %s/%s%s%s: %s" % (a["severity"], a["kind"], a["rule"], where(a), " \u00d7%d" % n if n > 1 else "",
                                         a["message"]))
        for m in (a.get("reasons") or [])[1:]:   # the other alerts of the same step (alerts.group)
            print("      + [%s] %s/%s: %s" % (m["severity"], a["kind"], m["rule"], m["message"]))
        for t in (a.get("triggers") or [])[-2::-1]:   # the earlier triggers, newest first
            print("      earlier: [%s] %s/%s%s: %s" % (t["severity"], a["kind"], "+".join(t.get("rules") or [t["rule"]]),
                                                       where(t), t["message"]))
    return 0


def cmd_intents(args):
    from .parse import resolve_root
    from .live.replay import find_transcript
    from .steps import summary
    try:
        path = find_transcript(args.session, resolve_root(getattr(args, "root", None), harness=getattr(args, "harness", None)))
    except LookupError as e:
        print(str(e), file=sys.stderr)
        return 1
    steps, gs, stats = intents_view(path, haiku=args.haiku)
    if args.json:
        out = [{k: (dict(v) if k in ("tools", "intents") else v) for k, v in x.items() if not k.startswith("_")}
               for x in steps]
        print(json.dumps({"steps": out, "groups": gs, "stats": stats}, indent=2))
        return 0
    gi = {i: g for g in gs for i in range(g["first"], g["last"] + 1)}

    def line(x, indent):
        tags = "".join(t for t, on in ((" \u21b5prompt", x["prompt_before"]),) if on)
        return "%s%-12s %-46s %-9s %7s %8s  %s%s" % (
            indent, "[%s]" % x["cat"].upper(), x["label"][:46], x["source"], fmt_tok(x["cw"]), fmt_usd(x["usd"]),
            summary(x), tags)
    shown = set()
    for x in steps:
        g = gi.get(x["i"])
        if g is None:
            print(line(x, ""))
        elif id(g) not in shown:
            shown.add(id(g))
            print("\u25be %-12s %-44s %-9s %7s %8s  %d steps \u00b7 %s" % (
                "[%s]" % g["cat"].upper(), g["label"][:44], g["source"], fmt_tok(g["cw"]), fmt_usd(g["usd"]),
                g["steps"], g["summary"]))
            for y in steps[g["first"]:g["last"] + 1]:
                print(line(y, "    "))
    print("\n%d steps, %d groups; declare_intent calls: %d; haiku calls: %d (%d tokens, %s)" % (
        len(steps), len(gs), stats.get("declared_calls", 0), stats.get("haiku_calls", 0),
        stats.get("haiku_tokens", 0), fmt_usd(stats.get("haiku_usd", 0))))
    return 0


def _cut(text, n):
    return text if len(text) <= n else text[:n - 1].rstrip() + "…"


def top_lines(r):
    """The `tokenscope top` table: the window's totals, then its top sessions, intents and steps (--by intent), or its
    top sessions, goals or turns (the session's goal after a goal's or turn's label)."""
    from .top import BY_KEY
    t = r["total"]
    by = r.get("by") or "intent"
    out = ["Top spenders%s, last %s (%s .. %s): %s API-equivalent, %s CW, %s tokens, %d sessions, %d calls" % (
        "" if by == "intent" else " by " + by, r["window"], r["start_iso"], r["end_iso"], fmt_usd(t["usd"]),
        fmt_tok(t["cw"]), fmt_tok(t["tokens"]), t["sessions"], t["calls"])]
    keys = ("sessions", "intents", "steps") if by == "intent" else (BY_KEY[by],)
    for key in keys:
        out.append("")
        out.append("%-9s %8s %6s %7s %7s %7s %7s  %s" % (key.upper(), "$", "share", "CW", "tokens", "wall", "active",
                                                          "session  label"))
        if not r[key]:
            out.append("  (nothing in this window)")
        for x in r[key]:
            label = x["label"] or "-"
            if key in ("intents", "steps"):
                label = "[%s] %s" % (str(x.get("cat") or "other").upper(), label)
            elif key == "sessions":
                if x.get("project"):
                    label = "%s · %s" % (x["project"], label)
            else:
                n = x.get("sessions", 1) - 1
                more = "  +%d session%s" % (n, "s" if n > 1 else "") if n else ""
                of = x.get("session_label") or "-"
                label = _cut(label, 56) + ("" if of == x["label"] else "  « " + _cut(of, 34)) + more
            out.append("%-9s %8s %5.1f%% %7s %7s %7s %7s  %s  %s" % (
                "", fmt_usd(x["usd"]), x["share"], fmt_tok(x["cw"]), fmt_tok(x["tokens"]), fmt_dur(x["wall_s"]),
                fmt_dur(x["active_s"]), x["short"], label if key in ("goals", "turns") else label[:70]))
    return out


def cmd_top(args):
    from .top import parse_now, top_spenders
    from .parse import resolve_root
    try:
        now = parse_now(args.now) if args.now else None
        root = args.index_root or resolve_root(harness=getattr(args, "harness", None))
        r = top_spenders(args.window, now=now, root=root, limit=args.limit, by=args.by)
    except ValueError as e:
        raise argparse.ArgumentTypeError(str(e))
    if args.json:
        print(json.dumps(r, indent=2))
    else:
        print("\n".join(top_lines(r)))
    return 0


def main(argv=None):
    p = argparse.ArgumentParser(prog="tokenscope", description="Explain where agent tokens go (Claude Code, Google Antigravity).")
    p.add_argument("--version", action="version", version="tokenscope " + __version__)
    root_parent = argparse.ArgumentParser(add_help=False)
    root_parent.add_argument("--root", default=argparse.SUPPRESS,
                             help="transcripts root (default ~/.claude/projects, ~/.gemini/antigravity-cli/brain or $TOKENSCOPE_ROOT)")
    root_parent.add_argument("--harness", choices=["claude", "antigravity", "auto"], default=None,
                             help="agent harness (claude, antigravity, or auto-detect)")
    goal_parent = argparse.ArgumentParser(add_help=False)
    goal_parent.add_argument("--goal-threshold", type=float, metavar="F",
                             help="similarity at which requests merge into one goal (default %.2f)" % GOAL_THRESHOLD)
    goal_parent.add_argument("--area-threshold", type=float, metavar="F",
                             help="similarity at which goals merge into one area (default %.2f)" % AREA_THRESHOLD)
    goal_parent.add_argument("--name-with-claude", action="store_true",
                             help="name goals and areas with one batched `claude -p --model haiku` call (cached in "
                                  "~/.cache/tokenscope/goal_names.json; sends prompt heads and file names to Claude)")
    goal_parent.add_argument("--label-with-claude", action="store_true", help="deprecated alias of --name-with-claude")
    goal_parent.add_argument("--goal-unit", choices=["thread", "request", "auto"], default="auto",
                             help="cluster threads (a request plus its in-session follow-ups) or single requests; "
                                  "auto = thread when >=30%% of requests are follow-ups (default auto)")
    goal_parent.add_argument("--redact", action="store_true",
                             help="mask prompts, goal names, paths, projects, session/workflow ids, private MCP "
                                  "servers, command args and error text in the output")
    goal_parent.add_argument("--include-automated", action="store_true",
                             help="discover goals over automated requests too (task notifications, hooks, headless "
                                  "claude -p, subagents); default: human requests only")
    scale_parent = argparse.ArgumentParser(add_help=False)
    scale_parent.add_argument("--no-cache", action="store_true",
                              help="do not read/write the parsed-transcript cache (~/.cache/tokenscope/parsed)")
    scale_parent.add_argument("--jobs", type=int, metavar="N",
                              help="worker processes for per-session analysis (default min(4, cpus); capped by "
                                   "available memory)")
    scale_parent.add_argument("--sample", type=int, metavar="N", help="deterministic random sample of N sessions")
    scale_parent.add_argument("--max-sessions", type=int, metavar="N", help="at most N sessions (after --sample)")
    scale_parent.add_argument("-v", "--verbose", action="store_true",
                              help="print a timing summary (sessions, transcripts, calls, seconds, cache) to stderr")
    p.add_argument("--root", default=None, help=argparse.SUPPRESS)
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("sessions", parents=[root_parent, scale_parent], help="list sessions")
    sp.add_argument("--project")
    sp.add_argument("--since", help="7d, 24h, 30m, 2w or a date like 2026-10-01")
    sp.add_argument("--sessions", metavar="FILE|ID,ID", help="only these sessions (file with one id per line, or comma list; prefixes ok)")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=cmd_sessions)

    ap = sub.add_parser("analyze", parents=[root_parent, scale_parent], help="analyze one session")
    ap.add_argument("session", help="session id or unique prefix")
    ap.add_argument("--json", action="store_true")
    ap.set_defaults(func=cmd_analyze)

    rp = sub.add_parser("report", parents=[root_parent, goal_parent, scale_parent], help="write a self-contained HTML report")
    rp.add_argument("--project")
    rp.add_argument("--since", help="7d, 24h, 30m, 2w or a date like 2026-10-01")
    rp.add_argument("--sessions", metavar="FILE|ID,ID", help="only these sessions (file with one id per line, or comma list; prefixes ok)")
    rp.add_argument("--session", help="session id or unique prefix (session-focused report)")
    rp.add_argument("-o", "--output", default="tokenscope-report.html")
    rp.add_argument("--open", action="store_true", help="open the report in the default browser")
    rp.add_argument("--no-patterns", action="store_true", help="skip the Patterns section (cross-session miners)")
    rp.add_argument("--corpus", action="store_true",
                    help="corpus mode: session views only for the 6 largest sessions, top 30 fixes "
                         "(automatic above 40 sessions)")
    rp.add_argument("--compare-link", metavar="FILE",
                    help="compare report to link from the top nav (4 Compare); greyed out when omitted")
    rp.set_defaults(func=cmd_report)

    fp = sub.add_parser("fixes", parents=[root_parent, scale_parent], help="suggest fixes (rule-based, from the analysis)")
    fp.add_argument("--redact", action="store_true", help="mask private data in the output (see redact.py)")
    fp.add_argument("--session", help="session id or unique prefix")
    fp.add_argument("--project")
    fp.add_argument("--since", help="7d, 24h, 30m, 2w or a date like 2026-10-01")
    fp.add_argument("--sessions", metavar="FILE|ID,ID", help="only these sessions (file with one id per line, or comma list; prefixes ok)")
    fp.add_argument("--json", action="store_true")
    fp.add_argument("--emit-config", metavar="DIR",
                    help="write CLAUDE.md.suggested and settings.suggested.json into DIR (never touches real config)")
    fp.add_argument("--overlay", action="store_true",
                    help="with --emit-config: write files at their real repo paths (CLAUDE.md, .claude/settings.json, "
                         "...) so `cp -r DIR/. <repo>` applies them, plus TOKENSCOPE_CHANGES.md")
    fp.set_defaults(func=cmd_fixes)

    cp = sub.add_parser("compare", parents=[root_parent, goal_parent, scale_parent],
                        help="compare two sessions, or two sets of sessions per goal (A = before, B = after)")
    cp.add_argument("a", nargs="?", help="session A id or prefix (before)")
    cp.add_argument("b", nargs="?", help="session B id or prefix (after)")
    cp.add_argument("--a-sessions", metavar="FILE|ID,ID",
                    help="set A (before): a file of session ids (one per line) or comma-separated ids/prefixes")
    cp.add_argument("--b-sessions", metavar="FILE|ID,ID", help="set B (after), same format")
    cp.add_argument("-o", "--output", help="also write a self-contained HTML report")
    cp.add_argument("--report-link", metavar="FILE",
                    help="report to link from the top nav (1 Understand, 2 Patterns, 3 Optimize); greyed out when omitted")
    cp.add_argument("--open", action="store_true", help="open the HTML report in the default browser (needs -o)")
    cp.set_defaults(func=cmd_compare)

    for name, hlp in (("goals", "what the requests were for: goals discovered from the data, grouped into areas, "
                                "with paths (tool chains), outcomes and evidence"),
                      ("understand", "alias of `goals`")):
        up = sub.add_parser(name, parents=[root_parent, goal_parent, scale_parent], help=hlp)
        up.add_argument("--project")
        up.add_argument("--since", help="7d, 24h, 30m, 2w or a date like 2026-10-01")
        up.add_argument("--sessions", metavar="FILE|ID,ID", help="only these sessions (file with one id per line, or comma list; prefixes ok)")
        up.add_argument("--session", help="session id or unique prefix")
        up.add_argument("--json", action="store_true")
        up.set_defaults(func=cmd_goals)

    pp = sub.add_parser("patterns", parents=[root_parent, goal_parent, scale_parent],
                        help="mine recurring real behavior: repeated sequences, re-discovery, retries, wasted context, "
                             "divergent paths, corrections, compactions, workflow fan-out, tool overhead")
    pp.add_argument("--project")
    pp.add_argument("--since", help="7d, 24h, 30m, 2w or a date like 2026-10-01")
    pp.add_argument("--sessions", metavar="FILE|ID,ID", help="only these sessions (file with one id per line, or comma list; prefixes ok)")
    pp.add_argument("--json", action="store_true")
    pp.add_argument("--top", type=int, metavar="N", help="only the N patterns with the most cost-weighted tokens")
    pp.add_argument("--min-support", type=int, metavar="N",
                    help="minimum sessions for sequences / start re-discovery / unused MCP (default 3)")
    pp.set_defaults(redact=False)
    pp.add_argument("--redact-patterns", dest="redact", action="store_true",
                    help="mask prompts, paths, projects, ids, private MCP servers and command args "
                         "(+ terms from $TOKENSCOPE_REDACT_TERMS)")
    pp.set_defaults(func=cmd_patterns)

    lp = sub.add_parser("live", help="watch sessions as they happen (local web board); "
                                     "`tokenscope live hooks` prints the optional hooks settings snippet")
    lp.add_argument("action", nargs="?", choices=["serve", "hooks", "intents-mcp", "intents-install", "alerts-config"],
                    default="serve",
                    help="serve (default); hooks: print a settings.json snippet for precise status; intents-mcp: "
                         "run the declare_intent MCP server (stdio); intents-install: print how to register it; "
                         "alerts-config: list every alerts config key with its default")
    lp.add_argument("--write-claude-md", metavar="PATH",
                    help="with intents-install: append the declare_intent snippet to this CLAUDE.md")
    from .live.server import add_arguments as live_arguments
    live_arguments(lp)
    lp.set_defaults(func=cmd_live)

    ap = sub.add_parser("alerts", help="cost / off-task / looping alerts of one session (replays its transcript)")
    ap.add_argument("session", help="transcript path (.jsonl) or session id / unique prefix")
    ap.add_argument("--root", default=argparse.SUPPRESS,
                    help="transcripts root (default ~/.claude/projects, ~/.gemini/antigravity-cli/brain or $TOKENSCOPE_ROOT)")
    ap.add_argument("--harness", choices=["claude", "antigravity", "auto"], default=None,
                    help="agent harness (claude, antigravity, or auto-detect)")
    ap.add_argument("--json", action="store_true")
    from .alerts import add_arguments as alert_arguments
    alert_arguments(ap)
    ap.set_defaults(func=cmd_alerts)

    ip = sub.add_parser("intents", help="steps and intent groups of one session (replays its transcript)")
    ip.add_argument("session", help="transcript path (.jsonl) or session id / unique prefix")
    ip.add_argument("--root", default=argparse.SUPPRESS,
                    help="transcripts root (default ~/.claude/projects, ~/.gemini/antigravity-cli/brain or $TOKENSCOPE_ROOT)")
    ip.add_argument("--harness", choices=["claude", "antigravity", "auto"], default=None,
                    help="agent harness (claude, antigravity, or auto-detect)")
    ip.add_argument("--haiku", action="store_true",
                    help="label steps without own signals with `claude -p --model haiku` (masked text, cached)")
    ip.add_argument("--json", action="store_true")
    ip.set_defaults(func=cmd_intents)

    tp = sub.add_parser("top", help="top spenders (sessions, goals, turns, intents) in the last 5h / 24h / 7d / 30d")
    tp.add_argument("--window", default="5h", help="5h (usage-limit window), 24h, 7d or 30d (default 5h)")
    tp.add_argument("--by", default="intent", choices=("session", "goal", "turn", "intent"),
                    help="rank sessions, goals (labelled user goals, across turns and resumed sessions), turns (one "
                         "per user prompt) or intents (default: sessions, intents and steps)")
    tp.add_argument("--index-root", default=None, metavar="DIR",
                    help="transcripts to rank (default $TOKENSCOPE_INDEX_ROOT, else default harness root)")
    tp.add_argument("--harness", choices=["claude", "antigravity", "auto"], default=None,
                    help="agent harness (claude, antigravity, or auto-detect)")
    tp.add_argument("--now", metavar="ISO", help="end of the window (default: now), e.g. 2026-10-12T09:00:00Z")
    tp.add_argument("--limit", type=int, default=10, metavar="N", help="rows per section (default 10)")
    tp.add_argument("--json", action="store_true")
    tp.set_defaults(func=cmd_top)

    args = p.parse_args(argv)
    set_cache(not getattr(args, "no_cache", False))
    try:
        rc = args.func(args)
        sys.stdout.flush()
        return rc
    except argparse.ArgumentTypeError as e:
        p.error(str(e))
    except BrokenPipeError:
        os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        return 0


if __name__ == "__main__":
    sys.exit(main())
