"""tokenscope live: the follow view (/follow, /api/follow tiles, pins), the stable board order, and goal labels for
prompts that read oddly (deterministic cleanup; Haiku only for those, cached on disk)."""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from tokenscope import top  # noqa: E402
from tokenscope.live import goalname, server, state as st  # noqa: E402
from test_live import SID, T0, LiveFixture, assistant, lines, result, tool_use, user_prompt  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATIC = os.path.join(ROOT, "tokenscope", "live", "static")


def row(sid, started, ended=False, **kw):
    return dict({"id": sid, "started": started, "ended": ended, "cw": 0.0, "last_ts": started}, **kw)


class TestBoardOrder(unittest.TestCase):
    def test_running_by_start_then_ended(self):
        rows = [row("c", 30.0), row("a", 10.0, ended=True), row("b", 20.0), row("d", 5.0)]
        self.assertEqual([r["id"] for r in st.board_order(rows)], ["d", "b", "c", "a"])

    def test_activity_and_cost_never_move_a_card(self):
        rows = [row("a", 10.0), row("b", 20.0), row("c", 30.0)]
        before = [r["id"] for r in st.board_order(rows)]
        rows[0].update(cw=1e6, last_ts=999.0)   # the oldest session suddenly spends and is the latest active
        rows[2].update(cw=0.0, last_ts=31.0)
        self.assertEqual([r["id"] for r in st.board_order(list(reversed(rows)))], before)

    def test_ending_moves_only_that_card(self):
        rows = [row("a", 10.0), row("b", 20.0), row("c", 30.0)]
        rows[0]["ended"] = True
        self.assertEqual([r["id"] for r in st.board_order(rows)], ["b", "c", "a"])

    def test_ties_and_missing_start_are_deterministic(self):
        rows = [row("z", None), row("b", 10.0), row("a", 10.0)]
        self.assertEqual([r["id"] for r in st.board_order(rows)], ["a", "b", "z"])


SID2 = "bbbbbbbb-1111-2222-3333-444444444444"


class TestBoardStable(LiveFixture):
    def test_board_keeps_order_while_the_newer_session_spends(self):
        self.build()   # SID starts at T0
        other = os.path.join(self.proj, SID2 + ".jsonl")
        recs = [dict(user_prompt(40, "add a csv export"), sessionId=SID2),
                dict(assistant(41, "n1", [tool_use("n1t", "Read", {"file_path": "/srv/checkout-app/a.py"})]), sessionId=SID2)]
        self.write(other, lines(*recs))
        self.step()
        now = self.state.sessions[SID2].last_ts + 1
        self.assertEqual([r["id"] for r in self.state.board(now=now)["sessions"]], [SID, SID2])
        # the first session becomes the most active and costly: no reorder
        self.write(self.main, lines(assistant(50, "m9", [tool_use("t9", "Bash", {"command": "pytest"})],
                                              usage={"input_tokens": 10 ** 6, "output_tokens": 10 ** 5}),
                                    result(51, "t9")))
        self.step()
        self.assertEqual([r["id"] for r in self.state.board(now=now + 20)["sessions"]], [SID, SID2])
        # it ends: it moves after the running one
        self.state.apply_hook({"event": "SessionEnd", "session_id": SID, "ts": self.state.sessions[SID].last_ts + 1})
        self.assertEqual([r["id"] for r in self.state.board(now=now + 30)["sessions"]], [SID2, SID])


class TestFollowPayload(LiveFixture):
    def test_tile(self):
        s = self.build()
        t = self.state.follow_tile(s, s.last_ts + 1)
        for k in ("id", "short", "project", "goal", "goal_source", "status", "cw", "usd", "turns", "started",
                  "last_ts", "seq", "steps_n", "agents_running", "now", "steps"):
            self.assertIn(k, t)
        self.assertEqual(t["status"], "waiting")   # the turn ended (end_turn) moments ago
        self.assertIsNone(t["now"])                # nothing runs while waiting
        self.assertTrue(t["steps"])
        self.assertEqual(len(t["steps"]), t["steps_n"])
        for x in t["steps"]:   # + the subagents a step launched (the fixture's Explore survey)
            self.assertEqual(set(x) - {"agents"}, {"i", "cat", "label", "cw", "usd", "open", "tested", "dur_s",
                                                   "intent"})
        # every step is in one intent (steps.intent_blocks), the intents in step order
        self.assertEqual(sorted({x["intent"] for x in t["steps"]}), list(range(len(t["intents"]))))
        self.assertEqual(sum(b["steps"] for b in t["intents"]), t["steps_n"])
        self.assertEqual((t["intents_before"], t["intents_n"]), (0, len(t["intents"])))
        for b in t["intents"]:
            self.assertIn(b["kind"], ("group", "pending", "step"))
        self.assertEqual([a["type"] for x in t["steps"] for a in x.get("agents", ())], ["Explore"])
        # per-step $ of the main thread; the running total adds subagents and the final answer
        self.assertTrue(0 < sum(x["usd"] for x in t["steps"]) <= t["usd"])
        self.assertEqual(t["usd"], round(s.totals["usd"], 6))

    def test_running_and_ended_status(self):
        s = self.build()
        self.write(self.main, lines(user_prompt(30, "now look at the order model"),
                                    assistant(31, "m9", [tool_use("t9", "Read", {"file_path": "/srv/checkout-app/o.py"})])))
        self.step()
        t = self.state.follow_tile(s, s.last_ts + 1)
        self.assertEqual(t["status"], "running")
        self.assertTrue(t["now"]["tool"].startswith("Read"))
        self.assertTrue(t["steps"][-1]["open"])
        self.state.apply_hook({"event": "SessionEnd", "session_id": SID, "ts": s.last_ts + 2})
        self.assertEqual(self.state.follow_tile(s, s.last_ts + 3)["status"], "ended")

    def test_follow_caps_dedupes_and_marks_missing(self):
        self.build()
        d = self.state.follow([SID[:8], SID, "nope", "", "x1", "x2", "x3"])
        self.assertEqual([x["id"] for x in d["sessions"]], [SID, SID, "nope", "x1"][:st.FOLLOW_MAX])
        self.assertEqual(len(d["sessions"]), st.FOLLOW_MAX)
        self.assertEqual(d["sessions"][2], {"id": "nope", "missing": True})
        self.assertTrue(d["redacted"])

    def test_last_n_steps(self):
        s = self.build()
        t = self.state.follow_tile(s, s.last_ts + 1, last_n=1)
        self.assertEqual(len(t["steps"]), 1)
        self.assertGreater(t["steps_n"], 1)

    def test_redacted_tile_leaks_nothing(self):
        s = self.build()
        text = json.dumps(self.state.follow([SID], s.last_ts + 1))
        for leak in ("/srv", "checkout-app", "cart.py", "please fix the checkout total tax"):
            self.assertNotIn(leak, text)


class TestFollowServer(LiveFixture):
    def setUp(self):
        super().setUp()
        self.live = server.Live(self.root, hooks=False)
        self.srv = server.make_server(self.live, "127.0.0.1", 0)
        self.port = self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.tailer, self.state = self.live.tailer, self.live.state

    def tearDown(self):
        self.live.stop.set()
        self.srv.shutdown()
        self.srv.server_close()
        super().tearDown()

    def fetch(self, path):
        with urllib.request.urlopen("http://127.0.0.1:%d%s" % (self.port, path), timeout=5) as r:
            return r.headers.get("Content-Type"), r.read()

    def test_follow_route_serves_the_page(self):
        for path in ("/follow", "/follow?s=a,b"):
            ctype, body = self.fetch(path)
            self.assertTrue(ctype.startswith("text/html"))
            self.assertIn(b'data-page="follow"', body)

    def test_api_follow(self):
        self.build()
        _, body = self.fetch("/api/follow?s=%s,nope" % SID[:8])
        d = json.loads(body)
        self.assertEqual([x["id"] for x in d["sessions"]], [SID, "nope"])
        self.assertTrue(d["sessions"][0]["steps"])
        self.assertTrue(d["sessions"][1]["missing"])
        _, body = self.fetch("/api/follow?s=%s&s=%s" % (SID, SID[:4]))   # repeated s= works too
        self.assertEqual(len(json.loads(body)["sessions"]), 2)
        self.assertEqual(json.loads(self.fetch("/api/follow")[1])["sessions"], [])

    def test_unknown_paths_still_404(self):
        with self.assertRaises(urllib.error.HTTPError):
            self.fetch("/follow/x")


def sub(i, aid, rec):
    """A subagent transcript record (its own sessionId is the parent's; isSidechain marks it)."""
    return dict(rec, isSidechain=True, agentId=aid)


class TestFollowSubagents(LiveFixture):
    """A session that delegates: two parallel background agents launched by one step (one launches a nested Explore),
    each with its own transcript under <session>/subagents/; the tile shows them as branches of that step."""

    def setUp(self):
        super().setUp()
        self.state = st.LiveState(redact=False)

    def agent(self, aid, meta, *recs):
        os.makedirs(self.subdir, exist_ok=True)
        path = os.path.join(self.subdir, "agent-%s.jsonl" % aid)
        if meta is not None and not os.path.exists(path[:-len(".jsonl")] + ".meta.json"):
            with open(path[:-len(".jsonl")] + ".meta.json", "w") as f:
                json.dump(meta, f)
        self.write(path, lines(*[sub(0, aid, r) for r in recs]))

    def launch(self):
        """Main: a prompt, one message with two background Agent calls, their results, end of the turn (t=0..5)."""
        self.write(self.main, lines(
            user_prompt(0, "audit the checkout module with parallel subagents"),
            assistant(1, "m1", [tool_use("tP1", "Agent", {"subagent_type": "general-purpose", "description": "Audit cart.py",
                                                         "prompt": "x", "run_in_background": True}),
                                tool_use("tP2", "Agent", {"subagent_type": "general-purpose", "description": "Audit tax.py",
                                                         "prompt": "y", "run_in_background": True})]),
            dict(result(2, "tP1"), message={"role": "user", "content": [{"type": "tool_result", "tool_use_id": "tP1",
                                                                         "content": "Async agent launched successfully."}]}),
            dict(result(2, "tP2"), message={"role": "user", "content": [{"type": "tool_result", "tool_use_id": "tP2",
                                                                         "content": "Async agent launched successfully."}]}),
            assistant(5, "m2", [{"type": "text", "text": "Both audits are running."}], stop="end_turn")))
        return self.step()

    def work(self):
        """p1 reads and launches a nested Explore (n1, which greps); p2 runs the tests (t=6..20)."""
        self.launch()
        self.agent("p1", {"agentType": "general-purpose", "description": "Audit cart.py", "toolUseId": "tP1"},
                   assistant(6, "p1m1", [tool_use("p1t1", "Read", {"file_path": "/srv/checkout-app/src/cart.py"})]),
                   result(7, "p1t1"),
                   assistant(8, "p1m2", [tool_use("p1t2", "Agent", {"subagent_type": "Explore",
                                                                     "description": "Map cart callers", "prompt": "z"})]))
        self.agent("p2", {"agentType": "general-purpose", "description": "Audit tax.py", "toolUseId": "tP2"},
                   assistant(9, "p2m1", [tool_use("p2t1", "Bash", {"command": "python3 -m pytest -q"})]))
        self.agent("n1", {"agentType": "Explore", "description": "Map cart callers", "toolUseId": "p1t2",
                          "parentAgentId": "p1"},
                   assistant(10, "n1m1", [tool_use("n1t1", "Grep", {"pattern": "cart_total"})]),
                   result(11, "n1t1"))
        return self.step()

    def at(self, i):
        return T0 + i

    def spawning(self, tile):
        hit = [x for x in tile["steps"] if x.get("agents")]
        self.assertEqual(len(hit), 1)
        return hit[0]

    def test_parallel_and_nested_branches(self):
        s = self.work()
        t = self.state.follow_tile(s, self.at(12))
        step = self.spawning(t)
        p1, p2 = step["agents"]                                          # siblings, in launch order
        self.assertEqual([(a["id"], a["type"], a["description"]) for a in (p1, p2)],
                         [("p1", "general-purpose", "Audit cart.py"), ("p2", "general-purpose", "Audit tax.py")])
        self.assertEqual((p1["status"], p2["status"]), ("running", "running"))
        self.assertTrue(p1["steps"] and p2["steps"])
        for a in (p1, p2):
            for x in a["steps"]:
                self.assertTrue({"i", "cat", "label", "cw", "usd", "open", "tested", "dur_s"} <= set(x))
        nested = [n for x in p1["steps"] for n in x.get("agents", ())]   # under the p1 step that launched it
        self.assertEqual([(n["id"], n["type"], n["description"]) for n in nested], [("n1", "Explore", "Map cart callers")])
        self.assertFalse([n for x in p2["steps"] for n in x.get("agents", ())])
        # cost rolls up: n1 into p1, both agents into the step that launched them, everything into the tile total
        a = s.agents
        self.assertAlmostEqual(p1["usd"], round(a["p1"]["usd"] + a["n1"]["usd"], 6), places=5)
        self.assertGreaterEqual(step["usd"] + 1e-6, p1["usd"] + p2["usd"])
        self.assertEqual(t["usd"], round(s.totals["usd"], 6))
        self.assertGreater(t["usd"], sum(x["usd"] for x in t["steps"]) - 1e-6 - p1["usd"] - p2["usd"])
        self.assertEqual(t["agents_running"], 3)
        self.assertEqual(t["status"], "running")
        self.assertEqual(t["now"]["agent"], "Explore")                   # the most recent subagent call
        json.dumps(t)

    def test_tile_keeps_moving_while_only_subagents_work(self):
        s = self.work()
        before = self.state.follow_tile(s, self.at(12))
        self.agent("p2", None, result(30, "p2t1"),
                   assistant(31, "p2m2", [tool_use("p2t2", "Edit", {"file_path": "/srv/checkout-app/src/tax.py",
                                                                    "old_string": "a", "new_string": "b"})]),
                   result(32, "p2t2"))
        s = self.step()
        after = self.state.follow_tile(s, self.at(33))
        self.assertGreater(after["seq"], before["seq"])                  # the tile sees a change ...
        self.assertEqual(after["steps_n"], before["steps_n"])            # ... with no new main-thread step
        p2 = self.spawning(after)["agents"][1]
        self.assertGreater(p2["steps_n"], self.spawning(before)["agents"][1]["steps_n"])
        self.assertGreater(after["usd"], before["usd"])

    def test_status_stays_running_while_a_subagent_works(self):
        s = self.work()
        self.assertEqual(s.last_kind, "end_turn")                        # the main turn is over
        # p2's test run has no result yet: a long call keeps it (and the session) running, quiet or not
        for dt in (60, 200, 500):
            self.assertEqual(self.state.follow_tile(s, self.at(11 + dt))["status"], "running", dt)
        self.assertEqual(self.state.follow_tile(s, self.at(11 + st.AGENT_CALL_S + 5))["status"], "waiting")
        # every subagent finishes: the session waits for the user
        self.agent("p2", None, result(40, "p2t1"), assistant(41, "p2m2", [{"type": "text", "text": "ok"}], stop="end_turn"))
        self.agent("n1", None, assistant(42, "n1m2", [{"type": "text", "text": "ok"}], stop="end_turn"))
        self.agent("p1", None, result(43, "p1t2"), assistant(44, "p1m3", [{"type": "text", "text": "ok"}], stop="end_turn"))
        s = self.step()
        t = self.state.follow_tile(s, self.at(50))
        self.assertEqual(t["status"], "waiting")
        self.assertEqual(t["agents_running"], 0)
        self.assertEqual({a["status"] for a in self.spawning(t)["agents"]}, {"done"})

    def test_session_end_while_subagents_work_is_not_ended(self):
        s = self.work()
        # a SessionEnd newer than every transcript record, while p2's call still runs
        self.state.apply_hook({"event": "SessionEnd", "session_id": SID, "ts": self.at(12)})
        self.assertEqual(self.state.follow_tile(s, self.at(20))["status"], "running")
        self.assertFalse(self.state.board(now=self.at(20))["sessions"][0]["ended"])
        self.agent("p2", None, result(13, "p2t1"), assistant(14, "p2m2", [{"type": "text", "text": "ok"}], stop="end_turn"))
        self.agent("p1", None, result(13, "p1t2"), assistant(14, "p1m3", [{"type": "text", "text": "ok"}], stop="end_turn"))
        self.agent("n1", None, assistant(14, "n1m2", [{"type": "text", "text": "ok"}], stop="end_turn"))
        s = self.step()
        self.state.apply_hook({"event": "SessionEnd", "session_id": SID, "ts": self.at(15)})
        self.assertEqual(self.state.follow_tile(s, self.at(20))["status"], "ended")

    def test_hooks_that_name_a_subagent(self):
        s = self.work()
        h = lambda ev, aid, i, **kw: self.state.apply_hook(dict({"event": ev, "session_id": SID, "agent_id": aid,
                                                                 "ts": self.at(i)}, **kw))  # noqa: E731
        self.agent("p2", None, result(12, "p2t1"))   # no open call left in p2's transcript
        self.agent("p1", None, result(12, "p1t2"), assistant(13, "p1m3", [{"type": "text", "text": "ok"}], stop="end_turn"))
        self.agent("n1", None, assistant(13, "n1m2", [{"type": "text", "text": "ok"}], stop="end_turn"))
        s = self.step()
        self.assertEqual(self.state.follow_tile(s, self.at(200))["status"], "waiting")   # p2 quiet for > 90 s
        h("SubagentStart", "agent-p2", 12, agent_type="general-purpose")
        h("PreToolUse", "agent-p2", 14, tool_name="Bash")
        self.assertEqual(s.hook["open_tools"], 0)            # a subagent's call is not the main thread's
        t = self.state.follow_tile(s, self.at(200))
        self.assertEqual(t["status"], "running")              # started, not stopped: still working
        self.assertEqual(t["agents_running"], 1)
        h("PostToolUse", "agent-p2", 300, tool_name="Bash")
        h("SubagentStop", "p2", 301)
        t = self.state.follow_tile(s, self.at(302))
        self.assertEqual((t["status"], t["agents_running"]), ("waiting", 0))
        self.assertEqual(self.spawning(t)["agents"][1]["status"], "idle")

    def move_on(self, n=8):
        """The main thread keeps working past its background agents: a new prompt, then n steps (t=12..)."""
        recs, t = [user_prompt(12, "now also update the docs")], 13
        for k in range(n):
            f = "/srv/checkout-app/docs/a%d.md" % k
            call = [("Read", {"file_path": f}), ("Edit", {"file_path": f, "old_string": "a", "new_string": "b"}),
                    ("Bash", {"command": "python3 -m pytest -q"})][k % 3]
            recs += [assistant(t, "q%d" % k, [tool_use("q%dt" % k, *call)]), result(t + 1, "q%dt" % k)]
            t += 2
        self.write(self.main, lines(*recs))
        return self.step(), t

    def test_running_branch_stays_pinned_while_the_main_thread_moves_on(self):
        self.work()
        s, t = self.move_on()
        tile = self.state.follow_tile(s, self.at(t))
        rows = tile["steps"]
        self.assertEqual(rows[0]["i"], 0)
        self.assertEqual([a["id"] for a in rows[0]["agents"]], ["p1", "p2"])
        self.assertTrue(all(a["live"] and not a["collapsed"] for a in rows[0]["agents"]))
        last = rows[-1]["i"]
        # every row is in an intent; the launching step's intent comes first
        self.assertEqual(rows[0]["intent"], 0)
        self.assertTrue(all(x["intent"] is not None for x in rows))
        # the launching step is older than the tile's latest steps: still shown, with its intent
        tile = self.state.follow_tile(s, self.at(t), last_n=3)
        self.assertEqual([x["i"] for x in tile["steps"]], [0, last - 2, last - 1, last])
        self.assertEqual([a["id"] for a in tile["steps"][0]["agents"]], ["p1", "p2"])
        self.assertEqual(tile["intents"][tile["steps"][0]["intent"]]["first"], 0)
        json.dumps(tile)
        # every agent finishes: nothing to pin, no early step
        self.agent("p2", None, result(40, "p2t1"), assistant(41, "p2m2", [{"type": "text", "text": "ok"}], stop="end_turn"))
        self.agent("n1", None, assistant(42, "n1m2", [{"type": "text", "text": "ok"}], stop="end_turn"))
        self.agent("p1", None, result(43, "p1t2"), assistant(44, "p1m3", [{"type": "text", "text": "ok"}], stop="end_turn"))
        s = self.step()
        tile = self.state.follow_tile(s, self.at(50), last_n=3)
        self.assertEqual([x["i"] for x in tile["steps"]], [last - 2, last - 1, last])

    def test_finished_branches_collapse_unless_latest(self):
        self.work()
        self.agent("n1", None, assistant(12, "n1m2", [{"type": "text", "text": "ok"}], stop="end_turn"))
        self.agent("p1", None, result(13, "p1t2"), assistant(14, "p1m3", [{"type": "text", "text": "ok"}], stop="end_turn"))
        s = self.step()
        p1, p2 = self.spawning(self.state.follow_tile(s, self.at(15)))["agents"]
        self.assertEqual((p1["status"], p1["live"], p1["collapsed"]), ("done", False, True))   # one header line
        self.assertEqual((p2["status"], p2["live"], p2["collapsed"]), ("running", True, False))
        n1, = [n for x in p1["steps"] for n in x.get("agents", ())]
        self.assertFalse(n1["collapsed"])                    # the latest of its own level
        self.agent("p2", None, result(16, "p2t1"), assistant(17, "p2m2", [{"type": "text", "text": "ok"}], stop="end_turn"))
        s = self.step()
        t = self.state.follow_tile(s, self.at(18))
        p1, p2 = self.spawning(t)["agents"]
        self.assertEqual([(a["live"], a["collapsed"]) for a in (p1, p2)], [(False, True), (False, False)])   # p2: latest
        self.assertTrue(all(isinstance(a["intents"], list) for a in (p1, p2)))   # a branch lists intents too

    def test_branch_rules(self):
        def step(i, closed=True, agents=()):
            return {"i": i, "open": not closed, "agents": [dict(a, steps=a.get("steps", [])) for a in agents]}
        live = {"status": "running"}
        done = {"status": "done"}
        rows = [step(0), step(1, agents=[live]), step(2), step(3, closed=False), step(4), step(5), step(6),
                step(7, agents=[done, done]), step(8), step(9)]
        self.assertTrue(st.follow_branches(rows))
        self.assertEqual([(a["live"], a["collapsed"]) for a in rows[7]["agents"]], [(False, True), (False, False)])
        # nested: a running agent under a finished one keeps it expanded and live
        outer = {"status": "done", "steps": [{"i": 0, "open": False, "agents": [{"status": "running", "steps": []}]}]}
        rows = [{"i": 0, "open": False, "agents": [outer, {"status": "done", "steps": []}]}]
        self.assertTrue(st.follow_branches(rows))
        self.assertEqual((outer["live"], outer["collapsed"]), (True, False))
        rows = [step(i) for i in range(5)]
        self.assertFalse(st.follow_branches(rows))

    def test_launched_subagent_without_a_transcript_yet(self):
        s = self.launch()
        t = self.state.follow_tile(s, self.at(10))
        self.assertEqual(t["status"], "running")             # the turn ended, but two agents are starting
        self.assertEqual(t["agents_running"], 2)
        self.assertEqual([(a["id"], a["status"], a["description"]) for a in self.spawning(t)["agents"]],
                         [(None, "starting", "Audit cart.py"), (None, "starting", "Audit tax.py")])
        # never written after 90 s: no longer counted
        self.assertEqual(self.state.follow_tile(s, self.at(5 + st.WORKING_S + 1))["status"], "waiting")

    def test_many_parallel_agents_are_capped(self):
        n = st.FOLLOW_AGENTS + 3
        calls = [tool_use("tw%d" % k, "Agent", {"subagent_type": "Explore", "description": "part %d" % k, "prompt": "p",
                                                "run_in_background": True}) for k in range(n)]
        self.write(self.main, lines(user_prompt(0, "survey everything"), assistant(1, "m1", calls),
                                    assistant(2, "m2", [{"type": "text", "text": "running"}], stop="end_turn")))
        self.step()
        for k in range(n):   # the first three are already done, the rest still work
            recs = [assistant(3 + k, "w%dm1" % k, [tool_use("w%dt" % k, "Grep", {"pattern": "x"})]), result(4 + k, "w%dt" % k)]
            if k < 3:
                recs.append(assistant(4 + k, "w%dm2" % k, [{"type": "text", "text": "ok"}], stop="end_turn"))
            self.agent("w%d" % k, {"agentType": "Explore", "description": "part %d" % k, "toolUseId": "tw%d" % k}, *recs)
        s = self.step()
        step = self.spawning(self.state.follow_tile(s, self.at(4 + n)))
        self.assertEqual(len(step["agents"]), st.FOLLOW_AGENTS)
        self.assertEqual(step["agents_more"], 3)
        self.assertEqual({a["status"] for a in step["agents"]}, {"running"})   # the active ones win the budget
        shown = [a["id"] for a in step["agents"]]
        self.assertEqual(shown, ["w%d" % k for k in range(3, n)])             # in launch order

    def test_polling_reuses_each_subagents_steps(self):
        s = self.work()
        self.state.follow_tile(s, self.at(12))
        cached = {k: v for k, v in s.agent_steps_cache.items()}
        self.agent("p2", None, result(13, "p2t1"))   # only p2 changes
        s = self.step()
        self.state.follow_tile(s, self.at(14))
        self.assertIs(s.agent_steps_cache["n1"], cached["n1"])
        self.assertIsNot(s.agent_steps_cache["p2"], cached["p2"])
        self.agent("n1", None, assistant(15, "n1m2", [tool_use("n1t2", "Read", {"file_path": "/srv/a.py"})]))
        s = self.step()
        self.state.follow_tile(s, self.at(16))
        self.assertIsNot(s.agent_steps_cache["p1"], cached["p1"])           # a nested agent's cost is its parent's too

    def test_redacted_branches_leak_nothing(self):
        self.state = st.LiveState(redact=True)
        s = self.work()
        t = self.state.follow_tile(s, self.at(12))
        self.assertEqual([a["description"] for a in self.spawning(t)["agents"]], [None, None])
        text = json.dumps(t)
        # (step labels follow the labelling policy: masked paths, as on the canvas and the main thread)
        for leak in ("cart.py", "tax.py", "/srv", "checkout-app", "cart_total", "pytest -q"):
            self.assertNotIn(leak, text)

    def test_api_follow_serves_the_branches(self):
        live = server.Live(self.root, hooks=False, xhints=False)
        live.state = self.state
        live.tailer = self.tailer
        self.work()
        srv = server.make_server(live, "127.0.0.1", 0)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            with urllib.request.urlopen("http://127.0.0.1:%d/api/follow?s=%s" % (srv.server_address[1], SID[:8]),
                                        timeout=5) as r:
                d = json.loads(r.read())
        finally:
            srv.shutdown()
            srv.server_close()
        step = self.spawning(d["sessions"][0])
        self.assertEqual([a["id"] for a in step["agents"]], ["p1", "p2"])


class TestHookAgentIds(unittest.TestCase):
    def test_event_line_keeps_the_agent(self):
        from tokenscope.live import hook
        ev = hook.event_line({"hook_event_name": "SubagentStart", "session_id": "s", "agent_id": "agent-abc",
                              "agent_type": "Explore", "prompt": "secret"}, now=1.0)
        self.assertEqual(ev, {"ts": 1.0, "event": "SubagentStart", "session_id": "s", "agent_id": "agent-abc",
                              "agent_type": "Explore"})

    def test_hook_agent_id(self):
        self.assertEqual(st.hook_agent_id("agent-a4431591fe2397dde"), "a4431591fe2397dde")
        self.assertEqual(st.hook_agent_id("a44"), "a44")
        for bad in (None, "", "../x", "a b"):
            self.assertIsNone(st.hook_agent_id(bad))


class TestOddLabels(unittest.TestCase):
    def test_reasons(self):
        cases = {
            "fix the checkout total tax in cart.py": None,
            "add a dark mode toggle to the settings page": None,
            "please fix the checkout total tax": "filler",
            "ok so the export is broken": "filler",
            "the export fails\nsee the log below": "multi-line",
            "fix /srv/app/src/export.py so it writes utf-8": "path",
            "read https://example.com/spec and implement it": "path",
            "why does `export()` crash": "code",
            "TypeError: cannot read property x of undefined": "code",
            "<command-message>review</command-message>": "markup",
            "implement " + "the thing " * 12: "long",
            "": None,
        }
        for text, why in cases.items():
            self.assertEqual(goalname.reads_oddly(text), why, text)

    def test_plain_prompts_keep_their_keyword_name(self):
        for text in ("fix the checkout total tax in cart.py", "add a dark mode toggle to the settings page"):
            self.assertEqual(goalname.goal_label(text), goalname.keyword_name(text))

    def test_odd_prompts_get_a_clean_short_name(self):
        text = "ok so here's the thing, /home/x/proj/foo.py line 3:\n```\ndef f(): pass\n```\nit crashes on save"
        name = goalname.goal_label(text)
        self.assertLessEqual(len(name), goalname.MAX_LEN)
        for junk in ("home", "def", "```", "\n", "ok so"):
            self.assertNotIn(junk, name)
        self.assertIn("foo.py", name)                       # a path keeps its file name ...
        self.assertNotIn("foo.py", goalname.goal_label(text, redact=True))   # ... unless redacted
        self.assertEqual(goalname.goal_label("<command-message>review</command-message>\n<command-name>/review"
                                             "</command-name>"), "Review")
        self.assertEqual(goalname.goal_label("yes"), "Work on")
        self.assertEqual(goalname.goal_label("please fix the checkout total tax"), "Fix: checkout total tax")

    def test_clean_prompt(self):
        self.assertEqual(goalname.clean_prompt("Hey, please `run_all()` on https://x.io/a and /a/b/c.py\nthen stop"),
                         "run_all() on and c.py. then stop.")

    def test_live_state_names_odd_prompts_cleanly(self):
        s = st.LiveState(redact=False)
        sess = s.session(SID, "/x", "p")
        s._new_prompt(sess, sess.main, "ok so\n```\nraise ValueError\n```\nfix the exporter crash", 1.0)
        self.assertEqual(sess.goal["name"], sess.prompts[0]["name"])
        self.assertNotIn("```", sess.goal["name"])
        self.assertTrue(sess.goal["name"].startswith("Fix"))

    def test_top_goal_from_raw_text_is_cleaned(self):
        self.assertEqual(top._raw_goal("add a csv export"), "add a csv export")   # reads fine: kept
        self.assertEqual(top._raw_goal("please fix the checkout total tax"), "Fix: checkout total tax")
        self.assertIsNone(top._raw_goal(None))


class TestOddHaiku(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.cache = os.path.join(self.tmp, "goal-names.json")
        self.addCleanup(shutil.rmtree, self.tmp)
        self.calls, self.got = [], {}
        self.done = threading.Event()

    def fake(self, text):   # never the network
        self.calls.append(text)
        return "Fix exporter crash"

    def named(self, key, name):
        self.got[key] = name
        self.done.set()

    def test_only_odd_prompts_reach_the_model_and_names_are_cached(self):
        n = goalname.HaikuNamer(self.named, namer=self.fake, only_odd=True, cache_path=self.cache)
        n.request("s1", "add a csv export")                         # reads fine: no call
        n.request("s2", "ok so the exporter crashes\nfix it")
        self.assertTrue(self.done.wait(5))
        self.assertEqual(self.got, {"s2": "Fix exporter crash"})
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(goalname.cached_name("ok so the exporter crashes\nfix it", self.cache), "Fix exporter crash")
        # a fresh namer (another server) answers from the disk cache without a model call
        self.done.clear()
        n2 = goalname.HaikuNamer(self.named, namer=self.fake, only_odd=True, cache_path=self.cache)
        n2.request("s3", "ok so the exporter crashes\nfix it")
        self.assertTrue(self.done.wait(5))
        self.assertEqual(self.got["s3"], "Fix exporter crash")
        self.assertEqual(len(self.calls), 1)

    def test_cache_key_is_the_masked_model_input(self):
        self.assertEqual(goalname.cache_key("fix /home/alice/a.py"), goalname.cache_key("fix /home/bob/a.py"))
        self.assertNotEqual(goalname.cache_key("fix a"), goalname.cache_key("fix b"))

    def test_live_names_odd_goals_in_the_background(self):
        os.environ["TOKENSCOPE_CACHE_DIR"] = self.tmp
        self.addCleanup(os.environ.pop, "TOKENSCOPE_CACHE_DIR", None)
        live = server.Live(self.tmp, hooks=False, xhints=False, odd_goals=True, goal_namer=self.fake)
        self.assertTrue(live.namer.only_odd)
        sess = live.state.session(SID, "/x", "p")
        with live.lock:
            live.state._new_prompt(sess, sess.main, "add a csv export", 1.0)   # reads fine: keyword name stays
            live.state._new_prompt(sess, sess.main, "ok so the exporter crashes\nfix it", 2.0)
        deadline = time.time() + 5
        while sess.prompts[1]["source"] != "haiku" and time.time() < deadline:
            time.sleep(0.01)
        self.assertEqual((sess.prompts[1]["name"], sess.prompts[1]["source"]), ("Fix exporter crash", "haiku"))
        self.assertEqual(sess.goal["source"], "keyword")
        self.assertEqual(self.calls, ["ok so the exporter crashes\nfix it"])
        self.assertTrue(os.path.exists(os.path.join(self.tmp, "goal-names.json")))

    def test_no_namer_without_the_options(self):
        self.assertIsNone(server.Live(self.tmp, hooks=False, xhints=False).namer)
        live = server.Live(self.tmp, hooks=False, xhints=False, haiku=True, goal_namer=self.fake)
        self.assertFalse(live.namer.only_odd)   # --haiku: every goal

    def test_cached_goals_never_call_the_model(self):
        """The server's default: an odd prompt gets the name an earlier run cached, never a model call."""
        os.environ["TOKENSCOPE_CACHE_DIR"] = self.tmp
        self.addCleanup(os.environ.pop, "TOKENSCOPE_CACHE_DIR", None)
        goalname.store_name("ok so the exporter crashes\nfix it", "Fix exporter crash", goalname.cache_path())
        live = server.Live(self.tmp, hooks=False, xhints=False, cached_goals=True, goal_namer=self.fake)
        sess = live.state.session(SID, "/x", "p")
        with live.lock:
            live.state._new_prompt(sess, sess.main, "ok so the exporter crashes\nfix it", 1.0)
            live.state._new_prompt(sess, sess.main, "uh\nanother odd one, not cached", 2.0)
        deadline = time.time() + 5
        while sess.goal["source"] != "haiku" and time.time() < deadline:
            time.sleep(0.01)
        time.sleep(0.05)
        self.assertEqual((sess.goal["name"], sess.goal["source"]), ("Fix exporter crash", "haiku"))
        self.assertEqual(sess.prompts[1]["source"], "keyword")   # a miss keeps the cleaned-up name
        self.assertEqual(self.calls, [])

    def test_server_main_flags(self):
        from test_live import TestLiveCliArgs
        runner = TestLiveCliArgs("test_args_reach_the_server")
        os.environ["TOKENSCOPE_CACHE_DIR"] = self.tmp
        self.addCleanup(os.environ.pop, "TOKENSCOPE_CACHE_DIR", None)
        live = runner.run_main(server.main, ["--root", self.tmp, "--no-xhints"])
        self.assertTrue(live.namer.only_odd)
        self.assertIs(live.namer.namer, server._no_model)        # default: cache only, no network
        live = runner.run_main(server.main, ["--root", self.tmp, "--no-xhints", "--haiku-odd"])
        self.assertTrue(live.namer.only_odd)
        self.assertIs(live.namer.namer, goalname.haiku_name)    # opt-in: Haiku for odd prompts only


class TestFollowUi(unittest.TestCase):
    def read(self, name):
        with open(os.path.join(STATIC, name), encoding="utf-8") as f:
            return f.read()

    def test_page_and_board_links(self):
        page = self.read("follow.html")
        for x in ('data-page="follow"', 'id="tiles"', 'id="add"', 'id="picker"', 'href="/"', "/static/live.js"):
            self.assertIn(x, page)
        self.assertIn('id="follow-link"', self.read("board.html"))
        js = self.read("live.js")
        board = js[js.index("// ---------- board ----------"):js.index("// ---------- follow ----------")]
        self.assertIn('el("button", "pin"', board)   # a pin button on each board card
        css = self.read("live.css")
        for sel in ('.tiles[data-n="1"]', '.tiles[data-n="2"]', '.tiles[data-n="3"]', ".fnode", ".scard .pin"):
            self.assertIn(sel, css)
        follow = js[js.index("// ---------- follow ----------"):js.index("// ---------- session ----------")]
        for x in ("syncBranch", "agents_more", "agents_running", "w.agent"):   # subagent branches in a tile
            self.assertIn(x, follow)
        for x in ("a.collapsed", "a.live", "pinScrollTop"):   # live branches stay pinned
            self.assertIn(x, follow)
        for x in ("s.intents", "a.intents", '"fint', '"fother"', " other step"):   # intent headers, latest step only
            self.assertIn(x, follow)
        self.assertNotIn("folds", follow)
        for sel in (".fint", ".fother", ".fint.pending", ".fag.collapsed>.fasteps", ".fag.foldable"):
            self.assertIn(sel, css)
        for sel in (".fbr", ".fag", ".fah", ".fasteps", ".fmore", ".fth .fagn"):
            self.assertIn(sel, css)

    @unittest.skipUnless(shutil.which("node"), "node not installed")
    def test_pin_helpers(self):
        script = r"""
const fs = require("fs"), vm = require("vm");
const win = {}, ctx = {window: win, document: {addEventListener() {}}, console};
ctx.window.TS_SOURCE = {};
vm.createContext(ctx);
vm.runInContext(fs.readFileSync(process.argv[1], "utf8"), ctx);
const p = win.TSLive.pins, out = {};
out.parse = p.parse(" a, b,,a ,c,d,e");
out.urlWins = p.resolve("?s=x%2Cy", "a,b");
out.stored = p.resolve("?other=1", "a,b");
out.emptyUrl = p.resolve("?s=", "a");
out.add = p.toggle(["a"], "b");
out.remove = p.toggle(["a", "b"], "a");
out.full = p.toggle(["a", "b", "c", "d"], "e");
out.href = p.href(["a", "b c"]);
out.hrefNone = p.href([]);
out.max = p.MAX;
const pin = win.TSLive.pinScrollTop;
out.scroll = [pin(300, 1000, null), pin(300, 1000, 800), pin(300, 1000, 500), pin(300, 200, 50)];
console.log(JSON.stringify(out));
"""
        r = subprocess.run(["node", "-e", script, os.path.join(STATIC, "live.js")], capture_output=True, text=True,
                           timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr)
        out = json.loads(r.stdout)
        self.assertEqual(out, {"parse": ["a", "b", "c", "d"], "urlWins": ["x", "y"], "stored": ["a", "b"],
                               "emptyUrl": ["a"], "add": ["a", "b"], "remove": ["b"], "full": ["a", "b", "c", "d"],
                               "href": "/follow?s=a,b%20c", "hrefNone": "/follow", "max": 4,
                               # end; end (the live region fits); its top (pinned); a short flow
                               "scroll": [700, 700, 496, 0]})


if __name__ == "__main__":
    unittest.main()
