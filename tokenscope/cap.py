#!/usr/bin/env python3
"""tokenscope-cap: run a hook command and cap the output that enters Claude's context.

Usage: tokenscope-cap.py [--max-lines N] -- <command...>

Runs the command through the shell with stdin passed through and stdout captured. If stdout is hook JSON
with hookSpecificOutput.additionalContext, that text is capped and the JSON printed back; otherwise plain
stdout is capped the same way. Capping keeps lines that look like failures (max N) plus the last N lines,
in original order. stderr passes through untouched and the command's exit code is preserved.

Standalone on purpose (stdlib only, no package imports): tokenscope copies this file into a repo as
.claude/hooks/tokenscope-cap.py.
"""
import json
import re
import shlex
import subprocess
import sys

DEFAULT_MAX_LINES = 40
KEEP_RE = re.compile(r"fail|error|✖|not ok|assert", re.I)


def cap_text(text, max_lines=DEFAULT_MAX_LINES):
    """Keep failure-looking lines (at most max_lines) + the last max_lines lines; unchanged if already short."""
    end = "\n" if text.endswith("\n") else ""
    lines = text[:len(text) - len(end)].split("\n")
    if len(lines) <= max_lines:
        return text
    keep = set(range(len(lines) - max_lines, len(lines)))
    hits = [i for i, ln in enumerate(lines) if KEEP_RE.search(ln) and i not in keep][:max_lines]
    keep.update(hits)
    dropped = len(lines) - len(keep)
    if not dropped:
        return text
    note = "[tokenscope-cap: %d of %d lines omitted; kept failure/error lines + last %d]" % (
        dropped, len(lines), max_lines)
    return "\n".join([note] + [lines[i] for i in sorted(keep)]) + end


def cap_output(stdout, max_lines=DEFAULT_MAX_LINES):
    """Cap hook stdout: JSON additionalContext if present, else the plain text."""
    try:
        obj = json.loads(stdout)
    except ValueError:
        obj = None
    hso = obj.get("hookSpecificOutput") if isinstance(obj, dict) else None
    if isinstance(hso, dict) and isinstance(hso.get("additionalContext"), str):
        hso["additionalContext"] = cap_text(hso["additionalContext"], max_lines)
        return json.dumps(obj, ensure_ascii=False) + "\n"
    if obj is not None:
        return stdout  # other hook JSON (decisions etc.): never alter it
    return cap_text(stdout, max_lines)


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    max_lines = DEFAULT_MAX_LINES
    if argv[:1] == ["--max-lines"] and len(argv) > 1:
        max_lines = max(1, int(argv[1]))
        argv = argv[2:]
    if argv[:1] == ["--"]:
        argv = argv[1:]
    if not argv:
        sys.stderr.write("usage: tokenscope-cap.py [--max-lines N] -- <command>\n")
        return 2
    cmd = argv[0] if len(argv) == 1 else shlex.join(argv)
    stdin = sys.stdin.buffer.read() if not sys.stdin.isatty() else b""
    p = subprocess.run(cmd, shell=True, input=stdin, stdout=subprocess.PIPE)
    out = p.stdout.decode("utf-8", errors="replace")
    sys.stdout.write(cap_output(out, max_lines))
    sys.stdout.flush()
    return p.returncode


if __name__ == "__main__":
    sys.exit(main())
