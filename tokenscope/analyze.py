"""Turn parsed transcripts into per-API-call records with token attribution and session aggregates.

All token figures from `usage` are measured. Anything derived from text length (chars/4) is an
estimate and is flagged with est=True.
"""
import json
import os
import re

from . import pricing
from .cap import cap_text
from .parse import content_text, parse_ts, real_prompt_text

CHARS_PER_TOKEN = 4
TTL_5M = 300
TTL_1H = 3600
CACHE_DROP_RATIO = 0.5      # cache_read below this fraction of previous context => miss
COMPACT_DROP_RATIO = 0.5    # context below this fraction of previous context => compaction
SCALE_UP_MIN = 2000         # unexplained residual (tokens) above which tool results absorb it
BIG_BASH_TOKENS = 2500
IMAGE_TOKENS_EST = 1600
# hook events whose plain (non-JSON) stdout is added to Claude's context
STDOUT_CONTEXT_EVENTS = ("UserPromptSubmit", "SessionStart")

INTENT_RULES = [  # priority order: first rule with a match wins; whole words only
    ("bugfix", ["fix", "fixing", "bug", "bugs", "error", "errors", "fail", "fails", "failing", "failed", "failure",
                "broken", "crash", "crashes", "crashing", "wrong"]),
    ("feature", ["add", "adding", "implement", "implementing", "build", "building", "create", "creating", "support"]),
    ("refactor", ["refactor", "refactoring", "clean", "cleanup", "rename", "restructure"]),
    ("docs", ["doc", "docs", "documentation", "readme", "comment", "comments"]),
    ("exploration", ["explain", "how", "why", "what", "explore", "understand", "investigate"]),
    ("review", ["review", "pr"]),
]
INTENT_PROMPT_CHARS = 300   # only the start of the prompt states the task; long prompts mention everything


def _kw_regex(kw):
    return re.compile(r"\b%s\b" % re.escape(kw), re.I)


_INTENT_RES = [(intent, [(kw, _kw_regex(kw)) for kw in kws]) for intent, kws in INTENT_RULES]


def classify_intent(prompt):
    """Coarse session-level kind of work from keyword rules on the first prompt (listings only; goals are
    discovered from the data in goals.py)."""
    return _classify_intent_legacy(prompt)


def _classify_intent_legacy(prompt):
    if not prompt:
        return {"intent": "other", "keyword": None, "est": True}
    for intent, rules in _INTENT_RES:
        for kw, rx in rules:
            m = rx.search(prompt[:INTENT_PROMPT_CHARS])
            if m:
                return {"intent": intent, "keyword": kw, "match": m.group(0), "est": True}
    return {"intent": "other", "keyword": None, "est": True}


def est_tokens(chars):
    return (int(chars) + CHARS_PER_TOKEN - 1) // CHARS_PER_TOKEN


def _i(x):
    try:
        return int(x or 0)
    except (TypeError, ValueError):
        return 0


def norm_usage(u):
    u = u if isinstance(u, dict) else {}
    cc = u.get("cache_creation") if isinstance(u.get("cache_creation"), dict) else {}
    det = u.get("output_tokens_details") if isinstance(u.get("output_tokens_details"), dict) else {}
    return {
        "input": _i(u.get("input_tokens")),
        "cache_write": _i(u.get("cache_creation_input_tokens")),
        "cache_write_5m": _i(cc.get("ephemeral_5m_input_tokens")),
        "cache_write_1h": _i(cc.get("ephemeral_1h_input_tokens")),
        "cache_read": _i(u.get("cache_read_input_tokens")),
        "output": _i(u.get("output_tokens")),
        "thinking": _i(det.get("thinking_tokens")),
    }


def relpath(p, cwd):
    if not isinstance(p, str):
        return ""
    if cwd and os.path.isabs(p):
        try:
            r = os.path.relpath(p, cwd)
            if not r.startswith(".."):
                return r
        except ValueError:
            pass
    return p


def mcp_parts(name):
    """mcp__server__tool -> (server, tool), else None."""
    if not isinstance(name, str) or not name.startswith("mcp__"):
        return None
    rest = name[len("mcp__"):]
    server, sep, tool = rest.partition("__")
    return (server, tool) if sep else (rest, "")


def tool_kind(name):
    if name == "Read":
        return "file_read"
    if name == "Bash":
        return "bash"
    if mcp_parts(name):
        return "mcp"
    if name in ("Agent", "Task"):
        return "subagent_result"
    if name in ("Grep", "Glob", "WebSearch", "WebFetch", "ToolSearch"):
        return "search"
    return "other"


def tool_target(name, inp, cwd):
    inp = inp if isinstance(inp, dict) else {}
    if name == "Bash":
        cmd = " ".join(str(inp.get("command", "")).split())
        return cmd[:117] + "..." if len(cmd) > 120 else cmd
    if name in ("Read", "Edit", "Write", "MultiEdit", "NotebookEdit"):
        return relpath(inp.get("file_path") or inp.get("notebook_path") or "", cwd)
    if name == "Grep":
        return str(inp.get("pattern", ""))[:120]
    if name == "Glob":
        return str(inp.get("pattern", ""))[:120]
    if name in ("Agent", "Task"):
        return str(inp.get("subagent_type") or "general-purpose")
    if name == "Skill":
        return str(inp.get("skill") or inp.get("name") or inp.get("command") or "")
    mp = mcp_parts(name)
    if mp:
        return "%s/%s" % mp
    for k in ("url", "query", "path", "description"):
        if inp.get(k):
            return str(inp[k])[:120]
    return ""


def tool_label(name, target):
    if not target:
        return name
    if name == "Bash":
        return "Bash: " + target
    if name in ("Agent", "Task"):
        return "Subagent result (%s)" % target
    if mcp_parts(name):
        return "MCP " + target
    return "%s %s" % (name, target)


def _result_size(block, tur):
    """(chars in context, est tokens, persisted, full_chars)."""
    content = block.get("content")
    text = content_text(content)
    chars = block.get("_chars", len(text))  # _chars: size before parse.compact_event cut the text
    images = block.get("_images", 0)
    if isinstance(content, list):
        images = sum(1 for b in content if isinstance(b, dict) and b.get("type") == "image")
    persisted = "<persisted-output>" in text[:200]
    full = None
    if persisted:
        if isinstance(tur, dict):
            full = _i(tur.get("persistedOutputSize")) or tur.get("_stdout_chars") or \
                len(str(tur.get("stdout") or "")) or None
        if full is None:
            m = re.search(r"Output too large \(([\d.]+)(KB|MB)\)", text)
            if m:
                full = int(float(m.group(1)) * (1024 if m.group(2) == "KB" else 1024 * 1024))
    return chars, est_tokens(chars) + images * IMAGE_TOKENS_EST, persisted, full


# ---------------------------------------------------------------- call building

def _new_pending():
    return {"results": [], "prompts": [], "attachments": [], "hooks": [], "compaction": None}


def _is_json(s):
    try:
        json.loads(s)
        return True
    except ValueError:
        return False


def hook_entry(att, runs):
    """Hook output that entered context, from a hook attachment (None if nothing did).

    hook_additional_context carries the text Claude sees; hook_success carries command/stdout (remembered in
    `runs` by (toolUseID, hookName)) and only counts itself when it has `content` or is plain stdout of an event
    whose stdout becomes context."""
    t = att.get("type")
    key = (att.get("toolUseID"), att.get("hookName"))
    if t == "hook_success":
        runs[key] = att
        text = att.get("content") if isinstance(att.get("content"), str) else ""
        stdout = str(att.get("stdout") or "")
        if not text and att.get("hookEvent") in STDOUT_CONTEXT_EVENTS and stdout.strip() and not _is_json(stdout):
            text = stdout
        source = "stdout"
    elif t == "hook_additional_context":
        c = att.get("content")
        text = "\n".join(str(x) for x in c) if isinstance(c, list) else str(c or "")
        source = "additional_context"
    else:
        return None
    if not text:
        return None
    run = runs.get(key) or {}
    return {"hook": att.get("hookName") or att.get("hookEvent") or "hook", "event": att.get("hookEvent"),
            "tool_use_id": att.get("toolUseID"), "command": run.get("command") or att.get("command"),
            "source": source, "chars": len(text), "est_tokens": est_tokens(len(text)),
            "capped_chars": len(cap_text(text)), "est": True}


def build_calls(events, cwd=None, agent_id=None):
    """Group assistant lines by message.id into API calls and attach the inputs that fed each call.

    Returns (calls, all_attachments).
    """
    calls, by_id, tool_uses = [], {}, {}
    pending = _new_pending()
    attachments, hook_runs = [], {}
    last_ts = None
    for ev in events:
        typ = ev.get("type")
        ts = parse_ts(ev.get("timestamp"))
        if typ == "assistant":
            msg = ev.get("message") if isinstance(ev.get("message"), dict) else {}
            if not isinstance(msg.get("usage"), dict) or msg.get("model") == "<synthetic>":
                last_ts = ts or last_ts
                continue
            mid = msg.get("id") or ev.get("uuid")
            call = by_id.get(mid)
            if call is None:
                call = {
                    "index": len(calls), "msg_id": mid, "ts": ts, "model": msg.get("model"),
                    "agent_id": agent_id or ev.get("agentId"), "is_sidechain": bool(ev.get("isSidechain")),
                    "gap_event_seconds": (ts - last_ts).total_seconds() if ts and last_ts else None,
                    "tool_uses": [], "text_chars": 0, "thinking_chars": 0, "inputs": pending,
                }
                pending = _new_pending()
                by_id[mid] = call
                calls.append(call)
            call["usage"] = norm_usage(msg["usage"])  # repeated per line; last one wins
            for b in msg.get("content") or []:
                if not isinstance(b, dict):
                    continue
                bt = b.get("type")
                if bt == "tool_use" and b.get("id") not in tool_uses:
                    name = b.get("name") or "?"
                    tu = {"id": b.get("id"), "name": name, "target": tool_target(name, b.get("input"), cwd),
                          "call_index": call["index"]}
                    inp = b.get("input") if isinstance(b.get("input"), dict) else {}
                    if name == "Read":
                        tu["range"] = [inp.get("offset"), inp.get("limit")]
                    if name in ("Agent", "Task"):
                        tu["description"] = inp.get("description")
                    tool_uses[tu["id"]] = tu
                    call["tool_uses"].append(tu)
                elif bt == "text":
                    call["text_chars"] += b.get("_chars") or len(str(b.get("text", "")))
                elif bt == "thinking":
                    call["thinking_chars"] += b.get("_chars") or len(str(b.get("thinking", "")))
        elif typ == "user":
            if ev.get("isCompactSummary"):
                pending["compaction"] = pending["compaction"] or {"source": "isCompactSummary"}
                msg = ev.get("message") or {}
                pending["compaction"]["summary_chars"] = msg.get("_chars") or len(content_text(msg.get("content")))
            else:
                content = (ev.get("message") or {}).get("content")
                if isinstance(content, list):
                    for b in content:
                        if isinstance(b, dict) and b.get("type") == "tool_result":
                            tu = tool_uses.get(b.get("tool_use_id")) or {"name": "unknown", "target": ""}
                            tur = ev.get("toolUseResult")
                            chars, est, persisted, full = _result_size(b, tur)
                            name = tu["name"]
                            pending["results"].append({
                                "tool_use_id": b.get("tool_use_id"), "tool": name, "target": tu["target"],
                                "kind": tool_kind(name), "label": tool_label(name, tu["target"]),
                                "chars": chars, "est_tokens": est, "est": True,
                                "is_error": b.get("is_error") is True or (isinstance(tur, str) and tur.lstrip().startswith("<tool_use_error>")),
                                "persisted": persisted, "full_chars": full,
                                "range": tu.get("range"),
                            })
                text = real_prompt_text(ev)
                if text:
                    pending["prompts"].append(text)
        elif typ == "system" and ev.get("subtype") == "compact_boundary":
            meta = ev.get("compactMetadata") if isinstance(ev.get("compactMetadata"), dict) else {}
            pending["compaction"] = {"source": "compact_boundary", "trigger": meta.get("trigger"),
                                     "pre_tokens": meta.get("preTokens")}
        elif typ == "attachment" and isinstance(ev.get("attachment"), dict):
            pending["attachments"].append(ev["attachment"])
            attachments.append(ev["attachment"])
            h = hook_entry(ev["attachment"], hook_runs)
            if h:
                pending["hooks"].append(h)
        if ts:
            last_ts = ts
    return calls, attachments


# ---------------------------------------------------------------- overhead / offerings

def _chars(x):
    if x is None:
        return 0
    if isinstance(x, str):
        return len(x)
    if isinstance(x, list) and all(isinstance(s, str) for s in x):
        return sum(len(s) + 1 for s in x)
    return len(json.dumps(x, ensure_ascii=False))


def overhead_components(pre_atts, all_atts):
    """Estimated components of the first call's context, from attachments."""
    comps = []

    def add(label, chars, detail=None):
        if chars:
            comps.append({"label": label, "tokens": est_tokens(chars), "chars": chars, "est": True, "detail": detail})

    snaps = [a for a in all_atts if a.get("type") == "prompt_snapshot"]
    sp = next((a for a in snaps if a.get("systemPrompt")), None)
    tools = next((a for a in snaps if a.get("tools")), None)
    has_tools = tools is not None
    if sp:
        add("system prompt", _chars(sp.get("systemPrompt")) + _chars(sp.get("cliPrefix")))
    if tools:
        n = len(tools["tools"]) if isinstance(tools["tools"], list) else None
        add("tool schemas", _chars(tools["tools"]), "%s tool definitions (from prompt_snapshot)" % n)
    for a in pre_atts:
        t = a.get("type")
        if t == "deferred_tools_delta":
            add("deferred tool names", _chars(a.get("addedLines") or a.get("addedNames")),
                "%d deferred tools" % len(a.get("addedNames") or []))
        elif t == "skill_listing":
            add("skill listing", _chars(a.get("content")), "%s skills" % a.get("skillCount", "?"))
        elif t == "agent_listing_delta":
            add("subagent type listing", _chars(a.get("addedLines") or a.get("addedTypes")))
        elif t == "mcp_instructions_delta":
            add("MCP server instructions", _chars(a.get("addedBlocks")), ", ".join(a.get("addedNames") or []))
        elif t == "instructions":
            for f in a.get("files") or []:
                if isinstance(f, dict):
                    add("CLAUDE.md / instructions", _chars(f.get("content")), f.get("path"))
        elif t in ("model", "session_context", "environment", "date", "total_tokens_reminder"):
            pass  # small; folded into "other"
    return comps, has_tools


def offerings(all_atts):
    """Offered MCP servers/tools, skills, subagent types (+ est tokens of their listings)."""
    mcp, skills, agents = {}, {}, {}
    found = False

    def server(name):
        mp = mcp_parts(name)
        return mp[0] if mp else None

    for a in all_atts:
        t = a.get("type")
        if t == "deferred_tools_delta":
            found = True
            names = list(a.get("addedNames") or []) + list(a.get("surfacedNames") or [])
            for n in names:
                s = server(n)
                if s:
                    e = mcp.setdefault(s, {"tools": set(), "chars": 0})
                    e["tools"].add(mcp_parts(n)[1])
            for line in a.get("addedLines") or []:
                s = server(str(line).split()[0] if str(line).split() else "")
                if s:
                    e = mcp.setdefault(s, {"tools": set(), "chars": 0})
                    e["chars"] += len(str(line)) + 1
                    e["name_chars"] = e.get("name_chars", 0) + len(str(line)) + 1
            for d in a.get("surfacedDefinitions") or []:
                s = server(d.get("name")) if isinstance(d, dict) else None
                if s:
                    mcp.setdefault(s, {"tools": set(), "chars": 0})["chars"] += _chars(d.get("definition"))
        elif t == "mcp_instructions_delta":
            found = True
            blocks = a.get("addedBlocks") or []
            for i, display in enumerate(a.get("addedNames") or []):
                s = re.sub(r"[^A-Za-z0-9_-]", "_", str(display))
                e = mcp.setdefault(s, {"tools": set(), "chars": 0})
                e["display"] = display
                if i < len(blocks):
                    e["chars"] += _chars(blocks[i])
        elif t == "skill_listing":
            found = True
            for line in str(a.get("content") or "").splitlines():
                m = re.match(r"^- (.+?): ", line)
                if m:
                    skills[m.group(1).strip()] = skills.get(m.group(1).strip(), 0) + len(line) + 1
            for n in a.get("names") or []:
                skills.setdefault(n, 0)
        elif t == "agent_listing_delta":
            found = True
            lines = a.get("addedLines") or []
            for typ in a.get("addedTypes") or []:
                line = next((l for l in lines if str(l).startswith("- %s:" % typ)), "")
                agents[typ] = agents.get(typ, 0) + len(line)
    return found, mcp, skills, agents


# ---------------------------------------------------------------- attribution

def _scale(items, budget):
    """Scale items' tokens so they sum to `budget` (proportional); returns new list."""
    total = sum(i["tokens"] for i in items)
    if total <= 0:
        return items
    out, acc = [], 0
    for k, it in enumerate(items):
        t = budget - acc if k == len(items) - 1 else int(round(it["tokens"] * budget / total))
        t = max(0, t)
        acc += t
        out.append(dict(it, tokens=t))
    return out


def _cap(items, budget):
    if sum(i["tokens"] for i in items) > budget:
        return _scale(items, budget), True
    return items, False


def _prompt_items(prompts):
    return [{"cause": "user_prompt", "label": "User prompt", "tokens": est_tokens(len(p)), "est": True,
             "detail": p[:120]} for p in prompts]


def _result_items(results):
    return [{"cause": "tool_result", "kind": r["kind"], "label": r["label"], "tokens": r["est_tokens"],
             "est": True, "detail": "%d chars%s%s" % (r["chars"], ", error" if r["is_error"] else "",
                                                      ", persisted preview" if r["persisted"] else ""),
             "tool_use_id": r["tool_use_id"]} for r in results]


def _hook_items(hooks, results):
    """Hook output as its own cause, so it isn't absorbed into the tool result it followed."""
    labels = {r["tool_use_id"]: r["label"] for r in results}
    out = []
    for i, h in enumerate(hooks):
        after = labels.get(h["tool_use_id"])
        out.append({"cause": "hook", "kind": "hook", "label": "hook " + h["hook"], "tokens": h["est_tokens"],
                    "est": True, "detail": "%d chars%s" % (h["chars"], " after " + after if after else ""),
                    "hook_tool_use_id": h["tool_use_id"], "hook_idx": i})
    return out


def attribute_calls(calls, attachments):
    prev = None
    for c in calls:
        u = c["usage"]
        c["context"] = u["input"] + u["cache_write"] + u["cache_read"]
        c["api_cost_usd"] = pricing.cost_usd(c.get("model"), u)
        c["cost_weighted"] = round(pricing.cost_weighted(u), 1)
        c["gap_seconds"] = (c["ts"] - prev["ts"]).total_seconds() if prev and c["ts"] and prev["ts"] else None
        inp = c["inputs"]
        causes = []
        c["cache_miss"] = None
        c["compaction"] = None
        if prev is None:
            # Everything in context is new to this session/agent (cache_read here is a cross-session prefix hit).
            new = c["context"]
            comps, has_tools = overhead_components(inp["attachments"], attachments)
            sysitems = [dict(cause="system_overhead", **x) for x in comps]
            items = (sysitems + _prompt_items(inp["prompts"]) + _result_items(inp["results"])
                     + _hook_items(inp["hooks"], inp["results"]))
            items, _ = _cap(items, new)
            rest = new - sum(i["tokens"] for i in items)
            if rest > 0:
                items.append({"cause": "system_overhead", "est": True, "tokens": rest,
                              "label": "other (unattributed)" if has_tools else "tool schemas / other (unattributed)",
                              "detail": "first-call context not explained by known attachments"})
            causes = items
        else:
            new = u["input"] + u["cache_write"]
            remaining = new
            ttl = TTL_1H if prev["usage"]["cache_write_1h"] > 0 else TTL_5M
            c["ttl_seconds"] = ttl
            dropped = prev["context"] and c["context"] < COMPACT_DROP_RATIO * prev["context"]
            if inp["compaction"] or dropped:
                c["compaction"] = dict(inp["compaction"] or {"source": "inferred (context drop)"},
                                       context_before=prev["context"], context_after=c["context"])
            elif prev["context"] and u["cache_read"] < CACHE_DROP_RATIO * prev["context"]:
                expired = c["gap_seconds"] is not None and c["gap_seconds"] > ttl
                miss = min(remaining, max(0, prev["context"] - u["cache_read"]))
                c["cache_miss"] = {"reason": "ttl_expired" if expired else "cache_dropped", "tokens": miss,
                                   "gap_seconds": c["gap_seconds"], "ttl_seconds": ttl}
                causes.append({"cause": "cache_miss", "tokens": miss, "est": False,
                               "label": "Cache miss: previous context re-written",
                               "detail": ("idle %.0fs > TTL %ds" % (c["gap_seconds"], ttl)) if expired
                               else "cache_read fell to %d vs previous context %d" % (u["cache_read"], prev["context"])})
                remaining -= miss
            if c["compaction"]:
                pitems, _ = _cap(_prompt_items(inp["prompts"]), remaining)
                causes += pitems
                remaining -= sum(i["tokens"] for i in pitems)
                causes.append({"cause": "compaction", "tokens": remaining, "est": False,
                               "label": "Compaction: context rebuilt (system + summary)",
                               "detail": "context %d -> %d" % (prev["context"], c["context"])})
                remaining = 0
            else:
                out = min(remaining, prev["usage"]["output"])
                if out:
                    causes.append({"cause": "assistant_output", "tokens": out, "est": True,
                                   "label": "Previous assistant output re-entering context",
                                   "detail": "previous call output_tokens (thinking may be stripped)"})
                    remaining -= out
                pitems, _ = _cap(_prompt_items(inp["prompts"]), remaining)
                causes += pitems
                remaining -= sum(i["tokens"] for i in pitems)
                ritems = _result_items(inp["results"]) + _hook_items(inp["hooks"], inp["results"])
                ritems, capped = _cap(ritems, remaining)
                resid = remaining - sum(i["tokens"] for i in ritems)
                if ritems and not capped and resid > max(SCALE_UP_MIN, 0.25 * new):
                    # chars/4 undercounts dense output (logs, hashes, minified code): measured delta wins.
                    ritems = _scale(ritems, remaining)
                    for i in ritems:
                        i["scaled_to_measured"] = True
                causes += ritems
                remaining -= sum(i["tokens"] for i in ritems)
                if remaining > 0:
                    causes.append({"cause": "other", "tokens": remaining, "est": True,
                                   "label": "Other (reminders, tool-call framing, tokenizer variance)",
                                   "detail": None})
        # record attributed tokens back on results
        by_tu = {i.get("tool_use_id"): i["tokens"] for i in causes if i["cause"] == "tool_result"}
        by_hook = {i["hook_idx"]: i["tokens"] for i in causes if i["cause"] == "hook"}
        hook_by_tu = {}
        for i, h in enumerate(inp["hooks"]):
            h["attributed_tokens"] = by_hook.get(i, 0)
            hook_by_tu[h["tool_use_id"]] = hook_by_tu.get(h["tool_use_id"], 0) + h["attributed_tokens"]
        for r in inp["results"]:
            r["attributed_tokens"] = by_tu.get(r["tool_use_id"], 0)
            r["hook_tokens"] = hook_by_tu.get(r["tool_use_id"], 0)
        c["new_tokens"] = new
        c["attribution"] = causes
        prev = c
    return calls


# ---------------------------------------------------------------- hooks / instructions

def later_calls(calls, pos):
    """Calls after position `pos` that still carry what was added at `pos` (stops at compaction)."""
    n = 0
    for c in calls[pos + 1:]:
        if c.get("compaction"):
            break
        n += 1
    return n


def hook_totals(contexts):
    """contexts: [(agent_id, calls)] -> {hookName: totals + per-firing list}; carry = tokens x later calls."""
    out = {}
    for agent_id, calls in contexts:
        for pos, c in enumerate(calls):
            for h in c["inputs"]["hooks"]:
                n = later_calls(calls, pos)
                e = out.setdefault(h["hook"], {"hook": h["hook"], "event": h["event"], "commands": [], "fires": 0,
                                               "chars": 0, "est_tokens": 0, "attributed_tokens": 0,
                                               "carry_tokens": 0, "firings": [], "est": True})
                if h["command"] and h["command"] not in e["commands"]:
                    e["commands"].append(h["command"])
                e["fires"] += 1
                e["chars"] += h["chars"]
                e["est_tokens"] += h["est_tokens"]
                e["attributed_tokens"] += h["attributed_tokens"]
                e["carry_tokens"] += h["attributed_tokens"] * n
                e["firings"].append({"call_index": c["index"], "agent_id": agent_id, "tool_use_id": h["tool_use_id"],
                                     "chars": h["chars"], "capped_chars": h["capped_chars"],
                                     "attributed_tokens": h["attributed_tokens"], "later_calls": n})
    return out


IMPORT_RE = re.compile(r"(?<![\w`@])@((?:~/|\.{1,2}/|/)?[\w.-][\w./-]*)")


def is_claude_md(path):
    return os.path.basename(path or "").upper().startswith("CLAUDE") and path.lower().endswith(".md")


def _blank(line):
    nl = "\n" if line.endswith("\n") else ""
    return " " * (len(line) - len(nl)) + nl


def _strip_fences(text):
    """Text with fenced code blocks blanked out (same length, so offsets stay valid)."""
    out, fence = [], False
    for line in text.splitlines(True):
        if line.lstrip().startswith("```"):
            fence = not fence
            out.append(_blank(line))
        else:
            out.append(_blank(line) if fence else line)
    return "".join(out)


def import_refs(text):
    """`@path` imports in instruction text (outside code blocks/spans), trailing punctuation stripped."""
    refs = []
    for m in IMPORT_RE.finditer(_strip_fences(text)):
        r = m.group(1).rstrip(".,;:)")
        if "/" in r or "." in r:
            refs.append(r)
    return refs


def md_sections(text):
    """Top-level `## ` sections: [(heading, start, end)] char offsets into text (ends at next #/## heading)."""
    plain = _strip_fences(text)
    heads, pos = [], 0
    for line in plain.splitlines(True):
        if line.startswith("## ") or line.startswith("# "):
            heads.append((pos, line[3:].strip() if line.startswith("## ") else None))
        pos += len(line)
    out = []
    for k, (start, h) in enumerate(heads):
        if h is not None:
            end = heads[k + 1][0] if k + 1 < len(heads) else len(text)
            out.append((h, start, end))
    return out


def _resolve_ref(ref, base_dir):
    if ref.startswith("~/"):
        return os.path.normpath(os.path.expanduser(ref))
    return os.path.normpath(ref if os.path.isabs(ref) else os.path.join(base_dir, ref))


def instruction_breakdown(atts, cwd):
    """Per instruction file (CLAUDE.md and what it @-imports) + `## ` sections of each CLAUDE.md."""
    files = []
    for a in atts:
        if a.get("type") == "instructions":
            for f in a.get("files") or []:
                if isinstance(f, dict) and isinstance(f.get("content"), str) and f not in files:
                    files.append(f)
    if not files:
        return None
    out, idx = [], {}
    for i, f in enumerate(files):
        path = str(f.get("path") or "?")
        rel = relpath(path, cwd)
        e = {"path": path, "rel": rel, "type": f.get("type"), "chars": len(f["content"]),
             "est_tokens": est_tokens(len(f["content"])), "imported_by": None, "import_ref": None,
             "in_repo": bool(cwd) and not os.path.isabs(rel), "est": True}
        if is_claude_md(path):
            e["content"] = f["content"]  # kept so `fixes --emit-config --overlay` can rewrite it
        out.append(e)
        idx[os.path.normpath(path)] = i
    for i, f in enumerate(files):
        base = os.path.dirname(out[i]["path"])
        for ref in import_refs(f["content"]):
            j = idx.get(_resolve_ref(ref, base))
            if j is None:
                tail = "/" + ref.lstrip("./")
                j = next((k for k, e in enumerate(out) if e["path"].endswith(tail)), None)
            if j is not None and j != i and out[j]["imported_by"] is None:
                out[j]["imported_by"] = out[i]["rel"]
                out[j]["import_ref"] = "@" + ref
    sections = []
    for e in out:
        if "content" in e:
            for h, start, end in md_sections(e["content"]):
                sections.append({"file": e["rel"], "heading": h, "chars": end - start,
                                 "est_tokens": est_tokens(end - start), "est": True})
    total = sum(e["chars"] for e in out)
    return {"files": out, "sections": sections, "total_chars": total, "total_est_tokens": est_tokens(total),
            "est": True}


# ---------------------------------------------------------------- session analysis

def _iso(dt):
    return dt.isoformat().replace("+00:00", "Z") if dt else None


def _sum_usage(calls):
    t = {"fresh_input": 0, "cache_write": 0, "cache_write_5m": 0, "cache_write_1h": 0, "cache_read": 0,
         "output": 0, "thinking": 0, "api_cost_usd": 0.0, "unpriced_calls": 0}
    unpriced = set()
    for c in calls:
        u = c["usage"]
        cost = pricing.cost_usd(c.get("model"), u)
        if cost is None:
            t["unpriced_calls"] += 1
            unpriced.add(str(c.get("model")))
        else:
            t["api_cost_usd"] += cost
        t["fresh_input"] += u["input"]
        t["cache_write"] += u["cache_write"]
        t["cache_write_5m"] += u["cache_write_5m"]
        t["cache_write_1h"] += u["cache_write_1h"]
        t["cache_read"] += u["cache_read"]
        t["output"] += u["output"]
        t["thinking"] += u["thinking"]
    t["all_in"] = t["fresh_input"] + t["cache_write"] + t["cache_read"] + t["output"]
    t["new"] = t["fresh_input"] + t["cache_write"]  # uncached input + cache write
    t["cost_weighted"] = round(pricing.cost_weighted_of(t["fresh_input"], t["cache_write"], t["cache_read"],
                                                        t["output"]), 1)
    t["api_cost_usd"] = round(t["api_cost_usd"], 6)
    t["unpriced_models"] = sorted(unpriced)
    t["calls"] = len(calls)
    return t


def _call_json(c):
    out = {k: v for k, v in c.items() if k not in ("inputs", "ts")}
    out["timestamp"] = _iso(c["ts"])
    out["tool_results"] = c["inputs"]["results"]
    out["user_prompts"] = [p[:300] for p in c["inputs"]["prompts"]]
    out["hooks"] = c["inputs"]["hooks"]
    return out


def analyze_session(session):
    calls, atts = build_calls(session.events, session.cwd)
    attribute_calls(calls, atts)
    tu_index = {tu["id"]: tu for c in calls for tu in c["tool_uses"]}

    subagents, sub_calls_all = [], []
    for sa in session.subagents:
        scalls, satts = build_calls(sa["events"], session.cwd, agent_id=sa["agent_id"])
        attribute_calls(scalls, satts)
        meta = sa["meta"]
        parent_tu = tu_index.get(meta.get("toolUseId")) or {}
        tot = _sum_usage(scalls)
        subagents.append({
            "agent_id": sa["agent_id"], "type": meta.get("agentType") or parent_tu.get("target"),
            "description": meta.get("description") or parent_tu.get("description"),
            "tool_use_id": meta.get("toolUseId"), "parent_call_index": parent_tu.get("call_index"),
            "workflow_id": sa.get("workflow_id"), "workflow_phase": meta.get("workflowPhase"),
            "spawn_depth": meta.get("spawnDepth"),
            "calls": len(scalls), "tokens": tot,
            "peak_context": max((c["context"] for c in scalls), default=0),
            "call_records": [_call_json(c) for c in scalls],
        })
        sub_calls_all += scalls

    every = calls + sub_calls_all
    hooks = hook_totals([(None, calls)] + [(sa["agent_id"], [c for c in sub_calls_all if c["agent_id"] == sa["agent_id"]])
                                           for sa in session.subagents])
    main_tot = _sum_usage(calls)
    all_tot = _sum_usage(every)

    # per-tool / per-file / per-mcp
    per_tool, per_file, per_mcp, big_bash = {}, {}, {}, []
    for c in every:
        for tu in c["tool_uses"]:
            per_tool.setdefault(tu["name"], {"calls": 0, "result_tokens_est": 0, "result_tokens_attributed": 0,
                                             "errors": 0})["calls"] += 1
            mp = mcp_parts(tu["name"])
            if mp:
                e = per_mcp.setdefault(mp[0], {"calls": 0, "tools": {}, "result_tokens_est": 0,
                                               "result_tokens_attributed": 0})
                e["calls"] += 1
                e["tools"][mp[1]] = e["tools"].get(mp[1], 0) + 1
        for r in c["inputs"]["results"]:
            e = per_tool.setdefault(r["tool"], {"calls": 0, "result_tokens_est": 0, "result_tokens_attributed": 0,
                                                "errors": 0})
            e["result_tokens_est"] += r["est_tokens"]
            e["result_tokens_attributed"] += r["attributed_tokens"]
            e["errors"] += r["is_error"]
            mp = mcp_parts(r["tool"])
            if mp and mp[0] in per_mcp:
                per_mcp[mp[0]]["result_tokens_est"] += r["est_tokens"]
                per_mcp[mp[0]]["result_tokens_attributed"] += r["attributed_tokens"]
            if r["tool"] == "Read":
                f = per_file.setdefault(r["target"], {"reads": 0, "result_tokens_est": 0,
                                                      "result_tokens_attributed": 0, "errors": 0, "ranges": []})
                f["reads"] += 1
                f["result_tokens_est"] += r["est_tokens"]
                f["result_tokens_attributed"] += r["attributed_tokens"]
                f["errors"] += r["is_error"]
                f["ranges"].append(r["range"])
            if r["tool"] == "Bash" and (r["persisted"] or r["est_tokens"] >= BIG_BASH_TOKENS):
                big_bash.append({"command": r["target"], "chars_in_context": r["chars"],
                                 "est_tokens": r["est_tokens"], "attributed_tokens": r["attributed_tokens"],
                                 "persisted": r["persisted"], "full_chars": r["full_chars"],
                                 "agent_id": c["agent_id"], "call_index": c["index"]})
    for f in per_file.values():
        keys = [tuple(x) if x else (None, None) for x in f["ranges"]]
        f["distinct_ranges"] = len(set(keys))
        f["repeated_reads"] = len(keys) - len(set(keys))
        f["est"] = True
    big_bash.sort(key=lambda b: -(b["full_chars"] or b["chars_in_context"]))

    cache_misses = [dict(c["cache_miss"], call_index=c["index"], agent_id=c["agent_id"], timestamp=_iso(c["ts"]))
                    for c in every if c.get("cache_miss")]
    compactions = [dict(c["compaction"], call_index=c["index"], agent_id=c["agent_id"], timestamp=_iso(c["ts"]))
                   for c in every if c.get("compaction")]
    idle_gaps = [{"call_index": c["index"], "gap_seconds": c["gap_seconds"], "ttl_seconds": c.get("ttl_seconds"),
                  "cache_miss": bool(c.get("cache_miss"))}
                 for c in calls if c["gap_seconds"] is not None and c["gap_seconds"] > TTL_5M]

    # offered vs used (offerings come from the main session's attachments)
    found, mcp_off, skills_off, agents_off = offerings(atts)
    used_tools = {tu["name"] for c in every for tu in c["tool_uses"]}
    used_mcp = {mcp_parts(n)[0] for n in used_tools if mcp_parts(n)}
    used_skills = {tu["target"] for c in every for tu in c["tool_uses"] if tu["name"] == "Skill"}
    used_agents = {tu["target"] for c in calls for tu in c["tool_uses"] if tu["name"] in ("Agent", "Task")}
    n_calls = len(calls)

    def entry(name, chars, used, extra=None):
        t = est_tokens(chars)
        e = {"name": name, "used": used, "est_tokens": t, "est": True,
             "per_session_reread_est": t * n_calls,
             "note": "~%d tokens sit in the cached prefix and are re-read on each of %d calls" % (t, n_calls)}
        e.update(extra or {})
        return e

    offered = {"available": found, "mcp_deferred": any(x.get("type") == "deferred_tools_delta" for x in atts)}
    if found:
        offered["mcp_servers"] = sorted(
            (entry(s, v["chars"], s in used_mcp, {"display": v.get("display"), "tools_offered": sorted(v["tools"]),
                                                  "names_est_tokens": est_tokens(v.get("name_chars", 0)),
                                                  "tools_used": sorted(n for n in used_tools
                                                                       if (mcp_parts(n) or ("",))[0] == s)})
             for s, v in mcp_off.items()), key=lambda e: -e["est_tokens"])
        offered["skills"] = sorted((entry(s, ch, s in used_skills) for s, ch in skills_off.items()),
                                   key=lambda e: -e["est_tokens"])
        offered["subagent_types"] = sorted((entry(s, ch, s in used_agents) for s, ch in agents_off.items()),
                                           key=lambda e: -e["est_tokens"])
        for k in ("mcp_servers", "skills", "subagent_types"):
            offered[k + "_unused_est_tokens"] = sum(e["est_tokens"] for e in offered[k] if not e["used"])
    else:
        offered["reason"] = "no deferred_tools/skill_listing/agent_listing/mcp_instructions attachments in transcript"

    # reported totals from cost-state (cross-check)
    reported = None
    for ev in session.events:
        if ev.get("type") == "cost-state" and isinstance(ev.get("modelUsage"), dict):
            reported = {"total_cost_usd": ev.get("totalCostUSD"), "model_usage": ev["modelUsage"]}

    intent = classify_intent(session.first_prompt)
    return {
        "session_id": session.id, "project": session.project, "slug": session.slug, "cwd": session.cwd,
        "git_branch": session.git_branch, "models": session.models, "path": session.path,
        "start": _iso(session.start), "end": _iso(session.end),
        "duration_seconds": (session.end - session.start).total_seconds() if session.start and session.end else None,
        "first_prompt": session.first_prompt, "intent": intent,
        "totals": {"main": main_tot, "subagents": _sum_usage(sub_calls_all), "all": all_tot},
        "peak_context": max((c["context"] for c in calls), default=0),
        "calls": [_call_json(c) for c in calls],
        "subagents": subagents,
        "per_tool": per_tool, "per_file": per_file, "per_mcp_server": per_mcp,
        "big_bash_outputs": big_bash, "cache_misses": cache_misses, "compactions": compactions,
        "idle_gaps": idle_gaps, "offered_vs_used": offered, "reported": reported,
        "hooks": hooks, "instructions": instruction_breakdown(atts, session.cwd),
        "workflows": [dict(w, phases=dict(w["phases"])) for w in getattr(session, "workflows", [])],
        "request_sources": dict(session.prompt_sources()) if hasattr(session, "prompt_sources") else {},
    }


def session_summary(session):
    """Cheap per-session row for listings."""
    calls, _ = build_calls(session.events, session.cwd)
    tot = _sum_usage(calls)
    for sa in session.subagents:
        sc, _ = build_calls(sa["events"], session.cwd, agent_id=sa["agent_id"])
        st = _sum_usage(sc)
        for k in tot:
            tot[k] = sorted(set(tot[k]) | set(st[k])) if isinstance(tot[k], list) else tot[k] + st[k]
    return {"session_id": session.id, "project": session.project, "start": _iso(session.start),
            "main_calls": len(calls), "calls": tot["calls"], "total_tokens": tot["all_in"],
            "cost_weighted": round(pricing.cost_weighted_of(tot["fresh_input"], tot["cache_write"], tot["cache_read"],
                                                            tot["output"]), 1),
            "api_cost_usd": round(tot["api_cost_usd"], 6), "unpriced_calls": tot["unpriced_calls"],
            "new": tot["fresh_input"] + tot["cache_write"], "output": tot["output"], "cache_read": tot["cache_read"],
            "subagents": len(session.subagents), "intent": classify_intent(session.first_prompt)["intent"],
            "first_prompt": session.first_prompt}
