"""Patterns miner: one synthetic corpus that triggers every miner, one that triggers none."""
import contextlib
import io
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(__file__))
from tokenscope import cli, pricing  # noqa: E402
from tokenscope import patterns as pt  # noqa: E402


class B:
    """Tiny transcript builder: each step = one API call with one tool use + its result."""

    def __init__(self, sid, cwd, agent=None):
        self.sid, self.cwd, self.agent, self.lines, self.n, self.t, self.ctx = sid, cwd, agent, [], 0, 0, 1000

    def ev(self, **kw):
        self.n += 1
        self.t += 5
        d = {"uuid": "%s-%d" % (self.sid[:4], self.n), "timestamp": "2026-10-02T09:%02d:%02d.000Z" % divmod(self.t, 60),
             "cwd": self.cwd, "sessionId": self.sid, "isSidechain": bool(self.agent), "entrypoint": "cli"}
        if self.agent:
            d["agentId"] = self.agent
        d.update(kw)
        self.lines.append(d)

    def prompt(self, text):
        self.ev(type="user", message={"role": "user", "content": text})

    def _call(self, content, out=50, new=200, stop=None, model="claude-sonnet-4-5"):
        mid = "m%s%d" % (self.sid[:4], self.n)
        u = {"input_tokens": 0, "cache_creation_input_tokens": new, "cache_read_input_tokens": self.ctx,
             "output_tokens": out}
        self.ctx += new
        msg = {"id": mid, "model": model, "content": content, "usage": u, "role": "assistant"}
        if stop:
            msg["stop_reason"] = stop
        self.ev(type="assistant", message=msg)
        return mid

    def step(self, tool, inp, result="ok", err=False, new=200, out=50, text=None):
        tid = "tu%s%d" % (self.sid[:4], self.n)
        content = ([{"type": "text", "text": text}] if text else []) + \
            [{"type": "tool_use", "id": tid, "name": tool, "input": inp}]
        self._call(content, out=out, new=new, stop="tool_use")
        self.ev(type="user", message={"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": tid, "content": result, "is_error": err}]})
        return tid

    def done(self, text="Done.", new=100):
        self._call([{"type": "text", "text": text}], new=new, stop="end_turn")

    def compact(self):
        self.ev(type="system", subtype="compact_boundary", compactMetadata={"trigger": "auto", "preTokens": self.ctx})
        self.ctx = 500

    def write(self, root, slug, path=None):
        p = path or os.path.join(root, slug, self.sid + ".jsonl")
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w") as f:
            for x in self.lines:
                f.write(json.dumps(x) + "\n")


def sid(i):
    return "%08x-0000-0000-0000-%012d" % (i, i)


FAIL = "not ok 1 - discount\n# pass 3\n# fail 1\n"
PASS = "# pass 4\n# fail 0\n"
LISTING = [{"type": "deferred_tools_delta", "addedNames": ["mcp__ghost__query"]},
           {"type": "mcp_instructions_delta", "addedNames": ["ghost"], "addedBlocks": ["g" * 4000]}]


def make_positive(root):
    cwd = "/work/alpha"
    for i in range(3):
        b = B(sid(i + 1), cwd)
        for a in LISTING:
            b.ev(type="attachment", attachment=a)
        b.prompt("Fix the discount rounding bug in src/cart.js")
        b.step("Read", {"file_path": cwd + "/README.md"}, "readme text " * 40)
        b.step("Bash", {"command": "cd /work/alpha && git status"}, "clean")
        b.step("Grep", {"pattern": "TODO"}, "src/cart.js:3")
        for k in range(6):  # paging chain
            b.step("Bash", {"command": "sed -n %d,%dp src/big.py" % (k * 50 + 1, k * 50 + 50)}, "line\n" * 50)
        for res in (FAIL, FAIL, PASS):  # edit -> fail -> edit -> fail -> edit -> pass
            b.step("Edit", {"file_path": cwd + "/src/cart.js", "old_string": "a%d" % i, "new_string": "b"}, "ok")
            b.step("Bash", {"command": "node --test"}, res, err=res is FAIL)
        b.step("Read", {"file_path": cwd + "/src/b.py"}, "b " * 300)
        b.step("Read", {"file_path": cwd + "/src/b.py"}, "b " * 300)  # re-read, no edit
        b.done("Fixed src/cart.js")
        b.prompt("no, that's wrong - keep two decimals")
        b.step("ToolSearch", {"query": "select:WebFetch"}, "loaded")
        b.done()
        b.write(root, "-work-alpha")
    # retries with variations + denied + big unreferenced output + compaction + subagent discarded
    b = B(sid(10), "/work/beta")
    b.prompt("Run the build")
    for c in ("make build", "make build -j2", "make build -j2 V=1"):
        b.step("Bash", {"command": c}, "error: missing header", err=True)
    b.step("Bash", {"command": "rm -rf build"}, "Permission to use Bash has been denied by the user.", err=True)
    b.step("Bash", {"command": "rm -r build"}, "ok")
    b.step("Bash", {"command": "cat logs/huge.log"}, "\n".join("evt_%06d failed at node_%d" % (k, k) for k in range(2000)),
           new=9000)
    for k in range(5):
        b.step("WebFetch", {"url": "https://docs.example.com/p%d" % k}, "page", err=k < 2)
    b.compact()
    b.step("Read", {"file_path": "/work/beta/notes.md"}, "x " * 100)
    tid = b.step("Agent", {"subagent_type": "Explore", "description": "find config", "prompt": "find it"},
                 "Found settings in config/zeta_settings_v2.yaml line 42")
    b.done("Build fixed.")
    b.write(root, "-work-beta")
    sub = os.path.join(root, "-work-beta", sid(10), "subagents")
    a = B(sid(10), "/work/beta", agent="x1")
    a.prompt("find config")
    a.step("Grep", {"pattern": "settings"}, "config/zeta_settings_v2.yaml")
    a.done("Found settings in config/zeta_settings_v2.yaml line 42")
    a.write(root, None, os.path.join(sub, "agent-x1.jsonl"))
    with open(os.path.join(sub, "agent-x1.meta.json"), "w") as f:
        json.dump({"agentType": "Explore", "description": "find config", "toolUseId": tid}, f)
    # workflow: 3 agents re-reading the same files; one failed
    wf = os.path.join(sub, "workflows", "wf_w1")
    for k in range(3):
        a = B(sid(10), "/work/beta", agent="w%d" % k)
        a.prompt("triage %d" % k)
        a.step("Read", {"file_path": "/work/beta/CLAUDE.md"}, "rules " * 50)
        a.step("Bash", {"command": "git log --oneline -20"}, "abc fix")
        a.step("StructuredOutput", {"x": 1}, "bad schema", err=k == 0)
        a.done()
        a.write(root, None, os.path.join(wf, "agent-w%d.jsonl" % k))
        with open(os.path.join(wf, "agent-w%d.meta.json" % k), "w") as f:
            json.dump({"agentType": "workflow-subagent", "description": "triage:%d" % k, "workflowPhase": "Triage"}, f)
    with open(os.path.join(wf, "journal.jsonl"), "w") as f:
        for x in ([{"type": "launched"}] + [{"type": "started", "agentId": "w%d" % k, "phase": "Triage"} for k in range(3)]
                  + [{"type": "result", "agentId": "w1"}, {"type": "result", "agentId": "w2"},
                     {"type": "failed", "agentId": "w0"}]):
            f.write(json.dumps(x) + "\n")
    # divergent paths: same goal, 3 dear runs and 2 cheap ones
    for i in range(5):
        cheap = i >= 3
        b = B(sid(20 + i), "/work/gamma")
        b.prompt("Add the shipping fee calculation to src/ship.js")
        if cheap:
            b.step("Grep", {"pattern": "fee"}, "src/ship.js:1", new=100)
        else:
            for k in range(4):
                b.step("Bash", {"command": "cat src/f%d.js" % k}, "code " * 200, new=5000)
        b.step("Edit", {"file_path": "/work/gamma/src/ship.js", "old_string": "x", "new_string": "y"}, "ok")
        b.done("Added the fee.")
        b.write(root, "-work-gamma")
    # recurring error: private MCP server failing auth; a result merely mentioning "error" is not an error
    b = B(sid(30), "/work/delta")
    b.prompt("Search the web for the release notes")
    for k in range(12):
        b.step("mcp__acme_internal__query", {"q": "notes %d" % k},
               "<tool_use_error>Invalid API key (req %d) for /home/u/.acme/key-%x</tool_use_error>" % (k, 4096 + k),
               err=True)
    for k in range(3):
        b.step("mcp__acme_internal__query", {"q": "ok"}, "results mention error handling and errors", err=False)
    b.done()
    b.write(root, "-work-delta")
    return root


def make_negative(root):
    for i, proj in enumerate(("one", "two", "three")):
        cwd = "/work/" + proj
        b = B(sid(100 + i), cwd)
        b.prompt("Explain module %s" % proj)
        b.step("Read", {"file_path": cwd + "/src/%s.py" % proj}, "def %s_handler_v%d(): pass" % (proj, i))
        b.step("Bash", {"command": "cat logs/%s.log" % proj}, "\n".join("id_%06d ok" % k for k in range(2000)), new=9000)
        b.done("%s.py defines %s_handler_v%d; logs show id_000001 ok" % (proj, proj, i))
        b.write(root, "-work-" + proj)
    return root


def run(root, *extra):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = cli.main(["patterns", "--root", root, "--json", "--no-cache", "--jobs", "1"] + list(extra))
    assert rc == 0
    return json.loads(buf.getvalue())


class PatternsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.root = make_positive(os.path.join(cls.tmp.name, "pos"))
        cls.pos = run(cls.root)
        cls.neg = run(make_negative(os.path.join(cls.tmp.name, "neg")))

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def by(self, d, kind):
        return [p for p in d["patterns"] if p["kind"] == kind]

    def one(self, d, pid):
        got = [p for p in d["patterns"] if p["id"] == pid or p["id"].startswith(pid)]
        self.assertTrue(got, "no pattern %s in %s" % (pid, [p["id"] for p in d["patterns"]]))
        return got[0]

    # --- normalization
    def test_normalization(self):
        self.assertEqual(pt.bash_segments("FOO=1 cd x && sudo npm run build | tail -5"), ["npm run build", "tail"])
        self.assertEqual(pt.bash_segments("uv run pytest -q"), ["uv run"])
        self.assertEqual(pt.bash_segments("python3 -m pytest tests"), ["python3 -m pytest"])
        self.assertEqual(pt.bash_segments("git -C repo log -3"), ["git log"])
        self.assertEqual(pt.kinds("WebFetch", {"url": "https://a.b.com/x"}, "")[1], "WebFetch(a.b.com)")
        self.assertEqual(pt.kinds("mcp__gh__issue", {}, ""), ("MCP", "MCP(gh.issue)"))
        self.assertEqual(pt.kinds("Grep", {"pattern": "TODO"}, "TODO")[1], 'Grep("TODO")')
        self.assertEqual(pt.family("/x/repo/.claude/worktrees/feat-a", "s"), "repo")
        self.assertEqual(pt.family("/x/conductor/workspaces/shop/lisbon", "s"), "shop")
        self.assertEqual(pt.family("/nonexistent/shop-issue-12", "s"), "shop")
        self.assertTrue(pt.is_correction("no, use the other file"))
        self.assertTrue(pt.is_correction("hmm it's still failing"))
        self.assertFalse(pt.is_correction("Now add a README section about notes"))

    # --- 1
    def test_repeated_sequence_and_chain(self):
        p = self.one(self.pos, 'seq:fine:Read(README.md) → Bash(git status) → Grep("TODO")')
        self.assertFalse([x for x in self.pos["patterns"] if x["id"].startswith("seq:coarse:Read → Bash → Grep")])
        self.assertEqual(p["sessions"], 3)
        self.assertEqual(p["suggestion"]["type"], "script")
        c = self.one(self.pos, "chain:Bash(sed)")
        self.assertEqual(c["count"], 3)
        self.assertIn("offset/limit", c["suggestion"]["text"])
        self.assertFalse(self.by(self.neg, "repeated_sequence"))

    def test_tokens_from_analyzer(self):
        p = self.one(self.pos, "chain:Bash(sed)")
        # 18 sed calls; each emitting call: output 50, the result is attributed in the next call (est)
        self.assertEqual(p["tokens"]["output"], 18 * 50)
        self.assertTrue(p["tokens_est"])
        t = p["tokens"]
        self.assertGreaterEqual(t["cost_weighted"], t["output"] * pricing.W_OUTPUT)
        self.assertGreater(p["api_cost_usd"], 0)

    # --- 2
    def test_start_rediscovery(self):
        p = self.one(self.pos, "start:alpha")
        self.assertIn("Read(README.md)", p["items"])
        self.assertIn("Bash(sed src/big.py)", p["items"])
        self.assertEqual(p["suggestion"]["type"], "claude_md")
        f = self.one(self.pos, "fanout:")
        self.assertIn("Read(CLAUDE.md)", f["items"])
        self.assertEqual(f["where"], {"workflow": 6})
        self.assertFalse(self.by(self.neg, "session_start_rediscovery"))

    # --- 3
    def test_retry_loops(self):
        p = self.one(self.pos, "retry:edit_test")
        self.assertEqual(p["files"], {"src/cart.js": 6})
        v = self.one(self.pos, "retry:variants")
        self.assertIn("Bash(make build)", v["commands"])
        d = self.one(self.pos, "retry:denied")
        self.assertEqual(d["suggestion"]["type"], "permission")
        e = self.one(self.pos, "errors:WebFetch")
        self.assertEqual((e["errors"], e["calls"]), (2, 5))
        self.assertFalse(self.by(self.neg, "retry_loops"))

    # --- 4
    def test_wasted_context(self):
        big = self.one(self.pos, "wasted:big_unreferenced")
        self.assertEqual(big["count"], 1)
        self.assertEqual(self.one(self.pos, "wasted:rereads")["count"], 3)
        self.assertTrue(self.one(self.pos, "wasted:full_reads")["count"] >= 1)
        dis = self.one(self.pos, "wasted:discarded_agents")
        self.assertEqual(dis["where"], {"subagent": 1, "workflow": 1})
        self.assertFalse(self.by(self.neg, "wasted_context"))

    # --- 5
    def test_divergent_paths(self):
        p = self.by(self.pos, "divergent_paths")
        self.assertEqual(len(p), 1)
        self.assertIn("Grep", p[0]["suggestion"]["text"])
        self.assertFalse(self.by(self.neg, "divergent_paths"))

    # --- 6
    def test_corrections(self):
        p = self.one(self.pos, "corrections")
        self.assertEqual(p["count"], 3)
        self.assertGreater(p["tokens"]["cost_weighted"], 0)
        self.assertFalse(self.by(self.neg, "user_corrections"))

    # --- 7
    def test_compactions(self):
        p = self.one(self.pos, "compactions")
        self.assertEqual(p["count"], 1)
        self.assertIn("Bash:cat", p["causes"])
        self.assertFalse(self.by(self.neg, "compactions"))

    # --- 8
    def test_workflow_fanout(self):
        w = self.pos["workflows"]
        self.assertEqual(len(w), 1)
        self.assertEqual((w[0]["agents"], w[0]["failed"], w[0]["structured_output_errors"]), (3, 1, 1))
        p = self.one(self.pos, "workflow:Triage")
        self.assertIn("33% of agents failed", p["title"])
        self.assertFalse(self.neg["workflows"])

    # --- 9
    def test_tool_overhead(self):
        self.assertEqual(self.one(self.pos, "overhead:toolsearch")["count"], 3)
        m = self.one(self.pos, "overhead:mcp:ghost")
        self.assertEqual((m["offered"], m["used"]), (3, 0))
        self.assertFalse(self.by(self.neg, "tool_overhead"))

    def test_error_is_is_error_only(self):
        e = self.one(self.pos, "errors:MCP(acme_internal.query)")
        self.assertEqual((e["errors"], e["calls"]), (12, 15))

    def test_recurring_error(self):
        self.assertEqual(pt.error_class("Error at /a/b/c.ts:12 id 0xdeadbeef9 3f2a1b4c-1111-2222-3333-444455556666"),
                         "Error at <path>:N id <hex> <uuid>")
        p = self.one(self.pos, "recerr:mcp:acme_internal:")
        self.assertEqual(p["count"], 12)
        self.assertEqual(p["error_class"], "Invalid API key (req N) for <path>")
        self.assertEqual(p["suggestion"]["type"], "mcp_config")
        self.assertIn("acme_internal", p["suggestion"]["text"])
        self.assertEqual(p["retries"], 12)
        self.assertEqual((p["first_date"], p["distinct_days"]), ("2026-10-02", 1))
        self.assertEqual(len(p["drill"]), 5)
        self.assertEqual(set(p["drill"][0]), {"session_id", "transcript", "call_index", "ts"})
        self.assertEqual(p["drill"][0]["transcript"], "main")
        self.assertFalse(self.by(self.neg, "recurring_error"))

    def test_tool_choice(self):
        p = self.one(self.pos, "toolchoice:read")
        self.assertGreater(p["bash_share"], 0.5)
        self.assertEqual(p["suggestion"]["type"], "claude_md")
        self.assertEqual(p["suggestion"]["est_savings"]["formula"], "not estimated")
        self.assertIn("main", p["split"])
        self.assertEqual(pt.bash_segments("cd /x && sed -n 1,5p a.py")[0], "sed")

    def test_sequence_families(self):
        tl = lambda seq: [{"n": i, "coarse": "Bash", "fine": "Bash(%s)" % x, "cw": 10.0, "new": 1, "out": 1, "cr": 1,  # noqa
                           "usd": 0.0, "call": i, "scaled": True} for i, x in enumerate(seq)]
        recs = [{"transcripts": [{"sid": "s%d" % k, "family": "p%d" % k, "agent": "main", "label": "main",
                                  "where": "main", "tools": tl(["grep", "sed", "grep", "sed", "grep", "sed", "ls"] * 3)}]}
                for k in range(4)]
        side = {}
        got = pt.mine_sequences(recs, {}, side)
        self.assertEqual(len(got), 1)  # rotations / overlaps / contained n-grams collapse into one
        self.assertEqual(got[0]["level"], "fine")
        self.assertTrue(side["coarse_sequences"] == [] or all("covered_by_args_level" in c
                                                               for c in side["coarse_sequences"]))
        self.assertIn("coarse_sequences", self.pos)

    def test_redact(self):
        from tokenscope import redact as rd
        with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as f:
            f.write("Zebracorp\n")
        os.environ["TOKENSCOPE_REDACT_TERMS"] = f.name
        try:
            obj = {"session_id": sid(1), "family": "alpha", "workflow": "wf_abc",
                   "prompt": "fix the Zebracorp billing bug in /work/alpha/src/cart.ts",
                   "title": "Read(/work/alpha/src/cart.ts) in alpha; Zebracorp", "cmd": "git push origin secret-branch",
                   "labels": ["MCP(acme_internal.query)", "MCP(perplexity.ask)", "mcp__claude_ai_Notion__x",
                              "mcp__secretco__y"], "error_class": 'Invalid key "sk-123" for <path> and more text here!!',
                   "tokens": {"new": 1234}, "agent": "a1b2c3d4e5"}
            r = rd.redact(obj, "s1")
            out = json.dumps(r)
            for leak in ("alpha", "cart", "Zebracorp", "secret-branch", "acme_internal", "secretco", "wf_abc", sid(1)):
                self.assertNotIn(leak, out)
            self.assertEqual(r["family"], "project-1")
            self.assertTrue(r["prompt"].startswith("fix… [") and r["prompt"].endswith("chars]"))
            self.assertIn(".ts", r["title"])
            self.assertEqual(r["cmd"], "git push …")
            self.assertEqual(r["labels"][1], "MCP(perplexity.ask)")
            self.assertEqual(r["labels"][2], "mcp__claude_ai_Notion__x")
            self.assertEqual(r["labels"][0], "MCP(mcp-1.query)")
            self.assertLessEqual(len(r["error_class"]), 40)
            self.assertNotIn("sk-123", r["error_class"])
            self.assertEqual(r["tokens"], {"new": 1234})
            self.assertEqual(len(r["session_id"]), 8)
            self.assertEqual(rd.redact(obj, "s1"), r)  # stable
        finally:
            del os.environ["TOKENSCOPE_REDACT_TERMS"]
            os.unlink(f.name)
        d = run(self.root, "--redact")
        self.assertNotIn("/work/alpha", json.dumps(d))
        self.assertNotIn("acme_internal", json.dumps(d))

    def test_shape_and_flags(self):
        for p in self.pos["patterns"]:
            for k in ("id", "kind", "title", "count", "transcripts", "sessions", "projects", "tokens", "api_cost_usd",
                      "examples", "suggestion", "confidence", "evidence"):
                self.assertIn(k, p)
            self.assertLessEqual(len(p["examples"]), 5)
            s = p["suggestion"]
            if s is not None:
                self.assertIn(s["type"], pt.SUGGESTION_TYPES)
        self.assertEqual(set(self.pos["counts"]), set(pt.MINERS))
        top = run(os.path.join(self.tmp.name, "pos"), "--top", "2")
        self.assertEqual(len(top["patterns"]), 2)
        strict = run(os.path.join(self.tmp.name, "pos"), "--min-support", "9")
        self.assertFalse([p for p in strict["patterns"] if p["id"].startswith(("seq:", "start:"))])


if __name__ == "__main__":
    unittest.main()
