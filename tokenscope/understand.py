"""Understand layer: WHAT the agent did, before WHERE the tokens went.

A session is split into requests (one per real user prompt). Each request gets an ordered list of steps (tool uses
and the hooks that fired after them), a chain (the path the agent took), an outcome read from transcript signals, and
the files it edited and read. Requests are then grouped into goals discovered from the data (goals.py) with per-goal
aggregates, chain frequencies and flow data, and each API call's context is split by source for a stacked chart.

Token numbers per request are measured (API usage). Per-step tokens are the analyzer's attribution of that tool's
result (chars/4 estimates, scaled to the measured delta when the analyzer did so). Context-by-source splits are
estimates carried forward from the analyzer's attribution.
"""
import hashlib
import json
import os
import re
import shlex
import statistics
from collections import Counter, OrderedDict
from datetime import datetime, timezone

from . import goals as gl
from .analyze import analyze_session, est_tokens, mcp_parts, relpath
from .fixes import k
from .parse import SOURCE_RULES, is_automated, parse_ts, prompt_source, real_prompt_text
from .parse import SOURCES as REQUEST_SOURCES

FLOW_POSITIONS = 6
LOOP_REPEAT = 3             # same kind+target this many times in a row => looping
LOOP_FAIL_CYCLES = 3        # fail -> edit -> fail test cycles => looping
STATUS_FINISH_MIN = 0.70    # finish rate below => Failing
STATUS_LOOP_SHARE = 1 / 3   # share of runs flagged looping => Looping
STATUS_COST_X = 1.5         # median all-in per run vs median of all requests => Costly
STATUS_FIX_SHARE = 0.30     # fixes claim this share of the goal's tokens => Costly
IN_PROGRESS_SECONDS = 300   # last request of a session written this recently may still be running
RESULT_TEXT_TAIL = 6000     # chars of tool/hook output kept for test parsing

EDIT_TOOLS = ("Edit", "MultiEdit", "Write", "NotebookEdit")
SUBCOMMAND_PROGRAMS = ("npm", "yarn", "pnpm", "git", "node", "python", "python3", "pytest", "go", "cargo", "make")
WRAPPERS = ("sudo", "time", "env", "nice", "nohup", "command", "exec")
SCRIPT_EXT = (".sh", ".py", ".js", ".mjs", ".cjs", ".ts", ".rb", ".pl", ".bash")


# ---------------------------------------------------------------- test output parsing

_NODE_RE = re.compile(r"^\s*(?:ℹ|#)\s+(pass|fail)\s+(\d+)\s*$", re.M)
_JEST_RE = re.compile(r"^\s*Tests:?\s+(\d.+)$", re.M)
_PYTEST_LINE_RE = re.compile(r"^.*\b\d+ (?:passed|failed)\b.*\bin [\d.]+m?s\b.*$", re.M)
_GO_FAIL_RE = re.compile(r"^(?:--- FAIL:|FAIL\s)", re.M)
_GO_TEST_FAIL_RE = re.compile(r"^\s*--- FAIL:", re.M)
_GO_TEST_PASS_RE = re.compile(r"^\s*--- PASS:", re.M)
_GO_PKG_OK_RE = re.compile(r"^ok\s+\S+\s+(?:[\d.]+s|\(cached\))", re.M)
_GO_PKG_FAIL_RE = re.compile(r"^FAIL\s+\S+\s+[\d.]+s", re.M)
_MOCHA_RE = re.compile(r"^\s*(\d+) (passing|failing)\b", re.M)


_UNITTEST_RE = re.compile(r"^Ran (\d+) tests? in [\d.]+s\s*\n\s*\n?((?:OK|FAILED)(?: \([^)]*\))?)", re.M)


def parse_test_output(text):
    """{passed, failed, framework} from the last test summary in `text`, or None if there is none."""
    if not text:
        return None
    hits = []  # (end offset, result) -> the summary printed last wins
    node = {}
    for m in _NODE_RE.finditer(text):
        node[m.group(1)] = (int(m.group(2)), m.end())
    if "pass" in node and "fail" in node:
        hits.append((max(node["pass"][1], node["fail"][1]),
                     {"passed": node["pass"][0], "failed": node["fail"][0], "framework": "node:test"}))
    for m in _JEST_RE.finditer(text):
        line = m.group(1)
        p, f = re.search(r"(\d+) passed", line), re.search(r"(\d+) failed", line)
        if p or f:
            hits.append((m.end(), {"passed": int(p.group(1)) if p else 0, "failed": int(f.group(1)) if f else 0,
                                   "framework": "jest"}))
    for m in _PYTEST_LINE_RE.finditer(text):
        line = m.group(0)
        if line.lstrip().startswith("Tests:"):
            continue
        p = re.search(r"(\d+) passed", line)
        f = sum(int(x) for x in re.findall(r"(\d+) (?:failed|errors?)\b", line))
        hits.append((m.end(), {"passed": int(p.group(1)) if p else 0, "failed": f, "framework": "pytest"}))
    if _GO_PKG_OK_RE.search(text) or _GO_PKG_FAIL_RE.search(text) or _GO_TEST_FAIL_RE.search(text):
        tf, tp = len(_GO_TEST_FAIL_RE.findall(text)), len(_GO_TEST_PASS_RE.findall(text))
        if tf or tp:
            res = {"passed": tp, "failed": tf}
        else:
            res = {"passed": len(_GO_PKG_OK_RE.findall(text)), "failed": len(_GO_PKG_FAIL_RE.findall(text))}
        last = max(m.end() for m in list(_GO_FAIL_RE.finditer(text)) + list(_GO_PKG_OK_RE.finditer(text)))
        hits.append((last, dict(res, framework="go test")))
    for m in _UNITTEST_RE.finditer(text):   # "Ran 9 tests in 0.003s\n\nOK" / "FAILED (failures=1, errors=2)"
        n, verdict = int(m.group(1)), m.group(2)
        f = sum(int(x) for x in re.findall(r"(?:failures|errors)=(\d+)", verdict or ""))
        sk = sum(int(x) for x in re.findall(r"(?:skipped|expected failures)=(\d+)", verdict or ""))
        hits.append((m.end(), {"passed": max(0, n - f - sk), "failed": f, "framework": "unittest"}))
    mocha = {}
    for m in _MOCHA_RE.finditer(text):
        mocha[m.group(2)] = (int(m.group(1)), m.end())
    if mocha:
        hits.append((max(v[1] for v in mocha.values()),
                     {"passed": mocha.get("passing", (0, 0))[0], "failed": mocha.get("failing", (0, 0))[0],
                      "framework": "mocha"}))
    if not hits:
        return None
    return max(hits, key=lambda h: h[0])[1]


def test_text(t):
    if t["failed"]:
        return "%d failed" % t["failed"]
    return "%d passed" % t["passed"]


# ---------------------------------------------------------------- step normalization

def _split_segments(cmd):
    """Top-level command segments split on && || ; | (quotes respected)."""
    try:
        lex = shlex.shlex(cmd, posix=True, punctuation_chars=";&|")
        lex.whitespace_split = True
        toks = list(lex)
    except ValueError:
        toks = cmd.split()
    segs, cur = [], []
    for t in toks:
        if t and set(t) <= set(";&|"):
            if cur:
                segs.append(cur)
            cur = []
        else:
            cur.append(t)
    if cur:
        segs.append(cur)
    return segs


def _is_script(tok):
    if re.search(r"[\s(){}=;'\"]", tok):
        return False
    return ("/" in tok or tok.endswith(SCRIPT_EXT)) and not tok.startswith("-")


SHELL_KEYWORDS = ("do", "then", "else", "elif", "{", "(", "!")
COMPOUND_HEADS = ("for", "while", "until", "if", "case", "select", "done", "fi", "esac", "}", ")")
INLINE_CODE_FLAGS = ("-e", "-c", "-p", "--eval", "--print")


def normalize_command(cmd):
    """First program of a shell command (+ subcommand for package managers / git / runtimes, or the script path).

    Loop/if headers are skipped, so `for f in *; do cat $f; done` is `cat`."""
    for seg in _split_segments(str(cmd or "").strip()):
        toks = list(seg)
        while toks and toks[0] in SHELL_KEYWORDS:
            toks.pop(0)
        if toks and toks[0] in COMPOUND_HEADS:
            continue
        while toks and (re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", toks[0]) or toks[0] in WRAPPERS):
            toks.pop(0)
        if toks and toks[0] == "timeout":
            toks = toks[1:]
            while toks and (toks[0].startswith("-") or re.match(r"^\d", toks[0])):
                toks.pop(0)
        if not toks or toks[0] in ("cd", "pushd", "popd", "export", "source", ".", "set"):
            continue
        prog = toks[0]
        if prog.startswith("./") or prog.startswith("../"):
            return prog
        base = os.path.basename(prog) if "/" in prog else prog
        args = [t for t in toks[1:] if not t.startswith(("<", ">")) and not re.match(r"^\d?>", t)]
        if base in ("bash", "sh", "zsh"):
            script = next((a for a in args if not a.startswith("-")), None)
            if script and _is_script(script):
                return script if script.startswith((".", "/")) else "./" + script
            return base
        if base in SUBCOMMAND_PROGRAMS:
            if base in ("python", "python3") and len(args) >= 2 and args[0] == "-m":
                return "%s -m %s" % (base, args[1])
            if base == "node" and args and args[0] == "--test":
                return "node --test"
            if base == "npm" and len(args) >= 2 and args[0] in ("run", "run-script"):
                return "npm run %s" % args[1]
            if base in ("yarn", "pnpm") and len(args) >= 2 and args[0] == "run":
                return "%s run %s" % (base, args[1])
            if base == "git":
                while len(args) >= 2 and args[0] in ("-C", "-c", "--git-dir", "--work-tree"):
                    args = args[2:]
            if base in ("node", "python", "python3") and args and args[0] in INLINE_CODE_FLAGS:
                return "%s %s" % (base, args[0])
            sub = next((a for a in args if not a.startswith("-")), None)
            if base == "pytest" or not sub:
                return base
            if base in ("node", "python", "python3"):
                return "%s %s" % (base, sub) if _is_script(sub) and not re.search(r"\s", sub) else base
            if base == "git" and "/" in sub:
                return base
            return "%s %s" % (base, sub)
        return base
    return "sh"


_RUNNER_SUB = {"npm": "test", "yarn": "test", "pnpm": "test", "bun": "test", "go": "test", "make": "test",
               "cargo": "test"}
_JS_RUNNERS = ("jest", "vitest", "mocha")
_PKG_EXEC = ("npx", "bunx", "pnpx")


def _runner_of(toks):
    """Test runner named by one command segment ('./run_tests.sh', 'npm test', 'pytest', ...) or None."""
    norm = normalize_command(" ".join(shlex.quote(t) for t in toks))
    words = norm.split()
    if not words:
        return None
    if words[0].startswith(("./", "../", "/")):
        return norm if re.search(r"test", os.path.basename(words[0]), re.I) else None
    if norm in ("pytest", "python -m pytest", "python3 -m pytest"):
        return "pytest"
    if len(toks) >= 3 and re.fullmatch(r"python[\d.]*", os.path.basename(toks[0])) and toks[1] == "-m" and toks[2] in ("pytest", "unittest"):
        return toks[2]
    if norm == "node --test":
        return norm
    if len(words) == 2 and _RUNNER_SUB.get(words[0]) == words[1]:
        return norm
    if len(words) == 3 and words[0] in ("npm", "yarn", "pnpm") and words[1] == "run" and words[2] == "test":
        return "%s test" % words[0]
    if words[0] in _JS_RUNNERS:
        return words[0]
    if words[0] in _PKG_EXEC or norm in ("pnpm exec", "yarn exec"):
        rest = [t for t in toks if not t.startswith("-")]
        nxt = next((t for t in rest[1:] if t not in ("exec",)), "")
        if os.path.basename(nxt) in _JS_RUNNERS:
            return os.path.basename(nxt)
    return None


def test_runner(cmd):
    """First known test runner among the segments of a (compound) shell command, or None."""
    for seg in _split_segments(str(cmd or "")):
        r = _runner_of(seg)
        if r:
            return r
    return None


def step_kind(name, inp):
    """(kind, target) for a tool_use; kind has paths/patterns stripped (chains group by it)."""
    inp = inp if isinstance(inp, dict) else {}
    if name == "Bash":
        return "Bash(%s)" % normalize_command(inp.get("command")), " ".join(str(inp.get("command", "")).split())
    if name in ("Task", "Agent"):
        return "Agent(%s)" % (inp.get("subagent_type") or "general-purpose"), str(inp.get("description") or "")
    if name == "Skill":
        sk = inp.get("skill") or inp.get("name") or inp.get("command") or "?"
        return "Skill(%s)" % sk, str(sk)
    mp = mcp_parts(name)
    if mp:
        return "MCP(%s.%s)" % mp, ""
    if name in ("TodoWrite", "TodoRead", "TaskCreate", "TaskUpdate"):
        return "Todo", ""
    if name == "MultiEdit":
        return "Edit", None
    return name, None


def step_label(step):
    k, tgt = step["kind"], step["target"]
    tests = step.get("tests")
    if k.startswith("Hook("):
        lab = "Hook " + k[5:-1]
        return lab + (" → " + test_text(tests) if tests else ("" if step["ok"] is not False else " → error"))
    if k == "Grep":
        lab = 'Grep "%s"' % (tgt or "")[:40]
    elif k in ("Read", "Edit", "Write", "Glob", "NotebookEdit"):
        lab = "%s %s" % (k, tgt) if tgt else k
    else:
        lab = k
    if tests:
        lab += " " + test_text(tests)
    elif step["ok"] is False:
        lab += " (error)"
    return lab


def _text_of(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        out = []
        for b in content:
            if isinstance(b, dict) and b.get("type") == "text":
                out.append(str(b.get("text", "")))
            elif isinstance(b, str):
                out.append(b)
        return "\n".join(out)
    return ""


# ---------------------------------------------------------------- transcript walk

def _walk(session):
    """One pass over the main transcript: requests (prompt + msg ids), steps, per-message stop info."""
    cwd = session.cwd
    requests = []
    cur = None
    steps_by_tu = {}
    hook_steps = {}
    msg_info = {}
    terminal = []  # (request idx, signal) for result/max-turns/interrupt events

    def new_request(prompt, ts, source="human"):
        r = {"prompt": prompt, "start": ts, "end": ts, "msg_ids": [], "steps": [], "source": source}
        requests.append(r)
        return r

    for ev in session.events:
        typ = ev.get("type")
        ts = parse_ts(ev.get("timestamp"))
        if typ == "user":
            prompt = real_prompt_text(ev)
            if prompt:
                cur = new_request(prompt, ts, prompt_source(ev, prompt))
                continue
            content = (ev.get("message") or {}).get("content")
            if cur is not None and content_startswith(content, "[Request interrupted"):
                terminal.append((len(requests) - 1, "interrupted by user"))
            if isinstance(content, list):
                for b in content:
                    if isinstance(b, dict) and b.get("type") == "tool_result":
                        st = steps_by_tu.get(b.get("tool_use_id"))
                        if st is None:
                            continue
                        tur = ev.get("toolUseResult")
                        st["ok"] = not (bool(b.get("is_error")) or isinstance(tur, str))
                        st["_text"] = _text_of(b.get("content"))[-RESULT_TEXT_TAIL:]
        elif typ == "assistant":
            msg = ev.get("message") if isinstance(ev.get("message"), dict) else {}
            if msg.get("model") == "<synthetic>":
                txt = _text_of(msg.get("content"))
                if cur is not None and re.search(r"max turns|maximum number of turns", txt, re.I):
                    terminal.append((len(requests) - 1, "max turns reached"))
                continue
            if not isinstance(msg.get("usage"), dict):
                continue
            if cur is None:
                cur = new_request(None, ts)
            mid = msg.get("id") or ev.get("uuid")
            info = msg_info.setdefault(mid, {"text": "", "stop_reason": None, "tool_uses": 0})
            if mid not in cur["msg_ids"]:
                cur["msg_ids"].append(mid)
            if msg.get("stop_reason"):
                info["stop_reason"] = msg["stop_reason"]
            for b in msg.get("content") or []:
                if not isinstance(b, dict):
                    continue
                if b.get("type") == "text" and str(b.get("text", "")).strip():
                    info["text"] = str(b["text"])
                elif b.get("type") == "tool_use" and b.get("id") not in steps_by_tu:
                    name = b.get("name") or "?"
                    kind, target = step_kind(name, b.get("input"))
                    if target is None:
                        inp = b.get("input") if isinstance(b.get("input"), dict) else {}
                        raw = inp.get("file_path") or inp.get("notebook_path") or inp.get("pattern") or ""
                        target = relpath(str(raw), cwd) if name not in ("Grep", "Glob") else str(raw)
                        if not target:
                            target = str(inp.get("url") or inp.get("query") or inp.get("description") or "")[:120]
                    st = {"kind": kind, "tool": name, "target": target, "ok": None, "tokens": 0, "est": True,
                          "tool_use_id": b.get("id"), "msg_id": mid, "detail": None, "tests": None,
                          "fp": input_fp(b.get("input"))}
                    if name in ("Grep", "Glob") and isinstance(b.get("input"), dict) and b["input"].get("path"):
                        st["path"] = relpath(str(b["input"]["path"]), cwd)
                    steps_by_tu[st["tool_use_id"]] = st
                    cur["steps"].append(st)
                    info["tool_uses"] += 1
        elif typ == "attachment" and isinstance(ev.get("attachment"), dict):
            a = ev["attachment"]
            t = str(a.get("type") or "")
            if t.startswith("hook_") and cur is not None:
                name = a.get("hookName") or a.get("hookEvent") or "hook"
                key = (a.get("toolUseID"), name, len(requests) if not a.get("toolUseID") else 0)
                st = hook_steps.get(key)
                if st is None:
                    st = {"kind": "Hook(%s)" % name, "tool": "hook", "target": a.get("command") or "", "ok": True,
                          "tokens": 0, "est": True, "tool_use_id": a.get("toolUseID"), "hook_name": name,
                          "msg_id": None, "detail": None, "tests": None, "_text": ""}
                    hook_steps[key] = st
                    parent = steps_by_tu.get(a.get("toolUseID"))
                    _insert_hook(cur["steps"], st, parent)
                if "error" in t or str(a.get("exitCode") or "0") not in ("0", ""):
                    st["ok"] = False
                if a.get("command"):
                    st["target"] = a["command"]
                c = a.get("content")
                txt = "\n".join(str(x) for x in c) if isinstance(c, list) else str(c or "")
                if not txt and t == "hook_success":
                    txt = str(a.get("stdout") or "")
                if txt:
                    st["_text"] = (st["_text"] + "\n" + txt)[-RESULT_TEXT_TAIL:]
        elif typ == "result" and str(ev.get("subtype") or "").startswith("error") and cur is not None:
            terminal.append((len(requests) - 1, "result subtype %s" % ev.get("subtype")))
        elif typ == "system" and "max_turns" in str(ev.get("subtype") or "") and cur is not None:
            terminal.append((len(requests) - 1, "max turns reached"))
        if ts and cur is not None:
            cur["end"] = ts
    return requests, msg_info, terminal


_PERSISTED_RE = re.compile(r"Full output saved to: (\S+)")


def full_output(text):
    """For a persisted-output preview, the tail of the saved file (read-only) when it still exists."""
    if "<persisted-output>" not in text[:200]:
        return text
    m = _PERSISTED_RE.search(text)
    if m and os.path.isfile(m.group(1)):
        try:
            with open(m.group(1), encoding="utf-8", errors="replace") as f:
                f.seek(max(0, os.path.getsize(m.group(1)) - 4 * RESULT_TEXT_TAIL))
                return f.read()[-RESULT_TEXT_TAIL:]
        except OSError:
            pass
    return text


def input_fp(inp):
    return hashlib.sha1(json.dumps(inp, sort_keys=True, default=str).encode("utf-8")).hexdigest()[:12]


def content_startswith(content, prefix):
    return _text_of(content).lstrip().startswith(prefix)


def _insert_hook(steps, hook, parent):
    """Place a hook right after the tool it followed (after earlier hooks of that tool), else at the end."""
    if parent is None or parent not in steps:
        steps.append(hook)
        return
    i = steps.index(parent) + 1
    while i < len(steps) and steps[i]["kind"].startswith("Hook(") and steps[i]["tool_use_id"] == parent["tool_use_id"]:
        i += 1
    steps.insert(i, hook)


# ---------------------------------------------------------------- chains / flow

def is_hook(step):
    return step["kind"].startswith("Hook(")


def chain_units(steps):
    """[(tool kind, (hook kinds...))]: each tool step with the hooks that fired after it."""
    units = []
    for s in steps:
        if is_hook(s) and units and s.get("tool_use_id") and units[-1][2] == s["tool_use_id"]:
            units[-1][1].append(s["kind"])
        else:
            units.append((s["kind"], [], s.get("tool_use_id") if not is_hook(s) else None))
    return [(k, tuple(h)) for k, h, _ in units]


def chain_of(steps):
    """Kinds with targets stripped; consecutive identical (tool + its hooks) units collapsed with ×N.

    e.g. Edit, Hook, Edit, Hook -> "Edit×2 → Hook(PostToolUse:Edit)×2"."""
    parts, prev, n = [], None, 0

    def flush():
        if prev is None:
            return
        sfx = "×%d" % n if n > 1 else ""
        parts.append(prev[0] + sfx)
        for h in prev[1]:
            parts.append(h + sfx)

    for u in chain_units(steps):
        if u == prev:
            n += 1
        else:
            flush()
            prev, n = u, 1
    flush()
    return " → ".join(parts) if parts else "(no tools)"


def chain_signature(steps):
    """Two most frequent distinct tool kinds (hooks excluded), ties by first appearance."""
    kinds = [s["kind"] for s in steps if not is_hook(s)]
    if not kinds:
        return "(no tools)"
    c = Counter(kinds)
    first = {}
    for i, k in enumerate(kinds):
        first.setdefault(k, i)
    top = sorted(c, key=lambda k: (-c[k], first[k]))[:2]
    return " + ".join(top)


def flow_sequence(steps, positions=FLOW_POSITIONS):
    kinds = []
    for s in steps:
        if is_hook(s):
            continue
        if not kinds or kinds[-1] != s["kind"]:
            kinds.append(s["kind"])
    return kinds[:positions]


def flow(seqs, positions=FLOW_POSITIONS):
    """Sankey-like data from kind sequences: nodes per position, edges between consecutive positions."""
    nodes, edges = Counter(), Counter()
    for seq in seqs:
        for i, k in enumerate(seq[:positions]):
            nodes[(i + 1, k)] += 1
            if i > 0:
                edges[(i, seq[i - 1], k)] += 1
    out_nodes = []
    for (pos, kind), cnt in sorted(nodes.items(), key=lambda kv: (kv[0][0], -kv[1], kv[0][1])):
        node = {"position": pos, "kind": kind, "count": cnt, "top_predecessor": None}
        preds = [(fk, c) for (fp, fk, tk), c in edges.items() if fp == pos - 1 and tk == kind]
        if preds:
            fk, c = max(preds, key=lambda x: (x[1], x[0]))
            node["top_predecessor"] = {"kind": fk, "count": c, "pct": round(100.0 * c / cnt, 1),
                                       "text": "%d%% after %s" % (round(100.0 * c / cnt), fk)}
        out_nodes.append(node)
    out_edges = []
    for (fp, fk, tk), c in sorted(edges.items(), key=lambda kv: (kv[0][0], -kv[1])):
        base = nodes[(fp, fk)]
        out_edges.append({"from_pos": fp, "to_pos": fp + 1, "from_kind": fk, "to_kind": tk, "count": c,
                          "pct": round(100.0 * c / base, 1) if base else 0.0})
    return {"positions": positions, "sequences": len(seqs), "nodes": out_nodes, "edges": out_edges}


# ---------------------------------------------------------------- outcomes

def last_test(steps):
    for s in reversed(steps):
        if s.get("tests"):
            return s
    return None


def _same_step(a, b):
    """Same kind + target, and the same input: successive different Edits of one file are progress, not a loop."""
    return (a["kind"], a["target"], a.get("fp")) == (b["kind"], b["target"], b.get("fp"))


def loop_info(steps):
    tools = [s for s in steps if not is_hook(s)]
    run, best, what = 1, 1, None
    for i in range(1, len(tools)):
        if _same_step(tools[i], tools[i - 1]):
            run += 1
            if run > best:
                best, what = run, tools[i]
        else:
            run = 1
    cycles, failing, edited = 0, False, False
    for s in steps:
        t = s.get("tests")
        if t:
            if t["failed"]:
                if failing and edited:
                    cycles += 1
                failing, edited = True, False
            else:
                failing = False
        elif s["tool"] in EDIT_TOOLS:
            edited = True
    signals = []
    if best >= LOOP_REPEAT:
        signals.append("%s ×%d in a row" % (step_label(what), best))
    if cycles >= LOOP_FAIL_CYCLES:
        signals.append("%d fail → edit → fail test cycles" % cycles)
    return signals


def outcome_of(req, last_info, terminal, in_progress=False):
    """(outcome, signals, flags) from transcript signals only."""
    steps = req["steps"]
    signals, flags = [], []
    final_text = bool(last_info and last_info["text"] and last_info["stop_reason"] == "end_turn")
    stop = last_info["stop_reason"] if last_info else None
    lt = last_test(steps)
    tools = [s for s in steps if not is_hook(s)]
    outcome = None
    err = [t for t in terminal if t.startswith("result subtype")]
    if err:
        outcome = "failed"
        signals += err
    elif not final_text or terminal:
        outcome = "abandoned"
        if terminal:
            signals += terminal
        elif last_info is None:
            signals.append("no assistant response")
        elif stop == "tool_use" or (tools and tools[-1]["ok"] is None):
            signals.append("ended mid-tool (stop_reason %s)" % stop)
        else:
            signals.append("no final assistant text (stop_reason %s)" % stop)
        if in_progress:
            flags.append("maybe_in_progress")
            signals.append("session written < %d min ago: may still be running" % (IN_PROGRESS_SECONDS // 60))
    elif lt and lt["tests"]["failed"]:
        outcome = "failed"
        signals.append("last test signal: %s (%s, %s)" % (test_text(lt["tests"]), lt["tests"]["framework"],
                                                           step_label(lt)))
    elif lt:
        outcome = "success"
        signals.append("last test signal: %s, 0 failed (%s, %s)" % (lt["tests"]["passed"], lt["tests"]["framework"],
                                                                    step_label(lt)))
    elif tools and tools[-1]["ok"] is False:
        outcome = "failed"
        signals.append("final step errored: %s" % step_label(tools[-1]))
    else:
        outcome = "success"
        signals.append("no tests; final assistant text, stop_reason end_turn")
    if final_text and outcome != "abandoned" and not any("final assistant text" in s for s in signals):
        signals.append("final assistant text, stop_reason end_turn")
    loops = loop_info(steps)
    if loops:
        flags.append("looping")
        signals += ["looping: " + x for x in loops]
    if not any(s["tool"] in EDIT_TOOLS for s in steps) and not _bash_wrote(steps):
        flags.append("read_only")
    return outcome, signals, flags


# ---------------------------------------------------------------- per-session

def _tok(calls):
    t = {"all_in": 0, "new": 0, "cache_read": 0, "output": 0, "fresh_input": 0, "cache_write": 0}
    for c in calls:
        u = c["usage"]
        t["fresh_input"] += u["input"]
        t["cache_write"] += u["cache_write"]
        t["cache_read"] += u["cache_read"]
        t["output"] += u["output"]
    t["new"] = t["fresh_input"] + t["cache_write"] + t["output"]
    t["all_in"] = t["new"] + t["cache_read"]
    return t


def _iso(dt):
    return dt.isoformat().replace("+00:00", "Z") if dt else None


def _step_tokens(analysis):
    """tool_use_id -> (tokens, scaled_to_measured, call) and (tool_use_id, hook) -> (tokens, scaled_to_measured)."""
    res, hooks = {}, {}
    for c in analysis["calls"]:
        scaled = {i.get("tool_use_id"): bool(i.get("scaled_to_measured")) for i in c["attribution"]
                  if i["cause"] == "tool_result"}
        hscaled = any(i.get("scaled_to_measured") for i in c["attribution"] if i["cause"] == "hook")
        for r in c["tool_results"]:
            res[r["tool_use_id"]] = (r["attributed_tokens"], scaled.get(r["tool_use_id"], False), c["index"])
        for h in c["hooks"]:
            k = (h["tool_use_id"], h["hook"])
            t, s = hooks.get(k, (0, False))
            hooks[k] = (t + h.get("attributed_tokens", 0), s or hscaled)
    return res, hooks


def understand_session(session, analysis=None, now=None):
    """Requests with steps/chains/outcomes + context-by-source series for one loaded session."""
    a = analysis or analyze_session(session)
    raw, msg_info, terminal = _walk(session)
    res_tok, hook_tok = _step_tokens(a)
    calls_by_mid = {c["msg_id"]: c for c in a["calls"]}
    sub_by_parent = {}
    for sa in a["subagents"]:
        sub_by_parent.setdefault(sa["tool_use_id"], []).append(sa)
    sess_all = a["totals"]["all"]["all_in"] or 0
    now = now or datetime.now(timezone.utc)
    in_progress = bool(session.end and (now - session.end).total_seconds() < IN_PROGRESS_SECONDS)
    sid8 = session.id[:8]
    requests = []
    n = 0
    for ri, r in enumerate(raw):
        main = [calls_by_mid[m] for m in r["msg_ids"] if m in calls_by_mid]
        subs = []
        for st in r["steps"]:
            tok = res_tok.get(st["tool_use_id"])
            if is_hook(st):
                st["tokens"], scaled = hook_tok.get((st["tool_use_id"], st["hook_name"]), (0, False))
                st["est"] = not scaled
            elif tok:
                st["tokens"], scaled, _ = tok
                st["est"] = not scaled
            text = full_output(st.pop("_text", ""))
            if st["kind"].startswith("Bash(") or is_hook(st):
                st["tests"] = parse_test_output(text)
                if st["tests"] and st["tests"]["failed"]:
                    st["ok"] = False
            if st["kind"].startswith("Bash(") and st["tests"]:
                # `cat src/*.js; ./run_tests.sh | grep ...` is a test run, not a cat
                runner = test_runner(st["target"])
                if runner:
                    st["kind"] = "Bash(%s)" % runner
            if st["tool"] in ("Task", "Agent"):
                for sa in sub_by_parent.get(st["tool_use_id"], []):
                    subs.append(sa)
                    st["detail"] = "subagent %s: %d calls, all-in %d" % (sa["type"], sa["calls"],
                                                                         sa["tokens"]["all_in"])
            elif st["kind"].startswith("Bash("):
                st["detail"] = st["target"]
            elif is_hook(st):
                st["detail"] = st["target"][:200]
            st["label"] = step_label(st)
        sub_calls = [c for sa in subs for c in sa["call_records"]]
        tok = _tok(main + sub_calls)
        if r["prompt"] is None and not main:
            continue
        n += 1
        last_info = msg_info.get(r["msg_ids"][-1]) if r["msg_ids"] else None
        term = [s for i, s in terminal if i == ri]
        outcome, signals, flags = outcome_of(r, last_info, term, in_progress and ri == len(raw) - 1)
        steps = r["steps"]
        requests.append({
            "id": "%s:%d" % (sid8, n), "session_id": session.id, "project": session.project, "cwd": session.cwd, "n": n,
            "prompt": r["prompt"], "start": _iso(r["start"]), "end": _iso(r["end"]),
            "duration_seconds": (r["end"] - r["start"]).total_seconds() if r["start"] and r["end"] else None,
            "calls": len(main) + len(sub_calls), "main_calls": len(main), "subagent_calls": len(sub_calls),
            "call_indexes": [c["index"] for c in main],
            "peak_context": max([c["context"] for c in main] or [0]),
            "tokens": tok, "share_of_session": round(tok["all_in"] / sess_all, 4) if sess_all else 0.0,
            "goal": None, "files": gl.request_files(steps, session.cwd), "steps": steps, "tool_steps": sum(1 for s in steps if not is_hook(s)),
            "chain": chain_of(steps), "signature": chain_signature(steps), "flow": flow_sequence(steps),
            "outcome": outcome, "signals": signals, "flags": flags, "source": r["source"],
            "final_text": (last_info or {}).get("text", "")[:300] if outcome != "abandoned" else "",
        })
    return {"session_id": session.id, "project": session.project, "start": a["start"], "end": a["end"],
            "totals": a["totals"]["all"], "requests": requests, "context": context_series(a),
            "tools_offered": offered_tools(session),
            "subagent_prompts": {"subagent": a.get("request_sources", {}).get("subagent", 0)}}


def offered_tools(session):
    """[{name, est_tokens}] of the tool definitions in the transcript's prompt snapshot ([] if not recorded)."""
    for ev in session.events:
        a = ev.get("attachment") if ev.get("type") == "attachment" else None
        if isinstance(a, dict) and a.get("type") == "prompt_snapshot" and isinstance(a.get("tools"), list):
            out = []
            for t in a["tools"]:
                name = t.get("name") if isinstance(t, dict) else str(t)
                chars = len(json.dumps(t, ensure_ascii=False)) if isinstance(t, dict) else len(str(t))
                out.append({"name": name or "?", "est_tokens": est_tokens(chars), "est": True})
            return out
    return []


# ---------------------------------------------------------------- context by source

SOURCES = ["system_prompt", "tool_definitions", "skills", "mcp_used", "mcp_unused", "instructions", "hook_output",
           "tool_results", "user_input", "assistant_history", "other"]
SOURCE_LABELS = {"system_prompt": "system prompt", "tool_definitions": "tool definitions",
                 "skills": "skills listing", "mcp_used": "MCP listings (used servers)",
                 "mcp_unused": "MCP listings (unused servers)", "instructions": "instructions (CLAUDE.md + imports)",
                 "hook_output": "hook output", "tool_results": "tool results (history)", "user_input": "user input",
                 "assistant_history": "assistant history", "other": "other"}
STATIC_SOURCES = ("system_prompt", "tool_definitions", "skills", "mcp_used", "mcp_unused", "instructions")


def _mcp_split(offered):
    """Fraction of MCP listing tokens that belong to servers that were used, + names."""
    servers = offered.get("mcp_servers") or []
    used = sum(e["est_tokens"] for e in servers if e["used"])
    total = sum(e["est_tokens"] for e in servers)
    return (used / total if total else 0.0), [e["name"] for e in servers if e["used"]], \
        [e["name"] for e in servers if not e["used"]]


def _source_of(item, mcp_names_share):
    """[(source, fraction)] for one attribution item."""
    cause, label = item["cause"], item.get("label") or ""
    if cause == "system_overhead":
        if label == "system prompt":
            return [("system_prompt", 1.0)]
        if label in ("tool schemas", "subagent type listing"):
            return [("tool_definitions", 1.0)]
        if label == "deferred tool names":
            return [("mcp", mcp_names_share), ("tool_definitions", 1.0 - mcp_names_share)]
        if label == "skill listing":
            return [("skills", 1.0)]
        if label == "MCP server instructions":
            return [("mcp", 1.0)]
        if label.startswith("CLAUDE.md"):
            return [("instructions", 1.0)]
        return [("other", 1.0)]
    return [({"user_prompt": "user_input", "tool_result": "tool_results", "hook": "hook_output",
              "assistant_output": "assistant_history"}.get(cause, "other"), 1.0)]


FIRST_CALL_RESIDUAL = ("other (unattributed)", "tool schemas / other (unattributed)")
SCALABLE_LATER = ("user_prompt", "tool_result", "hook")   # chars/4 estimates in a later call's new tokens


def _scaled_items(c, pos):
    """The call's attribution items with chars/4 estimates scaled to the measured new tokens.

    First call: the system components (system prompt, tool definitions, listings, instruction files) are scaled so
    that, with the user prompt / results, they sum to the measured context; the unattributed residual disappears.
    Later calls: user prompt, tool results and hook output absorb the 'Other' residual. Items are left alone (and the
    residual stays 'other') when there is nothing estimated to scale. Returns (items, scaled?)."""
    items = [it for it in c["attribution"] if it["cause"] not in ("cache_miss", "compaction")]
    if pos == 0:
        resid = [it for it in items if it["cause"] == "system_overhead" and it.get("label") in FIRST_CALL_RESIDUAL]
        system = [it for it in items if it["cause"] == "system_overhead" and it not in resid]
    else:
        resid = [it for it in items if it["cause"] == "other"]
        system = [it for it in items if it["cause"] in SCALABLE_LATER]
    est = sum(it["tokens"] for it in system)
    extra = sum(it["tokens"] for it in resid)
    if not resid or not extra or est <= 0:
        return items, False
    f = (est + extra) / est
    return [dict(it, tokens=it["tokens"] * f, scaled_to_measured=True) if it in system else it
            for it in items if it not in resid], True


def _context_series(calls, offered, used_share):
    """Per call: context split by source (carried forward), cached vs uncached (measured)."""
    servers = offered.get("mcp_servers") or []
    names_total = sum(e.get("names_est_tokens", 0) for e in servers)
    out, cur, files, scaled = [], {k: 0.0 for k in SOURCES}, {}, set()
    for pos, c in enumerate(calls):
        if pos == 0:
            cur = {k: 0.0 for k in SOURCES}
        if c.get("compaction"):
            static = {k: cur[k] for k in STATIC_SOURCES}
            cur = {k: 0.0 for k in SOURCES}
            cur.update(static)
            scaled &= set(STATIC_SOURCES)
        items, _ = _scaled_items(c, pos)
        for it in items:
            share = 0.0
            if it.get("label") == "deferred tool names" and it["tokens"]:
                share = min(1.0, names_total / it["tokens"])
            for src, frac in _source_of(it, share):
                t = it["tokens"] * frac
                keys = [("mcp_used", used_share), ("mcp_unused", 1 - used_share)] if src == "mcp" else [(src, 1.0)]
                for key, kf in keys:
                    cur[key] += t * kf
                    if it.get("scaled_to_measured") and t * kf:
                        scaled.add(key)
                if src == "instructions" and pos == 0:
                    files[it.get("detail") or "?"] = files.get(it.get("detail") or "?", 0) + it["tokens"]
        total = sum(cur.values())
        ctx = c["context"]
        if total > ctx and total:
            f = ctx / total
            cur = {k: v * f for k, v in cur.items()}
        elif total < ctx:
            cur["other"] += ctx - total
        u = c["usage"]
        out.append({"index": c["index"], "timestamp": c["timestamp"], "context": ctx,
                    "cached": u["cache_read"], "uncached": u["input"] + u["cache_write"], "output": u["output"],
                    "sources": {k: int(round(v)) for k, v in cur.items()}, "est": True,
                    "scaled_to_measured": sorted(k for k in scaled if cur[k] >= 0.5)})
    return out, {p: int(round(v)) for p, v in files.items()}


def context_series(a):
    offered = a["offered_vs_used"] if a["offered_vs_used"].get("available") else {}
    used_share, mcp_used, mcp_unused = _mcp_split(offered)
    main, files = _context_series(a["calls"], offered, used_share)
    agents = []
    for sa in a["subagents"]:
        s, _ = _context_series(sa["call_records"], {}, 0.0)
        agents.append({"agent_id": sa["agent_id"], "type": sa["type"], "calls": s})
    skills_used = [e["name"] for e in (offered.get("skills") or []) if e["used"]]
    totals = {k: 0 for k in SOURCES}
    scaled = set()
    for c in main + [x for ag in agents for x in ag["calls"]]:
        scaled.update(c["scaled_to_measured"])
        for k, v in c["sources"].items():
            totals[k] += v
    return {"main": main, "subagents": agents, "totals": totals, "instruction_files": files,
            "skills_used": skills_used, "mcp_used": mcp_used, "mcp_unused": mcp_unused, "est": True,
            "scaled_to_measured": sorted(scaled),
            "note": "per-call context is measured; the split by source is estimated (chars/4, carried forward) and "
                    "scaled to the measured new tokens of each call ('scaled to measured'); 'other' is what no "
                    "estimate covers"}


def context_aggregate(sessions):
    """Context-by-source totals per run (session) and per day (UTC)."""
    by_run, by_day = [], OrderedDict()
    for s in sorted(sessions, key=lambda s: s["start"] or ""):
        t = s["context"]["totals"]
        by_run.append({"session_id": s["session_id"], "project": s["project"], "start": s["start"],
                       "sources": dict(t)})
        day = (s["start"] or "")[:10] or "unknown"
        d = by_day.setdefault(day, {k: 0 for k in SOURCES})
        for k, v in t.items():
            d[k] += v
    return {"by_run": by_run, "by_day": [{"day": k, "sources": v} for k, v in by_day.items()],
            "labels": SOURCE_LABELS, "est": True}


# ---------------------------------------------------------------- aggregates

def _median(xs):
    return statistics.median(xs) if xs else 0


def goal_status(agg, overall_median, fix_share=None):
    """Status chip + the rule that produced it (first match wins: Failing, Looping, Costly, Healthy)."""
    if agg["finish_rate"] < STATUS_FINISH_MIN:
        return "Failing", "finish rate %d%% < %d%%" % (round(100 * agg["finish_rate"]), 100 * STATUS_FINISH_MIN)
    if agg["runs"] and agg["flags"].get("looping", 0) / agg["runs"] >= STATUS_LOOP_SHARE:
        return "Looping", "%d of %d runs flagged looping (≥ 1/3)" % (agg["flags"]["looping"], agg["runs"])
    if overall_median and agg["all_in"]["median"] > STATUS_COST_X * overall_median:
        return "Costly", "median all-in %s > %.1f× overall median %s" % (k(agg["all_in"]["median"]), STATUS_COST_X,
                                                                         k(overall_median))
    if fix_share is not None and fix_share > STATUS_FIX_SHARE:
        return "Costly", "fixes claim %d%% of this goal's new tokens (> %d%%)" % (round(100 * fix_share),
                                                                                 100 * STATUS_FIX_SHARE)
    return "Healthy", "no rule triggered"


def chain_stats(reqs, top=None):
    c = Counter(r["chain"] for r in reqs)
    out = []
    for chain, cnt in c.most_common(top):
        members = [r for r in reqs if r["chain"] == chain]
        out.append({"chain": chain, "count": cnt, "pct": round(100.0 * cnt / len(reqs), 1),
                    "signature": members[0]["signature"], "example": pick_example(members)["id"],
                    "request_ids": [r["id"] for r in members]})
    return out


def pick_example(reqs):
    """A representative request: a success if there is one, closest to the group's median all-in."""
    pool = [r for r in reqs if r["outcome"] == "success"] or reqs
    med = _median([r["tokens"]["all_in"] for r in pool])
    return min(pool, key=lambda r: (abs(r["tokens"]["all_in"] - med), r["start"] or ""))


def discover_goals(sessions, goal_threshold=None, area_threshold=None):
    """Run goal discovery over all requests of the understood sessions and tag each request with its goal."""
    reqs = [r for s in sessions for r in s["requests"]]
    d = gl.discover(reqs, goal_threshold, area_threshold)
    tag_requests(sessions, d)
    return d


def tag_requests(sessions, d):
    by_id = {g["id"]: g for g in d["goals"]}
    for s in sessions:
        for r in s["requests"]:
            g = by_id[d["assignment"][r["id"]]]
            r["goal"] = {"id": g["id"], "name": g["name"], "source": g["name_source"], "area": g["area"],
                         "area_name": g["area_name"]}


def aggregate(sessions, disc=None, fix_tokens_by_request=None):
    """Per-goal aggregates (evidence from discovery + outcomes, tokens, chains, flow, status).

    disc: goals.discover() output (run here when omitted). fix_tokens_by_request: {request id: tokens fixes claim}."""
    if disc is None:
        disc = discover_goals(sessions)
    reqs = [r for s in sessions for r in s["requests"]]
    by_id = {r["id"]: r for r in reqs}
    overall_median = _median([r["tokens"]["all_in"] for r in reqs])
    goals = []
    for ev in disc["goals"]:
        rs = sorted((by_id[i] for i in ev["request_ids"]), key=lambda r: (r["start"] or "", r["id"]))
        outcomes = Counter(r["outcome"] for r in rs)
        flags = Counter(f for r in rs for f in r["flags"])
        ai = [r["tokens"]["all_in"] for r in rs]
        nw = [r["tokens"]["new"] for r in rs]
        agg = dict(ev)
        agg.update({
            "goal": ev["name"], "runs": len(rs), "sessions": len({r["session_id"] for r in rs}),
            "finish_rate": outcomes.get("success", 0) / len(rs), "outcomes": dict(outcomes),
            "all_in": {"median": _median(ai), "min": min(ai), "max": max(ai)},
            "new": {"median": _median(nw), "min": min(nw), "max": max(nw)},
            "median_calls": _median([r["calls"] for r in rs]),
            "chains": chain_stats(rs), "signatures": dict(Counter(r["signature"] for r in rs).most_common()),
            "flags": dict(flags), "flow": flow([r["flow"] for r in rs]), "request_ids": [r["id"] for r in rs]})
        fix_share = None
        if fix_tokens_by_request is not None and sum(nw):
            fix_share = sum(fix_tokens_by_request.get(r["id"], 0) for r in rs) / sum(nw)
        agg["status"], agg["status_reason"] = goal_status(agg, overall_median, fix_share)
        goals.append(agg)
    unhandled = [r["id"] for r in reqs if r["tool_steps"] == 0 and r["outcome"] == "abandoned"]
    return {"goals": goals, "areas": disc["areas"], "goal_method": disc["method"],
            "overall_median_all_in": overall_median, "requests": len(reqs),
            "unhandled_requests": unhandled, "flow": flow([r["flow"] for r in reqs]),
            "status_rules": STATUS_RULES}


STATUS_RULES = [
    "Failing: finish rate (share of runs with outcome success) < 70%",
    "Looping: >= 1/3 of runs flagged looping (same step kind+target+input >= 3x in a row, or >= 3 fail->edit->fail "
    "test cycles)",
    "Costly: median all-in per run > 1.5x the median all-in of all requests, or fixes attributed to this goal's "
    "runs claim > 30% of its new tokens (new, not all-in: cache reads would make every listing look dominant)",
    "Healthy: none of the above (rules are checked in this order)",
]

FIX_SHARE_NOTE = ("Fix shares use new tokens (fresh input + cache write + output) of the goal's runs: cache reads "
                  "would make every listing look dominant. Listings (CLAUDE.md, skills, MCP) count only where they "
                  "were written, not where they were re-read from cache.")


def apply_fixes(u, fixes, update_status=True):
    """Tie fixes to the goals whose requests their evidence covers; wire the 'fixes claim > 30%' Costly rule.

    Each fix gets `goals`: [{goal_id, goal, runs_affected, tokens_in_goal, share_of_goal_tokens, saves_new_tokens}]
    (tokens = new tokens of the fix's cause in that goal's runs). Each goal gets `fix_share`, `top_fix` and a
    recomputed status (unless update_status is False). fixes: fix dicts with `touches` (see fixes.touch)."""
    reqs = [r for s in u["sessions"] for r in s["requests"]]
    by_call = {(r["session_id"], i): r for r in reqs for i in r["call_indexes"]}
    by_sid = {}
    for r in reqs:
        by_sid.setdefault(r["session_id"], []).append(r)
    groups = {g["id"]: g for g in u["goals"]}
    new_of = {gid: sum(r["tokens"]["new"] for r in reqs if r["goal"]["id"] == gid) for gid in groups}
    union = {}  # request id -> {touch key: tokens}: overlapping fixes (large read + repeated read) count once
    per_goal = {gid: [] for gid in groups}
    for fx in fixes:
        if not isinstance(fx, dict):
            continue
        hit = {}  # request id -> [request, tokens, saves]
        for t in fx.get("touches") or []:
            own = by_sid.get(t["session_id"]) or []
            r = by_call.get((t["session_id"], t["call_index"])) or (own[0] if own else None)
            if r is None:
                continue
            for x in (own if t["listing"] else [r]):
                hit.setdefault(x["id"], [x, 0, 0])
            hit[r["id"]][1] += t["tokens"]
            hit[r["id"]][2] += t["saves"]
            keyed = union.setdefault(r["id"], {})
            keyed[t["key"]] = max(keyed.get(t["key"], 0), t["tokens"])
        out = {}
        for r, tok, sav in hit.values():
            gid = r["goal"]["id"]
            e = out.setdefault(gid, {"goal_id": gid, "goal": groups[gid]["goal"], "runs_affected": 0,
                                     "tokens_in_goal": 0, "saves_new_tokens": 0})
            e["runs_affected"] += 1
            e["tokens_in_goal"] += tok
            e["saves_new_tokens"] += sav
        for e in out.values():
            e["share_of_goal_tokens"] = round(e["tokens_in_goal"] / new_of[e["goal_id"]], 4) \
                if new_of.get(e["goal_id"]) else 0.0
            per_goal.setdefault(e["goal_id"], []).append((fx, e))
        fx["goals"] = sorted(out.values(), key=lambda e: (-e["share_of_goal_tokens"], e["goal_id"]))
    if not update_status:
        return u
    fix_tok = {rid: sum(v.values()) for rid, v in union.items()}
    overall = u["overall_median_all_in"]
    for gid, g in groups.items():
        g["fix_share"] = round(sum(fix_tok.get(rid, 0) for rid in g["request_ids"]) / new_of[gid], 4) \
            if new_of[gid] else 0.0
        cands = [(fx, e) for fx, e in per_goal.get(gid, []) if e["saves_new_tokens"] > 0]
        g["top_fix"] = None
        if cands:
            fx, e = max(cands, key=lambda c: (c[1]["saves_new_tokens"], c[1]["tokens_in_goal"]))
            g["top_fix"] = {"id": fx["id"], "rule": fx["rule"], "title": fx["title"], "severity": fx["severity"],
                            "runs_affected": e["runs_affected"], "share": e["share_of_goal_tokens"],
                            "saves_per_run": e["saves_new_tokens"] / e["runs_affected"]}
        g["fixes_touching"] = len(per_goal.get(gid, []))
        g["status"], g["status_reason"] = goal_status(g, overall, g["fix_share"])
    u["fix_share_note"] = FIX_SHARE_NOTE
    return u


def split_sources(out, include_automated=False):
    """Keep human requests in s["requests"] (automated ones move to s["automated_requests"]) unless
    include_automated, or no human request exists at all. Returns (filter used, {source: requests})."""
    counts = Counter(r.get("source", "human") for s in out for r in s["requests"])
    for s in out:
        for src, n in (s.get("subagent_prompts") or {}).items():
            counts[src] += n
    if include_automated:
        used = "all (--include-automated)"
    elif not counts.get("human"):
        used = "all (no human requests found)"
    else:
        used = "human"
        for s in out:
            s["automated_requests"] = [r for r in s["requests"] if is_automated(r.get("source", "human"))]
            s["requests"] = [r for r in s["requests"] if not is_automated(r.get("source", "human"))]
    return used, {k: counts.get(k, 0) for k in REQUEST_SOURCES}


def understand(sessions, name_with_claude=False, namer_opts=None, goal_threshold=None, area_threshold=None,
               now=None, analyses=None, label_with_claude=False, include_automated=False, per_session=None,
               goal_unit="auto"):
    """Full understand data for loaded sessions. analyses: optional {session id: analyze_session result} to reuse.
    per_session: optional precomputed understand_session results (then `sessions` may be empty).

    Goals are discovered over the human requests of `sessions` together (all requests with include_automated;
    callers comparing sets pass the union). label_with_claude is the deprecated spelling of name_with_claude."""
    analyses = analyses or {}
    out = per_session if per_session is not None else \
        [understand_session(s, analysis=analyses.get(s.id), now=now) for s in sessions]
    source_filter, sources = split_sources(out, include_automated)
    disc = gl.discover([r for s in out for r in s["requests"]], goal_threshold, area_threshold, goal_unit)
    namer = {"used": "terms", "source": "terms"}
    if name_with_claude or label_with_claude:
        namer = gl.name_with_claude(disc, **(namer_opts or {}))
    tag_requests(out, disc)
    agg = aggregate(out, disc)
    agg["sessions"] = out
    agg["context"] = context_aggregate(out)
    agg["namer"] = namer
    agg["request_sources"] = sources
    agg["goal_request_filter"] = source_filter
    agg["goal_units"] = disc["units"]
    agg["source_rules"] = SOURCE_RULES
    return agg


# ---------------------------------------------------------------- text output

def replay(req, fmt_tok, max_steps=14):
    steps = req["steps"]
    shown = steps
    if len(steps) > max_steps:
        shown = steps[:max_steps - 4] + [None] + steps[-4:]
    parts = []
    for s in shown:
        if s is None:
            parts.append("… %d more …" % (len(steps) - max_steps + 3))
            continue
        lab = s["label"] if not is_hook(s) else "Hook(%s)%s" % (s["hook_name"], " " + test_text(s["tests"])
                                                                 if s["tests"] else "")
        parts.append("%s [%s%s]" % (lab, "~" if s["est"] else "", fmt_tok(s["tokens"])))
    body = " → ".join(parts) if parts else "(no tool steps)"
    return "ran %s  [%s, %d calls, %s]" % (body, req["outcome"], req["calls"], fmt_tok(req["tokens"]["all_in"]))


def to_text(u, fmt_tok, max_members=8):
    lines = []
    by_id = {r["id"]: r for s in u["sessions"] for r in s["requests"]}
    goals = {g["id"]: g for g in u["goals"]}
    m = u["goal_method"]
    lines.append("Goals: %d requests in %d sessions -> %d goals in %d areas  (%s)" % (
        u["requests"], len(u["sessions"]), len(u["goals"]), len(u["areas"]), gl.name_note(u["namer"])))
    gu = u.get("goal_units")
    if gu:
        lines.append("Goal unit: %s (%d threads; %d follow-ups folded; %d chores; areas by %s)" % (
            gu["unit"], gu["threads"], gu["follow_ups_folded"], gu["chores"], gu["area_rollup"]))
    if u.get("request_sources"):
        lines.append("Request sources: %s  (goals use: %s; --include-automated adds the rest)" % (
            ", ".join("%s %d" % kv for kv in u["request_sources"].items()), u.get("goal_request_filter")))
    lines.append("")
    w = max([len(_short(g["goal"], 60)) for g in u["goals"]] + [4])
    for a in u["areas"]:
        shared = ", ".join(f["path"] for f in a["shared_files"][:3]) or "-"
        lines.append("AREA %s  %s  (%d goals, %d requests; files: %s)" % (
            a["id"], a["name"], len(a["goals"]), a["members"], shared))
        for gid in a["goals"]:
            g = goals[gid]
            top = g["chains"][0]["chain"] if g["chains"] else "-"
            lines.append("  %-4s %-*s  %-8s %4d runs %4d%% %9s  %s" % (
                g["id"], w, _short(g["goal"], 60), g["status"], g["runs"], round(100 * g["finish_rate"]),
                fmt_tok(g["all_in"]["median"]), _short(top, 60)))
        lines.append("")
    lines.append("columns: id, goal, status, members, finish %, median all-in per run, top chain")
    if u["unhandled_requests"]:
        lines.append("")
        lines.append("%d requests got no tool steps and no answer: %s" % (
            len(u["unhandled_requests"]), ", ".join(u["unhandled_requests"][:8])))
    for g in u["goals"]:
        lines.append("")
        oc = ", ".join("%s %d" % (k, v) for k, v in sorted(g["outcomes"].items()))
        fl = ", ".join("%s %d" % (k, v) for k, v in sorted(g["flags"].items())) or "none"
        lines.append("== %s  %s  [%s: %s]" % (g["id"], g["goal"], g["status"], g["status_reason"]))
        lines.append("   area %s %s; %s" % (g["area"], g["area_name"], "named by " + g["name_source"]
                                           if g["name_source"] != "terms" else "named from terms"))
        lines.append("   runs %d in %d sessions; outcomes: %s; flags: %s" % (g["runs"], g["sessions"], oc, fl))
        lines.append("   all-in median %s (min %s, max %s); new median %s; median calls %s" % (
            fmt_tok(g["all_in"]["median"]), fmt_tok(g["all_in"]["min"]), fmt_tok(g["all_in"]["max"]),
            fmt_tok(g["new"]["median"]), _num(g["median_calls"])))
        coh = "-" if g["cohesion"] is None else "%.2f" % g["cohesion"]
        near = g["nearest"]
        lines.append("   cohesion %s; nearest other goal: %s" % (
            coh, "%s %s (similarity %.2f)" % (near["id"], near["name"], near["similarity"]) if near else "-"))
        if g["why"]:
            lines.append("   why grouped: %s" % ", ".join("%s %.2f" % (x["label"], x["contribution"])
                                                         for x in g["why"]))
        for key, lab in (("files_edited", "edited"), ("files_read", "read")):
            if g[key]:
                lines.append("   files %s: %s" % (lab, ", ".join("%s ×%d" % (f["path"], f["requests"])
                                                               for f in g[key][:6])))
        lines.append("   members:")
        prompts = OrderedDict()
        for mm in g["members"]:
            prompts.setdefault(mm["prompt"], []).append(mm)
        for i, (p, ms) in enumerate(prompts.items()):
            if i >= max_members:
                lines.append("     (+%d more distinct prompts)" % (len(prompts) - max_members))
                break
            oc = Counter(x["outcome"] for x in ms)
            lines.append("     ×%d %s  [%s]" % (len(ms), ", ".join(x["id"] for x in ms[:4]) +
                                               (" …" if len(ms) > 4 else ""),
                                               ", ".join("%s %d" % kv for kv in sorted(oc.items()))))
            lines.append("        \"%s\"" % (p or "(no prompt)"))
        lines.append("   paths (tool chains):")
        for ch in g["chains"][:3]:
            lines.append("   %5.1f%%  %s" % (ch["pct"], _short(ch["chain"], 160)))
            lines.append("           e.g. %s  %s" % (ch["example"], replay(by_id[ch["example"]], fmt_tok)))
        if len(g["chains"]) > 3:
            lines.append("           (+%d more chains)" % (len(g["chains"]) - 3))
    lines.append("")
    lines.append("Method: %s" % m["note"])
    lines.append("  weights %s; goal threshold %.2f, area threshold %.2f (area weights %s)" % (
        ", ".join("%s %.1f" % kv for kv in m["goal_weights"].items()), m["goal_threshold"], m["area_threshold"],
        ", ".join("%s %.1f" % kv for kv in m["area_weights"].items())))
    if m["ignored_files"]:
        lines.append("  ignored files: %s" % "; ".join("%s (%s)" % (x["path"], x["reason"])
                                                      for x in m["ignored_files"][:10]) +
                     (" (+%d more)" % (len(m["ignored_files"]) - 10) if len(m["ignored_files"]) > 10 else ""))
    lines.append("  generic task words dropped: %s" % " ".join(m["task_words"]))
    lines.append("Status rules: " + " | ".join(STATUS_RULES))
    lines.append("Step tokens: analyzer attribution of each tool result into the next call (~ = chars/4 estimate, "
                 "no ~ = scaled to the measured delta). Request tokens are measured.")
    return "\n".join(lines)


def _short(s, n):
    return s if len(s) <= n else s[:n - 1] + "…"


def _num(x):
    return ("%d" % x) if float(x).is_integer() else ("%.1f" % x)


def _bash_wrote(steps):
    """True if a Bash step wrote a repo file (redirect, tee, sed -i, python open(..., 'w')): not read-only."""
    from .goals import bash_write_targets
    for st in steps:
        if st.get("tool") == "Bash" and isinstance(st.get("detail"), str):
            if [t for t in bash_write_targets(st["detail"]) if not t.startswith("/dev/") and not t.startswith("/tmp/")]:
                return True
    return False
