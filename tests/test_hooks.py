"""Hook attribution, instruction-file breakdown, noisy_hook / instructions_bloat rules, overlay emit, cap script."""
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import synth  # noqa: E402
from tokenscope import cap, cli, fixes, report  # noqa: E402
from tokenscope.analyze import analyze_session  # noqa: E402
from tokenscope.parse import find_session  # noqa: E402

SID = "cccccccc-0000-0000-0000-000000000003"
HOOK_CMD = '"$CLAUDE_PROJECT_DIR"/.claude/hooks/run-tests.sh'
HOOK_TEXT = "\n".join("ok %03d %s" % (i, "." * 32) for i in range(300))  # 11,999 chars = 3,000 tokens
CHANGELOG = "## Changelog\n" + "".join("- 0.%d: %s\n" % (i, "c" * 60) for i in range(50))
CLAUDE_MD = ("# proj\n\n## Overview\nSmall repo.\n\n## API\nReference: @docs/api.md\n\n" + CHANGELOG +
             "\n## Release\n1. tag\n")
API_MD = "a" * 9000
SETTINGS = {"model": "x", "hooks": {"PostToolUse": [{"matcher": "Edit", "hooks": [
    {"type": "command", "command": HOOK_CMD}]}]}}


def _read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def make(root, cwd):
    w = synth._W(SID)
    w.prompt(0, "Fix the cart")
    w.att(0, {"type": "instructions", "files": [
        {"path": cwd + "/CLAUDE.md", "type": "Project", "content": CLAUDE_MD},
        {"path": cwd + "/docs/api.md", "type": "Project", "content": API_MD}]})
    w.call(1, "h0", (5, 30000, 0, 50), ("Edit", {"file_path": cwd + "/src/a.js"}))
    w.result(2, "h0", "ok")
    w.att(2, {"type": "hook_success", "hookName": "PostToolUse:Edit", "toolUseID": "tu_h0", "hookEvent": "PostToolUse",
              "content": "", "stdout": json.dumps({"hookSpecificOutput": {"additionalContext": HOOK_TEXT}}),
              "command": HOOK_CMD, "exitCode": 0})
    w.att(2, {"type": "hook_additional_context", "hookName": "PostToolUse:Edit", "toolUseID": "tu_h0",
              "hookEvent": "PostToolUse", "content": [HOOK_TEXT]})
    w.call(3, "h1", (0, 3060, 30005, 50))
    w.call(5, "h2", (0, 100, 33065, 50))
    w.call(7, "h3", (0, 100, 33165, 50))
    for x in w.lines:
        x["cwd"] = cwd
    w.write(root, "-repo")


class HookCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.root = os.path.join(cls.tmp.name, "projects")
        cls.cwd = os.path.join(cls.tmp.name, "repo")
        os.makedirs(os.path.join(cls.cwd, ".claude"))
        cls.settings_path = os.path.join(cls.cwd, ".claude", "settings.json")
        with open(cls.settings_path, "w") as fh:
            json.dump(SETTINGS, fh)
        make(cls.root, cls.cwd)
        cls.a = analyze_session(find_session(SID, cls.root))
        cls.fixes = fixes.session_fixes(cls.a)
        cls.by_rule = {f["rule"]: f for f in cls.fixes}

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()


class TestHookAttribution(HookCase):
    def test_hook_is_own_cause_not_tool_result(self):
        c1 = self.a["calls"][1]
        hook = [x for x in c1["attribution"] if x["cause"] == "hook"]
        self.assertEqual([(x["label"], x["tokens"]) for x in hook], [("hook PostToolUse:Edit", 3000)])
        self.assertIn("after Edit src/a.js", hook[0]["detail"])
        (r,) = c1["tool_results"]
        self.assertEqual((r["attributed_tokens"], r["hook_tokens"]), (1, 3000))  # Edit back to its real size
        self.assertEqual(self.a["per_tool"]["Edit"]["result_tokens_attributed"], 1)
        self.assertEqual(sum(x["tokens"] for x in c1["attribution"]), c1["new_tokens"])

    def test_hook_totals(self):
        h = self.a["hooks"]["PostToolUse:Edit"]
        self.assertEqual((h["fires"], h["chars"], h["attributed_tokens"], h["carry_tokens"]),
                         (1, len(HOOK_TEXT), 3000, 6000))
        self.assertEqual(h["commands"], [HOOK_CMD])
        self.assertEqual(h["firings"][0]["capped_chars"], len(cap.cap_text(HOOK_TEXT)))

    def test_hook_stdout_counts_only_when_it_enters_context(self):
        from tokenscope.analyze import hook_entry
        runs = {}
        js = {"type": "hook_success", "hookName": "PostToolUse:Edit", "hookEvent": "PostToolUse", "stdout": "x" * 99}
        self.assertIsNone(hook_entry(js, runs))  # PostToolUse plain stdout isn't shown to Claude
        sp = {"type": "hook_success", "hookName": "UserPromptSubmit", "hookEvent": "UserPromptSubmit", "stdout": "y" * 40}
        self.assertEqual(hook_entry(sp, runs)["chars"], 40)

    def test_instruction_breakdown(self):
        ins = self.a["instructions"]
        files = {e["rel"]: e for e in ins["files"]}
        self.assertEqual(files["docs/api.md"]["imported_by"], "CLAUDE.md")
        self.assertEqual(files["docs/api.md"]["import_ref"], "@docs/api.md")
        self.assertEqual(files["docs/api.md"]["est_tokens"], 2250)
        self.assertIsNone(files["CLAUDE.md"]["imported_by"])
        secs = {s["heading"]: s for s in ins["sections"]}
        self.assertEqual(list(secs), ["Overview", "API", "Changelog", "Release"])
        self.assertEqual(secs["Changelog"]["chars"], len(CHANGELOG) + 1)
        self.assertEqual(ins["total_est_tokens"], (len(CLAUDE_MD) + 9000 + 3) // 4)

    def test_html(self):
        doc = report.render_session(self.a, self.fixes)
        for s in ("Hooks", "run-tests.sh", "System overhead", "@-imported by CLAUDE.md", "## Changelog",
                  "hook PostToolUse:Edit"):
            self.assertIn(s, doc)


class TestRules(HookCase):
    def test_noisy_hook(self):
        f = self.by_rule["noisy_hook"]
        kept = round(3000 * len(cap.cap_text(HOOK_TEXT)) / len(HOOK_TEXT))
        self.assertEqual(f["savings_new_tokens"], 3000 - kept)
        self.assertEqual(f["savings_all_in_tokens"], 9000 - 3 * kept)
        self.assertEqual(f["config"]["hook_wrap"], [{"hook": "PostToolUse:Edit", "event": "PostToolUse",
                                                     "command": HOOK_CMD}])
        self.assertIn("tokenscope-cap.py --max-lines 40", f["action_code"])

    def test_instructions_bloat(self):
        f = self.by_rule["instructions_bloat"]
        cl = (len(CHANGELOG) + 1 + 3) // 4
        self.assertGreaterEqual(cl, fixes.INSTR_SECTION_TOKENS)
        self.assertEqual(f["savings_new_tokens"], 2250 + cl)
        self.assertEqual(f["savings_all_in_tokens"], (2250 + cl) * 4)  # write + 3 later cache reads
        self.assertEqual([x["path"] for x in f["config"]["instr_imports"]], ["docs/api.md"])
        self.assertEqual([x["dest"] for x in f["config"]["instr_sections"]], ["docs/claude/changelog.md"])
        self.assertTrue(any("docs/api.md: ~2,250 tokens" in e for e in f["evidence"]))
        self.assertIn("estimate", f["savings_formula"])

    def test_quiet_below_thresholds(self):
        a = dict(self.a, hooks={}, instructions=dict(self.a["instructions"], total_est_tokens=100))
        rules = {f["rule"] for f in fixes.session_fixes(a)}
        self.assertFalse(rules & {"noisy_hook", "instructions_bloat"})


class TestOverlay(HookCase):
    def test_overlay_emit(self):
        with tempfile.TemporaryDirectory() as d:
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = cli.main(["fixes", "--root", self.root, "--session", "cccc", "--emit-config", d, "--overlay"])
            self.assertEqual(rc, 0)
            got = sorted(os.path.relpath(os.path.join(p, n), d) for p, _, ns in os.walk(d) for n in ns)
            self.assertEqual(got, sorted([".claude/hooks/tokenscope-cap.py", ".claude/settings.json", "CLAUDE.md",
                                          "TOKENSCOPE_CHANGES.md", "docs/claude/changelog.md"]))
            md = _read(os.path.join(d, "CLAUDE.md"))
            self.assertNotIn("@docs/api.md", md)
            self.assertIn("`docs/api.md` (not auto-loaded: Read docs/api.md only when you need it)", md)
            self.assertIn("## Changelog\nMoved to `docs/claude/changelog.md`", md)
            self.assertNotIn("- 0.7:", md)
            self.assertIn("## Overview\nSmall repo.", md)
            self.assertIn("## Token hygiene", md)
            moved = _read(os.path.join(d, "docs/claude/changelog.md"))
            self.assertTrue(moved.startswith("# Changelog\n- 0.0:"))
            st = json.loads(_read(os.path.join(d, ".claude/settings.json")))
            self.assertEqual(st["model"], "x")  # existing settings kept
            cmd = st["hooks"]["PostToolUse"][0]["hooks"][0]["command"]
            self.assertEqual(cmd, 'python3 "$CLAUDE_PROJECT_DIR"/.claude/hooks/tokenscope-cap.py --max-lines 40 -- '
                                  "'\"$CLAUDE_PROJECT_DIR\"/.claude/hooks/run-tests.sh'")
            ch = _read(os.path.join(d, "TOKENSCOPE_CHANGES.md"))
            for s in ("noisy_hook:PostToolUse:Edit", "instructions_bloat:instructions", "est. saves",
                      "## Not applied automatically"):
                self.assertIn(s, ch)
        self.assertEqual(json.loads(_read(self.settings_path)), SETTINGS)  # repo untouched
        self.assertFalse(os.path.exists(os.path.join(self.cwd, "CLAUDE.md")))

    def test_default_emit_unchanged(self):
        with tempfile.TemporaryDirectory() as d:
            fixes.emit_config(self.fixes, d)
            self.assertEqual(sorted(os.listdir(d)), ["CLAUDE.md.suggested", "settings.suggested.json"])

    def test_never_writes_outside(self):
        with tempfile.TemporaryDirectory() as d:
            for bad in ("../x", "/etc/x", "a/../../x"):
                with self.assertRaises(ValueError):
                    fixes._safe_path(d, bad)
            self.assertTrue(fixes._safe_path(d, "docs/claude/x.md").startswith(os.path.realpath(d)))

    def test_overlay_needs_emit_config(self):
        with redirect_stdout(io.StringIO()):
            self.assertEqual(cli.main(["fixes", "--root", self.root, "--session", "cccc", "--overlay"]), 2)


class TestCapScript(unittest.TestCase):
    def test_cap_text(self):
        self.assertEqual(cap.cap_text("a\nb\n", 5), "a\nb\n")
        lines = ["l%d" % i for i in range(100)]
        lines[10] = "FAIL: thing"
        lines[20] = "  AssertionError: x"
        out = cap.cap_text("\n".join(lines) + "\n", 5).split("\n")
        self.assertTrue(out[0].startswith("[tokenscope-cap: 93 of 100 lines omitted"))
        self.assertEqual(out[1:], ["FAIL: thing", "  AssertionError: x", "l95", "l96", "l97", "l98", "l99", ""])

    def test_cap_output_json_stays_valid(self):
        js = json.dumps({"hookSpecificOutput": {"hookEventName": "PostToolUse",
                                                "additionalContext": "\n".join(str(i) for i in range(100))}})
        obj = json.loads(cap.cap_output(js, 10))
        ctx = obj["hookSpecificOutput"]["additionalContext"].split("\n")
        self.assertEqual(len(ctx), 11)
        self.assertEqual(obj["hookSpecificOutput"]["hookEventName"], "PostToolUse")
        other = json.dumps({"decision": "block", "reason": "x"})
        self.assertEqual(cap.cap_output(other, 1), other)

    def test_script_runs_command(self):
        script = cap.__file__
        inner = ("python3 -c 'import sys,json; d=sys.stdin.read(); "
                 "print(json.dumps({\"hookSpecificOutput\":{\"additionalContext\":d+\"\\n\"+\"\\n\".join(map(str,range(50)))}})); "
                 "sys.exit(3)'")
        p = subprocess.run([sys.executable, script, "--max-lines", "5", "--", inner], input=b"STDIN-OK",
                           stdout=subprocess.PIPE)
        self.assertEqual(p.returncode, 3)
        ctx = json.loads(p.stdout)["hookSpecificOutput"]["additionalContext"]
        self.assertEqual(ctx.split("\n")[1:], ["45", "46", "47", "48", "49"])
        p = subprocess.run([sys.executable, script, "--max-lines", "2", "--", "seq", "1", "9"], stdin=subprocess.DEVNULL,
                           stdout=subprocess.PIPE)
        self.assertEqual(p.stdout.decode().split("\n")[1:], ["8", "9", ""])


if __name__ == "__main__":
    unittest.main()
