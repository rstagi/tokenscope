"""Alert grouping (alerts.group / public), the alert history (tokenscope/live/alertstore.py: dedupe by id, ack /
dismiss, re-raise, merging a grown group, persistence across restarts) and its API (GET /api/alerts, POST
/api/alerts/ack|dismiss|reopen), over the synthetic traces of tests/alerts_synth.py."""
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
from contextlib import redirect_stderr

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from tokenscope import alerts as A  # noqa: E402
from tokenscope.alerts import Context, StepFacts  # noqa: E402
from tokenscope.live import server  # noqa: E402
from tokenscope.live.alertstore import AlertStore, default_path  # noqa: E402
from tokenscope.live.replay import replay  # noqa: E402
import alerts_synth  # noqa: E402
from test_alerts import cfg, fixture  # noqa: E402


def pub(*alerts, last=None):
    return A.public(list(alerts), last_step=last)


class TestGrouping(unittest.TestCase):
    def test_one_expensive_step_is_one_alert_with_its_reasons(self):
        # a $15 delegate step: session and intent thresholds and a jump, all at step 7
        steps = [StepFacts(i=i, usd=u, turns=1) for i, u in enumerate([1.5] * 7 + [24.0])]
        steps[7].delegated = True
        got = A.run(Context(config=cfg(**{"cost.session_usd": 25}), steps=steps, session_usd=34.5))
        self.assertEqual(sorted(a.rule for a in got), ["intent_usd", "jump", "session_usd"])
        p = A.public(got, last_step=7)
        at7 = [a for a in p if a["step"] == 7]
        self.assertEqual(len(at7), 1)
        g = at7[0]
        self.assertEqual([r["rule"] for r in g["reasons"]], ["intent_usd", "jump", "session_usd"])
        self.assertEqual((g["rule"], g["severity"]), ("intent_usd", "critical"))   # the worst leads
        self.assertEqual(set(g["members"]), {r["id"] for r in g["reasons"]})
        self.assertTrue(g["active"])
        self.assertEqual(A.counts(p)["n"], 1)   # counts count groups

    def test_cost_group_id_is_the_step(self):
        j = A.Alert("cost", "jump", "warning", "j", step=4, first=4, key="jump:4")
        i = A.Alert("cost", "intent_usd", "critical", "i", step=4, first=2, key="intent:2")
        self.assertEqual(pub(j)[0]["id"], pub(j, i)[0]["id"])   # a reason joining keeps the id
        g = pub(j, i)[0]
        self.assertEqual((g["first"], g["step"], g["severity"], g["rule"]), (2, 4, "critical", "intent_usd"))

    def test_kinds_threads_and_steps_stay_apart(self):
        a = A.Alert("looping", "retry", "warning", "r", step=5, first=3, key="a")
        b = A.Alert("looping", "repeat_command", "warning", "c", step=6, first=3, key="b")
        c = A.Alert("cost", "jump", "warning", "j", step=3, first=3, key="jump:3")
        d = A.Alert("looping", "retry", "warning", "r", step=5, first=3, agent="ag", key="a")
        p = pub(a, b, c, d)
        self.assertEqual([len(x["reasons"]) for x in p], [2, 1, 1])   # loops grouped by the step they began at
        self.assertEqual(p[0]["step"], 6)
        self.assertEqual(pub(a)[0]["id"], a.id)   # a lone loop keeps its own id

    def test_synthetic_threshold_trace_groups(self):
        state, s = replay(fixture("cost-threshold"), redact=True)
        al = state.alerts_public(s)
        self.assertEqual(len(al), 1)
        self.assertEqual([r["rule"] for r in al[0]["reasons"]], ["session_usd", "intent_usd"])


def rec(id_, rule="jump", sev="warning", kind="cost", members=None, step=3, msg="m"):
    return {"id": id_, "kind": kind, "rule": rule, "severity": sev, "message": msg, "step": step, "first": step,
            "agent": None, "reasons": [], "members": members or [id_]}


class TestStore(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.path = os.path.join(self.d, "sub", "h.jsonl")
        self.t = [1000.0]

    def tearDown(self):
        shutil.rmtree(self.d, ignore_errors=True)

    def store(self):
        return AlertStore(self.path, clock=lambda: self.t[0])

    def test_dedupe_ack_dismiss_and_restart(self):
        st = self.store()
        self.assertEqual(st.sync("s1", [rec("a"), rec("b", step=5)], {"project": "project-A"}), ["a", "b"])
        self.assertEqual(st.sync("s1", [rec("a"), rec("b", step=5)]), [])   # same alerts: nothing new
        self.assertEqual([r["n"] for r in st.records(["s1"])], [1, 2])
        self.assertEqual(st.ack(["a"]), 1)
        self.assertEqual(st.summary("s1"), {"new": 1, "acked": 1, "total": 2, "by_kind": {"cost": 1},
                                            "worst": "warning"})
        st.sync("s2", [rec("c", kind="looping", rule="retry", sev="critical")])
        self.assertEqual(st.dismiss(None, ["s2"]), 1)
        # a restart reads it all back
        st2 = self.store()
        self.assertEqual({r["id"]: r["state"] for r in st2.records(states=("new", "acked", "dismissed"))},
                         {"a": "acked", "b": "new", "c": "dismissed"})
        self.assertEqual(st2.records(["s1"])[0]["project"], "project-A")
        self.assertEqual(st2.sync("s1", [rec("a"), rec("b", step=5)]), [])
        self.assertEqual(st2.sync("s1", [rec("d")]), ["d"])
        self.assertEqual(st2.get("d")["n"], 3)   # per-session ordinals go on after a restart
        self.assertEqual(st2.ack(None, ["s1"]), 2)   # ack all of a session
        self.assertEqual(st2.summary("s1")["new"], 0)

    def test_worse_comes_back(self):
        st = self.store()
        st.sync("s", [rec("a", members=["m1"])])
        st.ack(["a"])
        self.assertEqual(st.sync("s", [rec("a", members=["m1"], msg="m2")]), [])   # only the text changed
        self.assertEqual(st.get("a")["state"], "acked")
        self.assertEqual(st.sync("s", [rec("a", members=["m1", "m2"])]), ["a"])   # a new reason joined
        self.assertEqual((st.get("a")["state"], st.get("a")["raised"]), ("new", 1))
        st.dismiss(["a"])
        self.assertEqual(st.sync("s", [rec("a", members=["m1", "m2"], sev="critical")]), ["a"])   # severity up
        self.assertEqual(st.get("a")["state"], "new")
        st.reopen(["a"])

    def test_a_grown_group_takes_over_its_members_records(self):
        st = self.store()
        st.sync("s", [rec("x", kind="looping", rule="retry")])
        st.ack(["x"])
        st.sync("s", [rec("g", kind="looping", rule="retry", members=["x", "y"])])
        self.assertIsNone(st.get("x"))
        g = st.get("g")
        self.assertEqual((g["n"], g["state"], g["members"]), (1, "new", ["x", "y"]))   # worse: new again
        self.assertEqual([r["id"] for r in self.store().records()], ["g"])   # the drop is on disk too

    def test_compaction_and_bad_lines(self):
        st = self.store()
        for k in range(300):
            st.sync("s", [rec("a", msg="m%d" % k)])
        with open(self.path, "a", encoding="utf-8") as f:
            f.write('{"op": "put", "rec": {"id": "z"\n')   # a line cut short
        st2 = self.store()
        self.assertEqual([r["message"] for r in st2.records()], ["m299"])
        with open(self.path, encoding="utf-8") as f:
            self.assertEqual(len(f.read().splitlines()), 1)

    def test_memory_only_and_unwritable(self):
        st = AlertStore(None)
        st.sync("s", [rec("a")])
        self.assertEqual(st.ack(["a"]), 1)
        bad = AlertStore(os.path.join(self.d, "f", "x.jsonl"))
        open(os.path.join(self.d, "f"), "w").close()   # a file where the dir should be
        err = io.StringIO()
        with redirect_stderr(err):
            bad.sync("s", [rec("a")])   # warns once, keeps working in memory
        self.assertIn("cannot write the alert history", err.getvalue())
        self.assertEqual(len(bad.records()), 1)

    def test_default_path(self):
        old = dict(os.environ)
        try:
            os.environ.pop("TOKENSCOPE_ALERTS_HISTORY", None)
            os.environ["TOKENSCOPE_CACHE_DIR"] = "/x/cache"
            self.assertEqual(default_path(), "/x/cache/alerts-history.jsonl")
            os.environ["TOKENSCOPE_ALERTS_HISTORY"] = "/y/h.jsonl"
            self.assertEqual(default_path(), "/y/h.jsonl")
        finally:
            os.environ.clear()
            os.environ.update(old)


class TestStateHistory(unittest.TestCase):
    def test_acked_alerts_stay_dismissed_ones_leave(self):
        on = cfg(**{"off_task.enabled": True, "off_task.use_prompt": True})
        state, s = replay(fixture("showcase"), redact=True, alerts_config=on)
        al = state.alerts_public(s)
        self.assertEqual([a["n"] for a in al], [1, 2, 3, 4, 5])
        self.assertTrue(all(a["at"] and a["intent"]["cat"] for a in al))
        state.alert_store.ack([al[0]["id"]])
        state.alert_store.dismiss([al[1]["id"]])
        al2 = state.alerts_public(s)
        self.assertEqual([(a["id"], a["state"]) for a in al2],
                         [(al[0]["id"], "acked")] + [(a["id"], "new") for a in al[2:]])
        row = state.board_row(s, time.time())["alerts"]
        self.assertEqual((row["n"], row["new"]), (4, 3))
        self.assertNotIn(al[0]["id"], [t["id"] for t in row["top"]])   # acked: no longer on the card
        tile = state.follow_tile(s)
        self.assertEqual(tile["alerts"]["new"], 3)
        self.assertEqual(set(tile["alert_steps"]), {str(a["step"]) for a in al[2:]})
        h = state.alert_history()
        self.assertEqual(h["new"], 3)
        self.assertEqual(h["sessions"][0]["id"], s.id)
        self.assertEqual(len(h["alerts"]), 4)
        self.assertEqual(len(state.alert_history(states=("new", "acked", "dismissed"))["alerts"]), 5)


class TestApi(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = tempfile.mkdtemp()
        cls.hist = os.path.join(cls.root, "history.jsonl")
        alerts_synth.write_all(cls.root, t0=time.time() - 600 * len(alerts_synth.SCENARIOS) - 1800)
        cls.start()

    @classmethod
    def start(cls):
        cls.live = server.Live(cls.root, since_minutes=10 ** 6, hooks=False, xhints=False,
                               alerts_config=cfg(**{"off_task.enabled": True, "off_task.use_prompt": True}),
                               alerts_history=cls.hist)
        cls.live.poll_once()
        cls.srv = server.make_server(cls.live, port=0)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.base = "http://127.0.0.1:%d" % cls.srv.server_address[1]

    @classmethod
    def stop(cls):
        cls.srv.shutdown()
        cls.srv.server_close()
        cls.live.stop.set()

    @classmethod
    def tearDownClass(cls):
        cls.stop()
        shutil.rmtree(cls.root, ignore_errors=True)

    def get(self, path):
        with urllib.request.urlopen(self.base + path, timeout=10) as r:
            return json.loads(r.read().decode())

    def post(self, path, body, ctype="application/json"):
        req = urllib.request.Request(self.base + path, data=json.dumps(body).encode(), method="POST",
                                     headers={"Content-Type": ctype})
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read().decode())

    def test_history_ack_dismiss_and_restart(self):
        h = self.get("/api/alerts")
        bad = {alerts_synth.SESSIONS[n] for n in ("cost-jump", "cost-threshold", "off-task", "read-loop",
                                                   "retry-loop", "edit-revert", "showcase")}
        self.assertEqual({x["id"] for x in h["sessions"]}, bad)   # the clean session needs no attention
        self.assertEqual(h["new"], len(h["alerts"]))
        self.assertTrue(all(x["new"] for x in h["sessions"]))
        self.assertEqual(h["sessions"][0]["worst"], "critical")   # worst first
        sid = alerts_synth.SESSIONS["showcase"]
        one = self.get("/api/alerts?s=" + sid[:8])
        self.assertEqual({a["sid"] for a in one["alerts"]}, {sid})
        a0 = one["alerts"][0]
        self.assertEqual(set(a0) >= {"id", "sid", "short", "n", "kind", "rule", "severity", "message", "step",
                                     "reasons", "ts", "at", "intent", "state", "project", "goal"}, True)
        # wrong content type, bad bodies
        self.assertEqual(self.post("/api/alerts/ack", {"ids": [a0["id"]]}, "text/plain")[0], 415)
        self.assertEqual(self.post("/api/alerts/ack", {})[0], 400)
        self.assertEqual(self.post("/api/alerts/ack", {"ids": "x"})[0], 400)
        self.assertEqual(self.post("/api/alerts/nope", {"all": True})[0], 404)
        self.assertEqual(self.post("/api/alerts/ack", {"ids": [a0["id"]]}), (200, {"ok": True, "changed": 1,
                                                                                   "version": self.live.state.alert_store.version}))
        got = self.get("/api/session/%s/alerts" % sid[:8])
        self.assertEqual({a["id"]: a["state"] for a in got["alerts"]}[a0["id"]], "acked")
        self.assertEqual(got["counts"]["new"], len(got["alerts"]) - 1)
        self.assertEqual(self.post("/api/alerts/dismiss", {"ids": [one["alerts"][1]["id"]]})[1]["changed"], 1)
        n = self.post("/api/alerts/ack", {"all": True, "sessions": [sid[:8]]})[1]["changed"]
        self.assertEqual(n, len(one["alerts"]) - 2)
        board = {r["id"]: r for r in self.get("/api/board")["sessions"]}
        self.assertEqual((board[sid]["alerts"]["new"], board[sid]["alerts"]["n"]), (0, len(one["alerts"]) - 1))
        # a restart keeps the history and its states, no duplicates
        before = self.get("/api/alerts?state=all")
        self.stop()
        self.start()
        after = self.get("/api/alerts?state=all")
        self.assertEqual(sorted((a["id"], a["state"], a["n"]) for a in before["alerts"]),
                         sorted((a["id"], a["state"], a["n"]) for a in after["alerts"]))
        self.assertEqual(self.post("/api/alerts/reopen", {"ids": [a0["id"]]})[1]["changed"], 1)
        # ack all, every session
        self.post("/api/alerts/ack", {"all": True})
        self.assertEqual(self.get("/api/alerts")["new"], 0)



def _playwright():
    try:
        from playwright.sync_api import sync_playwright  # noqa: F401
        return True
    except Exception:  # noqa: BLE001
        return False


@unittest.skipUnless(_playwright() and os.environ.get("TOKENSCOPE_SKIP_BROWSER") != "1", "Playwright not installed")
class TestBrowser(unittest.TestCase):
    """The alerts section (static/alerts.js) on the board, the follow view and the session canvas."""

    @classmethod
    def setUpClass(cls):
        cls.root = tempfile.mkdtemp()
        alerts_synth.write_all(cls.root, t0=time.time() - 600 * len(alerts_synth.SCENARIOS) - 1800)
        cls.live = server.Live(cls.root, since_minutes=10 ** 6, hooks=False, xhints=False,
                               alerts_history=os.path.join(cls.root, "h.jsonl"))
        cls.live.poll_once()
        cls.srv = server.make_server(cls.live, port=0)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.base = "http://127.0.0.1:%d" % cls.srv.server_address[1]

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()
        cls.live.stop.set()
        shutil.rmtree(cls.root, ignore_errors=True)

    def test_board_follow_and_canvas(self):
        from playwright.sync_api import sync_playwright
        show = alerts_synth.SESSIONS["showcase"][:8]
        with sync_playwright() as p:
            try:
                b = p.chromium.launch()
            except Exception as e:  # noqa: BLE001 - browsers not downloaded
                self.skipTest("no Chromium for Playwright: %s" % e)
            errors = []
            pg = b.new_page(viewport={"width": 1440, "height": 1000})
            pg.on("pageerror", lambda e: errors.append(str(e)))
            pg.goto(self.base + "/")
            pg.wait_for_selector("#alerts .alrow", timeout=10000)
            n = pg.locator("#alerts .alrow.new").count()
            self.assertGreaterEqual(n, 6)
            self.assertIn("new", pg.locator("#alerts .asum").inner_text())
            chips = pg.locator("#alerts .asess-chip")
            self.assertEqual(chips.count(), 6)   # every session but the clean one needs attention
            self.assertIn("critical", chips.first.get_attribute("class"))   # worst first
            self.assertGreaterEqual(pg.locator(".scard .abadge").count(), 6)
            self.assertGreaterEqual(pg.locator(".scard.al-critical").count(), 1)
            # ack one: it stays in the history, dimmed; the card's badge count drops
            pg.locator("#alerts .alrow.new .aok").first.click()
            pg.wait_for_function("document.querySelectorAll('#alerts .alrow.acked').length === 1", timeout=5000)
            self.assertEqual(pg.locator("#alerts .alrow.new").count(), n - 1)
            # the "new" filter hides it, and the choice and the collapsed state survive a reload
            pg.locator("#alerts .aseg button[data-f=new]").click()
            self.assertEqual(pg.locator("#alerts .alrow.acked").count(), 0)
            pg.locator("#alerts .atog").click()
            self.assertIn("collapsed", pg.locator("#alerts .apanel").get_attribute("class"))
            pg.reload()
            pg.wait_for_selector("#alerts .apanel.collapsed", timeout=10000)
            self.assertFalse(pg.locator("#alerts .alist").is_visible())
            pg.locator("#alerts .atog").click()
            pg.wait_for_selector("#alerts .alrow", timeout=5000)
            self.assertEqual(pg.locator("#alerts .alrow.acked").count(), 0)   # still on "new"
            # follow view: tile badges, ▲ on alerted steps, a row jumps to its tile's step
            pg.goto(self.base + "/follow?s=" + show)
            pg.wait_for_selector(".ftile .abadge", timeout=10000)
            self.assertGreaterEqual(pg.locator(".ftile .fnode .amark").count(), 1)
            pg.wait_for_selector("#fside .alrow", timeout=10000)
            pg.locator("#fside .alrow", has_text="read loop").first.click()
            pg.wait_for_selector(".ftile .fnode.afocus", timeout=5000)
            self.assertTrue(pg.locator(".ftile .fjump").is_visible())   # stopped following
            # "needs attention" on a session not followed yet pins it
            pg.locator("#fside .asess-chip", has_text=alerts_synth.SESSIONS["retry-loop"][:8]).click()
            pg.wait_for_function("document.querySelectorAll('.ftile').length === 2", timeout=10000)
            # canvas: the session's alerts as a drawer; a row selects the node of its step
            pg.goto(self.base + "/s/" + show)
            pg.wait_for_selector(".cside .alrow", timeout=10000)
            self.assertEqual(pg.locator(".cside .asess").count(), 0)   # one session: no session column
            pg.locator(".cside .alrow", has_text="cost jump").click()
            pg.wait_for_selector(".panel .palert", timeout=5000)
            self.assertIn("cost jump", pg.locator(".panel .palert").first.inner_text())
            self.assertEqual(errors, [])
            b.close()


if __name__ == "__main__":
    unittest.main()
