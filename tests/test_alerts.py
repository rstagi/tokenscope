"""Alerts (tokenscope/alerts.py): config parsing, each detector firing and not firing, the detector registry, the
privacy guard, and surfacing (LiveState payloads, the HTTP API, the CLI, board / canvas in a browser) over the
synthetic traces of tests/alerts_synth.py (committed under tests/fixtures/alerts)."""
import argparse
import copy
import io
import json
import math
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
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from tokenscope import alerts as A, cli  # noqa: E402
from tokenscope.alerts import Call, Context, StepFacts  # noqa: E402
from tokenscope.live import server  # noqa: E402
from tokenscope.live.replay import replay  # noqa: E402
import alerts_synth  # noqa: E402

FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "alerts")


def cfg(**over):
    """DEFAULTS with dotted overrides: cfg(**{"cost.jump_warmup": 3})."""
    c = copy.deepcopy(A.DEFAULTS)
    for k, v in over.items():
        A.set_key(c, k, v)
    return c


def steps_of(costs, **kw):
    return [StepFacts(i=i, usd=u, cw=u * 1e5, **kw) for i, u in enumerate(costs)]


def run(ctx, name):
    return [a for a in A.run(ctx) if a.kind == name]


class C:
    """Call builder: one thread's calls, positions in order, one step per call unless given."""

    def __init__(self):
        self.calls = []

    def add(self, kind, key, step=None, **kw):
        n = len(self.calls)
        kw.setdefault("res", "r")
        kw.setdefault("err", False)
        self.calls.append(Call(n=n, step=n if step is None else step, tool=kw.pop("tool", {
            "read": "Read", "search": "Grep", "command": "Bash", "edit": "Edit"}.get(kind, "WebFetch")),
            kind=kind, key=key, **kw))
        return self

    def ctx(self, config=None, **kw):
        steps = kw.pop("steps", None) or steps_of([0.05] * (len(self.calls) + 1))
        return Context(config=config or cfg(), steps=steps, calls=self.calls, **kw)


# ---------------------------------------------------------------- config

class TestConfig(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def path(self, name, text):
        p = os.path.join(self.dir, name)
        with open(p, "w", encoding="utf-8") as f:
            f.write(text)
        return p

    def test_defaults_and_every_key_documented(self):
        c = A.load_config(path=self.path("empty.toml", ""))
        self.assertEqual(c, A.DEFAULTS)
        self.assertFalse(c["off_task"]["enabled"])
        flat = A._flat(A.DEFAULTS)
        self.assertEqual(set(flat), set(A.KEYS))
        doc = A.keys_doc()
        self.assertEqual(len(doc), len(flat))
        self.assertIn("cost.jump_ratio = 4.0  # ", "\n".join(doc))

    def test_readme_documents_every_key_with_its_default(self):
        with open(os.path.join(os.path.dirname(FIXTURES), "..", "..", "README.md"), encoding="utf-8") as f:
            readme = f.read()
        for k, v in A._flat(A.DEFAULTS).items():
            self.assertIn("| `%s` | `%s` |" % (k, json.dumps(v)), readme, k)

    def test_toml_tables_dotted_keys_and_alerts_table(self):
        c = A.load_config(self.path("a.toml", '[cost]\nsession_usd = 10\njump_metric = "cw"\n'
                                              '[looping]\nretry_n = 5\n'))
        self.assertEqual((c["cost"]["session_usd"], c["cost"]["jump_metric"], c["looping"]["retry_n"]), (10.0, "cw", 5))
        self.assertIsInstance(c["cost"]["session_usd"], float)
        c = A.load_config(self.path("b.toml", '[alerts]\nactive_steps = 5\n[alerts.off_task]\nenabled = true\n'))
        self.assertEqual((c["active_steps"], c["off_task"]["enabled"]), (5, True))
        c = A.load_config(self.path("c.toml", 'cost.intent_usd = 2.5\n'))   # `alerts-config` output is valid TOML
        self.assertEqual(c["cost"]["intent_usd"], 2.5)

    def test_json_file_and_overrides_win(self):
        p = self.path("a.json", json.dumps({"cost": {"session_usd": 10}, "looping": {"ignore_polling": False}}))
        c = A.load_config(p, ["cost.session_usd=3", "looping.read_n = 6", "looping.ignore_polling=yes"])
        self.assertEqual((c["cost"]["session_usd"], c["looping"]["read_n"], c["looping"]["ignore_polling"]),
                         (3.0, 6, True))
        self.assertTrue(A.load_config(p, off_task=True)["off_task"]["enabled"])
        self.assertFalse(A.load_config(p, enabled=False)["enabled"])

    def test_alerts_config_output_parses_back(self):
        text = "\n".join(A.keys_doc())
        self.assertEqual(A.load_config(self.path("all.toml", text)), A.DEFAULTS)

    def test_errors(self):
        with self.assertRaisesRegex(ValueError, "unknown key"):
            A.load_config(self.path("x.toml", "[cost]\nsesion_usd = 1\n"))
        with self.assertRaisesRegex(ValueError, "wants a number"):
            A.load_config(None, ["cost.session_usd=lots"])
        with self.assertRaisesRegex(ValueError, "true/false"):
            A.load_config(None, ["off_task.enabled=maybe"])
        with self.assertRaisesRegex(ValueError, "usd or cw"):
            A.load_config(None, ["cost.jump_metric=eur"])
        with self.assertRaisesRegex(ValueError, "key=value"):
            A.load_config(None, ["cost.session_usd"])

    def test_env_path_and_cli_flags(self):
        p = self.path("env.toml", "[cost]\nintent_usd = 1\n")
        old = os.environ.get("TOKENSCOPE_ALERTS_CONFIG")
        os.environ["TOKENSCOPE_ALERTS_CONFIG"] = p
        try:
            self.assertEqual(A.load_config()["cost"]["intent_usd"], 1.0)
            ap = server.add_arguments(argparse.ArgumentParser())
            args = ap.parse_args(["--off-task", "--alert", "looping.retry_n=4", "--alert", "cost.intent_usd=2"])
            c = A.from_args(args)
            self.assertEqual((c["off_task"]["enabled"], c["looping"]["retry_n"], c["cost"]["intent_usd"]), (True, 4, 2.0))
            self.assertFalse(A.from_args(ap.parse_args(["--no-alerts"]))["enabled"])
        finally:
            if old is None:
                os.environ.pop("TOKENSCOPE_ALERTS_CONFIG")
            else:
                os.environ["TOKENSCOPE_ALERTS_CONFIG"] = old

    def test_live_alerts_config_command(self):
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(cli.main(["live", "alerts-config"]), 0)
        self.assertIn("looping.edit_revert_n = 2", out.getvalue())


# ---------------------------------------------------------------- cost

class TestCost(unittest.TestCase):
    def test_baseline_and_jumps_pure(self):
        m, lm, mad = A.robust_baseline([1.0, 1.0, 1.0, 1.0])
        self.assertEqual((m, lm), (1.0, 0.0))
        self.assertAlmostEqual(mad, math.log(1.25))   # MAD floor
        got = A.cost_jumps([0.1, 0.12, 0.09, 0.11, 0.1, 0.1, 1.5], window=20, warmup=6, ratio=4, z=3.5, floor=0.5)
        self.assertEqual([g[0] for g in got], [6])
        self.assertAlmostEqual(got[0][3], 15.0)
        # a spike the session already had is not a jump with max_ratio: only a new high is
        vals = [0.1] * 6 + [1.0] + [0.1] * 6 + [1.2]
        self.assertEqual([g[0] for g in A.cost_jumps(vals, 20, 6, 4, 3.5, 0.5)], [6, 13])
        self.assertEqual([g[0] for g in A.cost_jumps(vals, 20, 6, 4, 3.5, 0.5, max_ratio=1.5)], [6])

    def test_jump_fires_on_a_spike(self):
        ctx = Context(config=cfg(), steps=steps_of([0.1, 0.12, 0.09, 0.11, 0.1, 0.13, 0.1, 1.6, 0.1]))
        got = run(ctx, "cost")
        self.assertEqual([(a.rule, a.step, a.severity) for a in got], [("jump", 7, "critical")])
        self.assertIn("16.0\u00d7 the session median $0.10", got[0].message)
        self.assertEqual(got[0].details["metric"], "usd")

    def test_jump_not_fired(self):
        # during warm-up, under the floor, on gradual growth, or with a noisy baseline
        for costs in ([0.1, 0.1, 0.1, 2.0],                              # warm-up: only 3 prior steps
                      [0.01] * 8 + [0.5],                                 # 50x but under the $0.75 floor
                      [0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0],      # growth, ratio < 4
                      [0.1, 1.0, 0.2, 1.2, 0.1, 0.9, 0.3, 1.1, 2.5]):    # spiky history: ratio < 4
            self.assertEqual(run(Context(config=cfg(), steps=steps_of(costs)), "cost"), [], costs)

    def test_jump_options(self):
        steps = steps_of([0.1] * 7 + [1.6])
        steps[7].delegated = True
        got = run(Context(config=cfg(), steps=steps), "cost")
        self.assertIn("(incl. subagents)", got[0].message)
        self.assertEqual(run(Context(config=cfg(**{"cost.jump_subagents": False}), steps=steps), "cost"), [])
        self.assertEqual(run(Context(config=cfg(**{"cost.jump": False}), steps=steps), "cost"), [])
        # a long step of ordinary calls: a jump per step, none per call
        long = steps_of([0.1] * 7 + [1.6])
        long[7].turns = 16
        self.assertEqual(run(Context(config=cfg(), steps=long), "cost"), [])   # per call by default
        per_step = cfg(**{"cost.jump_per_turn": False})
        self.assertTrue(run(Context(config=per_step, steps=long), "cost")[0].message.endswith("over 16 calls"))
        self.assertTrue(A.DEFAULTS["cost"]["jump_per_turn"])
        one = steps_of([0.1] * 7 + [1.6])
        got = run(Context(config=cfg(), steps=one), "cost")
        self.assertEqual(got[0].message, "step cost per call $1.60, 16.0\u00d7 the session median $0.10")
        # unpriced model: cost-weighted tokens instead, with their own floor
        un = [StepFacts(i=i, usd=0.0, cw=c) for i, c in enumerate([20e3] * 7 + [400e3])]
        got = run(Context(config=cfg(**{"cost.jump_per_turn": False}), steps=un), "cost")
        self.assertEqual((got[0].details["metric"], got[0].message), ("cw", "step cost 400k CW, 20.0\u00d7 the session median 20k CW"))
        self.assertEqual(run(Context(config=cfg(**{"cost.jump_floor_cw": 500000}), steps=un), "cost"), [])

    def test_session_threshold(self):
        steps = steps_of([10.0, 10.0, 10.0])
        got = run(Context(config=cfg(**{"cost.intent_usd": 0}), steps=steps, session_usd=30.0), "cost")
        self.assertEqual([(a.rule, a.step, a.severity) for a in got], [("session_usd", 2, "warning")])
        self.assertEqual(got[0].message, "session spend $30.00 passed the $25.00 threshold")
        got = run(Context(config=cfg(**{"cost.session_usd": 10, "cost.intent_usd": 0}), steps=steps, session_usd=30.0), "cost")
        self.assertEqual((got[0].step, got[0].severity), (0, "critical"))   # >= 2x the threshold
        self.assertEqual(run(Context(config=cfg(), steps=steps_of([1.0]), session_usd=24.9), "cost"), [])
        self.assertEqual(run(Context(config=cfg(**{"cost.session_usd": 0, "cost.intent_usd": 0}), steps=steps, session_usd=99.0), "cost"), [])

    def test_intent_threshold_on_groups_and_steps(self):
        steps = steps_of([1.0, 2.0, 3.0, 6.0, 0.5])
        for x in steps[:3]:
            x.group = 0
        groups = [{"first": 0, "last": 2, "usd": 6.0, "cw": 6e5, "cat": "change"}]
        got = run(Context(config=cfg(**{"cost.jump": False}), steps=steps, groups=groups), "cost")
        # an intent points at the step that took it over the threshold (the group: 1 + 2 + 3 >= 5 at step 2)
        self.assertEqual([(a.rule, a.first, a.step) for a in got], [("intent_usd", 3, 3), ("intent_usd", 0, 2)])
        self.assertEqual(got[1].message, "[CHANGE] intent cost $6.00 (threshold $5.00) over 3 steps")
        groups[0]["last"] = 4
        for x in steps[3:]:
            x.group = 0
        groups[0]["usd"] = 12.5
        got = run(Context(config=cfg(**{"cost.jump": False}), steps=steps, groups=groups), "cost")
        self.assertEqual([(a.rule, a.first, a.step, a.severity) for a in got], [("intent_usd", 0, 2, "critical")])
        self.assertEqual(run(Context(config=cfg(**{"cost.jump": False}), steps=steps_of([4.9, 1.0])), "cost"), [])


# ---------------------------------------------------------------- off-task

class TestOffTask(unittest.TestCase):
    def ctx(self, files, todo="Fix tax rounding in invoice totals", on=True, prompt=None, use_prompt=False):
        c = C()
        for i, f in enumerate(files):
            c.add("read", "k%d" % i, step=i, file="/w/" + f)
        steps = [StepFacts(i=i, todo=todo, prompt=1) for i in range(len(files))]
        return c.ctx(cfg(**{"off_task.enabled": on, "off_task.use_prompt": use_prompt}), steps=steps,
                     prompt_words=[prompt or []])

    DRIFT = ["src/invoice/totals.py", "src/invoice/tax.py", "src/auth/session_store.py", "src/metrics/exporter.py",
             "web/theme/colors.css", "src/auth/login.py"]

    def test_fires_on_drift(self):
        got = run(self.ctx(self.DRIFT), "off_task")
        self.assertEqual([(a.rule, a.first, a.step) for a in got], [("unrelated", 2, 5)])
        self.assertEqual(got[0].message, "4 of the last 4 steps touched files unrelated to the current todo item")

    def test_not_fired(self):
        self.assertEqual(run(self.ctx(self.DRIFT, on=False), "off_task"), [])   # off by default
        on_task = ["src/invoice/totals.py", "src/invoice/tax.py", "tests/test_tax.py", "src/invoice/rounding.py"]
        self.assertEqual(run(self.ctx(on_task), "off_task"), [])
        self.assertEqual(run(self.ctx(self.DRIFT, todo="Fix it"), "off_task"), [])   # too few keywords
        self.assertEqual(run(self.ctx(self.DRIFT[:4]), "off_task"), [])              # only 2 unrelated

    def test_prompt_words_when_no_todo(self):
        words = ["fix", "the", "tax", "rounding", "in", "invoices"]
        self.assertEqual(run(self.ctx(self.DRIFT, todo=None, prompt=words), "off_task"), [])   # off by default
        got = run(self.ctx(self.DRIFT, todo=None, prompt=words, use_prompt=True), "off_task")
        self.assertEqual(got[0].details["source"], "prompt")
        self.assertEqual(run(self.ctx(self.DRIFT, todo=None, prompt=None, use_prompt=True), "off_task"), [])

    def test_related_searches_and_working_set(self):
        def ctx(words):
            c = C()
            c.add("search", "s", step=0, words=words, found=("a.py", "b.py", "c.py"))
            for i, f in enumerate(["src/lib/a.py", "src/lib/b.py", "src/lib/c.py"], 1):
                c.add("read", "r%d" % i, step=i, file="/w/" + f)
            steps = [StepFacts(i=i, todo="Fix tax rounding in invoice totals", prompt=1) for i in range(4)]
            return c.ctx(cfg(**{"off_task.enabled": True}), steps=steps)
        self.assertEqual(run(ctx(("tax",)), "off_task"), [])   # files a related search found
        self.assertEqual(len(run(ctx(("colour",)), "off_task")), 1)   # an unrelated search's finds are not
        c = C()
        for i, f in enumerate(["src/invoice/tax.py", "src/x/helpers2.py"]):   # helpers2 first read with the tax file
            c.add("read", "r%d" % i, step=0, file="/w/" + f)
        for i in range(1, 5):
            c.add("read", "r%d" % (i + 9), step=i, file="/w/src/x/helpers2.py")
        steps = [StepFacts(i=i, todo="Fix tax rounding in invoice totals", prompt=1) for i in range(5)]
        self.assertEqual(run(c.ctx(cfg(**{"off_task.enabled": True}), steps=steps), "off_task"), [])

    def test_words(self):
        self.assertEqual(A.words_of("Fix the TaxRounding bug in invoices"), ["tax", "round", "invoice"])
        self.assertEqual(A.path_words("/home/x/src/auth/session_store.py"), {"auth", "session", "store"})


# ---------------------------------------------------------------- looping

class TestLooping(unittest.TestCase):
    def test_repeat_read_fires_and_escalates(self):
        c = C()
        for _ in range(4):
            c.add("read", "rk", file="/w/a.py").add("search", "s%d" % len(c.calls), res="x%d" % len(c.calls))
        got = run(c.ctx(), "looping")
        self.assertEqual([(a.rule, a.first, a.step, a.severity) for a in got], [("repeat_read", 0, 6, "warning")])
        self.assertEqual(got[0].message, "same .py file read 4\u00d7 with the same result (file #1)")
        for _ in range(4):
            c.add("read", "rk", file="/w/a.py")
        self.assertEqual(run(c.ctx(), "looping")[0].severity, "critical")

    def test_repeat_read_not_fired(self):
        c = C()
        for k in range(5):
            c.add("read", "rk", file="/w/a.py", res="v%d" % k)          # the file changed every time
        self.assertEqual(run(c.ctx(), "looping"), [])
        c = C().add("read", "rk", file="/w/a.py").add("read", "rk", file="/w/a.py").add("read", "rk", file="/w/a.py")
        c.add("edit", "e", file="/w/a.py", edits=(("o", "n"),))
        c.add("read", "rk", file="/w/a.py")                           # an edit to it in between
        self.assertEqual(run(c.ctx(), "looping"), [])
        c = C()
        for k in range(4):
            c.add("read", "rk", file="/w/a.py")
            for j in range(10):
                c.add("search", "s%d%d" % (k, j), res="x%d%d" % (k, j))
        self.assertEqual(run(c.ctx(), "looping"), [])                 # spread wider than the 30-call window
        self.assertEqual(len(run(c.ctx(cfg(**{"looping.window": 50})), "looping")), 1)

    def test_repeat_search_and_command(self):
        c = C()
        for _ in range(4):
            c.add("search", "grep-k")
        self.assertEqual([a.rule for a in run(c.ctx(), "looping")], ["repeat_search"])
        c = C()
        for _ in range(4):
            c.add("command", "git-status")
        got = run(c.ctx(), "looping")
        self.assertEqual((got[0].rule, got[0].message), ("repeat_command", "same command run 4\u00d7 with the same result (command #1)"))
        c = C()
        for _ in range(6):
            c.add("command", "sleep", poll=True)                      # polling a background task
        self.assertEqual(run(c.ctx(), "looping"), [])
        self.assertEqual(len(run(c.ctx(cfg(**{"looping.ignore_polling": False})), "looping")), 1)
        c = C()
        for _ in range(4):
            c.add("other", "mcp-q", tool="mcp__private_srv__query")
        got = run(c.ctx(), "looping")
        self.assertEqual((got[0].rule, got[0].details["tool"]), ("repeat_call", "mcp"))
        self.assertNotIn("private", json.dumps(got[0].to_dict()))

    def test_call_facts_polling(self):
        from tokenscope import intents
        def facts(name, inp):
            rec = intents.record(name, inp)
            return A.call_facts(name, inp, rec, intents.tool_intent(rec))
        self.assertTrue(facts("Bash", {"command": "until [ -f done ]; do sleep 5; done"})["poll"])
        self.assertTrue(facts("Bash", {"command": "zsh /tmp/x.sh", "description": "Bounded wait slice"})["poll"])
        self.assertFalse(facts("Bash", {"command": "npm test", "description": "Run tests"})["poll"])
        self.assertTrue(facts("Read", {"file_path": "/tmp/claude-1/s/tasks/b9mia7b94.output"})["poll"])
        self.assertTrue(facts("mcp__srv__delegate_status", {})["poll"])
        self.assertTrue(facts("mcp__browser__wait", {})["poll"])
        self.assertFalse(facts("mcp__srv__query", {"q": "x"})["poll"])
        self.assertEqual(facts("TodoWrite", {"todos": []})["kind"], "skip")
        self.assertEqual(facts("Edit", {"file_path": "/a.py", "old_string": "a", "new_string": "b"})["edits"],
                         ((A.h("a"), A.h("b")),))

    def test_retry(self):
        c = C()
        for _ in range(3):
            c.add("command", "npm-build", err=True, res="e")
        got = run(c.ctx(), "looping")
        self.assertEqual([(a.rule, a.first, a.step) for a in got], [("retry", 0, 2)])
        self.assertEqual(got[0].message, "same failing command retried 3\u00d7 with no edit in between (command #1)")
        c = C().add("command", "t", err=True).add("command", "t", err=True)
        c.add("edit", "e", file="/w/a.py", edits=(("o", "n"),)).add("command", "t", err=True)   # fail -> edit -> fail
        self.assertEqual(run(c.ctx(), "looping"), [])
        c = C().add("command", "t", err=True).add("command", "t", err=True).add("command", "t", err=False)
        c.add("command", "t", err=True)                               # it passed in between
        self.assertEqual(run(c.ctx(), "looping"), [])
        c = C()
        for _ in range(3):
            c.add("edit", "same-edit", file="/w/a.py", err=True, edits=(("o", "n"),))   # an Edit that keeps failing
        self.assertEqual(run(c.ctx(), "looping")[0].message,
                         "same failing edit to a .py file retried 3\u00d7 with no edit in between (file #1)")

    def test_edit_revert(self):
        c = C()
        for k in range(2):
            c.add("edit", "e1", file="/w/a.py", edits=(("A", "B"),)).add("command", "t%d" % k, err=True)
            c.add("edit", "e2", file="/w/a.py", edits=(("B", "A"),)).add("command", "u%d" % k)
        got = run(c.ctx(), "looping")
        self.assertEqual([(a.rule, a.first, a.step) for a in got], [("edit_revert", 0, 6)])
        self.assertEqual(got[0].message, "edits to the same .py file undone 3\u00d7 (A\u2192B\u2192A) (file #1)")

    def test_edit_revert_not_fired(self):
        # one deliberate break-and-restore (a TDD sanity check) is not a loop at the default edit_revert_n = 2
        c = C().add("edit", "e1", file="/w/a.py", edits=(("A", "B"),)).add("edit", "e2", file="/w/a.py", edits=(("B", "A"),))
        self.assertEqual(run(c.ctx(), "looping"), [])
        self.assertEqual(len(run(c.ctx(cfg(**{"looping.edit_revert_n": 1})), "looping")), 1)
        c = C().add("edit", "e1", file="/w/a.py", edits=(("A", "B"),)).add("edit", "e2", file="/w/b.py", edits=(("B", "A"),))
        c.add("edit", "e3", file="/w/a.py", edits=(("B", "C"),)).add("edit", "e4", file="/w/a.py", edits=(("C", "D"),))
        self.assertEqual(run(c.ctx(), "looping"), [])                 # other file / forward edits only

    def test_write_back_to_an_earlier_content(self):
        c = C()
        for w in ("h1", "h2", "h1", "h2"):
            c.add("edit", "w" + w, file="/w/a.md", tool="Write", write=w)
        got = run(c.ctx(), "looping")
        self.assertEqual((got[0].rule, got[0].details["reverts"]), ("edit_revert", 2))

    def test_subagent_threads(self):
        sub = C()
        for _ in range(3):
            sub.add("command", "t", err=True)
        ctx = C().ctx(agents={"agent-1": (None, sub.calls)})
        got = run(ctx, "looping")
        self.assertEqual([(a.rule, a.agent, a.span) for a in got], [("retry", "agent-1", (0, 2))])
        ctx.config = cfg(**{"looping.subagents": False})
        self.assertEqual(run(ctx, "looping"), [])


# ---------------------------------------------------------------- registry, privacy, public shape

class TestRegistryAndPrivacy(unittest.TestCase):
    def test_custom_detector_and_broken_detector(self):
        class Always:
            name = "always"

            def detect(self, ctx):
                return [A.Alert("looping", "custom", "info", "custom alert", step=0, key="x")]

        class Broken:
            name = "broken"

            def detect(self, ctx):
                raise RuntimeError("boom")
        A.register("always", lambda c: Always())
        A.register("broken", lambda c: Broken())
        try:
            got = A.run(Context(config=cfg(), steps=steps_of([0.1])))
            self.assertEqual([a.rule for a in got], ["custom"])
        finally:
            A.REGISTRY.pop("always")
            A.REGISTRY.pop("broken")
        self.assertNotIn("off_task_haiku", A.REGISTRY)
        with self.assertRaises(NotImplementedError):
            A.HaikuOffTaskDetector(cfg()).detect(Context(config=cfg()))

    def test_disabled(self):
        c = C()
        for _ in range(5):
            c.add("command", "t", err=True)
        self.assertEqual(A.run(c.ctx(cfg(enabled=False))), [])
        self.assertEqual(A.run(c.ctx(cfg(**{"looping.enabled": False}))), [])

    def test_scrub(self):
        s = A.scrub("mail jane.doe@corp.example key sk-ant-abc123def456ghi789 path /home/jane/acme/payroll.py "
                    "~/secrets/x and C:\\Users\\jane\\x.txt file payroll_2026.py token "
                    "AbCdEfGhIjKlMnOpQrStUvWxYz0123456789")
        for bad in ("jane", "corp", "sk-ant", "/home", "payroll", "secrets", "Users", "AbCdEf"):
            self.assertNotIn(bad, s)
        self.assertEqual(A.scrub("same .py file read 6\u00d7 (file #2) $1.20"), "same .py file read 6\u00d7 (file #2) $1.20")

    def test_messages_never_name_files_or_commands(self):
        c = C()
        secret = "/home/alice.smith/acme-payroll/salaries_export.py"
        for _ in range(5):
            c.add("read", "rk", file=secret)
        for _ in range(3):
            c.add("command", A.h("bash", "curl -H 'Authorization: sk-live-123456789abc' https://alice@acme.io"), err=True)
        for k in range(2):
            c.add("edit", "e1", file=secret, edits=(("A", "B"),)).add("edit", "e2", file=secret, edits=(("B", "A"),))
        got = [a.to_dict() for a in A.run(c.ctx())]
        self.assertEqual({a["rule"] for a in got}, {"repeat_read", "retry", "edit_revert"})
        blob = json.dumps(got)
        for bad in ("alice", "acme", "salaries", "payroll", "curl", "sk-live", "home", "export"):
            self.assertNotIn(bad, blob)

    def test_public_active_and_counts(self):
        al = [A.Alert("cost", "session_usd", "warning", "s", step=1, key="session"),
              A.Alert("looping", "retry", "critical", "r", step=9, first=8, key="a"),
              A.Alert("looping", "repeat_read", "warning", "x", step=2, key="b"),
              A.Alert("looping", "retry", "warning", "y", step=0, agent="ag1", key="c")]
        pub = A.public(al, last_step=10, agents_live={"ag1"}, active_steps=3)
        self.assertEqual([a["active"] for a in pub], [True, True, False, True])
        self.assertEqual(pub[1]["first"], 8)
        self.assertEqual(pub[2]["first"], 2)
        self.assertEqual(len({a["id"] for a in pub}), 4)
        self.assertEqual(A.counts(pub), {"n": 4, "active": 3, "worst": "critical",
                                         "by_kind": {"cost": 1, "looping": 3}})


# ---------------------------------------------------------------- the synthetic traces through the live model

EXPECTED = {
    "cost-jump": {("cost", "jump")},
    "cost-threshold": {("cost", "session_usd"), ("cost", "intent_usd")},
    "off-task": {("off_task", "unrelated")},
    "read-loop": {("looping", "repeat_read")},
    "retry-loop": {("looping", "retry")},
    "edit-revert": {("looping", "edit_revert")},
    "clean": set(),
    "showcase": {("cost", "jump"), ("looping", "repeat_read"), ("looping", "retry"), ("looping", "edit_revert"),
                 ("off_task", "unrelated")},
}


def fixture(name):
    return os.path.join(FIXTURES, "-demo-" + name, alerts_synth.SESSIONS[name] + ".jsonl")


class TestSyntheticTraces(unittest.TestCase):
    def test_fixtures_are_current(self):
        d = tempfile.mkdtemp()
        try:
            alerts_synth.write_all(d)
            for name in alerts_synth.SESSIONS:
                with open(fixture(name), encoding="utf-8") as a, \
                        open(os.path.join(d, "-demo-" + name, alerts_synth.SESSIONS[name] + ".jsonl"), encoding="utf-8") as b:
                    self.assertEqual(a.read(), b.read(), "regenerate: python3 tests/alerts_synth.py")
        finally:
            shutil.rmtree(d, ignore_errors=True)

    def test_each_trace_fires_its_alerts(self):
        on = cfg(**{"off_task.enabled": True, "off_task.use_prompt": True})   # the showcase has no todo list
        for name, want in EXPECTED.items():
            state, s = replay(fixture(name), redact=True, alerts_config=on)
            got = state.alerts_public(s)
            self.assertEqual({(a["kind"], m["rule"]) for a in got for m in a["reasons"]}, want, name)
            self.assertTrue(all(a["step"] is not None for a in got), name)
        state, s = replay(fixture("off-task"), redact=True)   # default config: off-task stays off
        self.assertEqual(state.alerts_public(s), [])
        state, s = replay(fixture("showcase"), redact=True, alerts_config=cfg(**{"off_task.enabled": True}))
        self.assertNotIn("off_task", {a["kind"] for a in state.alerts_public(s)})   # no todo: needs use_prompt

    def test_trace_steps(self):
        state, s = replay(fixture("cost-jump"), redact=True)
        a = state.alerts_public(s)[0]
        self.assertEqual((a["step"], a["severity"]), (9, "critical"))
        state, s = replay(fixture("retry-loop"), redact=True)
        a = state.alerts_public(s)[0]
        self.assertEqual(a["message"], "same failing command retried 4\u00d7 with no edit in between (command #1)")

    def test_payloads_carry_alerts_and_no_private_text(self):
        on = cfg(**{"off_task.enabled": True, "off_task.use_prompt": True})
        for redact in (True, False):
            state, s = replay(fixture("showcase"), redact=redact, alerts_config=on)
            now = s.last_ts + 5
            compact = state.session_compact(s, now)
            flow = state.session_flow(s, now)
            self.assertEqual(len(compact["alerts"]), 5)
            self.assertEqual(flow["alerts"], compact["alerts"])
            self.assertEqual({"id", "kind", "rule", "severity", "message", "step", "first", "agent", "details",
                              "active", "reasons", "n", "ts", "at", "intent", "state", "raised", "live"},
                             set(compact["alerts"][0]))
            self.assertEqual({a["state"] for a in compact["alerts"]}, {"new"})
            # the newest ones (retry at the last step, the read loop just before it) are active
            act = {a["rule"] for a in compact["alerts"] if a["active"]}
            self.assertIn("retry", act)
            self.assertNotIn("edit_revert", act)
            row = state.board_row(s, now)
            self.assertEqual((row["alerts"]["n"], row["alerts"]["worst"]), (5, "warning"))
            self.assertTrue(row["alerts"]["top"])
            self.assertEqual(state.follow_tile(s, now)["alerts"]["n"], 5)
            # "intent" is the step's label as the views show it (redaction-aware), not alert text
            blob = json.dumps([{k: v for k, v in a.items() if k != "intent"} for a in compact["alerts"]]) + \
                json.dumps(row["alerts"])
            steps = {x["i"]: x for x in compact["intents"]["steps"]}
            groups = compact["intents"]["groups"]
            for a in compact["alerts"]:
                g = next((g for g in groups if g["first"] <= a["step"] <= g["last"]), None)
                self.assertEqual(a["intent"]["label"], A.scrub((g or steps[a["step"]])["label"]))
            for bad in ("/work", "demo", "invoice", "session_store", "colors", "tax.py", "npm", "eslint", "lint"):
                self.assertNotIn(bad, blob)

    def test_alerts_cached_until_the_session_changes(self):
        state, s = replay(fixture("retry-loop"), redact=True)
        a = state.alerts(s)
        self.assertIs(state.alerts(s), a)
        s.seq += 1
        self.assertIsNot(state.alerts(s), a)

    def test_cli_alerts(self):
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(cli.main(["alerts", "a1e70008", "--root", FIXTURES, "--off-task",
                                       "--alert", "off_task.use_prompt=true"]), 0)
        text = out.getvalue()
        self.assertIn("a1e70008: 5 alerts", text)
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(cli.main(["alerts", "a1e70002", "--root", FIXTURES]), 0)   # one step, two reasons
        self.assertIn("a1e70002: 1 alert\n  [warning] cost/session_usd step 8: session spend $27.58", out.getvalue())
        self.assertIn("\n      + [warning] cost/intent_usd: [", out.getvalue())
        self.assertIn("[critical] cost/jump step 6: step cost per call $0.84, 26.3\u00d7 the session median $0.03", text)
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(cli.main(["alerts", "a1e70008", "--root", FIXTURES, "--json", "--no-alerts"]), 0)
        self.assertEqual(json.loads(out.getvalue())["alerts"], [])


class TestLiveFacts(unittest.TestCase):
    """Call facts the live model records for the detectors."""

    def ingest(self, images):
        from test_live import SID, assistant, tool_use, user_prompt
        from tokenscope.live.state import LiveState
        from tokenscope.live.tail import TrackedFile
        recs = [user_prompt(0, "check the page looks right")]
        for k, data in enumerate(images):
            recs.append(assistant(2 * k + 1, "m%d" % k, [tool_use("t%d" % k, "Read", {"file_path": "/w/shot.png"})]))
            recs.append({"type": "user", "timestamp": recs[-1]["timestamp"], "sessionId": SID, "message": {
                "role": "user", "content": [{"type": "tool_result", "tool_use_id": "t%d" % k, "content": [
                    {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": data}}]}]}})
        state = LiveState(redact=True)
        s = state.ingest(TrackedFile("/w/x.jsonl", SID, "-w"), recs)
        return state.alerts_public(s)

    def test_screenshots_are_not_identical_results(self):
        self.assertEqual(self.ingest(["AAA%d" % k for k in range(6)]), [])   # a new screenshot each time
        self.assertEqual([a["rule"] for a in self.ingest(["AAA"] * 6)], ["repeat_read"])   # really the same image


class TestApi(unittest.TestCase):
    """/api/session/<id>/alerts and the alerts keys of /compact, /flow and /api/board, over recent copies of the
    synthetic traces."""

    @classmethod
    def setUpClass(cls):
        cls.root = tempfile.mkdtemp()
        alerts_synth.write_all(cls.root, t0=time.time() - 600 * len(alerts_synth.SCENARIOS) - 1800)
        cls.live = server.Live(cls.root, since_minutes=10 ** 6, hooks=False, xhints=False,
                               alerts_config=cfg(**{"off_task.enabled": True, "off_task.use_prompt": True}))
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

    def get(self, path):
        with urllib.request.urlopen(self.base + path, timeout=10) as r:
            return json.loads(r.read().decode())

    def test_endpoints(self):
        sid = alerts_synth.SESSIONS["showcase"]
        d = self.get("/api/session/%s/alerts" % sid[:8])
        self.assertEqual(len(d["alerts"]), 5)
        self.assertTrue(d["config"]["off_task"]["enabled"])
        self.assertEqual(self.get("/api/session/%s/compact" % sid[:8])["alerts"], d["alerts"])
        self.assertEqual(self.get("/api/session/%s/flow" % sid[:8])["alerts"], d["alerts"])
        board = {r["id"]: r for r in self.get("/api/board")["sessions"]}
        self.assertEqual(board[sid]["alerts"]["n"], 5)
        self.assertEqual(board[alerts_synth.SESSIONS["clean"]]["alerts"], {"n": 0, "active": 0, "worst": None,
                                                                           "by_kind": {}, "top": [], "new": 0,
                                                                           "new_by_kind": {}, "new_worst": None})
        follow = self.get("/api/follow?s=" + sid[:8])
        self.assertEqual(follow["sessions"][0]["alerts"]["n"], 5)


def _playwright():
    try:
        from playwright.sync_api import sync_playwright  # noqa: F401
        return True
    except Exception:  # noqa: BLE001
        return False


@unittest.skipUnless(_playwright() and os.environ.get("TOKENSCOPE_SKIP_BROWSER") != "1", "Playwright not installed")
class TestBrowser(TestApi):
    def test_canvas_markers_and_board_chips(self):
        from playwright.sync_api import sync_playwright
        sid = alerts_synth.SESSIONS["showcase"]
        with sync_playwright() as p:
            try:
                b = p.chromium.launch()
            except Exception as e:  # noqa: BLE001 - browsers not downloaded
                self.skipTest("no Chromium for Playwright: %s" % e)
            errors = []
            pg = b.new_page(viewport={"width": 1400, "height": 900})
            pg.on("pageerror", lambda e: errors.append(str(e)))
            pg.goto(self.base + "/s/" + sid[:8])
            pg.wait_for_selector(".react-flow__node-intent .amark", timeout=10000)
            marks = pg.locator(".react-flow__node-intent .ihd .amark")
            self.assertGreaterEqual(marks.count(), 4)
            tips = "\n".join(marks.nth(k).get_attribute("data-tip") for k in range(marks.count()))
            for name in ("cost jump", "retry loop", "read loop", "edit revert", "off-task"):
                self.assertIn(name, tips)
            self.assertIn("ALERTS", pg.locator(".head").inner_text())
            marks.first.hover()
            pg.wait_for_selector(".atip", timeout=5000)
            self.assertIn("\u25b2", pg.locator(".atip").inner_text())
            pg.locator(".react-flow__node-intent .inode.alerted.critical").first.click()
            pg.wait_for_selector(".panel .palert", timeout=5000)
            self.assertIn("cost jump", pg.locator(".panel .palert").first.inner_text())
            pg.goto(self.base + "/")
            pg.wait_for_selector(".scard .chip.alert", timeout=10000)
            self.assertGreaterEqual(pg.locator(".scard .chip.alert").count(), 2)
            self.assertEqual(errors, [])
            b.close()


if __name__ == "__main__":
    unittest.main()
