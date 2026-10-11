"""Step 8e: Patterns section + drill replays + corpus mode in the HTML report."""
import contextlib
import io
import os
import re
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(__file__))
from test_patterns import make_positive, run  # noqa: E402
from test_report import EXTERNAL, URL_IN_CSS  # noqa: E402
from tokenscope import cli  # noqa: E402
from tokenscope import patterns_html as ph  # noqa: E402

SENSITIVE = ["alpha", "cart.js", "discount", "README.md", "big.py", "00000001-0000"]


def report(root, out, *extra):
    with contextlib.redirect_stdout(io.StringIO()):
        rc = cli.main(["report", "--root", root, "--no-cache", "--jobs", "1", "-o", out] + list(extra))
    assert rc == 0
    with open(out, encoding="utf-8") as f:
        return f.read()


def assert_self_contained(tc, doc):  # fixture WebFetch urls appear as text, so only check resource references
    tc.assertIsNone(EXTERNAL.search(doc))
    tc.assertIsNone(URL_IN_CSS.search(doc))


class PatternsHtmlTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.root = make_positive(os.path.join(cls.tmp.name, "pos"))
        cls.kinds = {p["kind"] for p in run(cls.root)["patterns"]}
        cls.doc = report(cls.root, os.path.join(cls.tmp.name, "r.html"))

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_sections_and_order(self):
        d = self.doc
        assert_self_contained(self, d)
        self.assertLess(d.index('id="understand"'), d.index('id="patterns"'))
        self.assertLess(d.index('id="patterns"'), d.index('id="optimize"'))
        self.assertIn('<a href="#patterns">2 Patterns</a>', d)
        self.assertIn("WHERE THE MONEY GOES", d)
        self.assertIn("cache write 1h", d)

    def test_every_kind_has_a_card(self):
        self.assertEqual(self.kinds, set(ph.KIND_ORDER))
        for k in self.kinds:
            self.assertIn('id="pk-%s"' % k, self.doc)
        pos = [self.doc.index('id="pk-%s"' % k) for k in ph.KIND_ORDER]
        self.assertEqual(pos, sorted(pos))

    def test_drill_anchors(self):
        links = set(re.findall(r'href="#(drill-[^"]+)"', self.doc))
        self.assertTrue(links)
        for a in links:
            self.assertIn('id="%s"' % a, self.doc)
        self.assertIn('class="hit"', self.doc)

    def test_no_patterns_flag(self):
        d = report(self.root, os.path.join(self.tmp.name, "n.html"), "--no-patterns")
        self.assertNotIn('id="patterns"', d)
        self.assertIn('id="optimize"', d)

    def test_redact(self):
        d = report(self.root, os.path.join(self.tmp.name, "x.html"), "--redact")
        self.assertIn('id="patterns"', d)
        for s in SENSITIVE:
            self.assertNotIn(s, d)

    def test_corpus_mode(self):
        d = report(self.root, os.path.join(self.tmp.name, "c.html"), "--corpus")
        assert_self_contained(self, d)
        self.assertIn("(corpus)", d)
        self.assertLessEqual(len(re.findall(r'<details class="sess" id="s-', d)), 10)
        self.assertNotIn("Top token drains over time", d)
        self.assertIn("workflow agents", d)


class CorpusTopTest(unittest.TestCase):
    def test_corpus_omits_non_top_sessions(self):
        from tokenscope import report as rp
        old = rp.CORPUS_DETAIL
        rp.CORPUS_DETAIL = 1
        try:
            with tempfile.TemporaryDirectory() as t:
                root = make_positive(os.path.join(t, "pos"))
                d = report(root, os.path.join(t, "c.html"), "--corpus")
        finally:
            rp.CORPUS_DETAIL = old
        self.assertEqual(len(re.findall(r'<details class="sess" id="s-', d)), 1)


if __name__ == "__main__":
    unittest.main()
