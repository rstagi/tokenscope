"""Understand section of the HTML report: summary, goals table, flow SVG, chains/examples, context chart."""
import io
import os
import re
import sys
import tempfile
import unittest
from contextlib import redirect_stdout

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from test_report import ROOT, assert_self_contained  # noqa: E402
from test_understand import LATER, SID, make  # noqa: E402
from tokenscope import cli, report  # noqa: E402
from tokenscope import understand as un  # noqa: E402
from tokenscope import understand_html as uh  # noqa: E402
from tokenscope.report import esc  # noqa: E402
from tokenscope.analyze import analyze_session  # noqa: E402
from tokenscope.parse import find_session, load_sessions  # noqa: E402


class TestUnderstandHtml(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        make(cls.tmp.name)
        cls.session = find_session(SID[:8], cls.tmp.name)
        cls.a = analyze_session(cls.session)
        cls.u = un.understand([cls.session], now=LATER, analyses={cls.session.id: cls.a})
        cls.doc = report.render_session(cls.a, ["try a smaller Read"], understand=cls.u)
        sessions = load_sessions(ROOT)
        cls.analyses = [analyze_session(s) for s in sessions]
        cls.cu = un.understand(sessions, now=LATER, analyses={a["session_id"]: a for a in cls.analyses})
        cls.cross = report.render_cross(cls.analyses, cross_fixes=[], understand=cls.cu, compare_link="cmp.html")

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_story_order_and_nav(self):
        for doc in (self.doc, self.cross):
            assert_self_contained(self, doc)
            i_u, i_o = doc.index('id="understand"'), doc.index('id="optimize"')
            self.assertLess(i_u, i_o)
            self.assertIn('<nav class="steps">', doc)
        self.assertIn('<a href="cmp.html">4 Compare</a>', self.cross)
        self.assertIn('class="off" title="no compare report linked', self.doc)
        # Fixes come before the timeline inside Optimize
        opt = self.doc[self.doc.index('id="optimize"'):]
        self.assertLess(opt.index("<h2>Fixes</h2>"), opt.index("Timeline"))

    def test_summary(self):
        p = re.search(r'WHAT THE AGENT DID</div><p>(.*?)</p>', self.doc).group(1)
        self.assertRegex(p, r"^3 requests → 3 goals in \d areas; 2 of 3 finished \(1 abandoned\); 1 goal never "
                           r"finished: .+\. Goal status: 1 Failing, 2 Healthy\.")
        self.assertIn("Hooks fired 2 times (PostToolUse:Edit ×2); every edit triggered a hook", p)
        self.assertIn("first move was Bash(./run_tests.sh) (1/3)", p)
        self.assertIn("1 request ran tests; the last test signal was green in 1.", p)

    def test_goals_table(self):
        d = self.doc
        self.assertRegex(d, r"3 GOALS IN \d AREAS? · DISCOVERED FROM 3 REQUESTS")
        self.assertEqual(d.count('<details class="wf"'), 3)
        for g in self.u["goals"]:
            self.assertIn(esc(g["goal"]), d)
        self.assertIn("named from terms", d)
        self.assertIn('class="stp st-fail" title="Failing: finish rate 0% &lt; 70%"', d)  # Refactor: abandoned
        self.assertIn("<b>0</b> requests got no tool steps", d)

    def test_flow_svg(self):
        reqs = self.u["sessions"][0]["requests"]
        svg = uh.flow_svg(reqs)
        self.assertTrue(svg.startswith('<svg class="flow"'))
        self.assertIn("1ST CALL · 3", svg)
        self.assertIn("2ND CALL · 2", svg)
        self.assertIn("100% after Bash(./run_tests.sh)", svg)  # Edit after Bash(./run_tests.sh)
        self.assertIn("+hook ×2", svg)               # 2 hooks after the Edit node (collapsed Edit×2)
        self.assertIn('class="fe main"', svg)
        self.assertIn("No tool calls", uh.flow_svg([]))

    def test_examples_and_replay(self):
        d = self.doc
        self.assertIn("The agent ran Bash(./run_tests.sh), then edited src/pricing.js 2×; hooks fired 2×; last tests: 14 passed.",
                      d)
        self.assertIn('hook <span class="mono">PostToolUse:Edit</span> → 1 failed', d)
        self.assertIn('ran <span class="mono">Bash(./run_tests.sh)</span> — 1 failed', d)
        self.assertIn('href="#v-dddddddd">open in session view', d)
        self.assertIn('id="v-dddddddd"', d)
        self.assertIn('class="oc oc-ab"', d)   # abandoned request
        self.assertIn('class="oc oc-ok"', d)
        fix_goal = next(g for g in self.u["goals"] if "dddddddd:1" in g["request_ids"])
        self.assertIn("1 path inside %s" % esc(fix_goal["goal"]), d)
        self.assertIn('class="rd rd-1"', d)

    def test_context_chart(self):
        d = self.doc
        self.assertIn("CONTEXT SPEND OVER TIME", d)
        ctx = d[d.index('class="cx-run"'):d.index('class="cx-cum"')]
        self.assertEqual(ctx.count('<g class="bar">'), 3)  # one bar per request in a session report
        self.assertIn('id="cx-cum"', d)
        self.assertIn("Per request", d)
        self.assertRegex(d, r"Tool results( \*)? <b class=\"mono\">\d+(\.\d)?%</b>")
        self.assertIn("OFFERED VS USED", d)
        # cross report: one bar per session
        ctx = self.cross[self.cross.index('class="cx-run"'):self.cross.index('class="cx-cum"')]
        self.assertEqual(ctx.count('<g class="bar">'), len(self.analyses))

    def test_context_units_sum_to_context(self):
        units, _ = uh.context_units(self.u, per_request=True)
        main = self.u["sessions"][0]["context"]["main"]
        self.assertEqual(sum(sum(src.values()) for _, _, src in units), sum(sum(c["sources"].values()) for c in main))

    def test_narrate(self):
        r = {"steps": [], "outcome": "success", "flags": []}
        self.assertEqual(uh.narrate(r), "The agent answered without using tools.")

    def test_chip_labels_distinct(self):
        a, b = "A → B → C → D → E", "A → B → C → F"
        labs = uh.chip_labels([a, b])
        self.assertNotEqual(labs[a], labs[b])
        self.assertEqual(uh.chip_labels(["A → B"])["A → B"], "A → B")

    def test_cli_flags(self):
        with tempfile.TemporaryDirectory() as d:
            out = os.path.join(d, "r.html")
            with redirect_stdout(io.StringIO()):
                rc = cli.main(["report", "--root", ROOT, "-o", out, "--compare-link", "c.html"])
            self.assertEqual(rc, 0)
            with open(out, encoding="utf-8") as f:
                doc = f.read()
            assert_self_contained(self, doc)
            self.assertIn('<a href="c.html">4 Compare</a>', doc)
            self.assertIn("<h2>Goals</h2>", doc)


if __name__ == "__main__":
    unittest.main()
