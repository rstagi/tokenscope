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
from tests.test_live import LiveFixture  # noqa: E402
from tests.test_recap import PASS, SID, Sess, bash, edit, grep, read  # noqa: E402


def resolve(shown, cache):
    """The file a shown stub path ("~/..." or "<cache>/...") names."""
    return os.path.join(cache, shown[len("<cache>/"):]) if shown.startswith("<cache>/") else os.path.expanduser(shown)


def step(i, cat, label, files=(), cmds=(), cw=20_000, source="rule", tools=None, skills=(), runners=(),
         prompt=False, disc=None, edits=None, turns=1):
    """A step; `disc`: [(kind, path, cw)] or [(kind, path, cw, names its output showed)] discovery calls in it
    (default: an investigate step's files read in full, its CW split over them), `edits`: paths it edits (default: a
    change step's files)."""
    if disc is None:
        disc = [("read", f, cw / len(files)) for f in files] if cat == "investigate" and files else []
    if edits is None:
        edits = list(files) if cat == "change" else []
    return {"i": i, "cat": cat, "label": label, "source": source, "files": list(files), "cmds": list(cmds),
            "cw": cw, "usd": cw / 1e6, "tools": tools or {"Read": 1}, "skills": list(skills), "ts0": 1000.0 + i,
            "ts1": 1001.0 + i, "prompt_before": prompt, "runners": list(runners), "tested": "pass" if runners else None,
            "turns": turns, "disc": [{"kind": d[0], "path": d[1], "shown": d[1], "cw": d[2], "k": i,
                                      "names": list(d[3]) if len(d) > 3 else []} for d in disc],
            "edits": [{"path": p, "k": i} for p in edits]}


def workflow(sid, cw_read=40_000, skills=(), files=("cli.py", "README.md"), extra=(), cw_rest=20_000, turns=1,
             project="proj", found=("models.py",)):
    """A request: search, read cli.py + the `found` files (models.py) + README.md, edit cli.py and README.md, run the
    unit tests (one record). Discovery: the search (its output names the found files) and the reads of the found
    files (cw_read); the other reads are of files it edits."""
    steps = [step(0, "investigate", "reading code · cli.py", files[:1] + tuple(found) + tuple(extra), cw=cw_read,
                  prompt=True, skills=skills,
                  disc=[("search", "", cw_read / 2, found)] + [("read", f, cw_read / 4 / len(found)) for f in found]
                  + [("read", files[0], cw_read / 4)], turns=turns),
             step(1, "investigate", "reading docs · README.md", files[1:], cw=cw_read / 2, turns=turns),
             step(2, "change", "editing files · cli.py, README.md", files, tools={"Edit": 3}, cw=cw_rest, turns=turns),
             step(3, "verify", "running tests · unittest", (), ("unittest",), tools={"Bash unittest": 1}, cw=cw_rest,
                  runners=("Bash unittest",), turns=turns)]
    return xi.requests_of(steps, [], sid, project)[0]


def busy_project(project="proj", n=10, turns=60):
    """Other requests of the project (too small to match anything): the turns a CLAUDE.md line is paid on."""
    return [xi.requests_of([step(0, "change", "editing files · x.py", ("x.py",), cw=5_000, prompt=True,
                                 turns=turns)], [], "busy%d" % k, project)[0] for k in range(n)]


def net(disc, chars, turns=4):
    """Expected net of a skill whose draft body has `chars` characters, for runs of `turns` turns."""
    return disc - xi.context_cost(-(-chars // xi.CHARS_PER_TOKEN), turns)


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
        r = workflow("s1", found=())
        self.assertEqual(r["cats"], ["investigate", "change", "verify"])
        self.assertEqual(r["tools"], {"Read": 2, "Edit": 3, "Bash unittest": 1})
        self.assertEqual(r["exts"], [".md", ".py"])
        self.assertEqual(r["files"], ["cli.py", "README.md"])
        self.assertEqual(r["cw"], 100_000)
        self.assertEqual(r["cw_cat"], {"investigate": 60_000, "change": 20_000, "verify": 20_000})

    def test_device_files_and_task_output_are_not_project_files(self):
        r = workflow("s1", extra=("null", "b4x.output"), found=())
        self.assertEqual(r["files"], ["cli.py", "README.md"])

    def test_directories_are_not_files(self):
        r = workflow("s1", extra=("hist-1", "src", "Makefile", ".gitignore", "LICENSE"), found=())
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
    rd = lambda i, fs, cw=30_000: step(i, "investigate", "reading code · " + ", ".join(fs), fs, cw=cw,
                                       disc=[("search", "", cw, fs)])   # finding the files: all discovery
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
        h = xi.suggest([cur], hist + busy_project("-workspace-stockroom"), write=False)
        self.assertEqual(h["rule"], "repeat")
        self.assertEqual(h["action"], 'Make "Add CLI subcommand with tests, README and CHANGELOG" a skill')
        self.assertEqual(h["discovery_cw"], 60_000)   # the searches, not the whole run
        self.assertLess(h["est_saving_cw"], 60_000)
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
            h = xi.suggest([cur], hist + busy_project("-workspace-stockroom"), skills, write=False)
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
        a = workflow("s1", found=())
        b = workflow("s2", files=("cli.py", "setup.cfg"), found=())
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
        return xi.suggest([cur], hist + busy_project(), cache=self.tmp, **kw)

    def test_two_prior_no_hint_three_prior_hint(self):
        cur = workflow("now")
        self.assertIsNone(self.sug(cur, [workflow("p1"), workflow("p2")]))
        h = self.sug(cur, [workflow("p1"), workflow("p2"), workflow("p2")])
        self.assertIsNotNone(h)
        self.assertEqual(h["kind"], "cross_session")
        self.assertEqual(h["rule"], "repeat")
        self.assertEqual(h["action"], 'Make "change cli.py + README.md and test" a skill')
        want = net(30_000, len(xi.skill_body(xi.stub_text(xi.find_repeat(cur, [workflow("p1"), workflow("p2"),
                                                                                workflow("p2")]), h["action"][6:-9]))))
        self.assertEqual(h["est_saving_cw"], round(want))   # discovery (search + models.py), not the whole run
        self.assertEqual(h["discovery_cw"], 30_000)
        self.assertTrue(h["evidence"].startswith("≈30k discovery/run − ≈%d skill body → ≈%dk/run net · done 3× in 2 "
                                                 "sessions · ≈100k CW each · " % (h["cost_cw"], round(want / 1000))))
        self.assertIn("read cli.py, models.py, README.md → edit cli.py, README.md → run unittest", h["evidence"])
        self.assertTrue(h["estimate"].startswith("estimate: discovery = median CW"))
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

    def test_no_discovery_says_skill_for_consistency(self):
        """Runs that do not spend on finding their way: nothing a skill would save, but the same steps every
        time: a skill for consistency, said so."""
        def quick(sid):
            steps = [step(0, "change", "editing files · cli.py, README.md", ("cli.py", "README.md"), cw=60_000,
                          tools={"Edit": 3}, prompt=True),
                     step(1, "verify", "running tests", (), ("unittest",), tools={"Bash unittest": 1},
                          runners=("Bash unittest",))]
            return xi.requests_of(steps, [], sid, "p")[0]
        h = self.sug(quick("now"), [quick("p1"), quick("p2"), quick("p3")])
        self.assertEqual(h["rule"], "consistency")
        self.assertEqual(h["action"], 'Make "change cli.py + README.md and test" a skill for consistency')
        self.assertTrue(h["evidence"].startswith("same 3 steps in 3 runs; no token saving expected (≈0 discovery/run "
                                                 "− ≈%d skill body)" % h["cost_cw"]))
        self.assertEqual(h["cw_at_stake"], 0)
        self.assertTrue(os.path.isfile(resolve(h["stub_path"], self.tmp)))

    def test_stub_written_under_the_cache_dir_only(self):
        h = self.sug(workflow("now"), [workflow("p1"), workflow("p2"), workflow("p3")])
        path = os.path.join(self.tmp, "skill-drafts", "change-cli-py-readme-md-and-test", "SKILL.md")
        self.assertEqual(resolve(h["stub_path"], self.tmp), path)
        self.assertFalse(os.path.isabs(h["stub_path"]))
        with open(path, encoding="utf-8") as f:
            text = f.read()
        self.assertTrue(text.startswith("---\nname: change-cli-py-readme-md-and-test\ndescription: "))
        self.assertIn("- cli.py (in 3 of 3 runs)", text)
        self.assertIn("## Steps\n\n1. read cli.py, models.py, README.md\n2. edit cli.py\n3. update README.md\n4. run unittest\n",
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
        self.assertRegex(h["evidence"], r"^≈\d+k discovery/run − ≈\d+k? skill body → ≈\d+k/run net · done \d+× in "
                                        r"\d+ sessions · ≈\d+k CW each · ")

    def test_no_stub_when_asked_not_to_write(self):
        h = xi.suggest([workflow("now")], [workflow("p1"), workflow("p2"), workflow("p3")] + busy_project(),
                       write=False, cache=self.tmp)
        self.assertIsNone(h["stub_path"])
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "skill-drafts")))

    def test_skill_used_every_time_gets_a_file_map_hint(self):
        found = ("models.py", "store.py", "util.py")
        hist = [workflow(s, skills=("cli-change",), found=found) for s in ("p1", "p2", "p3")]
        h = self.sug(workflow("now", skills=("cli-change",), found=found), hist)
        self.assertEqual(h["rule"], "skill_filemap")
        self.assertEqual(h["skill"], "cli-change")
        # found, not edited
        self.assertEqual(h["action"], "Add a file map to skill /cli-change (models.py, store.py, util.py)")
        self.assertTrue(h["evidence"].startswith("≈30k discovery/run − ≈%d file map in the skill → ≈%dk/run net · "
                                                 "/cli-change in 3 of 3 runs" % (h["cost_cw"], round(h["net_cw"] / 1e3))))
        self.assertIsNone(h["stub_path"])


class Skills(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        for name, desc in (("cli-docs", "Change a CLI subcommand, update the README and run the unittest suite"),
                           ("release-notes", "Write release notes from the git log")):
            os.makedirs(os.path.join(self.tmp, "skills", name))
            with open(os.path.join(self.tmp, "skills", name, "SKILL.md"), "w", encoding="utf-8") as f:
                f.write("---\nname: %s\ndescription: %s\n---\n\n# %s\n\nSteps. Read `src/models.py` first.\n"
                        % (name, desc, name))
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
        body = self.skills[0]["body_tokens"]
        self.assertGreater(body, 0)
        self.assertEqual(h["cost_cw"], round(xi.context_cost(body, 4)))   # its body, every later turn of the run
        self.assertTrue(h["evidence"].startswith("≈30k discovery/run − ≈%d skill body → ≈30k/run net · done 3× "
                                                 "without it in 3 sessions · ≈100k CW each · " % h["cost_cw"]))
        self.assertIn('you have skill cli-docs ("Change a CLI subcommand', h["evidence"])
        self.assertEqual(h["skill"], "cli-docs")


def write_session(root, sid, sess):
    d = os.path.join(root, "-repo")
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, sid + ".jsonl"), "w", encoding="utf-8") as f:
        for r in sess.recs:
            f.write(json.dumps(dict(r, sessionId=sid)) + "\n")


def cli_work(prompt="update the cli"):
    return (Sess().prompt(prompt).turn(grep("def main"), cw=20_000, out="src/cli.py:9:def main\nsrc/models.py:3:x")
            .turn(read("cli.py"), cw=30_000).turn(read("models.py"), cw=15_000).turn(read("store.py"), cw=6_000)
            .turn(read("util.py"), cw=4_000).turn(read("README.md"), cw=25_000)
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
        self.assertEqual(rec["files"], ["cli.py", "models.py", "store.py", "util.py", "README.md"])
        self.assertEqual(rec["cats"], ["investigate", "change", "verify"])
        # discovery: the Grep (20k) and the full reads of models.py, store.py, util.py (25k); cli.py and README.md
        # are edited later
        self.assertEqual((rec["disc"], rec["disc_by"]), (45_000, {"search": 20_000, "read": 25_000}))
        self.assertEqual(rec["disc_files"], ["src/models.py", "src/store.py", "src/util.py"])   # relative to the project
        # the Grep's output named cli.py and models.py (what removable() checks)
        self.assertEqual(rec["disc_items"][0], ["search", 20_000, 0, ["cli.py", "models.py"]])
        self.assertEqual(rec["turns"], 8)
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
        # three short sessions are all the project has: a file map in CLAUDE.md costs least
        self.assertEqual(h["action"], "Add a file map to CLAUDE.md")
        self.assertEqual(h["discovery_cw"], 45_000)   # the grep that named models.py + the three reads
        self.assertEqual(h["est_saving_cw"], 45_000 - h["cost_cw"])
        self.assertEqual(h["stub_path"], "<cache>/claude-md-drafts/update-cli-with-tests-and-readme.md")
        with open(resolve(h["stub_path"], self.tmp), encoding="utf-8") as f:
            self.assertIn("- `src/models.py`: source", f.read())
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


def indexed(sess):
    """The request record of a synthetic session, indexed as XIndex does (discovery facts attached)."""
    state, s, _ = sess.state()
    steps, gs = state.steps(s, final=True)
    xi.attach_discovery(steps, *xi.call_facts(state, s))
    return xi.requests_of(steps, gs, s.id, "-repo", list(zip(s.prompt_at, s.prompt_words)))[0]


class Discovery(unittest.TestCase):
    def test_reads_of_files_edited_later_are_not_discovery(self):
        r = indexed(Sess().prompt("fix the totals").turn(read("store.py"), cw=30_000).turn(read("models.py"), cw=12_000)
                    .turn(edit("store.py"), cw=5_000).turn(read("store.py"), cw=7_000).answer())
        # store.py is edited: its reads are needed anyway (before the edit) or check it (after); models.py is only
        # read: discovery
        self.assertEqual(r["disc_by"], {"read": 12_000})
        self.assertEqual(r["disc_files"], ["src/models.py"])

    def test_glob_grep_listing_and_toolsearch_count(self):
        r = indexed(Sess().prompt("fix the totals")
                    .turn(("Glob", {"pattern": "**/*.py"}), grep("total"), cw=10_000)
                    .turn(bash("find . -name '*.py' | head"), cw=4_000).turn(bash("ls -la src"), cw=2_000)
                    .turn(bash("git status"), cw=9_000)
                    .turn(("ToolSearch", {"query": "select:mcp__x__y"}), cw=3_000)
                    .turn(read("store.py", offset=10, limit=20), cw=8_000)   # a partial read is not a full read
                    .turn(edit("store.py"), cw=5_000).answer())
        self.assertEqual(r["disc_by"], {"search": 16_000, "tools": 3_000})
        self.assertEqual(r["disc"], 19_000)

    def test_a_turn_cw_is_split_per_call_as_steps_split_it(self):
        r = indexed(Sess().prompt("fix the totals").turn(grep("total"), bash("python3 -m pytest -q"), cw=10_000,
                                                          out=PASS).answer())
        self.assertEqual(r["disc_by"], {"search": 5_000})

    def test_no_turn_facts_no_discovery(self):
        self.assertEqual(xi.requests_of([{"i": 0, "cat": "change", "label": "x", "files": ["a.py"], "cw": 60_000,
                                          "prompt_before": True}], [], "s", "p")[0]["disc"], 0)


class ExploreSubagent(LiveFixture):
    def test_exploration_subagent_is_discovery(self):
        s = self.build()
        disc, _ = xi.call_facts(self.state, s)
        sub = [d for d in disc if d["kind"] == "subagent"]
        self.assertEqual(len(sub), 1)
        agent = next(iter(s.agents.values()))
        self.assertGreater(agent["cw"], 0)
        self.assertGreater(sub[0]["cw"], agent["cw"])   # its CW plus the launching call's share
        self.assertEqual(len([d for d in disc if d["kind"] == "read"]), 3)   # cart.py, never edited


def runs_with(disc, turns, n=3, project="proj", found=("models.py",)):
    """`n` prior runs + the current one with discovery `disc` (a search naming the `found` files, then their reads)
    and `turns` main-thread turns each."""
    out = []
    for sid in ["p%d" % k for k in range(n)] + ["now"]:
        r = workflow(sid, project=project, found=found)
        r.update(disc=disc, turns=turns, disc_items=[["search", disc / 2, 0, list(found)]]
                 + [["read", disc / 2 / len(found), 0, f] for f in found])
        out.append(r)
    return out[-1], out[:-1]


class NetModel(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def body_tokens(self):
        cur, hist = runs_with(50_000, 10)
        rep = xi.find_repeat(cur, hist)
        return xi.est_tokens(xi.skill_body(xi.stub_text(rep, xi._name(hist + [cur], rep["files"]))))

    def opts(self, disc, turns, busy=True, found=("models.py",)):
        cur, hist = runs_with(disc, turns, found=found)
        hist = hist + (busy_project() if busy else [])
        rep = xi.find_repeat(cur, hist)
        return {o["rule"]: o for o in xi.remedies(rep, hist)}

    def test_skill_cost_scales_with_the_remaining_turns(self):
        tok = self.body_tokens()
        c10, c100 = self.opts(50_000, 10)["repeat"]["cost"], self.opts(50_000, 100)["repeat"]["cost"]
        self.assertAlmostEqual(c10, tok * (1.25 + 0.1 * 9))
        self.assertAlmostEqual(c100, tok * (1.25 + 0.1 * 99))
        self.assertGreater(c100, 5 * c10)

    def test_net_below_20k_gives_no_token_hint(self):
        tok = self.body_tokens()
        disc = 15_000 + xi.context_cost(tok, 10)   # net 15k, 100% of... no: > 30% of discovery, but < 20k
        cur, hist = runs_with(disc, 10)
        self.assertIsNone(xi.suggest([cur], hist + busy_project(), write=False))
        cur, hist = runs_with(disc + 6_000, 10)   # net 21k
        self.assertEqual(xi.suggest([cur], hist + busy_project(), write=False)["rule"], "repeat")

    def test_net_under_30_percent_of_discovery_gives_nothing(self):
        tok = self.body_tokens()
        turns = int(60_000 / (0.1 * tok))
        cost = xi.context_cost(tok, turns)
        self.assertGreater(cost, 50_000)
        cur, hist = runs_with(cost + 25_000, turns)   # net 25k >= 20k but < 30% of the discovery
        for r in hist + [cur]:
            r["disc_files"] = []   # searches only: no file map to offer instead
        o = self.opts(cost + 25_000, turns)["repeat"]
        self.assertFalse(o["ok"])
        self.assertIsNone(xi.suggest([cur], hist + busy_project(), write=False))   # nor a consistency hint: net > 0

    def test_file_map_preferred_when_it_nets_more(self):
        found = ("models.py", "store.py", "util.py")
        o = self.opts(50_000, 10, busy=False, found=found)   # the project is these runs only: a map costs next to nothing
        self.assertTrue(o["claude_md"]["stable"])
        self.assertGreater(o["claude_md"]["net"], o["repeat"]["net"])
        cur, hist = runs_with(50_000, 10, found=found)
        h = xi.suggest([cur], hist, cache=self.tmp)
        self.assertEqual((h["rule"], h["action"]), ("claude_md", "Add a file map to CLAUDE.md"))
        self.assertTrue(h["evidence"].startswith("≈50k discovery/run − ≈%s file map in CLAUDE.md → ≈%dk/run net"
                                                 % (xi._fk(h["cost_cw"]), round(h["net_cw"] / 1e3))))
        path = resolve(h["stub_path"], self.tmp)
        self.assertTrue(path.startswith(os.path.join(self.tmp, "claude-md-drafts") + os.sep))
        with open(path, encoding="utf-8") as f:
            self.assertIn("- `models.py`: source; read to get oriented in 3 of 3 past runs", f.read())
        # a busy project pays for the map on every turn of every session: the skill nets more
        o = self.opts(50_000, 10, found=found)
        self.assertGreater(o["repeat"]["net"], o["claude_md"]["net"])
        self.assertEqual(xi.suggest([cur], hist + busy_project(n=40), write=False)["rule"], "repeat")

    def test_file_map_needs_stable_files(self):
        cur, hist = runs_with(50_000, 10, n=4, found=("a.py", "b.py", "c.py", "d.py", "e.py", "f.py"))
        for k, r in enumerate(hist):   # two runs find a.py, b.py, c.py, the other two d.py, e.py, f.py
            r["disc_files"] = ["a.py", "b.py", "c.py"] if k < 2 else ["d.py", "e.py", "f.py"]
        self.assertEqual(xi.discovered_files(hist), (["a.py", "b.py", "c.py", "d.py", "e.py", "f.py"], False))
        opts = {o["rule"]: o for o in xi.remedies(xi.find_repeat(cur, hist), hist)}
        self.assertFalse(opts["claude_md"]["ok"])
        self.assertGreater(opts["claude_md"]["net"], opts["repeat"]["net"])   # cheaper, but not the same files
        self.assertEqual(xi.suggest([cur], hist, write=False)["rule"], "repeat")
        for r in hist[:3]:
            r["disc_files"] = ["a.py", "b.py", "c.py"]
        self.assertEqual(xi.discovered_files(hist), (["a.py", "b.py", "c.py"], True))

    def test_consistency_only_when_no_net_saving(self):
        tok = self.body_tokens()
        cur, hist = runs_with(0.5 * xi.context_cost(tok, 10), 10)   # net < 0
        h = xi.suggest([cur], hist + busy_project(), write=False)
        self.assertEqual(h["rule"], "consistency")
        self.assertTrue(h["action"].endswith('" a skill for consistency'))
        self.assertTrue(h["evidence"].startswith("same 4 steps in 3 runs; no token saving expected (≈"))
        self.assertIn(" discovery/run − ≈", h["evidence"])
        self.assertEqual(h["est_saving_cw"], 0)
        # two runs repeating the steps are not enough
        cur, hist = runs_with(0.5 * xi.context_cost(tok, 10), 10)
        hist[0]["steps"] = hist[0]["steps"][2:]
        self.assertIsNone(xi.consistency(xi.find_repeat(cur, hist), [{"rule": "repeat", "net": -1}]))

    def test_evidence_shows_both_terms(self):
        cur, hist = runs_with(41_000, 10)
        h = xi.suggest([cur], hist + busy_project(), write=False)
        self.assertRegex(h["evidence"], r"^≈41k discovery/run − ≈\d+ skill body → ≈\d+k/run net · done 3× in 3 sessions")

    def test_consistency_hint_does_not_displace_a_session_hint(self):
        sess = [{"kind": "session", "cw_at_stake": c} for c in (90, 80, 70)]
        h = {"kind": "cross_session", "rule": "consistency", "cw_at_stake": 0}
        self.assertNotIn(h, rc.with_cross(sess, h))
        self.assertIn(h, rc.with_cross(sess[:2], h))


def disc_run(sid, items, files=("cli.py", "models.py", "store.py", "util.py", "README.md"), skills=(), turns=4):
    """A request (two shared steps, ~100k CW) whose discovery calls are `items`: [kind, cw, step, read basename |
    search names]."""
    r = workflow(sid, skills=skills, found=tuple(f for f in files if f not in ("cli.py", "README.md")))
    r.update(disc_items=[list(x) for x in items], disc=sum(x[1] for x in items), turns=turns,
             disc_files=[x[3] for x in items if x[0] == "read"])
    return r


class RemovableDiscovery(unittest.TestCase):
    """Only discovery the remedy's content would have short-circuited counts as its saving."""
    MAP = ["src/models.py", "store.py", "util.py"]

    def test_reads_of_listed_files_and_the_searches_that_led_to_them(self):
        r = disc_run("a", [["search", 8_000, 0, ["models.py", "other.py"]],   # its output names a listed file
                           ["search", 5_000, 0, ["other.py"]],                # precedes models.py's read, same step
                           ["read", 10_000, 0, "models.py"],
                           ["search", 7_000, 0, ["nothing.py"]],              # after it, never led to a listed file
                           ["search", 6_000, 1, []],                          # another step, no listed read there
                           ["read", 9_000, 1, "other.py"],                    # not listed
                           ["tools", 3_000, 1, []], ["subagent", 20_000, 2, []],
                           ["read", 4_000, 2, "util.py"]])
        self.assertEqual(xi.removable(r, self.MAP), 8_000 + 5_000 + 10_000 + 4_000)
        self.assertEqual(xi.removable(r, []), 0)
        self.assertEqual(r["disc"], 72_000)   # the record still knows all of its discovery

    def test_a_search_in_a_step_without_a_listed_read_stays_out(self):
        r = disc_run("a", [["search", 30_000, 0, ["a.py"]], ["read", 2_000, 1, "store.py"]])
        self.assertEqual(xi.removable(r, self.MAP), 2_000)

    def test_one_file_map_does_not_claim_the_whole_discovery(self):
        """The pr-review case: runs spend ~40k searching the diff, and read report.md: a map of report.md would
        spare that read only, and one file is no map."""
        items = [["search", 20_000, 0, ["a.ts", "b.ts"]], ["read", 18_000, 0, "a.ts"], ["read", 2_000, 1, "report.md"]]
        runs = [disc_run(sid, items, skills=("pr-review",)) for sid in ("p1", "p2", "p3")]
        for r in runs:
            r["disc_files"] = ["report.md"]
        cur = disc_run("now", items, skills=("pr-review",))
        opts = xi.remedies(xi.find_repeat(cur, runs), runs)
        o = next(o for o in opts if o["rule"] == "skill_filemap")
        self.assertEqual(o["files"], ["report.md"])
        self.assertEqual(o["disc"], 2_000)
        self.assertFalse(o["ok"])
        self.assertIsNone(xi.suggest([cur], runs, write=False))

    def test_hint_shows_the_removable_part_as_discovery(self):
        items = [["search", 20_000, 0, ["models.py"]], ["read", 10_000, 0, "models.py"],
                 ["read", 6_000, 0, "store.py"], ["read", 4_000, 0, "util.py"],
                 ["search", 25_000, 1, ["unrelated.txt"]], ["tools", 5_000, 1, []]]
        runs = [disc_run(sid, items) for sid in ("p1", "p2", "p3")]
        h = xi.suggest([disc_run("now", items)], runs + busy_project(), write=False)
        self.assertEqual(h["rule"], "repeat")
        self.assertEqual(h["discovery_cw"], 40_000)   # not the 70k the runs spent finding their way
        self.assertTrue(h["evidence"].startswith("≈40k discovery/run − "))
        self.assertIn("full reads of the 5 files it lists", h["estimate"])


class SubstantiveRemedy(unittest.TestCase):
    ITEMS = [["search", 30_000, 0, ["models.py", "store.py", "util.py"]], ["read", 10_000, 0, "models.py"],
             ["read", 10_000, 0, "store.py"], ["read", 10_000, 0, "util.py"]]

    def opts(self, items, files=("cli.py", "models.py", "store.py", "util.py", "README.md"), skills=()):
        runs = [disc_run(sid, items, files, skills) for sid in ("p1", "p2", "p3")]
        cur = disc_run("now", items, files, skills)
        return {o["rule"]: o for o in xi.remedies(xi.find_repeat(cur, runs), runs)}

    def test_file_map_needs_three_stable_files(self):
        o = self.opts(self.ITEMS)
        self.assertTrue(o["claude_md"]["ok"])
        self.assertEqual(len(o["claude_md"]["files"]), 3)
        o = self.opts(self.ITEMS[:3])   # models.py and store.py only
        self.assertEqual(o["claude_md"]["files"], ["models.py", "store.py"])
        self.assertGreater(o["claude_md"]["net"], xi.MIN_NET)
        self.assertFalse(o["claude_md"]["ok"])
        o = self.opts(self.ITEMS[:3], skills=("cli-change",))
        self.assertFalse(o["skill_filemap"]["ok"])
        self.assertTrue(self.opts(self.ITEMS, skills=("cli-change",))["skill_filemap"]["ok"])

    def test_drafted_skill_needs_three_files(self):
        self.assertTrue(self.opts(self.ITEMS)["repeat"]["ok"])
        items = [["search", 40_000, 0, ["cli.py"]], ["read", 30_000, 0, "cli.py"]]
        o = self.opts(items, files=("cli.py", "README.md"))
        self.assertEqual(o["repeat"]["files"], ["cli.py", "README.md"])
        self.assertGreater(o["repeat"]["net"], xi.MIN_NET)
        self.assertFalse(o["repeat"]["ok"])


class BoilerplateWords(unittest.TestCase):
    """Text an app injects into every prompt must not make unrelated requests match."""
    BOILER = "You are working inside a Conductor workspace on a Mac, in parallel with other agents. "

    def req(self, sid, task, project="p"):
        steps = [step(0, "investigate", "reading code · a.py", ("a.py", "%s.py" % sid), prompt=True, cw=60_000),
                 step(1, "change", "editing files · a.py", ("a.py",), tools={"Edit": 1})]
        return xi.requests_of(steps, [], sid, project, [(0, xi.prompt_words(self.BOILER + task))])[0]

    def test_word_idf(self):
        hist = [self.req("h%d" % k, "Task %s about topic%s" % (w, w)) for k, w in enumerate(
            "alpha bravo charlie delta echo foxtrot golf hotel india juliet kilo lima mike november oscar papa "
            "quebec romeo sierra tango uniform victor whiskey xray yankee zulu amber birch cedar daisy".split())]
        w = xi.word_idf(hist)
        self.assertEqual(w["conductor"], 0.0)   # in every prompt
        self.assertNotIn("alpha", w)            # 1 of 30: full weight (unlisted = 1)
        self.assertEqual(xi.word_idf(hist[:10]), {})   # too few requests to tell

    def test_boilerplate_does_not_anchor_a_match(self):
        words = ("alpha bravo charlie delta echo foxtrot golf hotel india juliet kilo lima mike november oscar papa "
                 "quebec romeo sierra tango uniform victor whiskey xray yankee zulu amber birch cedar daisy").split()
        hist = [self.req("h%d" % k, "Rename the %s option" % w) for k, w in enumerate(words)]
        a = self.req("a", "Translate the onboarding emails into German")
        b = self.req("b", "Profile the slow export endpoint")
        self.assertTrue(xi.is_match(a, b)[0])   # unweighted: the shared boilerplate words carry it
        self.assertFalse(xi.is_match(a, b, widf=xi.word_idf(hist))[0])
        c = self.req("c", "Translate the onboarding emails into French")
        self.assertTrue(xi.is_match(a, c, widf=xi.word_idf(hist))[0])   # the task words still match

    def test_project_boilerplate_weighs_zero_in_that_project(self):
        words = ("alpha bravo charlie delta echo foxtrot golf hotel india juliet kilo lima mike november oscar papa "
                 "quebec romeo sierra tango uniform victor whiskey xray yankee zulu amber birch cedar daisy").split()
        pre = "Release checklist for the warehouse ledger nightly reconciliation pipeline. "
        hist = [self.req("h%d%s" % (k, v), "%s the %s option" % (v, w), project="other")
                for k, w in enumerate(words) for v in ("Rename", "Remove", "Document", "Deprecate")]
        hist += [self.req("q%d" % k, pre + "Fix the %s report" % w, project="ledger") for k, w in enumerate(words[:8])]
        w = xi.word_idf(hist)
        self.assertGreater(w.get("reconciliation", 1.0), 0.5)          # 8 of 128 globally: still weighs
        self.assertEqual(w.of("ledger")["reconciliation"], 0.0)        # but in every ledger prompt
        self.assertIs(w.of("other"), w.proj["other"])
        self.assertIs(w.of("tiny"), w)                                  # no per-project weights: the global ones
        self.assertNotIn("tiny", xi.word_idf(hist + [self.req("t%d" % k, pre, project="tiny") for k in range(7)]).proj)
        a = self.req("a", pre + "Translate the onboarding emails into German", project="ledger")
        b = self.req("b", pre + "Profile the slow export endpoint", project="ledger")
        self.assertTrue(xi.is_match(a, b, widf=dict(w))[0])   # global weights only: the preamble carries it
        self.assertFalse(xi.is_match(a, b, widf=w)[0])
        c = self.req("c", pre + "Translate the onboarding emails into French", project="ledger")
        self.assertTrue(xi.is_match(a, c, widf=w)[0])


class StrongPromptMatch(unittest.TestCase):
    """The same task asked in near-identical words is a match even when the run went another way (one git ls-files
    and a big cat instead of many Reads / finds)."""
    def by_reads(self, sid, task):
        fs = ("cli.py", "registry.py", "models.py", "store.py", "test_cli.py")
        steps = [step(0, "investigate", "reading code · " + ", ".join(fs), fs, tools={"Read": 6, "Bash find": 3},
                      prompt=True, cw=60_000),
                 step(1, "change", "editing files · count.py", ("count.py", "test_count.py"), tools={"Write": 2}),
                 step(2, "verify", "running tests · unittest", (), ("unittest",), tools={"Bash unittest": 1},
                      runners=("Bash unittest",))]
        return xi.requests_of(steps, [], sid, "p", [(0, xi.prompt_words(task))])[0]

    def by_cat(self, sid, task):
        steps = [step(0, "investigate", "running shell · git ls-files", ("files.txt",),
                      tools={"Bash git ls-files": 1, "Bash cat": 1}, prompt=True, cw=60_000),
                 step(1, "change", "editing files · top.py", ("top.py", "test_top.py", "top.md"),
                      tools={"Edit": 6, "Write": 3}),
                 step(2, "change", "editing files · CHANGELOG.md", ("CHANGELOG.md",), tools={"Edit": 1}),
                 step(3, "verify", "running tests · unittest", (), ("unittest",), tools={"Bash unittest": 6},
                      runners=("Bash unittest",))]
        return xi.requests_of(steps, [], sid, "p", [(0, xi.prompt_words(task))])[0]

    def test_strong_prompt_match_with_different_tools_matches(self):
        a = self.by_reads("a", SUBCMD % ("count", TASKS["count"]))
        b = self.by_cat("b", SUBCMD % ("top", TASKS["top"]))
        self.assertGreaterEqual(xi.token_sim(a, b)[0], xi.STRONG_PTOK)
        self.assertLess(xi.similarity(a, b)[0], xi.SIM_MIN)   # the tools / files alone would not carry it
        self.assertTrue(xi.is_match(a, b)[0])

    def test_weak_prompt_match_with_different_tools_does_not(self):
        a = self.by_reads("a", SUBCMD % ("count", TASKS["count"]))
        b = self.by_cat("b", "Add a `top` subcommand that lists the items with the highest stock value; keep the "
                             "registry sorted and print a table.")
        self.assertLess(xi.token_sim(a, b)[0], xi.STRONG_PTOK)
        self.assertFalse(xi.is_match(a, b)[0])

    def test_few_shared_words_are_not_strong_evidence(self):
        a = self.by_reads("a", "Translate the onboarding emails into German")
        b = dict(self.by_cat("b", "Translate the onboarding emails into French"), cats=["verify"])
        self.assertGreaterEqual(xi.token_sim(a, b)[0], xi.STRONG_PTOK)   # 3 shared words
        self.assertLess(xi.similarity(a, b)[0], xi.SIM_MIN)
        self.assertFalse(xi.is_match(a, b)[0])

    def test_preamble_dominated_prompts_are_not_strong_evidence(self):
        pre = ("Nightly warehouse ledger reconciliation: follow the runbook, compare supplier invoices against "
               "shipment manifests, flag discrepancies, summarise totals per region, attach evidence. ")
        widf = {t: 0.5 for t in xi.prompt_words(pre)}   # common across the index, not quite boilerplate
        a = self.by_reads("a", pre + "Translate the onboarding emails")
        b = dict(self.by_cat("b", pre + "Profile the slow export endpoint"), cats=["verify"])
        self.assertGreaterEqual(xi.token_sim(a, b, widf)[0], xi.STRONG_PTOK)        # the preamble carries the Dice
        self.assertGreaterEqual(xi.shared_word_weight(a, b, widf), xi.STRONG_PTOK_W)
        self.assertLess(xi.similarity(a, b, widf=widf)[0], xi.SIM_MIN)
        self.assertLess(xi.task_share(a, widf), xi.STRONG_TASK_SHARE)
        self.assertFalse(xi.strong_prompt_match(a, b, widf))
        self.assertFalse(xi.is_match(a, b, widf=widf)[0])
        self.assertTrue(xi.strong_prompt_match(self.by_reads("c", SUBCMD % ("count", TASKS["count"])),
                                               self.by_cat("d", SUBCMD % ("top", TASKS["top"])), widf))


class SkillFrontmatter(unittest.TestCase):
    def test_folded_and_literal_descriptions(self):
        meta, body = xi._frontmatter("---\nname: loop-pickup\ndescription: >-\n  Resume a multi-phase effort\n"
                                     "  in a fresh chat.\n\n  Use at the start.\nargument-hint: \"<x>\"\n---\n# Body\n")
        self.assertEqual(meta["description"], "Resume a multi-phase effort in a fresh chat. Use at the start.")
        self.assertEqual(meta["argument-hint"], "<x>")
        self.assertEqual(body, "# Body\n")
        meta, _ = xi._frontmatter("---\ndescription: |\n  Line one\n  line two\nname: x\n---\n")
        self.assertEqual((meta["description"], meta["name"]), ("Line one\nline two", "x"))
        self.assertEqual(xi._frontmatter("---\ndescription: 'plain'\n---\n")[0]["description"], "plain")

    def test_loaded_skill_has_the_folded_description_and_the_files_it_names(self):
        tmp = tempfile.mkdtemp()
        try:
            os.makedirs(os.path.join(tmp, "loop-pickup"))
            with open(os.path.join(tmp, "loop-pickup", "SKILL.md"), "w", encoding="utf-8") as f:
                f.write("---\nname: loop-pickup\ndescription: >-\n  Resume the plan from `.loop/plan.md`,\n"
                        "  then claim a lane.\n---\n\nRead `.loop/plan.md` and handoff.md (see https://x.io/a.html),"
                        " e.g. the lane.\n")
            sk = xi.load_skills([tmp])[0]
            self.assertEqual(sk["description"], "Resume the plan from `.loop/plan.md`, then claim a lane.")
            self.assertIn("resume", sk["tokens"])
            self.assertEqual(sk["files"], ["plan.md", "handoff.md"])
        finally:
            shutil.rmtree(tmp)

    def test_named_files(self):
        self.assertEqual(xi.named_files("src/a.py:3:def x\n./b.ts\nREADME.md  notes  v1.2  e.g. Makefile c.py."),
                         ["a.py", "b.ts", "README.md", "c.py"])


if __name__ == "__main__":
    unittest.main()
