import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tokenscope.live.statusline import fmt_tokens, line  # noqa: E402
from tokenscope.live.tail import Tailer  # noqa: E402


class TestStatusline(unittest.TestCase):
    def test_line(self):
        d = {"goal": {"name": "Fix: tax"}, "current_intent": "read_code", "cw": 1_234_000, "usd": 3.1,
             "flags": [{"flag": "reread_file", "count": 1}]}
        self.assertEqual(line(d), "◆ Fix: tax · read code · 1.2M CW · $3.10 · ⚠ reread file")
        self.assertEqual(fmt_tokens(950), "950")
        self.assertEqual(line({}), "◆ session · 0 CW")


class TestProjectFilter(unittest.TestCase):
    def test_glob(self):
        with tempfile.TemporaryDirectory() as root:
            for proj in ("-home-u-alpha", "-home-u-beta"):
                os.makedirs(os.path.join(root, proj))
                with open(os.path.join(root, proj, "s-%s.jsonl" % proj[-4:]), "w") as f:
                    f.write(json.dumps({"type": "user", "message": {"content": "hi"}}) + "\n")
            t = Tailer(root, project="*alpha*")
            self.assertEqual([os.path.basename(p) for p in t._candidates()], ["s-lpha.jsonl"])
            self.assertEqual(len(Tailer(root)._candidates()), 2)


if __name__ == "__main__":
    unittest.main()


class TestScriptWrites(unittest.TestCase):
    def test_inline_script_that_writes_is_edit(self):
        from tokenscope import intents as I
        w = "python3 - <<'EOF'\np='src/a.js'; s=open(p).read()\nopen(p,'w').write(s)\nEOF"
        self.assertEqual(I.tool_intent(I.record("Bash", {"command": w})), "edit")
        self.assertEqual(I.tool_intent(I.record("Bash", {"command": "node -e 'console.log(1)'"})), "compute")
        self.assertEqual(I.tool_intent(I.record("Bash", {"command": "node -e \"require('fs').writeFileSync('x','y')\""})), "edit")


from test_live import SID, LiveFixture, assistant, lines, tool_use, user_prompt  # noqa: E402
from tokenscope.live import state as st  # noqa: E402

TRACE = ("Traceback (most recent call last):\n  File \"/srv/app/%s\", line %d, in <module>\n%s\n")


def res(i, tid, text, err=False, stdout=None):
    r = {"type": "user", "timestamp": "2026-01-01T00:00:%02d.000Z" % (i % 60), "sessionId": SID, "uuid": "r%d" % i,
         "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": tid, "is_error": err,
                                                  "content": text}]}}
    if stdout is not None:
        r["toolUseResult"] = {"stdout": stdout, "stderr": ""}
    return r


class Scenario(LiveFixture):
    """Feeds one scenario as a list of (tool name, input, result text, is_error) main-thread tool turns."""

    def run_turns(self, turns, prompt="The test suite is red. Fix the failing tests in invoice.py", redact=True):
        self.state = st.LiveState(redact=redact, clock=lambda: 1e10)
        recs, i = [user_prompt(0, prompt)], 1
        for k, (name, inp, text, err) in enumerate(turns):
            tid = "t%d" % k
            recs += [assistant(i, "m%d" % k, [tool_use(tid, name, inp)]),
                     res(i + 1, tid, text, err, stdout=text if name == "Bash" else None)]
            i += 2
        self.write(self.main, lines(*recs))
        return self.step()


def pytest_fail(n_failed, msg):
    return "FAILED tests/test_invoice.py::test_%d - %s\n%d failed, 7 passed in 0.4%ds" % (n_failed, msg, n_failed, n_failed)


READ = ("Read", {"file_path": "/srv/app/invoice.py"}, "code", False)
EDIT = ("Edit", {"file_path": "/srv/app/invoice.py", "old_string": "a", "new_string": "b"}, "ok", False)
TEST = ("Bash", {"command": "python -m pytest -q"})


class TestWarningScenarios(Scenario):
    def test_normal_bugfix_session_has_no_warnings(self):
        errs = ["AssertionError: assert 3 == 4", "KeyError: 'total'", "TypeError: unsupported operand", "ImportError: x"]
        turns = [READ, READ]
        for k, e in enumerate(errs):
            turns += [TEST + (pytest_fail(4 - k, e), True), READ, EDIT]
        turns += [TEST + ("8 passed in 0.52s", False)]
        s = self.run_turns(turns, redact=False)
        self.assertEqual(s.warnings, {})
        c = self.state.session_compact(s)
        self.assertEqual(c["warnings"], [])
        self.assertEqual(c["tests"]["passed"], 8)
        self.assertEqual(c["tests"]["failed"], 0)
        self.assertTrue(c["tests"]["ok"])
        self.assertEqual(c["tests"]["runs"], 5)
        self.assertEqual(c["tests"]["trend"], [4, 3, 2, 1, 0])
        self.assertEqual(c["files_edited"], ["invoice.py"])
        self.assertEqual(c["now"]["intent"], "test")
        self.assertEqual(c["now"]["label"], "running tests")
        self.assertEqual(c["now"]["tool"], "Bash python")
        self.assertEqual(c["asked"]["goal"], "Fix: failing tests invoice.py")
        self.assertEqual(c["asked"]["prompt_n"], 1)

    def test_stuck_import_error(self):
        imp = TRACE % ("main.py", 3, "ImportError: cannot import name 'Invoice' from 'billing'")
        turns = [TEST + (imp, True), READ, TEST + (imp.replace("line 3", "line 4"), True),
                 ("Grep", {"pattern": "Invoice"}, "x", False), TEST + (imp, True)]
        s = self.run_turns(turns)
        self.assertEqual(list(s.warnings), ["stuck_error"])
        w = self.state.session_compact(s)["warnings"][0]
        self.assertEqual((w["rule"], w["count"], w["text"]), ("stuck_error", 1, "same error ×3, no edits since"))
        self.assertIn("different approach", w["hint"])
        self.assertEqual(self.state.session_compact(s)["tests"], {"passed": None, "failed": None, "ok": False,
                                                                 "runs": 3, "last_ts": s.tests[-1]["ts"],
                                                                 "trend": [None, None, None]})

    def test_reread_loop(self):
        s = self.run_turns([READ] * 4)
        self.assertEqual(s.warnings["reread_loop"]["text"], "re-read .py ×4")
        part = ("Read", {"file_path": "/srv/app/invoice.py", "offset": 10, "limit": 20}, "code", False)
        self.setUp()
        self.assertEqual(self.run_turns([READ, part, part, READ, part]).warnings, {})
        self.setUp()
        self.assertEqual(self.run_turns([READ, READ, EDIT, READ, READ]).warnings, {})
        self.setUp()
        cat = ("Bash", {"command": "cat /srv/app/invoice.py"}, "code", False)
        s = self.run_turns([cat, READ, cat, READ], redact=False)
        self.assertEqual(s.warnings["reread_loop"]["text"], "re-read invoice.py ×4")

    def test_context_near_full_and_latest_prompt(self):
        big = {"input_tokens": 10, "cache_creation_input_tokens": 1000, "cache_read_input_tokens": 180000,
               "output_tokens": 5}
        self.state = st.LiveState(redact=True, clock=lambda: 1e10)
        self.write(self.main, lines(user_prompt(0, "fix the tax"),
                                    assistant(1, "m1", [{"type": "text", "text": "ok"}], usage=big, stop="end_turn"),
                                    user_prompt(2, "now add a csv export to the report page")))
        s = self.step()
        w = s.warnings["context_near_full"]
        self.assertEqual(w["text"], "context 91% full")
        self.assertIn("/compact", w["hint"])
        a = self.state.session_compact(s)["asked"]
        self.assertEqual((a["prompt_n"], a["goal"]), (2, "Fix: tax"))
        self.assertTrue(a["latest"].startswith("Build: csv export"))
        self.assertIsNotNone(a["latest_ts"])
        self.state.set_prompt_name(SID, 2, "Add CSV export")
        self.assertEqual(self.state.session_compact(s)["asked"]["latest"], "Add CSV export")


class TestGoalNames(unittest.TestCase):
    CASES = [
        ("The test suite is red. Fix the failing tests in invoice_service.py", "Fix: failing tests invoice_service.py"),
        ("You are a senior engineer. Add retry logic to fetchOrders when the API returns 503",
         "Build: retry logic fetchOrders api"),
        ("please fix the checkout total tax in cart.py", "Fix: checkout total tax cart.py"),
        ("Can you refactor the parse_config function to use dataclasses?", "Refactor: parse_config function dataclasses"),
        ("The login page crashes on Safari. Can you debug why?", "Fix: login page crashes safari"),
        ("Why does useEffect run twice in OrderList?", "Explain: useEffect run twice OrderList"),
        ("Context: we migrated to postgres last week. Investigate slow queries in the reports endpoint.",
         "Research: slow queries reports endpoint"),
        ("Deploy the staging build and check the healthcheck", "Ship: staging build check healthcheck"),
        ("add a --dry-run flag to the sync CLI", "Build: dry-run flag sync cli"),
        ("Tests are failing after the upgrade to pydantic v2, please fix them", "Fix: tests failing after upgrade"),
    ]

    def test_realistic_prompts(self):
        from tokenscope.live.goalname import MAX_LEN, keyword_name
        for prompt, want in self.CASES:
            got = keyword_name(prompt)
            self.assertEqual(got, want, prompt)
            self.assertLessEqual(len(got), MAX_LEN)
        self.assertEqual(keyword_name(self.CASES[0][0], redact=True), "Fix: failing tests")
