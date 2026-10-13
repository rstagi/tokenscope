"""Claude Code hook script: stdin JSON -> one line appended to the live events file.

Run as `python3 -m tokenscope.live.hook` from the hooks printed by `tokenscope live hooks`. It never blocks and
always exits 0. Only the event name, session id, tool name, subagent id / type and (for Notification) a truncated
message are written; tool inputs/outputs and prompts never are. File: $TOKENSCOPE_LIVE_EVENTS (default
~/.cache/tokenscope-live/events.jsonl).
"""
import json
import os
import sys
import time

EVENTS = ("UserPromptSubmit", "PreToolUse", "PostToolUse", "PostToolUseFailure", "Notification", "SubagentStart",
          "SubagentStop", "Stop", "SessionStart", "SessionEnd")


def events_path():
    return os.environ.get("TOKENSCOPE_LIVE_EVENTS") or os.path.join(
        os.path.expanduser("~"), ".cache", "tokenscope-live", "events.jsonl")


def event_line(payload, now=None):
    """The (privacy-safe) event dict for a hook payload, or None if it is not usable."""
    if not isinstance(payload, dict) or not payload.get("hook_event_name"):
        return None
    ev = {"ts": now if now is not None else time.time(), "event": str(payload["hook_event_name"]),
          "session_id": str(payload.get("session_id") or "")}
    if payload.get("tool_name"):
        ev["tool_name"] = str(payload["tool_name"])[:120]
    if payload.get("agent_id"):   # a subagent's event (SubagentStart / SubagentStop, its tool calls)
        ev["agent_id"] = str(payload["agent_id"])[:80]
        if payload.get("agent_type"):
            ev["agent_type"] = str(payload["agent_type"])[:60]
    if ev["event"] == "Notification":
        if payload.get("notification_type"):
            ev["notification_type"] = str(payload["notification_type"])[:60]
        if payload.get("message"):
            ev["message"] = str(payload["message"])[:200]
    return ev


def main(stdin=None):
    try:
        ev = event_line(json.loads((stdin or sys.stdin).read() or "{}"))
        if ev:
            path = events_path()
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "a", encoding="utf-8") as f:
                f.write(json.dumps(ev) + "\n")
    except Exception:  # noqa: BLE001 - a hook must never fail the session
        pass
    return 0


def settings_snippet(python="python3"):
    """settings.json `hooks` block running this script (async) for every event the live view uses."""
    # the hook is stdlib-only, so point at the file itself: works without installing the package
    cmd = {"type": "command", "command": "%s %s" % (python, os.path.abspath(__file__)), "async": True, "timeout": 5}
    hooks = {}
    for ev in EVENTS:
        entry = {"hooks": [dict(cmd)]}
        if ev in ("PreToolUse", "PostToolUse", "PostToolUseFailure"):
            entry["matcher"] = "*"
        hooks[ev] = [entry]
    return {"hooks": hooks}


if __name__ == "__main__":
    sys.exit(main())
