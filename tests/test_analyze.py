import io
import json
import os
import sys
import unittest
from contextlib import redirect_stdout

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tokenscope import cli  # noqa: E402
from tokenscope.analyze import analyze_session, classify_intent  # noqa: E402
from tokenscope.parse import discover, find_session  # noqa: E402

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "projects")
SID = "11111111-2222-3333-4444-555555555555"


def causes(call, cause):
    return [c for c in call["attribution"] if c["cause"] == cause]


class TestParse(unittest.TestCase):
    def test_discover_and_prefix(self):
        ids = sorted(s.id for s in discover(ROOT))
        self.assertEqual(ids, ["11111111-2222-3333-4444-555555555555", "99999999-0000-0000-0000-000000000000"])
        s = find_session("1111", ROOT)
        self.assertEqual(s.id, SID)
        self.assertEqual(s.project, "demo")
        self.assertEqual(s.git_branch, "main")
        self.assertEqual(s.first_prompt, "Please fix the crash in the parser")  # /clear command skipped
        self.assertEqual(len(s.subagents), 1)
        with self.assertRaises(LookupError):
            find_session("nope", ROOT)


class TestAnalyze(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.a = analyze_session(find_session(SID, ROOT))

    def test_dedupe_by_message_id(self):
        calls = self.a["calls"]
        self.assertEqual([c["msg_id"] for c in calls], ["msg_A", "msg_B", "msg_C", "msg_D"])
        self.assertEqual([t["name"] for t in calls[0]["tool_uses"]], ["Read", "Agent"])
        m = self.a["totals"]["main"]
        self.assertEqual((m["fresh_input"], m["cache_write"], m["cache_read"], m["output"]), (23, 2800, 1010, 110))
        self.assertEqual(m["cache_write_1h"], 2800)
        s = self.a["totals"]["subagents"]
        self.assertEqual((s["calls"], s["cache_write"], s["cache_write_5m"], s["output"]), (2, 540, 540, 40))
        self.assertEqual(self.a["totals"]["all"]["all_in"], 23 + 2800 + 1010 + 110 + 4 + 540 + 500 + 40)

    def test_attribution_sums_to_new_tokens(self):
        for c in self.a["calls"] + self.a["subagents"][0]["call_records"]:
            self.assertEqual(sum(x["tokens"] for x in c["attribution"]), c["new_tokens"], c["msg_id"])

    def test_first_call_overhead(self):
        c0 = self.a["calls"][0]
        self.assertEqual(c0["new_tokens"], 1010)
        labels = {x["label"] for x in causes(c0, "system_overhead")}
        self.assertTrue({"system prompt", "skill listing", "CLAUDE.md / instructions",
                         "tool schemas / other (unattributed)"} <= labels)
        self.assertTrue(all(x["est"] for x in c0["attribution"]))

    def test_tool_results_feed_next_call(self):
        c1 = self.a["calls"][1]
        self.assertEqual(c1["gap_seconds"], 8.0)
        res = {r["tool"]: r for r in c1["tool_results"]}
        self.assertEqual(res["Read"]["target"], "src/parser.py")
        self.assertEqual(res["Read"]["chars"], 400)
        self.assertEqual(res["Read"]["est_tokens"], 100)
        self.assertEqual(res["Agent"]["kind"], "subagent_result")
        self.assertEqual(causes(c1, "assistant_output")[0]["tokens"], 50)
        self.assertIsNone(c1["cache_miss"])
        tr = causes(c1, "tool_result")
        self.assertEqual(len(tr), 2)
        self.assertLessEqual(sum(x["tokens"] for x in tr), c1["new_tokens"])

    def test_cache_miss_after_ttl(self):
        c2 = self.a["calls"][2]
        self.assertEqual(c2["cache_miss"]["reason"], "ttl_expired")
        self.assertEqual(c2["cache_miss"]["ttl_seconds"], 3600)
        self.assertEqual(causes(c2, "cache_miss")[0]["tokens"], 1215)
        bash = c2["tool_results"][0]
        self.assertTrue(bash["persisted"])
        self.assertEqual(bash["full_chars"], 51200)
        self.assertEqual(len(self.a["cache_misses"]), 1)
        self.assertEqual(self.a["big_bash_outputs"][0]["command"], "pytest -q")
        self.assertEqual(self.a["idle_gaps"][0]["gap_seconds"], 7210.0)

    def test_compaction(self):
        c3 = self.a["calls"][3]
        self.assertEqual(c3["compaction"]["source"], "compact_boundary")
        self.assertEqual(c3["compaction"]["summary_chars"], 35)
        self.assertEqual(len(self.a["compactions"]), 1)
        self.assertIsNone(c3["cache_miss"])

    def test_subagent_linked(self):
        sa = self.a["subagents"][0]
        self.assertEqual((sa["agent_id"], sa["type"], sa["tool_use_id"]), ("agent1", "Explore", "tu_agent"))
        self.assertEqual(sa["parent_call_index"], 0)
        self.assertEqual(sa["calls"], 2)
        self.assertEqual(self.a["per_tool"]["Grep"]["calls"], 1)

    def test_per_file_and_tools(self):
        f = self.a["per_file"]["src/parser.py"]
        self.assertEqual((f["reads"], f["result_tokens_est"], f["repeated_reads"]), (1, 100, 0))
        self.assertEqual(self.a["per_tool"]["Bash"]["calls"], 1)

    def test_offered_vs_used(self):
        o = self.a["offered_vs_used"]
        self.assertTrue(o["available"])
        self.assertEqual([e["name"] for e in o["mcp_servers"]], ["tracker"])
        self.assertFalse(o["mcp_servers"][0]["used"])
        self.assertEqual(o["mcp_servers"][0]["tools_offered"], ["create_issue", "list_issues"])
        self.assertEqual(sorted(e["name"] for e in o["skills"]), ["pdf", "plugin:review"])
        used = {e["name"]: e["used"] for e in o["subagent_types"]}
        self.assertEqual(used, {"Explore": True, "Plan": False})

    def test_no_offerings(self):
        a = analyze_session(find_session("9999", ROOT))
        self.assertFalse(a["offered_vs_used"]["available"])
        self.assertIn("reason", a["offered_vs_used"])
        self.assertEqual(a["intent"]["intent"], "exploration")


class TestIntent(unittest.TestCase):
    def test_rules(self):
        cases = {
            "Please fix the crash": ("bugfix", "fix"),
            "Tests are failing on CI": ("bugfix", "failing"),
            "Add support for discount codes": ("feature", "add"),
            "Refactor the cart module": ("refactor", "refactor"),
            "Update the README": ("docs", "readme"),
            "Explain the cache": ("exploration", "explain"),
            "Review my PR": ("review", "review"),
            "hello there": ("other", None),
            "Please address the README": ("docs", "readme"),  # 'address' is not 'add'
            "Write fixtures for the parser": ("other", None),  # 'fixtures' is not 'fix'
            "Fixes section layout; explain it": ("exploration", "explain"),
            "Explain the cache. " + "x " * 200 + "then fix it": ("exploration", "explain"),
            "Hello. " + "x " * 200 + "fix the crash": ("other", None),  # beyond the first 300 chars
        }
        for prompt, (intent, kw) in cases.items():
            from tokenscope.analyze import _classify_intent_legacy
            r = _classify_intent_legacy(prompt)
            self.assertEqual((r["intent"], r["keyword"]), (intent, kw), prompt)


class TestCli(unittest.TestCase):
    def run_cli(self, *argv):
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = cli.main(list(argv))
        return rc, buf.getvalue()

    def test_sessions_and_analyze(self):
        rc, out = self.run_cli("sessions", "--root", ROOT)
        self.assertEqual(rc, 0)
        self.assertIn("11111111", out)
        self.assertIn("bugfix", out)  # session kind (keyword rule)
        rc, out = self.run_cli("sessions", "--root", ROOT, "--since", "2026-09-15")
        self.assertNotIn("99999999", out)
        rc, out = self.run_cli("analyze", "1111", "--root", ROOT, "--json")
        self.assertEqual(json.loads(out)["session_id"], SID)
        rc, out = self.run_cli("analyze", "1111", "--root", ROOT)
        self.assertIn("CACHE MISS", out)
        rc, out = self.run_cli("compare", "--root", ROOT, "9999", "1111")
        self.assertEqual(rc, 0)
        self.assertIn("all-in tokens", out)


if __name__ == "__main__":
    unittest.main()
