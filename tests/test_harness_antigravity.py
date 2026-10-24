import json
import os
import tempfile
import unittest

from tokenscope.analyze import analyze_session, tool_kind, tool_label, tool_target
from tokenscope.harness import AntigravityHarness, ClaudeHarness, get_harness
from tokenscope.live.state import LiveState
from tokenscope.live.tail import Tailer, TrackedFile
from tokenscope.parse import Session, discover, find_session
from tokenscope.pricing import cost_usd, price_of


class TestAntigravityHarness(unittest.TestCase):
    def setUp(self):
        self.harness = AntigravityHarness()

    def test_gemini_pricing(self):
        # Gemini 3.8 Flash
        p = price_of("gemini-3.8-flash")
        self.assertIsNotNone(p)
        self.assertEqual(p, (0.15, 0.20, 0.20, 0.0375, 0.60))

        # Gemini 4 Argon
        p_argon = price_of("gemini-4-argon")
        self.assertEqual(p_argon, (2.00, 2.00, 2.00, 0.10, 10.00))
        self.assertEqual(price_of("Gemini 4 Argon"), p_argon)

        # Human-formatted name from settings.json
        p_human = price_of("Gemini 3.8 Flash (High)")
        self.assertEqual(p_human, p)

        # Gemini Pro and Flash variants
        self.assertIsNotNone(price_of("gemini-2.0-flash"))
        self.assertIsNotNone(price_of("gemini-1.5-pro"))
        self.assertIsNotNone(price_of("gemini-pro"))

        # Cost calculation
        u = {"input": 10000, "cache_write": 0, "cache_read": 50000, "output": 1000}
        usd = cost_usd("gemini-3.8-flash", u)
        self.assertAlmostEqual(usd, (10000 * 0.15 + 50000 * 0.0375 + 1000 * 0.60) / 1e6)

    def test_tool_mapping(self):
        self.assertEqual(tool_kind("view_file"), "file_read")
        self.assertEqual(tool_kind("run_command"), "bash")
        self.assertEqual(tool_kind("replace_file_content"), "edit")
        self.assertEqual(tool_kind("write_to_file"), "edit")
        self.assertEqual(tool_kind("search_web"), "search")
        self.assertEqual(tool_kind("read_url_content"), "search")
        self.assertEqual(tool_kind("grep_search"), "search")
        self.assertEqual(tool_kind("invoke_subagent"), "subagent_result")

        # tool_target
        self.assertEqual(tool_target("run_command", {"CommandLine": "ls -la"}, "/work"), "ls -la")
        self.assertEqual(tool_target("view_file", {"AbsolutePath": "/work/file.py"}, "/work"), "file.py")
        self.assertEqual(tool_target("search_web", {"query": "python playwright"}, "/work"), "python playwright")
        self.assertEqual(tool_label("run_command", "ls -la"), "Bash: ls -la")

    def test_canonical_conversion(self):
        open_tools = []
        user_line = {
            "step_index": 0,
            "type": "USER_INPUT",
            "content": "<USER_REQUEST>\nFix the parser bug\n</USER_REQUEST>",
            "created_at": "2026-10-18T05:00:00Z"
        }
        cev_user = self.harness.to_canonical_event(user_line, open_tools)
        self.assertEqual(cev_user["type"], "user")
        self.assertEqual(cev_user["message"]["content"], "Fix the parser bug")

        planner_line = {
            "step_index": 1,
            "type": "PLANNER_RESPONSE",
            "created_at": "2026-10-18T05:00:05Z",
            "input_tokens": 12000,
            "cache_read_tokens": 5000,
            "output_tokens": 200,
            "thinking": "Need to run tests",
            "tool_calls": [{
                "name": "run_command",
                "args": {"CommandLine": "\"pytest tests/\"", "Cwd": "\"/home/user/repo\"", "toolSummary": "\"Run test suite\""}
            }]
        }
        cev_assistant = self.harness.to_canonical_event(planner_line, open_tools)
        self.assertEqual(cev_assistant["type"], "assistant")
        self.assertEqual(cev_assistant["message"]["usage"]["input_tokens"], 12000)
        tool_use = cev_assistant["message"]["content"][1]
        self.assertEqual(tool_use["input"]["command"], "pytest tests/")
        self.assertEqual(tool_use["input"]["toolSummary"], "Run test suite")
        self.assertEqual(tool_use["input"]["summary"], "Run test suite")
        self.assertEqual(len(open_tools), 1)
        self.assertEqual(open_tools[0][0], "call_1_0")

        # LiveState tool_label and tool_target helpers
        from tokenscope.live import state as livestate
        rec = {"cmd": "pytest tests/", "segs": ["pytest tests/"]}
        self.assertEqual(livestate.tool_label("run_command", tool_use["input"], rec), "run_command pytest")
        self.assertEqual(livestate.tool_target("run_command", tool_use["input"], rec, redact=False), "Run test suite")
        self.assertEqual(livestate.tool_target("run_command", tool_use["input"], rec, redact=True), "pytest")

        # Command without summary (e.g. bash commands or run_command without toolSummary)
        rec_py = {"cmd": "python3 train.py --lr 0.01", "segs": ["python3 train.py --lr 0.01"]}
        self.assertEqual(livestate.tool_label("run_command", {"command": "python3 train.py"}, rec_py), "run_command python3 train.py")
        self.assertEqual(livestate.tool_target("run_command", {"command": "python3 train.py"}, rec_py, redact=False), "python3 train.py")
        self.assertEqual(livestate.tool_label("Bash", {"command": "pip install requests"}, {"cmd": "pip install requests", "segs": ["pip install requests"]}), "Bash pip install")

        tool_result_line = {
            "step_index": 2,
            "type": "GENERIC",
            "created_at": "2026-10-18T05:00:10Z",
            "content": "5 passed, 0 failed"
        }
        cev_result = self.harness.to_canonical_event(tool_result_line, open_tools)
        self.assertEqual(cev_result["type"], "user")
        res_block = cev_result["message"]["content"][0]
        self.assertEqual(res_block["type"], "tool_result")
        self.assertEqual(res_block["tool_use_id"], "call_1_0")
        self.assertEqual(res_block["content"], "5 passed, 0 failed")

    def test_synthetic_antigravity_session(self):
        with tempfile.TemporaryDirectory() as d:
            conv_id = "test-conv-0001"
            log_dir = os.path.join(d, conv_id, ".system_generated", "logs")
            os.makedirs(log_dir)
            transcript_path = os.path.join(log_dir, "transcript.jsonl")

            records = [
                {"step_index": 0, "type": "USER_INPUT", "created_at": "2026-10-18T05:00:00Z",
                 "content": "<USER_REQUEST>Add feature X</USER_REQUEST>"},
                {"step_index": 1, "type": "PLANNER_RESPONSE", "created_at": "2026-10-18T05:00:05Z",
                 "input_tokens": 5000, "output_tokens": 100, "cache_read_tokens": 1000,
                 "thinking": "Checking directory",
                 "tool_calls": [{"name": "run_command", "args": {"CommandLine": "\"ls -la\"", "Cwd": "\"/work/proj\""}}]},
                {"step_index": 2, "type": "GENERIC", "created_at": "2026-10-18T05:00:08Z",
                 "content": "file1.py  file2.py"},
                {"step_index": 3, "type": "PLANNER_RESPONSE", "created_at": "2026-10-18T05:00:15Z",
                 "input_tokens": 5200, "output_tokens": 150, "cache_read_tokens": 5000,
                 "content": "Done! I examined file1 and file2."}
            ]

            with open(transcript_path, "w", encoding="utf-8") as f:
                for r in records:
                    f.write(json.dumps(r) + "\n")

            # Discovery
            sessions = self.harness.discover(d)
            self.assertEqual(len(sessions), 1)
            s = sessions[0]
            self.assertEqual(s.id, conv_id)

            # Loading & Analysis
            s.load()
            self.assertEqual(len(s.events), 4)
            self.assertEqual(s.first_prompt, "Add feature X")

            a = analyze_session(s)
            self.assertEqual(len(a["calls"]), 2)
            self.assertEqual(a["totals"]["all"]["calls"], 2)
            self.assertGreater(a["totals"]["all"]["cost_weighted"], 0)

            # LiveState ingestion
            ls = LiveState(redact=False)
            tf = TrackedFile(transcript_path, conv_id, "proj")
            ls.ingest(tf, records)
            b = ls.board(now=ls.sessions[conv_id].last_ts + 1)
            self.assertEqual(len(b["sessions"]), 1)
            card = b["sessions"][0]
            self.assertEqual(b["harness"], "antigravity")
            self.assertEqual(b["harness_label"], "Google Antigravity")

            # Test live server board.html replacement
            import threading
            import urllib.request
            from tokenscope.live.server import Live, make_server
            live = Live(d, harness="antigravity", redact=False, since_minutes=120)
            srv = make_server(live, "127.0.0.1", 0)
            port = srv.server_address[1]
            t = threading.Thread(target=srv.serve_forever, daemon=True)
            t.start()
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/") as resp:
                    html = resp.read().decode("utf-8")
                    self.assertIn("Google Antigravity sessions on this machine", html)
                    self.assertNotIn("Claude Code sessions on this machine", html)
            finally:
                srv.shutdown()
                srv.server_close()


if __name__ == "__main__":
    unittest.main()
