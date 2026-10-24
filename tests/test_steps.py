"""Steps and intent groups (docs/intents-spec.md): segmentation, labels, locking, groups, Haiku batches, the
declare_intent MCP server and the live compact payload."""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tokenscope import stepnames, steps as sp  # noqa: E402
from tokenscope.live import intent_mcp, state as st  # noqa: E402
from tokenscope.live.tail import TrackedFile  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def T(*tools, cw=10.0, test=None, text="", thinking=0, sub_cw=0.0):
    return {"ts": None, "cw": cw, "usd": cw / 1000, "tools": [{"name": n, "input": i} for n, i in tools],
            "test": test, "text": text, "thinking": thinking, "sub_cw": sub_cw, "sub_usd": 0.0}


def read(f):
    return ("Read", {"file_path": "/src/" + f})


def edit(f):
    return ("Edit", {"file_path": "/src/" + f})


def bash(c):
    return ("Bash", {"command": c})


def todo(*items):
    return ("TodoWrite", {"todos": [{"content": c, "status": s, "activeForm": a} for c, s, a in items]})


def declare(label, kind="change"):
    return ("mcp__tokenscope-intents__declare_intent", {"label": label, "kind": kind})


def agent(desc):
    return ("Agent", {"description": desc, "subagent_type": "Explore", "prompt": "x"})


def bounds(steps):
    return [(s["turn_first"], s["turn_last"]) for s in steps]


class Segment(unittest.TestCase):
    def test_rule1_category_change(self):
        s = sp.segment([T(read("a.py")), T(read("b.py")), T(edit("a.py")), T(read("c.py"))])
        self.assertEqual(bounds(s), [(0, 1), (2, 2), (3, 3)])
        self.assertEqual([x["cat"] for x in s], ["investigate", "change", "investigate"])
        self.assertEqual(s[0]["files"], ["a.py", "b.py"])

    def test_rule2_test_run_closes(self):
        s = sp.segment([T(bash("python3 -m unittest -q")), T(bash("python3 -m unittest -q"), test="pass")])
        self.assertEqual(bounds(s), [(0, 0), (1, 1)])
        self.assertEqual(s[1]["tested"], "pass")
        self.assertTrue(s[1]["closed"])   # it ran tests: over, even as the last step
        self.assertIn("Bash unittest ✓", sp.summary(s[1]))

    def test_rule3_prompt(self):
        s = sp.segment([T(read("a.py")), T(read("b.py"))], [{"kind": "prompt", "at": 0}, {"kind": "prompt", "at": 1}])
        self.assertEqual(bounds(s), [(0, 0), (1, 1)])
        self.assertTrue(s[1]["prompt_before"])

    def test_rule4_subagent(self):
        s = sp.segment([T(read("a.py")), T(agent("Audit store.py"), sub_cw=500), T(agent("Audit cli.py")),
                        T(read("b.py"))])
        self.assertEqual(bounds(s), [(0, 0), (1, 1), (2, 2), (3, 3)])
        self.assertEqual(s[1]["cat"], "delegate")
        self.assertEqual(s[1]["cw"], 510.0)

    def test_rule5_todo_move(self):
        s = sp.segment([T(read("a.py")), T(todo(("A", "in_progress", "Doing A"))), T(read("b.py")),
                        T(todo(("A", "in_progress", "Doing A"))), T(read("c.py"))])
        # the first TodoWrite moves A to in_progress: boundary after it; the repeat moves nothing
        self.assertEqual(bounds(s), [(0, 1), (2, 4)])
        self.assertEqual(s[1]["todo"], "Doing A")

    def test_rule6_declare(self):
        s = sp.segment([T(read("a.py")), T(declare("Fix tax rounding")), T(edit("a.py")), T(edit("b.py"))])
        self.assertEqual(bounds(s), [(0, 0), (1, 3)])
        self.assertEqual(s[1]["cat"], "change")   # the declare-only turn takes the next category
        self.assertEqual(s[1]["declared"], "Fix tax rounding")
        self.assertNotIn("tokenscope-intents.declare_intent", s[1]["tools"])

    def test_stable_when_appending(self):
        turns = [T(read("a.py")), T(edit("a.py")), T(bash("pytest -q"), test="fail"), T(edit("a.py")),
                 T(todo(("A", "in_progress", None), ("B", "pending", None))), T(read("x.py")), T(agent("Survey")),
                 T(declare("Go")), T(read("z.py")), T(bash("pytest"), test="pass"), T(edit("q.py"))]
        events = [{"kind": "prompt", "at": 0}, {"kind": "prompt", "at": 6}]
        full = sp.segment([dict(t) for t in turns], events)
        for n in range(1, len(turns)):
            part = sp.segment([dict(t) for t in turns[:n]], events)
            self.assertEqual([x["turn_first"] for x in part], [x["turn_first"] for x in full][:len(part)])
            for a, b in zip(part[:-1], full):
                self.assertEqual((a["turn_first"], a["turn_last"], a["cat"]), (b["turn_first"], b["turn_last"], b["cat"]))
            self.assertFalse(part[-1]["closed"] and not (turns[n - 1].get("test") or "TodoWrite" in str(turns[n - 1])))

    def test_open_last(self):
        self.assertFalse(sp.segment([T(read("a.py"))])[-1]["closed"])
        self.assertTrue(sp.segment([T(read("a.py"))], open_last=False)[-1]["closed"])

    def test_summary_and_rule_label(self):
        s = sp.segment([T(read("a.py"), read("b.py"), ("Grep", {"pattern": "x"}), thinking=1),
                        T(read("c.py"), ("mcp__srv__get_thing", {}), ("Glob", {"pattern": "*"}))])[0]
        self.assertEqual(sp.summary(s), "Read ×3 · Grep · srv.get_thing · Glob · 1 thinking")
        self.assertEqual(sp.rule_label(s), "reading code · a.py, b.py")

    def test_bash_program(self):
        self.assertEqual(sp.display_name("Bash", {"command": "cd x && git status"}), "Bash git status")
        self.assertEqual(sp.display_name("Bash", {"command": "h=$(git rev-parse HEAD) && echo $h"}), "Bash git rev-parse")
        self.assertEqual(sp.display_name("Skill", {"skill": "pr-review"}), "✦ pr-review")
        self.assertEqual(sp.display_name("Bash", {"command": "FOO=1 python3 -m pytest -q"}), "Bash pytest")
        self.assertEqual(sp.display_name("Bash", {"command": "python3 train.py --epochs 10"}), "Bash python3 train.py")
        self.assertEqual(sp.display_name("run_command", {"CommandLine": "python3 train.py"}), "run_command python3 train.py")
        self.assertEqual(sp.display_name("Bash", {"command": "pip install -r requirements.txt"}), "Bash pip install")
        self.assertEqual(sp.display_name("Bash", {"command": "bash setup.sh"}), "Bash bash setup.sh")
        self.assertEqual(sp.display_name("Bash", {"command": "systemctl restart nginx"}), "Bash systemctl restart")
        self.assertEqual(sp.program("python3 train.py"), "python3 train.py")
        self.assertEqual(sp.program("python3 -m http.server 8080"), "python3 -m http.server")
        self.assertEqual(sp.program("bash deploy.sh"), "bash deploy.sh")
        self.assertEqual(sp.program("pip install torch"), "pip install")
        # rule_label reflects actual command / script target
        s_py = sp.segment([T(bash("python3 train.py"))], open_last=False)[0]
        self.assertEqual(sp.rule_label(s_py), "running a script · python3 train.py")
        s_pip = sp.segment([T(bash("pip install flask"))], open_last=False)[0]
        self.assertEqual(sp.rule_label(s_pip), "running a build/command · pip install")

    def test_program_skips_shell_syntax(self):
        for cmd, want in [("for f in *; do cat \"$f\"; done", "cat"), ("if [ -f x ]; then make; fi", "make"),
                          ("while true; do curl -s x; sleep 1; done", "curl"), ("until ! pytest; do :; done", "pytest"),
                          ("if [[ -n $X ]]; then echo hi; else git status; fi", "echo"),
                          ("select x in a b; do ls; done", "ls"), ("{ ls; } > out", "ls"),
                          ("for i in 1 2; do for j in 3; do wc -l $j; done; done", "wc"),
                          ("./srv & disown", "srv"), ("cd x; ./srv &\ndisown", "srv"),
                          ("echo hi", "echo"), ("export A=1", "export"), ("cd /x", "cd")]:
            self.assertEqual(sp.program(cmd), want, cmd)
        label = sp.rule_label(sp.segment([T(bash("for f in *; do cat \"$f\"; done"))], open_last=False)[0])
        self.assertNotIn("for", label)

    def test_program_is_never_a_number_or_flag(self):
        for cmd, want in [("sleep 100", "sleep"), ("timeout 100 zsh -c 'until x; do sleep 5; done'", "zsh"),
                          ("cd /tmp && timeout 30 agent-browser --session r snapshot", "agent-browser"),
                          ("timeout -s KILL 60 make build", "make build"), ("nohup 5", None),
                          ("T(){ curl -s x; }; T a", "T"), ("[ -n \"$X\" ] && echo yes", "echo"),
                          ("cd /x", "cd"), ("-v", None)]:
            self.assertEqual(sp.program(cmd), want, cmd)
        for cmd in ("sleep 100", "timeout 100 sleep 5", "timeout 105 zsh -c 'sleep 1'", "R=1; timeout 90 sleep 2"):
            s = sp.segment([T(bash(cmd))], open_last=False)[0]
            label = sp.rule_label(s)
            self.assertNotRegex(label, r"· .*\b\d+\b", cmd)
            self.assertNotRegex(label, r"· -", cmd)
        self.assertEqual(sp.rule_label(sp.segment([T(bash("sleep 100"))], open_last=False)[0]), "waiting · sleep")


class Labels(unittest.TestCase):
    def test_todo_labels_and_group(self):
        turns = [T(todo(("Read code", "pending", "Reading code"), ("Fix bug", "pending", "Fixing bug"))),
                 T(todo(("Read code", "in_progress", "Reading code"), ("Fix bug", "pending", "Fixing bug"))),
                 T(read("a.py")), T(edit("a.py")), T(bash("pytest"), test="pass"),
                 T(todo(("Read code", "completed", "Reading code"), ("Fix bug", "in_progress", "Fixing bug"))),
                 T(edit("b.py"))]
        s = sp.segment(turns, [{"kind": "prompt", "at": 0}], open_last=False)
        lab = sp.Labeller()
        gs = lab.finalize(s)
        self.assertEqual((s[0]["label"], s[0]["source"], s[0]["cat"]), ("Plan 2 tasks", "plan", "plan"))
        # the todo item spans 3 steps: it labels their group, each step gets its own (here: rule) label;
        # "Fixing bug" spans one step and keeps the todo label
        self.assertEqual([(x["label"], x["source"]) for x in s[1:]],
                         [("reading code · a.py", "rule"), ("editing files · a.py", "rule"),
                          ("running tests · pytest", "rule"), ("Fixing bug", "todo")])
        self.assertEqual([(g["label"], g["source"], g["first"], g["last"], g["steps"]) for g in gs],
                         [("Reading code", "todo", 1, 3, 3)])
        self.assertEqual(gs[0]["cat"], "verify")   # the test step + the todo update that ends it
        self.assertAlmostEqual(gs[0]["cw"], 40.0)

    def test_plan_only_with_evidence(self):
        s = sp.segment([T(todo(("A", "pending", None))), T(read("a.py"))], open_last=False)
        self.assertIsNone(s[0].get("plan"))   # a one-item list is not a plan
        s = sp.segment([T(("TaskList", {})), T(read("a.py"))], open_last=False)
        self.assertNotEqual(sp.label_from_signals(s[0])[1], "plan")
        s = sp.segment([T(("ExitPlanMode", {"plan": "1. read\n2. fix\n3. test"}))], open_last=False)
        self.assertEqual(sp.label_from_signals(s[0]), ("Plan 3 steps", "plan"))

    def test_subagent_labels(self):
        s = sp.segment([T(agent("Audit store.py edge cases")),
                        T(agent("Audit store.py"), agent("Audit pricing.py"), agent("Audit cli.py")),
                        T(agent("Survey repo"), agent("Check tests"))])
        self.assertEqual(sp.label_from_signals(s[0]), ("Audit store.py edge cases", "subagent"))
        self.assertEqual(sp.label_from_signals(s[1])[0], "Audit ×3: store.py, pricing.py, cli.py")
        self.assertTrue(sp.label_from_signals(s[2])[0].startswith("2 subagents: Survey repo"))

    def test_declared_labels_and_group(self):
        s = sp.segment([T(declare("Fix tax rounding")), T(read("a.py")), T(edit("a.py")), T(read("b.py"))],
                       [{"kind": "prompt", "at": 0}, {"kind": "prompt", "at": 3}], open_last=False)
        gs = sp.Labeller().finalize(s)
        self.assertEqual([x["source"] for x in s], ["rule"] * 3)   # the label is the group's, not each step's
        self.assertEqual(s[1]["label"], "editing files · a.py")
        self.assertEqual([(g["label"], g["source"], g["first"], g["last"], g["cat"]) for g in gs],
                         [("Fix tax rounding", "declared", 0, 1, "change")])   # tag from the declared kind

    def test_declared_single_step_keeps_label(self):
        s = sp.segment([T(declare("Check the config"), read("a.py")), T(edit("b.py"))],
                       [{"kind": "prompt", "at": 0}, {"kind": "prompt", "at": 1}], open_last=False)
        gs = sp.Labeller().finalize(s)
        self.assertEqual((s[0]["label"], s[0]["source"]), ("Check the config", "declared"))
        self.assertEqual(gs, [])

    def test_declared_group_kind_to_category(self):
        def tag(kind):
            s = sp.segment([T(declare("Do it", kind), read("a.py"), cw=5), T(edit("a.py"), cw=50),
                            T(read("b.py"), cw=10)], open_last=False)
            return sp.Labeller().finalize(s)[0]["cat"]
        self.assertEqual([tag(k) for k in ("investigate", "plan", "change", "verify")],
                         ["investigate", "plan", "change", "verify"])
        self.assertEqual(tag("other"), "change")   # CW-dominant category
        self.assertEqual(tag(None), "change")

    def test_declared_group_steps_get_model_labels(self):
        s = sp.segment([T(declare("Fix tax rounding"), read("a.py")), T(edit("a.py")), T(bash("pytest"), test="pass")],
                       open_last=False)
        lab = sp.Labeller()
        lab.assign(s)
        self.assertEqual([x["i"] for x in lab.pending(s)], [0, 1, 2])
        lab.add_model_labels({0: "Read the cart code", 1: "Round tax per line", 2: "Run the cart tests"})
        gs = lab.assign(s)
        self.assertEqual([(x["label"], x["source"]) for x in s],
                         [("Read the cart code", "haiku"), ("Round tax per line", "haiku"),
                          ("Run the cart tests", "haiku")])
        self.assertEqual([(g["label"], g["source"], g["steps"]) for g in gs], [("Fix tax rounding", "declared", 3)])

    def test_declared_label_not_locked_while_span_may_grow(self):
        # live: the last step closed after its test run, but the next step may continue the same declared span
        turns = [T(declare("Fix tax rounding"), bash("pytest"), test="fail")]
        lab = sp.Labeller()
        lab.no_model = True
        s = sp.segment(turns)
        lab.assign(s)
        self.assertEqual((s[0]["source"], s[0]["closed"], s[0]["locked"]), ("declared", True, False))
        s = sp.segment(turns + [T(edit("a.py"))])
        gs = lab.assign(s)
        self.assertEqual((s[0]["label"], s[0]["source"], s[0]["locked"]), ("running tests · pytest", "rule", True))
        self.assertEqual([(g["label"], g["first"], g["last"]) for g in gs], [("Fix tax rounding", 0, 1)])

    def test_toolsearch_step_is_overhead_with_rule_label(self):
        ts = lambda q: ("ToolSearch", {"query": q, "max_results": 5})
        turns = [T(ts("select:mcp__tokenscope-intents__declare_intent")), T(read("a.py")),
                 T(ts("select:WebFetch,WebSearch"), ts("select:Monitor")), T(edit("a.py")),
                 T(ts("notebook jupyter")), T(edit("b.py"))]
        lab = sp.Labeller()   # model labels on: the ToolSearch steps must not wait for one
        s = sp.segment(turns)
        lab.assign(s)
        got = [(x["cat"], x["label"], x["source"], x["locked"]) for x in s if x["cat"] == "overhead"]
        self.assertEqual(got, [("overhead", "Load declare_intent tool", "rule", True),
                               ("overhead", "Load 3 deferred tools", "rule", True),
                               ("overhead", "Load deferred tools", "rule", True)])
        self.assertNotIn(0, [x["i"] for x in lab.pending(s)])
        self.assertEqual([x["i"] for x in lab.pending(s)], [1, 3])

    def test_skill_only_step_has_rule_label_and_keeps_category(self):
        sk = lambda n: ("Skill", {"skill": n})
        turns = [T(read("a.py")), T(sk("tdd")), T(edit("a.py")), T(sk("pr-review"), sk("docs")), T(bash("ls")),
                 T(edit("b.py"), sk("lint"))]
        lab = sp.Labeller()   # model labels on: the skill-only steps must not wait for one
        s = sp.segment(turns, open_last=False)
        lab.assign(s)
        got = {x["label"]: (x["source"], x["locked"]) for x in s if x["skills"]}
        self.assertEqual(got.get("Use skill tdd"), ("rule", True))
        self.assertEqual(got.get("Use skills pr-review, docs"), ("rule", True))
        mixed = next(x for x in s if "lint" in x["skills"])
        self.assertIsNone(sp.skill_label(mixed))
        for x in s:
            if x["skills"] and sp.skill_label(x):
                self.assertNotEqual(x["cat"], "overhead")
                self.assertNotIn(x["i"], [y["i"] for y in lab.pending(s)])

    def test_slash_skill_only_step_has_rule_label(self):
        step = {"tools": {"TodoWrite": 1}, "skills": ["tdd"], "cat": "other"}
        self.assertEqual(sp.label_from_signals(step), ("Use skill tdd", "rule"))
        self.assertEqual(sp.label_from_signals({"tools": {"✦ tdd": 1}, "skills": ["tdd"]}),
                         ("Use skill tdd", "rule"))
        self.assertIsNone(sp.skill_label({"tools": {"✦ tdd": 1, "Read": 2}, "skills": ["tdd"]}))
        self.assertIsNone(sp.skill_label({"tools": {"Read": 2}, "skills": []}))

    def test_toolsearch_in_mixed_turn_is_its_own_step(self):
        ts = ("ToolSearch", {"query": "select:mcp__tokenscope-intents__declare_intent"})
        turns = [T(ts, bash("git ls-files"), cw=30.0, thinking=1, text="look at the repo"), T(read("a.py")),
                 T(ts, declare("Fix it"), edit("a.py"), bash("pytest -q"), cw=30.0, test="pass"), T(edit("b.py"))]
        events = [{"kind": "prompt", "at": 0}]
        lab = sp.Labeller()
        lab.finalize(no_model=True)
        s = sp.segment([dict(t) for t in turns], events)
        lab.assign(s)
        got = [(x["cat"], x["label"], x["cw"], x["turn_first"], x["part_first"], x["prompt_before"]) for x in s]
        self.assertEqual(got, [("overhead", "Load declare_intent tool", 15.0, 0, 0, True),
                               ("investigate", "checking git state · a.py", 25.0, 0, 1, False),
                               ("overhead", "Load declare_intent tool", 10.0, 2, 0, False),
                               ("change", "editing files · a.py", 20.0, 2, 1, False),
                               ("change", "editing files · b.py", 10.0, 3, None, False)])
        self.assertEqual([x["tools"] for x in s[:2]], [{"ToolSearch": 1}, {"Bash git ls-files": 1, "Read": 1}])
        self.assertEqual((s[0]["thinking"], s[0]["text"], s[1]["thinking"], s[1]["text"]),
                         (0, "", 1, "look at the repo"))
        self.assertEqual((s[2]["tested"], s[3]["tested"]), (None, "pass"))
        self.assertAlmostEqual(sum(x["cw"] for x in s), sum(t["cw"] for t in turns))
        # incremental-stable, labels locked: every prefix (and a turn whose calls arrive one by one) agrees
        live = sp.Labeller()
        live.finalize(no_model=True)
        for n in range(1, len(turns) + 1):
            for m in range(1, len(turns[n - 1]["tools"]) + 1):
                prefix = [dict(t) for t in turns[:n - 1]] + [dict(turns[n - 1], tools=turns[n - 1]["tools"][:m])]
                part = sp.segment(prefix, events)
                live.assign(part)
                for a, b in zip(part[:-1], s):
                    self.assertEqual((a["turn_first"], a["part_first"], a["turn_last"], a["part_last"], a["cat"]),
                                     (b["turn_first"], b["part_first"], b["turn_last"], b["part_last"], b["cat"]))
        self.assertEqual([x["label"] for x in s[:4]], [live.locked[i][0] for i in range(4)])
        # parts follow call order, so a ToolSearch call arriving after other calls of its turn appends a step
        late = sp.segment([T(read("a.py"), ts, bash("git ls-files"), cw=30.0)])
        self.assertEqual([(x["cat"], x["cw"], x["part_first"]) for x in late],
                         [("investigate", 10.0, 0), ("overhead", 10.0, 1), ("investigate", 10.0, 2)])

    def test_locking(self):
        lab = sp.Labeller()
        turns = [T(read("a.py")), T(edit("a.py")), T(read("b.py"))]
        s = sp.segment(turns)
        lab.assign(s)
        self.assertEqual([x["locked"] for x in s], [False, False, False])   # pending model answer / open
        self.assertEqual(s[0]["label"], "reading code · a.py")
        self.assertEqual([x["i"] for x in lab.pending(s)], [0, 1])
        lab.add_model_labels({0: "Read the store code"}, usage={"calls": 1, "tokens": 100, "usd": 0.001})
        lab.assign(s)
        self.assertEqual((s[0]["label"], s[0]["source"], s[0]["locked"]), ("Read the store code", "haiku", True))
        lab.add_model_labels({0: "Something else entirely", 1: "Edit the store"})
        s = sp.segment(turns + [T(edit("c.py"))])
        lab.assign(s)
        self.assertEqual(s[0]["label"], "Read the store code")
        self.assertEqual(s[1]["label"], "Edit the store")
        lab.finalize(s)
        self.assertEqual((s[2]["source"], s[2]["locked"]), ("rule", True))
        lab.add_model_labels({2: "Late label"})
        lab.assign(sp.segment(turns + [T(edit("c.py"))]))
        self.assertEqual(lab.locked[2][1], "rule")
        self.assertEqual(lab.stats["haiku_calls"], 1)

    def test_model_group_validation(self):
        turns = [T(read("a.py")), T(edit("a.py")), T(read("b.py")), T(edit("b.py")), T(read("c.py"))]
        s = sp.segment(turns, [{"kind": "prompt", "at": 3}])
        gs = sp.groups(s, [{"label": "Too short", "first": 0, "last": 0},
                           {"label": "Crosses prompt", "first": 2, "last": 3},
                           {"label": "Open step", "first": 3, "last": 4},
                           {"label": "Out of range", "first": 3, "last": 9},
                           {"label": "Good one", "first": 0, "last": 2},
                           {"label": "Overlaps", "first": 1, "last": 2}])
        self.assertEqual([(g["label"], g["source"], g["first"], g["last"]) for g in gs], [("Good one", "haiku", 0, 2)])
        lab = sp.Labeller()
        lab.add_model_labels({}, [{"label": "Good one", "first": 0, "last": 2}])
        lab.assign(s)
        lab.add_model_labels({}, [{"label": "Renamed", "first": 0, "last": 1}])
        self.assertEqual([g["label"] for g in lab.assign(s)], ["Good one"])   # a locked group is never renamed


class FakeRun:
    def __init__(self, result, rc=0):
        self.result, self.rc, self.calls = result, rc, []

    def __call__(self, argv, **kw):
        self.calls.append((argv, kw))
        out = json.dumps({"result": self.result, "usage": {"input_tokens": 300, "output_tokens": 40},
                          "total_cost_usd": 0.0005})
        return subprocess.CompletedProcess(argv, self.rc, stdout=out, stderr="")


class StepNames(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.cache = os.path.join(self.dir, "step-labels.json")

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def steps(self):
        return sp.segment([T(read("a.py"), text="Let me look at /home/me/secret/a.py for Acme"), T(edit("a.py"))],
                          open_last=False)

    def test_label_batch_and_cache(self):
        run = FakeRun('```json\n{"labels": {"0": "Read the code", "1": "Edit it", "9": "x"}, '
                      '"groups": [{"label": "Fix it", "first": 0, "last": 1}]}\n```')
        r = stepnames.label_batch(self.steps(), goal="Fix tax", runner=run, cache=self.cache)
        self.assertEqual(r["labels"], {0: "Read the code", 1: "Edit it"})
        self.assertEqual(r["groups"], [{"label": "Fix it", "first": 0, "last": 1}])
        self.assertEqual(r["usage"]["calls"], 1)
        self.assertEqual(r["usage"]["tokens"], 340)
        argv, kw = run.calls[0]
        self.assertIn("haiku", argv)
        self.assertTrue(kw["cwd"].endswith("goal-naming"))
        self.assertNotIn("/home/me/secret", argv[-1])
        self.assertNotIn("Acme", argv[-1])
        r2 = stepnames.label_batch(self.steps(), goal="Fix tax", runner=run, cache=self.cache)
        self.assertEqual(len(run.calls), 1)   # cache hit
        self.assertTrue(r2["usage"]["cached"])
        self.assertEqual(r2["labels"], r["labels"])

    def test_bad_json_never_raises(self):
        for run in (FakeRun("not json at all"), FakeRun("{bad"), FakeRun("", rc=1)):
            r = stepnames.label_batch(self.steps(), runner=run, cache=self.cache)
            self.assertEqual((r["labels"], r["groups"]), ({}, []))

        def boom(*a, **k):
            raise OSError("no claude")
        self.assertEqual(stepnames.label_batch(self.steps(), runner=boom, cache=self.cache)["labels"], {})


class IntentMCP(unittest.TestCase):
    def test_round_trip(self):
        d = tempfile.mkdtemp()
        try:
            log = os.path.join(d, "declared.jsonl")
            msgs = [{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}},
                    {"jsonrpc": "2.0", "method": "notifications/initialized"},
                    {"jsonrpc": "2.0", "id": 2, "method": "ping"},
                    {"jsonrpc": "2.0", "id": 3, "method": "tools/list"},
                    {"jsonrpc": "2.0", "id": 4, "method": "tools/call",
                     "params": {"name": "declare_intent", "arguments": {"label": "Fix tax", "kind": "change"}}},
                    {"jsonrpc": "2.0", "id": 5, "method": "nope"}]
            p = subprocess.run([sys.executable, os.path.join(ROOT, "tokenscope", "live", "intent_mcp.py")],
                               input="".join(json.dumps(m) + "\n" for m in msgs) + "garbage\n",
                               capture_output=True, text=True, timeout=20,
                               env=dict(os.environ, TOKENSCOPE_INTENT_LOG=log), cwd=d)
            out = [json.loads(x) for x in p.stdout.splitlines()]
            self.assertEqual([o.get("id") for o in out], [1, 2, 3, 4, 5, None])
            self.assertEqual(out[0]["result"]["protocolVersion"], "2025-06-18")
            self.assertEqual(out[2]["result"]["tools"][0]["name"], "declare_intent")
            self.assertEqual(out[3]["result"]["content"][0]["text"], "noted")
            self.assertEqual(out[4]["error"]["code"], -32601)
            self.assertEqual(out[5]["error"]["code"], -32700)
            with open(log) as f:
                self.assertEqual(json.loads(f.readline())["label"], "Fix tax")
        finally:
            shutil.rmtree(d, ignore_errors=True)

    def test_install_text(self):
        t = intent_mcp.install_text()
        self.assertIn("claude mcp add", t)
        self.assertIn(os.path.abspath(intent_mcp.__file__), t)
        self.assertIn("declare_intent", t)


SID = "bbbbbbbb-1111-2222-3333-444444444444"


def rec_user(i, text):
    return {"type": "user", "timestamp": "2026-10-11T10:00:%02d.000Z" % i, "sessionId": SID, "cwd": "/srv/app",
            "message": {"role": "user", "content": text}}


def rec_asst(i, mid, blocks, stop=None):
    return {"type": "assistant", "timestamp": "2026-10-11T10:00:%02d.000Z" % i, "sessionId": SID,
            "message": {"id": mid, "model": "claude-sonnet-4-5", "content": blocks, "stop_reason": stop,
                        "usage": {"input_tokens": 10, "cache_creation_input_tokens": 100,
                                  "cache_read_input_tokens": 1000, "output_tokens": 20}}}


def rec_result(i, tid, text="ok"):
    return {"type": "user", "timestamp": "2026-10-11T10:00:%02d.000Z" % i, "sessionId": SID,
            "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": tid, "content": text}]},
            "toolUseResult": {"stdout": text}}


def tu(tid, name, inp):
    return {"type": "tool_use", "id": tid, "name": name, "input": inp}


class LivePayload(unittest.TestCase):
    def records(self):
        return [rec_user(0, "please fix the tax rounding in cart.py"),
                rec_asst(1, "m1", [{"type": "text", "text": "Declaring."},
                                   tu("d1", "mcp__tokenscope-intents__declare_intent", {"label": "Fix tax rounding"})]),
                rec_result(2, "d1", "noted"),
                rec_asst(3, "m2", [{"type": "thinking", "thinking": "look"}, tu("r1", "Read", {"file_path": "/srv/app/cart.py"})]),
                rec_result(4, "r1"),
                rec_asst(5, "m3", [tu("e1", "Edit", {"file_path": "/srv/app/cart.py"})]),
                rec_result(6, "e1"),
                rec_asst(7, "m4", [tu("b1", "Bash", {"command": "python3 -m unittest -q"})]),
                rec_result(8, "b1", "Ran 3 tests in 0.1s\n\nOK"),
                rec_asst(9, "m5", [{"type": "text", "text": "Done."}], stop="end_turn")]

    def ingest(self, redact):
        state = st.LiveState(redact=redact, clock=lambda: 1_790_000_000.0)
        tf = TrackedFile("/x/%s.jsonl" % SID, SID, "-srv-app")
        s = state.ingest(tf, self.records())
        return state, s

    def test_compact_intents(self):
        state, s = self.ingest(redact=False)
        it = state.session_compact(s)["intents"]
        self.assertEqual([x["cat"] for x in it["steps"]], ["investigate", "change", "verify"])
        self.assertEqual([x["source"] for x in it["steps"]], ["rule", "rule", "rule"])   # the group has the label
        self.assertEqual(it["groups"][0]["label"], "Fix tax rounding")
        self.assertTrue(all(x["locked"] and not x["open"] for x in it["steps"]))   # idle session: all closed
        self.assertEqual(it["steps"][2]["tested"], "pass")
        self.assertEqual(it["declared"], {"calls": 1, "steps": 3})
        self.assertEqual(len(it["groups"]), 1)
        self.assertEqual(it["groups"][0]["steps"], 3)
        self.assertEqual([x["group"] for x in it["steps"]], [0, 0, 0])
        self.assertEqual(it["labeller"]["pending"], 0)
        self.assertIn("1 thinking", it["steps"][0]["summary"])
        json.dumps(it)

    def test_workflow_agents_cost_in_launching_step(self):
        def wf_result(i, tid, run_id):
            r = rec_result(i, tid, "launched")
            r["toolUseResult"] = {"status": "async_launched", "runId": run_id} if run_id else {"status": "async_launched"}
            return r

        def agent_recs(i, mid):
            return [rec_asst(i, mid, [tu(mid + "t", "Read", {"file_path": "/srv/app/x.py"})]), rec_result(i + 1, mid + "t")]

        main = [rec_user(0, "audit the cart"),
                rec_asst(1, "m1", [tu("r1", "Read", {"file_path": "/srv/app/cart.py"})]), rec_result(2, "r1"),
                rec_asst(3, "m2", [tu("w1", "Workflow", {"script": "...", "description": "audit"})]),
                wf_result(4, "w1", "wf_aaa"),
                rec_asst(10, "m3", [tu("e1", "Edit", {"file_path": "/srv/app/cart.py"})]), rec_result(11, "e1"),
                rec_asst(20, "m4", [tu("w2", "Workflow", {"script": "...", "description": "verify"})]),
                wf_result(21, "w2", None),   # no runId: matched by time
                rec_asst(40, "m5", [{"type": "text", "text": "Done."}], stop="end_turn")]
        state = st.LiveState(redact=False, clock=lambda: 1_790_000_000.0)
        state.ingest(TrackedFile("/x/p/%s.jsonl" % SID, SID, "p"), main)
        sub = "/x/p/%s/subagents/workflows" % SID
        for path, aid, recs in [(sub + "/wf_aaa/agent-a1.jsonl", "a1", agent_recs(5, "x1")),
                                (sub + "/wf_aaa/agent-a2.jsonl", "a2", agent_recs(6, "x2")),
                                (sub + "/wf_bbb/agent-b1.jsonl", "b1", agent_recs(22, "y1"))]:
            s = state.ingest(TrackedFile(path, SID, "p", aid), recs)
        steps, gs = state.steps(s, final=True)
        one = s.agents["a1"]["cw"]
        self.assertGreater(one, 0)
        wf1 = next(x for x in steps if "Workflow" in x["tools"] and x["turn_first"] == 1)
        wf2 = next(x for x in steps if "Workflow" in x["tools"] and x["turn_first"] == 3)
        self.assertEqual((wf1["wf_agents"], wf2["wf_agents"]), (2, 1))
        main_cw = sum(m["cw"] for m in s.main.msgs.values() if m["step"] is not None)
        self.assertAlmostEqual(sum(x["cw"] for x in steps), main_cw + 3 * one, places=0)
        self.assertEqual(sp.summary(wf1), "Workflow ×1 (2 agents)")
        self.assertEqual(sp.summary(wf2), "Workflow ×1 (1 agent)")
        self.assertIn("Workflow ×1 (2 agents)", [x["summary"] for x in state.intents(s)["steps"]])

    def test_open_step_ops(self):
        recs = self.records()[:8] + [rec_asst(9, "m5", [tu("e2", "Edit", {"file_path": "/srv/app/cart.py"}),
                                                     tu("b2", "Bash", {"command": "python3 -m unittest -q"})]),
                                     rec_result(10, "e2"), rec_result(11, "b2", "Ran 3 tests in 0.1s\n\nFAILED (failures=1)")]
        for redact in (False, True):
            state = st.LiveState(redact=redact, clock=lambda: 1_790_000_000.0)
            s = state.ingest(TrackedFile("/x/%s.jsonl" % SID, SID, "-srv-app"), recs)
            state.clock = lambda: s.last_ts + 1   # still working
            it = state.intents(s)
            *closed, live = it["steps"]
            self.assertFalse(live["open"])   # its test run closed it, but the session works on: still the live one
            self.assertTrue(all("ops" not in x for x in closed))
            ops = live["ops"]
            self.assertEqual([o["tool"] for o in ops], ["Edit", "Bash unittest"])
            self.assertEqual(ops[0]["target"], ".py" if redact else "cart.py")
            self.assertNotIn("test", ops[0])
            self.assertEqual((ops[1]["test"]["ok"], ops[1]["test"]["failed"]), (False, 1))
            self.assertEqual([o["err"] for o in ops], [False, False])
            self.assertAlmostEqual(ops[0]["cw"] + ops[1]["cw"], s.main.turn_steps[-1]["cw"], places=0)
            json.dumps(it)
            # all_ops (the web page's ?ops=all): the closed steps carry their calls too, the live one the same
            full = state.intents(s, all_ops=True)
            self.assertTrue(all("ops" in x for x in full["steps"]))
            self.assertEqual(full["steps"][-1]["ops"], ops)
            self.assertEqual(sum(len(x["ops"]) for x in full["steps"]),
                             sum(1 for t in s.main.turn_steps for x in t["tools"] if not sp.is_declare(x["name"])))
        # a call without its result yet is running: err None
        state = st.LiveState(redact=False, clock=lambda: 1_790_000_000.0)
        s = state.ingest(TrackedFile("/x/%s.jsonl" % SID, SID, "-srv-app"), recs[:9])
        state.clock = lambda: s.last_ts + 1
        self.assertIsNone(state.intents(s)["steps"][-1]["ops"][-1]["err"])

    def test_ops_of_a_step_after_toolsearch_in_the_same_turn(self):
        recs = self.records()[:2] + [
            rec_result(2, "d1", "noted"),
            rec_asst(3, "m2", [tu("t1", "ToolSearch", {"query": "select:WebFetch"}),
                               tu("g1", "Bash", {"command": "git ls-files"}),
                               tu("r1", "Read", {"file_path": "/srv/app/cart.py"})]),
            rec_result(4, "t1", "loaded"), rec_result(5, "g1"), rec_result(6, "r1")]
        state = st.LiveState(redact=False, clock=lambda: 1_790_000_000.0)
        s = state.ingest(TrackedFile("/x/%s.jsonl" % SID, SID, "-srv-app"), recs)
        state.clock = lambda: s.last_ts + 1   # still working
        it = state.intents(s)
        self.assertEqual([(x["cat"], x["label"], x["open"]) for x in it["steps"][-2:]],
                         [("overhead", "Load WebFetch tool", False), ("investigate", "checking git state · cart.py", True)])
        self.assertNotIn("ops", it["steps"][-2])
        ops = it["steps"][-1]["ops"]
        self.assertEqual([o["tool"] for o in ops], ["Bash git ls-files", "Read"])
        turn_cw = s.main.turn_steps[-1]["cw"]
        self.assertAlmostEqual(sum(o["cw"] for o in ops), turn_cw * 2 / 3, places=0)
        self.assertAlmostEqual(it["steps"][-2]["cw"], turn_cw / 3, places=0)

    def test_ops_leave_out_declare_intent(self):
        decl = "mcp__tokenscope-intents__declare_intent"
        recs = self.records()[:3] + [
            rec_asst(3, "m2", [tu("d2", decl, {"label": "Fix the cart"}),
                               tu("e2", "Edit", {"file_path": "/srv/app/cart.py"}),
                               tu("r2", "Read", {"file_path": "/srv/app/tax.py"})]),
            rec_result(4, "d2", "noted"), rec_result(5, "e2"), rec_result(6, "r2"),
            rec_asst(7, "m3", [tu("d3", decl, {"label": "Fix the tax"})])]
        for n, want in ((7, ["Edit", "Read"]), (8, [])):   # a mixed turn; then a turn with only a declare call
            state = st.LiveState(redact=False, clock=lambda: 1_790_000_000.0)
            s = state.ingest(TrackedFile("/x/%s.jsonl" % SID, SID, "-srv-app"), recs[:n])
            state.clock = lambda: s.last_ts + 1   # still working
            it = state.intents(s)
            ops = it["steps"][-1]["ops"]
            self.assertEqual([o["tool"] for o in ops], want)
            if ops:   # the declare call's CW share goes to the turn's other calls
                self.assertAlmostEqual(sum(o["cw"] for o in ops), s.main.turn_steps[1]["cw"], places=0)
            self.assertNotIn("declare", json.dumps([x["summary"] for x in it["steps"]]))

    def test_workflow_always_in_summary(self):
        s = {"tools": {"Read": 5, "Grep": 4, "Edit": 3, "Bash": 2, "Workflow": 1}, "wf_agents": 12}
        self.assertEqual(sp.summary(s), "Read ×5 · Grep ×4 · Edit ×3 · Workflow ×1 (12 agents)")

    def test_compact_redacted(self):
        state, s = self.ingest(redact=True)
        it = state.session_compact(s)["intents"]
        self.assertNotIn("cart.py", json.dumps(it))

    def test_label_requests(self):
        state = st.LiveState(redact=False, clock=lambda: 1_790_000_000.0, model_labels=True)
        recs = [r for r in self.records() if "declare_intent" not in json.dumps(r)]
        s = state.ingest(TrackedFile("/x/%s.jsonl" % SID, SID, "-srv-app"), recs)
        state.clock = lambda: s.last_ts + 5   # waiting for the user
        reqs = state.label_requests()
        self.assertEqual(len(reqs), 1)
        sid, batch, goal = reqs[0]
        self.assertEqual([x["i"] for x in batch], [0, 1, 2])
        self.assertEqual(state.label_requests(), [])   # in flight
        state.add_step_labels(sid, [0, 1, 2], {"labels": {0: "Read the cart code"}, "groups": [],
                                               "usage": {"calls": 1, "tokens": 50, "usd": 0.0001}})
        it = state.intents(s)
        self.assertEqual((it["steps"][0]["label"], it["steps"][0]["source"]), ("Read the cart code", "haiku"))
        self.assertEqual(it["labeller"]["haiku_calls"], 1)


class CLI(unittest.TestCase):
    def test_intents_cli(self):
        d = tempfile.mkdtemp()
        try:
            proj = os.path.join(d, "-srv-app")
            os.makedirs(proj)
            with open(os.path.join(proj, SID + ".jsonl"), "w") as f:
                f.write("".join(json.dumps(r) + "\n" for r in LivePayload().records()))
            p = subprocess.run([sys.executable, "-m", "tokenscope", "intents", SID[:8], "--root", d, "--json"],
                               capture_output=True, text=True, cwd=ROOT, timeout=60)
            self.assertEqual(p.returncode, 0, p.stderr)
            out = json.loads(p.stdout)
            self.assertEqual(len(out["steps"]), 3)
            self.assertEqual(out["groups"][0]["label"], "Fix tax rounding")
            p = subprocess.run([sys.executable, "-m", "tokenscope", "intents", SID, "--root", d],
                               capture_output=True, text=True, cwd=ROOT, timeout=60)
            self.assertIn("Fix tax rounding", p.stdout)
            md = os.path.join(d, "CLAUDE.md")
            p = subprocess.run([sys.executable, "-m", "tokenscope", "live", "intents-install", "--write-claude-md", md],
                               capture_output=True, text=True, cwd=ROOT, timeout=60)
            self.assertIn("claude mcp add", p.stdout)
            with open(md) as f:
                self.assertIn("declare_intent", f.read())
        finally:
            shutil.rmtree(d, ignore_errors=True)


class TestIntentBlocks(unittest.TestCase):
    """steps.intent_blocks: what the flow views list (every step under an intent, never loose)."""

    def test_groups_pending_and_settled_steps(self):
        def x(i, locked=True, cat="change", label=None):
            return {"i": i, "cat": cat, "label": label or "step %d" % i, "source": "rule", "locked": locked,
                    "cw": 10.0, "usd": 0.5, "ts0": 100.0 + i, "ts1": 100.5 + i, "skills": []}
        steps = [x(0), x(1), x(2), x(3), x(4), x(5, locked=False), x(6, locked=False, cat="verify")]
        gs = [{"label": "Fix the tax", "source": "todo", "first": 1, "last": 3, "cat": "change", "cw": 30.0,
               "usd": 1.5, "steps": 3, "dur_s": 2.5, "skills": []}]
        got = sp.intent_blocks(steps, gs)
        self.assertEqual([(b["kind"], b["first"], b["last"], b["steps"], b["label"]) for b in got],
                         [("step", 0, 0, 1, "step 0"), ("group", 1, 3, 3, "Fix the tax"), ("step", 4, 4, 1, "step 4"),
                          ("pending", 5, 6, 2, sp.PENDING_LABEL)])
        self.assertEqual(got[1]["group"], 0)
        self.assertEqual((got[3]["cat"], got[3]["usd"], got[3]["source"]), ("verify", 1.0, None))   # its latest step
        # pending runs join up to the next group or settled step; nothing is left out
        steps[0]["locked"] = steps[4]["locked"] = False
        got = sp.intent_blocks(steps, gs)
        self.assertEqual([(b["kind"], b["first"], b["last"]) for b in got],
                         [("pending", 0, 0), ("group", 1, 3), ("pending", 4, 6)])
        self.assertEqual(sum(b["steps"] for b in got), len(steps))
        self.assertEqual(sp.intent_blocks([], []), [])


if __name__ == "__main__":
    unittest.main()
