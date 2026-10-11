"""Understand layer: request split, steps, test parsing, chains, flow, outcomes, per-goal aggregates, CLI."""
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import synth  # noqa: E402
from tokenscope import cli  # noqa: E402
from tokenscope import understand as un  # noqa: E402
from tokenscope.parse import find_session  # noqa: E402

SID = "dddddddd-0000-0000-0000-000000000004"
LATER = datetime(2026, 10, 3, tzinfo=timezone.utc)  # well after the fixture: nothing is "in progress"
NODE_FAIL = "✔ a\n✖ b\nℹ tests 14\nℹ suites 0\nℹ pass 13\nℹ fail 1\nℹ cancelled 0\n"
NODE_PASS = "✔ a\nℹ tests 14\nℹ pass 14\nℹ fail 0\n"


def call(w, sec, mid, usage, tool=None, text="ok", stop="tool_use"):
    w.call(sec, mid, usage, tool, text)
    w.lines[-1]["message"]["stop_reason"] = stop


def make(root):
    """Session with 3 requests.

    r1 "Fix the failing checkout test": Bash(./run_tests.sh) (cat && ./run_tests.sh) -> Edit (+hook: 13 fail) -> Edit (+hook: 14 pass) -> final text
    r2 "Explain how the cart works": Agent(Explore) (subagent with 2 calls) -> Grep -> final text
    r3 "Refactor pricing": Read -> ends mid-tool (abandoned)
    Noise lines (command, system reminder, compact summary, interrupt) must not start requests.
    """
    w = synth._W(SID)
    cwd = synth.CWD
    w.prompt(0, "<command-name>/clear</command-name>")
    w.prompt(0, "Fix the failing checkout test")
    w.prompt(0, "<system-reminder>be nice</system-reminder>")
    call(w, 1, "m0", (5, 20000, 0, 50), ("Bash", {"command": "cat src/cart.js && ./run_tests.sh"}))
    w.result(2, "m0", "x" * 400 + "\n" + NODE_FAIL)
    call(w, 3, "m1", (0, 200, 20005, 50), ("Edit", {"file_path": cwd + "/src/pricing.js", "old_string": "a"}))
    w.result(4, "m1", "The file has been updated successfully.")
    w.att(4, {"type": "hook_success", "hookName": "PostToolUse:Edit", "toolUseID": "tu_m1",
              "hookEvent": "PostToolUse", "content": "", "stdout": "{}", "exitCode": "0", "command": "run.sh"})
    w.att(4, {"type": "hook_additional_context", "hookName": "PostToolUse:Edit", "toolUseID": "tu_m1",
              "hookEvent": "PostToolUse", "content": ["Test suite output after your edit:\n" + NODE_FAIL]})
    call(w, 5, "m2", (0, 300, 20205, 50), ("Edit", {"file_path": cwd + "/src/pricing.js", "old_string": "b"}))
    w.result(6, "m2", "The file has been updated successfully.")
    w.att(6, {"type": "hook_additional_context", "hookName": "PostToolUse:Edit", "toolUseID": "tu_m2",
              "hookEvent": "PostToolUse", "content": ["Test suite output after your edit:\n" + NODE_PASS]})
    call(w, 7, "m3", (0, 300, 20505, 80), text="Fixed: all 14 tests pass.", stop="end_turn")

    w.prompt(20, "Explain how the cart works")
    call(w, 21, "m4", (0, 100, 20805, 50), ("Agent", {"subagent_type": "Explore", "description": "look"}))
    w.result(22, "m4", "the cart adds items")
    call(w, 23, "m5", (0, 100, 20905, 50), ("Grep", {"pattern": "checkout"}))
    w.result(24, "m5", "src/cart.js:1: checkout")
    w.ev(24, type="user", isCompactSummary=True, message={"role": "user", "content": "summary of before"})
    call(w, 25, "m6", (0, 100, 21005, 300), text="The cart works like this.", stop="end_turn")

    w.prompt(40, "Refactor pricing into a module")
    call(w, 41, "m7", (0, 100, 21405, 50), ("Read", {"file_path": cwd + "/src/pricing.js"}))
    w.ev(42, type="user", message={"role": "user", "content": [
        {"type": "text", "text": "[Request interrupted by user for tool use]"}]})
    w.write(root)

    sub = os.path.join(root, "-work-shop", SID, "subagents")
    os.makedirs(sub, exist_ok=True)
    s = synth._W(SID)
    s.prompt(21, "look")
    call(s, 21, "s0", (5, 3000, 0, 20), ("Read", {"file_path": cwd + "/src/cart.js"}))
    s.result(21, "s0", "c" * 400)
    call(s, 22, "s1", (0, 100, 3005, 40), text="done", stop="end_turn")
    with open(os.path.join(sub, "agent-a1.jsonl"), "w") as f:
        for x in s.lines:
            f.write(json.dumps(dict(x, agentId="a1", isSidechain=True)) + "\n")
    with open(os.path.join(sub, "agent-a1.meta.json"), "w") as f:
        json.dump({"agentType": "Explore", "toolUseId": "tu_m4", "description": "look"}, f)
    return root


class Fixture(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        make(cls.tmp.name)
        cls.session = find_session(SID[:8], cls.tmp.name)
        cls.u = un.understand_session(cls.session, now=LATER)
        cls.reqs = cls.u["requests"]

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()


class TestRequestSplit(Fixture):
    def test_three_requests_noise_ignored(self):
        self.assertEqual([r["id"] for r in self.reqs], ["dddddddd:1", "dddddddd:2", "dddddddd:3"])
        self.assertEqual([r["prompt"] for r in self.reqs],
                         ["Fix the failing checkout test", "Explain how the cart works",
                          "Refactor pricing into a module"])

    def test_calls_and_tokens(self):
        r1, r2, r3 = self.reqs
        self.assertEqual(r1["main_calls"], 4)
        self.assertEqual((r2["main_calls"], r2["subagent_calls"], r2["calls"]), (3, 2, 5))
        self.assertEqual(r3["main_calls"], 1)
        # r1: fresh 5 + writes 20000+200+300+300 + reads 20005+20205+20505 + output 50*3+80
        self.assertEqual(r1["tokens"]["new"], 5 + 20800 + 230)
        self.assertEqual(r1["tokens"]["cache_read"], 20005 + 20205 + 20505)
        self.assertEqual(r1["tokens"]["all_in"], r1["tokens"]["new"] + r1["tokens"]["cache_read"])
        self.assertIn(3005, [r2["tokens"]["cache_read"] - (20805 + 20905 + 21005)])  # subagent read included
        total = sum(r["tokens"]["all_in"] for r in self.reqs)
        self.assertEqual(total, self.u["totals"]["all_in"])
        self.assertAlmostEqual(sum(r["share_of_session"] for r in self.reqs), 1.0, places=3)


class TestSteps(Fixture):
    def test_steps_and_hooks(self):
        s = self.reqs[0]["steps"]
        self.assertEqual([x["kind"] for x in s], ["Bash(./run_tests.sh)", "Edit", "Hook(PostToolUse:Edit)", "Edit",
                                                  "Hook(PostToolUse:Edit)"])
        self.assertEqual(s[0]["label"], "Bash(./run_tests.sh) 1 failed")
        self.assertEqual(s[1]["label"], "Edit src/pricing.js")
        self.assertEqual(s[2]["label"], "Hook PostToolUse:Edit → 1 failed")
        self.assertFalse(s[2]["ok"])
        self.assertEqual(s[4]["label"], "Hook PostToolUse:Edit → 14 passed")
        self.assertTrue(s[4]["ok"])
        self.assertGreater(s[2]["tokens"], 0)  # hook output attributed by the analyzer
        self.assertGreater(s[0]["tokens"], 0)
        r2 = self.reqs[1]["steps"]
        self.assertEqual([x["kind"] for x in r2], ["Agent(Explore)", "Grep"])
        self.assertEqual(r2[1]["label"], 'Grep "checkout"')
        self.assertIn("2 calls", r2[0]["detail"])

    def test_step_tokens_reuse_analyzer(self):
        from tokenscope.analyze import analyze_session
        a = analyze_session(self.session)
        attributed = {r["tool_use_id"]: r["attributed_tokens"] for c in a["calls"] for r in c["tool_results"]}
        for st in self.reqs[0]["steps"]:
            if not un.is_hook(st):
                self.assertEqual(st["tokens"], attributed[st["tool_use_id"]])

    def test_normalize_command(self):
        cases = {
            "./run_tests.sh": "./run_tests.sh", "bash run_tests.sh": "./run_tests.sh",
            "git status --short": "git status", "git -C /x log -3": "git log", "grep -rn foo src": "grep",
            "cat a b | head": "cat", "cd /w && npm test 2>&1 | tail -40": "npm test",
            "npm run build": "npm run build", "FOO=1 timeout 60 pytest -q tests/": "pytest",
            "python3 -m pytest -x": "python3 -m pytest", "python scripts/gen.py --x": "python scripts/gen.py",
            "node -e \"require('./src/cart')\"": "node -e", "for f in a b; do cat $f; done": "cat",
            "go test ./...": "go test", "cargo build --release": "cargo build", "make test": "make test",
            "yarn": "yarn", "/usr/bin/ls -la": "ls",
        }
        for cmd, want in cases.items():
            self.assertEqual(un.normalize_command(cmd), want, cmd)

    def test_step_kinds(self):
        self.assertEqual(un.step_kind("Task", {"subagent_type": "Plan"})[0], "Agent(Plan)")
        self.assertEqual(un.step_kind("Skill", {"skill": "dataviz"})[0], "Skill(dataviz)")
        self.assertEqual(un.step_kind("mcp__tracker__get_issue", {})[0], "MCP(tracker.get_issue)")
        self.assertEqual(un.step_kind("TodoWrite", {})[0], "Todo")
        self.assertEqual(un.step_kind("WebFetch", {})[0], "WebFetch")


class TestParseTests(unittest.TestCase):
    def test_node(self):
        self.assertEqual(un.parse_test_output(NODE_FAIL), {"passed": 13, "failed": 1, "framework": "node:test"})
        tap = "# tests 3\n# pass 3\n# fail 0\n"
        self.assertEqual(un.parse_test_output(tap)["passed"], 3)

    def test_pytest(self):
        t = "....F\n=========== 1 failed, 4 passed, 1 error in 0.12s ===========\n"
        self.assertEqual(un.parse_test_output(t), {"passed": 4, "failed": 2, "framework": "pytest"})
        self.assertEqual(un.parse_test_output("12 passed in 1.03s\n")["passed"], 12)

    def test_jest(self):
        t = "Test Suites: 1 failed, 2 passed, 3 total\nTests:       2 failed, 30 passed, 32 total\n"
        self.assertEqual(un.parse_test_output(t), {"passed": 30, "failed": 2, "framework": "jest"})
        t = " Test Files  1 passed (1)\n      Tests  5 passed (5)\n"
        self.assertEqual(un.parse_test_output(t), {"passed": 5, "failed": 0, "framework": "jest"})

    def test_go(self):
        t = "=== RUN TestA\n--- PASS: TestA (0.00s)\n=== RUN TestB\n--- FAIL: TestB (0.00s)\nFAIL\nFAIL\tex/m\t0.01s\n"
        self.assertEqual(un.parse_test_output(t), {"passed": 1, "failed": 1, "framework": "go test"})
        t = "ok  \tex/a\t0.01s\nok  \tex/b\t(cached)\n"
        self.assertEqual(un.parse_test_output(t), {"passed": 2, "failed": 0, "framework": "go test"})

    def test_mocha_and_none(self):
        self.assertEqual(un.parse_test_output("  10 passing (20ms)\n  2 failing\n"),
                         {"passed": 10, "failed": 2, "framework": "mocha"})
        self.assertIsNone(un.parse_test_output("hello world\n3 passed the bar"))
        self.assertIsNone(un.parse_test_output(""))

    def test_last_summary_wins(self):
        both = NODE_FAIL + "\n...\n" + "Tests: 9 passed, 9 total\n"
        self.assertEqual(un.parse_test_output(both)["framework"], "jest")


def S(kind, target="", tool=None, tests=None, ok=True, tu=None, fp=None):
    st = {"kind": kind, "target": target, "tool": tool or kind.split("(")[0], "tests": tests, "ok": ok,
          "tool_use_id": tu, "est": True, "tokens": 0, "fp": fp}
    if kind.startswith("Hook("):
        st["tool"], st["hook_name"] = "hook", kind[5:-1]
    st["label"] = un.step_label(st)
    return st


class TestChains(unittest.TestCase):
    def test_collapse_units(self):
        steps = [S("Bash(cat)", tu="1"), S("Read", "a", tu="2"), S("Read", "b", tu="3")]
        for i in range(3):
            steps += [S("Edit", "x%d" % i, tu="e%d" % i), S("Hook(PostToolUse:Edit)", tu="e%d" % i)]
        steps.append(S("Bash(./run_tests.sh)", tu="9"))
        self.assertEqual(un.chain_of(steps), "Bash(cat) → Read×2 → Edit×3 → Hook(PostToolUse:Edit)×3 → "
                                             "Bash(./run_tests.sh)")
        self.assertEqual(un.chain_signature(steps), "Edit + Read")

    def test_no_tools_and_signature_ties(self):
        self.assertEqual(un.chain_of([]), "(no tools)")
        steps = [S("Edit", tu="1"), S("Bash(./run_tests.sh)", tu="2"), S("Edit", tu="3"), S("Bash(./run_tests.sh)")]
        self.assertEqual(un.chain_signature(steps), "Edit + Bash(./run_tests.sh)")

    def test_flow(self):
        seqs = [["Read", "Edit", "Bash(npm test)"], ["Read", "Edit"], ["Grep", "Read", "Edit"],
                ["Read", "Bash(npm test)"]]
        f = un.flow(seqs)
        nodes = {(n["position"], n["kind"]): n for n in f["nodes"]}
        self.assertEqual(nodes[(1, "Read")]["count"], 3)
        edges = {(e["from_pos"], e["from_kind"], e["to_kind"]): e for e in f["edges"]}
        self.assertEqual(edges[(1, "Read", "Edit")]["count"], 2)
        self.assertAlmostEqual(edges[(1, "Read", "Edit")]["pct"], 66.7)
        self.assertAlmostEqual(edges[(1, "Read", "Bash(npm test)")]["pct"], 33.3)
        self.assertEqual(nodes[(2, "Edit")]["top_predecessor"]["text"], "100% after Read")
        self.assertEqual(nodes[(3, "Edit")]["top_predecessor"]["kind"], "Read")

    def test_flow_sequence_dedup_and_hooks(self):
        steps = [S("Read", "a"), S("Read", "b"), S("Edit", tu="e"), S("Hook(X)", tu="e"), S("Edit")] + \
                [S("Bash(k%d)" % i) for i in range(8)]
        self.assertEqual(un.flow_sequence(steps), ["Read", "Edit", "Bash(k0)", "Bash(k1)", "Bash(k2)", "Bash(k3)"])


class TestOutcomes(Fixture):
    def test_fixture_outcomes(self):
        r1, r2, r3 = self.reqs
        self.assertEqual(r1["outcome"], "success")
        self.assertIn("node:test", r1["signals"][0])
        self.assertNotIn("read_only", r1["flags"])
        self.assertEqual(r2["outcome"], "success")
        self.assertIn("read_only", r2["flags"])
        self.assertEqual(r3["outcome"], "abandoned")
        self.assertIn("interrupted by user", r3["signals"])

    def _out(self, steps, text="done", stop="end_turn", terminal=()):
        info = {"text": text, "stop_reason": stop, "tool_uses": 0}
        return un.outcome_of({"steps": steps}, info, list(terminal))

    def test_failed_and_success_rules(self):
        fail = {"passed": 13, "failed": 1, "framework": "node:test"}
        ok = {"passed": 14, "failed": 0, "framework": "node:test"}
        self.assertEqual(self._out([S("Bash(npm test)", tests=fail, ok=False)])[0], "failed")
        self.assertEqual(self._out([S("Bash(npm test)", tests=fail, ok=False), S("Edit"),
                                    S("Bash(npm test)", tests=ok)])[0], "success")
        self.assertEqual(self._out([S("Read", ok=False)])[0], "failed")
        self.assertEqual(self._out([S("Read")])[0], "success")
        self.assertEqual(self._out([S("Read")], terminal=["result subtype error_during_execution"])[0], "failed")

    def test_abandoned(self):
        self.assertEqual(self._out([S("Read", ok=None)], text="", stop="tool_use")[0], "abandoned")
        self.assertEqual(self._out([S("Read")], terminal=["max turns reached"])[0], "abandoned")
        self.assertEqual(un.outcome_of({"steps": []}, None, [])[0], "abandoned")

    def test_looping(self):
        same = [S("Bash(npm test)", "npm test", fp="a") for _ in range(3)]
        self.assertIn("looping", self._out(same)[2])
        edits = [S("Edit", "a.js", fp=str(i)) for i in range(5)]  # different edits of one file: progress
        self.assertNotIn("looping", self._out(edits)[2])
        fail = {"passed": 1, "failed": 1, "framework": "pytest"}
        cyc = [S("Bash(pytest)", tests=fail, ok=False)]
        for _ in range(3):
            cyc += [S("Edit", "a.py"), S("Bash(pytest)", tests=fail, ok=False)]
        o, sig, flags = self._out(cyc)
        self.assertEqual(o, "failed")
        self.assertIn("looping", flags)
        self.assertTrue(any("3 fail → edit → fail" in s for s in sig))


class TestAggregateAndCli(Fixture):
    def test_aggregate(self):
        agg = un.aggregate([self.u])
        self.assertEqual(len(agg["goals"]), 3)  # three unrelated prompts -> three goals
        by = {g["request_ids"][0]: g for g in agg["goals"]}
        self.assertEqual(by["dddddddd:3"]["status"], "Failing")  # abandoned => 0% finish
        self.assertEqual(by["dddddddd:1"]["finish_rate"], 1.0)
        self.assertEqual(agg["unhandled_requests"], [])
        self.assertEqual(by["dddddddd:1"]["chains"][0]["pct"], 100.0)
        self.assertIn("pricing.js", by["dddddddd:1"]["goal"])  # fallback name: terms · top edited file
        self.assertEqual(self.reqs[0]["goal"]["id"], by["dddddddd:1"]["id"])
        self.assertEqual(self.reqs[0]["files"], {"edited": ["src/pricing.js"], "read": ["src/cart.js"]})

    def test_status_rules(self):
        base = {"finish_rate": 1.0, "runs": 3, "flags": {}, "all_in": {"median": 100}}
        self.assertEqual(un.goal_status(base, 100)[0], "Healthy")
        self.assertEqual(un.goal_status(dict(base, finish_rate=0.6), 100)[0], "Failing")
        self.assertEqual(un.goal_status(dict(base, flags={"looping": 1}), 100)[0], "Looping")
        self.assertEqual(un.goal_status(dict(base, all_in={"median": 151}), 100)[0], "Costly")
        self.assertEqual(un.goal_status(base, 100, fix_share=0.31)[0], "Costly")

    def test_context_series(self):
        main = self.u["context"]["main"]
        self.assertEqual(len(main), 8)
        for c in main:
            self.assertAlmostEqual(sum(c["sources"].values()), c["context"], delta=len(un.SOURCES))
            self.assertEqual(c["cached"] + c["uncached"], c["context"])
        self.assertGreater(main[3]["sources"]["hook_output"], 0)
        self.assertGreater(main[5]["sources"]["tool_results"], 0)
        self.assertEqual(main[6]["sources"]["hook_output"], 0)  # compaction summary: history replaced
        self.assertEqual(main[6]["sources"]["tool_definitions"], main[5]["sources"]["tool_definitions"])
        self.assertEqual(len(self.u["context"]["subagents"]), 1)
        agg = un.context_aggregate([self.u])
        self.assertEqual(agg["by_day"][0]["day"], "2026-10-02")

    def test_cli(self):
        for cmd in ("goals", "understand"):  # understand = alias
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = cli.main([cmd, "--root", self.tmp.name])
            self.assertEqual(rc, 0)
            out = buf.getvalue()
            self.assertIn("Goals: 3 requests in 1 sessions -> 3 goals in", out)
            self.assertIn("named from terms", out)
            self.assertIn("ran Bash(./run_tests.sh) 1 failed [", out)
            self.assertIn("Hook(PostToolUse:Edit) 14 passed", out)
            self.assertIn('"Fix the failing checkout test"', out)  # members verbatim
            self.assertIn("files edited: src/pricing.js ×1", out)
            self.assertIn("nearest other goal:", out)
        buf = io.StringIO()
        with redirect_stdout(buf):
            cli.main(["goals", "--root", self.tmp.name, "--session", SID[:8], "--json", "--goal-threshold", "0"])
        d = json.loads(buf.getvalue())
        self.assertEqual(d["requests"], 3)
        self.assertEqual(len(d["goals"]), 1)  # threshold 0: everything merges
        self.assertEqual(d["goal_method"]["goal_threshold"], 0.0)
        self.assertEqual(d["namer"]["used"], "terms")


if __name__ == "__main__":
    unittest.main()
