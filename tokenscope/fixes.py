"""Rule-based fix suggestions, computed only from analyzer output (see analyze.analyze_session).

Two kinds of token savings are reported and never mixed:
  new      = tokens that would not have been written into context (fresh input / cache write)
  all-in   = new + carry, where carry = tokens re-sent as cache reads by every later call of the same
             agent context until compaction:  carry = added_tokens * later_calls_in_same_context

The headline is priced from those two (see pricing.py):
  cost-weighted = new x W_CACHE_WRITE + (all_in - new) x W_CACHE_READ   (= new x 1.25 + carry x 0.1)
             i.e. the saved new tokens are counted as cache writes (they were written once) and the carry as cache
             reads. When all_in < new (a cache miss: the re-write would have been a cache read instead) the
             negative carry correctly turns the saving into new x (1.25 - 0.1). Clamped at 0.
  savings_usd = new x 5m-cache-write price + (all_in - new) x cache-read price of the session's dominant model
             (the model with the most API calls, else the first of a["models"]); None when that model is
             unpriced. Subagents on another model are priced at the dominant model: an approximation.
Fixes are sorted by cost-weighted savings (ties: new tokens).
"""
import copy
import json
import os
import re
import shlex

from collections import Counter

from . import pricing
from .analyze import est_tokens, is_claude_md, later_calls, md_sections  # noqa: F401 (later_calls re-exported)
from .cli import fmt_tok

# ---------------------------------------------------------------- thresholds
LARGE_READ_TOKENS = 5_000          # one Read result
LARGE_FILE_TOTAL_TOKENS = 8_000    # all reads of one file
LARGE_BASH_TOKENS = 2_000          # one Bash result in context
MCP_MIN_LISTING_TOKENS = 150       # don't bother suggesting removal of tinier listings
SKILLS_MIN_UNUSED_TOKENS = 500     # aggregate unused skill listing per call
SKILLS_TOP = 8                     # skills named in the unused-skills fix
SUBAGENT_MIN_ALL_IN = 20_000       # subagent all-in tokens worth flagging
SUBAGENT_RATIO = 10                # all-in / returned-result ratio considered lopsided
CONTEXT_PEAK = 100_000
CONTEXT_JUMP_SHARE = 0.30          # one call adding this share of its context
COMPACT_SUMMARY_EST = 5_000        # assumed context of a /compact summary
SYSTEM_OVERHEAD_TOKENS = 20_000    # first-call context
TAIL_LINES = 40
LISTING_LINES = 100                # git ls-files | head -N suggested for listing commands
NOISY_HOOK_TOKENS = 2_000          # hook output attributed in one session
INSTR_TOTAL_TOKENS = 3_000         # all instruction files (CLAUDE.md + imports)
INSTR_IMPORT_TOKENS = 1_000        # one @-imported file worth loading on demand instead
INSTR_SECTION_TOKENS = 800         # one `## ` section of CLAUDE.md worth moving out
CAP_SCRIPT = ".claude/hooks/tokenscope-cap.py"
SEV_HIGH_ABS, SEV_HIGH_SHARE = 200_000, 0.10   # all-in savings (or new, if larger) vs session all-in
SEV_MED_ABS, SEV_MED_SHARE = 20_000, 0.02

ARTIFACT_DIRS = ("dist", "build", "node_modules", "vendor", "logs", "log", "out", "coverage", "target",
                 ".next", "__pycache__", ".venv", "venv")
ARTIFACT_EXT = (".log", ".min.js", ".min.css", ".map")
LOCKFILES = ("package-lock.json", "yarn.lock", "pnpm-lock.yaml", "poetry.lock", "Cargo.lock", "Gemfile.lock",
             "composer.lock", "go.sum", "uv.lock", "Pipfile.lock", "bun.lockb")
RUNNERS = ("npm", "npx", "yarn", "pnpm", "bun", "python", "python3", "node", "go", "cargo", "make", "bash", "sh",
           "uv", "poetry", "bundle", "mvn", "gradle", "./gradlew", "dotnet", "docker", "kubectl", "git")


def f(n):
    return format(int(round(n or 0)), ",")


def k(n):
    """Compact token count (same style as the rest of tokenscope)."""
    return fmt_tok(int(n or 0), m_digits=2)


# ---------------------------------------------------------------- data helpers

def contexts(a):
    """Agent contexts: [(agent_label, calls)], main first. Each call list is in API order."""
    out = [("main", a["calls"])]
    for s in a["subagents"]:
        out.append(("%s subagent" % (s["type"] or "?"), s["call_records"]))
    return out


def results(a):
    """Every tool result with where it entered context: dicts {r, call, ctx, pos, later, ref, main_index}.

    main_index: the main-session call it belongs to (a subagent's calls belong to the call that spawned it)."""
    parent = {id(c): s["parent_call_index"] for s in a["subagents"] for c in s["call_records"]}
    out = []
    for label, calls in contexts(a):
        for pos, c in enumerate(calls):
            for r in c.get("tool_results") or []:
                ref = "call #%d" % c["index"] if label == "main" else "%s call #%d" % (label, c["index"])
                out.append({"r": r, "call": c, "ctx": label, "pos": pos, "later": later_calls(calls, pos),
                            "ref": ref, "main_index": c["index"] if label == "main" else parent.get(id(c))})
    return out


def written_fraction(a):
    """Share of the first call's listings (CLAUDE.md, skills, MCP, deferred tools) that was actually written.

    Requests send tools + system prompt first, then the messages that carry the listings. A cache read on the first
    call (an identical prefix from an earlier session) covers that order, so only the written tail counts as new."""
    if not a["calls"]:
        return 0.0
    c = a["calls"][0]
    written = c["usage"]["input"] + c["usage"]["cache_write"]
    head = sum(x["tokens"] for x in c["attribution"]
               if x["cause"] == "system_overhead" and x["label"] in ("system prompt", "tool schemas"))
    msgs = c["context"] - head
    if msgs <= 0:
        return 1.0 if written else 0.0
    return min(1.0, written / msgs)


def touch(a, call_index, key, tokens, saves=None, listing=False):
    """Where a fix's tokens land: one main call of one session (listing: every request that carried it)."""
    return {"session_id": a["session_id"], "call_index": call_index, "key": key, "tokens": int(round(tokens)),
            "saves": int(round(tokens if saves is None else saves)), "listing": listing}


def listing_touch(a, key, tokens):
    t = tokens * written_fraction(a)
    return [touch(a, a["calls"][0]["index"], key, t, listing=True)] if a["calls"] else []


def carry_of(items):
    """items: [(tokens, later_calls)] -> (new, carry, formula parts)."""
    new = sum(t for t, _ in items)
    carry = sum(t * n for t, n in items)
    return new, carry


def formula(items):
    """'88,512 written + 88,512 x 7 later cache reads = 708,096' (several items joined with +)."""
    new, carry = carry_of(items)
    if len(items) == 1:
        t, n = items[0]
        return "%s written + %s x %d later cache reads = %s" % (f(t), f(t), n, f(t + t * n))
    w = " + ".join(f(t) for t, _ in items)
    c = " + ".join("%s x %d" % (f(t), n) for t, n in items if n)
    return "(%s) written + (%s) later cache reads = %s + %s = %s" % (w, c or "0", f(new), f(carry), f(new + carry))


def is_artifact(path):
    p = path.replace("\\", "/").lstrip("./")
    parts = p.split("/")
    base = parts[-1]
    if base in LOCKFILES:
        return "lockfile"
    if any(seg in ARTIFACT_DIRS for seg in parts[:-1]):
        return "generated/artifact directory"
    if base.endswith(ARTIFACT_EXT):
        return "log/minified/sourcemap file"
    return None


def deny_pattern(path):
    """Narrowest sensible permissions.deny Read() pattern for an artifact path."""
    p = path.replace("\\", "/")
    if os.path.isabs(p):
        return "Read(/%s)" % p.lstrip("/")
    p = p[2:] if p.startswith("./") else p
    parts = p.split("/")
    base = parts[-1]
    for i, seg in enumerate(parts[:-1]):
        if seg in ARTIFACT_DIRS:
            return "Read(./%s/**)" % "/".join(parts[:i + 1])
    if base in LOCKFILES:
        return "Read(./%s)" % p
    for ext in ARTIFACT_EXT:
        if base.endswith(ext):
            return "Read(./**/*%s)" % ext
    return "Read(./%s)" % p


def norm_command(cmd):
    """Group key for a shell command: its first program (+ subcommand for runners like npm/python)."""
    first = re.split(r"\s*(?:&&|\|\||;|\|)\s*", cmd.strip(), maxsplit=1)[0]
    words = [w for w in first.split() if not re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", w)]
    if not words:
        return cmd.strip()[:40]
    if words[0] in ("cd",) and len(words) > 1:
        rest = re.split(r"\s*(?:&&|;)\s*", cmd.strip(), maxsplit=1)
        return norm_command(rest[1]) if len(rest) > 1 else cmd.strip()[:40]
    if words[0] in RUNNERS and len(words) > 1:
        return " ".join(words[:2])
    return words[0]


def severity(all_in, new, session_all_in):
    v = max(all_in, new)
    base = session_all_in or 1
    if v >= SEV_HIGH_ABS or v >= SEV_HIGH_SHARE * base:
        return "high"
    if v >= SEV_MED_ABS or v >= SEV_MED_SHARE * base:
        return "med"
    return "low"


def dominant_model(a):
    """Model with the most API calls (main + subagents), else the first of a["models"], else None."""
    cnt = Counter(c.get("model") for c in list(a.get("calls") or []) +
                  [c for s in a.get("subagents") or [] for c in s.get("call_records") or []] if c.get("model"))
    if cnt:
        return max(cnt.items(), key=lambda kv: (kv[1], kv[0] == (a.get("models") or [None])[0]))[0]
    return (a.get("models") or [None])[0]


def cost_weighted_savings(new, all_in):
    """new tokens as cache writes + carry (all_in - new) as cache reads; see module docstring."""
    return round(max(0.0, new * pricing.W_CACHE_WRITE + (all_in - new) * pricing.W_CACHE_READ), 1)


def usd_savings(new, all_in, model):
    p = pricing.price_of(model)
    if p is None:
        return None
    return round(max(0.0, (new * p[1] + (all_in - new) * p[3]) / 1e6), 6)


def add_cost_fields(fx, a):
    """Fill savings_cost_weighted_tokens / savings_usd / savings_model from the token savings."""
    model = dominant_model(a)
    fx["savings_cost_weighted_tokens"] = cost_weighted_savings(fx["savings_new_tokens"], fx["savings_all_in_tokens"])
    fx["savings_usd"] = usd_savings(fx["savings_new_tokens"], fx["savings_all_in_tokens"], model)
    fx["savings_model"] = model
    return fx


def make_fix(a, rule, target, title, evidence, action, action_code, new, all_in, formula_text, confidence,
             config=None, sev=None, overlaps=None, label=None, touches=None):
    return add_cost_fields({
        "id": "%s:%s" % (rule, target), "rule": rule, "target": target, "target_label": label or target,
        "severity": sev or severity(all_in, new, a["totals"]["all"]["all_in"]),
        "title": title, "evidence": evidence, "action": action, "action_code": action_code,
        "savings_new_tokens": int(new), "savings_all_in_tokens": int(all_in), "est_savings_tokens": int(all_in),
        "savings_formula": formula_text, "confidence": confidence, "config": config or {},
        "overlaps": overlaps, "sessions": [a["session_id"]], "est": True, "touches": touches or [],
    }, a)


# ---------------------------------------------------------------- rules

def rule_large_file_read(a, res):
    by_file = {}
    for x in res:
        r = x["r"]
        if r["tool"] == "Read" and r["target"]:
            by_file.setdefault(r["target"], []).append(x)
    out = []
    for path, xs in by_file.items():
        tot = sum(x["r"]["attributed_tokens"] for x in xs)
        big = [x for x in xs if x["r"]["attributed_tokens"] >= LARGE_READ_TOKENS]
        if not big and tot < LARGE_FILE_TOTAL_TOKENS:
            continue
        items = [(x["r"]["attributed_tokens"], x["later"]) for x in xs if x["r"]["attributed_tokens"]]
        new, carry = carry_of(items)
        first = max(xs, key=lambda x: x["r"]["attributed_tokens"])
        if len(xs) == 1:
            title = "Reading %s cost %s tokens, then rode along in %d more call%s" % (
                path, k(new), first["later"], "" if first["later"] == 1 else "s")
        else:
            title = "%d reads of %s cost %s tokens, carried for %s more cache-read tokens" % (
                len(xs), path, k(new), k(carry))
        ev = []
        for x in xs:
            r = x["r"]
            note = []
            if r.get("scaled_to_measured") or r["attributed_tokens"] > r["est_tokens"] * 1.2:
                note.append("chars/4 = %s; measured cache write is higher" % f(r["est_tokens"]))
            if r["is_error"]:
                note.append("error")
            rng = ", ".join("%s %s" % kv for kv in zip(("offset", "limit"), r.get("range") or ()) if kv[1] is not None)
            ev.append("%s: Read %s%s — %s chars, %s tokens attributed, carried by %d later call%s%s" % (
                x["ref"], path, " (%s)" % rng if rng else "", f(r["chars"]), f(r["attributed_tokens"]), x["later"], "" if x["later"] == 1 else "s",
                " (%s)" % "; ".join(note) if note else ""))
        kind = is_artifact(path)
        cfg = {}
        if kind:
            pat = deny_pattern(path)
            ev.append("%s is a %s: rarely worth reading in full" % (path, kind) if kind != "generated/artifact directory"
                      else "%s lives in a generated/artifact directory: rarely worth reading in full" % path)
            action = ("Add to permissions.deny in .claude/settings.json so it can't be Read whole; search it "
                      "instead (grep for errors / the relevant ids, then Read with offset+limit).")
            code = '"permissions": { "deny": ["%s"] }' % pat
            if path.endswith(".log"):
                code += "\n# instead of reading it:\ngrep -nE 'ERROR|WARN|Exception' %s | tail -50" % path
            line = "- Never Read %s in full; grep it (e.g. `grep -n ERROR %s | tail -50`) and Read small ranges." % (
                pat[5:-1], path)
            cfg = {"deny": [pat], "claude_md": [line]}
        else:
            action = ("Locate what you need first (Grep for the symbol), then Read only that part with "
                      "offset/limit instead of the whole file.")
            code = 'Grep pattern="<symbol>" path="%s" -n\nRead file_path="%s" offset=<line-20> limit=80' % (path, path)
            cfg = {"claude_md": ["- %s is large: Grep it and Read with offset/limit (≈80 lines), never whole." % path]}
        out.append(make_fix(
            a, "large_file_read", path, title, ev, action, code, new, new + carry,
            formula(items),
            "Token counts are the measured cache writes attributed to the result; carry assumes the content would "
            "otherwise not have been in context at all. A targeted grep/partial read still costs a little.",
            config=cfg, touches=[touch(a, x["main_index"], x["r"].get("tool_use_id"), x["r"]["attributed_tokens"])
                                 for x in xs if x["r"]["attributed_tokens"]]))
    return out


def rule_repeated_reads(a, res):
    """Same file + same range read again, with no Edit/Write of that file in between (per agent context)."""
    out = {}
    parent = {id(c): s["parent_call_index"] for s in a["subagents"] for c in s["call_records"]}
    for label, calls in contexts(a):
        seen = {}
        for pos, c in enumerate(calls):
            for r in c.get("tool_results") or []:
                if r["tool"] == "Read" and r["target"] and not r["is_error"]:
                    key = tuple(r["range"]) if r.get("range") and any(v is not None for v in r["range"]) else None
                    prev = seen.setdefault(r["target"], set())
                    if key in prev or None in prev:
                        out.setdefault(r["target"], []).append(
                            (r, later_calls(calls, pos),
                             "call #%d" % c["index"] if label == "main" else "%s call #%d" % (label, c["index"]),
                             c["index"] if label == "main" else parent.get(id(c))))
                    prev.add(key)
            for tu in c.get("tool_uses") or []:
                if tu["name"] in ("Edit", "Write", "MultiEdit", "NotebookEdit"):
                    seen.pop(tu["target"], None)
    fixes = []
    for path, xs in out.items():
        items = [(r["attributed_tokens"], n) for r, n, _, _ in xs if r["attributed_tokens"]]
        if not items:
            continue
        new, carry = carry_of(items)
        reads = (a["per_file"].get(path) or {}).get("reads", len(xs) + 1)
        title = "%s was read %d times; the %d unchanged re-read%s added %s tokens" % (
            path, reads, len(xs), "" if len(xs) == 1 else "s", k(new))
        ev = ["%s: re-read %s — %s tokens, carried by %d later calls" % (ref, path, f(r["attributed_tokens"]), n)
              for r, n, ref, _ in xs]
        fixes.append(make_fix(
            a, "repeated_reads", path, title, ev,
            "The file hadn't changed since the last Read in this context. Add a CLAUDE.md line so earlier reads are "
            "reused; if the context was compacted in between, re-read only the needed range.",
            "- Don't re-Read files already read in this session unless they changed; reuse what you read.",
            new, new + carry, formula(items),
            "Counts only re-reads of the same range with no Edit/Write of that file in between; files regenerated "
            "by a Bash command in between can't be detected and would be false positives.",
            config={"claude_md": ["- Don't re-Read files already read in this session unless they changed."]},
            touches=[touch(a, mi, r.get("tool_use_id"), r["attributed_tokens"]) for r, _, _, mi in xs
                     if r["attributed_tokens"]]))
    return fixes


def rule_large_bash(a, res):
    groups, followups = {}, []
    for x in res:
        r = x["r"]
        if r["tool"] != "Bash":
            continue
        if "/tool-results/" in (r["target"] or ""):
            followups.append(x)
            continue
        if r["persisted"] or r["attributed_tokens"] >= LARGE_BASH_TOKENS:
            groups.setdefault(norm_command(r["target"]), []).append(x)
    # reading a persisted output back is part of the cost of the command that produced it
    for x in followups:
        prior = [(g, y) for g, ys in groups.items() for y in ys
                 if y["r"]["persisted"] and y["ctx"] == x["ctx"] and y["pos"] <= x["pos"]]
        if prior:
            g = max(prior, key=lambda gy: gy[1]["pos"])[0]
            x["followup"] = True
            groups[g].append(x)
    fixes = []
    for g, xs in groups.items():
        items = [(x["r"]["attributed_tokens"], x["later"]) for x in xs if x["r"]["attributed_tokens"]]
        new, carry = carry_of(items)
        runs = [x for x in xs if not x.get("followup")]
        fol = [x for x in xs if x.get("followup")]
        pers = [x for x in runs if x["r"]["persisted"]]
        if pers:
            full = sum(x["r"]["full_chars"] or 0 for x in pers)
            title = "%s printed %s chars (persisted to disk)%s; %s tokens entered context" % (
                g, f(full), "" if not fol else ", then %d follow-up read%s of the dump" % (
                    len(fol), "" if len(fol) == 1 else "s"), f(new))
        else:
            title = "%s output put %s tokens into context over %d run%s" % (g, k(new), len(runs),
                                                                           "" if len(runs) == 1 else "s")
        ev = []
        for x in xs:
            r = x["r"]
            extra = ""
            if r["persisted"] and r["full_chars"]:
                extra = "; full output %s chars (~%s tokens) persisted, only a preview entered context" % (
                    f(r["full_chars"]), f(est_tokens(r["full_chars"])))
            ev.append("%s: %s`%s` — %s chars in context, %s tokens, carried by %d later calls%s" % (
                x["ref"], "follow-up read of persisted output " if x.get("followup") else "",
                r["target"][:100], f(r["chars"]), f(r["attributed_tokens"]), x["later"], extra))
        cmd = runs[0]["r"]["target"] if runs else g
        base = cmd.split("|")[0].strip()
        touches = [touch(a, x["main_index"], x["r"].get("tool_use_id"), x["r"]["attributed_tokens"])
                   for x in xs if x["r"]["attributed_tokens"]]
        if is_listing(g):
            if g.startswith("git ls-files"):
                code = ("git ls-files src | head -%d   # one directory, capped\n"
                        'Glob pattern="src/**/*.js"    # or list by pattern' % LISTING_LINES)
            else:
                code = ("git ls-files | head -%d       # tracked files only, no build output\n"
                        "git ls-files src | head -%d   # one directory\n"
                        'Glob pattern="src/**/*.js"    # or list by pattern' % (LISTING_LINES, LISTING_LINES))
            line = listing_line([g])
            action = ("Scope the listing: tracked files only (git ls-files skips dist/, node_modules/, logs) or one "
                      "directory / file pattern, capped with head; say so in CLAUDE.md so it sticks.")
        else:
            test_like = re.search(r"test|spec|pytest|jest|check|lint|build", g, re.I)
            code = "%s 2>&1 | tail -%d" % (base, TAIL_LINES)
            if test_like:
                code += "\n# or only failures:\n%s 2>&1 | grep -E 'FAIL|Error|✗|not ok' | head -40" % base
            line = "- Run `%s` as `%s`%s; never print the full output." % (
                g, "%s 2>&1 | tail -%d" % (g, TAIL_LINES),
                " (add `| grep -E 'FAIL|Error'` for failures)" if test_like else "")
            action = ("Pipe the output through tail/grep (or use a quiet flag such as -q / --silent) so only the "
                      "summary enters context; say so in CLAUDE.md so it sticks.")
        fixes.append(make_fix(
            a, "large_bash_output", g, title, ev, action, code, new, new + carry, formula(items),
            "Upper bound: a filtered output still costs some tokens (~%d lines). %s" % (
                LISTING_LINES if is_listing(g) else TAIL_LINES,
                "Persisted outputs only put a preview in context; reading the dump back is counted "
                "as a follow-up." if pers else ""),
            config={"claude_md": [line]}, touches=touches))
    return fixes


LISTING_PROGRAMS = ("ls", "find", "tree", "fd", "du")
_LISTING_LINE_RE = re.compile(r"^- To see the repo layout use .*; don't dump whole trees with (.+)\.$")


def listing_line(cmds):
    """One CLAUDE.md line for directory-listing commands. Unscoped `git ls-files` is itself one of the dumps, so
    once it is involved the suggestion is a scoped, capped `git ls-files <dir>`."""
    git = any(c.startswith("git ls-files") for c in cmds)
    use = "git ls-files <dir> | head -%d" % LISTING_LINES if git else "git ls-files | head -%d" % LISTING_LINES
    names = [("a bare `%s`" if c == "git ls-files" else "`%s`") % c for c in cmds]
    return ("- To see the repo layout use `%s` or Glob with a pattern (e.g. `src/**/*.js`); don't dump whole trees "
            "with %s." % (use, " or ".join(names) if len(names) < 3 else ", ".join(names[:-1]) + " or " + names[-1]))


def hygiene_lines(lines):
    """Token hygiene lines with exact duplicates dropped and the per-command repo-layout lines (ls, find,
    git ls-files, ...) merged into one: [(line, [source lines])]."""
    out, listing = [], []
    for ln in lines:
        m = _LISTING_LINE_RE.match(ln)
        if m:
            cmds = re.findall(r"`([^`]+)`", m.group(1))
            if not listing:
                out.append(None)  # placeholder keeps the first listing line's position
            listing.append((ln, cmds))
        elif ln not in [x[0] for x in out if x]:
            out.append((ln, [ln]))
    if listing:
        cmds = []
        for _, cs in listing:
            cmds += [c for c in cs if c not in cmds]
        out[out.index(None)] = (listing_line(cmds), [ln for ln, _ in listing])
    return out


def is_listing(g):
    """True for directory-listing commands (grouped by norm_command: 'ls', 'find', 'git ls-files', ...)."""
    words = g.split()
    return bool(words) and (words[0] in LISTING_PROGRAMS or g.startswith("git ls-files"))


def _calls_main(a):
    return a["totals"]["main"]["calls"]


def rule_unused_mcp(a, res):
    o = a["offered_vs_used"]
    if not o.get("available"):
        return []
    n = _calls_main(a)
    out = []
    for e in o.get("mcp_servers") or []:
        if e["used"] or e["est_tokens"] < MCP_MIN_LISTING_TOKENS:
            continue
        name = e["name"]
        disp = e.get("display") or (name.replace("claude_ai_", "claude.ai ") if name.startswith("claude_ai_") else name)
        t = e["est_tokens"]
        if name.startswith("claude_ai_"):
            code = "# claude.ai connector: disable it for this project in /mcp,\n# or disconnect \"%s\" in " \
                   "claude.ai Settings > Connectors" % disp.replace("claude_ai_", "").replace("claude.ai ", "")
        else:
            code = "claude mcp remove %s\n# or keep it but disable it: /mcp -> %s -> Disable" % (name, name)
        tools = len(e.get("tools_offered") or [])
        out.append(make_fix(
            a, "unused_mcp", name,
            "MCP server %s was offered but never called: ~%s tokens per call x %d calls" % (disp, f(t), n),
            ["listing: ~%s tokens (%s%s) in the cached prefix" % (
                f(t), "%d tools; " % tools if tools else "", "server instructions + deferred tool names"),
             "used in this session: 0 calls; main session calls: %d" % n],
            "Disable or remove the server in projects that don't use it.", code,
            t, t * n, "~%s tokens per call x %d calls = %s (written once = %s new)" % (f(t), n, f(t * n), f(t)),
            "Estimate: listing size is chars/4 of the instructions and deferred tool names in the transcript.",
            sev="low" if t * n < SEV_MED_ABS else None, label=disp, touches=listing_touch(a, "listing:mcp:" + name, t)))
    return out


def rule_unused_skills(a, res):
    o = a["offered_vs_used"]
    if not o.get("available"):
        return []
    unused = sorted([e for e in o.get("skills") or [] if not e["used"]], key=lambda e: -e["est_tokens"])
    t = sum(e["est_tokens"] for e in unused)
    if t < SKILLS_MIN_UNUSED_TOKENS:
        return []
    n = _calls_main(a)
    top = unused[:SKILLS_TOP]
    plugins = {}
    for e in unused:
        if ":" in e["name"]:
            p = e["name"].split(":", 1)[0]
            plugins[p] = plugins.get(p, 0) + e["est_tokens"]
    code = []
    for p, v in sorted(plugins.items(), key=lambda kv: -kv[1]):
        code.append("/plugin  # disable plugin '%s' here if you don't need it (~%s tokens/call of skills)" % (p, f(v)))
    code.append("# personal skills: move unused ones out of ~/.claude/skills/ (or .claude/skills/ in the repo)")
    return [make_fix(
        a, "unused_skills", "skills",
        "%d of %d offered skills were never used: ~%s tokens of listing per call x %d calls" % (
            len(unused), len(o.get("skills") or []), f(t), n),
        ["largest unused: " + ", ".join("%s (~%s)" % (e["name"], f(e["est_tokens"])) for e in top)],
        "Trim the skill listing: disable plugins whose skills you don't use, remove unused personal skills.",
        "\n".join(code), t, t * n, "~%s tokens per call x %d calls = %s" % (f(t), n, f(t * n)),
        "Estimate from the skill_listing text (chars/4). Some built-in skills may not be removable.", sev="low",
        touches=listing_touch(a, "listing:skills", t))]


def rule_cache_misses(a, res):
    misses = a.get("cache_misses") or []
    gaps = [g for g in a.get("idle_gaps") or [] if not g["cache_miss"]]
    if not misses:
        return []
    tok = sum(m["tokens"] for m in misses)
    sub_parent = {s["agent_id"]: s["parent_call_index"] for s in a["subagents"]}
    expired = [m for m in misses if m["reason"] == "ttl_expired"]
    ev = ["%scall #%d: %s — %s tokens re-written%s" % (
        "" if not m.get("agent_id") else "subagent ", m["call_index"],
        "cache expired" if m["reason"] == "ttl_expired" else "cache dropped", f(m["tokens"]),
        " after idle %ds > TTL %ds" % (m["gap_seconds"], m["ttl_seconds"]) if m["reason"] == "ttl_expired" else "")
        for m in misses]
    if gaps:
        ev.append("%d other idle gap%s > 5 min without a miss" % (len(gaps), "" if len(gaps) == 1 else "s"))
    return [make_fix(
        a, "cache_misses", "session",
        "%d cache miss%s re-wrote %s tokens of context%s" % (
            len(misses), "" if len(misses) == 1 else "es", k(tok),
            " (%d after idle gaps longer than the cache TTL)" % len(expired) if expired else ""),
        ev,
        "Before a break, /compact (smaller prefix to re-write) or finish the task; start a fresh session for a new "
        "task instead of resuming a stale one.",
        "/compact   # before stepping away\n/clear     # when switching to an unrelated task",
        tok, 0,
        "%s = %s re-written tokens (new). All-in is unchanged: those tokens would have been cache reads "
        "(~0.1x price) instead of cache writes (1.25-2x)." % (" + ".join(f(m["tokens"]) for m in misses), f(tok)),
        "Measured: cache_read fell below half the previous context.",
        sev="med" if tok >= SEV_MED_ABS else "low",
        touches=[touch(a, m["call_index"] if not m.get("agent_id") else sub_parent.get(m["agent_id"]),
                       "miss:%s:%d" % (m.get("agent_id") or "main", m["call_index"]), m["tokens"]) for m in misses])]


def rule_subagent_fanout(a, res):
    out = []
    session_all = a["totals"]["all"]["all_in"] or 1
    by_tu = {x["r"].get("tool_use_id"): x for x in res}
    for s in a["subagents"]:
        t = s["tokens"]
        if t["all_in"] < SUBAGENT_MIN_ALL_IN:
            continue
        rx = by_tu.get(s.get("tool_use_id"))
        ret = rx["r"]["attributed_tokens"] if rx else 0
        new_sub = t["fresh_input"] + t["cache_write"] + t["output"]
        share = 100.0 * t["all_in"] / session_all
        typ = s["type"] or "subagent"
        if ret and t["all_in"] >= SUBAGENT_RATIO * ret:
            title = "%s subagent burned %s all-in to return a %s summary (%.0f%% of the session)" % (
                typ, k(t["all_in"]), k(ret), share)
        else:
            title = "%s subagent used %s all-in tokens (%.0f%% of the session) in %d calls" % (
                typ, k(t["all_in"]), share, s["calls"])
        ev = ["spawned by call #%s: \"%s\"" % (s["parent_call_index"], (s["description"] or "")[:80]),
              "%d calls, peak context %s, all-in %s (cache reads %s, cache writes %s, output %s)" % (
                  s["calls"], f(s["peak_context"]), f(t["all_in"]), f(t["cache_read"]), f(t["cache_write"]),
                  f(t["output"]))]
        if rx:
            ev.append("%s: result returned to main context = %s tokens" % (rx["ref"], f(ret)))
        sub_first = s["call_records"][0]["context"] if s["call_records"] else 0
        if sub_first:
            ev.append("its first call alone loaded %s tokens of system prompt/tools" % f(sub_first))
        out.append(make_fix(
            a, "subagent_fanout", "%s:%s" % (typ, s["agent_id"][:8]), title, ev,
            "For a small repo, a couple of targeted Grep/Glob calls answer the same question without a second "
            "context (which re-loads tools and system prompt). Spawn subagents only for broad searches.",
            "- Small repo: use Grep/Glob directly instead of spawning an Explore subagent.",
            max(0, new_sub - ret), max(0, t["all_in"] - ret),
            "%s subagent all-in - %s for a direct search returning similar info = %s (new: %s - %s = %s)" % (
                f(t["all_in"]), f(ret), f(max(0, t["all_in"] - ret)), f(new_sub), f(ret), f(max(0, new_sub - ret))),
            "Assumes a direct Grep/Read yields equivalent information for about the size of the summary.",
            config={"claude_md": ["- Small repo: use Grep/Glob directly instead of spawning an Explore subagent."]},
            touches=[touch(a, s["parent_call_index"], "sub:%s" % s["agent_id"], new_sub, max(0, new_sub - ret))]))
    return out


def rule_context_bloat(a, res, prior):
    calls = a["calls"]
    if not calls:
        return []
    peak = a["peak_context"]
    jump = None
    for pos, c in enumerate(calls[1:], 1):
        if c.get("compaction") or c.get("cache_miss") or not c["context"]:
            continue
        if c["new_tokens"] > CONTEXT_JUMP_SHARE * c["context"] and (jump is None or c["new_tokens"] > jump[1]["new_tokens"]):
            jump = (pos, c)
    if peak <= CONTEXT_PEAK and jump is None:
        return []
    if jump is not None:
        pos, c = jump
    else:
        pos, c = next((i, x) for i, x in enumerate(calls) if x["context"] > CONTEXT_PEAK)
    top = max(c["attribution"], key=lambda x: x["tokens"]) if c.get("attribution") else None
    base = calls[0]["context"]
    n = later_calls(calls, pos)
    cut = max(0, c["context"] - base - COMPACT_SUMMARY_EST)
    est = cut * n
    ev = ["peak context %s (main session)" % f(peak),
          "call #%d: context %s, +%s new (%.0f%% of its context)" % (
              c["index"], f(c["context"]), f(c["new_tokens"]), 100.0 * c["new_tokens"] / (c["context"] or 1))]
    if top:
        ev.append("biggest cause at call #%d: %s (%s tokens)" % (c["index"], top["label"], f(top["tokens"])))
    ev.append("%d later calls each re-read that context" % n)
    overlap = None
    if top and top.get("tool_use_id"):
        for p in prior:
            for e in p.get("_tool_use_ids", ()):
                if e == top["tool_use_id"]:
                    overlap = p
    formula_text = "(%s context at call #%d - %s first-call context - ~%s summary) x %d later calls = %s" % (
        f(c["context"]), c["index"], f(base), f(COMPACT_SUMMARY_EST), n, f(est))
    # whatever /compact would drop is mostly the same content other fixes already remove: don't count it twice
    others = [p for p in prior if p["rule"] in ("large_file_read", "repeated_reads", "large_bash_output", "noisy_hook")]
    counted = sum(p["savings_all_in_tokens"] for p in others)
    all_in = max(0, est - counted)
    if others:
        formula_text += "; minus %s already counted in %d other fix%s (%s) -> %s additional" % (
            f(counted), len(others), "" if len(others) == 1 else "es",
            ", ".join(p["id"] for p in others[:3]) + (", ..." if len(others) > 3 else ""), f(all_in))
    cause = top["label"] if top else "growth"
    title = "Context peaked at %s; call #%d alone added %s (%s)" % (k(peak), c["index"], k(c["new_tokens"]),
                                                                   cause[:60])
    return [make_fix(
        a, "context_bloat", "main", title, ev,
        "Remove the cause (see the related fix) or run /compact right after a big one-off result is no longer "
        "needed; start a fresh session (/clear) for the next task.",
        "/compact keep only the conclusions from the log analysis\n/clear    # new task, new session",
        0, all_in, formula_text,
        "Assumes /compact shrinks context to roughly the first-call size plus a ~%s-token summary." % f(
            COMPACT_SUMMARY_EST),
        overlaps=overlap["id"] if overlap else None,
        sev="med" if overlap else None)]


def rule_system_overhead(a, res):
    calls = a["calls"]
    if not calls or calls[0]["context"] <= SYSTEM_OVERHEAD_TOKENS:
        return []
    c = calls[0]
    comps = sorted([x for x in c["attribution"] if x["cause"] == "system_overhead"], key=lambda x: -x["tokens"])
    o = a["offered_vs_used"]
    reducible = 0
    if o.get("available"):
        reducible = o.get("mcp_servers_unused_est_tokens", 0) + o.get("skills_unused_est_tokens", 0)
    ev = ["%s: ~%s tokens%s" % (x["label"], f(x["tokens"]), " (%s)" % x["detail"] if x.get("detail") else "")
          for x in comps[:7]]
    return [make_fix(
        a, "system_overhead", "first_call",
        "First call loaded %s tokens before any work; biggest: %s" % (
            k(c["context"]), ", ".join("%s ~%s" % (x["label"], k(x["tokens"])) for x in comps[:3])),
        ev,
        "Tool schemas and the system prompt are fixed; the reducible parts are unused MCP servers and skills "
        "(see those fixes) and a long CLAUDE.md.",
        "claude mcp list   # review servers\n/plugin           # review plugins/skills\nwc -c CLAUDE.md",
        0, 0,
        "informational: %s of %s first-call tokens are listings of unused MCP servers/skills; savings are counted "
        "in the unused_mcp / unused_skills fixes" % (f(reducible), f(c["context"])),
        "Component sizes are chars/4 estimates of the transcript attachments; %s tokens are unattributed." % f(
            sum(x["tokens"] for x in comps if "unattributed" in x["label"])),
        sev="low")]


def wrap_hook_command(cmd, max_lines=TAIL_LINES):
    """Hook command wrapped by the cap script; the original is passed as one quoted arg and run by a shell."""
    return 'python3 "$CLAUDE_PROJECT_DIR"/%s --max-lines %d -- %s' % (CAP_SCRIPT, max_lines, shlex.quote(cmd))


def rule_noisy_hook(a, res):
    labels = {x["r"].get("tool_use_id"): x["r"]["label"] for x in res}
    sub_parent = {s["agent_id"]: s["parent_call_index"] for s in a["subagents"]}
    out = []
    for name, h in sorted((a.get("hooks") or {}).items()):
        if h["attributed_tokens"] < NOISY_HOOK_TOKENS:
            continue
        fs = [x for x in h["firings"] if x["attributed_tokens"]]
        items = [(x["attributed_tokens"], x["later_calls"]) for x in fs]
        # what a capped output would still cost: same tokens-per-char as measured, on the lines the cap keeps
        kept = [(int(round(x["attributed_tokens"] * x["capped_chars"] / (x["chars"] or 1))), x["later_calls"])
                for x in fs]
        new, carry = carry_of(items)
        knew, kcarry = carry_of(kept)
        ev = []
        for x, (kt, _) in zip(fs, kept):
            ref = "call #%d" % x["call_index"] if not x["agent_id"] else "subagent call #%d" % x["call_index"]
            after = labels.get(x["tool_use_id"])
            ev.append("%s: %s%s chars, %s tokens attributed, carried by %d later call%s; capped to %d lines it would "
                      "be ~%s chars (~%s tokens)" % (
                          ref, "after %s — " % after if after else "", f(x["chars"]), f(x["attributed_tokens"]),
                          x["later_calls"], "" if x["later_calls"] == 1 else "s", TAIL_LINES, f(x["capped_chars"]),
                          f(kt)))
        if h["attributed_tokens"] > h["est_tokens"] * 1.2:
            ev.append("chars/4 = %s; dense output (timestamps, ids) tokenizes worse, measured cache writes win" %
                      f(h["est_tokens"]))
        for c in h["commands"]:
            ev.append("command: %s" % c)
        wraps = [{"hook": name, "event": h["event"], "command": c} for c in h["commands"]]
        code = ("# .claude/settings.json: replace the hook's command with\n" +
                "\n".join('"command": %s' % json.dumps(wrap_hook_command(c)) for c in h["commands"] or ["<command>"]) +
                "\n# the cap script is written by: tokenscope fixes --emit-config DIR --overlay")
        out.append(make_fix(
            a, "noisy_hook", name,
            "Hook %s injected %s tokens of output over %d run%s (%s more as carry)" % (
                name, k(new), h["fires"], "" if h["fires"] == 1 else "s", k(carry)),
            ev,
            "Cap what the hook returns: wrap its command with tokenscope-cap.py, which keeps failure/error lines plus "
            "the last %d lines, keeps the hook JSON valid and preserves the exit code. Better still, make the hook "
            "report only pass/fail counts." % TAIL_LINES,
            code, new - knew, new + carry - knew - kcarry,
            "%s; minus capped output (%d lines) %s = %s" % (
                formula(items), TAIL_LINES, f(knew + kcarry), f(new + carry - knew - kcarry)),
            "Hook text is chars/4, scaled with the call's other inputs to the measured cache write. The capped size "
            "is measured on these outputs (the lines the cap script would keep).",
            config={"hook_wrap": wraps, "claude_md": [
                "- The %s hook output is capped (failures + last %d lines); for full detail run the command yourself "
                "piped through `| tail -%d` or `| grep -iE 'fail|error'`." % (name, TAIL_LINES, TAIL_LINES)]},
            touches=[touch(a, x["call_index"] if not x["agent_id"] else sub_parent.get(x["agent_id"]),
                           "hook:%s:%s" % (x["tool_use_id"], name), x["attributed_tokens"], x["attributed_tokens"] - kt)
                     for x, (kt, _) in zip(fs, kept)]))
    return out


def slugify(s):
    return re.sub(r"[^a-z0-9]+", "-", (s or "").lower()).strip("-")[:60] or "section"


def pointer_text(path):
    return "`%s` (not auto-loaded: Read %s only when you need it)" % (path, path)


def rule_instructions_bloat(a, res):
    ins = a.get("instructions")
    if not ins or ins["total_est_tokens"] < INSTR_TOTAL_TOKENS:
        return []
    n = _calls_main(a)
    reads = max(0, n - 1)
    contents = {e["rel"]: e.get("content") for e in ins["files"]}
    imports, sections, ev, used = [], [], [], set()
    for e in ins["files"]:
        if e["imported_by"] and e["est_tokens"] >= INSTR_IMPORT_TOKENS:
            t = e["est_tokens"]
            line = next((ln for ln in (contents.get(e["imported_by"]) or "").splitlines() if e["import_ref"] in ln), "")
            imports.append({"file": e["imported_by"], "ref": e["import_ref"], "path": e["rel"], "tokens": t,
                            "new": t, "all_in": t + t * reads, "line": line})
            ev.append("%s: ~%s tokens (%s chars), eagerly @-imported by %s (`%s`), so it sits in every call's prefix: "
                      "%s written + %s x %d later cache reads = %s" % (
                          e["rel"], f(t), f(e["chars"]), e["imported_by"], e["import_ref"], f(t), f(t), reads,
                          f(t + t * reads)))
    for sct in ins["sections"]:
        if sct["est_tokens"] >= INSTR_SECTION_TOKENS:
            slug = base = slugify(sct["heading"])
            i = 2
            while slug in used:
                slug, i = "%s-%d" % (base, i), i + 1
            used.add(slug)
            t = sct["est_tokens"]
            dest = "docs/claude/%s.md" % slug
            sections.append({"file": sct["file"], "heading": sct["heading"], "dest": dest, "tokens": t, "new": t,
                             "all_in": t + t * reads})
            ev.append("%s `## %s`: ~%s tokens (%s chars); move to %s with a pointer: %s written + %s x %d later cache "
                      "reads = %s" % (sct["file"], sct["heading"], f(t), f(sct["chars"]), dest, f(t), f(t), reads,
                                      f(t + t * reads)))
    if not imports and not sections:
        return []
    ev.append("instruction files total ~%s tokens: %s" % (f(ins["total_est_tokens"]), ", ".join(
        "%s ~%s" % (e["rel"], f(e["est_tokens"])) for e in ins["files"])))
    subs = imports + sections
    new = sum(x["new"] for x in subs)
    all_in = sum(x["all_in"] for x in subs)
    code = []
    for x in imports:
        code.append("# %s: load %s on demand instead of importing it" % (x["file"], x["path"]))
        if x["line"]:
            code.append("- " + x["line"])
            code.append("+ " + x["line"].replace(x["ref"], pointer_text(x["path"])))
    for x in sections:
        code.append("# %s: move \"## %s\" (~%s tokens) to %s, leave:" % (x["file"], x["heading"], k(x["tokens"]),
                                                                        x["dest"]))
        code.append("## %s\nMoved to %s." % (x["heading"], pointer_text(x["dest"])))
    names = [x["path"] for x in imports] + ["## " + x["heading"] for x in sections]
    return [make_fix(
        a, "instructions_bloat", "instructions",
        "Instruction files load %s tokens into every call; %s could load on demand (~%s per call)" % (
            k(ins["total_est_tokens"]), ", ".join(names[:3]) + (", ..." if len(names) > 3 else ""), k(new)),
        ev,
        "Turn eager @imports of big reference docs into plain pointers and move long CLAUDE.md sections "
        "(changelogs, reference tables) to docs/claude/, leaving a one-line pointer. Claude reads them when needed.",
        "\n".join(code), new, all_in,
        "estimate: (%s) tokens x (1 write + %d later cache reads) = %s x %d = %s" % (
            " + ".join(f(x["tokens"]) for x in subs), reads, f(new), reads + 1, f(all_in)),
        "Estimate: chars/4 of the instruction text in the transcript, main session calls only. Upper bound: when a "
        "task needs the moved text, Claude reads it once (and it is carried from then on).",
        config={"instr_imports": imports, "instr_sections": sections}, label="CLAUDE.md",
        touches=listing_touch(a, "listing:instructions", new))]


RULES = [rule_large_file_read, rule_repeated_reads, rule_large_bash, rule_unused_mcp, rule_unused_skills,
         rule_cache_misses, rule_subagent_fanout, rule_noisy_hook, rule_instructions_bloat]


def session_fixes(a):
    """All triggered fixes for one analyzed session, sorted by cost-weighted savings."""
    res = results(a)
    fixes = [fx for rule in RULES for fx in rule(a, res)]
    # remember which tool results each fix covers (for overlap detection)
    for fx in fixes:
        if fx["rule"] in ("large_file_read", "repeated_reads"):
            fx["_tool_use_ids"] = [x["r"].get("tool_use_id") for x in res
                                   if x["r"]["tool"] == "Read" and x["r"]["target"] == fx["target"]]
    fixes += rule_context_bloat(a, res, fixes)
    fixes += rule_system_overhead(a, res)
    for fx in fixes:
        fx.pop("_tool_use_ids", None)
    return sort_fixes(fixes)


SEV_ORDER = {"high": 0, "med": 1, "low": 2}


SOURCE_READ_NOTE = (" Not a generated/artifact path: reading it may have been needed for the task, so severity is "
                    "capped at medium; consider smaller files or offset/limit reads.")


def cap_source_reads(fx):
    """Reads of ordinary source files are often necessary: never rank them 'high'."""
    if fx["rule"] in ("large_file_read", "repeated_reads") and not is_artifact(fx.get("target") or ""):
        if fx["severity"] == "high":
            fx["severity"] = "med"
        if SOURCE_READ_NOTE not in (fx.get("confidence") or ""):
            fx["confidence"] = (fx.get("confidence") or "") + SOURCE_READ_NOTE
    return fx


def sort_fixes(fixes):
    fixes = [cap_source_reads(fx) for fx in fixes]
    return sorted(fixes, key=lambda x: (-cw_of(x), -x["savings_new_tokens"], -x["savings_all_in_tokens"],
                                        SEV_ORDER[x["severity"]]))


def cw_of(fx):
    v = fx.get("savings_cost_weighted_tokens")
    return cost_weighted_savings(fx["savings_new_tokens"], fx["savings_all_in_tokens"]) if v is None else v


def usd_txt(v):
    return "$%.2f" % v if v >= 0.01 or v == 0 else "$%.4f" % v


def savings_headline(fx):
    """'~15,000 new tokens, ~19,050 cost-weighted ($0.06 API-equivalent)'."""
    out = "~%s new tokens, ~%s cost-weighted" % (f(fx["savings_new_tokens"]), f(cw_of(fx)))
    usd = fx.get("savings_usd")
    if usd is not None:
        out += " (%s%s API-equivalent%s)" % ("≥" if fx.get("savings_usd_partial") else "", usd_txt(usd),
                                             ", %d unpriced session%s not counted" % (
                                                 fx["savings_usd_partial"], "" if fx["savings_usd_partial"] == 1
                                                 else "s") if fx.get("savings_usd_partial") else "")
    elif fx.get("savings_model") is not None or fx.get("savings_usd_partial"):
        out += " ($ unknown: %s unpriced)" % (fx.get("savings_model") or "model")
    return out


# ---------------------------------------------------------------- cross-session aggregation

AGG_LABEL = {
    "large_file_read": "large file read", "repeated_reads": "repeated reads", "large_bash_output": "large Bash output",
    "unused_mcp": "unused MCP server", "unused_skills": "unused skills", "cache_misses": "cache misses",
    "subagent_fanout": "subagent fan-out", "context_bloat": "context bloat", "system_overhead": "system overhead",
    "noisy_hook": "noisy hook", "instructions_bloat": "instruction files",
}


def aggregate(per_session, analyses):
    """per_session: {session_id: [fix]} -> merged fixes (same rule+target across sessions)."""
    used_mcp = set()
    for a in analyses:
        for e in (a["offered_vs_used"].get("mcp_servers") or []):
            if e["used"]:
                used_mcp.add(e["name"])
    total_all = sum(a["totals"]["all"]["all_in"] for a in analyses)
    groups = {}
    for sid, fixes in per_session.items():
        for fx in fixes:
            key = fx["id"] if fx["rule"] not in ("subagent_fanout",) else "subagent_fanout:" + fx["target"].split(":")[0]
            groups.setdefault(key, []).append(fx)
    out = []
    for key, fs in groups.items():
        rule = fs[0]["rule"]
        if rule == "unused_mcp" and fs[0]["target"] in used_mcp:
            continue  # used in some session: not a removal candidate
        if len(fs) == 1:
            fx = dict(fs[0])
            fx["evidence"] = ["session %s: %s" % (fx["sessions"][0][:8], e) for e in fx["evidence"]]
            out.append(fx)
            continue
        new = sum(x["savings_new_tokens"] for x in fs)
        alli = sum(x["savings_all_in_tokens"] for x in fs)
        cwt = round(sum(cw_of(x) for x in fs), 1)
        usds = [x.get("savings_usd") for x in fs]
        known = [u for u in usds if u is not None]
        # honest sum: only priced sessions are added; savings_usd_partial = number of unpriced sessions left out
        usd = round(sum(known), 6) if known else None
        partial = (len(usds) - len(known)) if known else 0
        sids = [x["sessions"][0] for x in fs]
        tgt = fs[0].get("target_label", fs[0]["target"]) if rule not in ("cache_misses", "context_bloat", "system_overhead", "unused_skills") \
            else ""
        title = "%s%s in %d sessions: %s new, %s cost-weighted" % (
            tgt + ": " if tgt else "", AGG_LABEL.get(rule, rule), len(fs), k(new), k(cwt))
        if rule == "context_bloat":
            title = "Context ballooned in %d sessions; /compact or a fresh session would save ~%s cost-weighted " \
                    "(%s all-in) beyond the other fixes" % (len(fs), k(cwt), k(alli))
        if rule in ("system_overhead",):
            title = "First calls loaded >%s tokens in %d sessions before any work" % (
                k(SYSTEM_OVERHEAD_TOKENS), len(fs))
        ev = ["session %s: %s (%s new / %s all-in)" % (x["sessions"][0][:8], x["title"], f(x["savings_new_tokens"]),
                                                       f(x["savings_all_in_tokens"])) for x in fs]
        cfg = merge_config([x["config"] for x in fs])
        best = max(fs, key=lambda x: (cw_of(x), x["savings_all_in_tokens"]))
        models = sorted({str(x.get("savings_model")) for x in fs if x.get("savings_usd") is None})
        out.append({
            "id": key, "rule": rule, "target": fs[0]["target"],
            "target_label": fs[0].get("target_label", fs[0]["target"]), "severity": severity(alli, new, total_all),
            "title": title, "evidence": ev, "action": best["action"], "action_code": best["action_code"],
            "savings_new_tokens": new, "savings_all_in_tokens": alli, "est_savings_tokens": alli,
            "savings_cost_weighted_tokens": cwt, "savings_usd": usd, "savings_usd_partial": partial,
            "savings_model": best.get("savings_model") if not models else ", ".join(models),
            "savings_formula": "sum over sessions: all-in %s = %s; new %s = %s" % (
                " + ".join(f(x["savings_all_in_tokens"]) for x in fs), f(alli),
                " + ".join(f(x["savings_new_tokens"]) for x in fs), f(new)),
            "confidence": best["confidence"], "config": cfg, "overlaps": None, "sessions": sids, "est": True,
            "touches": [t for x in fs for t in x.get("touches") or []],
        })
    return sort_fixes(out)


def config_key(ck, v):
    """Identity of one config value: the same CLAUDE.md change from several sessions is one change."""
    if ck == "instr_imports":
        return json.dumps(["imp", v["file"], v["ref"]])
    if ck == "instr_sections":
        return json.dumps(["sec", v["file"], v["heading"]])
    return json.dumps(v, sort_keys=True)


def merge_config(configs):
    """Union of fix configs; instruction moves seen in several sessions are merged with their savings summed."""
    cfg, seen = {}, {}
    for c in configs:
        for ck, vals in c.items():
            for v in vals:
                key = config_key(ck, v)
                if key in seen.setdefault(ck, {}):
                    old = seen[ck][key]
                    if ck in ("instr_imports", "instr_sections"):
                        old["new"] += v["new"]
                        old["all_in"] += v["all_in"]
                        old["sessions"] = old.get("sessions", 1) + v.get("sessions", 1)
                    continue
                v = dict(v) if isinstance(v, dict) else v
                seen[ck][key] = v
                cfg.setdefault(ck, []).append(v)
    return cfg


# ---------------------------------------------------------------- goals (filled by understand.apply_fixes)

LISTING_RULES = ("unused_mcp", "unused_skills", "instructions_bloat")


def cause_noun(fx):
    t = fx.get("target_label") or fx.get("target") or ""
    return {"noisy_hook": "hook output", "large_file_read": "reads of %s" % t, "repeated_reads": "re-reads of %s" % t,
            "large_bash_output": "`%s` output" % t, "unused_mcp": "the %s listing" % t,
            "unused_skills": "the unused skill listing", "instructions_bloat": "the movable CLAUDE.md text",
            "cache_misses": "cache re-writes", "subagent_fanout": "the subagent"}.get(fx["rule"], fx["rule"])


def _share(x):
    return "<1%" if 0 < x < 0.005 else "%d%%" % round(100 * x)


def goal_line(fx, others=2):
    """'Most of this lands in goal "Add SAVE10 discount code": hook output = 41% of its new tokens (3 runs)' or
    None."""
    its = [e for e in fx.get("goals") or [] if e["runs_affected"]]
    if not its:
        return None
    top = its[0]
    runs = lambda e: "%d run%s" % (e["runs_affected"], "" if e["runs_affected"] == 1 else "s")
    lead = "Most of this lands in" if len(its) > 1 else "All of this lands in"
    out = '%s goal "%s": %s = %s of its new tokens (%s)' % (lead, top["goal"], cause_noun(fx),
                                                       _share(top["share_of_goal_tokens"]), runs(top))
    if len(its) > 1:
        out += "; also " + ", ".join('"%s" %s (%s)' % (e["goal"], _share(e["share_of_goal_tokens"]), runs(e))
                                     for e in its[1:1 + others])
        if len(its) > 1 + others:
            out += ", +%d more" % (len(its) - 1 - others)
    if fx["rule"] in LISTING_RULES:
        out += ". Listings count where they were written, not re-read from cache"
    return out


# ---------------------------------------------------------------- output

SAVINGS_NOTE = ("  sorted by cost-weighted savings = saved new tokens x%g (cache write) + carry x%g (cache reads); "
                "$ = API-equivalent at the session model's list price (%s)" % (
                    pricing.W_CACHE_WRITE, pricing.W_CACHE_READ, re.sub(r"^\w+://", "", pricing.PRICES_SOURCE)))


def to_text(fixes, header=""):
    lines = []
    if header:
        lines.append(header)
        if fixes:
            lines.append(SAVINGS_NOTE)
    if not fixes:
        lines.append("  no fixes triggered")
        return "\n".join(lines)
    for i, fx in enumerate(fixes, 1):
        lines.append("")
        lines.append("%d. [%s] %s" % (i, fx["severity"].upper(), fx["title"]))
        lines.append("   rule: %s   saves %s" % (fx["rule"], savings_headline(fx)))
        lines.append("   all-in incl. carry: ~%s" % f(fx["savings_all_in_tokens"]))
        lines.append("   how:  %s" % fx["savings_formula"])
        il = goal_line(fx)
        if il:
            lines.append("   where: %s" % il)
        lines.append("   do:   %s" % fx["action"])
        for ln in fx["action_code"].splitlines():
            lines.append("         | %s" % ln)
        for e in fx["evidence"]:
            lines.append("   - %s" % e)
        lines.append("   note: %s" % fx["confidence"])
    return "\n".join(lines)


def emit_config(fixes, d):
    """Write CLAUDE.md.suggested and settings.suggested.json into directory d. Returns paths."""
    deny, md = [], []
    for fx in fixes:
        for v in fx["config"].get("deny", []):
            if v not in deny:
                deny.append(v)
        for v in fx["config"].get("claude_md", []):
            if v not in md:
                md.append(v)
    os.makedirs(d, exist_ok=True)
    p1 = os.path.join(d, "settings.suggested.json")
    with open(p1, "w", encoding="utf-8") as fh:
        json.dump({"permissions": {"deny": deny}}, fh, indent=2)
        fh.write("\n")
    p2 = os.path.join(d, "CLAUDE.md.suggested")
    with open(p2, "w", encoding="utf-8") as fh:
        fh.write("# Suggested by tokenscope - review, then merge into CLAUDE.md by hand\n\n")
        fh.write("## Token hygiene\n\n")
        md = [ln for ln, _ in hygiene_lines(md)]
        fh.write("\n".join(md) + "\n" if md else "(no suggestions)\n")
    return p1, p2


# ---------------------------------------------------------------- overlay (files at real repo paths)

OVERLAY_RULES = ("noisy_hook", "instructions_bloat")


def _safe_path(d, rel):
    """Absolute path for repo-relative `rel` inside d; refuses anything that would land outside d."""
    root = os.path.realpath(d)
    p = os.path.realpath(os.path.join(root, rel))
    if os.path.isabs(rel) or os.path.commonpath([root, p]) != root:
        raise ValueError("refusing to write outside %s: %s" % (d, rel))
    return p


def _merged_config(fixes):
    cfg, owners = merge_config([fx["config"] for fx in fixes]), {}
    for fx in fixes:
        for ck, vals in fx["config"].items():
            for v in vals:
                own = owners.setdefault(ck, {}).setdefault(config_key(ck, v), [])
                if fx not in own:
                    own.append(fx)
    return cfg, owners


def _replace_import(text, ref, path):
    return re.sub(re.escape(ref) + r"(?![\w./-])", lambda m: pointer_text(path), text)


def _move_sections(text, moves):
    """moves: [{heading, dest}] -> (new text, {dest: moved section text})."""
    by_head = {m["heading"]: m for m in moves}
    moved = {}
    for h, start, end in sorted(md_sections(text), key=lambda x: -x[1]):
        m = by_head.get(h)
        if not m or m["dest"] in moved:
            continue
        body = text[start:end]
        moved[m["dest"]] = "# %s\n%s" % (h, body.split("\n", 1)[1] if "\n" in body else "")
        tail = "\n\n" if body.endswith("\n\n") else "\n"
        text = text[:start] + "## %s\nMoved to %s.%s" % (h, pointer_text(m["dest"]), tail) + text[end:]
    return text, moved


def _ids(fxs):
    return ", ".join(sorted({fx["id"] for fx in fxs}))


def _saves(x):
    n = x.get("sessions", 1)
    return "~%s cost-weighted / ~%s new / ~%s all-in%s" % (
        f(cost_weighted_savings(x["new"], x["all_in"])), f(x["new"]), f(x["all_in"]), " (summed over %d sessions)" % n if n > 1 else "")


def hook_tool(hook_name):
    """'PostToolUse:Write' -> 'Write' (None for hooks without a tool, e.g. 'Stop')."""
    return hook_name.split(":", 1)[1] if ":" in (hook_name or "") else None


def matcher_covers(matcher, tool):
    """Claude Code hook matchers are regexes over the tool name ('Edit|Write|MultiEdit'); empty or '*' = all."""
    if tool is None or matcher in (None, "", "*"):
        return True
    try:
        return re.fullmatch(matcher, tool) is not None
    except re.error:
        return tool in matcher.split("|")


def build_overlay(fixes, a, max_lines=TAIL_LINES):
    """Overlay files {repo-relative path: text} + change log, computed without writing anything.

    Reads the transcript's CLAUDE.md content and (read-only) the repo's .claude/settings.json."""
    cfg, owners = _merged_config(fixes)
    cwd = a.get("cwd") or ""
    ins = a.get("instructions") or {"files": []}
    files, changes, manual = {}, [], []

    def change(path, text, fxs=(), saves=None):
        changes.append((path, text, _ids(fxs) if fxs else "", saves))

    def owner(ck, v):
        return owners.get(ck, {}).get(config_key(ck, v), [])

    # ---- CLAUDE.md files (only ones inside the repo; content comes from the transcript)
    md_files = [e for e in ins["files"] if "content" in e]
    root_md = next((e for e in md_files if e["in_repo"] and e["rel"] in ("CLAUDE.md", ".claude/CLAUDE.md")), None)
    for e in md_files:
        imps = [x for x in cfg.get("instr_imports", []) if x["file"] == e["rel"]]
        secs = [x for x in cfg.get("instr_sections", []) if x["file"] == e["rel"]]
        if not e["in_repo"]:
            for x in imps + secs:
                manual.append(("%s is outside the repo: %s by hand" % (
                    e["path"], "replace `%s` with a pointer" % x["ref"] if "ref" in x else
                    "move `## %s` out" % x["heading"]), owner("instr_imports" if "ref" in x else "instr_sections", x)))
            continue
        if not imps and not secs and e is not root_md:
            continue
        text = e["content"]
        for x in imps:
            text = _replace_import(text, x["ref"], x["path"])
            change(e["rel"], "Replaced the eager import `%s` (~%s tokens) with a plain pointer, so %s is read only "
                   "when needed" % (x["ref"], f(x["tokens"]), x["path"]), owner("instr_imports", x), _saves(x))
        text, moved = _move_sections(text, secs)
        for x in secs:
            if x["dest"] in moved:
                files[x["dest"]] = moved[x["dest"]]
                change(e["rel"], "Moved `## %s` (~%s tokens) to %s, leaving a one-line pointer" % (
                    x["heading"], f(x["tokens"]), x["dest"]), owner("instr_sections", x), _saves(x))
                change(x["dest"], "New: the `## %s` section moved out of %s" % (x["heading"], e["rel"]),
                       owner("instr_sections", x))
        files[e["rel"]] = text.rstrip("\n") + "\n"
    done = {e["rel"] for e in md_files}
    for ck in ("instr_imports", "instr_sections"):
        for x in cfg.get(ck, []):
            if x["file"] not in done:
                manual.append(("%s: %s by hand (its content is not in this session's transcript)" % (
                    x["file"], "replace `%s` with a pointer" % x["ref"] if ck == "instr_imports" else
                    "move `## %s` to %s" % (x["heading"], x["dest"])), owner(ck, x)))
    # ---- Token hygiene lines
    md_lines = cfg.get("claude_md", [])
    if md_lines:
        target = root_md["rel"] if root_md else "CLAUDE.md"
        text = files.get(target)
        if text is None:
            disk = os.path.join(cwd, "CLAUDE.md") if cwd else None
            text = ""
            if disk and os.path.isfile(disk):
                with open(disk, encoding="utf-8") as fh:
                    text = fh.read()
        merged = [(ln, src) for ln, src in hygiene_lines(md_lines) if ln not in text]
        new_lines = [ln for ln, _ in merged]
        if new_lines:
            text = text.rstrip("\n") + ("\n\n" if text.strip() else "") + "## Token hygiene\n\n" + \
                "\n".join(new_lines) + "\n"
            files[target] = text
            fxs = []
            for _, src in merged:
                for fx in (f for ln in src for f in owner("claude_md", ln)):
                    if fx not in fxs:
                        fxs.append(fx)
            change(target, 'Added "## Token hygiene" with %d line%s' % (len(new_lines), "" if len(new_lines) == 1
                                                                        else "s"), fxs)
    # ---- .claude/settings.json (existing repo settings, read-only, + deny + capped hooks)
    deny, wraps = cfg.get("deny", []), cfg.get("hook_wrap", [])
    if deny or wraps:
        src = os.path.join(cwd, ".claude", "settings.json") if cwd else None
        base, ok = {}, True
        if src and os.path.isfile(src):
            try:
                with open(src, encoding="utf-8") as fh:
                    base = json.load(fh)
                if not isinstance(base, dict):
                    raise ValueError("not an object")
            except (OSError, ValueError) as err:
                ok = False
                manual.append(("%s could not be parsed (%s): add the deny entries / hook wrappers by hand" % (
                    src, err), [fx for v in deny for fx in owner("deny", v)] +
                    [fx for v in wraps for fx in owner("hook_wrap", v)]))
        if ok:
            merged = copy.deepcopy(base)
            if deny:
                cur = merged.setdefault("permissions", {}).setdefault("deny", [])
                add = [v for v in deny if v not in cur]
                cur.extend(add)
                if add:
                    change(".claude/settings.json", "permissions.deny += %s" % ", ".join("`%s`" % v for v in add),
                           [fx for v in add for fx in owner("deny", v)])
            wrapped = set()
            for event, groups in (merged.get("hooks") or {}).items():
                for g in groups if isinstance(groups, list) else []:
                    if not isinstance(g, dict):
                        continue
                    for hk in g.get("hooks") or []:
                        cmd = hk.get("command") if isinstance(hk, dict) else None
                        if not cmd:
                            continue
                        hit = [w for w in wraps if w["event"] in (event, None) and
                               matcher_covers(g.get("matcher"), hook_tool(w["hook"])) and
                               (cmd == w["command"] or (CAP_SCRIPT in cmd and shlex.quote(w["command"]) in cmd))]
                        if not hit:
                            continue
                        wrapped.update(json.dumps(w, sort_keys=True) for w in hit)
                        fx = [x for x in fixes if any(x in owner("hook_wrap", w) for w in hit)]
                        names = ", ".join(sorted({w["hook"] for w in hit}))
                        matcher = " (matcher `%s`)" % g["matcher"] if g.get("matcher") else ""
                        if CAP_SCRIPT in cmd:
                            change(".claude/settings.json", "%s hook `%s`%s already runs through tokenscope-cap.py: "
                                   "nothing to change" % (event, hit[0]["command"], matcher), fx)
                            continue
                        hk["command"] = wrap_hook_command(cmd, max_lines)
                        change(".claude/settings.json", "Wrapped %s hook `%s`%s with tokenscope-cap.py (--max-lines "
                               "%d); covers %s" % (event, cmd, matcher, max_lines, names), fx,
                               "~%s cost-weighted / ~%s new / ~%s all-in" % (
                                   f(sum(cw_of(x) for x in fx)), f(sum(x["savings_new_tokens"] for x in fx)),
                                   f(sum(x["savings_all_in_tokens"] for x in fx))))
            for w in wraps:
                if json.dumps(w, sort_keys=True) not in wrapped:
                    manual.append(("Hook %s (`%s`) is not in the repo's .claude/settings.json (settings.local.json or "
                                   "~/.claude/settings.json?): change its command to `%s` by hand" % (
                                       w["hook"], w["command"], wrap_hook_command(w["command"], max_lines)),
                                   owner("hook_wrap", w)))
            if merged != base:
                files[".claude/settings.json"] = json.dumps(merged, indent=2, ensure_ascii=False) + "\n"
            if wraps:
                from . import cap
                with open(cap.__file__, encoding="utf-8") as fh:
                    files[CAP_SCRIPT] = fh.read()
                change(CAP_SCRIPT, "New: runs the hook command, keeps failure/error lines + the last N lines of its "
                       "output (JSON additionalContext or plain stdout), preserves the exit code",
                       [fx for v in wraps for fx in owner("hook_wrap", v)])
    # ---- fixes that live outside the repo or need judgement
    for fx in fixes:
        if fx["rule"] in OVERLAY_RULES or fx["config"].get("deny") or fx["config"].get("claude_md"):
            continue
        manual.append(("[%s] %s — %s" % (fx["severity"], fx["title"], fx["action"]), [fx]))
    return files, changes, manual


def _utc(iso):
    return (iso or "?")[:16].replace("T", " ")


def source_text(a, analyses=None):
    """'session <id> (project, cwd X)' or 'N sessions in project X, <first start> – <last end> UTC (cwd X)'."""
    analyses = analyses or [a]
    cwd = a.get("cwd") or "?"
    if len(analyses) == 1:
        return "session %s (%s, cwd `%s`)" % (a["session_id"], a["project"], cwd)
    projects = sorted({x["project"] for x in analyses})
    lo = min((x["start"] for x in analyses if x["start"]), default=None)
    hi = max((x["end"] or x["start"] for x in analyses if x["end"] or x["start"]), default=None)
    rng = _utc(lo) + " – " + (_utc(hi)[11:] if _utc(hi)[:10] == _utc(lo)[:10] else _utc(hi))
    return "%d sessions in project %s, %s UTC (cwd `%s`)" % (len(analyses), ", ".join(projects), rng, cwd)


def changes_md(a, fixes, files, changes, manual, analyses=None):
    lines = ["# tokenscope changes", "",
             "Generated by `tokenscope fixes --emit-config DIR --overlay` from %s." % source_text(a, analyses),
             "Files are at their real repo-relative paths: review them, then `cp -r DIR/. <repo>`. "
             "Nothing in the repo was modified.",
             "Savings are estimates (chars/4, scaled to measured cache writes where possible); all-in includes carry "
             "(the same tokens re-read as cache reads by every later call).", "", "## Changes", ""]
    if not changes:
        lines.append("(none)")
    for path in sorted({c[0] for c in changes}, key=lambda p: [c[0] for c in changes].index(p)):
        lines.append("### %s%s" % (path, "" if path in files else " (not written)"))
        for p, text, ids, saves in changes:
            if p == path:
                lines.append("- %s%s%s" % (text, " — fix `%s`" % ids if ids else "",
                                            " — est. saves %s" % saves if saves else ""))
        lines.append("")
    lines += ["## Not applied automatically", ""]
    if not manual:
        lines.append("(nothing)")
    for text, fxs in manual:
        top = max(fxs, key=cw_of) if fxs else None
        lines.append("- %s%s%s" % (text, " — fix `%s`" % _ids(fxs) if fxs else "",
                                    " — est. saves %s; ~%s all-in incl. carry" % (
                                        savings_headline(top), f(top["savings_all_in_tokens"]))
                                    if top and (top["savings_all_in_tokens"] or top["savings_new_tokens"]) else ""))
        for fx in fxs[:1]:
            if fx["rule"] not in OVERLAY_RULES and fx["action_code"]:
                lines += ["", "  ```", *["  " + ln for ln in fx["action_code"].splitlines()], "  ```"]
    lines.append("")
    return "\n".join(lines)


def emit_overlay(fixes, a, d, max_lines=TAIL_LINES, analyses=None):
    """Write the overlay into d (never outside it; never touches the repo). Returns written paths.

    a: the latest session (its CLAUDE.md content and cwd are used); analyses: all sessions the fixes came from."""
    files, changes, manual = build_overlay(fixes, a, max_lines)
    files["TOKENSCOPE_CHANGES.md"] = changes_md(a, fixes, files, changes, manual, analyses)
    targets = {rel: _safe_path(d, rel) for rel in files}  # validate every path before writing any
    out = []
    for rel, p in targets.items():
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as fh:
            fh.write(files[rel])
        if rel == CAP_SCRIPT:
            os.chmod(p, 0o755)
        out.append(p)
    return out
