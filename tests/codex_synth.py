"""Synthetic OpenAI Codex rollouts for the Codex tests (tests/test_codex_live.py, tests/test_harness_codex.py).

RolloutWriter writes `<root>/YYYY/MM/DD/rollout-<timestamp>-<uuid>.jsonl` files in the modern (ordinal +
token_usage_record) or legacy (token_count only) style. Every value is invented: cwd /home/user/project, fake ids.
"""
import json
import os
import time

CWD = "/home/user/project"
MODEL = "gpt-6.1-sol"
DAY0 = 1792486800.0   # 2026-10-20T09:00:00Z
SMALL = (10000, 8000, 500)       # input (incl. cached), cached, output
BIG = (900000, 100000, 60000)


def sid(n):
    return "019a0000-0000-7000-8000-%012d" % n


class RolloutWriter:
    """One Codex rollout. turn() writes one model response: [reasoning] [text] calls, usage, call outputs."""

    def __init__(self, session_id, t0=DAY0, cwd=CWD, model=MODEL, legacy=False, parent=None):
        self.sid, self.t, self.cwd, self.model, self.legacy = session_id, t0, cwd, model, legacy
        self.lines, self.n, self.t0 = [], 0, t0
        self.totals = [0, 0, 0]
        meta = {"id": session_id, "timestamp": self._ts(), "cwd": cwd, "originator": "codex_exec",
                "cli_version": "0.120.0" if legacy else "0.160.0", "source": "exec"}
        if parent:
            meta.update(parent_thread_id=parent, forked_from_id=parent, subagent_history_start_ordinal=1,
                        source={"subagent": {"thread_spawn": {"parent_thread_id": parent, "depth": 1}}})
        self._rec("session_meta", meta)
        self._rec("turn_context", {"cwd": cwd, "model": model})

    def _ts(self):
        return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(self.t)) + ".000Z"

    def _rec(self, typ, payload):
        d = {"timestamp": self._ts(), "type": typ, "payload": payload}
        if not self.legacy:
            d["ordinal"] = len(self.lines)
        self.lines.append(d)

    def _item(self, payload):
        self._rec("response_item", payload)

    def prompt(self, text):
        self.t += 5
        self._item({"type": "message", "role": "user", "content": [{"type": "input_text", "text": text}]})

    def turn(self, calls=(), usage=SMALL, text=None, reasoning=None, dt=6):
        """calls: [(item payload without call_id, output payload or None)] from the call builders below."""
        self.t += dt
        if reasoning is not None:
            self._item({"type": "reasoning", "summary": [{"type": "summary_text", "text": reasoning}],
                        "encrypted_content": "gAAAAFAKE"})
        if text:
            self._item({"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": text}]})
        ids = []
        for item, _ in calls:
            self.n += 1
            ids.append("call_%d" % self.n)
            item = dict(item)
            if item["type"] == "web_search_call":
                item["id"] = "ws_%d" % self.n
            else:
                item["call_id"] = ids[-1]
            self._item(item)
        self.usage(usage)
        for cid, (item, out) in zip(ids, calls):
            if out is not None:
                self.t += 2
                kind = "custom_tool_call_output" if item["type"] == "custom_tool_call" else "function_call_output"
                self._item({"type": kind, "call_id": cid, "output": out})

    def usage(self, usage):
        inp, cached, out = usage
        u = {"input_tokens": inp, "cached_input_tokens": cached, "output_tokens": out, "reasoning_output_tokens": 0,
             "total_tokens": inp + out}
        self.totals = [a + b for a, b in zip(self.totals, usage)]
        total = dict(zip(("input_tokens", "cached_input_tokens", "output_tokens"), self.totals))
        if not self.legacy:
            self._rec("token_usage_record", {"response_id": "resp_%d" % len(self.lines), "usage": u})
        self._rec("event_msg", {"type": "token_count", "info": {"total_token_usage": total, "last_token_usage": u}})

    def end(self, text="Done."):
        self.turn(text=text)

    def path(self, root):
        day = time.strftime("%Y/%m/%d", time.gmtime(self.t0))
        stamp = time.strftime("%Y-%m-%dT%H-%M-%S", time.gmtime(self.t0))
        return os.path.join(root, day, "rollout-%s-%s.jsonl" % (stamp, self.sid))

    def text(self):
        return "".join(json.dumps(x, separators=(",", ":")) + "\n" for x in self.lines)

    def write(self, root):
        p = self.path(root)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            f.write(self.text())
        return p


# ---------------------------------------------------------------- call builders: (item, output)

def shell(cmd, out="", code=0):
    """Legacy `shell` function call (argv form) with a JSON output carrying the exit code."""
    args = {"command": ["bash", "-lc", cmd], "workdir": CWD}
    return ({"type": "function_call", "name": "shell", "arguments": json.dumps(args)},
            json.dumps({"output": out, "metadata": {"exit_code": code, "duration_seconds": 0.2}}))


def exec_command(cmd, out=""):
    return ({"type": "function_call", "name": "exec_command", "arguments": json.dumps({"cmd": cmd})}, out)


def read(rel, body="line 1\nline 2\n"):
    return shell("sed -n '1,200p' %s" % rel, body)


def patch(files, out=None):
    """apply_patch custom tool call; files = [("Add"|"Update"|"Delete", path)]."""
    body = "*** Begin Patch\n" + "".join("*** %s File: %s\n@@\n-old body line\n+new body line\n" % f for f in files) + \
        "*** End Patch\n"
    return ({"type": "custom_tool_call", "name": "apply_patch", "input": body},
            out or "Success. Updated the following files:\n" + "".join("M %s\n" % p for _, p in files))


def plan(steps, explanation="", status="in_progress"):
    args = {"explanation": explanation, "plan": [{"step": s, "status": status} for s in steps]}
    return ({"type": "function_call", "name": "update_plan", "arguments": json.dumps(args)}, "Plan updated")


def mcp(name, args, out="ok"):
    return ({"type": "function_call", "name": name, "arguments": json.dumps(args)}, out)


def spawn(prompt, task_name, out=None):
    return ({"type": "function_call", "name": "spawn_agent",
             "arguments": json.dumps({"prompt": prompt, "task_name": task_name})}, out or '{"agent_id": "x"}')


def web_search(query):
    return ({"type": "web_search_call", "status": "completed", "action": {"type": "search", "query": query}}, None)
