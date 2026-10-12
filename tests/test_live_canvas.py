"""tokenscope live, session canvas: the /flow payload (subagent sub-flows and their launch steps), the server-side
replay (?replay=1), output-capture files kept out of targets, cached step labels, and a browser smoke test of the
canvas (skipped without Playwright)."""
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from tokenscope import intents, stepnames  # noqa: E402
from tokenscope.live import server, state as st  # noqa: E402
from tokenscope.live.replay import LiveReplay, replay, schedule  # noqa: E402
from test_live import SID, assistant, lines, result, tool_use, user_prompt  # noqa: E402

CAPTURE = "/srv/.claude/projects/-srv-checkout-app/%s/tool-results/b7x4ihbvq.txt" % SID


def end(i, mid, text="done"):
    return assistant(i, mid, [{"type": "text", "text": text}], stop="end_turn")


def side(rec):
    return dict(rec, isSidechain=True)


def write_fixture(root):
    """Main thread: read (+ a capture file) -> 2 parallel subagents -> edit + test. Subagent x1 launches a nested
    subagent x3. Returns the main transcript path."""
    proj = os.path.join(root, "-srv-checkout-app")
    sub = os.path.join(proj, SID, "subagents")
    os.makedirs(sub)
    main = os.path.join(proj, SID + ".jsonl")
    with open(main, "w", encoding="utf-8") as f:
        f.write(lines(
            user_prompt(0, "audit the cart and fix the tax rounding"),
            assistant(1, "m1", [tool_use("t1", "Read", {"file_path": "/srv/checkout-app/src/cart.py"}),
                                tool_use("t1b", "Read", {"file_path": CAPTURE})]),
            result(2, "t1"), result(2, "t1b"),
            assistant(3, "m2", [tool_use("tA", "Agent", {"subagent_type": "Explore", "description": "Audit cart"}),
                                tool_use("tB", "Agent", {"subagent_type": "Explore", "description": "Audit tax"})]),
            result(30, "tA"), result(31, "tB"),
            assistant(32, "m3", [tool_use("t3", "Edit", {"file_path": "/srv/checkout-app/src/tax.py",
                                                         "old_string": "a", "new_string": "b"})]),
            result(33, "t3"),
            assistant(34, "m4", [tool_use("t4", "Bash", {"command": "python3 -m unittest -q"})]),
            result(35, "t4"),
            end(36, "m5")))
    for aid, tid, desc, recs in (
            ("x1", "tA", "Audit cart", [
                side(assistant(4, "s1", [tool_use("s1t", "Grep", {"pattern": "round"})])), side(result(5, "s1t")),
                side(assistant(6, "s2", [tool_use("tC", "Agent", {"subagent_type": "Explore",
                                                                  "description": "Map callers"})])),
                side(result(20, "tC")), side(end(21, "s3"))]),
            ("x2", "tB", "Audit tax", [
                side(assistant(4, "u1", [tool_use("u1t", "Read", {"file_path": "/srv/checkout-app/src/tax.py"})])),
                side(result(5, "u1t")), side(end(25, "u2"))]),
            ("x3", "tC", "Map callers", [
                side(assistant(7, "v1", [tool_use("v1t", "Grep", {"pattern": "cart_total"})])),
                side(result(8, "v1t")), side(end(19, "v2"))])):
        meta = {"agentType": "Explore", "description": desc, "toolUseId": tid}
        with open(os.path.join(sub, "agent-%s.meta.json" % aid), "w") as f:
            json.dump(meta, f)
        with open(os.path.join(sub, "agent-%s.jsonl" % aid), "w") as f:
            f.write(lines(*recs))
    return main


class FlowFixture(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.main = write_fixture(self.root)

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)


class TestFlowPayload(FlowFixture):
    def test_subagents_have_their_own_intents_and_launch_steps(self):
        state, s = replay(self.main)
        d = state.session_flow(s, now=s.last_ts + 3600)
        steps = d["intents"]["steps"]
        self.assertTrue(all("ts0" in x and "ops" in x for x in steps))
        agents = {a["description"]: a for a in d["flow"]["agents"]}
        self.assertEqual(set(agents), {"Audit cart", "Audit tax", "Map callers"})
        fan = next(x["i"] for x in steps if any(o["tool"].startswith("Agent") for o in x["ops"]))
        self.assertEqual(agents["Audit cart"]["launch"], {"agent": None, "step": fan})
        self.assertEqual(agents["Audit tax"]["launch"], {"agent": None, "step": fan})
        nested = agents["Map callers"]
        self.assertEqual(nested["parent"], agents["Audit cart"]["id"])
        self.assertEqual(nested["launch"]["agent"], agents["Audit cart"]["id"])
        cart_steps = agents["Audit cart"]["intents"]["steps"]
        self.assertIn(nested["launch"]["step"], [x["i"] for x in cart_steps])
        self.assertTrue(all(a["intents"]["steps"] for a in agents.values()))
        self.assertTrue(all(a["status"] == "done" for a in agents.values()))
        self.assertIn("header", d)
        self.assertIn("totals", d["header"])

    def test_compact_default_payload_unchanged(self):
        state, s = replay(self.main)
        c = state.session_compact(s)
        self.assertNotIn("flow", c)
        self.assertNotIn("header", c)
        self.assertTrue(all("ts0" not in x for x in c["intents"]["steps"]))

    def test_capture_files_dropped_from_targets_and_summaries(self):
        state, s = replay(self.main)
        d = state.session_flow(s, now=s.last_ts + 3600)
        blob = json.dumps(d)
        self.assertNotIn("b7x4ihbvq", blob)
        self.assertNotIn("b7x4ihbvq", json.dumps(state.session_compact(s)))
        self.assertTrue(intents.is_capture_file(CAPTURE))
        self.assertTrue(intents.is_capture_file("/tmp/claude-1000/p/sid/tasks/b12ab3cd9.output"))
        self.assertFalse(intents.is_capture_file("/srv/app/notes.txt"))
        self.assertFalse(intents.is_capture_file("requirements.txt"))


class TestReplay(FlowFixture):
    def test_schedule_compresses_to_the_duration(self):
        offs, speed = schedule([0, 10, 20, 3600, 3610], duration=20, max_gap=8)
        self.assertAlmostEqual(offs[-1], 20, delta=0.5)
        self.assertLessEqual(max(b - a for a, b in zip(offs, offs[1:])), 8.0 + 1e-6)
        self.assertEqual(schedule([0, 10, 20], duration=60, max_gap=3)[0], [0.0, 3.0, 6.0])   # short: capped gaps only
        self.assertEqual(offs, sorted(offs))
        self.assertGreater(speed, 1)

    def test_replay_builds_the_session_progressively(self):
        clock = [0.0]
        rp = LiveReplay(self.main, redact=False, duration=20, max_gap=3, clock=lambda: clock[0])
        seen = []
        for t in (0, 2, 5, 8, 12, 16, 21, 40):
            clock[0] = t
            d = rp.flow()
            n = len(d["intents"]["steps"]) if d.get("intents") else 0
            seen.append((n, len(d["flow"]["agents"]), d["status"], d["replay"]["fed"]))
        fed = [x[3] for x in seen]
        self.assertEqual(fed, sorted(fed))
        self.assertLess(fed[1], fed[-1])
        self.assertTrue(rp.done())
        self.assertIn("working", [x[2] for x in seen])
        self.assertEqual(seen[-1][1], 3)
        self.assertGreater(seen[-1][0], seen[1][0])
        self.assertEqual(seen[-1][2], "waiting")
        self.assertIn("recap", d)   # ends like the live session: waiting, with the idle recap

    def test_replay_endpoint(self):
        live = server.Live(self.root, since_minutes=10 ** 7, hooks=False)
        live.poll_once()
        srv = server.make_server(live, port=0)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        base = "http://127.0.0.1:%d" % srv.server_address[1]
        try:
            def get(path):
                with urllib.request.urlopen(base + path, timeout=10) as r:
                    return json.loads(r.read().decode("utf-8"))
            full = get("/api/session/%s/flow" % SID[:8])
            self.assertEqual(len(full["flow"]["agents"]), 3)
            first = get("/api/session/%s/flow?replay=1&restart=1&duration=30" % SID[:8])
            self.assertIn("replay", first)
            self.assertLess(first["replay"]["fed"], first["replay"]["records"])
            time.sleep(0.3)
            again = get("/api/session/%s/flow?replay=1" % SID[:8])
            self.assertGreaterEqual(again["replay"]["fed"], first["replay"]["fed"])
            with urllib.request.urlopen(base + "/s/" + SID[:8], timeout=10) as r:
                page = r.read().decode("utf-8")
            self.assertIn("/static/canvas.js", page)
        finally:
            srv.shutdown()
            srv.server_close()
            live.stop.set()


class TestCachedStepLabels(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.cache = os.path.join(self.dir, "step-labels.json")

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def steps(self, idx):
        return [{"i": i, "cat": "change", "tools": {"Edit": 1}, "files": ["f%d.py" % i], "text": "edit f%d" % i,
                 "thinking": 0} for i in idx]

    def test_per_step_cache_serves_another_batching_and_cache_only(self):
        def runner(cmd, **kw):
            ans = {"labels": {"1": "Fix one", "2": "Fix two", "3": "Fix three"}, "groups": []}
            out = {"result": json.dumps(ans), "usage": {}, "total_cost_usd": 0.001}
            return type("P", (), {"returncode": 0, "stdout": json.dumps(out)})()
        r = stepnames.label_batch(self.steps([1, 2, 3]), runner=runner, cache=self.cache)
        self.assertEqual(r["labels"][2], "Fix two")

        def boom(cmd, **kw):
            raise AssertionError("cache only: no model call")
        # the same steps under other indices / in another batch: per-step hits, no model call
        r2 = stepnames.label_batch(self.steps([2, 3]), runner=boom, cache=self.cache, cache_only=True)
        self.assertEqual(r2["labels"], {2: "Fix two", 3: "Fix three"})
        r3 = stepnames.label_batch(self.steps([3, 9]), runner=boom, cache=self.cache, cache_only=True)
        self.assertEqual(r3["labels"], {3: "Fix three"})

    def test_idle_sessions_get_label_requests(self):
        root = tempfile.mkdtemp()
        try:
            main = write_fixture(root)
            state, s = replay(main, model_labels=True)
            state.clock = lambda: s.last_ts + 6 * 3600   # long idle
            self.assertEqual(state.status(s), "idle")
            reqs = state.label_requests()
            self.assertTrue(reqs and reqs[0][0] == SID)
        finally:
            shutil.rmtree(root, ignore_errors=True)


def _playwright():
    try:
        from playwright.sync_api import sync_playwright  # noqa: F401
        return True
    except Exception:  # noqa: BLE001
        return False


@unittest.skipUnless(_playwright() and os.environ.get("TOKENSCOPE_SKIP_BROWSER") != "1", "Playwright not installed")
class TestCanvasBrowser(FlowFixture):
    def test_canvas_renders_nodes_for_the_fixture(self):
        from playwright.sync_api import sync_playwright
        live = server.Live(self.root, since_minutes=10 ** 7, hooks=False, redact=False)
        live.poll_once()
        srv = server.make_server(live, port=0)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        url = "http://127.0.0.1:%d/s/%s" % (srv.server_address[1], SID[:8])
        try:
            with sync_playwright() as p:
                try:
                    b = p.chromium.launch()
                except Exception as e:  # noqa: BLE001 - browsers not downloaded
                    self.skipTest("no Chromium for Playwright: %s" % e)
                errors = []
                pg = b.new_page(viewport={"width": 1300, "height": 800})
                pg.on("pageerror", lambda e: errors.append(str(e)))
                pg.goto(url)
                pg.wait_for_selector(".react-flow__node-intent", timeout=10000)
                self.assertGreaterEqual(pg.locator(".react-flow__node-agent").count(), 3)
                self.assertGreaterEqual(pg.locator(".react-flow__node-intent").count(), 4)
                self.assertGreater(pg.locator(".react-flow__edge").count(), 3)
                self.assertEqual(pg.locator(".react-flow__minimap").count(), 1)
                self.assertIn("ASKED", pg.locator(".head").inner_text())
                pg.locator(".react-flow__node-intent").first.click()
                pg.wait_for_selector(".panel .op", timeout=5000)
                self.assertEqual(errors, [])
                b.close()
        finally:
            srv.shutdown()
            srv.server_close()
            live.stop.set()


if __name__ == "__main__":
    unittest.main()
