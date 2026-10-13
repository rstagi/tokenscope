"""tokenscope live: the multi-session board (/api/board rows: ASKED / NOW / sparkline / done / warnings), its
redaction, and the way back from a session canvas to it."""
import json
import os
import re
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from tokenscope.live import state as st  # noqa: E402
from test_live import SID, LiveFixture, assistant, lines, result, tool_use, user_prompt  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATIC = os.path.join(ROOT, "tokenscope", "live", "static")


def board_js():
    with open(os.path.join(STATIC, "live.js"), encoding="utf-8") as f:
        js = f.read()
    return js[js.index("// ---------- board ----------"):js.index("// ---------- session ----------")]


class TestBoardRow(LiveFixture):
    def row(self, now=None):
        s = self.state.sessions[SID]
        return self.state.board_row(s, s.last_ts + 1 if now is None else now)

    def test_fields(self):
        self.build()
        r = self.row()
        self.assertEqual(r["asked"]["goal"], r["goal"]["name"])
        self.assertIsNone(r["asked"]["latest"])          # one prompt: the goal is all that was asked
        self.assertEqual(r["now"]["tool"], "Agent(Explore)")   # the latest main-thread call
        self.assertFalse(r["ended"])
        self.assertTrue(r["cw_series"] and all(isinstance(x, int) for x in r["cw_series"]))
        self.assertEqual(r["warnings"][0]["rule"], "stuck_error")   # existing rules, as before

    def test_follow_up_prompt_is_the_latest_ask(self):
        self.build()
        self.write(self.main, lines(user_prompt(30, "now also add a test for the rounding"),
                                    assistant(31, "m9", [tool_use("t9", "Bash", {"command": "pytest -q"})])))
        self.step()
        r = self.row()
        self.assertEqual(r["asked"]["prompt_n"], 2)
        self.assertTrue(r["asked"]["latest"])
        self.assertEqual(r["now"]["tool"], "Bash pytest")

    def test_session_end_hook_marks_done(self):
        s = self.build()
        self.state.apply_hook({"event": "SessionEnd", "session_id": SID, "ts": s.last_ts + 5})
        self.assertTrue(self.row(s.last_ts + 6)["ended"])
        # a later transcript event (resumed) clears it
        self.write(self.main, lines(user_prompt(90, "one more thing")))
        self.step()
        self.assertFalse(self.row()["ended"])

    def test_redacted_row_leaks_no_paths_or_prompts(self):
        self.build()
        self.write(self.main, lines(user_prompt(30, "also look at /home/alice/secret/notes.txt please"),
                                    assistant(31, "m9", [tool_use("t9", "Read",
                                                                  {"file_path": "/home/alice/secret/notes.txt"})])))
        self.step()
        text = json.dumps(self.state.board(now=self.state.sessions[SID].last_ts + 1))
        for leak in ("/srv", "checkout-app", "cart.py", "/home", "alice", "notes.txt", "secret",
                     "please fix the checkout total tax"):
            self.assertNotIn(leak, text)
        r = self.row()
        self.assertEqual(r["now"]["target"], ".txt")      # extension only
        self.assertTrue(re.match(r"project-[A-Z]+$", r["project"]))

    def test_unredacted_row_names_the_file(self):
        self.state = st.LiveState(redact=False)
        self.build()
        self.write(self.main, lines(assistant(30, "m9", [tool_use("t9", "Read", {"file_path": "/srv/checkout-app/src/cart.py"})]),
                                    result(31, "t9")))
        self.step()
        r = self.row()
        self.assertEqual(r["now"]["target"], "cart.py")
        self.assertEqual(r["project"], "checkout-app")


class TestSparkSeries(unittest.TestCase):
    def test_short_series_kept(self):
        self.assertEqual(st.LiveState.cw_series([{"cw": 1.4}, {"cw": None}, {"cw": 3}]), [1, 0, 3])

    def test_long_series_bucketed_and_sum_kept(self):
        turns = [{"cw": float(i)} for i in range(1000)]
        out = st.LiveState.cw_series(turns)
        self.assertLessEqual(len(out), st.BOARD_SPARK_N)
        self.assertGreater(len(out), st.BOARD_SPARK_N // 2)
        self.assertEqual(sum(out), sum(range(1000)))
        self.assertEqual(out, sorted(out))   # rising costs stay rising


class TestBoardUi(unittest.TestCase):
    def test_board_card_shows_the_new_fields(self):
        code = board_js()
        for f in ("s.asked", "s.now", "s.cw_series", "s.ended", "s.warnings"):
            self.assertIn(f, code)
        self.assertIn("wdot", code)
        with open(os.path.join(STATIC, "live.css"), encoding="utf-8") as f:
            css = f.read()
        for sel in (".wdot", ".spark", ".pill.done", ".scard .latest"):
            self.assertIn(sel, css)

    def test_board_is_the_landing_page_with_top_spenders(self):
        with open(os.path.join(STATIC, "board.html"), encoding="utf-8") as f:
            page = f.read()
        self.assertIn('data-page="board"', page)
        self.assertIn('id="top"', page)
        self.assertIn('id="grid"', page)

    def test_canvas_links_back_to_the_list(self):
        with open(os.path.join(ROOT, "web", "canvas", "src", "main.jsx"), encoding="utf-8") as f:
            src = f.read()
        self.assertIn('className="back" href="/"', src)
        with open(os.path.join(STATIC, "canvas.js"), encoding="utf-8") as f:
            bundle = f.read()
        self.assertIn("all sessions", bundle)   # the committed bundle is rebuilt from that source


if __name__ == "__main__":
    unittest.main()
