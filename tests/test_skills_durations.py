"""Wall time (ops / steps / groups) and skill invocations (Skill tool + slash-command skills): detection, steps,
the live compact payload, the offline pipeline and tools/skills_stats.py. Synthetic transcripts only."""
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from contextlib import redirect_stdout

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools"))

from tokenscope import skills as sk, steps as sp  # noqa: E402
from tokenscope.live import recap as rc, state as st  # noqa: E402
from tokenscope.live.tail import TrackedFile  # noqa: E402

import intents_offline  # noqa: E402
import skills_stats  # noqa: E402

SID = "bbbbbbbb-1111-2222-3333-444444444444"
T0 = 1_790_000_000.0


def ts(i):
    return T0 + i


def user(i, content, **kw):
    return dict({"type": "user", "timestamp": ts(i), "sessionId": SID, "cwd": "/srv/app",
                 "message": {"role": "user", "content": content}}, **kw)


def asst(i, mid, blocks, stop=None):
    return {"type": "assistant", "timestamp": ts(i), "sessionId": SID,
            "message": {"id": mid, "model": "claude-sonnet-4-5", "content": blocks, "stop_reason": stop,
                        "usage": {"input_tokens": 10, "cache_creation_input_tokens": 100,
                                  "cache_read_input_tokens": 1000, "output_tokens": 20}}}


def tu(tid, name, inp):
    return {"type": "tool_use", "id": tid, "name": name, "input": inp}


def res(i, tid, text="ok"):
    return {"type": "user", "timestamp": ts(i), "sessionId": SID,
            "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": tid, "content": text}]},
            "toolUseResult": {"stdout": text}}


def slash_cmd(i, name, args=""):
    return user(i, "<command-message>%s</command-message>\n<command-name>/%s</command-name>\n"
                   "<command-args>%s</command-args>" % (name, name, args))


def skill_body(i, name, source=None):
    r = user(i, [{"type": "text", "text": "Base directory for this skill: /skills/%s\n\n# %s\n\nDo it." % (name, name)}],
             isMeta=True)
    if source:
        r["sourceToolUseID"] = source
    return r


def builtin_cmd(i, name):
    return user(i, "<command-name>/%s</command-name>\n<command-message>%s</command-message>\n<command-args></command-args>"
                % (name, name))


class Detection(unittest.TestCase):
    def test_skill_tool(self):
        self.assertEqual(sk.tool_skill("Skill", {"skill": "pr-review", "args": "#1"}), "pr-review")
        self.assertEqual(sk.tool_skill("Skill", {"name": "/tdd"}), "tdd")
        self.assertIsNone(sk.tool_skill("Read", {"skill": "x"}))
        self.assertIsNone(sk.tool_skill("Skill", {}))

    def test_scan_both_kinds_without_double_counting(self):
        recs = [
            builtin_cmd(0, "model"), user(1, "<local-command-stdout>Set model</local-command-stdout>"),   # built-in
            slash_cmd(2, "pr-review", "#218"), skill_body(3, "pr-review"),                              # slash skill
            asst(4, "m1", [tu("s1", "Skill", {"skill": "tdd"})]), skill_body(5, "tdd", source="s1"),     # Skill tool
            user(6, "<command-message>workflow-authoring</command-message>\n<command-name>workflow-authoring"
                    "</command-name>\n<skill-format>true</skill-format>", isMeta=True),                 # skill-format
            user(7, [{"type": "text", "text": "# Workflow authoring reference"}], isMeta=True),
            skill_body(8, "release-notes"),                                                              # body only
        ]
        self.assertEqual(sk.scan(recs), [("slash", "pr-review"), ("tool", "tdd"), ("slash", "workflow-authoring"),
                                         ("slash", "release-notes")])

    def test_body_with_an_image_first(self):
        body = user(1, [{"type": "image", "source": {}}, {"type": "text", "text": "Base directory for this skill: "
                                                                                    "/s/prep\n\n# Prep"}], isMeta=True)
        self.assertEqual(sk.scan([slash_cmd(0, "prep"), body]), [("slash", "prep")])

    def test_typed_flag(self):
        sc = sk.SkillScanner()
        sc.feed(slash_cmd(0, "pr-review"))
        self.assertEqual(sc.feed(skill_body(1, "pr-review")), [("slash", "pr-review")])
        self.assertTrue(sc.typed)
        sc.feed(user(2, "<command-name>wa</command-name>\n<skill-format>true</skill-format>", isMeta=True))
        self.assertFalse(sc.typed)


class Durations(unittest.TestCase):
    def test_fmt_dur(self):
        self.assertEqual([sp.fmt_dur(x) for x in (None, 0, 4.9, 59, 72, 3600, 3780, 7322)],
                         ["", "0s", "4s", "59s", "1m12s", "1h0m", "1h3m", "2h2m"])
        self.assertEqual(sp.duration(10.0, 14.26), 4.3)
        self.assertIsNone(sp.duration(None, 5.0))

    def test_step_runs_to_its_last_result(self):
        turns = [{"ts": 100.0, "ts_end": 104.0, "cw": 1, "usd": 0, "tools": [{"name": "Read", "input": {"file_path": "a.py"}}]},
                 {"ts": 106.0, "ts_end": 170.0, "cw": 1, "usd": 0, "tools": [{"name": "Read", "input": {"file_path": "b.py"}}]},
                 {"ts": 171.0, "cw": 1, "usd": 0, "tools": [{"name": "Edit", "input": {"file_path": "a.py"}}]}]
        s = sp.segment(turns, open_last=False)
        self.assertEqual([x["dur_s"] for x in s], [70.0, 0.0])
        g = sp.groups(s, [{"label": "Do it", "first": 0, "last": 1}])
        self.assertEqual((g[0]["dur_s"], g[0]["ts0"], g[0]["ts1"]), (71.0, 100.0, 171.0))


class Steps(unittest.TestCase):
    def T(self, *tools, **kw):
        return dict({"ts": None, "cw": 10.0, "usd": 0.01, "tools": [{"name": n, "input": i} for n, i in tools]}, **kw)

    def test_skill_tool_tags_step_and_leads_summary(self):
        s = sp.segment([self.T(("Skill", {"skill": "pr-review"}), ("Read", {"file_path": "/a.py"}),
                               ("Read", {"file_path": "/b.py"}))], open_last=False)
        self.assertEqual(s[0]["skills"], ["pr-review"])
        self.assertEqual(sp.summary(s[0]), "✦ pr-review · Read ×2")

    def test_slash_skill_event_tags_the_next_step_and_typed_is_a_boundary(self):
        turns = [self.T(("Read", {"file_path": "/a.py"})), self.T(("Read", {"file_path": "/b.py"})),
                 self.T(("Read", {"file_path": "/c.py"}))]
        ev = [{"kind": "prompt", "at": 0}, {"kind": "skill", "at": 1, "name": "pr-review", "user": True}]
        s = sp.segment(turns, ev, open_last=False)
        self.assertEqual([(x["turn_first"], x["skills"], x["prompt_before"]) for x in s],
                         [(0, [], True), (1, ["pr-review"], True)])
        self.assertTrue(sp.summary(s[1]).startswith("✦ pr-review · "))
        # loaded by the harness (not typed): tagged, no boundary
        ev[1]["user"] = False
        s = sp.segment(turns, ev, open_last=False)
        self.assertEqual(len(s), 1)
        self.assertEqual(s[0]["skills"], ["pr-review"])

    def test_group_collects_skills(self):
        turns = [self.T(("Skill", {"skill": "tdd"}), ("Read", {"file_path": "/a.py"})),
                 self.T(("Edit", {"file_path": "/a.py"})),
                 self.T(("Skill", {"skill": "pr-review"}), ("Read", {"file_path": "/b.py"}))]
        s = sp.segment(turns, open_last=False)
        g = sp.groups(s, [{"label": "Build", "first": 0, "last": 2}])
        self.assertEqual(g[0]["skills"], ["tdd", "pr-review"])
        self.assertTrue(g[0]["summary"].startswith("✦ tdd · ✦ pr-review · "))


class LivePayload(unittest.TestCase):
    def records(self):
        return [user(0, "please review and fix the store"),
                slash_cmd(1, "pr-review", "#218"), skill_body(1.5, "pr-review"),
                asst(2, "m1", [tu("s1", "Skill", {"skill": "tdd"})]), skill_body(2.5, "tdd", source="s1"),
                res(5, "s1", "Launching skill: tdd"),
                asst(6, "m2", [tu("r1", "Read", {"file_path": "/srv/app/store.py"})]), res(7, "r1"),
                asst(10, "m3", [tu("e1", "Edit", {"file_path": "/srv/app/store.py"})]), res(12, "e1"),
                asst(13, "m4", [tu("b1", "Bash", {"command": "python3 -m unittest -q"})]),
                res(17, "b1", "Ran 3 tests in 0.1s\n\nOK"),
                asst(18, "m5", [{"type": "text", "text": "Done."}], stop="end_turn")]

    def state(self, recs, now, redact=False):
        clock = {"t": now}
        state = st.LiveState(redact=redact, clock=lambda: clock["t"])
        s = state.ingest(TrackedFile("/x/%s.jsonl" % SID, SID, "-srv-app"), recs)
        return state, s, clock

    def test_skills_in_compact_and_steps(self):
        state, s, _ = self.state(self.records(), ts(30))
        c = state.session_compact(s)
        self.assertEqual(c["skills"], [{"name": "pr-review", "count": 1}, {"name": "tdd", "count": 1}])
        steps = c["intents"]["steps"]
        self.assertEqual(steps[0]["skills"], ["pr-review", "tdd"])
        self.assertTrue(steps[0]["summary"].startswith("✦ pr-review · ✦ tdd"))
        json.dumps(c)

    def test_step_and_op_durations(self):
        state, s, _ = self.state(self.records(), ts(30))
        steps = state.session_compact(s)["intents"]["steps"]
        # first call to the last result of each step: read (6 -> 7), edit (10 -> 12), tests (13 -> 17); the
        # skill-only step spans the work it drove: its load (2) to the end of the tests (17)
        self.assertEqual([x["dur_s"] for x in steps], [15.0, 1.0, 2.0, 4.0])
        self.assertEqual([x["skills"] for x in steps], [["pr-review", "tdd"], [], [], []])
        self.assertTrue(all("ops" not in x for x in steps))   # waiting: no live step

    def test_skill_step_duration_covers_the_work_it_drove(self):
        # the Skill call itself returns at once (2 -> 2): the step still reports the driven work, never 0s
        recs = self.records()
        recs[5] = res(2, "s1", "Launching skill: tdd")
        state, s, clock = self.state(recs, ts(30))
        steps = state.session_compact(s)["intents"]["steps"]
        self.assertEqual(steps[0]["label"], "Use skills pr-review, tdd")
        self.assertGreater(steps[0]["dur_s"], 0)
        self.assertEqual(steps[0]["dur_s"], 15.0)
        # ... up to the next user prompt (or skill), not past it
        more = recs + [user(40, "now the docs"), asst(41, "m6", [tu("r2", "Read", {"file_path": "/srv/app/README.md"})]),
                       res(50, "r2")]
        state, s, _ = self.state(more, ts(60))
        it = state.session_compact(s)["intents"]
        self.assertEqual(it["steps"][0]["dur_s"], 15.0)
        # a group holding the skill step is never shorter than it
        turn = lambda t0, t1, name, inp: {"ts": t0, "ts_end": t1, "cw": 10.0, "usd": 0.01,
                                          "tools": [{"name": name, "input": inp}]}
        seg = sp.segment([turn(100, 100, "Skill", {"skill": "tdd"}), turn(105, 109, "Read", {"file_path": "/a.py"}),
                          turn(110, 120, "Edit", {"file_path": "/a.py"})], open_last=False)
        self.assertEqual([x["dur_s"] for x in seg], [20.0, 4.0, 10.0])
        g = sp.groups(seg, [{"label": "Start", "first": 0, "last": 1}])
        self.assertEqual(g[0]["dur_s"], 20.0)
        # while working, a skill step driving the live step ticks with it
        recs = self.records()[:8]
        state, s, clock = self.state(recs, ts(20))
        it = state.session_compact(s)["intents"]
        self.assertEqual(it["steps"][0]["dur_s"], 18.0)   # 2 -> now (20)

    def test_running_op_and_open_step_tick_while_working(self):
        recs = self.records()[:-3] + [asst(13, "m4", [tu("b1", "Bash", {"command": "python3 -m unittest -q"})])]
        state, s, clock = self.state(recs, ts(20))
        it = state.session_compact(s)["intents"]
        live = it["steps"][-1]
        self.assertIn("ops", live)                            # a test run closes its step, but it is still live
        self.assertEqual(live["dur_s"], 7.0)                  # 13 -> now (20)
        self.assertEqual(live["ops"][-1]["dur_s"], 7.0)       # running call: elapsed
        self.assertIsNone(live["ops"][-1]["err"])
        clock["t"] = ts(25)
        live = state.session_compact(s)["intents"]["steps"][-1]
        self.assertEqual((live["dur_s"], live["ops"][-1]["dur_s"]), (12.0, 12.0))

    def test_skill_op_and_group_fields(self):
        state, s, clock = self.state(self.records()[:4], ts(4))
        it = state.session_compact(s)["intents"]
        op = it["steps"][-1]["ops"][0]
        self.assertEqual(op, {"tool": "skill:tdd", "target": "tdd", "cw": op["cw"], "err": None, "dur_s": 2.0,
                              "skill": "tdd"})   # running since 2
        s = state.ingest(TrackedFile("/x/%s.jsonl" % SID, SID, "-srv-app"), self.records()[4:6])
        clock["t"] = ts(6)
        it = state.session_compact(s)["intents"]
        self.assertEqual(it["steps"][-1]["ops"][0]["dur_s"], 3.0)   # tool_use (2) -> tool_result (5)
        self.assertEqual(it["steps"][-1]["ops"][0]["err"], False)
        for g in it["groups"]:
            self.assertIn("dur_s", g)
            self.assertIn("skills", g)

    def test_typed_slash_skill_counts_as_activity(self):
        recs = self.records()[:3]
        state, s, _ = self.state(recs, ts(3))
        self.assertEqual(state.status(s), "working")
        self.assertEqual(s.slash_skills, [{"at": 0, "name": "pr-review", "user": True}])

    def test_redacted_payload_keeps_skill_names(self):
        state, s, _ = self.state(self.records(), ts(30), redact=True)
        c = state.session_compact(s)
        self.assertEqual([x["name"] for x in c["skills"]], ["pr-review", "tdd"])


class RecapHints(unittest.TestCase):
    def test_hint_has_action_and_kind(self):
        h = rc._hint("reread", "Do this", "because", 20_000, "x")
        self.assertEqual((h["action"], h["text"], h["kind"], h["evidence"]), ("Do this", "Do this", "session", "because"))


class Offline(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        proj = os.path.join(self.root, "-srv-app")
        os.makedirs(os.path.join(proj, SID, "subagents"))
        with open(os.path.join(proj, SID + ".jsonl"), "w", encoding="utf-8") as f:
            for r in LivePayload().records():
                f.write(json.dumps(r) + "\n")
        with open(os.path.join(proj, SID, "subagents", "agent-a1.jsonl"), "w", encoding="utf-8") as f:
            f.write(json.dumps(dict(asst(3, "x1", [tu("s9", "Skill", {"skill": "tdd"})]), isSidechain=True)) + "\n")
            f.write("{bad json\n")

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def test_skills_stats_tool(self):
        st_, n = skills_stats.stats(self.root)
        self.assertEqual(n, 2)
        self.assertEqual(st_["tdd"], {"total": 2, "tool": 2, "slash": 0, "sessions": 1})
        self.assertEqual(st_["pr-review"], {"total": 1, "tool": 0, "slash": 1, "sessions": 1})
        out = io.StringIO()
        with redirect_stdout(out):
            skills_stats.main(["--root", self.root])
        lines = out.getvalue().splitlines()
        self.assertTrue(lines[2].startswith("tdd "))
        self.assertTrue(lines[-1].split()[:4] == ["TOTAL", "3", "2", "1"])

    def test_offline_replay_carries_skills_and_durations(self):
        path = os.path.join(self.root, "-srv-app", SID + ".jsonl")
        sid, _, steps, _, skills = intents_offline.replay_one(path)
        self.assertEqual(skills, {"pr-review": 1, "tdd": 2})
        self.assertEqual(intents_offline.skills_rows(skills), [{"name": "tdd", "count": 2}, {"name": "pr-review", "count": 1}])
        row = intents_offline.step_row(steps[0], None)
        self.assertEqual(row["skills"], ["pr-review", "tdd"])
        self.assertEqual(row["dur_s"], 15.0)   # skill load (2) to the end of the work it drove (17)
        self.assertTrue(row["summary"].startswith("✦ pr-review"))


if __name__ == "__main__":
    unittest.main()
