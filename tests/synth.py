"""Generate small synthetic transcripts with known token numbers (for fixes/compare tests).

waste session (7 main calls, all 5m cache):
  c0  first call, 25,005 context; reads dist/app.min.js
  c1  +15,100 (min.js result 60,000 chars = 15,000 tok); reads src/a.py
  c2  +600 (a.py 2,000 chars = 500 tok); reads src/a.py again (same range)
  c3  +600 (re-read 500); runs `npm test -- --verbose`
  c4  +3,100 (bash 12,000 chars = 3,000 tok)
  c5  2h later: cache miss, 44,500 re-written
  c6  +100
lean session: 2 calls, 18,005 + 500.
"""
import json
import os

WASTE = "aaaaaaaa-0000-0000-0000-000000000001"
LEAN = "bbbbbbbb-0000-0000-0000-000000000002"
CWD = "/work/shop"


def _ts(sec):
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    return "2026-10-02T%02d:%02d:%02d.000Z" % (9 + h, m, s)


class _W:
    def __init__(self, sid):
        self.sid, self.lines, self.n = sid, [], 0

    def ev(self, sec, **kw):
        self.n += 1
        d = {"uuid": "e%d" % self.n, "timestamp": _ts(sec), "cwd": CWD, "sessionId": self.sid, "gitBranch": "main",
             "isSidechain": False}
        d.update(kw)
        self.lines.append(d)

    def prompt(self, sec, text):
        self.ev(sec, type="user", message={"role": "user", "content": text})

    def att(self, sec, a):
        self.ev(sec, type="attachment", attachment=a)

    def call(self, sec, mid, usage, tool=None, text="ok"):
        u = {"input_tokens": usage[0], "cache_creation_input_tokens": usage[1], "cache_read_input_tokens": usage[2],
             "output_tokens": usage[3], "cache_creation": {"ephemeral_5m_input_tokens": usage[1],
                                                           "ephemeral_1h_input_tokens": 0}}
        content = [{"type": "text", "text": text}]
        if tool:
            content.append({"type": "tool_use", "id": "tu_" + mid, "name": tool[0], "input": tool[1]})
        self.ev(sec, type="assistant", message={"id": mid, "model": "claude-test-1", "content": content, "usage": u})

    def result(self, sec, mid, text, extra=None):
        self.ev(sec, type="user", message={"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "tu_" + mid, "content": text}]}, toolUseResult=extra or {})

    def write(self, root, slug="-work-shop"):
        d = os.path.join(root, slug)
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, self.sid + ".jsonl"), "w", encoding="utf-8") as f:
            for x in self.lines:
                f.write(json.dumps(x) + "\n")


SKILLS = "\n".join("- skill%d: %s" % (i, "x" * 790) for i in range(3))


def listings(w, sec):
    w.att(sec, {"type": "deferred_tools_delta", "addedNames": ["mcp__bigsrv__query", "mcp__bigsrv__write"]})
    w.att(sec, {"type": "mcp_instructions_delta", "addedNames": ["bigsrv"], "addedBlocks": ["y" * 2000]})
    w.att(sec, {"type": "skill_listing", "content": SKILLS, "skillCount": 3})


def make(root):
    w = _W(WASTE)
    w.prompt(0, "Fix the failing checkout tests")
    listings(w, 0)
    w.call(1, "m0", (5, 25000, 0, 50), ("Read", {"file_path": CWD + "/dist/app.min.js"}))
    w.result(2, "m0", "z" * 60000)
    w.call(3, "m1", (0, 15100, 25005, 50), ("Read", {"file_path": CWD + "/src/a.py"}))
    w.result(4, "m1", "a" * 2000)
    w.call(5, "m2", (0, 600, 40105, 50), ("Read", {"file_path": CWD + "/src/a.py"}))
    w.result(6, "m2", "a" * 2000)
    w.call(7, "m3", (0, 600, 40705, 50), ("Bash", {"command": "npm test -- --verbose"}))
    w.result(8, "m3", "t" * 12000)
    w.call(9, "m4", (0, 3100, 41305, 50))
    w.prompt(7209, "continue")
    w.call(7210, "m5", (0, 44500, 0, 50))
    w.call(7212, "m6", (0, 100, 44500, 50))
    w.write(root)

    w = _W(LEAN)
    w.prompt(0, "Fix the failing checkout tests")
    listings(w, 0)
    w.call(100, "n0", (5, 18000, 0, 50), ("Bash", {"command": "npm test 2>&1 | tail -40"}))
    w.result(101, "n0", "ok " * 100)
    w.call(102, "n1", (0, 500, 18005, 50))
    w.write(root)
    return root
