#!/usr/bin/env python3
"""Offline check of the cross-session hints (tokenscope/xindex.py) with a chronological leave-future-out replay.

    python3 tools/eval_xhints.py [--root DIR | --index-root DIR] [--skills-dir DIR ...] [--out FILE] [--sample 20]
                                 [--seed 0] [--jobs N] [--explain SID ...]

The index is built (or loaded from the cache) once over every main transcript under the root. Then, for each
session in start order, each finished user request is an idle point: the hint is computed as the live recap would
compute it there, against an index holding only the sessions that STARTED EARLIER (never the session itself, never
the future). Prints the firing rate (idle points, sessions) per rule and writes a random sample of up to --sample
firings with their numbers (median discovery, the remedy's context cost, net, kind), evidence and the matched prior
requests (their step labels) to --out (default ./xhints_sample.log). --explain SID (a session id prefix, repeatable)
adds every remedy weighed at that session's idle points, fired or not. Skill drafts go to a temp dir (not the cache)
unless --keep-drafts.
"""
import argparse
import os
import random
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tokenscope import xindex as xi  # noqa: E402


def idle_points(entry):
    """[(current records)] per request of a session: the request and its groups."""
    out, cur = [], []
    for r in entry["records"]:
        if r["level"] == "request" and cur:
            out.append(cur)
            cur = []
        cur.append(r)
    if cur:
        out.append(cur)
    return out


def main(argv=None):
    p = argparse.ArgumentParser(prog="eval_xhints")
    p.add_argument("--root", "--index-root", dest="root", default=None,
                   help="transcripts root (default $TOKENSCOPE_INDEX_ROOT, else ~/.claude/projects)")
    p.add_argument("--skills-dir", action="append", default=[], help="extra skills dir (repeatable)")
    p.add_argument("--out", default="xhints_sample.log")
    p.add_argument("--sample", type=int, default=20)
    p.add_argument("--seed", type=int, default=0)
    p.add_argument("--jobs", type=int, default=None, help="replay processes for transcripts not in the cache")
    p.add_argument("--keep-drafts", action="store_true", help="write skill drafts to the cache dir")
    p.add_argument("--explain", action="append", default=[], help="session id prefix: log every remedy weighed there")
    a = p.parse_args(argv)

    idx = xi.XIndex(a.root)
    st = idx.build(jobs=a.jobs)
    t1 = time.time()
    stats2 = xi.XIndex(a.root).build()   # a rebuild from the cache, for the timing line
    drafts = None if a.keep_drafts else tempfile.mkdtemp(prefix="xhints-drafts-")
    order = sorted(idx.sessions.values(), key=lambda e: (e.get("ts0") or 0, e["sid"]))
    skills_by_cwd = {}
    points = 0
    fired = []
    sessions_fired = set()
    per_rule = {}
    explained = []
    t2 = time.time()
    for e in order:
        if e.get("ts0") is None:
            continue
        history = idx.records(exclude_sid=e["sid"], before=e["ts0"])
        cwd = e.get("cwd")
        if cwd not in skills_by_cwd:
            skills_by_cwd[cwd] = xi.load_skills(xi.skill_dirs(cwd, a.skills_dir))
        for k, cur in enumerate(idle_points(e)):
            points += 1
            trace = []
            h = xi.suggest(cur, history, skills_by_cwd[cwd], e.get("skills") or (), cache=drafts, trace=trace)
            if any(e["sid"].startswith(x) for x in a.explain):
                explained.append((e, k, trace, h))
            if h:
                fired.append((e, k, cur, h))
                sessions_fired.add(e["sid"])
                per_rule[h["rule"]] = per_rule.get(h["rule"], 0) + 1
    t_eval = time.time() - t2
    n_sess = len(order)
    lines = ["root: %s" % idx.root,
             "index: %d transcripts, %d sessions, %d records; build %.1fs (%d replayed), rebuild from cache %.2fs"
             % (st["transcripts"], st["sessions"], st["records"], st["seconds"], st["replayed"], stats2["seconds"]),
             "skills indexed (first session's dirs): %d" % len(next(iter(skills_by_cwd.values()), [])),
             "thresholds: " + ", ".join("%s=%s" % (k, getattr(xi, k)) for k in (
                 "SIM_MIN", "ANCHOR_TOK", "ANCHOR_FILES", "MIN_OCC", "MIN_SESSIONS", "PRIOR_MIN_CW", "CUR_MIN_CW",
                 "MIN_NET", "MIN_NET_SHARE", "CONSISTENCY_MIN", "SK_MIN_OVERLAP", "SK_MIN_COVER")),
             "%-16s %12s %9s %9s %12s" % ("rule", "idle points", "firings", "sessions", "firing rate")]
    for rule in ("repeat", "claude_md", "consistency", "skill_filemap", "existing_skill"):
        f = [x for x in fired if x[3]["rule"] == rule]
        lines.append("%-16s %12d %9d %9d %11.1f%%" % (rule, points, len(f), len({x[0]["sid"] for x in f}),
                                                     100.0 * len(f) / points if points else 0.0))
    lines.append("%-16s %12d %9d %9d %11.1f%%   (sessions: %d/%d = %.1f%%)"
                 % ("any", points, len(fired), len(sessions_fired), 100.0 * len(fired) / points if points else 0.0,
                    len(sessions_fired), n_sess, 100.0 * len(sessions_fired) / n_sess if n_sess else 0.0))
    lines.append("evaluation: %.2fs for %d idle points (%.1f ms each)" % (t_eval, points,
                                                                        1000.0 * t_eval / points if points else 0))
    report = "\n".join(lines)
    print(report)

    rng = random.Random(a.seed)
    pick = rng.sample(fired, min(a.sample, len(fired)))
    out = [report, ""]
    for e, k, trace, h in explained:
        out.append("explain: session %s  request #%d  -> %s" % (e["sid"][:8], k, h["rule"] if h else "no hint"))
        for t in trace:
            out.append("  %s level, %d matched runs:" % (t["level"], t["runs"]))
            for o in t["opts"]:
                out.append("    %-15s discovery %6s  cost %6s  net %7s  %s" % (
                    o["rule"], xi._fk(o["disc"]), xi._fk(o["cost"]), xi._fk(o["net"]), "passes" if o["ok"] else "fails"))
                out.append("      lists %d files: %s" % (len(o["files"] or ()), ", ".join(o["files"] or ()) or "-"))
                out += ["      run %s (score %.2f, %s turns): removable %6s = reads %6s + searches %6s; left out %6s"
                        % (x["sid"], x["score"], x["turns"], xi._fk(x["read"] + x["search"]), xi._fk(x["read"]),
                           xi._fk(x["search"]), xi._fk(x["kept"])) for x in o["per_run"]]
        if not trace:
            out.append("  no repeat passed the match gates")
    out += ["", "sample of %d firings (seed %d)" % (len(pick), a.seed)]
    for e, k, cur, h in pick:
        req = cur[0]
        out += ["", "=" * 100,
                "session %s  project %s  request #%d  (%s CW, %s level)" % (e["sid"][:8], e["project"], k,
                                                                         xi._fk(req["cw"]), h["matches"][0]["level"]),
                "  ➜ %s" % h["action"], "    %s" % h["evidence"],
                "    kind: %s  discovery median: %s  %s cost: %s  net: %s/run" % (
                    h["rule"], xi._fk(h["discovery_cw"]), "file-map" if h["rule"] in ("claude_md", "skill_filemap")
                    else "skill", xi._fk(h["cost_cw"]), xi._fk(h["net_cw"])),
                "    (%s)" % h["estimate"]]
        if h.get("skill"):
            out.append("    skill: %s%s" % (h["skill"], "  words: " + ", ".join(h["skill_words"]) if h.get("skill_words") else ""))
        if h.get("stub_path"):
            out.append("    stub: %s" % h["stub_path"])
        out.append("  current request steps:")
        out += ["    [%s] %s · %s" % (x["cat"].upper(), x["label"], xi._fk(x["cw"])) for x in req["steps"][:15]]
        if len(req["steps"]) > 15:
            out.append("    … +%d steps" % (len(req["steps"]) - 15))
        hist = {(r["sid"][:8], r["cw"]): r for r in idx.records(exclude_sid=e["sid"], before=e["ts0"])}
        for m in h["matches"][:5]:
            r = hist.get((m["sid"], m["cw"]))
            out.append("  matched prior: session %s  project %s  %s CW  score %.2f"
                       % (m["sid"], r["project"] if r else "?", xi._fk(m["cw"]), m["score"]))
            for x in (r["steps"] if r else [])[:10]:
                out.append("      [%s] %s · %s" % (x["cat"].upper(), x["label"], xi._fk(x["cw"])))
            if r and len(r["steps"]) > 10:
                out.append("      … +%d steps" % (len(r["steps"]) - 10))
    with open(a.out, "w", encoding="utf-8") as f:
        f.write("\n".join(out) + "\n")
    print("sample written to %s" % a.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
