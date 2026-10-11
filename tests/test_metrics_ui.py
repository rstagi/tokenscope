"""Honest metrics: cost-weighted + API-equivalent $ headlines, fix cost savings, request sources, scaling caps."""
import copy
import os
import re
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import synth  # noqa: E402
from test_report import assert_self_contained  # noqa: E402
from tokenscope import compare, fixes, pricing, report  # noqa: E402
from tokenscope.analyze import analyze_session  # noqa: E402
from tokenscope.parse import find_session  # noqa: E402

PRICED = "claude-sonnet-4-5"


def _reprice(root, model):
    for d, _, files in os.walk(root):
        for fn in files:
            if fn.endswith(".jsonl"):
                p = os.path.join(d, fn)
                with open(p, encoding="utf-8") as fh:
                    text = fh.read()
                with open(p, "w", encoding="utf-8") as fh:
                    fh.write(text.replace('"claude-test-1"', '"%s"' % model))


class MetricsCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        root = synth.make(os.path.join(cls.tmp.name, "u"))
        cls.waste = analyze_session(find_session(synth.WASTE, root))
        cls.lean = analyze_session(find_session(synth.LEAN, root))
        proot = synth.make(os.path.join(cls.tmp.name, "p"))
        _reprice(proot, PRICED)
        cls.pwaste = analyze_session(find_session(synth.WASTE, proot))
        cls.plean = analyze_session(find_session(synth.LEAN, proot))

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()


class TestHeadlines(MetricsCase):
    def test_unpriced_session_kpis(self):
        doc = report.render_session(self.waste, fixes.session_fixes(self.waste))
        assert_self_contained(self, doc)
        t = self.waste["totals"]["all"]
        self.assertEqual(t["unpriced_calls"], 7)
        kp = re.search(r'<div class="kpis">.*?</div><p class="muted small cnote">', doc, re.S).group(0)
        # headline first, then new / output / cache reads as separate numbers, all-in last and muted
        labels = re.findall(r'<div class="l">(?:<i [^>]*></i>)?([^<]+)</div>', kp)
        self.assertEqual(labels[:5], ["cost-weighted tokens", "API-equivalent", "new", "output", "cache reads"])
        self.assertEqual(labels[-1], "all-in")
        self.assertIn('<div class="kpi dim"', kp)
        self.assertIn("(7 calls unpriced: claude-test-1)", kp)
        self.assertIn(pricing.COST_WEIGHTED_FORMULA, doc)
        self.assertIn("platform.claude.com/docs/en/about-claude/pricing", doc)
        self.assertEqual(report.metrics(t)["new"], t["fresh_input"] + t["cache_write"])
        self.assertIn("API-equivalent $ unknown", report.headline_text(t))

    def test_priced_session(self):
        t = self.pwaste["totals"]["all"]
        self.assertEqual(t["unpriced_calls"], 0)
        p = pricing.PRICES[PRICED]
        want = (t["fresh_input"] * p[0] + t["cache_write"] * p[1] + t["cache_read"] * p[3] + t["output"] * p[4]) / 1e6
        self.assertAlmostEqual(t["api_cost_usd"], want, places=6)
        doc = report.render_session(self.pwaste)
        assert_self_contained(self, doc)
        self.assertIn('<span class="n">%s</span>' % report.fmt_usd(t["api_cost_usd"]), doc)
        self.assertNotIn("unpriced", re.search(r'<div class="kpis">.*?cnote', doc, re.S).group(0))
        self.assertTrue(report.headline_text(t).startswith(
            "%s cost-weighted tokens, %s API-equivalent; new " % (report.fmt_tok(t["cost_weighted"]),
                                                                 report.fmt_usd(t["api_cost_usd"]))))

    def test_compare_headlines(self):
        d = compare.compare_data(self.pwaste, self.plean)
        self.assertRegex(d["headline"], r"^B used \d+% fewer cost-weighted tokens \([^)]+\), API-equivalent "
                                        r"\$[\d.]+ -> \$[\d.]+; new .*\(all-in ")
        self.assertNotIn("unpriced", d["headline"])
        rows = [r["key"] for r in d["rows"]]
        self.assertEqual(rows[:6], ["cost_weighted", "api_cost_usd", "new", "output", "cache_read", "all_in"])
        self.assertIn(pricing.COST_WEIGHTED_FORMULA, compare.to_text(d))
        doc = compare.render_html(d)
        assert_self_contained(self, doc)
        self.assertIn('class="kpi head"', doc)
        self.assertIn('class="kpi dim"', doc)

    def test_sum_totals_robust(self):
        t = report._sum_totals([self.waste, self.pwaste])
        self.assertEqual(t["unpriced_models"], ["claude-test-1"])
        self.assertEqual(t["unpriced_calls"], 7)
        self.assertAlmostEqual(t["api_cost_usd"], self.pwaste["totals"]["all"]["api_cost_usd"])
        self.assertAlmostEqual(t["cost_weighted"], 2 * self.waste["totals"]["all"]["cost_weighted"], places=1)


class TestFixSavings(MetricsCase):
    def test_cost_weighted_and_usd(self):
        fs = {f["rule"]: f for f in fixes.session_fixes(self.pwaste)}
        big = fs["large_file_read"]
        self.assertEqual((big["savings_new_tokens"], big["savings_all_in_tokens"]), (15000, 90000))
        self.assertEqual(big["savings_cost_weighted_tokens"], 15000 * 1.25 + 75000 * 0.1)
        p = pricing.PRICES[PRICED]
        self.assertAlmostEqual(big["savings_usd"], (15000 * p[1] + 75000 * p[3]) / 1e6, places=5)
        miss = fs["cache_misses"]  # all-in 0 < new: the re-write would have been a cache read
        self.assertAlmostEqual(miss["savings_cost_weighted_tokens"], 44405 * (1.25 - 0.1), places=0)
        self.assertAlmostEqual(miss["savings_usd"], 44405 * (p[1] - p[3]) / 1e6, places=5)
        txt = fixes.to_text(list(fs.values()), "fixes")
        self.assertIn("~15,000 new tokens, ~26,250 cost-weighted ($0.08 API-equivalent)", txt)
        self.assertIn("all-in incl. carry: ~90,000", txt)
        self.assertIn("sorted by cost-weighted savings", txt)
        card = report.fix_card(big)
        self.assertIn("cost-weighted", card)
        self.assertIn("$0.08", card)
        self.assertIn("all-in incl. carry ~90.0k", card)

    def test_unpriced_fix(self):
        f = next(f for f in fixes.session_fixes(self.waste) if f["rule"] == "large_file_read")
        self.assertIsNone(f["savings_usd"])
        self.assertEqual(f["savings_model"], "claude-test-1")
        self.assertIn("($ unknown: claude-test-1 unpriced)", fixes.savings_headline(f))
        self.assertIn("$ unknown (unpriced model)", report.fix_card(f))

    def test_sorted_by_cost_weighted(self):
        fs = fixes.session_fixes(self.pwaste)
        keys = [(f["savings_cost_weighted_tokens"], f["savings_new_tokens"]) for f in fs]
        self.assertEqual(keys, sorted(keys, reverse=True))

    def test_aggregate_partial_usd(self):
        per = {self.pwaste["session_id"] + "p": fixes.session_fixes(self.pwaste),
               self.lean["session_id"]: fixes.session_fixes(self.lean)}
        agg = fixes.aggregate(per, [self.pwaste, self.lean])
        mcp = next(f for f in agg if f["id"] == "unused_mcp:bigsrv")
        parts = [next(f for f in fs if f["id"] == "unused_mcp:bigsrv") for fs in per.values()]
        self.assertAlmostEqual(mcp["savings_cost_weighted_tokens"],
                               sum(p["savings_cost_weighted_tokens"] for p in parts), places=1)
        self.assertAlmostEqual(mcp["savings_usd"], parts[0]["savings_usd"])  # only the priced session
        self.assertEqual(mcp["savings_usd_partial"], 1)
        self.assertIn("≥$", fixes.savings_headline(mcp))
        self.assertIn("1 unpriced session not counted", fixes.savings_headline(mcp))


class TestCrossExtras(MetricsCase):
    def test_sources_and_workflows(self):
        a, b = copy.deepcopy(self.waste), copy.deepcopy(self.lean)
        a["request_sources"] = {"human": 2, "hook": 1}
        b["request_sources"] = {"human": 1, "notification": 4}
        b["workflows"] = [{"id": "w1", "launched": 3, "started": 3, "results": 2, "failed": 1, "phases": {},
                           "agents": []}]
        doc = report.render_cross([a, b])
        assert_self_contained(self, doc)
        self.assertIn("<b>Request sources</b> human <span class=\"n\">3</span> &middot; notification "
                      "<span class=\"n\">4</span> &middot; hook <span class=\"n\">1</span>", doc)
        self.assertIn("<b>Workflows</b> <span class=\"n\">1</span> &middot; started agents <span class=\"n\">3</span>"
                      " &middot; failed <span class=\"n\">1</span>", doc)
        b["workflows"] = []
        self.assertNotIn("<b>Workflows</b>", report.render_cross([a, b]))

    def test_cross_headline(self):
        doc = report.render_cross([self.pwaste, self.lean])
        kp = re.search(r'<div class="kpis">.*?cnote', doc, re.S).group(0)
        self.assertIn("cost-weighted tokens", kp)
        self.assertIn("(2 calls unpriced: claude-test-1)", kp)


def _fake_analysis(n, miss_at=None, sub_every=0):
    calls = []
    for i in range(n):
        calls.append({"index": i, "usage": {"input": 1, "cache_write": 100 + i % 7, "cache_read": 1000 + 10 * i,
                                            "output": 20}, "context": 1101 + 10 * i, "attribution": [],
                      "timestamp": None, "gap_seconds": None, "tool_uses": [], "tool_results": [],
                      "cache_miss": {"reason": "ttl"} if i == miss_at else None, "compaction": None})
    return {"calls": calls, "subagents": []}


class TestScale(unittest.TestCase):
    def test_bucketed_timeline(self):
        ents = report.entries(_fake_analysis(1000, miss_at=500))
        svg = report.timeline_svg(ents)
        bars = svg.count('<g class="bar">')
        self.assertLessEqual(bars, report.MAX_TIMELINE_BARS)
        self.assertGreater(bars, 100)
        self.assertIn("1,000 calls grouped into %d bars" % bars, svg)
        self.assertIn("calls #0–#2 (3 calls)", svg)
        self.assertIn("cache miss", svg)  # marker kept for the bucket containing it
        b = report.bucket_entries(ents)
        self.assertEqual(sum(e["u"]["cache_read"] for e in b), sum(e["u"]["cache_read"] for e in ents))
        self.assertEqual(max(e["context"] for e in b), max(e["context"] for e in ents))
        small = report.entries(_fake_analysis(50))
        self.assertEqual(report.timeline_svg(small).count('<g class="bar">'), 50)
        self.assertNotIn("grouped into", report.timeline_svg(small))

    def test_caps(self):
        ents = report.entries(_fake_analysis(10))
        for e in ents:
            e["spike"] = True
        old = report.SPIKES_CAP
        report.SPIKES_CAP = 3
        try:
            html = report.spikes_section(ents)
        finally:
            report.SPIKES_CAP = old
        self.assertEqual(html.count('<div class="spike">'), 3)
        self.assertIn("+7 more spikes", html)

    def test_fix_cards_cap(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        a = analyze_session(find_session(synth.WASTE, synth.make(tmp.name)))
        fs = fixes.session_fixes(a)
        old = report.FIX_CARDS_CAP
        report.FIX_CARDS_CAP = 2
        try:
            doc = report.render_session(a, fs)
        finally:
            report.FIX_CARDS_CAP = old
        self.assertEqual(doc.count('<div class="fix sev-'), 2)
        self.assertIn("+%d more fixes" % (len(fs) - 2), doc)
        for f in fs[2:]:
            self.assertIn('id="%s"' % report.fix_anchor(f["id"]), doc)  # anchors kept for goal links


if __name__ == "__main__":
    unittest.main()
