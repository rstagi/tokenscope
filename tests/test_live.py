"""tokenscope live: tailer, live state (steps, totals, flags, status), goal names, hooks and the HTTP API."""
import io
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
import urllib.request
from contextlib import redirect_stdout

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tokenscope import cli  # noqa: E402
from tokenscope.live import goalname, hook, hookevents, server, state as st  # noqa: E402
from tokenscope.live.tail import Tailer  # noqa: E402

SID = "aaaaaaaa-1111-2222-3333-444444444444"
MODEL = "claude-sonnet-4-5"
T0 = time.time() - 60


def ts(i):
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(T0 + i)) + ".000Z"


def user_prompt(i, text):
    return {"type": "user", "timestamp": ts(i), "sessionId": SID, "cwd": "/srv/checkout-app", "uuid": "u%d" % i,
            "message": {"role": "user", "content": text}}


def assistant(i, mid, blocks, usage=None, stop=None):
    return {"type": "assistant", "timestamp": ts(i), "sessionId": SID, "uuid": "a%d" % i,
            "message": {"id": mid, "model": MODEL, "content": blocks, "stop_reason": stop,
                        "usage": usage or {"input_tokens": 10, "cache_creation_input_tokens": 100,
                                           "cache_read_input_tokens": 1000, "output_tokens": 20}}}


def tool_use(tid, name, inp):
    return {"type": "tool_use", "id": tid, "name": name, "input": inp}


def result(i, tid, err=False):
    return {"type": "user", "timestamp": ts(i), "sessionId": SID, "uuid": "r%d" % i,
            "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": tid, "is_error": err,
                                                     "content": "x" * 50}]},
            "toolUseResult": {"stdout": "x" * 50}}


def lines(*recs):
    return "".join(json.dumps(r) + "\n" for r in recs)


class LiveFixture(unittest.TestCase):
    """A synthetic ~/.claude/projects tree written in chunks."""

    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.proj = os.path.join(self.root, "-srv-checkout-app")
        os.makedirs(self.proj)
        self.main = os.path.join(self.proj, SID + ".jsonl")
        self.subdir = os.path.join(self.proj, SID, "subagents")
        self.tailer = Tailer(self.root)
        self.state = st.LiveState(redact=True)

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def write(self, path, text):
        with open(path, "a", encoding="utf-8") as f:
            f.write(text)

    def step(self):
        for tf, recs, reset in self.tailer.poll():
            self.state.ingest(tf, recs, reset)
        return self.state.sessions.get(SID)

    def build(self):
        """Write the whole scenario in chunks, ingesting after each one."""
        # chunk 1: prompt + first assistant line; the second line of message m1 is split mid-way
        m1b = json.dumps(assistant(2, "m1", [tool_use("t1", "Read", {"file_path": "/srv/checkout-app/src/cart.py"})],
                                   usage={"input_tokens": 10, "cache_creation_input_tokens": 100,
                                          "cache_read_input_tokens": 1000, "output_tokens": 30})) + "\n"
        self.write(self.main, lines(user_prompt(0, "please fix the checkout total tax in cart.py"),
                                    assistant(1, "m1", [{"type": "thinking", "thinking": "", "signature": "s"}],
                                              usage={"input_tokens": 10, "cache_creation_input_tokens": 100,
                                                     "cache_read_input_tokens": 1000, "output_tokens": 5}))
                   + m1b[:40])
        s = self.step()
        self.assertEqual(s.totals["turns"], 1)
        self.assertEqual(s.totals["tool_calls"], 0)
        # chunk 2: rest of the split line, then re-reads (3x Read of cart.py) and a bad line
        self.write(self.main, m1b[40:] + lines(result(3, "t1"))
                   + "{not json\n"
                   + lines(assistant(4, "m2", [tool_use("t2", "Read", {"file_path": "/srv/checkout-app/src/cart.py"})]),
                           result(5, "t2"),
                           assistant(6, "m3", [tool_use("t3", "Read", {"file_path": "/srv/checkout-app/src/cart.py"})]),
                           result(7, "t3")))
        s = self.step()
        self.assertEqual(self.tailer.malformed, 1)
        # chunk 3: a tool error retry loop (3 failing test turns), then a fan-out to a subagent
        recs = []
        for k in range(3):
            tid = "e%d" % k
            recs += [assistant(10 + 2 * k, "me%d" % k, [tool_use(tid, "Bash", {"command": "pytest -q tests/"})]),
                     result(11 + 2 * k, tid, err=True)]
        recs.append(assistant(20, "m4", [tool_use("tA", "Agent", {"subagent_type": "Explore", "description": "Survey",
                                                                 "prompt": "look around"})]))
        self.write(self.main, lines(*recs))
        s = self.step()
        # chunk 4: the subagent transcript + meta, then its result and a large-context final answer
        os.makedirs(self.subdir)
        with open(os.path.join(self.subdir, "agent-x1.meta.json"), "w") as f:
            json.dump({"agentType": "Explore", "description": "Survey", "toolUseId": "tA"}, f)
        sub = os.path.join(self.subdir, "agent-x1.jsonl")
        self.write(sub, lines(dict(assistant(21, "s1", [tool_use("st1", "Grep", {"pattern": "tax"})]), isSidechain=True),
                              dict(result(22, "st1"), isSidechain=True)))
        s = self.step()
        self.write(self.main, lines(result(23, "tA"),
                                    assistant(24, "m5", [{"type": "text", "text": "Fixed."}], stop="end_turn",
                                              usage={"input_tokens": 1000, "cache_creation_input_tokens": 9000,
                                                     "cache_read_input_tokens": 150000, "output_tokens": 100})))
        return self.step()


class TestTailer(LiveFixture):
    def test_partial_lines_and_truncation(self):
        self.write(self.main, '{"type":"user"')
        self.assertEqual([r for _, r, _ in self.tailer.poll()], [[]])
        self.write(self.main, ',"x":1}\n')
        got = self.tailer.poll()
        self.assertEqual(got[0][1], [{"type": "user", "x": 1}])
        with open(self.main, "w") as f:
            f.write('{"a":1}\n')
        tf, recs, reset = self.tailer.poll()[0]
        self.assertTrue(reset)
        self.assertEqual(recs, [{"a": 1}])

    def test_old_files_ignored_until_changed(self):
        self.write(self.main, lines(user_prompt(0, "hi there")))
        old = time.time() - 3 * 3600
        os.utime(self.main, (old, old))
        self.assertEqual(Tailer(self.root).poll(), [])
        t = Tailer(self.root)
        t.poll()
        self.write(self.main, lines(user_prompt(1, "again")))
        self.assertEqual(len(t.poll()[0][1]), 2)

    def test_subagent_classified(self):
        self.build()
        tfs = {tf.agent_id: tf for tf in self.tailer.files.values()}
        self.assertEqual(tfs["x1"].session_id, SID)
        self.assertEqual(tfs["x1"].meta["toolUseId"], "tA")


class TestState(LiveFixture):
    def test_steps_totals_flags(self):
        s = self.build()
        kinds = [x["k"] for x in s.main.steps]
        self.assertEqual(kinds[0], "prompt")
        self.assertNotIn("text", s.main.steps[0])           # redacted
        self.assertIn("fanout", kinds)
        self.assertIn("flag", kinds)
        turns = s.main.turn_steps
        self.assertEqual([t["intent"] for t in turns[:3]], ["read_code"] * 3)
        self.assertEqual([t["intent"] for t in turns[3:6]], ["test"] * 3)
        self.assertTrue(all(t["err"] for t in turns[3:6]))
        self.assertEqual(turns[0]["tools"][0]["target"], ".py")
        self.assertEqual(turns[3]["tools"][0]["label"], "Bash pytest")
        self.assertEqual(turns[6]["tools"][0]["label"], "Agent(Explore)")
        # usage once per message.id (last line wins): m1 counted with output 30, not 5 + 30
        main_msgs = 1 + 2 + 3 + 1 + 1          # m1, m2, m3, me0-2, m4, m5
        self.assertEqual(s.totals["turns"], main_msgs + 1)   # + subagent s1
        self.assertEqual(s.totals["output"], 30 + 20 * 6 + 100 + 20)
        self.assertEqual(s.totals["input"], 10 * 7 + 1000 + 10)
        self.assertEqual(s.totals["tool_calls"], 8)
        self.assertEqual(s.totals["errors"], 3)
        self.assertGreater(s.totals["usd"], 0)
        u = {"input": 1000, "cache_write": 9000, "cache_read": 150000, "output": 100}
        self.assertEqual(s.context_tokens, 160000)
        self.assertAlmostEqual(s.main.msgs["m5"]["cw"], 1000 + 1.25 * 9000 + 15000 + 500)
        self.assertEqual(u["cache_read"], 150000)
        flags = set(s.flags)
        self.assertTrue({"retry_loop", "reread_file", "dead_tool", "context_large"} <= flags, flags)
        self.assertEqual(s.flags["reread_file"]["detail"], ".py x3")
        # subagent branch linked to the fan-out
        a = s.agents["x1"]
        self.assertEqual((a["type"], a["tool_use_id"], a["status"], a["turns"]), ("Explore", "tA", "done", 1))
        self.assertIsNone(a["description"])                   # redacted
        self.assertEqual(a["steps"][0]["intent"], "locate")
        fo = next(x for x in s.main.steps if x["k"] == "fanout")
        self.assertEqual(fo["agent_id"], "x1")
        seqs = [x["seq"] for x in s.main.steps]
        self.assertEqual(seqs, sorted(seqs))
        self.assertEqual(s.goal["name"], "Fix: checkout total tax")
        self.assertEqual(self.state._header(s, 0)["project"], "project-A")

    def test_status(self):
        s = self.build()
        self.assertEqual(self.state.status(s, now=s.last_ts + 5), "waiting")
        self.assertEqual(self.state.status(s, now=s.last_ts + 3600), "idle")
        self.state.apply_hook({"event": "UserPromptSubmit", "session_id": SID, "ts": s.last_ts + 10})
        self.assertEqual(self.state.status(s, now=s.last_ts + 12), "working")
        self.state.apply_hook({"event": "Notification", "session_id": SID, "ts": s.last_ts + 20})
        self.assertEqual(self.state.status(s, now=s.last_ts + 21), "waiting")
        self.state.apply_hook({"event": "SessionEnd", "session_id": SID, "ts": s.last_ts + 30})
        self.assertEqual(self.state.status(s, now=s.last_ts + 31), "idle")

    def test_since_includes_updates(self):
        self.write(self.main, lines(user_prompt(0, "run the tests"),
                                    assistant(1, "m1", [tool_use("t1", "Bash", {"command": "pytest"})])))
        s = self.step()
        seq = s.seq
        self.write(self.main, lines(result(2, "t1", err=True)))
        s = self.step()
        d = self.state.session_since(s, seq)
        self.assertEqual(len(d["steps"]), 1)
        self.assertTrue(d["steps"][0]["tools"][0]["err"])
        self.assertNotIn("_recs", d["steps"][0])

    def test_unredacted_keeps_text(self):
        self.state = st.LiveState(redact=False)
        self.write(self.main, lines(user_prompt(0, "explain the cart module"),
                                    assistant(1, "m1", [{"type": "text", "text": "Sure"}], stop="end_turn")))
        s = self.step()
        self.assertEqual(s.main.steps[0]["text"], "explain the cart module")
        self.assertEqual(s.main.steps[1]["text"], "Sure")
        self.assertEqual(s.project_raw, "checkout-app")

    def test_reset_on_truncation(self):
        self.build()
        with open(self.main, "w") as f:
            f.write(lines(user_prompt(0, "fresh start")))
        s = self.step()
        self.assertEqual(len(s.prompts), 1)
        self.assertEqual(s.totals["turns"], 0)


class TestRules(unittest.TestCase):
    def test_retry_loop(self):
        self.assertEqual(st.retry_loop([("test", True)] * 3), 1)
        self.assertEqual(st.retry_loop([("test", True), ("edit", True), ("test", True)]), 0)
        self.assertEqual(st.retry_loop([("test", True)] * 2 + [("test", False)]), 0)

    def test_same_intent_after_error(self):
        t = [("test", True), ("test", False), ("edit", True), ("edit", True), ("edit", False)]
        self.assertEqual(st.same_intent_after_error(t), 3)

    def test_browser_loop(self):
        self.assertEqual(st.browser_loop(["browse_ui"] * 10), 1)
        self.assertEqual(st.browser_loop(["browse_ui"] * 9 + ["edit"]), 0)

    def test_reread(self):
        ev = [("read", "a.py")] * 2 + [("write", "a.py")] + [("read", "a.py")] * 2
        self.assertEqual(st.reread_files(ev), {})
        self.assertEqual(st.reread_files([("read", "a.py")] * 3), {"a.py": 3})

    def test_dead_tools(self):
        self.assertEqual(st.dead_tools([("Bash x", True)] * 3), {"Bash x": 3})
        self.assertEqual(st.dead_tools([("Bash x", True), ("Bash x", False), ("Bash x", True)]), {})

    def test_context_large(self):
        self.assertTrue(st.context_large(150_000))
        self.assertFalse(st.context_large(149_999))

    def test_status_of(self):
        now = 1000.0
        self.assertEqual(st.status_of(now, now - 10, "tool_use", now - 10), "working")
        self.assertEqual(st.status_of(now, now - 200, "tool_use", now - 200), "idle")
        self.assertEqual(st.status_of(now, now - 200, "end_turn", now - 300), "waiting")
        hk = {"last": ("PreToolUse", now - 300), "active_ts": now - 300, "stop_ts": 0, "open_tools": 1}
        self.assertEqual(st.status_of(now, now - 300, "tool_use", now - 300, hk), "working")

    def test_labels(self):
        from tokenscope.intents import record
        self.assertEqual(st.tool_label("mcp__srv__op", {}, record("mcp__srv__op", {})), "mcp:srv.op")
        self.assertEqual(st.tool_label("Skill", {}, record("Skill", {"skill": "pdf"})), "skill:pdf")
        self.assertEqual(st.tool_label("Bash", {}, record("Bash", {"command": "git status"})), "Bash git")
        r = record("WebFetch", {"url": "https://example.org/x"})
        self.assertEqual(st.tool_target("WebFetch", {"url": "https://example.org/x"}, r, False), "example.org")
        self.assertEqual(st.tool_target("WebFetch", {"url": "https://example.org/x"}, r, True), "…")


class TestGoalName(unittest.TestCase):
    def test_keyword(self):
        self.assertEqual(goalname.keyword_name("please fix the checkout total tax"), "Fix: checkout total tax")
        self.assertTrue(goalname.keyword_name("can you add a dark mode toggle").startswith("Build:"))
        self.assertEqual(goalname.keyword_name("why?"), "Explain")
        self.assertLessEqual(len(goalname.keyword_name("refactor " + " ".join("word%d" % i for i in range(40)))), 48)

    def test_redact_masks(self):
        n = goalname.keyword_name("fix /srv/someone/app/main.py for bob@example.com at https://x.io/a", redact=True)
        self.assertNotIn("someone", n)
        self.assertNotIn("bob", n)
        self.assertNotIn("x.io", n)

    def test_haiku_failure_is_none(self):
        def boom(*a, **k):
            raise OSError("no claude")
        self.assertIsNone(goalname.haiku_name("fix it", runner=boom))


class TestHook(unittest.TestCase):
    def test_event_line_privacy(self):
        ev = hook.event_line({"hook_event_name": "PreToolUse", "session_id": "s", "tool_name": "Bash",
                              "tool_input": {"command": "secret"}, "prompt": "secret"}, now=1)
        self.assertEqual(ev, {"ts": 1, "event": "PreToolUse", "session_id": "s", "tool_name": "Bash"})
        n = hook.event_line({"hook_event_name": "Notification", "session_id": "s", "message": "m" * 500})
        self.assertEqual(len(n["message"]), 200)
        self.assertIsNone(hook.event_line({}))

    def test_main_appends_and_tail_reads(self):
        d = tempfile.mkdtemp()
        try:
            path = os.path.join(d, "sub", "events.jsonl")
            os.environ["TOKENSCOPE_LIVE_EVENTS"] = path
            he = hookevents.HookEvents(path)
            self.assertEqual(he.poll(), [])
            self.assertEqual(hook.main(io.StringIO("not json")), 0)
            hook.main(io.StringIO(json.dumps({"hook_event_name": "Stop", "session_id": "s1"})))
            self.assertEqual(he.poll(), [])                  # first poll starts at the end of the file
            hook.main(io.StringIO(json.dumps({"hook_event_name": "Stop", "session_id": "s1"})))
            self.assertEqual([e["event"] for e in he.poll()], ["Stop"])
        finally:
            os.environ.pop("TOKENSCOPE_LIVE_EVENTS", None)
            shutil.rmtree(d, ignore_errors=True)

    def test_cli_hooks_snippet(self):
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(cli.main(["live", "hooks"]), 0)
        snip = json.loads(out.getvalue())
        self.assertEqual(set(snip["hooks"]), set(hook.EVENTS))
        self.assertTrue(snip["hooks"]["Stop"][0]["hooks"][0]["command"].endswith(os.path.join("tokenscope", "live", "hook.py")))


class TestServer(LiveFixture):
    def setUp(self):
        super().setUp()
        self.live = server.Live(self.root, hooks=False)
        self.srv = server.make_server(self.live, "127.0.0.1", 0)
        self.port = self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def tearDown(self):
        self.live.stop.set()
        self.srv.shutdown()
        self.srv.server_close()
        super().tearDown()

    def get(self, path):
        with urllib.request.urlopen("http://127.0.0.1:%d%s" % (self.port, path), timeout=5) as r:
            self.assertEqual(r.headers["Cache-Control"], "no-store")
            return json.loads(r.read())

    def test_api(self):
        self.tailer = self.live.tailer
        self.state = self.live.state
        self.build()
        b = self.get("/api/board")
        self.assertTrue(b["redacted"])
        row = b["sessions"][0]
        for k in ("id", "short", "project", "goal", "status", "current_intent", "current_group", "last_step",
                  "started", "last_ts", "elapsed_s", "cw", "usd", "turns", "tool_calls", "errors", "agents_running",
                  "agents_total", "context_tokens", "flags", "intent_mix"):
            self.assertIn(k, row)
        self.assertEqual(row["agents_total"], 1)
        self.assertIn({"flag": "retry_loop", "count": 1}, row["flags"])
        full = self.get("/api/session/" + SID)
        self.assertEqual(full["seq"], max(x["useq"] for x in full["steps"] + full["agents"][0]["steps"]))
        self.assertEqual(full["agents"][0]["steps"][0]["agent"], "x1")
        part = self.get("/api/session/%s?since=%d" % (SID[:8], full["seq"] - 2))
        self.assertTrue(1 <= len(part["steps"]) <= 2)
        self.assertIn("totals", part)
        self.assertEqual(part["agents"][0]["type"], "Explore")
        c = self.get("/api/session/%s/compact" % SID)
        self.assertEqual(set(c), {"goal", "status", "cw", "usd", "context_tokens", "current_intent", "flags",
                                  "recent", "agents", "elapsed_s", "turns", "tool_calls", "errors", "cw_series"})
        r3 = c["recent"][3]
        launched = [a for t in c["recent"] for a in t["agents"]]
        self.assertEqual([a["type"] for a in launched], ["Explore"])
        self.assertEqual({k: r3[k] for k in ("intent", "group", "tools", "err", "tool_err")},
                         {"intent": "test", "group": "verify", "tools": ["Bash pytest"], "err": True, "tool_err": [True]})
        self.assertEqual(len(c["cw_series"]), len(c["recent"]))   # one value per main-thread tool turn
        self.assertEqual(c["agents"], [{"type": "Explore", "status": "done", "turns": 1}])
        with self.assertRaises(urllib.error.HTTPError):
            self.get("/api/session/nope")

    def test_events_and_static(self):
        self.write(self.main, lines(user_prompt(0, "fix it")))
        with urllib.request.urlopen("http://127.0.0.1:%d/" % self.port, timeout=5) as r:
            self.assertIn(b"<", r.read())
        r = urllib.request.urlopen("http://127.0.0.1:%d/events" % self.port, timeout=5)
        time.sleep(0.1)
        self.live.poll_once()
        got = b""
        while b'"k":"prompt"' not in got or b"event: board" not in got:
            line = r.readline()
            self.assertTrue(line)
            got += line
        r.close()
        self.assertIn(b'"k":"prompt"', got)


if __name__ == "__main__":
    unittest.main()


class TestAsyncAgents(LiveFixture):
    def test_task_notification_is_not_a_prompt(self):
        launched = result(3, "tA")
        launched["message"]["content"][0]["content"] = [{"type": "text", "text": "Async agent launched successfully."}]
        note = user_prompt(5, "<task-notification>\n<task-id>x</task-id>\n<tool-use-id>tA</tool-use-id>\n"
                              "<status>completed</status></task-notification>")
        note["origin"] = {"kind": "task-notification"}
        self.write(self.main, lines(user_prompt(0, "review the api"),
                                    assistant(1, "m1", [tool_use("tA", "Agent", {"subagent_type": "Explore",
                                                                                "description": "Survey"})]),
                                    launched))
        os.makedirs(self.subdir)
        self.write(os.path.join(self.subdir, "agent-x1.meta.json"), json.dumps({"toolUseId": "tA", "agentType": "Explore"}))
        self.write(os.path.join(self.subdir, "agent-x1.jsonl"),
                   lines(assistant(2, "a1", [tool_use("s1", "Read", {"file_path": "/srv/a.py"})])))
        s = self.step()
        a = next(iter(s.agents.values()))
        self.assertNotEqual(a["status"], "done")   # async launch result does not finish it
        self.write(self.main, lines(note))
        s = self.step()
        self.assertEqual(len(s.prompts), 1)
        self.assertEqual(a["status"], "done")
