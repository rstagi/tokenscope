import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import synth  # noqa: E402
from tokenscope import cli, compare, fixes, report  # noqa: E402
from tokenscope.analyze import analyze_session  # noqa: E402
from tokenscope.parse import find_session  # noqa: E402
from test_report import assert_self_contained  # noqa: E402

FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "projects")


class SynthCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.root = synth.make(cls.tmp.name)
        cls.waste = analyze_session(find_session(synth.WASTE, cls.root))
        cls.lean = analyze_session(find_session(synth.LEAN, cls.root))
        cls.fixes = fixes.session_fixes(cls.waste)
        cls.by_rule = {}
        for f in cls.fixes:
            cls.by_rule.setdefault(f["rule"], []).append(f)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()


class TestRules(SynthCase):
    def test_carry_helper(self):
        calls = [{}, {}, {"compaction": {"x": 1}}, {}]
        self.assertEqual(fixes.later_calls(calls, 0), 1)  # stops at compaction
        self.assertEqual(fixes.later_calls(calls, 2), 1)
        self.assertEqual(fixes.later_calls(calls, 3), 0)

    def test_large_file_read_artifact(self):
        (f,) = self.by_rule["large_file_read"]
        self.assertEqual(f["target"], "dist/app.min.js")
        self.assertEqual(f["savings_new_tokens"], 15000)
        self.assertEqual(f["savings_all_in_tokens"], 15000 + 15000 * 5)
        self.assertEqual(f["savings_formula"], "15,000 written + 15,000 x 5 later cache reads = 90,000")
        self.assertEqual(f["title"], "Reading dist/app.min.js cost 15.0k tokens, then rode along in 5 more calls")
        self.assertEqual(f["config"]["deny"], ["Read(./dist/**)"])
        self.assertIn("Read(./dist/**)", f["action_code"])
        self.assertEqual(f["severity"], "high")  # 90k of 281k all-in

    def test_repeated_reads(self):
        (f,) = self.by_rule["repeated_reads"]
        self.assertEqual(f["target"], "src/a.py")
        self.assertEqual((f["savings_new_tokens"], f["savings_all_in_tokens"]), (500, 500 + 500 * 3))
        self.assertIn("read 2 times", f["title"])

    def test_large_bash(self):
        (f,) = self.by_rule["large_bash_output"]
        self.assertEqual(f["target"], "npm test")
        self.assertEqual((f["savings_new_tokens"], f["savings_all_in_tokens"]), (3000, 9000))
        self.assertIn("npm test -- --verbose 2>&1 | tail -40", f["action_code"])

    def test_unused_mcp_and_skills(self):
        (m,) = self.by_rule["unused_mcp"]
        self.assertEqual(m["target"], "bigsrv")
        self.assertEqual((m["savings_new_tokens"], m["savings_all_in_tokens"]), (500, 3500))
        self.assertIn("~500 tokens per call x 7 calls", m["title"])
        (s,) = self.by_rule["unused_skills"]
        per = self.waste["offered_vs_used"]["skills_unused_est_tokens"]
        self.assertEqual(s["savings_all_in_tokens"], per * 7)
        self.assertEqual(s["severity"], "low")

    def test_cache_miss(self):
        (f,) = self.by_rule["cache_misses"]
        self.assertEqual(f["savings_new_tokens"], 44405)
        self.assertEqual(f["savings_all_in_tokens"], 0)
        self.assertIn("1 cache miss re-wrote 44.4k tokens", f["title"])

    def test_context_bloat_overlap_not_double_counted(self):
        (f,) = self.by_rule["context_bloat"]
        self.assertIn("call #1 alone added 15.1k", f["title"])
        self.assertEqual(f["overlaps"], "large_file_read:dist/app.min.js")
        self.assertEqual(f["savings_all_in_tokens"], 0)  # 5 x (40,105-25,005-5,000) = 50,500 < 90,000

    def test_system_overhead(self):
        (f,) = self.by_rule["system_overhead"]
        self.assertTrue(f["title"].startswith("First call loaded 25.0k tokens"))
        self.assertEqual(f["savings_all_in_tokens"], 0)

    def test_sorted_and_fields(self):
        vals = [(f["savings_cost_weighted_tokens"], f["savings_new_tokens"]) for f in self.fixes]
        self.assertEqual(vals, sorted(vals, reverse=True))
        # the 2h cache miss (44,405 re-written instead of read) outweighs the 90k all-in large read once priced
        self.assertEqual(self.fixes[0]["rule"], "cache_misses")
        for f in self.fixes:
            for k in ("id", "rule", "severity", "title", "evidence", "action", "action_code", "est_savings_tokens",
                      "savings_formula", "confidence", "savings_cost_weighted_tokens", "savings_usd"):
                self.assertIn(k, f)
            self.assertIn(f["severity"], ("high", "med", "low"))

    def test_lean_session_quiet(self):
        rules = {f["rule"] for f in fixes.session_fixes(self.lean)}
        self.assertFalse(rules & {"large_file_read", "repeated_reads", "large_bash_output", "cache_misses",
                                  "context_bloat", "system_overhead"})

    def test_subagent_fanout(self):
        a = analyze_session(find_session("1111", FIXTURES))
        self.assertNotIn("subagent_fanout", {f["rule"] for f in fixes.session_fixes(a)})
        old = fixes.SUBAGENT_MIN_ALL_IN
        fixes.SUBAGENT_MIN_ALL_IN = 100
        try:
            (f,) = [f for f in fixes.session_fixes(a) if f["rule"] == "subagent_fanout"]
        finally:
            fixes.SUBAGENT_MIN_ALL_IN = old
        sub = a["subagents"][0]["tokens"]["all_in"]
        ret = next(r for r in a["calls"][1]["tool_results"] if r["tool"] == "Agent")["attributed_tokens"]
        self.assertEqual(f["savings_all_in_tokens"], sub - ret)

    def test_aggregate(self):
        per = {a["session_id"]: fixes.session_fixes(a) for a in (self.waste, self.lean)}
        agg = fixes.aggregate(per, [self.waste, self.lean])
        mcp = next(f for f in agg if f["id"] == "unused_mcp:bigsrv")
        self.assertEqual(len(mcp["sessions"]), 2)
        self.assertEqual(mcp["savings_all_in_tokens"], 500 * 7 + 500 * 2)
        self.assertIn("in 2 sessions", mcp["title"])
        single = next(f for f in agg if f["rule"] == "large_file_read")
        self.assertEqual(single["savings_all_in_tokens"], 90000)


class TestOutputs(SynthCase):
    def run_cli(self, *argv):
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = cli.main(list(argv))
        return rc, buf.getvalue()

    def test_cli_fixes_and_emit_config(self):
        with tempfile.TemporaryDirectory() as d:
            rc, out = self.run_cli("fixes", "--root", self.root, "--session", "aaaa", "--emit-config", d)
            self.assertEqual(rc, 0)
            self.assertIn("[HIGH] Reading dist/app.min.js", out)
            self.assertIn("15,000 written + 15,000 x 5 later cache reads = 90,000", out)
            with open(os.path.join(d, "settings.suggested.json")) as fh:
                self.assertEqual(json.load(fh), {"permissions": {"deny": ["Read(./dist/**)"]}})
            with open(os.path.join(d, "CLAUDE.md.suggested")) as fh:
                self.assertIn("npm test", fh.read())
            rc, out = self.run_cli("fixes", "--root", self.root, "--json")
            self.assertEqual(rc, 0)
            self.assertTrue(any(f["rule"] == "unused_mcp" for f in json.loads(out)))

    def test_html_cards(self):
        doc = report.render_session(self.waste, self.fixes)
        assert_self_contained(self, doc)
        self.assertIn('class="fix sev-high"', doc)
        self.assertIn("Read(./dist/**)", doc)
        self.assertIn("evidence (", doc)
        per = {a["session_id"]: fixes.session_fixes(a) for a in (self.waste, self.lean)}
        doc = report.render_cross([self.waste, self.lean], per, cross_fixes=fixes.aggregate(per, [self.waste, self.lean]))
        assert_self_contained(self, doc)
        self.assertIn("same rule + target merged", doc)
        self.assertIn("2 sessions", doc)


class TestCompare(SynthCase):
    def test_compare_data(self):
        d = compare.compare_data(self.waste, self.lean)
        rows = {r["key"]: r for r in d["rows"]}
        self.assertEqual(rows["all_in"]["a"], self.waste["totals"]["all"]["all_in"])
        self.assertEqual(rows["calls"]["a"], 7)
        self.assertEqual(rows["calls"]["b"], 2)
        self.assertLess(rows["all_in"]["pct"], 0)
        self.assertNotIn("cost", rows)  # neither has cost-state
        p = round(-rows["cost_weighted"]["pct"])
        self.assertTrue(d["headline"].startswith("B used %d%% fewer cost-weighted tokens (" % p), d["headline"])
        self.assertEqual(rows["cost_weighted"]["a"], self.waste["totals"]["all"]["cost_weighted"])
        # synth model is unpriced: no $, the unpriced calls are named; new/output/cache reads always shown
        self.assertIn("(9 calls unpriced: claude-test-1)", d["headline"])
        self.assertNotIn("$", d["headline"])
        self.assertRegex(d["headline"], r"; new [^,]+ -> [^,]+, output \S+ -> \S+, cache reads \S+ -> \S+ \(all-in ")
        self.assertIn("gone in B: ", d["headline"])
        self.assertIn("Read dist/app.min.js (15.0k)", d["headline"])
        gone = {t["cause"] for t in d["top_causes"] if t["status"] == "gone"}
        self.assertIn("Read dist/app.min.js", gone)
        self.assertGreater(len(d["fixes_a"]), len(d["fixes_b"]))
        txt = compare.to_text(d)
        self.assertIn("fixes triggered: A %d" % len(d["fixes_a"]), txt)

    def test_compare_html_shared_scale(self):
        d = compare.compare_data(self.waste, self.lean)
        doc = compare.render_html(d)
        assert_self_contained(self, doc)
        for s in ("A · before", "B · after", "Context size per main call", "Top causes, A vs B", "Fixes triggered in"):
            self.assertIn(s, doc)
        # both timelines share the y-axis top label
        import re
        tops = re.findall(r'<h3 class="tl">.*?</h3><svg.*?</svg>', doc, re.S)
        self.assertEqual(len(tops), 2)
        ylabels = [re.findall(r'class="axl" text-anchor="end">([^<]+)<', t) for t in tops]
        self.assertEqual(ylabels[0], ylabels[1])

    def test_cli_compare(self):
        with tempfile.TemporaryDirectory() as d:
            out = os.path.join(d, "c.html")
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = cli.main(["compare", "--root", self.root, "aaaa", "bbbb", "-o", out])
            self.assertEqual(rc, 0)
            self.assertIn("fewer cost-weighted tokens", buf.getvalue())
            self.assertTrue(os.path.getsize(out) > 1000)


if __name__ == "__main__":
    unittest.main()
