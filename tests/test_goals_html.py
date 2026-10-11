"""Step 7b: Goals in the HTML: area header rows, evidence card, stable anchors, fixes-by-goal matrix, compare per
goal (areas, one-side-only, path diff), deduped Token hygiene lines."""
import os
import re
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from test_step6c import E1, E2, F1, F2, Case  # noqa: E402
from test_understand import LATER  # noqa: E402
from tokenscope import compare, fixes, report  # noqa: E402
from tokenscope import understand as un  # noqa: E402
from tokenscope import understand_html as uh  # noqa: E402
from tokenscope.report import esc  # noqa: E402


class GoalsCase(Case):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        an = [cls.a[x] for x in (E1, E2, F1, F2)]
        cls.fx = fixes.aggregate({a["session_id"]: fixes.session_fixes(a) for a in an}, an)
        cls.u = un.understand([cls.s[x] for x in (E1, E2, F1, F2)], now=LATER,
                              analyses={a["session_id"]: a for a in an})
        un.apply_fixes(cls.u, cls.fx)
        cls.doc = report.render_cross(an, cross_fixes=cls.fx, understand=cls.u)


class TestGoalsOverview(GoalsCase):
    def test_area_header_rows(self):
        d = self.doc
        areas = uh.area_order(self.u)
        self.assertEqual(d.count('<div class="ar-row"'), len(areas))
        for a, gs in areas:
            row = re.search(r'<div class="ar-row" id="area-%s">(.*?)</div>' % re.escape(a["id"]), d).group(1)
            self.assertIn(esc(a["name"]), row)
            self.assertIn("%d goal" % len(gs), row)
            self.assertIn("%d request" % sum(g["runs"] for g in gs), row)
            for f in a["shared_files"][:6]:
                self.assertIn(esc(f["path"]), row)
        # goal rows follow their area header
        first_area, gs = areas[0]
        self.assertLess(d.index('id="area-%s"' % first_area["id"]),
                        d.index('id="%s"' % uh.goal_anchor(gs[0]["goal"], gs[0]["request_ids"])))
        n = len(self.u["goals"])
        self.assertIn("%d GOAL%s IN %d AREA" % (n, "S" if n != 1 else "", len(areas)), d)
        self.assertIn("DISCOVERED FROM 4 REQUESTS", d)
        self.assertIn("<b>0</b> requests no goal handles", d)
        self.assertIn("How goals are discovered", d)
        self.assertIn("<b class=\"mono\">%.2f</b>" % self.u["goal_method"]["goal_threshold"], d)
        self.assertIn("The outcome is not a feature", d)

    def test_summary_is_goal_based(self):
        p = uh.summary_paragraph(self.u, per_request=False)
        self.assertRegex(p, r"^4 requests in 4 sessions → %d goals? in \d areas?; " % len(self.u["goals"]))

    def test_stable_anchors(self):
        g = self.u["goals"][0]
        a = uh.goal_anchor(g["goal"], g["request_ids"])
        self.assertRegex(a, r"^goal-[a-z0-9-]+-[0-9a-f]{6}$")
        self.assertEqual(a, uh.goal_anchor(g["goal"], list(reversed(g["request_ids"]))))  # order-independent
        self.assertNotEqual(a, uh.goal_anchor(g["goal"], g["request_ids"][:-1] or ["x"]))
        self.assertIn('<details class="wf" id="%s"' % a, self.doc)
        self.assertNotRegex(self.doc, r'id="wf-\d+"')
        # the same goal gets the same anchor when discovered again
        u2 = un.understand([self.s[x] for x in (E1, E2, F1, F2)], now=LATER, analyses=self.a)
        self.assertEqual({uh.goal_anchor(x["goal"], x["request_ids"]) for x in u2["goals"]},
                         {uh.goal_anchor(x["goal"], x["request_ids"]) for x in self.u["goals"]})

    def test_evidence_card(self):
        g = next(g for g in self.u["goals"] if g["runs"] > 1)
        by_id = {r["id"]: r for s in self.u["sessions"] for r in s["requests"]}
        card = uh.evidence_card(g, by_id, {})
        self.assertIn("WHY THESE ARE ONE GOAL", card)
        # identical prompts are folded with a count and the outcome mix
        prompts = {}
        for rid in g["request_ids"]:
            prompts.setdefault(" ".join(by_id[rid]["prompt"].split()), []).append(rid)
        top = max(prompts.values(), key=len)
        self.assertIn('<span class="xn">×%d</span>' % len(top), card)
        self.assertEqual(card.count('<span class="xn">'), len(prompts))
        self.assertIn('class="oc oc-ok"', card)
        for f in g["files_edited"]:
            self.assertIn("%s <b>×%d</b>" % (esc(f["path"]), f["requests"]), card)
        self.assertIn("WHY GROUPED", card)
        for x in g["why"]:
            self.assertIn(esc(x["label"]), card)
        self.assertIn("cohesion <b class=\"mono\">%.2f</b>" % g["cohesion"], card)
        if g["nearest"]:
            self.assertIn("nearest other goal:", card)
            self.assertIn("(similarity <b class=\"mono\">%.2f</b>)" % g["nearest"]["similarity"], card)
        # long prompts are cut at MEMBERS_SHOWN
        long = dict(by_id[g["request_ids"][0]], prompt="word " * 200)
        card = uh.evidence_card(dict(g, request_ids=[long["id"]], why=[], cohesion=None, nearest=None),
                                {long["id"]: long}, {})
        self.assertIn("…”", card)
        self.assertIn("A goal of one request", card)

    def test_context_legend_uses_goal_codes(self):
        codes = uh.goal_codes(self.u)
        self.assertEqual(sorted(codes.values(), key=lambda c: int(c[1:])),
                         ["G%d" % i for i in range(1, len(self.u["goals"]) + 1)])
        units, _ = uh.context_units(self.u, per_request=False)
        self.assertTrue(all(re.search(r" G\d+$", lab) for lab, _, _ in units))
        self.assertIn('<div class="gkey">', self.doc)
        g = self.u["goals"][0]
        self.assertIn('<b class="mono">%s</b> %s' % (codes[g["id"]], esc(g["goal"])), self.doc)


class TestFixesByGoal(GoalsCase):
    def test_matrix(self):
        m = uh.fixes_by_goal(self.u, self.fx)
        self.assertIn("FIXES BY GOAL", m)
        cols = [f for f in self.fx if f.get("goals")][:6]
        self.assertEqual(m.count('<th class="fxc"'), len(cols))
        hook = next(f for f in self.fx if f["id"] == "noisy_hook:PostToolUse:Edit")
        e = hook["goals"][0]
        self.assertIn("%d%%</span></td>" % round(100 * e["share_of_goal_tokens"]), m)
        self.assertIn('href="#%s"' % report.fix_anchor(hook["id"]), m)
        self.assertIn('id="%s"' % report.fix_anchor(hook["id"]), self.doc)
        # rows sorted by the goal's new tokens; blank cells for goals a fix does not touch
        new = uh.goal_new_tokens(self.u)
        order = re.findall(r'<td class="fxg"><a href="#([^"]+)"', m)
        anchors = {uh.goal_anchor(g["goal"], g["request_ids"]): g["id"] for g in self.u["goals"]}
        vals = [new[anchors[a]] for a in order]
        self.assertEqual(vals, sorted(vals, reverse=True))
        if len(self.u["goals"]) > 1:
            self.assertIn('<td class="fxv"></td>', m)
        self.assertLess(self.doc.index("FIXES BY GOAL"), self.doc.index('<div class="fixes">'))
        self.assertEqual(uh.fixes_by_goal(self.u, ["just text"]), "")

    def test_goal_line_says_goal(self):
        hook = next(f for f in self.fx if f["id"] == "noisy_hook:PostToolUse:Edit")
        self.assertRegex(fixes.goal_line(hook), r'lands in goal "[^"]+": ')


class TestCompareGoals(GoalsCase):
    def test_per_goal_table(self):
        d = compare.compare_sets([self.s[E1], self.s[E2]], [self.s[F1], self.s[F2]], now=LATER)
        html = compare.render_sets_html(d)
        t = compare._goal_table(d)
        groups = compare._area_groups(d)
        self.assertEqual(t.count('<tr class="arow">'), len(groups))
        for a, gs in groups:
            self.assertIn(esc(a["name"]), t)
        g, other = d["goals"]
        self.assertIn('id="%s"' % compare._anchor(g, "cmp-"), t)
        # one-side-only: tagged in its row, linked to the warning, only that side's numbers
        self.assertIn('⚠ B only', t)
        self.assertIn('href="#%s"' % compare._anchor(other, "pd-"), t)
        self.assertIn('id="%s"' % compare._anchor(other, "pd-"), html)
        self.assertIn('<tr class="one">', t)
        self.assertIn("⚠ one side only", html)
        self.assertRegex(html, r"\d GOALS? IN \d AREAS? · DISCOVERED FROM 4 REQUESTS \(A ∪ B\)")
        self.assertIn(compare._pct_txt(g["delta_pct"]["all_in"]), t)

    def test_path_diff(self):
        d = compare.compare_sets([self.s[E1], self.s[E2]], [self.s[F1], self.s[F2]], now=LATER)
        g = d["goals"][0]
        ps, more = compare.path_diff(g)
        self.assertEqual(sum(p["a"] for p in ps), g["a"]["runs"])
        self.assertEqual(sum(p["b"] for p in ps), g["b"]["runs"])
        self.assertEqual(more, 0)
        for p in ps:
            if p["a"] and not p["b"]:
                self.assertEqual(p["status"], "gone in B")
            if p["b"] and not p["a"]:
                self.assertEqual(p["status"], "new in B")
        t = compare._chain_table(d)
        self.assertIn("hooks/run", t)
        self.assertIn(esc(ps[0]["chain"])[:40], t)
        if any(p["status"] == "gone in B" for p in ps):
            self.assertIn("gone in B", t)


class TestHygieneDedupe(unittest.TestCase):
    def test_listing_lines_merge(self):
        ls, git = fixes.listing_line(["ls"]), fixes.listing_line(["git ls-files"])
        self.assertIn("`git ls-files | head -100`", ls)
        # bare git ls-files is the dump: suggest a scoped, capped listing instead of itself
        self.assertIn("`git ls-files <dir> | head -100`", git)
        self.assertNotIn("`git ls-files | head -100`", git)
        out = fixes.hygiene_lines(["- a", ls, "- a", git, "- b"])
        lines = [x for x, _ in out]
        self.assertEqual(lines[0], "- a")
        self.assertEqual(lines[2], "- b")
        self.assertEqual(len(lines), 3)
        self.assertEqual(sum("To see the repo layout" in x for x in lines), 1)
        self.assertIn("`ls` or a bare `git ls-files`", lines[1])
        self.assertIn("<dir>", lines[1])
        self.assertEqual(out[1][1], [ls, git])

    def test_overlay_writes_one_layout_line(self):
        ls, git = fixes.listing_line(["ls"]), fixes.listing_line(["git ls-files"])
        fxs = [{"id": "large_bash_output:%s" % c, "rule": "large_bash_output", "target": c,
                "config": {"claude_md": [ln]}} for c, ln in (("ls", ls), ("git ls-files", git))]
        files, changes, _ = fixes.build_overlay(fxs, {"cwd": "", "instructions": {"files": []}})
        md = files["CLAUDE.md"]
        self.assertEqual(md.count("To see the repo layout"), 1)
        self.assertEqual(md.count("## Token hygiene"), 1)
        self.assertNotIn("with `git ls-files`.", md)
        ch = [c for c in changes if c[0] == "CLAUDE.md"][0]
        self.assertIn("1 line", ch[1])
        self.assertEqual(ch[2], "large_bash_output:git ls-files, large_bash_output:ls")


if __name__ == "__main__":
    unittest.main()
