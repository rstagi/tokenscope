"""OpenAI Codex harness (tokenscope/codex.py + CodexHarness): the record converter, discovery and harness selection,
analysis and pricing, and the CLI, over the hand-written rollouts in tests/fixtures/codex.

Hermetic: no test reads HOME, ~/.codex or ~/.claude; roots are the fixtures or temp dirs, the parse cache is off."""
import copy
import io
import json
import os
import random
import shutil
import sys
import tempfile
import unittest
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from tokenscope import cli, codex, parse  # noqa: E402
from tokenscope.analyze import analyze_session, tool_kind  # noqa: E402
from tokenscope.codex import CodexConverter, canonical_usage, map_tool  # noqa: E402
from tokenscope.harness import HARNESSES, CodexHarness, get_harness  # noqa: E402
from tokenscope.pricing import cost_usd, price_of  # noqa: E402
import codex_synth  # noqa: E402

FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "codex")
ROOT = os.path.join(FIXTURES, "sessions")
MALFORMED_ROOT = os.path.join(FIXTURES, "malformed")
MAIN, SUB, LEGACY, BAD = (codex_synth.sid(n) for n in (1, 2, 3, 4))
MODERN_PATH = os.path.join(ROOT, "2026", "10", "20", "rollout-2026-10-20T09-00-00-%s.jsonl" % MAIN)
SUB_PATH = os.path.join(ROOT, "2026", "10", "20", "rollout-2026-10-20T09-00-31-%s.jsonl" % SUB)
LEGACY_PATH = os.path.join(ROOT, "2026", "10", "19", "rollout-2026-10-19T08-00-00-%s.jsonl" % LEGACY)
BAD_PATH = os.path.join(MALFORMED_ROOT, "2026", "10", "21", "rollout-2026-10-21T10-00-00-%s.jsonl" % BAD)
ENV_KEYS = ("TOKENSCOPE_ROOT", "TOKENSCOPE_HARNESS", "CODEX_HOME")
_CACHE = {}
_ENV = {}


def setUpModule():
    """No parse cache (and a temp cache dir if a CLI command turns it on); harness / root variables pinned to an
    empty temp root, so a call without an explicit root (auto-detection, `alerts <path>`) never looks at the home."""
    _CACHE.update(parse.CACHE)
    _ENV["root"], _ENV["cache"] = tempfile.mkdtemp(), tempfile.mkdtemp()
    parse.set_cache(False, _ENV["cache"])
    _ENV["patch"] = mock.patch.dict(os.environ, {"TOKENSCOPE_ROOT": _ENV["root"]})
    _ENV["patch"].start()
    for k in ("TOKENSCOPE_HARNESS", "CODEX_HOME"):
        os.environ.pop(k, None)


def tearDownModule():
    parse.CACHE.update(_CACHE)
    _ENV["patch"].stop()
    shutil.rmtree(_ENV["root"], ignore_errors=True)
    shutil.rmtree(_ENV["cache"], ignore_errors=True)


@contextmanager
def clean_env(**env):
    """os.environ without the harness / root variables, plus `env`."""
    with mock.patch.dict(os.environ, env):
        for k in ENV_KEYS:
            if k not in env:
                os.environ.pop(k, None)
        yield


def records(path):
    return list(parse.read_jsonl(path))


def convert(recs, use_usage_records=False):
    conv = CodexConverter(use_usage_records=use_usage_records)
    out = []
    for r in recs:
        out.extend(conv.feed(r))
    return out + conv.finish()


def shape(events):
    """Short tuples of canonical events: ("prompt", text) / ("assistant", id, model, stop, usage, blocks) /
    ("result", tool_use_id, is_error, text)."""
    out = []
    for e in events:
        m = e["message"]
        if e["type"] == "user" and isinstance(m["content"], str):
            out.append(("prompt", m["content"]))
        elif e["type"] == "user":
            b = m["content"][0]
            out.append(("result", b["tool_use_id"], b["is_error"], b["content"]))
        else:
            u = m["usage"]
            blocks = [(b["type"], b.get("id"), b.get("name"), b.get("input")) if b["type"] == "tool_use"
                      else (b["type"], b.get("text", b.get("thinking"))) for b in m["content"]]
            out.append(("assistant", m["id"], m["model"], m.get("stop_reason"),
                        (u["input_tokens"], u["cache_read_input_tokens"], u["cache_creation_input_tokens"],
                         u["output_tokens"]), blocks))
    return out


def usage_sum(events):
    """(input, cache read, cache write, output) summed over the last usage of each message id."""
    last = {}
    for e in events:
        if e["type"] == "assistant":
            last[e["message"]["id"]] = e["message"]["usage"]
    keys = ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens", "output_tokens")
    return tuple(sum(u[k] for u in last.values()) for k in keys)


def rec(typ, payload, ts="2026-10-20T09:00:00.000Z"):
    return {"timestamp": ts, "type": typ, "payload": payload}


def item(payload):
    return rec("response_item", payload)


def call(name, args, cid):
    return item({"type": "function_call", "name": name, "arguments": json.dumps(args), "call_id": cid})


def output(cid, text):
    return item({"type": "function_call_output", "call_id": cid, "output": text})


def usage_rec(rid, inp, cached, out):
    return rec("token_usage_record", {"response_id": rid, "usage": {
        "input_tokens": inp, "cached_input_tokens": cached, "output_tokens": out}})


def run_cli(*argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = cli.main(list(argv))
    return code, out.getvalue(), err.getvalue()


# ---------------------------------------------------------------- converter

class TestConverterModern(unittest.TestCase):
    def setUp(self):
        self.events = convert(records(MODERN_PATH), use_usage_records=True)

    def test_exact_events(self):
        m = "gpt-6.1-sol"
        result_2 = " M src/parser.py\ndef split(s):\n    return s.split()\n"
        patched = "Success. Updated the following files:\nM src/parser.py\nA tests/test_split.py\n"
        self.assertEqual(shape(self.events), [
            ("prompt", "Fix the failing parser test"),
            ("assistant", "resp_1", m, "tool_use", (2000, 8000, 0, 500), [
                ("thinking", ""), ("text", "I will run the parser tests first."),
                ("tool_use", "call_1", "Bash", {"command": "python3 -m unittest tests.test_parser"})]),
            ("result", "call_1", False, "FAIL: test_split (tests.test_parser.T)\nFAILED (failures=1)\n"),
            ("assistant", "resp_2", m, "tool_use", (2000, 10000, 0, 300), [
                ("tool_use", "call_2", "Bash", {"command": "git status --short"}),
                ("tool_use", "call_2#2", "Bash", {"command": "sed -n '1,40p' src/parser.py"})]),
            ("result", "call_2", False, result_2),
            ("result", "call_2#2", False, result_2),
            ("assistant", "resp_3", m, "tool_use", (2000, 12000, 0, 800), [
                ("tool_use", "call_3", "Edit", {"file_path": "src/parser.py"}),
                ("tool_use", "call_3#2", "Write", {"file_path": "tests/test_split.py"})]),
            ("result", "call_3", False, patched),
            ("result", "call_3#2", False, patched),
            ("assistant", "resp_4", m, "tool_use", (2000, 14000, 0, 200), [
                ("tool_use", "call_4", "TodoWrite", {"todos": [
                    {"content": "Fix the parser split", "status": "completed", "activeForm": "Fix the parser split"},
                    {"content": "Run the parser tests", "status": "in_progress",
                     "activeForm": "Run the parser tests"},
                    {"content": "Update the changelog", "status": "pending", "activeForm": "Update the changelog"}]}),
                ("tool_use", "call_5", "mcp__docs__search", {"query": "tokenize split"}),
                ("tool_use", "ws_1", "WebSearch", {"query": "python str.split separator"})]),
            ("result", "call_4", False, "Plan updated"),
            ("result", "call_5", False, "split(sep) splits on sep"),
            ("assistant", "resp_5", m, "tool_use", (2000, 16000, 0, 150), [
                ("tool_use", "call_6", "Agent", {"subagent_type": "subagent"})]),
            ("result", "call_6", False, json.dumps({"agent_id": SUB}, separators=(",", ":"))),
            ("assistant", "resp_6", m, "end_turn", (2000, 18000, 0, 100), [
                ("text", "The parser test passes now.")]),
        ])

    def test_event_fields(self):
        uuids = [e["uuid"] for e in self.events]
        self.assertEqual(len(uuids), len(set(uuids)))
        for e in self.events:
            self.assertEqual((e["cwd"], e["sessionId"]), ("/home/user/project", MAIN))
            self.assertNotIn("agentId", e)
        first = next(e for e in self.events if e["type"] == "assistant")
        self.assertEqual(first["timestamp"], "2026-10-20T09:00:05.000Z")   # its first item (the reasoning)
        # the reasoning summary never reaches the event (it would reach step labels)
        self.assertEqual(first["message"]["content"][0], {"type": "thinking", "thinking": "", "signature": "codex"})
        self.assertNotIn("Running the parser tests", json.dumps(self.events))

    def test_scaffolding_dropped_prompt_kept_once(self):
        prompts = [e["message"]["content"] for e in self.events
                   if e["type"] == "user" and isinstance(e["message"]["content"], str)]
        self.assertEqual(prompts, ["Fix the failing parser test"])   # not the event_msg user_message copy
        blob = json.dumps(self.events)
        for scaffold in ("environment_context", "AGENTS.md", "permissions", "Run the tests before finishing"):
            self.assertNotIn(scaffold, blob)
        for prefix in codex.SCAFFOLD_PREFIXES:
            self.assertEqual(convert([item({"type": "message", "role": "user", "content": [
                {"type": "input_text", "text": "  " + prefix + " x"}]})]), [])

    def test_token_count_ignored_when_usage_records(self):
        want = (12000, 78000, 0, 2050)
        self.assertEqual(usage_sum(self.events), want)
        # streaming (live) converter: it learns about usage records on the way; same numbers
        self.assertEqual(usage_sum(convert(records(MODERN_PATH))), want)
        self.assertEqual(shape(convert(records(MODERN_PATH))), shape(self.events))

    def test_batch_read_events(self):
        evs = codex.read_events(MODERN_PATH)
        self.assertEqual([e["type"] for e in evs], [e["type"] for e in self.events])
        self.assertEqual(usage_sum(evs), (12000, 78000, 0, 2050))


class TestConverterLegacy(unittest.TestCase):
    def setUp(self):
        self.events = convert(records(LEGACY_PATH))

    def test_exact_events(self):
        m = "gpt-5-codex"
        self.assertEqual(shape(self.events), [
            ("prompt", "Make the calculator tests pass"),
            ("assistant", "codex_3", m, "tool_use", (2000, 6000, 0, 400), [
                ("thinking", ""), ("tool_use", "call_a", "Bash", {"command": "python3 -m unittest discover -s tests"})]),
            ("result", "call_a", True, "FAIL: test_add (tests.test_calc.T)\nFAILED (failures=1)\n"),
            ("assistant", "codex_7", m, "tool_use", (2000, 7000, 0, 300), [
                ("tool_use", "call_b", "Edit", {"file_path": "src/calc.py"})]),   # apply_patch heredoc in a shell
            ("result", "call_b", False, "Success. Updated the following files:\nM src/calc.py\n"),
            ("assistant", "codex_11", m, "tool_use", (2000, 8000, 0, 200), [
                ("tool_use", "call_c", "Bash", {"command": "python3 -m unittest discover -s tests"})]),   # local_shell
            ("result", "call_c", False, "Ran 3 tests in 0.01s\n\nOK\n"),
            ("assistant", "codex_14", m, "end_turn", (2000, 9000, 0, 100), [
                ("text", "All calculator tests pass.")]),   # usage from the total delta (no last_token_usage)
        ])
        self.assertEqual(usage_sum(self.events), (8000, 30000, 0, 1000))   # null info and the duplicate skipped

    def test_shell_argv(self):
        sc = codex.shell_command
        self.assertEqual(sc({"command": ["bash", "-lc", "ls -la | wc -l"]}), "ls -la | wc -l")
        self.assertEqual(sc({"command": ["/bin/zsh", "-c", "echo hi"]}), "echo hi")
        self.assertEqual(sc({"command": ["git", "commit", "-m", "two words"]}), "git commit -m 'two words'")
        self.assertEqual(sc({"command": "make test"}), "make test")
        self.assertEqual(sc({"cmd": "pytest -q"}), "pytest -q")
        self.assertEqual(sc({"command": 7}), "")

    def test_outputs(self):
        ot = codex.output_text
        self.assertEqual(ot(json.dumps({"output": "x", "metadata": {"exit_code": 2}})), ("x", True))
        self.assertEqual(ot(json.dumps({"output": "x", "metadata": {"exit_code": 0}})), ("x", False))
        self.assertEqual(ot({"content": "nope", "success": False}), ("nope", True))
        self.assertEqual(ot([{"type": "input_text", "text": "a"}, {"type": "input_text", "text": "b"}]), ("ab", False))
        self.assertEqual(ot("error: no such file"), ("error: no such file", True))
        self.assertEqual(ot("all good"), ("all good", False))
        self.assertEqual(ot('{"not": "an output"}'), ('{"not": "an output"}', False))
        self.assertEqual(ot(None), ("", False))


class TestToolMapping(unittest.TestCase):
    def test_canonical_names(self):
        self.assertEqual(map_tool("shell", "", {"command": ["bash", "-lc", "ls"]}), [("Bash", {"command": "ls"})])
        self.assertEqual(map_tool("exec_command", "", {"cmd": "ls"}), [("Bash", {"command": "ls"})])
        self.assertEqual(map_tool("functions.shell_command", "", {"command": "ls"}), [("Bash", {"command": "ls"})])
        self.assertEqual(map_tool("write_stdin", "", {"chars": "y\n", "session_id": 3}), [("BashOutput", {})])
        self.assertEqual(map_tool("read_file", "", {"path": "a.py"}), [("Read", {"file_path": "a.py"})])
        self.assertEqual(map_tool("grep_files", "", {"pattern": "def", "path": "src"}),
                         [("Grep", {"pattern": "def", "path": "src"})])
        self.assertEqual(map_tool("list_dir", "", {"dir_path": "src"}), [("Glob", {"pattern": "*", "path": "src"})])
        self.assertEqual(map_tool("web_search", "", {"query": "q"}), [("WebSearch", {"query": "q"})])
        self.assertEqual(map_tool("fetch", "", {"url": "https://example.com/x"}),
                         [("WebFetch", {"url": "https://example.com/x"})])
        self.assertEqual(map_tool("send_input", "", {"message": "free text"}), [("SendMessage", {})])
        self.assertEqual(map_tool("wait_agent", "", {"ids": ["a"]}), [("TaskOutput", {})])
        self.assertEqual(map_tool("close_agent", "", {"id": "a"}), [("TaskOutput", {})])

    def test_spawn_agent_input_is_fixed(self):
        args = {"prompt": "secret plan words", "task_name": "secret_task", "message": "m", "agent_type": "worker"}
        self.assertEqual(map_tool("spawn_agent", "", args), [("Agent", {"subagent_type": "subagent"})])

    def test_mcp_names(self):
        self.assertEqual(map_tool("mcp__docs__search", "", {"query": "q"}), [("mcp__docs__search", {"query": "q"})])
        self.assertEqual(map_tool("search", "mcp__docs__", {"query": "q"})[0][0], "mcp__docs__search")
        self.assertEqual(map_tool("search", "mcp__docs", {})[0][0], "mcp__docs__search")
        self.assertEqual(map_tool("docs__search", "docs", {})[0][0], "mcp__docs__search")
        big = map_tool("mcp__docs__search", "", {"q": "x" * 10000, "n": 3, "deep": {"a": {"b": {"c": 1}}}})[0][1]
        self.assertEqual((len(big["q"]), big["n"]), (codex.INPUT_MAX, 3))

    def test_update_plan(self):
        got = map_tool("update_plan", "", {"explanation": "free text", "plan": [
            {"step": "a", "status": "completed"}, {"step": "b", "status": "in_progress"}, {"step": "c"}, "junk"]})
        self.assertEqual(got, [("TodoWrite", {"todos": [
            {"content": "a", "status": "completed", "activeForm": "a"},
            {"content": "b", "status": "in_progress", "activeForm": "b"},
            {"content": "c", "status": "pending", "activeForm": "c"}]})])

    def test_unknown_tool_keeps_only_small_scalars(self):
        self.assertEqual(map_tool("frobnicate", "", {"n": 3, "ok": True, "text": "private words", "l": [1]}),
                         [("frobnicate", {"n": 3, "ok": True})])
        self.assertEqual(map_tool("", "", {}), [("unknown", {})])

    def test_apply_patch(self):
        text = ("*** Begin Patch\n*** Add File: a/new.py\n+x\n*** Update File: b/old.py\n*** Move to: b/moved.py\n"
                "@@\n-y\n+z\n*** Delete File: c/gone.py\n*** End Patch\n")
        self.assertEqual(codex.patch_tools(text), [("Write", {"file_path": "a/new.py"}),
                                                   ("Edit", {"file_path": "b/old.py"}),
                                                   ("Edit", {"file_path": "c/gone.py"})])
        self.assertEqual(codex.patch_tools("no headers"), [("Edit", {"file_path": ""})])
        self.assertEqual(map_tool("apply_patch", "", {"input": text})[0], ("Write", {"file_path": "a/new.py"}))
        self.assertEqual(map_tool("shell", "", {"command": ["apply_patch", text]})[1], ("Edit", {"file_path": "b/old.py"}))
        evs = convert([item({"type": "custom_tool_call", "name": "apply_patch", "call_id": "p1", "input": text}),
                       item({"type": "custom_tool_call_output", "call_id": "p1", "output": "error: patch failed"})])
        self.assertEqual([b["id"] for b in evs[0]["message"]["content"]], ["p1", "p1#2", "p1#3"])
        self.assertEqual([(x[1], x[2]) for x in shape(evs[1:])], [("p1", True), ("p1#2", True), ("p1#3", True)])
        self.assertNotIn("+z", json.dumps(evs[0]))   # patch bodies are not kept

    def test_code_mode(self):
        cm = codex._code_mode
        self.assertEqual(cm('text(await tools.exec_command({cmd:"git status", max_output_tokens:2000}));'),
                         [("Bash", {"command": "git status"})])
        self.assertEqual(cm("await tools.exec_command({'cmd': 'a \\'b\\''}); await tools.read_file({path: `x.py`})"),
                         [("Bash", {"command": "a 'b'"}), ("Read", {"file_path": "x.py"})])
        self.assertEqual(cm('tools.exec_command({cmd: "ls", opts: {a: [1, 2.5, -3e2, true, null]}})'),
                         [("Bash", {"command": "ls"})])
        self.assertEqual(cm('tools.exec_command({cmd: "ls", bad: someVar})'), [("Bash", {"command": "ls"})])   # regex
        self.assertEqual(cm('tools.apply_patch({input: "*** Begin Patch\\n*** Add File: n.py\\n*** End Patch"})'),
                         [("Write", {"file_path": "n.py"})])
        self.assertEqual(cm("tools.spawn_agent({prompt: 'secret'})"), [("Agent", {"subagent_type": "subagent"})])
        self.assertEqual(cm("store('x', 1); const y = 2;"), [("CodexExec", {})])
        self.assertEqual(cm("tools.exec_command(someVariable)"), [("CodexExec", {})])
        self.assertEqual(cm(""), [("CodexExec", {})])


class TestUsageOrder(unittest.TestCase):
    """The response is held until its usage; usage before or after the tool output, or never."""

    CALL = call("exec_command", {"cmd": "ls"}, "c1")
    OUT = output("c1", "a.py")

    def test_usage_before_output(self):
        conv = CodexConverter()
        self.assertEqual(conv.feed(self.CALL), [])
        got = conv.feed(usage_rec("r1", 1000, 600, 50))
        self.assertEqual([x[0] for x in shape(got)], ["assistant"])
        self.assertEqual(shape(got)[0][4], (400, 600, 0, 50))
        self.assertEqual(shape(conv.feed(self.OUT)), [("result", "c1", False, "a.py")])
        self.assertEqual(conv.finish(), [])

    def test_usage_after_output(self):
        conv = CodexConverter()
        self.assertEqual(conv.feed(self.CALL) + conv.feed(self.OUT), [])   # the result waits for its response
        got = shape(conv.feed(usage_rec("r1", 1000, 600, 50)))
        self.assertEqual([(x[0], x[1]) for x in got], [("assistant", "r1"), ("result", "c1")])

    def test_token_count_after_output(self):
        tc = rec("event_msg", {"type": "token_count", "info": {"last_token_usage": {
            "input_tokens": 1000, "cached_input_tokens": 600, "output_tokens": 50}}})
        conv = CodexConverter()
        got = shape(conv.feed(self.CALL) + conv.feed(self.OUT) + conv.feed(tc))
        self.assertEqual([x[0] for x in got], ["assistant", "result"])
        self.assertEqual(got[0][4], (400, 600, 0, 50))

    def test_late_usage_after_flush(self):
        conv = CodexConverter()
        text = item({"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "next"}]})
        got = shape(conv.feed(self.CALL) + conv.feed(self.OUT) + conv.feed(text))   # next response: flushed
        self.assertEqual([(x[0], x[4] if x[0] == "assistant" else None) for x in got],
                         [("assistant", (0, 0, 0, 0)), ("result", None)])
        late = conv.feed(usage_rec("r9", 1000, 600, 50))   # belongs to the pending "next" response
        self.assertEqual(shape(late)[0][1:5], ("r9", "codex", "end_turn", (400, 600, 0, 50)))

    def test_usage_with_no_pending_response(self):
        conv = CodexConverter()
        evs = conv.feed(self.CALL) + conv.feed(self.OUT) + conv.finish()
        first_id = evs[0]["message"]["id"]
        late = conv.feed(usage_rec("r2", 500, 0, 5))   # the flushed response's own usage, same message id
        self.assertEqual((late[0]["message"]["id"], late[0]["message"]["content"]), (first_id, []))
        self.assertEqual(usage_sum(evs + late), (500, 0, 0, 5))
        orphan = conv.feed(usage_rec("r3", 700, 0, 7))   # no response left to attach to: still billed
        self.assertEqual(usage_sum(orphan), (700, 0, 0, 7))
        self.assertEqual(conv.feed(usage_rec("r4", 0, 0, 0)), [])

    def test_flush_at_finish_and_on_prompt(self):
        conv = CodexConverter()
        self.assertEqual(conv.feed(self.CALL) + conv.feed(self.OUT), [])
        self.assertEqual([x[0] for x in shape(conv.finish())], ["assistant", "result"])
        conv = CodexConverter()
        conv.feed(self.CALL)
        got = shape(conv.feed(item({"type": "message", "role": "user", "content": [
            {"type": "input_text", "text": "stop"}]})))
        self.assertEqual([x[0] for x in got], ["assistant", "prompt"])
        self.assertEqual(shape(conv.feed(self.OUT)), [("result", "c1", False, "a.py")])   # after the flush: direct

    def test_unmatched_output_dropped(self):
        self.assertEqual(convert([output("nope", "x")]), [])

    def test_canonical_usage(self):
        self.assertEqual(canonical_usage({"input_tokens": 10000, "cached_input_tokens": 8000,
                                          "cache_write_input_tokens": 500, "output_tokens": 300,
                                          "reasoning_output_tokens": 100}),
                         {"input_tokens": 1500, "output_tokens": 300, "cache_read_input_tokens": 8000,
                          "cache_creation_input_tokens": 500})
        self.assertEqual(canonical_usage({"input_tokens": 5, "cached_input_tokens": 9, "output_tokens": "x"}),
                         {"input_tokens": 0, "output_tokens": 0, "cache_read_input_tokens": 9,
                          "cache_creation_input_tokens": 0})

    def test_token_count_totals_only_uses_delta(self):
        def tc(i, c, o):
            return rec("event_msg", {"type": "token_count", "info": {"total_token_usage": {
                "input_tokens": i, "cached_input_tokens": c, "output_tokens": o}}})
        conv = CodexConverter()
        evs = []
        for r in (self.CALL, tc(1000, 800, 10), self.OUT, call("exec_command", {"cmd": "pwd"}, "c2"),
                  tc(1000, 800, 10), tc(2500, 2000, 30), rec("event_msg", {"type": "token_count", "info": None})):
            evs += conv.feed(r)
        evs += conv.finish()
        self.assertEqual([x[4] for x in shape(evs) if x[0] == "assistant"], [(200, 800, 0, 10), (300, 1200, 0, 20)])


class TestSubagentAndMeta(unittest.TestCase):
    def test_subagent_rollout(self):
        evs = convert(records(SUB_PATH), use_usage_records=True)
        self.assertEqual(usage_sum(evs), (2000, 9000, 0, 150))   # the forked history (99999 tokens) is ignored
        self.assertEqual([x[0] for x in shape(evs)], ["prompt", "assistant", "result", "assistant"])
        for e in evs:
            self.assertEqual((e["sessionId"], e["agentId"], e["isSidechain"], e["cwd"]),
                             (MAIN, SUB, True, "/home/user/project"))   # the parent's copy did not override

    def test_model_fallbacks(self):
        msg = item({"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "hi"}]})
        evs = convert([rec("world_state", {"state": {"model": "gpt-5.4"}}), msg])
        self.assertEqual(evs[0]["message"]["model"], "gpt-5.4")
        evs = convert([rec("event_msg", {"type": "thread_settings_applied", "thread_settings": {"model": "gpt-5"}}),
                       msg])
        self.assertEqual(evs[0]["message"]["model"], "gpt-5")
        evs = convert([rec("turn_context", {"model": "gpt-5.2"}), rec("world_state", {"state": {"model": "x"}}), msg])
        self.assertEqual(evs[0]["message"]["model"], "gpt-5.2")
        self.assertEqual(convert([msg])[0]["message"]["model"], "codex")


class TestRobustness(unittest.TestCase):
    def test_malformed_fixture(self):
        evs = convert(records(BAD_PATH))
        got = shape(evs)
        self.assertEqual(got[0], ("prompt", "Count the lines of the readme"))
        names = [b[2] for x in got if x[0] == "assistant" for b in x[5] if b[0] == "tool_use"]
        self.assertEqual(names, ["Bash", "unknown", "Bash", "CodexExec", "Edit"])
        self.assertIn(("result", "call_m2", False, "3 README.md"), got)
        self.assertNotIn("orphan output", json.dumps(evs))
        self.assertEqual(usage_sum(evs), (1000, 0, 0, 10))   # string / negative usage values count as 0
        d = os.path.dirname(BAD_PATH)
        got = {n[-7:-6]: codex.read_events(os.path.join(d, n)) for n in sorted(os.listdir(d))}
        self.assertEqual((len(got["4"]) > 0, got["5"], got["6"]), (True, [], []))   # empty and binary files: []

    def test_garbage_never_raises(self):
        conv = CodexConverter()
        for raw in (None, 1, "x", [], {}, {"type": 3}, {"type": "response_item"}, {"type": "response_item",
                    "payload": None}, {"type": "session_meta", "payload": "x"}, {"type": "event_msg", "payload": {
                    "type": "token_count", "info": {"total_token_usage": "x", "last_token_usage": [1]}}},
                    item({"type": "reasoning", "summary": "x"}), item({"type": "function_call_output"}),
                    item({"type": "local_shell_call", "action": "x"}), item({"type": "web_search_call", "action": 5}),
                    item({"type": "custom_tool_call", "name": "exec", "input": None}),
                    item({"type": "function_call", "name": "shell", "arguments": "[1, 2]", "call_id": True}),
                    {"type": "response_item", "payload": {"type": "message", "role": "user", "content": "x" * 300000}}):
            self.assertIsInstance(conv.feed(raw), list)
        self.assertIsInstance(conv.finish(), list)

    def test_fuzz(self):
        """Seeded random mutations of the fixture records: the converter's own code (not only feed()'s guard)
        never raises, and every event it gives is a well-formed user / assistant event."""
        rng = random.Random(20261026)
        base = records(MODERN_PATH) + records(LEGACY_PATH) + records(SUB_PATH)
        junk = [None, 0, -1, 1.5, True, "", "x", [], {}, [None], {"a": None}, "\x00\ud800", 10 ** 30]

        def mutate(o, depth=0):
            if isinstance(o, dict) and o:
                o = dict(o)
                k = rng.choice(list(o))
                r = rng.random()
                if r < 0.3:
                    del o[k]
                elif r < 0.6:
                    o[k] = rng.choice(junk)
                else:
                    o[k] = mutate(o[k], depth + 1)
                return o
            if isinstance(o, list) and o:
                o = list(o)
                i = rng.randrange(len(o))
                o[i] = mutate(o[i], depth + 1) if rng.random() < 0.6 else rng.choice(junk)
                return o
            if isinstance(o, str) and o:
                return o[:rng.randrange(len(o))] if rng.random() < 0.5 else o + rng.choice(["{", "\"", "*** Add File: "])
            return rng.choice(junk)

        for _ in range(300):
            conv = CodexConverter(use_usage_records=rng.random() < 0.5)
            recs = [mutate(copy.deepcopy(r)) if rng.random() < 0.3 else r for r in base]
            rng.shuffle(recs) if rng.random() < 0.2 else None
            evs = []
            for r in recs:
                evs += conv._feed(r)
            evs += conv._flush()
            names = {}
            for e in evs:
                self.assertIn(e["type"], ("user", "assistant"))
                self.assertIsInstance(e["message"], dict)
                parse.compact_event(e, names)


# ---------------------------------------------------------------- discovery and harness selection

class TestDiscovery(unittest.TestCase):
    def setUp(self):
        self.h = CodexHarness()
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_discover(self):
        found = self.h.discover(ROOT)
        self.assertEqual([(s.id, s.slug, s.harness) for s in found],
                         [(LEGACY, "project", "codex"), (MAIN, "project", "codex")])   # the subagent is attached
        self.assertEqual(parse.discover(ROOT, harness="codex")[1].path, MODERN_PATH)

    def test_load_attaches_subagent_without_double_counting(self):
        s = next(x for x in self.h.discover(ROOT) if x.id == MAIN).load()
        self.assertEqual((s.cwd, s.project, s.models, s.first_prompt),
                         ("/home/user/project", "project", ["gpt-6.1-sol"], "Fix the failing parser test"))
        self.assertEqual(len(s.subagents), 1)
        sub = s.subagents[0]
        self.assertEqual({k: sub[k] for k in ("agent_id", "path", "meta", "workflow_id", "agent_type", "description",
                                              "workflow_phase", "spawn_depth")},
                         {"agent_id": SUB, "path": SUB_PATH, "meta": {"agentType": "subagent"}, "workflow_id": None,
                          "agent_type": "subagent", "description": None, "workflow_phase": None, "spawn_depth": 1})
        self.assertEqual(usage_sum(sub["events"]), (2000, 9000, 0, 150))
        self.assertEqual(usage_sum(s.events), (12000, 78000, 0, 2050))

    def test_orphan_subagent_is_a_main_session(self):
        d = os.path.join(self.tmp, "2026", "10", "20")
        os.makedirs(d)
        shutil.copy(SUB_PATH, d)
        found = self.h.discover(self.tmp)
        self.assertEqual([s.id for s in found], [SUB])
        self.assertEqual(found[0].load().subagents, [])

    def test_flat_archive_and_bad_files(self):
        shutil.copy(LEGACY_PATH, self.tmp)
        with open(os.path.join(self.tmp, "notes.jsonl"), "w") as f:
            f.write("{}\n")
        self.assertEqual([s.id for s in self.h.discover(self.tmp)], [LEGACY])
        bad = self.h.discover(MALFORMED_ROOT)   # garbage, empty and binary rollouts: listed, never raise
        self.assertEqual([s.id for s in bad], [BAD, codex_synth.sid(5), codex_synth.sid(6)])
        for s in bad:
            s.load()
        self.assertEqual(self.h.discover(os.path.join(self.tmp, "missing")), [])

    def test_ids_and_roots(self):
        self.assertEqual(codex.rollout_id(MODERN_PATH), MAIN)
        self.assertEqual(codex.rollout_id("/x/rollout-odd.jsonl"), "rollout-odd")
        self.assertEqual(codex.sessions_root(MODERN_PATH), os.path.abspath(ROOT))
        self.assertEqual(codex.sessions_root("/x/archived/rollout-a.jsonl"), "/x/archived")

    def test_tail_candidates_and_classify(self):
        files = sorted([MODERN_PATH, SUB_PATH, LEGACY_PATH])
        self.assertEqual(self.h.tail_candidates(ROOT), files)
        self.assertEqual(self.h.tail_candidates(ROOT, "proj*"), files)
        self.assertEqual(self.h.tail_candidates(ROOT, "other*"), [])
        self.assertEqual(self.h.classify_tail_path(ROOT, MODERN_PATH), (MAIN, "project", None))
        self.assertEqual(self.h.classify_tail_path(ROOT, SUB_PATH), (MAIN, "project", SUB))
        self.assertIsNone(self.h.classify_tail_path(ROOT, os.path.join(ROOT, "x.jsonl")))
        p = os.path.join(self.tmp, "rollout-2026-10-20T10-00-00-%s.jsonl" % codex_synth.sid(9))
        with open(p, "w") as f:
            f.write('{"timestamp":"2026-10-20T10:00:00Z","type":"session_meta","payload":{"id":"x","cw')
        self.assertIsNone(self.h.classify_tail_path(self.tmp, p))   # first line not complete yet
        with open(p, "w") as f:
            f.write(json.dumps(rec("session_meta", {"id": "x", "cwd": "/home/user/other"})) + "\n")
        self.assertEqual(self.h.classify_tail_path(self.tmp, p), (codex_synth.sid(9), "other", None))

    def test_detect(self):
        d = self.h.detect
        self.assertTrue(d(MODERN_PATH))
        self.assertTrue(d(ROOT))
        self.assertTrue(d(os.path.join(self.tmp, ".codex", "sessions")))   # need not exist
        self.assertFalse(d(self.tmp))
        shutil.copy(LEGACY_PATH, self.tmp)
        self.assertTrue(d(self.tmp))   # flat archive
        other = os.path.join(self.tmp, "renamed.jsonl")
        shutil.copy(LEGACY_PATH, other)
        self.assertTrue(d(other))      # first line is a session_meta
        claude = os.path.join(os.path.dirname(FIXTURES), "projects")
        self.assertFalse(d(claude))
        self.assertFalse(d(""))

    def test_default_root(self):
        with clean_env(HOME=self.tmp):
            self.assertEqual(self.h.default_root(), os.path.join(self.tmp, ".codex", "sessions"))
            self.assertFalse(self.h.is_available())
        with clean_env(CODEX_HOME=os.path.join(self.tmp, "ch")):
            self.assertEqual(self.h.default_root(), os.path.join(self.tmp, "ch", "sessions"))
        with clean_env(CODEX_HOME=os.path.join(self.tmp, "ch"), TOKENSCOPE_ROOT=ROOT):
            self.assertEqual(self.h.default_root(), ROOT)
            self.assertTrue(self.h.is_available())


class TestGetHarness(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_explicit_and_hints(self):
        with clean_env():
            self.assertEqual(get_harness("codex").name, "codex")
            self.assertEqual(get_harness("CODEX").name, "codex")
            self.assertEqual(get_harness(path=MODERN_PATH).name, "codex")
            self.assertEqual(get_harness(root=ROOT).name, "codex")
            self.assertEqual(get_harness(root=os.path.join(os.path.dirname(FIXTURES), "projects")).name, "claude")
            ag = os.path.join(self.tmp, "brain", "c1", ".system_generated", "logs")
            os.makedirs(ag)
            open(os.path.join(ag, "transcript.jsonl"), "w").close()
            self.assertEqual(get_harness(path=os.path.join(ag, "transcript.jsonl")).name, "antigravity")
        with clean_env(TOKENSCOPE_HARNESS="codex"):
            self.assertEqual(get_harness(root=os.path.join(os.path.dirname(FIXTURES), "projects")).name, "codex")

    @contextmanager
    def homes(self, claude=False, antigravity=False, codex_sessions=False, codex_dir=True):
        """Default roots of the three harnesses under self.tmp, each with sessions or not."""
        cl, ag = HARNESSES["claude"], HARNESSES["antigravity"]
        cl_root = os.path.join(self.tmp, "claude", "projects")
        ag_dir = os.path.join(self.tmp, "gemini", "antigravity-cli")
        cx_home = os.path.join(self.tmp, "codex")
        if claude:
            os.makedirs(os.path.join(cl_root, "-home-user-project"), exist_ok=True)
            with open(os.path.join(cl_root, "-home-user-project", "c0de0000-0000-4000-8000-000000000001.jsonl"),
                      "w") as f:
                f.write(json.dumps({"type": "user", "timestamp": "2026-10-20T09:00:00.000Z", "cwd": "/home/user/project",
                                    "message": {"role": "user", "content": "hi"}}) + "\n")
        if antigravity:
            logs = os.path.join(ag_dir, "brain", "conv-1", ".system_generated", "logs")
            os.makedirs(logs, exist_ok=True)
            with open(os.path.join(logs, "transcript.jsonl"), "w") as f:
                f.write(json.dumps({"step_index": 0, "type": "USER_INPUT", "content": "hi",
                                    "created_at": "2026-10-18T05:00:00Z"}) + "\n")
        if codex_dir:
            os.makedirs(os.path.join(cx_home, "sessions"), exist_ok=True)
        if codex_sessions:
            shutil.copytree(os.path.join(ROOT, "2026"), os.path.join(cx_home, "sessions", "2026"), dirs_exist_ok=True)
        with clean_env(HOME=self.tmp, CODEX_HOME=cx_home), \
                mock.patch.object(cl, "_default_root", cl_root), \
                mock.patch.object(ag, "app_data_dir", ag_dir), \
                mock.patch.object(ag, "_default_brain", os.path.join(ag_dir, "brain")):
            yield

    def test_auto_detect(self):
        with self.homes(codex_sessions=True):
            self.assertEqual(get_harness().name, "codex")   # only Codex has sessions
            self.assertEqual([s.id for s in parse.discover()], [LEGACY, MAIN])
        with self.homes(claude=True, codex_sessions=True):
            self.assertEqual(get_harness().name, "claude")   # Claude has sessions: unchanged
        shutil.rmtree(os.path.join(self.tmp, "claude"))
        with self.homes(antigravity=True, codex_sessions=True):
            self.assertEqual(get_harness().name, "antigravity")
        shutil.rmtree(self.tmp)
        os.makedirs(self.tmp)
        with self.homes(codex_dir=False):
            self.assertEqual(get_harness().name, "claude")   # nothing anywhere: today's default
        with self.homes(codex_dir=True):
            self.assertEqual(get_harness().name, "claude")   # an empty Codex dir is not chosen


# ---------------------------------------------------------------- analysis and pricing

class TestAnalysis(unittest.TestCase):
    def test_totals_equal_the_fixture_numbers(self):
        s = next(x for x in parse.discover(ROOT, harness="codex") if x.id == MAIN).load()
        a = analyze_session(s)
        main, subs, tot = a["totals"]["main"], a["totals"]["subagents"], a["totals"]["all"]
        self.assertEqual((main["calls"], main["fresh_input"], main["cache_read"], main["cache_write"], main["output"]),
                         (6, 12000, 78000, 0, 2050))
        self.assertEqual((subs["calls"], subs["fresh_input"], subs["cache_read"], subs["output"]), (2, 2000, 9000, 150))
        self.assertEqual(tot["calls"], 8)
        # gpt-6.1-sol: $2 input, $0.10 cache read, $10 output per 1M
        self.assertAlmostEqual(main["api_cost_usd"], (12000 * 2 + 78000 * 0.10 + 2050 * 10) / 1e6, places=4)
        self.assertAlmostEqual(subs["api_cost_usd"], (2000 * 2 + 9000 * 0.10 + 150 * 10) / 1e6, places=4)
        self.assertEqual(main["unpriced_calls"], 0)
        names = [t["name"] for c in a["calls"] for t in c["tool_uses"]]
        self.assertEqual(names[:7], ["Bash", "Bash", "Bash", "Edit", "Write", "TodoWrite", "mcp__docs__search"])

    def test_legacy_totals(self):
        s = next(x for x in parse.discover(ROOT, harness="codex") if x.id == LEGACY).load()
        t = analyze_session(s)["totals"]["all"]
        self.assertEqual((t["calls"], t["fresh_input"], t["cache_read"], t["output"]), (4, 8000, 30000, 1000))
        self.assertAlmostEqual(t["api_cost_usd"], (8000 * 1.25 + 30000 * 0.125 + 1000 * 10) / 1e6, places=4)

    def test_tool_kind_of_mapped_tools(self):
        self.assertEqual(tool_kind("Bash"), "bash")
        self.assertEqual(tool_kind("Read"), "file_read")
        self.assertEqual(tool_kind("Edit"), "edit")
        self.assertEqual(tool_kind("Write"), "edit")
        self.assertEqual(tool_kind("Grep"), "search")
        self.assertEqual(tool_kind("WebSearch"), "search")
        self.assertEqual(tool_kind("mcp__docs__search"), "mcp")
        self.assertEqual(tool_kind("CodexExec"), tool_kind("frobnicate"))

    def test_pricing(self):
        self.assertEqual(price_of("gpt-6.1-sol"), (2, 2.5, 5, 0.10, 10))
        self.assertEqual(price_of("gpt-5"), (1.25, 1.25, 1.25, 0.125, 10))
        self.assertEqual(price_of("gpt-5-2025-08-07"), price_of("gpt-5"))       # OpenAI date suffix
        self.assertEqual(price_of("gpt-5.4-mini"), (0.75, 0.75, 0.75, 0.075, 4.5))   # not gpt-5.4
        self.assertEqual(price_of("gpt-5-codex"), (1.25, 1.25, 1.25, 0.125, 10))
        self.assertEqual(price_of("codex-mini-latest"), (1.5, 1.5, 1.5, 0.375, 6))
        for unknown in ("gpt-5.1-codex-max", "gpt-5.4-pro", "gpt-6.1-sol-preview", "gpt-7", "codex"):
            self.assertIsNone(price_of(unknown), unknown)
        self.assertIsNone(cost_usd("gpt-5.1-codex-max", {"input": 1000, "cache_write": 0, "cache_read": 0,
                                                         "output": 10}))
        self.assertAlmostEqual(cost_usd("gpt-6.1-sol", {"input": 1e6, "cache_write": 0, "cache_read": 1e6,
                                                        "output": 1e6}), 12.10)
        # Claude and Gemini results unchanged
        self.assertEqual(price_of("claude-opus-5-20260101"), price_of("claude-opus-5"))
        self.assertEqual(price_of("claude-sonnet-4-5[1m]"), (3, 3.75, 6, 0.3, 15))
        self.assertEqual(price_of("Gemini 3.8 Flash (High)"), (0.15, 0.20, 0.20, 0.0375, 0.60))
        self.assertEqual(price_of("gemini-2.0-flash"), price_of("gemini-2.0-flash"))
        self.assertIsNotNone(price_of("gemini-pro"))
        self.assertIsNone(price_of("<synthetic>"))


# ---------------------------------------------------------------- CLI

class TestCli(unittest.TestCase):
    def test_sessions(self):
        code, out, _ = run_cli("sessions", "--harness", "codex", "--root", ROOT, "--no-cache")
        self.assertEqual(code, 0)
        rows = [l for l in out.splitlines() if l.startswith("019a0000")]
        self.assertEqual(len(rows), 2)
        self.assertIn("Fix the failing parser test", rows[1])
        self.assertIn("8*", rows[1])   # 6 main + 2 subagent calls
        self.assertIn("Make the calculator tests pass", rows[0])

    def test_analyze(self):
        code, out, _ = run_cli("analyze", MAIN, "--harness", "codex", "--root", ROOT, "--no-cache", "--json")
        self.assertEqual(code, 0)
        a = json.loads(out)
        self.assertEqual((a["session_id"], a["totals"]["all"]["calls"], a["models"]), (MAIN, 8, ["gpt-6.1-sol"]))
        code, out, _ = run_cli("analyze", LEGACY[:12] + "0", "--harness", "codex", "--root", ROOT, "--no-cache")
        self.assertEqual(code, 2)   # unknown id

    def test_intents_and_alerts(self):
        code, out, _ = run_cli("intents", MAIN, "--harness", "codex", "--root", ROOT, "--json")
        self.assertEqual(code, 0)
        steps = json.loads(out)["steps"]
        self.assertTrue(steps)
        tools = {t for x in steps for t in x["tools"]}
        self.assertTrue({"Edit", "Write", "TodoWrite", "Agent", "WebSearch", "docs.search", "Bash git status"} <= tools,
                        tools)
        code, out, _ = run_cli("intents", MODERN_PATH)   # a rollout path, no root
        self.assertEqual(code, 0)
        self.assertIn("[", out)
        code, out, _ = run_cli("alerts", MAIN, "--harness", "codex", "--root", ROOT)
        self.assertEqual((code, out.splitlines()[0]), (0, MAIN[:8] + ": 0 alerts"))
        code, out, _ = run_cli("alerts", LEGACY_PATH, "--json")
        self.assertEqual((code, json.loads(out)["session"]), (0, LEGACY))

    def test_every_harness_flag_accepts_codex(self):
        for cmd in ("sessions", "analyze", "report", "fixes", "compare", "goals", "understand", "patterns", "live",
                    "alerts", "intents", "top"):
            with self.assertRaises(SystemExit) as cm:
                run_cli(cmd, "--harness", "codex", "--help")
            self.assertEqual(cm.exception.code, 0, cmd)
        with self.assertRaises(SystemExit) as cm:
            run_cli("sessions", "--harness", "nope")
        self.assertEqual(cm.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
