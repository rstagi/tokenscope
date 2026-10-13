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
from test_live import SID, LiveFixture, assistant, lines, result, tool_use, user_prompt  # noqa: E402

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
                  "last_ts", "seq", "steps_n", "now", "steps"):
            self.assertIn(k, t)
        self.assertEqual(t["status"], "waiting")   # the turn ended (end_turn) moments ago
        self.assertIsNone(t["now"])                # nothing runs while waiting
        self.assertTrue(t["steps"])
        self.assertEqual(len(t["steps"]), t["steps_n"])
        for x in t["steps"]:
            self.assertEqual(set(x), {"i", "cat", "label", "cw", "usd", "open", "tested", "dur_s"})
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
console.log(JSON.stringify(out));
"""
        r = subprocess.run(["node", "-e", script, os.path.join(STATIC, "live.js")], capture_output=True, text=True,
                           timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr)
        out = json.loads(r.stdout)
        self.assertEqual(out, {"parse": ["a", "b", "c", "d"], "urlWins": ["x", "y"], "stored": ["a", "b"],
                               "emptyUrl": ["a"], "add": ["a", "b"], "remove": ["b"], "full": ["a", "b", "c", "d"],
                               "href": "/follow?s=a,b%20c", "hrefNone": "/follow", "max": 4})


if __name__ == "__main__":
    unittest.main()
