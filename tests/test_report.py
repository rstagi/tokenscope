import io
import os
import re
import sys
import tempfile
import unittest
from contextlib import redirect_stdout

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tokenscope import cli, report  # noqa: E402
from tokenscope.analyze import analyze_session  # noqa: E402
from tokenscope.parse import load_sessions  # noqa: E402

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "projects")
EXTERNAL = re.compile(r"""<(?:script|link|img|iframe|source)\b[^>]*\b(?:src|href)\s*=\s*["']?\s*(?:https?:)?//""", re.I)
URL_IN_CSS = re.compile(r"""(?:url\(\s*["']?|@import\s+["']?)\s*(?:https?:)?//""", re.I)


def assert_self_contained(tc, doc):
    tc.assertTrue(doc.startswith("<!DOCTYPE html>"))
    tc.assertIsNone(EXTERNAL.search(doc), "external resource reference")
    tc.assertIsNone(URL_IN_CSS.search(doc), "external CSS url")
    tc.assertNotIn("http://", doc)
    tc.assertNotIn("https://", doc)
    tc.assertIn("where did my tokens go?", doc)


class TestReport(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.analyses = [analyze_session(s) for s in load_sessions(ROOT)]

    def test_session_reports(self):
        for a in self.analyses:
            doc = report.render_session(a, fixes=["try a smaller Read"])
            assert_self_contained(self, doc)
            self.assertIn(a["session_id"], doc)
            self.assertIn("try a smaller Read", doc)
            for sect in ("Timeline", "Spikes explained", "Turns", "Breakdown", "Offered vs used", "Fixes"):
                self.assertIn(sect, doc)

    def test_session_markers(self):
        a = next(a for a in self.analyses if a["session_id"].startswith("1111"))
        doc = report.render_session(a)
        self.assertIn("cache miss", doc)
        self.assertIn("compaction", doc)
        self.assertIn("Explore subagent", doc)
        self.assertIn("<svg", doc)

    def test_cross_report(self):
        doc = report.render_cross(self.analyses)
        assert_self_contained(self, doc)
        for a in self.analyses:
            self.assertIn('id="s-%s"' % a["session_id"][:8], doc)
        self.assertIn("How intent is computed", doc)
        self.assertIn("Methodology", doc)

    def test_cli_report(self):
        with tempfile.TemporaryDirectory() as d:
            for extra in ([], ["--session", "1111"]):
                out = os.path.join(d, "r.html")
                with redirect_stdout(io.StringIO()):
                    rc = cli.main(["report", "--root", ROOT, "-o", out] + extra)
                self.assertEqual(rc, 0)
                with open(out, encoding="utf-8") as f:
                    assert_self_contained(self, f.read())

    def test_fmt_tok(self):
        for n, s in ((950, "950"), (8_412, "8.4k"), (89_203, "89.2k"), (99_960, "100k"), (412_300, "412k"),
                     (999_700, "1.0M"), (1_340_000, "1.3M")):
            self.assertEqual(cli.fmt_tok(n), s)

    def test_spike_label_matches_cause(self):
        for a in self.analyses:
            for e in report.entries(a):
                if e["spike"] and e["top"]:
                    self.assertTrue(e["top_label"].startswith("+%s " % cli.fmt_tok(e["top"]["tokens"])))

    def test_cross_biggest_drain_tile(self):
        self.assertIn("biggest single drain", report.render_cross(self.analyses))


class TestCompareHelpers(unittest.TestCase):
    def test_prompt_diff(self):
        from tokenscope.compare import prompt_diff
        self.assertIsNone(prompt_diff("fix  the\nbug", "fix the bug"))
        d = prompt_diff("fix the bug and read logs/app.log in full", "fix the bug")
        self.assertEqual(d, {"a": "and read logs/app.log in full", "b": ""})
        self.assertLessEqual(len(prompt_diff("x " * 400, "y")["a"]), 300)


if __name__ == "__main__":
    unittest.main()
