"""Step 8a: nested subagents + workflow journals, compaction, parse cache, pricing, request sources, CLI scale flags."""
import contextlib
import io
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))

import synth  # noqa: E402
from tokenscope import cli, parse, pricing  # noqa: E402
from tokenscope import understand as un  # noqa: E402
from tokenscope.analyze import analyze_session  # noqa: E402

SID = "cccccccc-0000-0000-0000-000000000003"
SLUG = "-work-shop"


def _write(path, lines):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for x in lines:
            f.write((x if isinstance(x, str) else json.dumps(x)) + "\n")


def _agent(agent_id, mid, usage, model="claude-sonnet-4-5"):
    w = synth._W(SID)
    w.prompt(0, "Triage item %s" % agent_id)
    w.call(1, mid, usage)
    for x in w.lines:
        x["isSidechain"], x["agentId"] = True, agent_id
        if x["type"] == "assistant":
            x["message"]["model"] = model
    return w.lines


def make_layout(root):
    """Main session + one direct subagent + two workflow subagents (one nested a level deeper) + a journal."""
    w = synth._W(SID)
    w.prompt(0, "Run the triage workflow")
    w.call(1, "p0", (5, 1000, 0, 10), ("Bash", {"command": "npm test"}))
    w.result(2, "p0", "x" * 50000 + "\n3 passing\n1 failing")
    w.prompt(3, "<task-notification>agent finished</task-notification>")
    w.call(4, "p1", (0, 100, 1005, 10))
    lines = [json.dumps({"type": "file-history-snapshot", "snapshot": {"big": "y" * 1000}}), "{not json",
             json.dumps({"type": "queue-operation", "operation": "enqueue", "content": "hi"})] + w.lines
    _write(os.path.join(root, SLUG, SID + ".jsonl"), lines)
    sub = os.path.join(root, SLUG, SID, "subagents")
    _write(os.path.join(sub, "agent-a1.jsonl"), _agent("a1", "s1", (5, 500, 0, 20)))
    _write(os.path.join(sub, "agent-a1.meta.json"), [{"agentType": "Explore", "description": "look around"}])
    wf = os.path.join(sub, "workflows", "wf_abc123")
    for aid, mid, deeper in (("b1", "s2", ""), ("b2", "s3", "nested")):
        d = os.path.join(wf, deeper) if deeper else wf
        _write(os.path.join(d, "agent-%s.jsonl" % aid), _agent(aid, mid, (5, 400, 100, 20)))
        _write(os.path.join(d, "agent-%s.meta.json" % aid),
               [{"agentType": "workflow-subagent", "description": "triage:%s" % aid, "workflowPhase": "Triage",
                 "spawnDepth": 1}])
    _write(os.path.join(wf, "journal.jsonl"), [
        {"type": "launched"}, {"type": "started", "agentId": "b1", "label": "triage:b1", "phase": "Triage"},
        {"type": "started", "agentId": "b2", "label": "triage:b2", "phase": "Triage"},
        {"type": "result", "agentId": "b1", "result": {"secret": "payload"}}, {"type": "failed", "agentId": "b2"}])
    return root


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = make_layout(os.path.join(self.tmp.name, "projects"))
        parse.set_cache(True, os.path.join(self.tmp.name, "cache"))

    def tearDown(self):
        parse.set_cache(True, parse.CACHE_DIR)
        self.tmp.cleanup()

    def load(self):
        return parse.find_session(SID[:8], self.root)


class TestLayout(Base):
    def test_recursive_subagents_and_meta(self):
        s = self.load()
        self.assertEqual(sorted(sa["agent_id"] for sa in s.subagents), ["a1", "b1", "b2"])
        by = {sa["agent_id"]: sa for sa in s.subagents}
        self.assertIsNone(by["a1"]["workflow_id"])
        self.assertEqual(by["a1"]["agent_type"], "Explore")
        self.assertEqual(by["b2"]["workflow_id"], "wf_abc123")
        self.assertEqual((by["b1"]["workflow_phase"], by["b1"]["spawn_depth"]), ("Triage", 1))
        self.assertFalse(any(sa["path"].endswith("journal.jsonl") for sa in s.subagents))

    def test_journal_counts_only(self):
        s = self.load()
        self.assertEqual(len(s.workflows), 1)
        w = s.workflows[0]
        self.assertEqual((w["id"], w["launched"], w["started"], w["results"], w["failed"]),
                         ("wf_abc123", 1, 2, 1, 1))
        self.assertEqual(dict(w["phases"]), {"Triage": 2})
        self.assertEqual(w["agents"], ["b1", "b2"])
        self.assertNotIn("payload", repr(w))

    def test_analysis_totals_include_nested(self):
        a = analyze_session(self.load())
        self.assertEqual(a["totals"]["subagents"]["calls"], 3)
        self.assertEqual(a["workflows"][0]["phases"], {"Triage": 2})
        wf = [x for x in a["subagents"] if x["workflow_id"]]
        self.assertEqual({x["workflow_phase"] for x in wf}, {"Triage"})


class TestCompaction(Base):
    def test_irrelevant_and_bad_lines_skipped(self):
        s = self.load()
        types = {e.get("type") for e in s.events}
        self.assertNotIn("file-history-snapshot", types)
        self.assertNotIn("queue-operation", types)
        self.assertTrue(parse._skip_line(b'{"type":"file-history-snapshot","x":1}\n'))
        self.assertFalse(parse._skip_line(b'{"type":"user"}'))

    def test_big_result_cut_but_measured(self):
        s = self.load()
        res = [b for e in s.events if e["type"] == "user" and isinstance(e["message"]["content"], list)
               for b in e["message"]["content"] if b.get("type") == "tool_result"][0]
        self.assertLess(len(res["content"]), 4000)
        self.assertEqual(res["_chars"], 50000 + len("\n3 passing\n1 failing"))
        self.assertTrue(res["content"].endswith("1 failing"))  # Bash tail kept for test parsing
        a = analyze_session(s)
        r = a["calls"][1]["tool_results"][0]
        self.assertEqual(r["chars"], res["_chars"])
        u = un.understand([s], analyses={s.id: a}, include_automated=True)
        st = u["sessions"][0]["requests"][0]["steps"][0]
        self.assertEqual(st["tests"]["failed"], 1)


class TestCache(Base):
    def test_hits_and_invalidation(self):
        before = dict(parse.STATS)
        self.load()
        mid = dict(parse.STATS)
        self.assertEqual(mid["misses"] - before["misses"], 4)
        self.load()
        self.assertEqual(parse.STATS["hits"] - mid["hits"], 4)
        p = os.path.join(self.root, SLUG, SID + ".jsonl")
        with open(p, "a") as f:
            f.write(json.dumps({"type": "user", "message": {"content": "more"}}) + "\n")
        after = dict(parse.STATS)
        s = self.load()
        self.assertEqual(parse.STATS["misses"] - after["misses"], 1)
        self.assertEqual(s.events[-1]["message"]["content"], "more")

    def test_no_cache_writes_nothing(self):
        parse.set_cache(False)
        self.load()
        self.assertFalse(os.path.exists(os.path.join(self.tmp.name, "cache")))


class TestPricing(unittest.TestCase):
    def test_prefix_match(self):
        self.assertEqual(pricing.price_of("claude-opus-5-5"), pricing.PRICES["claude-opus-5-5"])
        self.assertEqual(pricing.price_of("claude-opus-5"), pricing.PRICES["claude-opus-5"])
        self.assertEqual(pricing.price_of("claude-haiku-4-5-20251001"), pricing.PRICES["claude-haiku-4-5"])
        self.assertEqual(pricing.price_of("claude-fable-5-1[1m]"), pricing.PRICES["claude-fable-5-1"])
        self.assertIsNone(pricing.price_of("claude-opus-5-7"))  # not silently priced as opus-5
        self.assertIsNone(pricing.price_of("<synthetic>"))
        self.assertIsNone(pricing.price_of("claude-test-1"))

    def test_cost_split_and_weighted(self):
        u = {"input": 1_000_000, "cache_write": 3_000_000, "cache_write_1h": 1_000_000, "cache_read": 10_000_000,
             "output": 1_000_000}
        # sonnet-4-5: 3 + 2M x 3.75 + 1M x 6 + 10M x 0.3 + 15
        self.assertAlmostEqual(pricing.cost_usd("claude-sonnet-4-5", u), 3 + 7.5 + 6 + 3 + 15)
        self.assertIsNone(pricing.cost_usd("mystery", u))
        self.assertEqual(pricing.cost_weighted(u), 1e6 + 3.75e6 + 1e6 + 5e6)

    def test_session_totals(self):
        with tempfile.TemporaryDirectory() as d:
            root = make_layout(d)
            parse.set_cache(False)
            try:
                a = analyze_session(parse.find_session(SID[:8], root))
            finally:
                parse.set_cache(True)
        t = a["totals"]["all"]
        self.assertEqual(t["unpriced_calls"], 2)  # main calls use claude-test-1
        self.assertEqual(t["unpriced_models"], ["claude-test-1"])
        sub = a["totals"]["subagents"]
        exp = sum(pricing.cost_usd("claude-sonnet-4-5", c["usage"]) for s in a["subagents"]
                  for c in s["call_records"])
        self.assertAlmostEqual(sub["api_cost_usd"], exp, places=6)
        self.assertEqual(t["new"], t["fresh_input"] + t["cache_write"])
        self.assertAlmostEqual(t["cost_weighted"], t["fresh_input"] + 1.25 * t["cache_write"]
                               + 0.1 * t["cache_read"] + 5 * t["output"], places=0)


class TestSources(Base):
    def test_rules(self):
        ps = parse.prompt_source
        self.assertEqual(ps({}, "fix it"), "human")
        self.assertEqual(ps({}, "<task-notification>x"), "notification")
        self.assertEqual(ps({}, "[SYSTEM] wake up"), "notification")
        self.assertEqual(ps({"isSidechain": True}, "hi"), "subagent")
        self.assertEqual(ps({}, "hi", in_subagent=True), "subagent")
        self.assertEqual(ps({"entrypoint": "sdk-cli"}, "hi"), "headless")
        self.assertEqual(ps({"entrypoint": "cli"}, "hi"), "human")
        self.assertEqual(ps({"origin": {"kind": "hook"}}, "hi"), "hook")
        self.assertEqual(ps({"origin": "user"}, "hi"), "human")

    def test_goals_use_human_requests(self):
        s = self.load()
        a = analyze_session(s)
        u = un.understand([s], analyses={s.id: a})
        self.assertEqual(u["goal_request_filter"], "human")
        self.assertEqual(u["requests"], 1)
        self.assertEqual(u["request_sources"]["notification"], 1)
        self.assertEqual(u["request_sources"]["subagent"], 3)
        self.assertEqual(len(u["sessions"][0]["automated_requests"]), 1)
        u2 = un.understand([self.load()], include_automated=True)
        self.assertEqual(u2["requests"], 2)

    def test_fallback_when_no_human(self):
        s = self.load()
        for e in s.events:
            e["entrypoint"] = "sdk-cli"
        u = un.understand([s])
        self.assertTrue(u["goal_request_filter"].startswith("all (no human"))
        self.assertEqual(u["requests"], 2)


class TestCli(Base):
    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = cli.main(list(argv))
        return rc, out.getvalue(), err.getvalue()

    def _many(self):
        d = os.path.join(self.tmp.name, "many")
        synth.make(d)
        for i in range(6):
            w = synth._W("dddddddd-0000-0000-0000-00000000000%d" % i)
            w.prompt(0, "task %d" % i)
            w.call(1, "q%d" % i, (5, 100 * (i + 1), 0, 10))
            w.write(d)
        return d

    def ids(self, *extra):
        rc, out, _ = self.run_cli("sessions", "--root", self._many(), "--json", *extra)
        self.assertEqual(rc, 0)
        return [r["session_id"] for r in json.loads(out)["sessions"]]

    def test_sample_and_max(self):
        self.assertEqual(len(self.ids()), 8)
        a, b = self.ids("--sample", "3"), self.ids("--sample", "3")
        self.assertEqual(len(a), 3)
        self.assertEqual(a, b)
        self.assertEqual(len(self.ids("--max-sessions", "2")), 2)

    def test_jobs_same_result_and_verbose(self):
        root = self._many()
        _, o1, _ = self.run_cli("goals", "--root", root, "--json", "--jobs", "1", "--no-cache")
        rc, o2, err = self.run_cli("goals", "--root", root, "--json", "--jobs", "3", "-v")
        self.assertEqual(rc, 0)
        strip = lambda o: [(g["goal"], g["runs"]) for g in json.loads(o)["goals"]]
        self.assertEqual(strip(o1), strip(o2))
        self.assertRegex(err, r"tokenscope: 8 sessions, 8 transcripts, \d+ calls, [\d.]+s, jobs \d+, cache")


if __name__ == "__main__":
    unittest.main()
