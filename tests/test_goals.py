"""Goal discovery: tokens, features, tf-idf + cosine, clustering, areas, naming (fallback + Claude), evidence, union."""
import json
import os
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tokenscope import goals as gl  # noqa: E402
from tokenscope import labeler  # noqa: E402


def step(kind, target="", tool=None, tests=None, **kw):
    tool = tool or (kind.split("(")[0] if not kind.startswith("Hook(") else "hook")
    return dict({"kind": kind, "tool": tool, "target": target, "tests": tests}, **kw)


def req(rid, prompt, edited=(), read=(), outcome="success", tokens=1000, start=None, session=None):
    steps = [step("Read", p) for p in read]
    for p in edited:
        steps += [step("Edit", p), step("Hook(PostToolUse:Edit)")]
    steps.append(step("Bash(./run_tests.sh)", "./run_tests.sh", tests={"passed": 3, "failed": 0}))
    r = {"id": rid, "session_id": session or rid.split(":")[0], "prompt": prompt, "steps": steps,
         "outcome": outcome, "tokens": {"all_in": tokens}, "start": start or "2026-10-01T00:00:%02dZ" % len(rid),
         "chain": " → ".join(s["kind"] for s in steps)}
    r["files"] = gl.request_files(steps)
    return r


ROUND = "Checkout total is off by a few cents when rounding in toCents"
ROUND2 = "why does the checkout total drift by a cent? rounding in addEur looks wrong"
SAVE10 = "Add a SAVE10 discount code to Cart#applyCode"
BOGO = "Support a BOGO promo code: cheapest unit free"
PW = "Require passwords of at least 10 characters in register()"
LOCK = "Lock an account after 5 failed login attempts"


def corpus():
    return [
        req("r1:1", ROUND, ["src/money.js", "test/money.test.js"], ["src/money.js"]),
        req("r2:1", ROUND, ["src/money.js", "test/money.test.js"], ["src/money.js"], outcome="failed"),
        req("r3:1", ROUND2, ["src/money.js", "test/money.test.js"], ["src/money.js", "src/pricing.js"]),
        req("s1:1", SAVE10, ["src/discount.js", "test/discount.test.js"], ["src/cart.js"]),
        req("s2:1", SAVE10, ["src/discount.js", "test/discount.test.js"], ["src/cart.js"]),
        req("b1:1", BOGO, ["src/promo.js", "test/promo.test.js"], ["src/cart.js"]),
        req("p1:1", PW, ["src/auth.js", "test/password.test.js"], ["src/auth.js", "src/users.js"]),
        req("l1:1", LOCK, ["src/auth.js", "test/lockout.test.js"], ["src/auth.js", "src/session.js"]),
    ]


class TestText(unittest.TestCase):
    def test_stem(self):
        for w, s in (("rounding", "round"), ("rounded", "round"), ("rounds", "round"), ("taxes", "tax"),
                     ("reserving", "reserv"), ("reserve", "reserv"), ("cents", "cent"), ("status", "status"),
                     ("running", "run"), ("save10", "save10")):
            self.assertEqual(gl.stem(w), s, w)

    def test_tokenize(self):
        toks = gl.tokenize("Please fix the rounding in toCents for src/money.js, 3 times; apply SAVE10 to cart_items")
        self.assertIn("round", toks)
        self.assertIn("cent", toks)          # camelCase split ...
        self.assertIn("tocent", toks)        # ... and the whole identifier
        self.assertIn("save10", toks)        # identifier with digits kept whole
        self.assertIn("money", toks)         # path split
        self.assertIn("item", toks)          # snake_case split
        for gone in ("please", "fix", "the", "3", "for", "to"):
            self.assertNotIn(gone, toks)     # stopwords, task words, bare numbers
        self.assertEqual(gl.tokenize("add tests and make sure all tests pass"), [])
        self.assertIn("test", gl.TASK_WORDS)


class TestFeatures(unittest.TestCase):
    def test_request_files(self):
        steps = [step("Read", "src/a.js"), step("Grep", "foo", path="src/lib"),
                 step("Bash(cat)", "cat src/b.js src/c.js 2>/dev/null; head -c 600 test/r.test.js; sed -n 1,50p d.py"),
                 step("Edit", "/repo/src/a.js"), step("Hook(PostToolUse:Edit)", "run.sh"),
                 step("Write", "test/a.test.js"),
                 step("Bash(python3)", "python3 - <<'EOF' p='src/e.js' s=open(p).read() open(p,'w').write(s) EOF"),
                 step("Bash(cat)", "cat > notes/x.md <<'EOF' hi EOF")]
        f = gl.request_files(steps, cwd="/repo")
        self.assertEqual(f["edited"], ["src/a.js", "test/a.test.js", "src/e.js", "notes/x.md"])
        self.assertEqual(f["read"], ["src/a.js", "src/lib", "src/b.js", "src/c.js", "test/r.test.js", "d.py"])

    def test_bash_parsers(self):
        self.assertEqual(gl.bash_read_targets("grep -n total src/money.js | head -5"), ["src/money.js"])
        self.assertEqual(gl.bash_write_targets("sed -i s/a/b/ src/y.js && npm test | tee out.log"),
                         ["src/y.js", "out.log"])
        self.assertEqual(gl.bash_read_targets("sed -i s/a/b/ src/y.js"), [])  # in-place edit, not a read

    def test_signature_groups(self):
        r = req("x:1", "Fix rounding in toCents", ["src/money.js"], ["src/money.js", "CLAUDE.md"])
        ign = gl.overhead_files([r])
        self.assertEqual(ign, {"CLAUDE.md": "agent config"})
        sig = gl.signature(r, ign)
        self.assertEqual(set(sig["files_edited"]), {"src/money.js"})
        self.assertEqual(set(sig["files_read"]), {"src/money.js"})       # CLAUDE.md ignored
        self.assertIn("money", sig["path"])
        self.assertIn("Edit>Hook", sig["chain"])
        self.assertIn("Read>Edit", sig["chain"])
        self.assertIn("./run_tests.sh", sig["commands"])
        self.assertIn("tests:./run_tests.sh", sig["commands"])
        self.assertNotIn("outcome", sig)

    def test_overhead_share(self):
        reqs = [req("o%d:1" % i, "goal %d" % i, read=["package.json", "src/f%d.js" % i]) for i in range(6)]
        reqs.append(req("o9:1", "other", read=["src/z.js", ".claude/settings.json", "/tmp/log.txt"]))
        ign = gl.overhead_files(reqs)
        self.assertIn("6 of 7 requests", ign["package.json"])
        self.assertEqual(ign[".claude/settings.json"], "agent config")
        self.assertEqual(ign["/tmp/log.txt"], "outside the repo")
        self.assertNotIn("src/f1.js", ign)
        self.assertEqual(gl.overhead_files(reqs[:3]).get("package.json"), None)  # too few requests to judge

    def test_path_tokens(self):
        self.assertEqual([s for s, _ in gl.path_tokens("src/inventory/stock.test.js")], ["inventory", "stock"])
        self.assertEqual([s for s, _ in gl.path_tokens("CHANGELOG.md")], ["changelog", "docs"])


class TestSimilarity(unittest.TestCase):
    def test_tfidf_cosine(self):
        sigs = [{"text": {"round": 1, "cent": 1}}, {"text": {"round": 1, "cent": 1}}, {"text": {"bogo": 2}}]
        g = gl.tfidf(sigs)
        for v in g:
            self.assertAlmostEqual(sum(x * x for x in v["text"].values()), 1.0)
        vs = [gl.combine(x, gl.GOAL_WEIGHTS) for x in g]
        self.assertAlmostEqual(gl.dot(vs[0], vs[1]), 1.0)
        self.assertEqual(gl.dot(vs[0], vs[2]), 0.0)

    def test_idf_downweights_common(self):
        sigs = [{"text": {"cart": 1, "save10": 1}}, {"text": {"cart": 1, "bogo": 1}}, {"text": {"cart": 1}}]
        v = gl.tfidf(sigs)[0]["text"]
        self.assertGreater(v["save10"], v["cart"])

    def test_missing_group_not_penalised(self):
        a = {"text": {"x": 1.0}, "files_edited": {"f": 1.0}}
        b = {"text": {"x": 1.0}}
        s = gl.dot(gl.combine(a, gl.GOAL_WEIGHTS), gl.combine(b, gl.GOAL_WEIGHTS))
        w = gl.GOAL_WEIGHTS
        self.assertAlmostEqual(s, w["text"] / ((w["text"] + w["files_edited"]) * w["text"]) ** 0.5)

    def test_clustering_threshold(self):
        S = [[1, .9, .2, .1], [.9, 1, .3, .1], [.2, .3, 1, .6], [.1, .1, .6, 1]]
        self.assertEqual(gl.agglomerate(S, 0.5), [[0, 1], [2, 3]])
        self.assertEqual(gl.agglomerate(S, 0.95), [[0], [1], [2], [3]])
        self.assertEqual(gl.agglomerate(S, 0.7), [[0, 1], [2], [3]])
        # average linkage: {0,1} vs {2,3} mean = (.2+.1+.3+.1)/4 = .175
        self.assertEqual(gl.agglomerate(S, 0.17), [[0, 1, 2, 3]])
        self.assertEqual(gl.agglomerate(S, 0.18), [[0, 1], [2, 3]])
        self.assertEqual(gl.agglomerate([], 0.5), [])


class TestDiscover(unittest.TestCase):
    def setUp(self):
        self.d = gl.discover(corpus())
        self.by = {g["id"]: g for g in self.d["goals"]}
        self.of = self.d["assignment"]

    def test_goals(self):
        a = self.of
        self.assertEqual(len(self.d["goals"]), 5)
        self.assertEqual(a["r1:1"], a["r3:1"])        # two phrasings of the rounding bug merge
        self.assertEqual(a["r1:1"], a["r2:1"])        # outcome (success vs failed) does not split a goal
        self.assertEqual(a["s1:1"], a["s2:1"])
        self.assertNotEqual(a["s1:1"], a["b1:1"])     # SAVE10 vs BOGO: related, different goals
        self.assertNotEqual(a["p1:1"], a["l1:1"])     # same file, different feature
        self.assertEqual(self.d["goals"][0]["runs"], 3)  # biggest first, deterministic
        self.assertEqual(gl.discover(corpus())["assignment"], a)

    def test_threshold_extremes(self):
        self.assertEqual(len(gl.discover(corpus(), goal_threshold=0.0)["goals"]), 1)
        self.assertEqual(len(gl.discover(corpus(), goal_threshold=1.01)["goals"]), 8)

    def test_areas(self):
        areas = {a["id"]: a for a in self.d["areas"]}
        area_of = {g["id"]: g["area"] for g in self.d["goals"]}
        self.assertEqual(area_of[self.of["p1:1"]], area_of[self.of["l1:1"]])   # auth
        self.assertEqual(area_of[self.of["s1:1"]], area_of[self.of["b1:1"]])   # cart / pricing
        self.assertNotEqual(area_of[self.of["p1:1"]], area_of[self.of["s1:1"]])
        auth = areas[area_of[self.of["p1:1"]]]
        self.assertEqual(auth["members"], 2)
        self.assertIn("src/auth.js", [f["path"] for f in auth["shared_files"]])
        self.assertIn("auth", auth["name"])
        self.assertEqual(len(gl.discover(corpus(), area_threshold=1.01)["areas"]), 5)  # each goal its own area

    def test_fallback_names(self):
        g = self.by[self.of["r1:1"]]
        self.assertEqual(g["name_source"], "terms")
        terms, top = g["name"].split(" · ")
        self.assertEqual(top, "src/money.js")
        self.assertIn("cents", terms)
        self.assertLessEqual(len(terms.split()), gl.NAME_TERMS)
        self.assertIn("bogo", self.by[self.of["b1:1"]]["name"])
        self.assertTrue(self.by[self.of["s1:1"]]["name"].startswith("save10 "))  # ties: prompt order
        self.assertEqual(len({g["name"] for g in self.d["goals"]}), 5)

    def test_evidence(self):
        g = self.by[self.of["r1:1"]]
        self.assertEqual([m["id"] for m in g["members"]], ["r1:1", "r2:1", "r3:1"])
        self.assertEqual(g["members"][2]["prompt"], ROUND2)
        self.assertEqual(set(g["members"][0]), {"id", "session", "prompt", "outcome", "tokens", "chain"})
        self.assertEqual(g["outcomes"], {"failed": 1, "success": 2})
        self.assertEqual(g["files_edited"][0], {"path": "src/money.js", "requests": 3})
        self.assertIn({"path": "src/pricing.js", "requests": 1}, g["files_read"])
        self.assertEqual(sum(c["count"] for c in g["chains"]), 3)
        self.assertGreater(g["cohesion"], gl.GOAL_THRESHOLD)
        self.assertNotEqual(g["nearest"]["id"], g["id"])
        self.assertTrue(0 <= g["nearest"]["similarity"] < g["cohesion"])
        self.assertTrue(g["nearest"]["name"])
        labels = [w["label"] for w in g["why"]]
        self.assertIn("file:src/money.js", labels)
        self.assertTrue(any(lab.startswith("text:'") for lab in labels))
        self.assertLessEqual(sum(w["contribution"] for w in g["why"]), g["cohesion"] + 1e-3)
        single = self.by[self.of["b1:1"]]
        self.assertIsNone(single["cohesion"])
        self.assertEqual(single["why"], [])
        m = self.d["method"]
        self.assertEqual(m["goal_threshold"], gl.GOAL_THRESHOLD)
        self.assertIn("not a feature", m["note"])

    def test_union_for_compare(self):
        a = [req("a%d:1" % i, p, e, rd, session="A%d" % i) for i, (p, e, rd) in
             enumerate([(SAVE10, ["src/cart.js"], ["src/cart.js"]), (PW, ["src/auth.js"], [])])]
        b = [req("b%d:1" % i, p, e, rd, session="B%d" % i) for i, (p, e, rd) in
             enumerate([(SAVE10, ["src/cart.js", "src/pricing.js"], []), (LOCK, ["src/session.js"], [])])]
        d = gl.discover(a + b)
        self.assertEqual(d["assignment"]["a0:1"], d["assignment"]["b0:1"])  # same prompt, same goal both sides
        self.assertNotEqual(d["assignment"]["a1:1"], d["assignment"]["b1:1"])


class FakeRunner:
    def __init__(self, replies):
        self.replies, self.calls = list(replies), []

    def __call__(self, cmd, **kw):
        self.calls.append((cmd, kw))
        r = self.replies.pop(0)
        if isinstance(r, Exception):
            raise r
        rc, out = r
        return subprocess.CompletedProcess(cmd, rc, stdout=out, stderr="boom" if rc else "")


def reply(obj, model="claude-haiku-5-5"):
    return 0, json.dumps({"type": "result", "subtype": "success", "is_error": False,
                          "result": "```json\n%s\n```" % json.dumps(obj), "modelUsage": {model: {"outputTokens": 9}}})


class TestClaudeNaming(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cache = os.path.join(self.tmp.name, "goal_names.json")

    def tearDown(self):
        self.tmp.cleanup()

    def names(self, d):
        return {"goals": {g["id"]: "Name for " + g["id"] for g in d["goals"]},
                "areas": {a["id"]: "Area " + a["id"] for a in d["areas"]}}

    def test_batch_then_cache(self):
        d = gl.discover(corpus())
        r = FakeRunner([reply(self.names(d))])
        st = gl.name_with_claude(d, cache_path=self.cache, runner=r)
        self.assertEqual(len(r.calls), 1)
        cmd, kw = r.calls[0]
        self.assertEqual(cmd[:2], ["claude", "-p"])
        self.assertEqual(cmd[cmd.index("--model") + 1], "haiku")
        self.assertEqual(cmd[-2:], ["--tools", ""])
        self.assertIn("--no-session-persistence", cmd)
        self.assertIs(kw["stdin"], subprocess.DEVNULL)
        self.assertEqual(kw["timeout"], 180)
        self.assertEqual(kw["cwd"], self.tmp.name)
        body = cmd[2]
        self.assertIn(ROUND2[:60], body)               # member prompts
        self.assertIn('"files_edited"', body)
        self.assertIn('"top_chains"', body)
        self.assertIn('"area": "a1"', body)
        self.assertIn("never generic", body)
        g = d["goals"][0]
        self.assertEqual((g["name"], g["name_source"]), ("Name for g1", "claude-haiku"))
        self.assertEqual(g["area_name"], "Area " + g["area"])
        self.assertEqual((st["named"], st["cached"], st["fallback_reason"]), (5 + len(d["areas"]), 0, None))
        self.assertEqual(gl.name_note(st), "named by claude-haiku (0 cached, %d new)" % st["named"])
        with open(self.cache) as fh:
            cached = json.load(fh)
        self.assertEqual(cached["goals"][g["key"]]["model"], "claude-haiku-5-5")
        # a rerun (fresh discovery, same clusters) makes no call and keeps the names
        d2 = gl.discover(corpus())
        r2 = FakeRunner([])
        st2 = gl.name_with_claude(d2, cache_path=self.cache, runner=r2)
        self.assertEqual(r2.calls, [])
        self.assertFalse(st2["called"])
        self.assertEqual([x["name"] for x in d2["goals"]], [x["name"] for x in d["goals"]])

    def test_generic_and_missing_fall_back(self):
        d = gl.discover(corpus())
        names = self.names(d)
        names["goals"]["g1"] = "Fix a bug"
        del names["goals"]["g2"]
        st = gl.name_with_claude(d, cache_path=self.cache, runner=FakeRunner([reply(names)]))
        by = {g["id"]: g for g in d["goals"]}
        self.assertEqual(by["g1"]["name_source"], "terms")
        self.assertIn(" · ", by["g1"]["name"])
        self.assertEqual(by["g2"]["name_source"], "terms")
        self.assertEqual(by["g3"]["name_source"], "claude-haiku")
        self.assertIn("2 names missing, invalid or generic", st["fallback_reason"])

    def test_haiku_unavailable_then_default(self):
        d = gl.discover(corpus())
        r = FakeRunner([(1, ""), reply(self.names(d), model="claude-sonnet-5-5")])
        gl.name_with_claude(d, cache_path=self.cache, runner=r)
        self.assertEqual(len(r.calls), 2)
        self.assertNotIn("--model", r.calls[1][0])
        self.assertEqual(d["goals"][0]["name_source"], "claude-sonnet")

    def test_failure_keeps_term_names(self):
        for replies, why in (([FileNotFoundError(), FileNotFoundError()], "not found"),
                             ([subprocess.TimeoutExpired("claude", 180)] * 2, "timed out"),
                             ([(0, json.dumps({"result": "sorry, no"}))] * 2, "no JSON")):
            d = gl.discover(corpus())
            before = [g["name"] for g in d["goals"]]
            st = gl.name_with_claude(d, cache_path=self.cache, runner=FakeRunner(replies))
            self.assertEqual([g["name"] for g in d["goals"]], before)
            self.assertEqual(st["used"], "claude (failed)")
            self.assertIn(why, st["fallback_reason"])
            self.assertIn("terms fallback", gl.name_note(st))

    def test_extract_json(self):
        self.assertEqual(labeler.extract_json('Here: {"a": {"b": "}"}} trailing'), {"a": {"b": "}"}})
        self.assertIsNone(labeler.extract_json("nothing"))


if __name__ == "__main__":
    unittest.main()
