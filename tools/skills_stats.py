"""Skill invocations per skill name across a root of Claude Code transcripts (both kinds, see tokenscope/skills.py):
the Skill tool, and slash-command skills.

    python3 tools/skills_stats.py [--root DIR] [--json]

Counts main sessions and subagent transcripts (each file is one thread). Prints a table: skill, total, via the
Skill tool, via a slash command, sessions using it.
"""
import argparse
import glob
import json
import os
import sys
from collections import Counter, defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tokenscope.skills import SkillScanner  # noqa: E402


def transcripts(root):
    """[(session id, path)]: main transcripts and their subagent transcripts."""
    out = []
    for p in sorted(glob.glob(os.path.join(root, "*", "*.jsonl"))):
        sid = os.path.basename(p)[:-len(".jsonl")]
        out.append((sid, p))
        sub = os.path.join(os.path.dirname(p), sid, "subagents")
        out += [(sid, q) for q in sorted(glob.glob(os.path.join(sub, "**", "*.jsonl"), recursive=True))]
    return out


def scan_file(path):
    sc = SkillScanner()
    out = []
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                if "Skill" not in line and "command-name" not in line and "Base directory" not in line:
                    continue   # the scanner only reacts to these records; skipping the rest keeps big trees fast
                try:
                    out += sc.feed(json.loads(line))
                except ValueError:
                    continue
    except OSError:
        pass
    return out


def stats(root):
    """{name: {"total", "tool", "slash", "sessions"}} and the number of files read."""
    by = defaultdict(lambda: {"tool": 0, "slash": 0, "sessions": set()})
    files = transcripts(root)
    for sid, path in files:
        for kind, name in scan_file(path):
            by[name][kind] += 1
            by[name]["sessions"].add(sid)
    return {n: {"total": v["tool"] + v["slash"], "tool": v["tool"], "slash": v["slash"],
                "sessions": len(v["sessions"])} for n, v in by.items()}, len(files)


def table(st):
    rows = sorted(st.items(), key=lambda kv: (-kv[1]["total"], kv[0]))
    w = max([len("skill")] + [len(n) for n in st])
    out = ["%-*s  %6s  %5s  %5s  %8s" % (w, "skill", "total", "tool", "slash", "sessions")]
    for n, v in rows:
        out.append("%-*s  %6d  %5d  %5d  %8d" % (w, n, v["total"], v["tool"], v["slash"], v["sessions"]))
    c = Counter()
    for v in st.values():
        c.update(total=v["total"], tool=v["tool"], slash=v["slash"])
    out.append("%-*s  %6d  %5d  %5d" % (w, "TOTAL", c["total"], c["tool"], c["slash"]))
    return "\n".join(out)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--root", default=os.path.expanduser("~/.claude/projects"))
    ap.add_argument("--json", action="store_true", help="print JSON instead of a table")
    a = ap.parse_args(argv)
    st, n = stats(a.root)
    if a.json:
        print(json.dumps(st, indent=1, sort_keys=True))
    else:
        print("%d transcripts, %d skills" % (n, len(st)))
        print(table(st))
    return 0


if __name__ == "__main__":
    sys.exit(main())
