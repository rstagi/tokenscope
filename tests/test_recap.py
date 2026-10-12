"""Idle recap (tokenscope/live/recap.py): costliest / slowest intent, and each hint rule firing on a synthetic
positive and staying silent on a near miss; the recap is in the compact payload only while the session is idle."""
import glob
import json
import os
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tokenscope.live import recap as rc, state as st  # noqa: E402
from tokenscope.live.tail import Tailer, TrackedFile, classify  # noqa: E402

SID = "bbbbbbbb-1111-2222-3333-444444444444"
T0 = 1_700_000_000.0


def iso(sec):
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(T0 + sec)) + ".000Z"


def read(f, **kw):
    return ("Read", dict({"file_path": "/repo/src/" + f}, **kw))


def edit(f):
    return ("Edit", {"file_path": "/repo/src/" + f, "old_string": "a", "new_string": "b"})


def grep(p):
    return ("Grep", {"pattern": p})


def bash(c):
    return ("Bash", {"command": c})


PASS = "4 passed in 0.10s"
FAIL = "FAILED tests/test_store.py::test_total - AssertionError: 3 != 4\n1 failed, 3 passed in 0.12s"


class Sess:
    """A synthetic main-thread transcript: prompts, tool turns (CW, context, test output), final answers."""

    def __init__(self):
        self.recs, self.sec, self.n, self.ctx = [], 0, 0, 1_000

    def _ts(self, dt=10):
        self.sec += dt
        return iso(self.sec)

    def prompt(self, text="please fix the store totals", **kw):
        self.recs.append(dict({"type": "user", "timestamp": self._ts(), "sessionId": SID, "cwd": "/repo",
                               "message": {"role": "user", "content": text}}, **kw))
        return self

    def turn(self, *calls, cw=5_000, ctx=None, out=None, err=False, dt=10):
        self.n += 1
        ctx = self.ctx = self.ctx if ctx is None else ctx
        usage = {"input_tokens": max(0, int(cw - 0.1 * ctx)), "cache_creation_input_tokens": 0,
                 "cache_read_input_tokens": ctx, "output_tokens": 0}
        blocks = [{"type": "tool_use", "id": "t%d_%d" % (self.n, j), "name": n, "input": i}
                  for j, (n, i) in enumerate(calls)]
        self.recs.append({"type": "assistant", "timestamp": self._ts(dt), "sessionId": SID,
                          "message": {"id": "m%d" % self.n, "model": "claude-sonnet-4-5", "content": blocks,
                                      "stop_reason": "tool_use", "usage": usage}})
        for b in blocks:
            text = out if out is not None else "ok"
            self.recs.append({"type": "user", "timestamp": self._ts(1), "sessionId": SID,
                              "message": {"role": "user", "content": [
                                  {"type": "tool_result", "tool_use_id": b["id"], "is_error": err, "content": text}]},
                              "toolUseResult": {"stdout": text}})
        return self

    def answer(self):
        self.n += 1
        self.recs.append({"type": "assistant", "timestamp": self._ts(), "sessionId": SID,
                          "message": {"id": "m%d" % self.n, "model": "claude-sonnet-4-5", "stop_reason": "end_turn",
                                      "content": [{"type": "text", "text": "done"}],
                                      "usage": {"input_tokens": 0, "cache_read_input_tokens": self.ctx,
                                                "output_tokens": 0}}})
        return self

    def state(self, redact=False, at=None):
        now = {"t": T0 + (self.sec + 5 if at is None else at)}
        state = st.LiveState(redact=redact, clock=lambda: now["t"])
        s = state.ingest(TrackedFile("/x/-repo/%s.jsonl" % SID, SID, "-repo"), self.recs)
        return state, s, now


def hints_of(sess, redact=False):
    state, s, _ = sess.state(redact)
    r = state.recap(s)
    return {h["rule"]: h for h in r["hints"]}, r


def smooth():
    """A toy session that goes well: read a few files, edit, tests pass."""
    return (Sess().prompt().turn(read("store.py"), read("models.py")).turn(grep("total"))
            .turn(edit("store.py")).turn(bash("python3 -m pytest -q"), out=PASS).answer())


class Recap(unittest.TestCase):
    def test_smooth_session_has_no_hints(self):
        h, r = hints_of(smooth())
        self.assertEqual(h, {})
        self.assertEqual(r["intents"], 3)
        self.assertEqual(r["costliest"]["cat"], "investigate")
        self.assertEqual(r["costliest"]["cw"], 10_000)   # two reads + a grep turn (5k each, 1 step per category)
        self.assertEqual(r["costliest"]["pct"], 50)
        self.assertIn("slowest", r)
        self.assertEqual(r["session"]["intents"], 3)

    def test_window_is_since_last_prompt_and_session_covers_all(self):
        s = smooth().prompt("now the cli").turn(read("cli.py"), cw=40_000).turn(edit("cli.py"), cw=2_000).answer()
        _, r = hints_of(s)
        self.assertEqual(r["intents"], 2)
        self.assertEqual(r["cw"], 42_000)
        self.assertEqual(r["costliest"]["pct"], 95)
        self.assertEqual(r["session"]["intents"], 5)
        self.assertEqual(r["session"]["cw"], 62_000)

    def test_slowest_is_first_to_last_timestamp(self):
        s = Sess().prompt().turn(read("a.py")).turn(read("b.py"), dt=300).turn(edit("a.py")).answer()
        _, r = hints_of(s)
        self.assertEqual(r["slowest"]["cat"], "investigate")
        self.assertEqual(r["slowest"]["s"], 301.0)   # the second read's turn starts 300 s + its result's 1 s later

    # (a) re-discovery
    def _rereads(self, n_intents, edit_between=False):
        s = Sess().prompt()
        for k in range(n_intents):
            s.turn(read("store.py"), cw=20_000)
            if edit_between:
                s.turn(edit("store.py"))
            s.turn(bash("python3 -m pytest -q"), out=PASS)   # a test run closes the step: the next read is a new intent
        return s.answer()

    def test_reread_fires(self):
        h, _ = hints_of(self._rereads(4))
        self.assertIn("reread", h)
        self.assertEqual(h["reread"]["evidence"], "store.py read in full 4× across 4 intents, 3× unchanged")
        self.assertIn("CLAUDE.md", h["reread"]["text"])

    def test_reread_near_miss(self):
        self.assertNotIn("reread", hints_of(self._rereads(3))[0])                    # 3 reads only
        self.assertNotIn("reread", hints_of(self._rereads(5, edit_between=True))[0])  # edited between: it changed

    def test_reread_redacted(self):
        h, _ = hints_of(self._rereads(4), redact=True)
        self.assertNotIn("store", json.dumps(h))
        self.assertRegex(h["reread"]["evidence"], r"^…/\w{3}\.py read in full")

    # (b) one intent, mostly searches / re-reads, takes most of the request
    def _search_heavy(self, cw):
        s = Sess().prompt().turn(read("store.py"), cw=5_000).turn(edit("store.py"), cw=5_000).prompt("next")
        s.turn(read("models.py"), cw=5_000)
        for k in range(10):
            s.turn(grep("thing%d" % k), cw=cw)
        return s.turn(edit("models.py"), cw=5_000).answer()

    def test_search_heavy_fires(self):
        h, _ = hints_of(self._search_heavy(15_000))
        self.assertIn("search_heavy", h)
        self.assertRegex(h["search_heavy"]["evidence"], r"^'.*': 97% of this request's CW \(155k\), 10 of 11 calls")
        self.assertNotIn("outlier", h)   # the same intent is not hinted twice

    def test_search_heavy_near_miss(self):
        self.assertNotIn("search_heavy", hints_of(self._search_heavy(9_000))[0])   # 95k < B_MIN_CW

    # (c) tests failing again and again the same way
    def _fails(self, n, out=FAIL):
        s = Sess().prompt()
        for k in range(n):
            s.turn(edit("store.py"), cw=8_000).turn(bash("python3 -m pytest -q"), out=out, err=True, cw=8_000)
        return s.answer()

    def test_test_repeat_fires(self):
        h, _ = hints_of(self._fails(4))
        self.assertIn("test_repeat", h)
        self.assertEqual(h["test_repeat"]["evidence"], "pytest failed 4× in a row with the same failure (1 failed), "
                                                       "3 edits between")
        self.assertEqual(h["test_repeat"]["cw_at_stake"], 5 * 8_000)   # 2nd failing run .. the last

    def test_test_repeat_near_miss(self):
        self.assertNotIn("test_repeat", hints_of(self._fails(3))[0])
        s = Sess().prompt()
        for k in range(4):   # a different failure every time: progress, not a loop
            s.turn(edit("store.py"), cw=8_000).turn(bash("python3 -m pytest -q"), err=True, cw=8_000,
                                                    out=FAIL.replace("1 failed", "%d failed" % (k + 1)))
        self.assertNotIn("test_repeat", hints_of(s.answer())[0])

    # (d) ToolSearch overhead
    def _toolsearch(self, n, cw):
        s = Sess().prompt()
        for k in range(n):
            s.turn(("ToolSearch", {"query": "select:mcp__x__tool%d" % k}), cw=cw).turn(read("f%d.py" % k), cw=10_000)
        return s.answer()

    def test_toolsearch_fires(self):
        h, _ = hints_of(self._toolsearch(3, 15_000))
        self.assertEqual(h["toolsearch"]["evidence"], "3 ToolSearch calls: 45k CW, 60% of this request")

    def test_toolsearch_near_miss(self):
        self.assertNotIn("toolsearch", hints_of(self._toolsearch(2, 15_000))[0])
        self.assertNotIn("toolsearch", hints_of(self._toolsearch(3, 5_000))[0])

    # (e) this request brought the context near autocompaction
    def _ctx(self, before, after, entrypoint=None):
        kw = {"entrypoint": entrypoint} if entrypoint else {}
        s = Sess().prompt(**kw).turn(read("a.py"), ctx=20_000).turn(read("c.py")).turn(read("d.py"))
        return s.prompt("go on", **kw).turn(read("b.py"), cw=20_000, ctx=before).turn(grep("x")).turn(
            read("e.py"), ctx=after).answer()

    def test_context_fires_near_autocompaction(self):
        h, _ = hints_of(self._ctx(120_000, 175_000))
        self.assertEqual(h["context"]["evidence"], "context 175k / 200k (88%), +152k during this request")
        med = int(h["context"]["estimate"].split("median ")[1].split()[0])
        self.assertEqual(h["context"]["cw_at_stake"], round(0.1 * (175_000 - 20_000) * med))
        self.assertIn("this session's median %d turns per request" % med, h["context"]["estimate"])
        self.assertGreaterEqual(med, 3)

    def test_context_near_miss(self):
        self.assertNotIn("context", hints_of(self._ctx(120_000, 165_000))[0])              # 83% < 85%
        # already that full before this request: said at an earlier idle point
        s = Sess().prompt().turn(read("a.py"), ctx=172_000).prompt("go on").turn(read("b.py"), cw=20_000, ctx=175_000)
        self.assertNotIn("context", hints_of(s.answer())[0])
        # headless (`claude -p` / SDK): no next task to prepare for
        self.assertNotIn("context", hints_of(self._ctx(120_000, 175_000, entrypoint="sdk-cli"))[0])
        self.assertIn("context", hints_of(self._ctx(120_000, 175_000, entrypoint="cli"))[0])

    def test_context_estimate_uses_median_turns(self):
        h = rc.rule_context({"now": 180_000, "start": 100_000, "window": 200_000}, [{}] * 10, [0, 2, 4])
        self.assertEqual(h["cw_at_stake"], round(0.1 * 160_000 * 2))                       # requests of 2, 2, 6 turns
        self.assertIsNone(rc.rule_context({"now": 180_000, "start": 100_000, "window": 200_000, "headless": True},
                                          [{}] * 10, [0, 2, 4]))

    # (f) subagents reading the same files (pure: the agents' reads = (path, tokens, later turns of that agent))
    def test_shared_reads(self):
        agents = [{"id": "a%d" % k, "ts": 10, "reads": [("/r/store.py", 16_000, 10), ("/r/cli.py", 8_000, 10)]}
                  for k in range(4)]
        h = rc.rule_shared_reads(agents, 0, os.path.basename)
        self.assertEqual(h["evidence"], "cli.py, store.py each read in full by 4 of 4 subagents")
        # size-based: 3 duplicate agents x 24k tokens x (1.25 write + 10 later turns x 0.1 read) = 162k
        self.assertEqual(h["cw_at_stake"], round(3 * 24_000 * (1.25 + 10 * 0.1)))
        self.assertIn("chars/4", h["estimate"])
        self.assertIsNone(rc.rule_shared_reads(agents[:2], 0, os.path.basename))           # 2 agents
        one = [dict(a, reads=a["reads"][:1]) for a in agents]
        self.assertIsNone(rc.rule_shared_reads(one, 0, os.path.basename))                  # 1 file
        self.assertIsNone(rc.rule_shared_reads(agents, 100, os.path.basename))             # an earlier request's

    def test_shared_reads_size_not_turn_cw(self):
        # small files read late: the same 4 agents, 1k-token files, 2 later turns -> 3 x 2k x 1.45 = 8.7k < F_MIN_CW,
        # however costly the turns they happened in were
        agents = [{"id": "a%d" % k, "ts": 10, "reads": [("/r/a.md", 1_000, 2), ("/r/b.md", 1_000, 2)]}
                  for k in range(4)]
        self.assertIsNone(rc.rule_shared_reads(agents, 0, os.path.basename))

    def test_shared_reads_share(self):
        def run(readers, total):
            agents = [{"id": "a%d" % k, "ts": 10, "reads": ([("/r/store.py", 16_000, 10), ("/r/cli.py", 8_000, 10)]
                                                            if k < readers else [])} for k in range(total)]
            return rc.rule_shared_reads(agents, 0, os.path.basename)
        h = run(4, 20)                                                                     # 4 of 20 = 20%
        self.assertEqual(h["evidence"], "cli.py, store.py each read in full by 4 of 20 subagents")
        self.assertIsNone(run(4, 21))                                                      # 4 of 21 < 20%
        self.assertIsNone(run(5, 470))                                                     # 5 of 470

    def test_shared_reads_near_miss(self):
        # duplicates worth < F_MIN_CW: 2 extra agents x 12k x (1.25 + 0.3) = 37.2k
        agents = [{"id": "a%d" % k, "ts": 10, "reads": [("/r/store.py", 8_000, 3), ("/r/cli.py", 4_000, 3)]}
                  for k in range(3)]
        self.assertIsNone(rc.rule_shared_reads(agents, 0, os.path.basename))
        # screenshots / PDFs / binaries: each agent looks at them itself
        shots = [{"id": "a%d" % k, "ts": 10, "reads": [("/r/jaeger.PNG", 40_000, 10), ("/r/spec.pdf", 40_000, 10),
                                                       ("/r/app.wasm", 40_000, 10), ("/r/cli.py", 40_000, 10)]}
                 for k in range(9)]
        self.assertIsNone(rc.rule_shared_reads(shots, 0, os.path.basename))

    # (g) polling
    def _polls(self, n):
        s = Sess().prompt().turn(bash("nohup ./long_job.sh &"))
        for k in range(n):
            s.turn(bash("sleep 60 && tail -3 /tmp/job.log"), cw=12_000)
        return s.answer()

    def test_polling_fires(self):
        h, _ = hints_of(self._polls(5))
        self.assertEqual(h["polling"]["evidence"], "5 wake-ups in a row only waited (sleep ×5): 60k CW")
        self.assertEqual(h["polling"]["cw_at_stake"], 48_000)

    def test_polling_near_miss(self):
        self.assertNotIn("polling", hints_of(self._polls(4))[0])

    # (h) an intent far above the session's median, mostly exploration
    def _outlier(self, cw):
        s = Sess().prompt()
        for k in range(5):
            s.turn(read("m%d.py" % k), cw=5_000).turn(edit("m%d.py" % k), cw=5_000)
        s.prompt("why is it slow")
        for k in range(8):
            s.turn(read("deep%d.py" % k), cw=cw)
        return s.turn(edit("deep0.py")).answer()

    def test_outlier_fires(self):
        h, _ = hints_of(self._outlier(30_000))
        self.assertEqual(h["outlier"]["evidence"], "'reading code · deep0.py, deep1.py': 240k CW = 48× the median "
                                                   "intent (5k), 8 of 8 calls reads or searches")

    def test_outlier_near_miss(self):
        self.assertNotIn("outlier", hints_of(self._outlier(20_000))[0])   # 160k < H_MIN_CW

    def test_at_most_three_hints_ranked_by_cw_at_stake(self):
        hs = [rc._hint(r, "t", "e", cw, "x") for r, cw in (("a", 20_000), ("b", 90_000), ("c", 50_000), ("d", 70_000))]
        orig = {k: getattr(rc, k) for k in ("rule_reread", "rule_context", "rule_polling", "rule_toolsearch")}
        try:
            rc.rule_reread = lambda *a: hs[0]
            rc.rule_context = lambda *a: hs[1]
            rc.rule_polling = lambda *a: hs[2]
            rc.rule_toolsearch = lambda *a: hs[3]
            _, r = hints_of(smooth())
        finally:
            for k, v in orig.items():
                setattr(rc, k, v)
        self.assertEqual([h["rule"] for h in r["hints"]], ["b", "d", "c"])


class Payload(unittest.TestCase):
    def test_recap_only_while_idle_or_waiting(self):
        s = smooth()
        state, sess, now = s.state()
        c = state.session_compact(sess)
        self.assertEqual(c["status"], "waiting")
        self.assertEqual(c["recap"]["hints"], [])
        now["t"] += 3600
        self.assertEqual(state.session_compact(sess)["status"], "idle")
        self.assertIn("recap", state.session_compact(sess))
        # the agent works again: no recap
        s2 = Sess()
        s2.recs = list(s.recs)
        s2.sec, s2.n = s.sec, s.n
        s2.prompt("more").turn(read("x.py"))
        state, sess, now = s2.state(at=s2.sec + 2)
        c = state.session_compact(sess)
        self.assertEqual(c["status"], "working")
        self.assertNotIn("recap", c)

    def test_no_recap_before_any_work(self):
        state, sess, _ = Sess().prompt().state()
        self.assertNotIn("recap", state.session_compact(sess))


TOY = os.path.expanduser("~/.claude/projects")


@unittest.skipUnless(glob.glob(os.path.join(TOY, "-workspace-live-demo-stockroom*", "*.jsonl")),
                     "no toy stockroom sessions on this machine")
class ToySessions(unittest.TestCase):
    """The real toy sessions (single tasks, a few with audit subagents, all going smoothly) give no hints."""

    def test_smooth_toy_sessions_have_no_hints(self):
        sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools"))
        import eval_hints
        root = TOY
        for path in sorted(glob.glob(os.path.join(root, "-workspace-live-demo-stockroom*", "*.jsonl"))):
            _, out = eval_hints.replay(root, path, classify(root, path))
            for idx, r in out:
                self.assertEqual(r["hints"], [], "%s idle#%d" % (os.path.basename(path)[:8], idx))


if __name__ == "__main__":
    unittest.main()
