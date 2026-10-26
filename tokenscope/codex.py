"""OpenAI Codex CLI rollouts -> tokenscope's canonical (Claude-shaped) events.

A rollout is `<root>/YYYY/MM/DD/rollout-<timestamp>-<uuid>.jsonl` (or a flat `rollout-*.jsonl` under an archive root):
one `{"timestamp", "type", "payload"[, "ordinal"]}` record per line. CodexConverter turns the records into `user` /
`assistant` events with tool_use / tool_result blocks and `message.usage`, so analyze, steps, intents, alerts and live
read them unchanged. Tools map to canonical Claude names (shell -> Bash, apply_patch -> Write / Edit, ...).

Privacy: free text (reasoning summaries, subagent prompts, task names, plan explanations, patch bodies, code-mode
scripts) never goes into a tool input field other than the ones the Claude shape already uses for it (Bash `command`,
file paths, `query`, `url`, TodoWrite todos). Every string input is cut to INPUT_MAX chars.
"""
import glob
import json
import os
import re
import shlex

from .parse import Session, compact_event, load_events, read_jsonl

INPUT_MAX = 4000
COPY_CHARS = 200            # output copy given to the 2nd.. tool_use of one call
HEAD_LINE_MAX = 4 << 20     # bytes read from a rollout's first line (session_meta can carry long instructions)
ROLLOUT_RE = re.compile(r"^rollout-.*?([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})"
                        r"\.jsonl$")
SCAFFOLD_PREFIXES = ("<environment_context>", "<user_instructions>", "<INSTRUCTIONS>", "# AGENTS.md instructions",
                     "<permissions", "<collaboration_mode>", "<turn_aborted>", "<subagent_notification>", "<skill>",
                     "<user_shell_command>", "<user_action>", "<hook_prompt>")
SHELL_TOOLS = {"shell", "container.exec", "shell_command", "exec_command", "unified_exec", "local_shell"}
SHELLS = {"bash", "sh", "zsh", "dash", "ksh"}
READ_TOOLS = {"read_file", "view_image"}
GREP_TOOLS = {"grep_files", "grep", "search", "search_files"}
GLOB_TOOLS = {"list_dir", "glob", "list_files", "find_files"}
SEND_TOOLS = {"send_message", "send_input", "resume_agent"}
WAIT_TOOLS = {"wait_agent", "wait", "list_agents", "close_agent"}
TODO_STATUS = {"completed": "completed", "done": "completed", "in_progress": "in_progress",
               "in-progress": "in_progress", "active": "in_progress"}
PATCH_HEADERS = (("*** Add File: ", "Write"), ("*** Update File: ", "Edit"), ("*** Delete File: ", "Edit"))
ERROR_PREFIXES = ("error:", "Error:", "ERROR:", "error ", "Error ", "failed:", "Failed:")
NESTED_CALL_RE = re.compile(r"\btools\s*\.\s*([A-Za-z_][\w.]*)\s*\(")
_HEAD_CACHE = {}           # path -> head_meta of a rollout whose first line is complete
CMD_RE = re.compile(r"""\bcmd\s*:\s*("(?:[^"\\]|\\.)*"|'(?:[^'\\]|\\.)*'|`[^`]*`)""")


class CodexConverter:
    """Streaming Codex rollout -> canonical events: feed(record) per line, finish() at the end of a batch.

    One assistant event per model response (consecutive reasoning / message / tool call items). The response is held
    until its usage arrives (token_usage_record, or an event_msg token_count) and is then emitted with the tool results
    buffered meanwhile; a user prompt, a model item after buffered tool outputs, or finish() emits it with zero usage.
    Usage that arrives after such a flush is sent as a content-free assistant event with the same message.id (later
    lines of one message.id replace its usage downstream)."""

    def __init__(self, use_usage_records=False):
        self.meta = None              # payload of the first session_meta (the file's own identity)
        self.session_id = None
        self.agent_id = None          # own id when the rollout is a subagent's
        self.cwd = None
        self.model = None
        self.start_ordinal = None     # subagent: records before it are the parent's forked history
        self.usage_records = use_usage_records   # token_usage_record seen: token_count is ignored
        self.prev_total = None
        self.pending = None
        self.buffered = []
        self.calls = {}               # call_id -> [tool_use ids] (emitted or pending)
        self.last = None              # (message id, model, timestamp, has usage) of the last emitted response
        self.n = 0

    # ------------------------------------------------------------ public
    def feed(self, raw):
        """Canonical events for one raw record (never raises; bad records give [])."""
        try:
            return self._feed(raw)
        except Exception:  # noqa: BLE001 - one odd record must not stop a transcript
            return []

    def finish(self):
        try:
            return self._flush()
        except Exception:  # noqa: BLE001
            return []

    # ------------------------------------------------------------ dispatch
    def _feed(self, raw):
        if not isinstance(raw, dict):
            return []
        typ = raw.get("type")
        payload = raw.get("payload") if isinstance(raw.get("payload"), dict) else {}
        ts = raw.get("timestamp") if isinstance(raw.get("timestamp"), str) else None
        if typ == "session_meta":
            self._session_meta(payload)
            return []
        if self._forked(raw):
            return []
        if typ == "turn_context":
            self._set_model(payload.get("model"))
            if isinstance(payload.get("cwd"), str) and payload["cwd"]:
                self.cwd = payload["cwd"]
            return []
        if typ == "world_state":
            st = payload.get("state") if isinstance(payload.get("state"), dict) else {}
            if not self.model:
                self._set_model(st.get("model"))
            return []
        if typ == "token_usage_record":
            self.usage_records = True
            u = payload.get("usage")
            if not isinstance(u, dict):
                return []
            rid = payload.get("response_id")
            return self._usage(canonical_usage(u), rid if isinstance(rid, str) and rid else None)
        if typ == "event_msg":
            return self._event_msg(payload)
        if typ == "response_item":
            return self._response_item(payload, ts)
        return []

    def _session_meta(self, p):
        if self.meta is not None:
            return                    # a later copy (e.g. the parent's, in a subagent file) keeps our identity
        self.meta = p
        sid = p.get("id")
        self.session_id = sid if isinstance(sid, str) and sid else None
        if isinstance(p.get("cwd"), str) and p["cwd"]:
            self.cwd = p["cwd"]
        parent = parent_thread_id(p)
        if parent:
            self.agent_id = self.session_id
            self.session_id = parent
            so = p.get("subagent_history_start_ordinal")
            self.start_ordinal = so if isinstance(so, int) and not isinstance(so, bool) else None

    def _forked(self, raw):
        o = raw.get("ordinal")
        return (self.start_ordinal is not None and isinstance(o, int) and not isinstance(o, bool)
                and o < self.start_ordinal)

    def _set_model(self, m):
        if isinstance(m, str) and m.strip():
            self.model = m.strip()

    def _event_msg(self, p):
        kind = p.get("type")
        if kind == "thread_settings_applied":
            ts = p.get("thread_settings") if isinstance(p.get("thread_settings"), dict) else {}
            if not self.model:
                self._set_model(ts.get("model"))
            return []
        if kind != "token_count" or self.usage_records:
            return []
        info = p.get("info")
        if not isinstance(info, dict):
            return []
        total = info.get("total_token_usage") if isinstance(info.get("total_token_usage"), dict) else None
        last = info.get("last_token_usage") if isinstance(info.get("last_token_usage"), dict) else None
        if total is not None and total == self.prev_total:
            return []                 # the same numbers again
        prev, self.prev_total = self.prev_total, total if total is not None else self.prev_total
        if last is not None:
            return self._usage(canonical_usage(last), None)
        if total is not None:
            delta = {k: _int(total.get(k)) - _int((prev or {}).get(k)) for k in total}
            return self._usage(canonical_usage(delta), None)
        return []

    def _response_item(self, p, ts):
        kind = p.get("type")
        if kind == "message":
            role = p.get("role")
            if role == "assistant":
                text = content_text(p.get("content"))
                return self._model_item([{"type": "text", "text": text}] if text else [], ts)
            if role == "user":
                text = content_text(p.get("content"))
                if not text.strip() or text.lstrip().startswith(SCAFFOLD_PREFIXES):
                    return []
                out = self._flush()
                out.append(self._event("user", ts, {"role": "user", "content": text}))
                return out
            return []                 # developer / system
        if kind == "reasoning":       # summary text is dropped: it would reach step labels (Claude: signed, empty)
            return self._model_item([{"type": "thinking", "thinking": "", "signature": "codex"}], ts)
        if kind in ("function_call", "custom_tool_call", "local_shell_call", "web_search_call"):
            call_id = next((c for c in (p.get("call_id"), p.get("id")) if isinstance(c, (str, int)) and c != ""
                            and not isinstance(c, bool)), None)
            call_id = str(call_id) if call_id is not None else "codex_call_%d" % (self.n + 1)
            self.n += 1
            tools = tool_uses_of(p, call_id)
            if not tools:
                return []
            out = self._model_item([{"type": "tool_use", "id": i, "name": n, "input": inp} for i, n, inp in tools],
                                   ts)
            if kind != "web_search_call":     # web search has no output item
                self.calls[call_id] = [i for i, _, _ in tools]
            return out
        if kind in ("function_call_output", "custom_tool_call_output"):
            call_id = p.get("call_id")
            ids = self.calls.pop(str(call_id), None) if isinstance(call_id, (str, int)) and call_id != "" else None
            if not ids:
                return []
            text, err = output_text(p.get("output"))
            results = [self._result(ids[0], text, err, ts)]
            short = text[:COPY_CHARS]
            results += [self._result(i, short, err, ts) for i in ids[1:]]
            if self.pending is not None and ids[0] in self.pending["ids"]:
                self.buffered.extend(results)
                return []
            return results
        return []

    # ------------------------------------------------------------ assembly
    def _model_item(self, blocks, ts):
        out = []
        if self.pending is not None and self.buffered:
            out = self._flush()       # tool outputs came back: this is the next response
        if not blocks:
            return out
        if self.pending is None:
            self.pending = {"ts": ts, "blocks": [], "ids": set(), "model": self.model}
        self.pending["blocks"].extend(blocks)
        self.pending["ids"].update(b["id"] for b in blocks if b.get("type") == "tool_use")
        return out

    def _usage(self, usage, response_id):
        if self.pending is not None:
            return self._emit_pending(usage, response_id)
        if self.last is not None and not self.last[3]:
            mid, model, ts, _ = self.last
            self.last = (mid, model, ts, True)
            return [self._event("assistant", ts, {"id": mid, "role": "assistant", "model": model or "codex",
                                                  "usage": usage, "content": []})]
        if not any(usage.values()):
            return []
        self.n += 1                   # a response with no item we keep (e.g. only ignored items): still billed
        mid = response_id or "codex_%d" % self.n
        self.last = (mid, self.model, None, True)
        return [self._event("assistant", None, {"id": mid, "role": "assistant", "model": self.model or "codex",
                                                "stop_reason": "end_turn", "usage": usage, "content": []})]

    def _flush(self):
        if self.pending is None:
            return []
        return self._emit_pending(None, None)

    def _emit_pending(self, usage, response_id):
        p, self.pending = self.pending, None
        self.n += 1
        mid = response_id or "codex_%d" % self.n
        model = p["model"] or self.model or "codex"
        has_tools = bool(p["ids"])
        msg = {"id": mid, "role": "assistant", "model": model, "stop_reason": "tool_use" if has_tools else "end_turn",
               "usage": usage or zero_usage(), "content": p["blocks"]}
        self.last = (mid, model, p["ts"], usage is not None)
        out = [self._event("assistant", p["ts"], msg)]
        out.extend(self.buffered)
        self.buffered = []
        return out

    def _result(self, tool_use_id, text, err, ts):
        return self._event("user", ts, {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": tool_use_id, "content": text, "is_error": err}]})

    def _event(self, typ, ts, message):
        self.n += 1
        ev = {"type": typ, "uuid": "codex-%s-%d" % (self.agent_id or self.session_id or "x", self.n),
              "message": message}
        if ts:
            ev["timestamp"] = ts
        if self.cwd:
            ev["cwd"] = self.cwd
        if self.session_id:
            ev["sessionId"] = self.session_id
        if self.agent_id:
            ev["agentId"] = self.agent_id
            ev["isSidechain"] = True
        return ev


# ---------------------------------------------------------------- batch read / discovery

def read_events(path):
    """Compacted canonical events of one rollout (see parse.compact_event)."""
    conv = CodexConverter(use_usage_records=_has_usage_records(path))
    names, out = {}, []

    def keep(evs):
        for ev in evs:
            c = compact_event(ev, names)
            if c is not None:
                out.append(c)
    for raw in read_jsonl(path):
        keep(conv.feed(raw))
    keep(conv.finish())
    return out


def _has_usage_records(path):
    try:
        with open(path, "rb") as f:
            for line in f:
                if b'"token_usage_record"' in line[:200]:
                    return True
    except OSError:
        pass
    return False


def is_rollout_name(name):
    return name.startswith("rollout-") and name.endswith(".jsonl")


def rollout_id(path):
    """Session uuid from a rollout file name (else the base name without .jsonl)."""
    name = os.path.basename(path)
    m = ROLLOUT_RE.match(name)
    if m:
        return m.group(1).lower()
    return name[:-len(".jsonl")] if name.endswith(".jsonl") else name


def rollout_files(root):
    """Every rollout under root (YYYY/MM/DD layout and flat archive files), sorted."""
    if not root or not os.path.isdir(root):
        return []
    pats = (os.path.join(root, "*", "*", "*", "rollout-*.jsonl"), os.path.join(root, "rollout-*.jsonl"))
    return sorted({p for pat in pats for p in glob.glob(pat) if os.path.isfile(p)})


def sessions_root(path):
    """The sessions root of a rollout path: three levels up for YYYY/MM/DD, else its directory."""
    d = os.path.dirname(os.path.abspath(path))
    parts = d.split(os.sep)
    if len(parts) >= 4 and re.fullmatch(r"\d{4}", parts[-3]) and re.fullmatch(r"\d{2}", parts[-2]) \
            and re.fullmatch(r"\d{2}", parts[-1]):
        return os.sep.join(parts[:-3]) or os.sep
    return d



def head_meta(path):
    """{"id", "cwd", "parent", "depth", "complete"} from the rollout's first session_meta line ("complete": that line
    is fully written; it never changes afterwards, so it is cached from then on)."""
    hit = _HEAD_CACHE.get(path)
    if hit is not None:
        return hit
    out = {"id": rollout_id(path), "cwd": None, "parent": None, "depth": None, "complete": False}
    try:
        with open(path, "rb") as f:
            line = f.readline(HEAD_LINE_MAX)
    except OSError:
        return out
    p = None
    try:
        obj = json.loads(line)
        if isinstance(obj, dict) and obj.get("type") == "session_meta" and isinstance(obj.get("payload"), dict):
            p = obj["payload"]
    except ValueError:            # cut at HEAD_LINE_MAX (or garbage): look for the two fields we need
        m = re.search(rb'"cwd"\s*:\s*"((?:[^"\\]|\\.)*)"', line)
        if m:
            try:
                out["cwd"] = json.loads(b'"' + m.group(1) + b'"')
            except ValueError:
                pass
        m = re.search(rb'"parent_thread_id"\s*:\s*"([^"]+)"', line)
        if m:
            out["parent"] = m.group(1).decode("utf-8", "replace")
    if p is not None:
        if isinstance(p.get("cwd"), str) and p["cwd"]:
            out["cwd"] = p["cwd"]
        out["parent"] = parent_thread_id(p)
        d = _thread_spawn(p).get("depth")
        out["depth"] = d if isinstance(d, int) and not isinstance(d, bool) else None
    if line.endswith(b"\n") or len(line) >= HEAD_LINE_MAX:
        out["complete"] = True
        _HEAD_CACHE[path] = out
    return out


def project_of(meta):
    cwd = meta.get("cwd")
    return (os.path.basename(cwd.rstrip("/")) or cwd) if isinstance(cwd, str) and cwd else "codex"


def discover(root):
    """CodexSession per main rollout under root (subagent rollouts whose parent file is under root are attached to
    it by CodexSession.load instead)."""
    files = rollout_files(root)
    metas = {p: head_meta(p) for p in files}
    ids = {rollout_id(p) for p in files}
    out = []
    for p in files:
        m = metas[p]
        if m["parent"] and m["parent"] in ids:
            continue
        out.append(CodexSession(p, project_of(m), root=root, files=files))
    return out


def descendants(root_id, files):
    """[(path, depth)] of the subagent rollouts below session root_id (transitively), sorted by path."""
    children = {}
    for p in files:
        m = head_meta(p)
        if m["parent"]:
            children.setdefault(m["parent"], []).append(p)
    out, todo, seen = [], [(root_id, 0)], {root_id}
    while todo:
        sid, depth = todo.pop()
        for p in children.get(sid, ()):
            own = rollout_id(p)
            if own in seen:
                continue
            seen.add(own)
            out.append((p, head_meta(p)["depth"] or depth + 1))
            todo.append((own, depth + 1))
    return sorted(out)


def root_session_id(path, files):
    """The main session a rollout belongs to: its own id, else the top of its parent_thread_id chain (the last
    parent named, even when that parent's file is not under the root)."""
    by_id = {rollout_id(p): p for p in files}
    sid, seen = rollout_id(path), set()
    while sid in by_id and sid not in seen:
        seen.add(sid)
        parent = head_meta(by_id[sid])["parent"]
        if not parent:
            break
        sid = parent
    return sid


class CodexSession(Session):
    """A main Codex rollout; load() also attaches its subagent rollouts."""

    def __init__(self, path, slug, root=None, files=None):
        super().__init__(path, slug, harness="codex")
        self.id = rollout_id(path)
        self.root = root or sessions_root(path)
        self._files = files

    def load_subagents(self):
        files = self._files if self._files is not None else rollout_files(self.root)
        for p, depth in descendants(self.id, files):
            if os.path.abspath(p) == os.path.abspath(self.path):
                continue
            self.subagents.append({
                "agent_id": rollout_id(p), "path": p, "meta": {"agentType": "subagent"}, "events": load_events(p),
                "workflow_id": None, "agent_type": "subagent", "description": None, "workflow_phase": None,
                "spawn_depth": depth})


# ---------------------------------------------------------------- tool mapping

def tool_uses_of(p, call_id):
    """[(tool_use id, canonical name, small input)] for one Codex call item."""
    kind = p.get("type")
    if kind == "local_shell_call":
        action = p.get("action") if isinstance(p.get("action"), dict) else {}
        return _ids(call_id, _shell(action))
    if kind == "web_search_call":
        action = p.get("action") if isinstance(p.get("action"), dict) else {}
        q = action.get("query")
        if not q and isinstance(action.get("queries"), list) and action["queries"]:
            q = action["queries"][0]
        return _ids(call_id, [("WebSearch", {"query": _s(q)})])
    name = p.get("name") if isinstance(p.get("name"), str) else ""
    ns = p.get("namespace") if isinstance(p.get("namespace"), str) else ""
    if kind == "custom_tool_call":
        inp = p.get("input")
        inp = inp if isinstance(inp, str) else ""
        if name == "exec":
            return _ids(call_id, _code_mode(inp))
        if name == "apply_patch":
            return _ids(call_id, patch_tools(inp))
        return _ids(call_id, map_tool(name, ns, _parse_args(inp)))
    return _ids(call_id, map_tool(name, ns, _parse_args(p.get("arguments"))))


def map_tool(name, namespace, args):
    """[(canonical name, small input)] for one named Codex tool call with parsed arguments."""
    args = args if isinstance(args, (dict, str)) else {}
    base = name.rsplit(".", 1)[-1] if name.startswith(("functions.", "tools.")) else name
    mcp = _mcp_name(base, namespace)
    if mcp:
        return [(mcp, _small_dict(args) if isinstance(args, dict) else {})]
    if base in SHELL_TOOLS:
        return _shell(args if isinstance(args, dict) else {"command": args})
    if base == "write_stdin":
        return [("BashOutput", {})]
    if base in ("apply_patch", "applypatch"):
        text = args if isinstance(args, str) else (args.get("input") or args.get("patch") or "")
        return patch_tools(text if isinstance(text, str) else "")
    d = args if isinstance(args, dict) else {}
    if base in READ_TOOLS:
        return [("Read", {"file_path": _s(d.get("path") or d.get("file_path") or "")})]
    if base in GREP_TOOLS:
        inp = {"pattern": _s(d.get("pattern") or d.get("query") or "")}
        if d.get("path"):
            inp["path"] = _s(d["path"])
        return [("Grep", inp)]
    if base in GLOB_TOOLS:
        inp = {"pattern": _s(d.get("pattern") or "*")}
        path = d.get("dir_path") or d.get("path")
        if path:
            inp["path"] = _s(path)
        return [("Glob", inp)]
    if base in ("web_search", "search_query"):
        return [("WebSearch", {"query": _s(d.get("query") or d.get("q") or "")})]
    if base in ("fetch", "open", "web_fetch", "open_url"):
        return [("WebFetch", {"url": _s(d.get("url") or d.get("ref_id") or "")})]
    if base == "spawn_agent":
        return [("Agent", {"subagent_type": "subagent"})]
    if base in SEND_TOOLS:
        return [("SendMessage", {})]
    if base in WAIT_TOOLS:
        return [("TaskOutput", {})]
    if base == "update_plan":
        return [("TodoWrite", {"todos": _todos(d.get("plan"))})]
    return [(base or "unknown", {k: v for k, v in d.items()
                                 if isinstance(v, (int, float, bool)) and len(str(k)) <= 40})]


def patch_tools(text):
    """[("Write"|"Edit", {"file_path"})] per file header of an apply_patch text (one empty Edit when none)."""
    out = []
    for line in (text or "").splitlines():
        line = line.strip()
        for prefix, tool in PATCH_HEADERS:
            if line.startswith(prefix):
                path = line[len(prefix):].strip()
                if path:
                    out.append((tool, {"file_path": _s(path)}))
    return out or [("Edit", {"file_path": ""})]


def shell_command(args):
    """The command string of shell arguments ({"command": argv|str} / {"cmd": str} / {"action": exec})."""
    cmd = args.get("command")
    if cmd is None:
        cmd = args.get("cmd")
    if isinstance(cmd, list):
        argv = [str(a) for a in cmd]
        if len(argv) >= 3 and os.path.basename(argv[0]) in SHELLS and argv[1].startswith("-") \
                and argv[1].endswith("c"):
            return argv[2]
        try:
            return shlex.join(argv)
        except (TypeError, ValueError):
            return " ".join(argv)
    return cmd if isinstance(cmd, str) else ""


def _shell(args):
    cmd = shell_command(args)
    first = cmd.lstrip().split(None, 1)[0] if cmd.strip() else ""
    if first in ("apply_patch", "applypatch"):
        return patch_tools(cmd)
    return [("Bash", {"command": _s(cmd)})]


def _mcp_name(name, namespace):
    if name.startswith("mcp__"):
        return name
    if namespace.startswith("mcp__"):
        ns = namespace if namespace.endswith("__") else namespace + "__"
        return ns + name if name else None
    if namespace and "__" in name:
        return "mcp__" + name
    return None


def _code_mode(script):
    """Canonical tools of the nested tools.<name>({...}) calls of a code-mode `exec` script."""
    out = []
    for m in NESTED_CALL_RE.finditer(script or ""):
        name = m.group(1).rsplit(".", 1)[-1]
        try:
            arg, _ = _js_value(script, _skip_ws(script, m.end()))
        except (ValueError, IndexError, RecursionError):
            arg = _cmd_fallback(script[m.end():m.end() + 20000])
            if not arg:
                continue          # unreadable arguments: an empty Bash command would only mislead
        out.extend(map_tool(name, "", arg if isinstance(arg, (dict, str)) else {}))
    return out or [("CodexExec", {})]


def _cmd_fallback(text):
    m = CMD_RE.search(text)
    if not m:
        return {}
    raw = m.group(1)
    try:
        return {"cmd": json.loads(raw) if raw[0] == '"' else raw[1:-1]}
    except ValueError:
        return {"cmd": raw[1:-1]}


def _todos(plan):
    out = []
    for item in plan if isinstance(plan, list) else ():
        if not isinstance(item, dict):
            continue
        step = _s(item.get("step") or item.get("content") or "")
        st = TODO_STATUS.get(str(item.get("status") or "").lower(), "pending")
        out.append({"content": step, "status": st, "activeForm": step})
    return out


def _ids(call_id, tools):
    return [(call_id if k == 0 else "%s#%d" % (call_id, k + 1), n, inp) for k, (n, inp) in enumerate(tools)]


def _parse_args(raw):
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        s = raw.strip()
        if s[:1] in ("{", "["):
            try:
                v = json.loads(s)
                return v if isinstance(v, dict) else {}
            except ValueError:
                return {}
        return raw
    return {}


def _small_dict(d, depth=0):
    out = {}
    for k, v in list(d.items())[:50]:
        if isinstance(v, str):
            out[k] = _s(v)
        elif isinstance(v, (int, float, bool)) or v is None:
            out[k] = v
        elif isinstance(v, dict) and depth < 2:
            out[k] = _small_dict(v, depth + 1)
        elif isinstance(v, list) and depth < 2:
            out[k] = [_s(x) if isinstance(x, str) else x for x in v[:50] if isinstance(x, (str, int, float, bool))]
    return out


def _s(v):
    v = v if isinstance(v, str) else ("" if v is None else str(v))
    return v[:INPUT_MAX]


# ---------------------------------------------------------------- tolerant JS literal reader (code mode)

def _skip_ws(s, i):
    while i < len(s) and s[i] in " \t\r\n":
        i += 1
    return i


def _js_value(s, i, depth=0):
    """(value, next index) of a JS-ish literal at s[i]: objects (unquoted keys ok), arrays, strings, numbers,
    true/false/null; anything else raises ValueError."""
    if depth > 20:
        raise ValueError("too deep")
    c = s[i]
    if c == "{":
        out, i = {}, _skip_ws(s, i + 1)
        while s[i] != "}":
            if s[i] in "\"'`":
                key, i = _js_string(s, i)
            else:
                m = re.compile(r"[A-Za-z_$][\w$]*").match(s, i)
                if not m:
                    raise ValueError("bad key")
                key, i = m.group(0), m.end()
            i = _skip_ws(s, i)
            if s[i] in ",}":          # shorthand {cmd}: value unknown
                out[key] = None
            else:
                if s[i] != ":":
                    raise ValueError("expected :")
                val, i = _js_value(s, _skip_ws(s, i + 1), depth + 1)
                out[key] = val
            i = _skip_ws(s, i)
            if s[i] == ",":
                i = _skip_ws(s, i + 1)
            elif s[i] != "}":
                raise ValueError("expected , or }")
        return out, i + 1
    if c == "[":
        out, i = [], _skip_ws(s, i + 1)
        while s[i] != "]":
            val, i = _js_value(s, i, depth + 1)
            out.append(val)
            i = _skip_ws(s, i)
            if s[i] == ",":
                i = _skip_ws(s, i + 1)
            elif s[i] != "]":
                raise ValueError("expected , or ]")
        return out, i + 1
    if c in "\"'`":
        return _js_string(s, i)
    m = re.compile(r"-?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?").match(s, i)
    if m:
        t = m.group(0)
        return (float(t) if any(x in t for x in ".eE") else int(t)), m.end()
    for word, val in (("true", True), ("false", False), ("null", None), ("undefined", None)):
        if s.startswith(word, i):
            return val, i + len(word)
    raise ValueError("unsupported value")


_ESC = {"n": "\n", "t": "\t", "r": "\r", "b": "\b", "f": "\f", "v": "\v", "0": "\0"}


def _js_string(s, i):
    q, i, buf = s[i], i + 1, []
    while True:
        c = s[i]
        if c == "\\":
            n = s[i + 1]
            if n == "u" and re.fullmatch(r"[0-9a-fA-F]{4}", s[i + 2:i + 6]):
                buf.append(chr(int(s[i + 2:i + 6], 16)))
                i += 6
                continue
            buf.append(_ESC.get(n, n))
            i += 2
            continue
        if c == q:
            return "".join(buf), i + 1
        buf.append(c)
        i += 1


# ---------------------------------------------------------------- helpers

def parent_thread_id(p):
    """The parent thread of a subagent session_meta payload (None for a main session)."""
    parent = p.get("parent_thread_id")
    if not parent:
        parent = _thread_spawn(p).get("parent_thread_id")
    return parent if isinstance(parent, str) and parent else None


def _thread_spawn(p):
    src = p.get("source")
    sub = src.get("subagent") if isinstance(src, dict) else None
    spawn = sub.get("thread_spawn") if isinstance(sub, dict) else None
    return spawn if isinstance(spawn, dict) else {}


def content_text(content):
    """Text of a Codex message content (str, or a list of input_text / output_text / text items)."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(str(x.get("text") or "") for x in content
                       if isinstance(x, dict) and x.get("type") in ("input_text", "output_text", "text"))
    return ""


def output_text(out):
    """(text, is_error) of a function / custom tool call output."""
    err = False
    if isinstance(out, str):
        s = out.strip()
        if s.startswith("{"):
            try:
                d = json.loads(s)
            except ValueError:
                d = None
            if isinstance(d, dict) and ("output" in d or "metadata" in d):
                return _output_dict(d)
        return out, out.lstrip().startswith(ERROR_PREFIXES)
    if isinstance(out, dict):
        return _output_dict(out)
    if isinstance(out, list):
        return content_text(out), err
    return ("" if out is None else str(out)), err


def _output_dict(d):
    body = d.get("output") if "output" in d else d.get("content")
    text = body if isinstance(body, str) else content_text(body) if isinstance(body, list) else \
        ("" if body is None else json.dumps(body))
    meta = d.get("metadata") if isinstance(d.get("metadata"), dict) else {}
    code = meta.get("exit_code", d.get("exit_code"))
    err = (isinstance(code, int) and not isinstance(code, bool) and code != 0) or d.get("success") is False
    return text, bool(err)


def canonical_usage(u):
    """OpenAI usage (input includes cached + cache writes; output includes reasoning) -> Claude-shaped usage."""
    inp, cached = _int(u.get("input_tokens")), _int(u.get("cached_input_tokens"))
    write = _int(u.get("cache_write_input_tokens"))
    return {"input_tokens": max(inp - cached - write, 0), "output_tokens": _int(u.get("output_tokens")),
            "cache_read_input_tokens": cached, "cache_creation_input_tokens": write}


def zero_usage():
    return {"input_tokens": 0, "output_tokens": 0, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}


def _int(v):
    return v if isinstance(v, int) and not isinstance(v, bool) and v > 0 else 0
