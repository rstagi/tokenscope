"""Top spenders by time window (tokenscope/top.py): window boundaries, a session straddling the window start split by
call time, shares summing to 100 %, the cached index on repeated calls, the CLI table and the live server's /api/top;
grouped by session, goal (follow-ups and resumed sessions, in-window calls only) and turn (one per real prompt)."""
import contextlib
import io
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tokenscope import cli, pricing, top as tp  # noqa: E402
from tokenscope.live import server  # noqa: E402
from tokenscope.live.state import parse_ts  # noqa: E402
from tokenscope.xindex import XIndex  # noqa: E402
from tests.test_recap import PASS, T0, Sess, bash, edit, read  # noqa: E402
from tests.test_xindex import write_session  # noqa: E402

A = "aaaaaaaa-0000-0000-0000-000000000000"
B = "bbbbbbbb-0000-0000-0000-000000000000"


def work():
    """Two prompts: read + edit, then a test run and an answer (sonnet-4-5 calls of 5k / 30k / 8k / 12k CW)."""
    return (Sess().prompt("fix the store totals").turn(read("store.py"), cw=5_000).turn(edit("store.py"), cw=30_000)
            .prompt("now run the tests").turn(bash("python3 -m pytest -q"), out=PASS, cw=8_000)
            .turn(read("models.py"), cw=12_000).answer())


def calls(sess):
    """[(ts, usd)] of a synthetic session's assistant messages."""
    out = []
    for r in sess.recs:
        if r["type"] == "assistant":
            u = r["message"]["usage"]
            usage = {"input": u.get("input_tokens", 0), "cache_write": u.get("cache_creation_input_tokens", 0),
                     "cache_read": u.get("cache_read_input_tokens", 0), "output": u.get("output_tokens", 0)}
            out.append((parse_ts(r["timestamp"]), pricing.cost_usd("claude-sonnet-4-5", usage)))
    return out


class TopSpenders(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.root = os.path.join(self.tmp, "projects")
        self.old_cache = os.environ.get("TOKENSCOPE_CACHE_DIR")
        os.environ["TOKENSCOPE_CACHE_DIR"] = os.path.join(self.tmp, "cache")
        self.sess = work()
        write_session(self.root, A, self.sess)
        self.calls = calls(self.sess)

    def tearDown(self):
        if self.old_cache is None:
            os.environ.pop("TOKENSCOPE_CACHE_DIR", None)
        else:
            os.environ["TOKENSCOPE_CACHE_DIR"] = self.old_cache
        shutil.rmtree(self.tmp)

    def at(self, now, window="5h", limit=None):
        return tp.top_spenders(window, now=now, root=self.root, limit=limit, jobs=1)

    def test_windows_and_now(self):
        self.assertEqual(tp.window_seconds("5h"), ("5h", 5 * 3600))
        self.assertEqual(tp.window_seconds("day"), ("24h", 86400))
        self.assertEqual(tp.window_seconds("week"), ("7d", 7 * 86400))
        self.assertEqual(tp.window_seconds("30d"), ("30d", 30 * 86400))
        with self.assertRaises(ValueError):
            tp.window_seconds("3h")
        self.assertEqual(tp.parse_now("2026-10-12T09:00:00Z"), tp.parse_now("2026-10-12T09:00:00"))   # naive = UTC
        self.assertEqual(tp.parse_now("2026-10-12T11:00:00+02:00"), tp.parse_now("2026-10-12T09:00:00Z"))
        with self.assertRaises(ValueError):
            tp.parse_now("yesterday")

    def test_boundaries_include_both_ends_and_nothing_outside(self):
        first, last = self.calls[0][0], self.calls[-1][0]
        whole = sum(u for _, u in self.calls)
        # the window starts exactly at the first call and ends exactly at the last: all of it
        r = tp.top({A: self._entry()}, last, last - first)
        self.assertAlmostEqual(r["total"]["usd"], whole, places=5)
        self.assertEqual(r["total"]["calls"], len(self.calls))
        # one second later at both ends: the first call falls out
        r = tp.top({A: self._entry()}, last + 1, last - first)
        self.assertEqual(r["total"]["calls"], len(self.calls) - 1)
        # a window that ends before the first call, or starts after the last: nothing
        self.assertEqual(self.at(first - 1)["sessions"], [])
        self.assertEqual(self.at(last + 5 * 3600 + 1)["sessions"], [])
        self.assertEqual(self.at(last + 5 * 3600)["total"]["calls"], 1)   # start == last call: included

    def _entry(self):
        x = XIndex(self.root)
        x.build(jobs=1)
        return x.sessions[A]

    def test_session_straddling_the_window_start_is_split_by_call_time(self):
        # the window starts between the 2nd call (the edit) and the 3rd (the test run)
        cut = (self.calls[1][0] + self.calls[2][0]) / 2
        now = cut + 5 * 3600
        r = self.at(now)
        inside = [u for t, u in self.calls if t >= cut]
        self.assertEqual(r["total"]["calls"], len(inside))
        self.assertAlmostEqual(r["total"]["usd"], sum(inside), places=5)
        s = r["sessions"][0]
        self.assertEqual((s["sid"], s["short"], s["calls"]), (A, A[:8], len(inside)))
        self.assertAlmostEqual(s["usd"], sum(inside), places=5)
        # wall time: from the window start to the session's last event, not from its first call
        self.assertLessEqual(s["wall_s"], self.calls[-1][0] - cut + 2)
        self.assertGreater(s["wall_s"], self.calls[-1][0] - self.calls[2][0] - 1)
        # the read and edit steps before the cut are gone; the steps after it carry what is inside
        cats = {x["cat"] for x in r["steps"]}
        self.assertNotIn("change", cats)
        self.assertIn("verify", cats)
        self.assertAlmostEqual(sum(x["usd"] for x in r["steps"]), sum(inside), places=5)
        # and the part before the cut is exactly the rest
        before = self.at(cut - 0.001, window="5h")
        self.assertAlmostEqual(before["total"]["usd"] + r["total"]["usd"], sum(u for _, u in self.calls), places=5)

    def test_shares_sum_to_100_percent(self):
        write_session(self.root, B, Sess().prompt("add a cli flag").turn(read("cli.py"), cw=50_000)
                      .turn(edit("cli.py"), cw=70_000).answer())
        r = self.at(T0 + 3600, window="24h")
        self.assertEqual(r["share_of"], "usd")
        self.assertEqual([x["sid"] for x in r["sessions"]], [B, A])   # B costs more
        for key in ("sessions", "intents", "steps"):
            self.assertAlmostEqual(sum(x["share"] for x in r[key]), 100.0, delta=0.05, msg=key)
        self.assertAlmostEqual(sum(x["usd"] for x in r["sessions"]), r["total"]["usd"], places=5)
        self.assertEqual(r["total"]["sessions"], 2)
        for x in r["intents"] + r["steps"]:
            self.assertLessEqual(x["active_s"], x["wall_s"] + 0.01)
        self.assertEqual(len(self.at(T0 + 3600, window="24h", limit=1)["steps"]), 1)

    def test_repeated_calls_hit_the_cache(self):
        first = self.at(T0 + 3600)
        self.assertEqual(first["index"]["replayed"], 1)
        again = self.at(T0 + 3600)
        self.assertEqual(again["index"]["replayed"], 0)   # keyed by path + size + mtime: nothing replayed
        self.assertEqual(again["sessions"], first["sessions"])
        with open(os.path.join(self.root, "-repo", A + ".jsonl"), "a", encoding="utf-8") as f:
            f.write("\n")
        self.assertEqual(self.at(T0 + 3600)["index"]["replayed"], 1)   # changed transcript: replayed again
        self.assertTrue(os.path.isdir(os.path.join(self.tmp, "cache", "xindex")))

    def test_cli_table(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = cli.main(["top", "--window", "24h", "--index-root", self.root, "--now",
                           tp.iso(T0 + 3600), "--limit", "2"])
        self.assertEqual(rc, 0)
        text = out.getvalue()
        self.assertIn("Top spenders, last 24h", text)
        for head in ("SESSIONS", "INTENTS", "STEPS"):
            self.assertIn(head, text)
        self.assertIn("100.0%", text)
        self.assertIn(A[:8], text)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            cli.main(["top", "--window", "5h", "--index-root", self.root, "--now", tp.iso(T0 - 3600), "--json"])
        self.assertEqual(json.loads(out.getvalue())["sessions"], [])

    def test_live_server_api_top(self):
        live = server.Live(self.root, hooks=False, index_root=self.root)
        self.assertTrue(live.top("5h", T0 + 3600)["pending"])   # first call: the index builds in the background
        for _ in range(200):
            if live.top_index is not None:
                break
            time.sleep(0.05)
        srv = server.make_server(live, "127.0.0.1", 0)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            url = "http://127.0.0.1:%d/api/top?window=day&now=%s" % (srv.server_address[1], tp.iso(T0 + 3600))
            with urllib.request.urlopen(url) as resp:
                r = json.loads(resp.read())
            self.assertEqual(r["window"], "24h")
            self.assertEqual(r["sessions"][0]["sid"], A)
            self.assertEqual(r["sessions"][0]["project"], "project-A")   # redacted by default
            self.assertNotIn("index", r)
            with self.assertRaises(urllib.error.HTTPError) as e:
                urllib.request.urlopen(url.replace("window=day", "window=3h"))
            self.assertEqual(e.exception.code, 400)
        finally:
            srv.shutdown()
            srv.server_close()


def resumed():
    """Session B picks up A's task: a follow-up opens it, then a new goal (one 40k CW call each)."""
    return (Sess().prompt("continue", uuid="b-p1").turn(read("store.py"), cw=40_000)
            .prompt("add a cli flag for verbose output", uuid="b-p2").turn(edit("cli.py"), cw=40_000).answer())


class TopBy(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.root = os.path.join(self.tmp, "projects")
        self.old_cache = os.environ.get("TOKENSCOPE_CACHE_DIR")
        os.environ["TOKENSCOPE_CACHE_DIR"] = os.path.join(self.tmp, "cache")
        a = (Sess().prompt("fix the store totals", uuid="a-p1").turn(read("store.py"), cw=5_000)
             .turn(edit("store.py"), cw=30_000)
             .prompt("now run the tests", uuid="a-p2").turn(bash("python3 -m pytest -q"), out=PASS, cw=8_000)
             .turn(read("models.py"), cw=12_000).answer())
        a.recs.insert(2, {"type": "user", "timestamp": a.recs[1]["timestamp"], "isMeta": True, "uuid": "meta",
                          "message": {"role": "user", "content": "<local-command-stdout>ok</local-command-stdout>"}})
        b = resumed()
        for r in b.recs:   # B starts an hour after A
            r["timestamp"] = tp.iso(parse_ts(r["timestamp"]) + 3600)
        write_session(self.root, A, a)
        write_session(self.root, B, b)
        self.a, self.b = calls(a), calls(b)

    def tearDown(self):
        if self.old_cache is None:
            os.environ.pop("TOKENSCOPE_CACHE_DIR", None)
        else:
            os.environ["TOKENSCOPE_CACHE_DIR"] = self.old_cache
        shutil.rmtree(self.tmp)

    def at(self, by, now=T0 + 7200, window="24h"):
        return tp.top_spenders(window, now=now, root=self.root, limit=None, jobs=1, by=by)

    def test_parse_by(self):
        self.assertEqual([tp.parse_by(x) for x in ("goal", "Turns", "", None, "sessions")],
                         ["goal", "turn", "intent", "intent", "session"])
        with self.assertRaises(ValueError):
            tp.parse_by("step")

    def test_by_intent_is_the_default_and_unchanged(self):
        r, d = self.at("intent"), tp.top_spenders("24h", now=T0 + 7200, root=self.root, limit=None, jobs=1)
        self.assertEqual(r, dict(d, index=r["index"]))
        self.assertEqual(r["by"], "intent")
        self.assertEqual(sorted(k for k in r if isinstance(r[k], list)), ["intents", "sessions", "steps"])

    def test_by_session(self):
        r = self.at("session")
        self.assertEqual(sorted(k for k in r if isinstance(r[k], list)), ["sessions"])
        self.assertEqual([x["sid"] for x in r["sessions"]], [B, A])
        self.assertAlmostEqual(r["sessions"][1]["usd"], sum(u for _, u in self.a), places=5)
        self.assertEqual(r["sessions"][1]["session_label"], r["sessions"][1]["label"])
        self.assertEqual(r["sessions"], self.at("intent")["sessions"])

    def test_by_turn_one_row_per_real_prompt(self):
        r = self.at("turn")
        rows = {x["id"]: x for x in r["turns"]}
        self.assertEqual(sorted(rows), ["a-p1", "a-p2", "b-p1", "b-p2"])   # never the meta record
        self.assertEqual(rows["a-p2"]["label"], "now run the tests")
        self.assertEqual((rows["a-p2"]["sid"], rows["a-p2"]["n"], rows["a-p2"]["calls"]), (A, 2, 3))   # + the answer
        self.assertAlmostEqual(rows["a-p1"]["usd"], self.a[0][1] + self.a[1][1], places=5)
        self.assertEqual(rows["b-p2"]["session_label"], "Work on: continue")
        self.assertGreater(rows["a-p1"]["wall_s"], 0)
        self.assertAlmostEqual(sum(x["share"] for x in r["turns"]), 100.0, delta=0.05)
        self.assertAlmostEqual(sum(x["usd"] for x in r["turns"]), r["total"]["usd"], places=5)

    def test_turn_label_is_the_cut_prompt_text(self):
        self.assertEqual(tp.turn_label("x" * tp.TURN_CHARS), "x" * tp.TURN_CHARS)
        cut = tp.turn_label("word " * 40)
        self.assertEqual(len(cut), tp.TURN_CHARS)
        self.assertTrue(cut.endswith("…"))

    def test_by_goal_spans_follow_ups_and_the_resumed_session(self):
        r = self.at("goal")
        self.assertEqual(len(r["goals"]), 2)
        fix, cli_flag = sorted(r["goals"], key=lambda g: g["id"])
        self.assertEqual((fix["id"], fix["label"]), ("a-p1", "Fix: store totals"))
        self.assertEqual((fix["sessions"], fix["turns"]), (2, 3))   # A's two prompts + B's "continue"
        self.assertAlmostEqual(fix["usd"], sum(u for _, u in self.a) + self.b[0][1], places=5)
        self.assertEqual((fix["sid"], fix["session_label"]), (A, "Fix: store totals"))   # most of its $ is in A
        self.assertEqual((cli_flag["sid"], cli_flag["turns"], cli_flag["sessions"]), (B, 1, 1))
        self.assertAlmostEqual(sum(g["usd"] for g in r["goals"]), r["total"]["usd"], places=5)

    def test_by_goal_costs_only_in_window_calls(self):
        # the window is B's span: A's calls (an hour earlier) are out, B's "continue" turn still counts for A's goal
        r = tp.top(self._sessions(), self.b[-1][0], self.b[-1][0] - self.b[0][0], None, "goal")
        fix = next(g for g in r["goals"] if g["id"] == "a-p1")
        self.assertAlmostEqual(fix["usd"], self.b[0][1], places=5)
        self.assertEqual((fix["sid"], fix["sessions"], fix["turns"]), (B, 1, 1))
        self.assertAlmostEqual(r["total"]["usd"], sum(u for _, u in self.b), places=5)

    def _sessions(self):
        x = XIndex(self.root)
        x.build(jobs=1)
        return x.sessions

    def test_cli_by(self):
        def run(*extra):
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                self.assertEqual(cli.main(["top", "--window", "24h", "--index-root", self.root, "--now",
                                           tp.iso(T0 + 7200), "--limit", "3"] + list(extra)), 0)
            return out.getvalue()
        text = run("--by", "goal")
        self.assertIn("Top spenders by goal, last 24h", text)
        self.assertIn("GOALS", text)
        self.assertIn("Fix: store totals  +1 session\n", text)   # opened its session: no « session goal
        self.assertNotIn("INTENTS", text)
        text = run("--by", "turn")
        self.assertIn("TURNS", text)
        self.assertIn("fix the store totals  « Fix: store totals", text)
        text = run("--by", "session")
        self.assertIn("SESSIONS", text)
        self.assertNotIn("TURNS", text)
        self.assertEqual(run("--by", "intent"), run())
        self.assertIn("INTENTS", run())
        r = json.loads(run("--by", "turn", "--json"))
        self.assertEqual((r["by"], len(r["turns"])), ("turn", 3))   # --limit 3
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
            run("--by", "step")

    def test_live_server_api_top_by(self):
        live = server.Live(self.root, hooks=False, index_root=self.root)
        live.top("5h")
        for _ in range(200):
            if live.top_index is not None:
                break
            time.sleep(0.05)
        srv = server.make_server(live, "127.0.0.1", 0)
        threading.Thread(target=srv.serve_forever, daemon=True).start()

        def get(by):
            url = "http://127.0.0.1:%d/api/top?window=day&now=%s%s" % (
                srv.server_address[1], tp.iso(T0 + 7200), "&by=" + by if by is not None else "")
            with urllib.request.urlopen(url) as resp:
                return json.loads(resp.read())
        try:
            self.assertEqual(get(None)["by"], "intent")
            self.assertIn("intents", get("intent"))
            r = get("session")
            self.assertEqual((r["by"], [x["sid"] for x in r["sessions"]]), ("session", [B, A]))
            self.assertNotIn("intents", r)
            r = get("goal")
            self.assertEqual(sorted(g["id"] for g in r["goals"]), ["a-p1", "b-p2"])
            self.assertEqual({g["project"] for g in r["goals"]}, {"project-A"})   # redacted by default
            r = get("turn")
            self.assertEqual(len(r["turns"]), 4)
            self.assertIn("now run the tests", [x["label"] for x in r["turns"]])
            with self.assertRaises(urllib.error.HTTPError) as e:
                get("steps")
            self.assertEqual(e.exception.code, 400)
        finally:
            srv.shutdown()
            srv.server_close()

if __name__ == "__main__":
    unittest.main()
