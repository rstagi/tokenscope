"""OpenAI Codex rollouts in tokenscope live: tailing and ingestion (growing files, truncation, subagents), replay,
the server page, the cross-session index, redaction of everything the server exposes, alerts on synthetic Codex
sessions (tests/codex_synth.py), and malformed input.

Hermetic: roots are temp copies of tests/fixtures/codex or synthetic rollouts; no test reads HOME, ~/.codex or
~/.claude; the parse cache is off and the alert history stays in memory."""
import copy
import io
import json
import os
import re
import shutil
import sys
import tempfile
import threading
import unittest
import urllib.request
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from tokenscope import alerts as A, cli, parse, xindex  # noqa: E402
from tokenscope.analyze import analyze_session  # noqa: E402
from tokenscope.live import server  # noqa: E402
from tokenscope.live.replay import LiveReplay, replay, transcript_files  # noqa: E402
from tokenscope.live.state import LiveState  # noqa: E402
from tokenscope.live.tail import Tailer  # noqa: E402
import alerts_synth  # noqa: E402
import codex_synth as cs  # noqa: E402
from codex_synth import RolloutWriter, exec_command, mcp, patch, plan, read, shell, spawn, web_search  # noqa: E402

FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "codex")
ROOT = os.path.join(FIXTURES, "sessions")
MAIN, SUB, LEGACY = cs.sid(1), cs.sid(2), cs.sid(3)
DAY = os.path.join("2026", "10", "20")
MODERN_REL = os.path.join(DAY, "rollout-2026-10-20T09-00-00-%s.jsonl" % MAIN)
SUB_REL = os.path.join(DAY, "rollout-2026-10-20T09-00-31-%s.jsonl" % SUB)
BAD_PATH = os.path.join(FIXTURES, "malformed", "2026", "10", "21",
                        "rollout-2026-10-21T10-00-00-%s.jsonl" % cs.sid(4))
FOREVER = 10 ** 7   # Tailer(since_minutes=...): track the fixture files whatever their age
_CACHE = {}
_ENV = {}


def setUpModule():
    """No parse cache (and a temp cache dir if a CLI command turns it on); harness / root variables pinned to an
    empty temp root, so a call without an explicit root (auto-detection, `alerts <path>`) never looks at the home."""
    _CACHE.update(parse.CACHE)
    _ENV["root"], _ENV["cache"] = tempfile.mkdtemp(), tempfile.mkdtemp()
    parse.set_cache(False, _ENV["cache"])
    _ENV["patch"] = mock.patch.dict(os.environ, {"TOKENSCOPE_ROOT": _ENV["root"]})
    _ENV["patch"].start()
    for k in ("TOKENSCOPE_HARNESS", "CODEX_HOME"):
        os.environ.pop(k, None)


def tearDownModule():
    parse.CACHE.update(_CACHE)
    _ENV["patch"].stop()
    shutil.rmtree(_ENV["root"], ignore_errors=True)
    shutil.rmtree(_ENV["cache"], ignore_errors=True)


def cfg(**over):
    c = copy.deepcopy(A.DEFAULTS)
    for k, v in over.items():
        A.set_key(c, k, v)
    return c


def batch_totals(root, sid):
    """(calls, usd) of a session through the batch path (discover + load + analyze)."""
    s = next(x for x in parse.discover(root, harness="codex") if x.id == sid).load()
    t = analyze_session(s)["totals"]["all"]
    return t["calls"], round(t["api_cost_usd"], 4)


def live_totals(state, s):
    t = state.totals(s)
    return t["turns"], round(t["usd"], 4)


class TempRoot(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)


# ---------------------------------------------------------------- ingestion

class TestLiveIngestion(TempRoot):
    def feed(self, state, tailer):
        for tf, recs, reset in tailer.poll():
            state.ingest(tf, recs, reset)

    def test_growing_files_in_chunks(self):
        """Both rollouts written a few bytes at a time (lines cut anywhere): the live totals end equal to batch."""
        srcs = [(os.path.join(ROOT, rel), os.path.join(self.root, rel)) for rel in (MODERN_REL, SUB_REL)]
        os.makedirs(os.path.join(self.root, DAY))
        data = []
        for src, dst in srcs:
            with open(src, "rb") as f:
                data.append(f.read())
            open(dst, "wb").close()
        state, tailer = LiveState(redact=True), Tailer(self.root, since_minutes=FOREVER)
        pos, step = [0, 0], 97
        while pos[0] < len(data[0]) or pos[1] < len(data[1]):
            for k, (_, dst) in enumerate(srcs):
                if pos[k] < len(data[k]):
                    with open(dst, "ab") as f:
                        f.write(data[k][pos[k]:pos[k] + step])
                    pos[k] += step
            self.feed(state, tailer)
        self.assertEqual(tailer.malformed, 0)   # a partial last line waits for the rest
        self.assertEqual(list(state.sessions), [MAIN])   # the subagent is grouped under its parent
        s = state.sessions[MAIN]
        self.assertEqual(list(s.agents), [SUB])
        self.assertEqual(live_totals(state, s), batch_totals(self.root, MAIN))
        self.assertEqual(live_totals(state, s), (8, 0.0587))
        b = state.board(s.last_ts + 5)
        self.assertEqual((b["harness"], b["harness_label"], len(b["sessions"])), ("codex", "OpenAI Codex", 1))
        self.assertEqual(state.tree_cost(s)[SUB][0], 3650.0)   # forked history not counted

    def test_reset_on_truncation(self):
        os.makedirs(os.path.join(self.root, DAY))
        dst = os.path.join(self.root, MODERN_REL)
        with open(os.path.join(ROOT, MODERN_REL), "rb") as f:
            data = f.read()
        with open(dst, "wb") as f:
            f.write(data)
        state, tailer = LiveState(redact=True), Tailer(self.root, since_minutes=FOREVER)
        self.feed(state, tailer)
        full = live_totals(state, state.sessions[MAIN])
        with open(dst, "wb") as f:   # rotated: shorter, then it grows again
            f.write(data[:len(data) // 2].rsplit(b"\n", 1)[0] + b"\n")
        got = tailer.poll()
        self.assertEqual([r for _, _, r in got], [True])
        for tf, recs, reset in got:
            state.ingest(tf, recs, reset)
        self.assertLess(live_totals(state, state.sessions[MAIN])[0], full[0])
        with open(dst, "wb") as f:
            f.write(data)
        self.feed(state, tailer)
        self.assertEqual(live_totals(state, state.sessions[MAIN]), full)   # a fresh converter: nothing doubled

    def test_replay_and_transcript_files(self):
        path = os.path.join(ROOT, MODERN_REL)
        files, root = transcript_files(path)
        self.assertEqual(root, os.path.abspath(ROOT))
        self.assertEqual([(os.path.basename(f.path), f.session_id, f.project_dir, f.agent_id) for f in files],
                         [(os.path.basename(MODERN_REL), MAIN, "project", None),
                          (os.path.basename(SUB_REL), MAIN, "project", SUB)])
        state, s = replay(path, redact=True)
        self.assertEqual((s.id, live_totals(state, s)), (MAIN, (8, 0.0587)))
        clock = [0.0]
        r = LiveReplay(path, redact=True, clock=lambda: clock[0])
        clock[0] = 1000.0
        r.advance()
        self.assertTrue(r.done())
        self.assertEqual(live_totals(r.state, r.session()), (8, 0.0587))
        self.assertEqual(r.flow()["intents"] is not None, True)

    def test_server_board_page(self):
        live = server.Live(ROOT, harness="codex", redact=True, since_minutes=FOREVER, hooks=False, xhints=False)
        self.assertEqual((live.harness, live.harness_label), ("codex", "OpenAI Codex"))
        srv = server.make_server(live, "127.0.0.1", 0)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            base = "http://127.0.0.1:%d" % srv.server_address[1]
            with urllib.request.urlopen(base + "/") as resp:
                html = resp.read().decode("utf-8")
            self.assertIn("OpenAI Codex sessions on this machine", html)
            self.assertNotIn("Claude Code sessions on this machine", html)
            live.poll_once()   # the fixture sessions are old: tracked, but idle for the board
            self.assertEqual(sorted(live.state.sessions), [MAIN, LEGACY])
            with urllib.request.urlopen(base + "/api/board") as resp:
                self.assertEqual(json.loads(resp.read().decode("utf-8"))["harness"], "codex")
            with urllib.request.urlopen(base + "/api/session/%s/compact" % MAIN) as resp:
                self.assertEqual(json.loads(resp.read().decode("utf-8"))["turns"], 8)
        finally:
            srv.shutdown()
            srv.server_close()

    def test_xindex_on_a_codex_root(self):
        cache = os.path.join(self.root, "cache")
        stats = xindex.XIndex(ROOT, cache=cache).build(jobs=1)
        self.assertEqual((stats["transcripts"], stats["sessions"]), (2, 2))   # the subagent rollout is not its own
        x = xindex.XIndex(ROOT, cache=cache)
        self.assertEqual(x.build(jobs=1)["replayed"], 0)
        self.assertEqual(sorted(x.sessions), [MAIN, LEGACY])


class TestMalformedLive(TempRoot):
    def test_tailer_and_state_never_raise(self):
        d = os.path.join(self.root, "2026", "10", "21")
        shutil.copytree(os.path.dirname(BAD_PATH), d)
        state, tailer = LiveState(redact=True), Tailer(self.root, since_minutes=FOREVER)
        for _ in range(2):
            for tf, recs, reset in tailer.poll():
                state.ingest(tf, recs, reset)
        self.assertGreaterEqual(tailer.malformed, 2)   # the garbage and the truncated line (+ binary lines)
        s = state.sessions[cs.sid(4)]
        steps, _ = state.steps(s, final=True)
        self.assertTrue(steps)
        self.assertEqual(state.totals(s)["input"], 1000)
        json.dumps([state.board(s.last_ts + 5), state.session_full(s), state.session_flow(s)])


# ---------------------------------------------------------------- redaction

# Never visible in a redacted payload: free text the converter must drop, and secrets inside commands.
SECRETS = ["user@example.com", "203.0.113.7", "sk-FAKE0000000000000000000000", "ghp_FAKE0000000000000000",
           "db-internal.corp.example", "secretmod", "quokkaprompt", "zeppelintask", "marmotmessage",
           "narwhalthought", "wombatexplain", "pangolintext", "lemurresult"]
# Visible in some redacted payload fields for Claude sessions too (shared code, not the Codex converter): the
# script name (Bash label / target), the edited .patch file name (step label), the plan step (TodoWrite content:
# intent label), the private MCP server (session_full tool name) and a distinctive prompt word (keyword goal name).
# Checked: never in more fields than for the equivalent Claude transcript.
SHARED = ["deploy_prod_secret", "zanzibar_hotfix", "ocelotplan", "acmecorp_internal", "pangolinprompt"]


def secret_session():
    w = RolloutWriter(cs.sid(101), t0=cs.DAY0)
    w.prompt("Please fix the build PangolinPrompt")
    w.turn([shell("curl -s https://db-internal.corp.example/health -H 'Authorization: sk-FAKE0000000000000000000000'",
                  "ok lemurresult")], reasoning="NarwhalThought-summary here", text="pangolintext looking")
    w.turn([shell("ssh user@example.com@203.0.113.7 uptime", "up")])
    w.turn([shell("git push https://ghp_FAKE0000000000000000@example.com/x.git", "done")])
    w.turn([shell("python3 -m secretmod.runner --flag", "fine")])
    w.turn([exec_command("./deploy_prod_secret.sh --now", "boom")])
    w.turn([shell("git apply zanzibar_hotfix.patch", "applied")])
    w.turn([plan(["ocelotplan step one", "second ocelotplan"], explanation="wombatexplain why")])
    w.turn([spawn("QuokkaPrompt investigate the thing", "zeppelintask_probe-run")])
    w.turn([mcp("mcp__acmecorp_internal__lookup", {"q": "marmotmessage"})])
    w.turn([({"type": "function_call", "name": "send_message",
              "arguments": json.dumps({"message": "MarmotMessage hello"})}, "sent")])
    w.turn([web_search("weather")])
    w.turn([patch([("Update", "src/zanzibar_hotfix.patch"), ("Add", "src/app.py")])])
    w.end("pangolintext done")
    return w


def claude_twin(root):
    """The same work as a Claude Code transcript (alerts_synth.Writer): the baseline for SHARED."""
    w = alerts_synth.Writer("c0de0000-0000-4000-8000-000000000101", cs.DAY0)
    w.prompt("Please fix the build PangolinPrompt")
    w.turn([alerts_synth.bash("python3 -m secretmod.runner --flag", "fine")])
    w.turn([alerts_synth.bash("./deploy_prod_secret.sh --now", "boom")])
    w.turn([alerts_synth.bash("git apply zanzibar_hotfix.patch", "applied")])
    w.turn([alerts_synth.todo("ocelotplan step one")])
    w.turn([("mcp__acmecorp_internal__lookup", {"q": "x"}, "ok", False)])
    w.turn([alerts_synth.edit("src/zanzibar_hotfix.patch", "a", "b"), alerts_synth.edit("src/app.py", "a", "b")])
    w.end()
    return w.write(root, "-home-user-project")


def exposed(state, s):
    """Everything the live server exposes for one session (and the board / history around it)."""
    now = s.last_ts + 5
    return {
        "board": state.board(now), "follow": state.follow([s.id], now), "tile": state.follow_tile(s, now),
        "full": state.session_full(s, now), "since": state.session_since(s, 0, now),
        "compact": state.session_compact(s, now), "compact_all": state.session_compact(s, now, all_ops=True),
        "flow": state.session_flow(s, now), "alerts": state.alerts_public(s, now),
        "counts": state.alert_counts(state.alerts_public(s, now)), "history": state.alert_history(now=now),
        "recap": state.recap(s, now), "warnings": state.warnings_list(s),
        "intents": state.intents(s, now, all_ops=True), "agents": state.agents_summary(s),
    }


def hits(obj, words, path=""):
    """{(normalised field path, word)} of every string in obj that contains a word (case-insensitive)."""
    out = set()
    if isinstance(obj, dict):
        for k, v in obj.items():
            out |= hits(v, words, path + "." + str(k))
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            out |= hits(v, words, path + "[]")
    elif isinstance(obj, str):
        low = obj.lower()
        out |= {(path, w) for w in words if w.lower() in low}
    return out


def field(path):
    """A hit's field path with the recap slot generalised: which step is the costliest / slowest one depends on the
    numbers, not on what a label may show."""
    return re.sub(r"\.(costliest|slowest)\.", ".<recap step>.", path)


class TestRedaction(TempRoot):
    def setUp(self):
        super().setUp()
        self.path = secret_session().write(self.root)

    def test_nothing_private_in_redacted_payloads(self):
        state, s = replay(self.path, redact=True)
        payloads = exposed(state, s)
        blob = json.dumps(payloads, default=str)
        for word in SECRETS:
            self.assertNotIn(word.lower(), blob.lower(), word)
            for part in re.split(r"[-_.@]", word):   # nor a distinctive piece of a compound
                if len(part) >= 6 and part.lower() not in ("example", "internal"):
                    self.assertNotIn(part.lower(), blob.lower(), (word, part))
        self.assertEqual(hits(payloads, SECRETS), set())

    def test_shared_leaks_no_wider_than_claude(self):
        state, s = replay(self.path, redact=True)
        codex_fields = {(field(p), w) for p, w in hits(exposed(state, s), SHARED)}
        cstate, cs_ = replay(claude_twin(os.path.join(self.root, "claude")), redact=True)
        claude_fields = {(field(p), w) for p, w in hits(exposed(cstate, cs_), SHARED)}
        self.assertTrue(claude_fields)   # the baseline really shows them
        self.assertLessEqual(codex_fields, claude_fields, codex_fields - claude_fields)

    def test_control_unredacted_shows_commands(self):
        state, s = replay(self.path, redact=False)
        blob = json.dumps(exposed(state, s), default=str)
        self.assertIn("secretmod", blob)   # the test can see a leak when there is one
        self.assertIn("deploy_prod_secret", blob)
        for word in ("quokkaprompt", "zeppelintask", "narwhalthought", "wombatexplain", "marmotmessage"):
            self.assertNotIn(word, blob.lower())   # never in a tool input, even unredacted

    def test_cli_alerts_and_intents(self):
        out = io.StringIO()
        with redirect_stdout(out), redirect_stderr(io.StringIO()):
            self.assertEqual(cli.main(["alerts", self.path]), 0)
            self.assertEqual(cli.main(["alerts", self.path, "--json", "--off-task"]), 0)
        for word in SECRETS + SHARED:
            self.assertNotIn(word.lower(), out.getvalue().lower(), word)
        out = io.StringIO()
        with redirect_stdout(out):   # intents is a local, unredacted view: no converter-dropped free text in it
            self.assertEqual(cli.main(["intents", self.path]), 0)
            self.assertEqual(cli.main(["intents", self.path, "--json"]), 0)
        for word in ("quokkaprompt", "zeppelintask", "narwhalthought", "wombatexplain", "marmotmessage", "lemurresult"):
            self.assertNotIn(word, out.getvalue().lower(), word)


# ---------------------------------------------------------------- alerts

def normal_work(w, n, area="report"):
    for k in range(n):
        m = k % 3
        if m == 0:
            w.turn([read("src/%s/part%d.py" % (area, k))])
        elif m == 1:
            w.turn([patch([("Update", "src/%s/part%d.py" % (area, k - 1))])])
        else:
            w.turn([shell("python3 -m unittest discover -s tests", "Ran 4 tests\n\nOK")])


def scenario(name):
    """A synthetic Codex session that triggers one alert kind (or none: "clean")."""
    n = {"cost-jump": 201, "read-loop": 202, "retry-loop": 203, "off-task": 204, "cost-threshold": 205, "clean": 206}
    w = RolloutWriter(cs.sid(n[name]), t0=cs.DAY0 + 600 * n[name])
    if name == "cost-jump":
        w.prompt("add a CSV export to the monthly report page")
        normal_work(w, 9)
        w.turn([read("build/report_bundle.js", "x" * 4000)], cs.BIG)
        normal_work(w, 3)
    elif name == "read-loop":
        w.prompt("why does the scheduler skip jobs when the clock changes")
        for k in range(6):   # the same file read 6x with the same content, a search in between
            w.turn([read("src/sched/scheduler.py", "class Scheduler:\n    pass\n")])
            w.turn([exec_command("rg -n dst_%d src" % k, "")])
    elif name == "retry-loop":
        w.prompt("deploy the staging build")
        w.turn([read("web/package.json", "{}")])
        for _ in range(4):
            w.turn([shell("npm run build -- --mode staging", "error: Module not found\nBuild failed.", code=1)])
    elif name == "off-task":
        w.prompt("fix the tax rounding bug in the invoice totals")
        w.turn([plan(["Fix tax rounding in invoice totals"])])
        w.turn([read("src/invoice/totals.py")])
        w.turn([patch([("Update", "src/invoice/tax.py")])])
        w.turn([shell("python3 -m unittest discover -s tests", "OK")])
        w.turn([read("src/auth/session_store.py")])
        w.turn([patch([("Update", "src/auth/session_store.py")])])
        w.turn([read("src/metrics/exporter.py"), read("web/theme/colors.css")])
        w.turn([patch([("Update", "web/theme/colors.css")])])
    elif name == "cost-threshold":
        w.prompt("migrate the billing service to the new ledger API")
        for k in range(12):
            w.turn([read("src/billing/mod%d.py" % k)], (900000, 600000, 20000))
    else:
        w.prompt("make the discount code case-insensitive")
        w.turn([read("src/cart/discount.py")])
        w.turn([patch([("Update", "src/cart/discount.py")])])
        w.turn([shell("python3 -m unittest discover -s tests", "FAILED (failures=1)", code=1)])
        w.turn([patch([("Update", "src/cart/codes.py")])])
        w.turn([shell("python3 -m unittest discover -s tests", "OK")])
    w.end()
    return w


EXPECTED = {
    "cost-jump": {("cost", "jump")},
    "read-loop": {("looping", "repeat_read")},
    "retry-loop": {("looping", "retry")},
    "off-task": {("off_task", "unrelated")},
    "cost-threshold": {("cost", "session_usd"), ("cost", "intent_usd")},
    "clean": set(),
}
PRIVATE = ("/home/user", "project", "invoice", "session_store", "colors", "scheduler", "report_bundle", "billing",
           "npm", "staging", "discount", "tax.py", "sed", "unittest")


class TestAlerts(TempRoot):
    def test_each_session_fires_its_alerts(self):
        on = cfg(**{"off_task.enabled": True, "cost.session_usd": 8.0})
        for name, want in EXPECTED.items():
            path = scenario(name).write(self.root)
            state, s = replay(path, redact=True, alerts_config=on)
            got = state.alerts_public(s)
            self.assertEqual({(a["kind"], r) for a in got for t in a["triggers"] for r in t["rules"]}, want, name)
            blob = json.dumps([{k: v for k, v in a.items() if k != "intent"} for a in got])
            for bad in PRIVATE:
                self.assertIsNone(re.search(r"(?<![a-z])%s(?![a-z])" % re.escape(bad), blob), (name, bad))
        state, s = replay(scenario("off-task").write(self.root), redact=True)   # default config: off-task is off
        self.assertEqual(state.alerts_public(s), [])

    def test_messages(self):
        state, s = replay(scenario("retry-loop").write(self.root), redact=True)
        self.assertEqual(state.alerts_public(s)[0]["message"],
                         "same failing command retried 4× with no edit in between (command #1)")
        state, s = replay(scenario("read-loop").write(self.root), redact=True)
        self.assertEqual(state.alerts_public(s)[0]["message"],
                         "same .py file read 6× with the same result (file #1)")
        state, s = replay(scenario("cost-jump").write(self.root), redact=True)
        a = state.alerts_public(s)[0]
        self.assertEqual((a["rule"], a["severity"]), ("jump", "critical"))

    def test_cli_alerts(self):
        path = scenario("retry-loop").write(self.root)
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(cli.main(["alerts", cs.sid(203)[:12], "--root", self.root, "--harness", "codex"]), 0)
        self.assertIn("[warning] looping/retry", out.getvalue())
        self.assertNotIn("npm", out.getvalue())
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(cli.main(["alerts", path, "--json"]), 0)
        self.assertEqual(json.loads(out.getvalue())["session"], cs.sid(203))


if __name__ == "__main__":
    unittest.main()
