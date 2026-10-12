"""Cross-session suggestions (tokenscope/xindex.py): signature normalisation, similarity, the evidence gates, skill
matching, stub drafts, the index cache and the live recap payload."""
import json
import os
import shutil
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tokenscope import xindex as xi  # noqa: E402
from tokenscope.live import recap as rc  # noqa: E402
from tokenscope.live.state import LiveState  # noqa: E402
from tokenscope.live.tail import TrackedFile  # noqa: E402
from tests.test_recap import PASS, SID, Sess, bash, edit, read  # noqa: E402


def resolve(shown, cache):
    """The file a shown stub path ("~/..." or "<cache>/...") names."""
    return os.path.join(cache, shown[len("<cache>/"):]) if shown.startswith("<cache>/") else os.path.expanduser(shown)


def step(i, cat, label, files=(), cmds=(), cw=20_000, source="rule", tools=None, skills=(), runners=(),
         prompt=False):
    return {"i": i, "cat": cat, "label": label, "source": source, "files": list(files), "cmds": list(cmds),
            "cw": cw, "usd": cw / 1e6, "tools": tools or {"Read": 1}, "skills": list(skills), "ts0": 1000.0 + i,
            "ts1": 1001.0 + i, "prompt_before": prompt, "runners": list(runners), "tested": "pass" if runners else None}


def workflow(sid, cw_read=40_000, skills=(), files=("cli.py", "README.md"), extra=(), cw_rest=20_000):
    """A request: read cli.py + README.md, edit them, run the unit tests (one record)."""
    steps = [step(0, "investigate", "reading code · cli.py", files[:1] + tuple(extra), cw=cw_read, prompt=True,
                  skills=skills),
             step(1, "investigate", "reading docs · README.md", files[1:], cw=cw_read / 2),
             step(2, "change", "editing files · cli.py, README.md", files, tools={"Edit": 3}, cw=cw_rest),
             step(3, "verify", "running tests · unittest", (), ("unittest",), tools={"Bash unittest": 1}, cw=cw_rest,
                  runners=("Bash unittest",))]
    return xi.requests_of(steps, [], sid, "proj")[0]


class Normalise(unittest.TestCase):
    def test_tokens_drop_stopwords_generic_words_identifiers_and_numbers(self):
        self.assertEqual(xi.tokens("Add the per-warehouse quantities to Item 3"), ["warehouse", "quantity", "item"])
        self.assertEqual(xi.tokens("editing files · store.py, cli.py"), [])
        self.assertEqual(xi.tokens("Run store_tests in tests/test_x.py for fooBar v2"), [])
        self.assertEqual(xi.tokens("Fix CLI subcommand parsing"), ["cli", "subcommand", "pars"])

    def test_rule_and_overhead_labels_have_no_words_of_their_own(self):
        self.assertEqual(xi.label_tokens({"label": "running a build/command · git status", "source": "rule"}), [])
        self.assertEqual(xi.label_tokens({"label": "Load notion-update-page tool", "cat": "overhead",
                                          "source": "rule"}), [])
        self.assertEqual(xi.label_tokens({"label": "Write release notes", "source": "todo"}), ["write", "release", "note"])

    def test_signature(self):
        r = workflow("s1")
        self.assertEqual(r["cats"], ["investigate", "change", "verify"])
        self.assertEqual(r["tools"], {"Read": 2, "Edit": 3, "Bash unittest": 1})
        self.assertEqual(r["exts"], [".md", ".py"])
        self.assertEqual(r["files"], ["cli.py", "README.md"])
        self.assertEqual(r["cw"], 100_000)
        self.assertEqual(r["cw_cat"], {"investigate": 60_000, "change": 20_000, "verify": 20_000})

    def test_device_files_and_task_output_are_not_project_files(self):
        r = workflow("s1", extra=("null", "b4x.output"))
        self.assertEqual(r["files"], ["cli.py", "README.md"])

    def test_directories_are_not_files(self):
        r = workflow("s1", extra=("hist-1", "src", "Makefile", ".gitignore", "LICENSE"))
        self.assertEqual(r["files"], ["cli.py", "Makefile", ".gitignore", "LICENSE", "README.md"])
        self.assertEqual(r["steps"][0]["files"], ["cli.py", "Makefile", ".gitignore", "LICENSE"])

    def test_requests_split_at_prompts_and_groups_indexed(self):
        steps = [step(0, "investigate", "a", ("a.py",), prompt=True), step(1, "change", "b", ("a.py",)),
                 step(2, "investigate", "c", ("b.py",), prompt=True), step(3, "change", "d", ("b.py",))]
        g = [{"label": "Implement warehouse transfer", "first": 2, "last": 3}]
        recs = xi.requests_of(steps, g, "s", "p")
        self.assertEqual([r["level"] for r in recs], ["request", "request", "group"])
        self.assertEqual(recs[2]["tokens"], ["implement", "transfer", "warehouse"])
        self.assertEqual([r["level"] for r in xi.current_records(steps, g)], ["request", "group"])


SUBCMD = ("Add a new `%s` subcommand to the stockroom CLI: %s. Add unit tests for it, document it in the README usage "
          "section, add an Unreleased CHANGELOG entry, and run the full test suite until green. Do not commit.")
TASKS = {"count": "print the number of catalog items and total units on hand",
         "export-csv": "write the catalog (sku, name, quantity, price) as CSV to stdout",
         "search": "find items whose sku or name contains a case-insensitive term",
         "top": "list the N items with the highest stock value (quantity x price), default N=5"}


def prompted(sid, prompt, tdd=False, files=("cli.py", "test_cli.py", "README.md", "CHANGELOG.md")):
    """A request started by `prompt`: read, edit, test once; or (tdd) a /tdd run: skill step, then test / edit
    cycles: a different step shape for the same task."""
    rd = lambda i, fs, cw=30_000: step(i, "investigate", "reading code · " + ", ".join(fs), fs, cw=cw)
    ed = lambda i, fs: step(i, "change", "editing files · " + ", ".join(fs), fs, tools={"Edit": 2}, cw=10_000)
    rn = lambda i: step(i, "verify", "running tests · unittest", (), ("unittest",), tools={"Bash unittest": 1},
                        cw=8_000, runners=("Bash unittest",))
    if tdd:
        steps = [step(0, "delegate", "Use skill tdd", (), tools={"✦ tdd": 1}, skills=("tdd",), cw=8_000),
                 rd(1, files[:2]), ed(2, files[1:2]), rn(3), ed(4, files[:1]), rn(5), rd(6, files[2:], 20_000),
                 ed(7, files[2:]), rn(8)]
    else:
        steps = [rd(0, files[:1], 40_000), rd(1, files[1:], 20_000), ed(2, files), rn(3)]
    steps[0]["prompt_before"] = True
    return xi.requests_of(steps, [], sid, "-workspace-stockroom", [(0, xi.prompt_words(prompt))])[0]


class Prompts(unittest.TestCase):
    def test_prompt_words_drop_code_paths_numbers(self):
        w = xi.prompt_words("Add `top` to src/stockroom/cli.py and reports.top_value(), N=5. See https://x.io/a\n"
                            "```\nprint(1)\n```\nDon't ask")
        self.assertEqual(w, ["add", "to", "and", "n", "|", "see", "|", "don", "ask"])
        self.assertEqual(xi.tokens(" ".join(w)), ["see"])

    def test_request_words_do_not_depend_on_the_step_labeller(self):
        """Model step labels (--haiku) carry words rule labels never have: a prompted request ignores both, so it
        matches the indexed (rule-labelled) runs the same either way."""
        pw = [(0, xi.prompt_words(SUBCMD % ("top", TASKS["top"])))]
        rule = [step(0, "investigate", "reading code · cli.py", ("cli.py",), prompt=True),
                step(1, "change", "editing files · cli.py", ("cli.py",), tools={"Edit": 1})]
        model = [dict(x, label=lab, source="haiku") for x, lab in zip(rule, ["Inspect ranking helper",
                                                                           "Implement stable tie break"])]
        a, b = (xi.requests_of(st, [], "s", "p", pw)[0] for st in (rule, model))
        self.assertEqual(b["tokens"], a["tokens"])
        self.assertNotIn("tie", b["tokens"])

    def test_request_words_come_from_the_prompt_not_a_skill_step(self):
        r = prompted("s", SUBCMD % ("top", TASKS["top"]), tdd=True)
        self.assertNotIn("tdd", r["tokens"])
        self.assertTrue({"subcommand", "cli", "changelog", "readme", "unit"} <= set(r["tokens"]))
        self.assertEqual(r["label"], "reading code · cli.py, test_cli.py")   # not "Use skill tdd"
        self.assertNotIn("delegate", r["cats"])
        self.assertEqual(xi.label_tokens({"label": "Use skill tdd", "source": "rule", "cat": "delegate"}), [])

    def test_same_task_by_prompt_matches_across_step_shapes(self):
        cur = prompted("now", SUBCMD % ("top", TASKS["top"]), tdd=True)
        hist = [prompted("p%d" % k, SUBCMD % (n, TASKS[n])) for k, n in enumerate(("count", "export-csv", "search"))]
        for r in hist:
            self.assertTrue(xi.is_match(cur, r)[0])
        h = xi.suggest([cur], hist, write=False)
        self.assertEqual(h["rule"], "repeat")
        self.assertEqual(h["action"], 'Make "Add CLI subcommand with tests, README and CHANGELOG" a skill')
        self.assertEqual(h["est_saving_cw"], 60_000)
        self.assertNotIn("tdd", h["seq"])

    def test_unrelated_prompts_on_the_same_files_do_not_match(self):
        a = prompted("a", SUBCMD % ("count", TASKS["count"]))
        b = prompted("b", "The price column renders with too many decimals; round money to cents everywhere and "
                          "keep the quantity alignment.")
        self.assertEqual(a["files"], b["files"])
        self.assertFalse(xi.is_match(a, b)[0])
        self.assertIsNone(xi.suggest([b], [a, prompted("c", SUBCMD % ("search", TASKS["search"])),
                                           prompted("d", SUBCMD % ("top", TASKS["top"]))], write=False))

    def test_prompt_without_work_is_not_the_request_prompt(self):
        steps = [step(0, "investigate", "reading code · cli.py", ("cli.py",), prompt=True)]
        r = xi.requests_of(steps, [], "s", "p", [(0, xi.prompt_words("thanks")),
                                                  (0, xi.prompt_words("Rename the warehouse option"))])[0]
        self.assertEqual(r["tokens"], ["option", "rename", "warehouse"])

    def test_skill_the_request_used_is_not_suggested(self):
        tmp = tempfile.mkdtemp()
        try:
            os.makedirs(os.path.join(tmp, "tdd"))
            with open(os.path.join(tmp, "tdd", "SKILL.md"), "w", encoding="utf-8") as f:
                f.write("---\nname: tdd\ndescription: Add a CLI subcommand test first: unit tests, README, "
                        "CHANGELOG\n---\n")
            skills = xi.load_skills([tmp])
            cur = prompted("now", SUBCMD % ("top", TASKS["top"]), tdd=True)
            hist = [prompted("p%d" % k, SUBCMD % (n, TASKS[n])) for k, n in enumerate(("count", "export-csv", "search"))]
            h = xi.suggest([cur], hist, skills, write=False)
            self.assertEqual((h["rule"], h["skill"]), ("repeat", None))
            hist_tdd = [dict(r, skills=["tdd"]) for r in hist]
            h = xi.suggest([dict(cur, skills=[])], hist_tdd, skills, used_skills=("tdd",), write=False)
            self.assertNotEqual(h["rule"], "existing_skill")
        finally:
            shutil.rmtree(tmp)


class Similarity(unittest.TestCase):
    def test_same_workflow_matches_a_different_one_does_not(self):
        a, b = workflow("s1"), workflow("s2")
        ok, sc = xi.is_match(a, b)
        self.assertTrue(ok)
        self.assertGreater(sc, 0.9)
        other = xi.requests_of([step(0, "research", "researching the web", ("notes.md",), prompt=True,
                                     tools={"WebSearch": 4})], [], "s3", "p")[0]
        self.assertFalse(xi.is_match(a, other)[0])

    def test_one_shared_file_is_no_anchor(self):
        a = workflow("s1")
        b = workflow("s2", files=("cli.py", "setup.cfg"))
        self.assertFalse(xi.is_match(a, b)[0])

    def test_idf_weighs_rare_files(self):
        fo_plain, _ = xi.file_overlap(["README.md", "a.py"], ["README.md", "b.py"])
        fo_idf, _ = xi.file_overlap(["README.md", "a.py"], ["README.md", "b.py"], {"README.md": 0.1, "a.py": 3,
                                                                                      "b.py": 3})
        self.assertAlmostEqual(fo_plain, 0.5)
        self.assertLess(fo_idf, 0.1)

    def test_shared_label_words_anchor(self):
        steps = lambda sid: xi.requests_of([step(0, "change", "Write release notes", ("CHANGES.md",), source="todo",
                                                 prompt=True, cw=60_000)], [], sid, "p")[0]
        self.assertTrue(xi.is_match(steps("a"), steps("b"))[0])


class Gates(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def sug(self, cur, hist, **kw):
        return xi.suggest([cur], hist, cache=self.tmp, **kw)

    def test_two_prior_no_hint_three_prior_hint(self):
        cur = workflow("now")
        self.assertIsNone(self.sug(cur, [workflow("p1"), workflow("p2")]))
        h = self.sug(cur, [workflow("p1"), workflow("p2"), workflow("p2")])
        self.assertIsNotNone(h)
        self.assertEqual(h["kind"], "cross_session")
        self.assertEqual(h["rule"], "repeat")
        self.assertEqual(h["action"], 'Make "change cli.py + README.md and test" a skill')
        self.assertTrue(h["evidence"].startswith("≈60k/run saving · done 3× in 2 sessions · ≈100k CW each · "))
        self.assertIn("read cli.py, README.md → edit cli.py, README.md → run unittest", h["evidence"])
        self.assertEqual(h["est_saving_cw"], 60_000)   # the investigate steps on the shared files, not the whole run
        self.assertTrue(h["estimate"].startswith("estimate: median CW"))
        self.assertEqual(len(h["matches"]), 3)

    def test_one_session_is_not_enough(self):
        self.assertIsNone(self.sug(workflow("now"), [workflow("p1")] * 4))

    def test_same_session_repeats_do_not_count(self):
        self.assertIsNone(self.sug(workflow("now"), [workflow("now"), workflow("now"), workflow("p1"),
                                                     workflow("p2")]))
        self.assertIsNotNone(self.sug(workflow("now"), [workflow("now"), workflow("p3"), workflow("p1"),
                                                        workflow("p2")]))

    def test_low_cw_no_hint(self):
        small = lambda sid: workflow(sid, cw_read=24_000, cw_rest=5_000)   # 46k CW per run, 36k of it locating
        self.assertIsNone(self.sug(workflow("now"), [small("p1"), small("p2"), small("p3")]))
        self.assertIsNone(self.sug(small("now"), [workflow("p1"), workflow("p2"), workflow("p3")]))

    def test_no_rediscovery_no_hint(self):
        """Runs that do not spend on locating the shared files: nothing a skill would save."""
        def quick(sid):
            steps = [step(0, "change", "editing files · cli.py, README.md", ("cli.py", "README.md"), cw=60_000,
                          tools={"Edit": 3}, prompt=True),
                     step(1, "verify", "running tests", (), ("unittest",), tools={"Bash unittest": 1},
                          runners=("Bash unittest",))]
            return xi.requests_of(steps, [], sid, "p")[0]
        self.assertIsNone(self.sug(quick("now"), [quick("p1"), quick("p2"), quick("p3")]))

    def test_stub_written_under_the_cache_dir_only(self):
        h = self.sug(workflow("now"), [workflow("p1"), workflow("p2"), workflow("p3")])
        path = os.path.join(self.tmp, "skill-drafts", "change-cli-py-readme-md-and-test", "SKILL.md")
        self.assertEqual(resolve(h["stub_path"], self.tmp), path)
        self.assertFalse(os.path.isabs(h["stub_path"]))
        with open(path, encoding="utf-8") as f:
            text = f.read()
        self.assertTrue(text.startswith("---\nname: change-cli-py-readme-md-and-test\ndescription: "))
        self.assertIn("- cli.py (in 3 of 3 runs)", text)
        self.assertIn("## Steps\n\n1. read cli.py, README.md\n2. edit cli.py\n3. update README.md\n4. run unittest\n",
                      text)
        self.assertIn("unittest", text)
        self.assertNotIn(os.path.expanduser("~") + os.sep, text.replace(self.tmp, ""))

    def test_stub_path_never_absolute(self):
        home = os.path.join(self.tmp, "home", "me")
        cache = os.path.join(self.tmp, "srv", "cache")
        under = os.path.join(home, ".cache", "tokenscope", "skill-drafts", "x", "SKILL.md")
        self.assertEqual(xi.shown_path(under, cache, home=home), "~/.cache/tokenscope/skill-drafts/x/SKILL.md")
        outside = os.path.join(cache, "skill-drafts", "add-cli", "SKILL.md")
        self.assertEqual(xi.shown_path(outside, cache, home=home), "<cache>/skill-drafts/add-cli/SKILL.md")
        # not under the cache dir either (a moved cache): still named relative to it, never absolute
        self.assertEqual(xi.shown_path(os.path.join(self.tmp, "elsewhere", "add-cli", "SKILL.md"), cache, home=home),
                         "<cache>/skill-drafts/add-cli/SKILL.md")
        self.assertIsNone(xi.shown_path(None, cache))

    def test_slug_keeps_whole_words(self):
        name = "Add CLI subcommand with tests, README and CHANGELOG"
        sl = xi.slug(name)
        self.assertEqual(sl, "add-cli-subcommand-with-tests-readme-and")
        self.assertLessEqual(len(sl), 40)
        words = set(name.lower().replace(",", "").split())
        self.assertTrue(all(w in words for w in sl.split("-")))
        self.assertEqual(xi.slug("change cli.py + README.md and test"), "change-cli-py-readme-md-and-test")
        self.assertEqual(xi.slug("x" * 60), "x" * 40)
        self.assertEqual(xi.slug("!!"), "workflow")

    def test_draft_steps_are_the_runs_common_step_sequence(self):
        def run(sid, extra=None):
            steps = [step(0, "investigate", "reading code", ("cli.py", "models.py", "store.py"), prompt=True,
                          cw=40_000),
                     step(1, "change", "editing files", ("cli.py", "test_cli.py"), tools={"Edit": 2}),
                     step(2, "change", "editing docs", ("README.md", "CHANGELOG.md"), tools={"Edit": 2}),
                     step(3, "verify", "running tests", (), ("unittest",), tools={"Bash unittest": 1},
                          runners=("Bash unittest",))]
            if extra:
                steps.insert(1, step(1, "investigate", "reading more", (extra,)))
                for k, x in enumerate(steps):
                    x["i"] = k
            return xi.requests_of(steps, [], sid, "proj")[0]
        runs = [run("p1"), run("p2", "util.py"), run("p3")]
        self.assertEqual(xi.common_steps(runs), ["read cli.py, models.py, store.py", "edit cli.py + tests",
                                                 "update README.md, CHANGELOG.md", "run unittest"])
        h = self.sug(run("now"), runs)
        with open(resolve(h["stub_path"], self.tmp), encoding="utf-8") as f:
            text = f.read()
        steps = text.split("## Steps\n\n")[1].split("\n\n")[0].splitlines()
        self.assertEqual(steps, ["1. read cli.py, models.py, store.py", "2. edit cli.py + tests",
                                 "3. update README.md, CHANGELOG.md", "4. run unittest"])

    def test_evidence_leads_with_the_numbers(self):
        h = self.sug(workflow("now"), [workflow("p1"), workflow("p2"), workflow("p3")])
        self.assertRegex(h["evidence"], r"^≈\d+k/run saving · done \d+× in \d+ sessions · ≈\d+k CW each · ")

    def test_no_stub_when_asked_not_to_write(self):
        h = xi.suggest([workflow("now")], [workflow("p1"), workflow("p2"), workflow("p3")], write=False,
                       cache=self.tmp)
        self.assertIsNone(h["stub_path"])
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "skill-drafts")))

    def test_skill_used_every_time_gets_a_file_map_hint(self):
        hist = [workflow(s, skills=("cli-change",)) for s in ("p1", "p2", "p3")]
        h = self.sug(workflow("now", skills=("cli-change",)), hist)
        self.assertEqual(h["rule"], "skill_filemap")
        self.assertEqual(h["skill"], "cli-change")
        self.assertEqual(h["action"], "Add a file map to skill /cli-change (cli.py, README.md)")
        self.assertIsNone(h["stub_path"])


class Skills(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        for name, desc in (("cli-docs", "Change a CLI subcommand, update the README and run the unittest suite"),
                           ("release-notes", "Write release notes from the git log")):
            os.makedirs(os.path.join(self.tmp, "skills", name))
            with open(os.path.join(self.tmp, "skills", name, "SKILL.md"), "w", encoding="utf-8") as f:
                f.write("---\nname: %s\ndescription: %s\n---\n\n# %s\n\nSteps.\n" % (name, desc, name))
        self.skills = xi.load_skills([os.path.join(self.tmp, "skills")])

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def test_load(self):
        self.assertEqual([s["name"] for s in self.skills], ["cli-docs", "release-notes"])
        self.assertEqual(self.skills[1]["description"], "Write release notes from the git log")

    def test_skill_dirs_include_project_and_extra(self):
        dirs = xi.skill_dirs("/some/project", ["extra"])
        self.assertEqual(dirs[0], os.path.join(os.path.expanduser("~"), ".claude", "skills"))
        self.assertEqual(dirs[1], os.path.join("/some/project", ".claude", "skills"))
        self.assertEqual(dirs[2], os.path.abspath("extra"))

    def test_match_positive_and_negative(self):
        words = xi.workflow_words([workflow("a")]) | {"subcommand"}
        best = xi.match_skill(words, self.skills)
        self.assertEqual(best[0]["name"], "cli-docs")
        self.assertIsNone(xi.match_skill(words, self.skills, used={"cli-docs"}))
        self.assertIsNone(xi.match_skill({"warehouse", "stock", "transfer"}, self.skills))
        self.assertIsNone(xi.match_skill({"readme", "cli"}, self.skills))   # 2 shared words: below the bar

    def test_repeated_workflow_matching_an_unused_skill_says_use_it(self):
        def run(sid):
            r = workflow(sid)
            r["tokens"] = ["subcommand"]
            return r
        h = xi.suggest([run("now")], [run("p1"), run("p2"), run("p3")], self.skills, write=False, cache=self.tmp)
        self.assertEqual(h["rule"], "existing_skill")
        self.assertEqual(h["action"], "Use skill /cli-docs for this")
        self.assertTrue(h["evidence"].startswith("≈60k/run saving · done 3× without it in 3 sessions · ≈100k CW each · "))
        self.assertIn('you have skill cli-docs ("Change a CLI subcommand', h["evidence"])
        self.assertEqual(h["skill"], "cli-docs")


def write_session(root, sid, sess):
    d = os.path.join(root, "-repo")
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, sid + ".jsonl"), "w", encoding="utf-8") as f:
        for r in sess.recs:
            f.write(json.dumps(dict(r, sessionId=sid)) + "\n")


def cli_work(prompt="update the cli"):
    return (Sess().prompt(prompt).turn(read("cli.py"), cw=30_000).turn(read("README.md"), cw=25_000)
            .turn(edit("cli.py"), edit("README.md"), cw=10_000).turn(bash("python3 -m pytest -q"), out=PASS)
            .answer())


class IndexAndLive(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.root = os.path.join(self.tmp, "projects")
        for k in range(3):
            write_session(self.root, "%08d-0000-0000-0000-000000000000" % k, cli_work())

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def test_index_builds_then_rebuilds_from_cache(self):
        x = xi.XIndex(self.root, cache=self.tmp)
        st = x.build(jobs=1)
        self.assertEqual((st["sessions"], st["replayed"]), (3, 3))
        rec = x.sessions["00000000-0000-0000-0000-000000000000"]["records"][0]
        self.assertEqual(rec["files"], ["cli.py", "README.md"])
        self.assertEqual(rec["cats"], ["investigate", "change", "verify"])
        self.assertEqual(xi.XIndex(self.root, cache=self.tmp).build(jobs=1)["replayed"], 0)
        write_session(self.root, "00000009-0000-0000-0000-000000000000", cli_work())
        self.assertEqual(xi.XIndex(self.root, cache=self.tmp).build(jobs=1)["replayed"], 1)
        self.assertEqual(len(x.records(exclude_sid="00000000-0000-0000-0000-000000000000")), 2)

    def test_index_root_from_env(self):
        old = os.environ.get("TOKENSCOPE_INDEX_ROOT")
        os.environ["TOKENSCOPE_INDEX_ROOT"] = self.root
        try:
            self.assertEqual(xi.index_root(), self.root)
            self.assertEqual(xi.index_root(self.tmp), self.tmp)
        finally:
            if old is None:
                os.environ.pop("TOKENSCOPE_INDEX_ROOT")
            else:
                os.environ["TOKENSCOPE_INDEX_ROOT"] = old

    def test_live_recap_payload_carries_the_cross_session_hint(self):
        sug = xi.Suggester(self.root, cache=self.tmp)
        sug.refresh()
        state, s, _ = cli_work().state()
        state.xhints = sug
        r = state.session_compact(s)["recap"]
        cross = [h for h in r["hints"] if h["kind"] == "cross_session"]
        self.assertEqual(len(cross), 1)
        h = cross[0]
        for k in ("action", "evidence", "est_saving_cw", "skill", "stub_path", "matches"):
            self.assertIn(k, h)
        self.assertEqual(h["action"], 'Make "Update CLI with tests and README" a skill')   # from the prompts
        self.assertEqual(h["est_saving_cw"], 55_000)
        self.assertTrue(os.path.isfile(resolve(h["stub_path"], self.tmp)))
        self.assertTrue(resolve(h["stub_path"], self.tmp).startswith(os.path.join(self.tmp, "skill-drafts")))
        self.assertFalse(os.path.isabs(h["stub_path"]))

    def _model_state(self, sug):
        """cli_work() in a state that waits for model step labels (`tokenscope live --haiku`)."""
        state, _, _ = cli_work().state()
        mstate = LiveState(redact=False, clock=state.clock, model_labels=True)
        s = mstate.ingest(TrackedFile("/x/-repo/%s.jsonl" % SID, SID, "-repo"), cli_work().recs)
        mstate.xhints = sug
        return mstate, s

    def test_live_hint_is_the_same_with_model_step_labels(self):
        """Regression: model labels add words the indexed (rule-labelled) runs never have; a request's words come
        from its prompt only, so --haiku gets the same hint as without it."""
        sug = xi.Suggester(self.root, cache=self.tmp)
        sug.refresh()
        state, s, _ = cli_work().state()
        state.xhints = sug
        want = [h for h in state.recap(s)["hints"] if h["kind"] == "cross_session"]
        self.assertEqual(len(want), 1)
        mstate, ms = self._model_state(sug)
        steps, _ = mstate.steps(ms)
        mstate.add_step_labels(ms.id, [x["i"] for x in steps], {
            "labels": {x["i"]: lab for x, lab in zip(steps, ["Inspect the CLI argument parser module",
                                                            "Review the README usage documentation",
                                                            "Implement option handling and document behaviour",
                                                            "Verify everything passes with pytest"])},
            "groups": [{"label": "Extend command line interface", "first": 0, "last": len(steps) - 1}]})
        self.assertEqual({x["source"] for x in mstate.steps(ms)[0]}, {"haiku"})
        got = [h for h in mstate.recap(ms)["hints"] if h["kind"] == "cross_session"]
        self.assertEqual([h["action"] for h in got], [h["action"] for h in want])
        self.assertEqual(got[0]["matches"], want[0]["matches"])   # same runs, same scores
        self.assertEqual(got[0]["est_saving_cw"], want[0]["est_saving_cw"])

    def test_live_recap_reevaluates_when_model_labels_or_the_index_arrive(self):
        calls = []

        class Hints:
            version = 0

            def __call__(self, *a, **k):
                calls.append([x["label"] for x in a[3]])
                return None

        h = Hints()
        mstate, ms = self._model_state(h)
        mstate.recap(ms)
        mstate.recap(ms)
        self.assertEqual(len(calls), 1)   # cached while nothing changes
        mstate.add_step_labels(ms.id, [0], {"labels": {0: "Inspect the CLI parser"}})
        mstate.recap(ms)
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[-1][0], "Inspect the CLI parser")
        h.version += 1   # the index (re)built
        mstate.recap(ms)
        self.assertEqual(len(calls), 3)

    def test_live_excludes_its_own_session(self):
        sug = xi.Suggester(self.root, cache=self.tmp)
        sug.refresh()
        state, s, _ = cli_work().state()
        s.id = "00000000-0000-0000-0000-000000000000"   # the current session is one of the three indexed
        state.xhints = sug
        self.assertEqual([h for h in state.recap(s)["hints"] if h["kind"] == "cross_session"], [])

    def test_with_cross_keeps_one_and_ranks(self):
        sess = [{"kind": "session", "cw_at_stake": c, "rule": str(c)} for c in (90, 80, 70)]
        h = {"kind": "cross_session", "cw_at_stake": 85}
        self.assertEqual([x["cw_at_stake"] for x in rc.with_cross(sess, h)], [90, 85, 80])
        low = dict(h, cw_at_stake=1)
        self.assertEqual(rc.with_cross(sess, low)[-1], low)   # always kept: replaces the weakest session hint
        self.assertEqual(sum(x["kind"] == "cross_session" for x in rc.with_cross(sess + [h], low)), 1)


if __name__ == "__main__":
    unittest.main()
