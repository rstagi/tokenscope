#!/usr/bin/env python3
"""declare_intent: a tiny stdio MCP server (docs/intents-spec.md section 3; opt-in experiment).

JSON-RPC 2.0, one message per line: initialize, notifications/initialized, ping, tools/list, tools/call. The one
tool, declare_intent(label, kind?), only appends the call to $TOKENSCOPE_INTENT_LOG (default
~/.cache/tokenscope/declared.jsonl) and answers "noted"; the live state reads the tool_use from the transcript.
Stdlib only and import-free of the package, so `python3 /abs/path/intent_mcp.py` works without installing it.
"""
import json
import os
import sys
import time

PROTOCOL = "2024-11-05"
KINDS = ["investigate", "plan", "change", "verify", "other"]
TOOL = {
    "name": "declare_intent",
    "description": "Declare the sub-task you are starting, as a short label (3-7 words). Call it before starting "
                   "each new sub-task. It only records the label.",
    "inputSchema": {"type": "object", "properties": {
        "label": {"type": "string", "description": "short imperative label, e.g. 'Add retry to the fetch client'"},
        "kind": {"type": "string", "enum": KINDS}}, "required": ["label"]},
}
CLAUDE_MD = """## Intents (tokenscope)
Before starting each new sub-task, call declare_intent with a short label (3-7 words, e.g. "Fix tax rounding in
cart totals") and a kind (investigate / plan / change / verify / other). Do not call it for every tool call.
"""


def log_path():
    return os.environ.get("TOKENSCOPE_INTENT_LOG") or os.path.join(os.path.expanduser("~"), ".cache", "tokenscope",
                                                                   "declared.jsonl")


def record(args):
    try:
        path = log_path()
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps({"ts": time.time(), "label": str(args.get("label") or "")[:200],
                                "kind": args.get("kind") if args.get("kind") in KINDS else None,
                                "cwd": os.getcwd()}) + "\n")
    except OSError:
        pass


def handle(msg):
    """Response dict for one JSON-RPC message, or None for notifications."""
    if not isinstance(msg, dict):
        return {"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "invalid request"}}
    mid, method = msg.get("id"), msg.get("method")
    if mid is None:
        return None   # notifications (notifications/initialized, cancelled, ...)
    params = msg.get("params") if isinstance(msg.get("params"), dict) else {}
    if method == "initialize":
        res = {"protocolVersion": params.get("protocolVersion") or PROTOCOL, "capabilities": {"tools": {}},
               "serverInfo": {"name": "tokenscope-intents", "version": "1"}}
    elif method == "ping":
        res = {}
    elif method == "tools/list":
        res = {"tools": [TOOL]}
    elif method == "tools/call":
        if params.get("name") != "declare_intent":
            return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32602, "message": "unknown tool"}}
        args = params.get("arguments") if isinstance(params.get("arguments"), dict) else {}
        record(args)
        res = {"content": [{"type": "text", "text": "noted"}], "isError": False}
    else:
        return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": "method not found"}}
    return {"jsonrpc": "2.0", "id": mid, "result": res}


def serve(stdin=sys.stdin, stdout=sys.stdout):
    for line in stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            out = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "parse error"}}
        else:
            out = [r for r in map(handle, msg) if r] if isinstance(msg, list) else handle(msg)
        if out:
            stdout.write(json.dumps(out) + "\n")
            stdout.flush()
    return 0


def install_text(name="tokenscope-intents"):
    path = os.path.abspath(__file__)
    return ("# register the declare_intent MCP server (user scope; new sessions only):\n"
            "claude mcp add --scope user %s -- %s %s\n\n"
            "# add to CLAUDE.md (or pass --write-claude-md PATH):\n%s" % (name, sys.executable or "python3", path,
                                                                        CLAUDE_MD))


def write_claude_md(path):
    with open(path, "a", encoding="utf-8") as f:
        f.write("\n" + CLAUDE_MD)


if __name__ == "__main__":
    sys.exit(serve())
