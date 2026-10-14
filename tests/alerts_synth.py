"""Synthetic Claude Code transcripts that trigger each alert kind (tokenscope/alerts.py), one session each, plus a
clean control session and a showcase session mixing several (demo screenshots).

    python3 tests/alerts_synth.py OUT_DIR [--now]     # default OUT_DIR: tests/fixtures/alerts

Writes OUT_DIR/-demo-<name>/<session id>.jsonl. Timestamps start on 2026-10-02 09:00 UTC, or (--now) a few minutes
ago, so `tokenscope live --root OUT_DIR` shows them as recent sessions. Fake paths under /work/demo only.

    session                                expected alerts (default config; off_task needs --off-task, and for the
                                           showcase, which has no todo list, --alert off_task.use_prompt=true)
    -demo-cost-jump        ...0001         cost/jump at step 9
    -demo-cost-threshold   ...0002         cost/session_usd, cost/intent_usd
    -demo-off-task         ...0003         off_task/unrelated
    -demo-read-loop        ...0004         looping/repeat_read
    -demo-retry-loop       ...0005         looping/retry
    -demo-edit-revert      ...0006         looping/edit_revert
    -demo-clean            ...0007         none (fail -> edit -> fail -> edit -> pass)
    -demo-showcase         ...0008         cost/jump, looping/repeat_read, looping/retry, looping/edit_revert,
                                           off_task/unrelated (no todo list: every step is its own canvas node)
"""
import json
import os
import sys
import time

MODEL = "claude-opus-5"
ROOT = "/work/demo"
DAY0 = 1790931600.0   # 2026-10-02T09:00:00Z

SESSIONS = {
    "cost-jump": "a1e70001-0000-4000-8000-000000000001",
    "cost-threshold": "a1e70002-0000-4000-8000-000000000002",
    "off-task": "a1e70003-0000-4000-8000-000000000003",
    "read-loop": "a1e70004-0000-4000-8000-000000000004",
    "retry-loop": "a1e70005-0000-4000-8000-000000000005",
    "edit-revert": "a1e70006-0000-4000-8000-000000000006",
    "clean": "a1e70007-0000-4000-8000-000000000007",
    "showcase": "a1e70008-0000-4000-8000-000000000008",
}

SMALL = (2, 1500, 28000, 350)     # input, cache write, cache read, output: ~$0.03 on claude-opus-5
BIG = (2, 52000, 98000, 9000)     # ~$0.60


class Writer:
    def __init__(self, sid, t0):
        self.sid, self.t, self.lines, self.n = sid, t0, [], 0

    def _ev(self, **kw):
        self.n += 1
        d = {"uuid": "u%04d" % self.n, "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(self.t)) +
             ".000Z", "cwd": ROOT, "sessionId": self.sid, "isSidechain": False, "gitBranch": "main"}
        d.update(kw)
        self.lines.append(d)

    def prompt(self, text):
        self.t += 5
        self._ev(type="user", message={"role": "user", "content": text})

    def turn(self, calls, usage=SMALL, text=None, dt=6):
        """One assistant message calling `calls` [(name, input, result text, is_error)], then their results."""
        self.t += dt
        mid = "msg_%04d" % (self.n + 1)
        blocks = [{"type": "text", "text": text}] if text else []
        ids = []
        for k, (name, inp, _, _) in enumerate(calls):
            ids.append("toolu_%s_%d" % (mid, k))
            blocks.append({"type": "tool_use", "id": ids[-1], "name": name, "input": inp})
        u = dict(zip(("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens", "output_tokens"),
                     usage))
        u["cache_creation"] = {"ephemeral_5m_input_tokens": usage[1], "ephemeral_1h_input_tokens": 0}
        self._ev(type="assistant", message={"id": mid, "model": MODEL, "role": "assistant", "content": blocks,
                                            "stop_reason": "tool_use", "usage": u})
        for tid, (name, inp, out, err) in zip(ids, calls):
            self.t += 2
            extra = {"stdout": out, "stderr": "", "interrupted": False} if name == "Bash" else {}
            self._ev(type="user", message={"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": tid, "content": out, "is_error": err}]}, toolUseResult=extra)

    def end(self, text="Done."):
        self.t += 4
        u = {"input_tokens": 2, "cache_creation_input_tokens": 800, "cache_read_input_tokens": 30000,
             "output_tokens": 200}
        self._ev(type="assistant", message={"id": "msg_end_%d" % self.n, "model": MODEL, "role": "assistant",
                                            "content": [{"type": "text", "text": text}], "stop_reason": "end_turn",
                                            "usage": u})

    def write(self, root, slug):
        d = os.path.join(root, slug)
        os.makedirs(d, exist_ok=True)
        path = os.path.join(d, self.sid + ".jsonl")
        with open(path, "w", encoding="utf-8") as f:
            f.writelines(json.dumps(x) + "\n" for x in self.lines)
        return path


def P(rel):
    return ROOT + "/" + rel


def read(rel, body=None):
    return ("Read", {"file_path": P(rel)}, body or "     1\t# %s\n     2\tdef f():\n     3\t    return 1\n" % rel, False)


def edit(rel, old, new):
    return ("Edit", {"file_path": P(rel), "old_string": old, "new_string": new},
            "The file %s has been updated successfully." % P(rel), False)


def bash(cmd, out, err=False):
    return ("Bash", {"command": cmd, "description": "run"}, out, err)


def grep(pattern, out="src/a.py:3: match"):
    return ("Grep", {"pattern": pattern, "path": P("src")}, out, False)


def todo(content, status="in_progress"):
    return ("TodoWrite", {"todos": [{"content": content, "status": status, "activeForm": content}]},
            "Todos have been modified successfully.", False)


TESTS_OK = "Ran 42 tests in 0.8s\n\nOK"
TESTS_FAIL = "FAIL: test_total (tests.test_x.T)\nAssertionError: 3 != 4\nRan 42 tests in 0.8s\n\nFAILED (failures=1)"
BUILD_FAIL = "error: Module not found: Can't resolve './config/staging' in '/work/demo/web'\nBuild failed."


def normal_work(w, n, area="report"):
    """n cheap alternating investigate / change / verify steps."""
    for k in range(n):
        m = k % 3
        if m == 0:
            w.turn([read("src/%s/part%d.py" % (area, k))], SMALL)
        elif m == 1:
            w.turn([edit("src/%s/part%d.py" % (area, k - 1), "x = %d" % k, "x = %d" % (k + 1))], SMALL)
        else:
            w.turn([bash("python3 -m unittest discover -s tests", TESTS_OK)], SMALL)


def cost_jump(t0):
    w = Writer(SESSIONS["cost-jump"], t0)
    w.prompt("add a CSV export to the monthly report page")
    normal_work(w, 9)
    # step 9: one huge step (a big generated file read in full, long answer): ~20x the median step
    w.turn([read("build/report_bundle.js", "x" * 4000)], (2, 160000, 40000, 30000))
    normal_work(w, 3)
    w.end()
    return w, "-demo-cost-jump"


def cost_threshold(t0):
    w = Writer(SESSIONS["cost-threshold"], t0)
    w.prompt("migrate the billing service to the new ledger API")
    f = 0
    for s in range(9):   # 9 steps of 4 expensive turns (~$2.4 each)
        for k in range(4):
            f += 1
            if s % 2 == 0:
                w.turn([read("src/billing/mod%d.py" % f)], BIG)
            else:
                w.turn([edit("src/billing/mod%d.py" % (f - 4), "old_%d" % f, "new_%d" % f)], BIG)
    for k in range(10):   # one intent of 10 expensive turns (~$6): over cost.intent_usd, not a jump (ratio ~2.5)
        w.turn([read("src/ledger/client%d.py" % k)], BIG)
    w.end()
    return w, "-demo-cost-threshold"


def off_task(t0):
    w = Writer(SESSIONS["off-task"], t0)
    w.prompt("fix the tax rounding bug in the invoice totals")
    w.turn([todo("Fix tax rounding in invoice totals")])
    w.turn([read("src/invoice/totals.py"), grep("round")])
    w.turn([edit("src/invoice/tax.py", "round(x, 1)", "round(x, 2)")])
    w.turn([bash("python3 -m unittest discover -s tests", TESTS_OK)])
    # drift: auth sessions, metrics, theme colours; nothing to do with tax or invoices
    w.turn([read("src/auth/session_store.py")])
    w.turn([edit("src/auth/session_store.py", "ttl = 60", "ttl = 120")])
    w.turn([read("src/metrics/exporter.py"), read("web/theme/colors.css")])
    w.turn([edit("web/theme/colors.css", "#333", "#222")])
    w.end()
    return w, "-demo-off-task"


def read_loop(t0):
    w = Writer(SESSIONS["read-loop"], t0)
    w.prompt("why does the scheduler skip jobs when the clock changes for DST?")
    body = "     1\tclass Scheduler:\n     2\t    def next_run(self):\n     3\t        return self.t + 3600\n"
    for k in range(6):   # the same file read 6x with the same content, a different search in between
        w.turn([read("src/sched/scheduler.py", body)])
        w.turn([grep("dst_%d" % k, "no matches")])
    w.end("It adds a fixed 3600 s.")
    return w, "-demo-read-loop"


def retry_loop(t0):
    w = Writer(SESSIONS["retry-loop"], t0)
    w.prompt("deploy the staging build")
    w.turn([read("web/package.json", '{"scripts": {"build": "vite build"}}')])
    for k in range(4):   # the same failing build, nothing changed in between
        w.turn([bash("npm run build -- --mode staging", BUILD_FAIL, err=True)])
    w.end("The build fails: a config module is missing.")
    return w, "-demo-retry-loop"


def edit_revert(t0):
    w = Writer(SESSIONS["edit-revert"], t0)
    w.prompt("speed up the cache lookup")
    w.turn([read("src/cache/lookup.py")])
    a, b = "for k in keys:\n    if k == key:", "if key in keyset:"
    for k in range(2):   # A -> B (tests fail), B -> A (tests pass), twice
        w.turn([edit("src/cache/lookup.py", a, b)])
        w.turn([bash("python3 -m unittest discover -s tests", TESTS_FAIL, err=True)])
        w.turn([edit("src/cache/lookup.py", b, a)])
        w.turn([bash("python3 -m unittest discover -s tests", TESTS_OK)])
    w.end()
    return w, "-demo-edit-revert"


def clean(t0):
    w = Writer(SESSIONS["clean"], t0)
    w.prompt("make the discount code case-insensitive")
    w.turn([read("src/cart/discount.py")])
    w.turn([edit("src/cart/discount.py", "code ==", "code.upper() ==")])
    w.turn([bash("python3 -m unittest discover -s tests", TESTS_FAIL, err=True)])
    w.turn([edit("src/cart/discount.py", "CODES[c]", "CODES[c.upper()]")])
    w.turn([bash("python3 -m unittest discover -s tests", TESTS_OK)])
    w.end()
    return w, "-demo-clean"


def showcase(t0):
    w = Writer(SESSIONS["showcase"], t0)
    w.prompt("fix the tax rounding bug in the invoice totals")
    w.turn([read("src/invoice/totals.py"), grep("round")])
    w.turn([edit("src/invoice/tax.py", "round(x, 1)", "round(x, 2)")])
    w.turn([bash("python3 -m unittest discover -s tests", TESTS_OK)])
    w.turn([read("src/invoice/lines.py")])
    w.turn([edit("src/invoice/lines.py", "qty * p", "qty * price")])
    w.turn([bash("python3 -m unittest discover -s tests", TESTS_OK)])
    w.turn([read("src/invoice/pdf.py")])
    w.turn([read("vendor/invoice_fonts.js", "x" * 4000)], (2, 150000, 40000, 28000))   # the cost jump
    a, b = "q = Decimal(x)", "q = float(x)"
    for k in range(2):   # edit-revert x2
        w.turn([edit("src/invoice/tax.py", a, b)])
        w.turn([bash("python3 -m unittest discover -s tests", TESTS_FAIL, err=True)])
        w.turn([edit("src/invoice/tax.py", b, a)])
        w.turn([bash("python3 -m unittest discover -s tests", TESTS_OK)])
    body = "     1\tSESSION_TTL = 60\n"
    for k in range(4):   # drifting into unrelated code, re-reading the same file
        w.turn([read("src/auth/session_store.py", body)])
        w.turn([edit("web/theme/colors.css", "#%d%d%d" % (k, k, k), "#%d%d%d" % (k + 1, k + 1, k + 1))])
    for k in range(3):   # the same failing command
        w.turn([bash("npm run lint -- --fix web/theme", "error: Cannot find module 'eslint-plugin-x'", err=True)])
    w.end("Stuck on the linter.")
    return w, "-demo-showcase"


SCENARIOS = [cost_jump, cost_threshold, off_task, read_loop, retry_loop, edit_revert, clean, showcase]


def write_all(root, t0=DAY0):
    """Write every scenario under root; returns {name: main transcript path}."""
    out = {}
    for k, fn in enumerate(SCENARIOS):
        w, slug = fn(t0 + 600 * k)
        out[slug[len("-demo-"):]] = w.write(root, slug)
    return out


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    root = args[0] if args else os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "alerts")
    t0 = time.time() - 600 * len(SCENARIOS) - 1800 if "--now" in sys.argv else DAY0
    for name, path in write_all(root, t0).items():
        print(name, os.path.relpath(path, root))
