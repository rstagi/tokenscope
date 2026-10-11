"""Claude Code status line: one line from a running `tokenscope live` server.

settings.json:
    "statusLine": {"type": "command", "command": "python3 -m tokenscope.live.statusline"}

Claude Code pipes a JSON payload (with `session_id`) on stdin after each turn. We ask the local server for the
session's compact view and print e.g. `◆ Fix: checkout tax · verify · 1.2M CW · $3.10 · ⚠ re-read file`. If the
server is not running we print nothing (exit 0), so the status line just stays empty.
"""
import json
import os
import sys
import urllib.request



def fmt_tokens(n):
    n = float(n or 0)
    return "%.1fM" % (n / 1e6) if n >= 1e6 else "%dk" % round(n / 1e3) if n >= 1e3 else "%d" % n


def line(d):
    goal = d.get("goal") or {}
    goal = goal.get("name") if isinstance(goal, dict) else goal
    parts = ["◆ " + (goal or "session")]
    if d.get("current_intent"):
        parts.append(str(d["current_intent"]).replace("_", " "))
    parts.append(fmt_tokens(d.get("cw")) + " CW")
    if isinstance(d.get("usd"), (int, float)):
        parts.append("$%.2f" % d["usd"])
    flags = [str(f.get("flag", "")).replace("_", " ") for f in d.get("flags") or [] if isinstance(f, dict)]
    if flags:
        parts.append("⚠ " + ", ".join(flags[:2]))
    return " · ".join(parts)


def main():
    try:
        payload = json.load(sys.stdin)
        sid = payload.get("session_id")
        base = os.environ.get("TOKENSCOPE_LIVE_URL", "http://127.0.0.1:8765").rstrip("/")
        if not sid:
            return 0
        with urllib.request.urlopen("%s/api/session/%s/compact" % (base, sid), timeout=0.5) as r:
            print(line(json.load(r)))
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
