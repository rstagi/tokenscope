#!/usr/bin/env python3
"""Offline check of the live warning rules over a transcripts tree.

    python3 tools/eval_warnings.py --root DIR [--limit N] [--show]

Every main transcript under DIR (<project>/<session>.jsonl) and its subagents (<project>/<session>/subagents/
agent-*.jsonl) is ingested at once through the same tailer + LiveState code the live server uses. Prints sessions
scanned, turns, and per rule the sessions where it fired and its firings. --show lists each firing as session id
prefix, rule, count and the normalised error signature / file basename (never prompt text).
"""
import argparse
import glob
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tokenscope.live.state import HINTS, LiveState  # noqa: E402
from tokenscope.live.tail import Tailer, TrackedFile, classify  # noqa: E402


def sessions(root):
    for path in sorted(glob.glob(os.path.join(root, "*", "*.jsonl"))):
        c = classify(root, path)
        if c and c[2] is None:
            yield path, c


def scan(root, path, c, tailer):
    sid, proj, _ = c
    state = LiveState(redact=False)
    files = [TrackedFile(path, sid, proj)]
    for sub in sorted(glob.glob(os.path.join(root, proj, sid, "subagents", "agent-*.jsonl"))):
        sc = classify(root, sub)
        if sc:
            files.append(TrackedFile(sub, sc[0], sc[1], sc[2]))
    s = None
    for tf in files:
        got = tailer._read(tf)
        if got is not None:
            s = state.ingest(tf, got[0], got[1])
    if s is None:
        return None, {}
    fired = {}
    for step in s.main.steps:
        if step["k"] == "flag":
            fired.setdefault(step["flag"], {"firings": 0, "detail": None, "count": 0})["firings"] += 1
    for rule, w in s.warnings.items():
        f = fired.setdefault(rule, {"firings": 1, "detail": None, "count": 0})
        if rule != "context_near_full":
            f["firings"] = w["count"]
        f["count"], f["detail"] = w["count"], (w["detail"] if rule != "context_near_full" else w["text"])
    return s, fired


def main(argv=None):
    p = argparse.ArgumentParser(prog="eval_warnings")
    p.add_argument("--root", required=True)
    p.add_argument("--limit", type=int, default=0)
    p.add_argument("--show", action="store_true")
    a = p.parse_args(argv)
    root = os.path.abspath(os.path.expanduser(a.root))
    tailer = Tailer(root)
    n = turns = with_any = 0
    per = {r: {"sessions": 0, "firings": 0} for r in HINTS}
    shows = []
    for path, c in sessions(root):
        if a.limit and n >= a.limit:
            break
        s, fired = scan(root, path, c, tailer)
        if s is None:
            continue
        n += 1
        turns += s.totals["turns"]
        with_any += bool(fired)
        for rule, f in fired.items():
            per.setdefault(rule, {"sessions": 0, "firings": 0})
            per[rule]["sessions"] += 1
            per[rule]["firings"] += f["firings"]
            shows.append((s.id[:8], rule, f["firings"], f["detail"]))
    print("sessions scanned: %d   turns: %d" % (n, turns))
    print("%-20s %10s %10s" % ("rule", "sessions", "firings"))
    for rule, v in per.items():
        print("%-20s %10d %10d" % (rule, v["sessions"], v["firings"]))
    print("sessions with >= 1 warning: %d/%d (%.1f%%)" % (with_any, n, 100.0 * with_any / n if n else 0.0))
    if a.show:
        for sid, rule, cnt, detail in shows:
            print("%s  %-18s x%-3d %s" % (sid, rule, cnt, detail if detail is not None else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
