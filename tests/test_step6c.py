"""Step 6c: test-runner step kinds, context scaled to measured, fixes tied to goals, set compare, overlay fixes."""
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
from test_hooks import CLAUDE_MD, HOOK_CMD, HOOK_TEXT, API_MD  # noqa: E402
from test_understand import LATER, NODE_PASS  # noqa: E402
from tokenscope import cli, compare, fixes, report  # noqa: E402
from tokenscope import understand as un  # noqa: E402
from tokenscope.analyze import analyze_session  # noqa: E402
from tokenscope.parse import find_session  # noqa: E402

E1 = "e1e1e1e1-0000-0000-0000-000000000001"   # A: Edit + Write (both hooked), ls dump
E2 = "e2e2e2e2-0000-0000-0000-000000000002"   # A: same prompt, same CLAUDE.md
F1 = "f1f1f1f1-0000-0000-0000-000000000001"   # B: same prompt, quiet
F2 = "f2f2f2f2-0000-0000-0000-000000000002"   # B: a different prompt for the same goal
PROMPT = "Add a SAVE10 discount code and tests"
SETTINGS = {"hooks": {"PostToolUse": [{"matcher": "Edit|Write|MultiEdit", "hooks": [
    {"type": "command", "command": HOOK_CMD}]}]}}


def _hook(w, sec, mid, name):
    w.att(sec, {"type": "hook_additional_context", "hookName": name, "toolUseID": "tu_" + mid,
                "hookEvent": "PostToolUse", "content": [HOOK_TEXT], "command": HOOK_CMD})


def make_noisy(root, cwd, sid, start):
    """First call 30,005 (instructions + skills listing); ls dump; Edit + hook; Write + hook; tests pass."""
    w = synth._W(sid)
    w.prompt(start, PROMPT)
    w.att(start, {"type": "instructions", "files": [
        {"path": cwd + "/CLAUDE.md", "type": "Project", "content": CLAUDE_MD},
        {"path": cwd + "/docs/api.md", "type": "Project", "content": API_MD}]})
    w.att(start, {"type": "skill_listing", "content": synth.SKILLS, "skillCount": 3})
    w.call(start + 1, "%s0" % sid[:2], (5, 30000, 0, 50), ("Bash", {"command": "ls -R"}))
    w.result(start + 2, "%s0" % sid[:2], "src/a.js\n" * 1500)
    w.call(start + 3, "%s1" % sid[:2], (0, 3500, 30005, 50), ("Edit", {"file_path": cwd + "/src/a.js"}))
    w.result(start + 4, "%s1" % sid[:2], "ok")
    _hook(w, start + 4, "%s1" % sid[:2], "PostToolUse:Edit")
    w.call(start + 5, "%s2" % sid[:2], (0, 3100, 33505, 50), ("Write", {"file_path": cwd + "/src/b.js"}))
    w.result(start + 6, "%s2" % sid[:2], "ok")
    _hook(w, start + 6, "%s2" % sid[:2], "PostToolUse:Write")
    w.call(start + 7, "%s3" % sid[:2], (0, 3100, 36605, 50), ("Bash", {"command": "cat src/*.js; ./run_tests.sh | tail"}))
    w.result(start + 8, "%s3" % sid[:2], NODE_PASS)
    w.call(start + 9, "%s4" % sid[:2], (0, 100, 39705, 60), text="Done.")
    w.lines[-1]["message"]["stop_reason"] = "end_turn"
    for x in w.lines:
        x["cwd"] = cwd
    w.write(root, "-repo")


def make_quiet(root, cwd, sid, start, prompt=PROMPT):
    w = synth._W(sid)
    w.prompt(start, prompt)
    w.call(start + 1, "%s0" % sid[:2], (5, 12000, 0, 50), ("Edit", {"file_path": cwd + "/src/a.js"}))
    w.result(start + 2, "%s0" % sid[:2], "ok")
    w.call(start + 3, "%s1" % sid[:2], (0, 200, 12005, 40), text="Done.")
    w.lines[-1]["message"]["stop_reason"] = "end_turn"
    for x in w.lines:
        x["cwd"] = cwd
    w.write(root, "-repo")


class Case(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.root = os.path.join(cls.tmp.name, "projects")
        cls.cwd = os.path.join(cls.tmp.name, "repo")
        os.makedirs(os.path.join(cls.cwd, ".claude"))
        with open(os.path.join(cls.cwd, ".claude", "settings.json"), "w") as fh:
            json.dump(SETTINGS, fh)
        make_noisy(cls.root, cls.cwd, E1, 0)
        make_noisy(cls.root, cls.cwd, E2, 100)
        make_quiet(cls.root, cls.cwd, F1, 200)
        make_quiet(cls.root, cls.cwd, F2, 300, prompt="Add a WELCOME5 discount code")
        cls.s = {sid: find_session(sid, cls.root) for sid in (E1, E2, F1, F2)}
        cls.a = {sid: analyze_session(s) for sid, s in cls.s.items()}

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()


class TestStepKinds(unittest.TestCase):
    def test_runner_in_compound_command(self):
        for cmd, want in [("cat src/*.js; ./run_tests.sh | grep -E 'pass|fail'", "./run_tests.sh"),
                          ("cd app && npx jest --ci 2>&1 | tail -20", "jest"),
                          ("ls && python -m pytest -q", "pytest"), ("npm run test", "npm test"),
                          ("echo hi | go test ./...", "go test"), ("make test", "make test"),
                          ("node --test test/", "node --test"), ("bash run_tests.sh", "./run_tests.sh"),
                          ("ls -la && cat a.js", None), ("./build.sh", None)]:
            self.assertEqual(un.test_runner(cmd), want, cmd)


class TestStepsAndContext(Case):
    def test_kind_only_when_tests_parsed(self):
        reqs = un.understand_session(self.s[E1], self.a[E1], now=LATER)["requests"]
        kinds = [st["kind"] for st in reqs[0]["steps"]]
        self.assertEqual(kinds[-1], "Bash(./run_tests.sh)")  # cat ...; ./run_tests.sh, tests parsed
        self.assertEqual(kinds[0], "Bash(ls)")
        self.assertEqual(reqs[0]["steps"][-1]["detail"], "cat src/*.js; ./run_tests.sh | tail")  # full command kept

    def test_first_call_scaled_to_measured(self):
        ctx = un.understand_session(self.s[E1], self.a[E1], now=LATER)["context"]
        c0 = ctx["main"][0]
        self.assertEqual(c0["sources"]["other"], 0)  # the unattributed rest went into the estimated parts
        self.assertAlmostEqual(sum(c0["sources"].values()), c0["context"], delta=3)
        self.assertIn("instructions", c0["scaled_to_measured"])
        self.assertIn("skills", c0["scaled_to_measured"])
        raw = sum(x["tokens"] for x in self.a[E1]["calls"][0]["attribution"]
                  if x["cause"] == "system_overhead" and x["label"].startswith("CLAUDE.md"))
        self.assertGreater(sum(ctx["instruction_files"].values()), raw)  # per-file numbers scaled too
        # later calls: hook output / tool results absorb the residual
        for c in ctx["main"]:
            self.assertLessEqual(c["sources"]["other"], 2)

    def test_no_estimate_keeps_other(self):
        c = {"attribution": [{"cause": "system_overhead", "label": "tool schemas / other (unattributed)",
                              "tokens": 900}]}
        items, scaled = un._scaled_items(c, 0)
        self.assertFalse(scaled)
        self.assertEqual(items[0]["tokens"], 900)


class TestFixGoals(Case):
    def setUp(self):
        an = [self.a[E1], self.a[E2]]
        self.fx = fixes.aggregate({a["session_id"]: fixes.session_fixes(a) for a in an}, an)
        self.u = un.understand([self.s[E1], self.s[E2]], now=LATER, analyses={a["session_id"]: a for a in an})
        un.apply_fixes(self.u, self.fx)
        self.by = {f["id"]: f for f in self.fx}

    def test_hook_fix_lands_in_goal(self):
        hook = self.by["noisy_hook:PostToolUse:Edit"]
        self.assertEqual(len(hook["goals"]), 1)
        e = hook["goals"][0]
        g = self.u["goals"][0]
        self.assertEqual((e["goal"], e["goal_id"]), (g["goal"], g["id"]))
        self.assertEqual(e["runs_affected"], 2)
        self.assertGreater(e["tokens_in_goal"], 4000)
        self.assertAlmostEqual(e["share_of_goal_tokens"], e["tokens_in_goal"] / (2 * g["new"]["median"]),
                               places=3)
        line = fixes.goal_line(hook)
        self.assertRegex(line, r'^All of this lands in goal "[^"]+": hook output = \d+% of its new tokens '
                               r'\(2 runs\)$')

    def test_listing_counts_where_written(self):
        # E1/E2 first calls are pure cache writes -> the whole listing is new
        self.assertEqual(fixes.written_fraction(self.a[E1]), 1.0)
        a = json.loads(json.dumps(self.a[E1], default=str))
        a["calls"][0]["usage"].update(cache_write=0, input=5)  # identical prefix read from cache
        self.assertLess(fixes.written_fraction(a), 0.01)

    def test_costly_rule_and_top_fix(self):
        g = self.u["goals"][0]
        self.assertGreater(g["fix_share"], 0.30)
        self.assertEqual(g["status"], "Costly")
        self.assertIn("new tokens", g["status_reason"])
        self.assertTrue(g["top_fix"]["saves_per_run"] > 0)
        doc = report.render_cross(list(self.a[x] for x in (E1, E2)), cross_fixes=self.fx, understand=self.u)
        self.assertIn("WHAT TO CHANGE</span> <a href=\"#fix-", doc)
        self.assertIn('class="fxi"', doc)
        self.assertIn("Fix shares use new tokens", doc)

    def test_nothing_to_change(self):
        u = un.understand([self.s[F1]], now=LATER, analyses={F1: self.a[F1]})
        un.apply_fixes(u, fixes.session_fixes(self.a[F1]))
        from tokenscope import understand_html as uh
        self.assertIsNone(u["goals"][0]["top_fix"])
        self.assertIn("Nothing to change yet.", uh.fix_line(u["goals"][0]))

    def test_cli_fixes_json_has_goals(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            self.assertEqual(cli.main(["fixes", "--root", self.root, "--session", E1[:8], "--json"]), 0)
        out = json.loads(buf.getvalue())
        self.assertTrue(all("goals" in f for f in out))


class TestListingBash(Case):
    def test_listing_wording(self):
        fx = next(f for f in fixes.session_fixes(self.a[E1]) if f["rule"] == "large_bash_output")
        self.assertEqual(fx["target"], "ls")
        self.assertIn("git ls-files | head -100", fx["action_code"])
        self.assertIn("Glob", fx["action_code"])
        self.assertNotIn("tail -40", fx["action_code"])
        self.assertNotIn("FAIL", fx["config"]["claude_md"][0])
        self.assertIn("git ls-files | head -100", fx["config"]["claude_md"][0])


class TestOverlay(Case):
    def test_dedupe_header_and_matcher(self):
        with tempfile.TemporaryDirectory() as d:
            with redirect_stdout(io.StringIO()):
                rc = cli.main(["fixes", "--root", self.root, "--project", "repo", "--since", "2000-01-01",
                               "--emit-config", d, "--overlay"])
            self.assertEqual(rc, 0)
            with open(os.path.join(d, "TOKENSCOPE_CHANGES.md"), encoding="utf-8") as fh:
                ch = fh.read()
            with open(os.path.join(d, ".claude", "settings.json"), encoding="utf-8") as fh:
                st = json.load(fh)
        self.assertIn("from 4 sessions in project repo, 2026-10-02 09:00 – ", ch)
        self.assertEqual(ch.count("Replaced the eager import `@docs/api.md`"), 1)
        self.assertEqual(ch.count("Moved `## Changelog`"), 1)
        self.assertIn("(summed over 2 sessions)", ch)
        self.assertNotIn("is not in the repo's", ch)
        self.assertIn("covers PostToolUse:Edit, PostToolUse:Write", ch)
        cmd = st["hooks"]["PostToolUse"][0]["hooks"][0]["command"]
        self.assertEqual(cmd.count("tokenscope-cap.py"), 1)

    def test_matcher_regex(self):
        self.assertTrue(fixes.matcher_covers("Edit|Write|MultiEdit", "Write"))
        self.assertFalse(fixes.matcher_covers("Edit", "Write"))
        self.assertTrue(fixes.matcher_covers("", "Write"))
        self.assertTrue(fixes.matcher_covers("*", "Bash"))
        self.assertTrue(fixes.matcher_covers("mcp__.*", "mcp__x__y"))
        self.assertTrue(fixes.matcher_covers("Edit|(", "Edit"))  # invalid regex: plain alternatives

    def test_already_wrapped(self):
        a = dict(self.a[E1])
        with tempfile.TemporaryDirectory() as repo:
            os.makedirs(os.path.join(repo, ".claude"))
            wrapped = json.loads(json.dumps(SETTINGS))
            wrapped["hooks"]["PostToolUse"][0]["hooks"][0]["command"] = fixes.wrap_hook_command(HOOK_CMD)
            with open(os.path.join(repo, ".claude", "settings.json"), "w") as fh:
                json.dump(wrapped, fh)
            a["cwd"] = repo
            files, changes, manual = fixes.build_overlay(fixes.session_fixes(a), a)
        self.assertNotIn(".claude/settings.json", files)
        self.assertTrue(any("already runs through tokenscope-cap.py" in c[1] for c in changes))
        self.assertFalse(any("is not in the repo" in m[0] for m in manual))


class TestCompareSets(Case):
    def test_compare_sets(self):
        d = compare.compare_sets([self.s[E1], self.s[E2]], [self.s[F1], self.s[F2]], now=LATER)
        # discovered on A + B together: the same SAVE10 prompt lands in one goal on both sides; WELCOME5 is a
        # different discount code, so a different goal, and it only has runs in B
        self.assertEqual(len(d["goals"]), 2)
        g, other = d["goals"]
        self.assertEqual((g["a"]["runs"], g["b"]["runs"]), (2, 1))
        self.assertTrue(g["same_prompts"])
        self.assertEqual((other["a"]["runs"], other["b"]["runs"]), (0, 1))
        self.assertEqual(other["only_b"], ["Add a WELCOME5 discount code"])
        self.assertEqual([(c["goal_id"], c["side"], c["runs"]) for c in d["checks"]], [(other["id"], "B", 1)])
        self.assertIn("has members only in B (1 run)", d["checks"][0]["text"])
        a_all = sorted(self.a[x]["totals"]["all"]["all_in"] for x in (E1, E2))
        self.assertEqual(g["a"]["all_in"]["median"], sum(a_all) / 2)
        self.assertLess(g["delta_pct"]["all_in"], 0)
        self.assertEqual(g["a"]["hook_fires_per_run"], 2.0)
        self.assertEqual(g["b"]["hook_fires_per_run"], 0.0)
        self.assertRegex(d["headline"], r"^B: −\d+% median cost-weighted per run across 1 goal, same prompts, "
                                        r"finish rate A 100% -> B 100% \(1 goal only on one side\)$")
        self.assertIn("noisy_hook:PostToolUse:Edit", {f["id"] for f in d["fixes_resolved"]})
        self.assertGreater(d["context"]["a"]["hook_output"], 0)
        self.assertEqual(d["context"]["b"].get("hook_output", 0), 0)
        txt = compare.sets_to_text(d)
        self.assertIn("PROMPTS DIFFER in %s %s" % (other["id"], other["goal"]), txt)
        self.assertIn("WARNING: goal %s" % other["id"], txt)
        self.assertIn("hooks/run 2.0 -> 0.0", txt)

    def test_same_prompts(self):
        d = compare.compare_sets([self.s[E1]], [self.s[F1]], now=LATER)
        self.assertTrue(d["goals"][0]["same_prompts"])
        self.assertIn(", same prompts, ", d["headline"])

    def test_cli_sets_html(self):
        with tempfile.TemporaryDirectory() as d:
            ids = os.path.join(d, "a.txt")
            with open(ids, "w") as fh:
                fh.write("# before\n%s\n%s\n" % (E1, E2[:8]))
            out = os.path.join(d, "c.html")
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = cli.main(["compare", "--root", self.root, "--a-sessions", ids, "--b-sessions",
                               "%s,%s" % (F1[:8], F2[:8]), "-o", out, "--report-link", "r.html"])
            self.assertEqual(rc, 0)
            self.assertIn("median cost-weighted per run across 1 goal", buf.getvalue())
            with open(out, encoding="utf-8") as fh:
                doc = fh.read()
        for s in ('<a href="r.html#understand">1 Understand</a>', '<a href="#compare" class="on">4 Compare</a>',
                  "Per goal", "⚠ B only", "Paths per goal", "Context by source", "no longer trigger in B"):
            self.assertIn(s, doc)
        self.assertNotIn("<script src", doc)

    def test_cli_needs_both_sets(self):
        with redirect_stdout(io.StringIO()):
            self.assertEqual(cli.main(["compare", "--root", self.root, "--a-sessions", E1[:8]]), 2)
            self.assertEqual(cli.main(["compare", "--root", self.root]), 2)


if __name__ == "__main__":
    unittest.main()
