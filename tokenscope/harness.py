"""Harness-agnostic adapter framework for coding agent transcripts.

Supports multiple agent harnesses (Claude Code, Google Antigravity CLI/IDE)
with automatic detection and canonical event normalization.
"""
import glob
import json
import os
import re
import sqlite3
from typing import Any, Dict, List, Optional, Tuple


def _unwrap_val(v):
    if isinstance(v, str):
        v = v.strip()
        if (v.startswith('"') and v.endswith('"')) or (v.startswith("'") and v.endswith("'")):
            try:
                return json.loads(v)
            except Exception:
                return v[1:-1]
    return v


def _unwrap_args(raw_args):
    if not isinstance(raw_args, dict):
        return {}
    return {k: _unwrap_val(v) for k, v in raw_args.items()}


def clean_user_prompt(text: str) -> str:
    """Extract clean user prompt from antigravity or formatted user messages."""
    if not isinstance(text, str):
        return ""
    if "<USER_REQUEST>" in text and "</USER_REQUEST>" in text:
        parts = text.split("<USER_REQUEST>", 1)
        req = parts[1].split("</USER_REQUEST>", 1)[0]
        return req.strip()
    # Strip trailing metadata blocks if present
    for tag in ("<ADDITIONAL_METADATA>", "<USER_SETTINGS_CHANGE>", "<SYSTEM_MESSAGE>"):
        if tag in text:
            text = text.split(tag, 1)[0]
    return text.strip()


class Harness:
    """Base class for coding agent harness adapters."""
    name: str = "base"

    def is_available(self) -> bool:
        return False

    def default_root(self) -> str:
        raise NotImplementedError

    def detect(self, path_or_root: str) -> bool:
        return False

    def discover(self, root: Optional[str] = None) -> list:
        raise NotImplementedError

    def read_events(self, path: str) -> list:
        raise NotImplementedError

    def tail_candidates(self, root: str, project: str = "*") -> list:
        raise NotImplementedError

    def classify_tail_path(self, root: str, path: str) -> Optional[Tuple[str, str, Optional[str]]]:
        raise NotImplementedError

    def normalize_record(self, raw_record: dict, state: Any = None) -> list:
        """Translate a single streaming record from tailer into canonical records."""
        return [raw_record]


class ClaudeHarness(Harness):
    """Adapter for Claude Code transcripts."""
    name = "claude"

    def __init__(self):
        self._default_root = os.path.join(os.path.expanduser("~"), ".claude", "projects")

    def default_root(self) -> str:
        return os.path.expanduser(os.environ.get("TOKENSCOPE_ROOT") or self._default_root)

    def is_available(self) -> bool:
        return os.path.isdir(self.default_root())

    def detect(self, path_or_root: str) -> bool:
        if not path_or_root:
            return False
        norm = os.path.normpath(path_or_root)
        if ".claude" in norm:
            return True
        if os.path.isdir(norm):
            # check if directory has slug/*.jsonl
            try:
                for entry in os.listdir(norm):
                    sub = os.path.join(norm, entry)
                    if os.path.isdir(sub):
                        for f in os.listdir(sub):
                            if f.endswith(".jsonl") and not f.startswith("."):
                                return True
            except OSError:
                pass
        elif norm.endswith(".jsonl"):
            # check first line
            try:
                with open(norm, "rb") as f:
                    first = f.readline()
                    if b'"type":"user"' in first or b'"type":"assistant"' in first or b'"type": "user"' in first:
                        return True
            except OSError:
                pass
        return False

    def discover(self, root: Optional[str] = None) -> list:
        from .parse import Session, resolve_root
        target = resolve_root(root, harness="claude")
        out = []
        if not os.path.isdir(target):
            return out
        for slug in sorted(os.listdir(target)):
            d = os.path.join(target, slug)
            if not os.path.isdir(d):
                continue
            for name in sorted(os.listdir(d)):
                p = os.path.join(d, name)
                if name.endswith(".jsonl") and os.path.isfile(p):
                    out.append(Session(p, slug, harness="claude"))
        return out

    def read_events(self, path: str) -> list:
        from .parse import compact_event, read_jsonl
        names = {}
        out = []
        for ev in read_jsonl(path):
            c = compact_event(ev, names)
            if c is not None:
                out.append(c)
        return out

    def tail_candidates(self, root: str, project: str = "*") -> list:
        proj = project or "*"
        pats = (os.path.join(root, proj, "*.jsonl"),
                os.path.join(root, proj, "*", "subagents", "**", "agent-*.jsonl"))
        out = []
        for p in pats:
            out.extend(glob.glob(p, recursive=True))
        return out

    def classify_tail_path(self, root: str, path: str) -> Optional[Tuple[str, str, Optional[str]]]:
        rel = os.path.relpath(path, root).split(os.sep)
        name = rel[-1]
        if len(rel) == 2 and name.endswith(".jsonl"):
            return name[:-6], rel[0], None
        if len(rel) >= 4 and rel[2] == "subagents" and name.startswith("agent-") and name.endswith(".jsonl"):
            return rel[1], rel[0], name[len("agent-"):-6]
        return None

    def normalize_record(self, raw_record: dict, state: Any = None) -> list:
        return [raw_record]


class AntigravityHarness(Harness):
    """Adapter for Google Antigravity CLI/IDE transcripts and brains."""
    name = "antigravity"

    def __init__(self):
        self.app_data_dir = os.path.join(os.path.expanduser("~"), ".gemini", "antigravity-cli")
        self._default_brain = os.path.join(self.app_data_dir, "brain")
        self._summaries_cache = None
        self._summaries_mtime = 0
        self._model_cache = None

    def default_root(self) -> str:
        env_root = os.environ.get("TOKENSCOPE_ROOT")
        if env_root:
            return os.path.expanduser(env_root)
        if os.path.isdir(self._default_brain):
            return self._default_brain
        return self.app_data_dir

    def is_available(self) -> bool:
        return os.path.isdir(self._default_brain) or os.path.isdir(self.app_data_dir)

    def detect(self, path_or_root: str) -> bool:
        if not path_or_root:
            return False
        norm = os.path.normpath(path_or_root)
        if "antigravity-cli" in norm or ".system_generated" in norm:
            return True
        if os.path.isdir(norm):
            if os.path.isfile(os.path.join(norm, "conversation_summaries.db")):
                return True
            try:
                for entry in os.listdir(norm):
                    t = os.path.join(norm, entry, ".system_generated", "logs", "transcript.jsonl")
                    if os.path.isfile(t):
                        return True
            except OSError:
                pass
            return False
        elif norm.endswith(".jsonl"):
            try:
                with open(norm, "rb") as f:
                    for _ in range(10):
                        line = f.readline()
                        if not line:
                            break
                        if b'"step_index"' in line or b'"PLANNER_RESPONSE"' in line or b'"USER_INPUT"' in line:
                            return True
            except OSError:
                pass
        return False

    def get_configured_model(self) -> str:
        if self._model_cache:
            return self._model_cache
        settings_path = os.path.join(self.app_data_dir, "settings.json")
        try:
            with open(settings_path, encoding="utf-8") as f:
                d = json.load(f)
                if isinstance(d, dict) and d.get("model"):
                    self._model_cache = str(d["model"])
                    return self._model_cache
        except Exception:
            pass
        return "Gemini 3.8 Flash (High)"

    def load_summaries(self) -> Dict[str, dict]:
        db_path = os.path.join(self.app_data_dir, "conversation_summaries.db")
        if not os.path.isfile(db_path):
            return {}
        try:
            mtime = os.path.getmtime(db_path)
            if self._summaries_cache is not None and mtime == self._summaries_mtime:
                return self._summaries_cache
            summaries = {}
            conn = sqlite3.connect(db_path)
            cur = conn.cursor()
            cur.execute("SELECT conversation_id, preview, workspace_uris, status, last_modified_time, "
                        "parent_conversation_id, nesting_depth FROM conversation_summaries")
            for cid, prev, uris, st, lm_time, parent, depth in cur.fetchall():
                proj = "antigravity"
                cwd = None
                if uris:
                    try:
                        u = json.loads(uris)
                        if u and isinstance(u, list) and u[0].startswith("file://"):
                            cwd = u[0][7:].rstrip("/")
                            proj = os.path.basename(cwd) or "antigravity"
                    except Exception:
                        pass
                summaries[cid] = {
                    "preview": prev or "",
                    "project": proj,
                    "cwd": cwd,
                    "status": st or "",
                    "last_modified": lm_time,
                    "parent": parent or "",
                    "depth": depth or 0,
                }
            conn.close()
            self._summaries_cache = summaries
            self._summaries_mtime = mtime
            return summaries
        except Exception:
            return {}

    def _resolve_brain_root(self, root: Optional[str] = None) -> str:
        base = os.path.expanduser(root or self.default_root())
        if os.path.isdir(os.path.join(base, "brain")):
            return os.path.join(base, "brain")
        return base

    def discover(self, root: Optional[str] = None) -> list:
        from .parse import Session
        brain_dir = self._resolve_brain_root(root)
        summaries = self.load_summaries()
        out = []
        if not os.path.isdir(brain_dir):
            return out

        # Find all transcripts: <brain_dir>/<conversation_id>/.system_generated/logs/transcript.jsonl
        for cid in sorted(os.listdir(brain_dir)):
            log_path = os.path.join(brain_dir, cid, ".system_generated", "logs", "transcript.jsonl")
            if not os.path.isfile(log_path):
                continue
            meta = summaries.get(cid, {})
            slug = meta.get("project") or "antigravity"
            s = Session(log_path, slug, harness="antigravity")
            s.id = cid
            if meta.get("cwd"):
                s.cwd = meta["cwd"]
            if meta.get("preview"):
                s.first_prompt = meta["preview"]
            out.append(s)
        return out

    def to_canonical_event(self, raw_record: dict, open_tool_calls: list) -> Optional[dict]:
        """Convert a single raw Antigravity transcript record into a canonical event."""
        typ = raw_record.get("type")
        ts = raw_record.get("created_at")
        sidx = raw_record.get("step_index", 0)

        if typ == "USER_INPUT":
            content = clean_user_prompt(raw_record.get("content", ""))
            return {
                "type": "user",
                "timestamp": ts,
                "uuid": f"step_{sidx}",
                "step_index": sidx,
                "message": {
                    "role": "user",
                    "content": content
                }
            }
        elif typ == "PLANNER_RESPONSE":
            blocks = []
            if raw_record.get("thinking"):
                blocks.append({
                    "type": "thinking",
                    "thinking": raw_record["thinking"]
                })
            if raw_record.get("content"):
                blocks.append({
                    "type": "text",
                    "text": raw_record["content"]
                })
            tool_calls = raw_record.get("tool_calls") or []
            for tidx, tc in enumerate(tool_calls):
                call_id = f"call_{sidx}_{tidx}"
                tname = tc.get("name")
                open_tool_calls.append((call_id, tname))
                args = _unwrap_args(tc.get("args"))
                # Canonical mapping for Antigravity tool arguments
                if tname in ("run_command", "run_shell_command"):
                    if "CommandLine" in args and "command" not in args:
                        args["command"] = args["CommandLine"]
                elif tname in ("read_file", "view_file", "edit_file", "write_file", "write_to_file", "replace_file_content"):
                    f = args.get("Path") or args.get("AbsolutePath") or args.get("TargetFile")
                    if f and "file_path" not in args:
                        args["file_path"] = f
                elif tname in ("grep_search", "list_dir", "list_directory"):
                    pat = args.get("Query") or args.get("DirectoryPath") or args.get("SearchPath")
                    if pat and "pattern" not in args:
                        args["pattern"] = pat
                elif tname in ("invoke_subagent", "manage_subagents", "send_message"):
                    stype = args.get("Role") or args.get("TypeName")
                    if stype and "subagent_type" not in args:
                        args["subagent_type"] = stype
                    desc = args.get("Prompt") or args.get("Message")
                    if desc and "description" not in args:
                        args["description"] = desc
                sm = args.get("toolSummary") or args.get("summary") or args.get("Description") or args.get("Instruction")
                if sm:
                    args["toolSummary"] = sm
                    args["summary"] = sm
                blocks.append({
                    "type": "tool_use",
                    "id": call_id,
                    "name": tname,
                    "input": args
                })
            model = self.get_configured_model()
            stop_reason = "tool_use" if tool_calls else "end_turn"
            return {
                "type": "assistant",
                "timestamp": ts,
                "uuid": f"step_{sidx}",
                "step_index": sidx,
                "message": {
                    "id": f"msg_step_{sidx}",
                    "role": "assistant",
                    "model": model,
                    "stop_reason": stop_reason,
                    "usage": {
                        "input_tokens": raw_record.get("input_tokens", 0),
                        "output_tokens": raw_record.get("output_tokens", 0),
                        "cache_read_input_tokens": raw_record.get("cache_read_tokens", 0),
                        "cache_creation_input_tokens": 0,
                    },
                    "content": blocks
                }
            }
        elif open_tool_calls and typ in ("GENERIC", "RUN_COMMAND", "VIEW_FILE", "CODE_ACTION", "ERROR_MESSAGE",
                                         "GREP_SEARCH", "LIST_DIRECTORY"):
            call_id, _tool_name = open_tool_calls.pop(0)
            return {
                "type": "user",
                "timestamp": ts,
                "uuid": f"res_{sidx}",
                "step_index": sidx,
                "message": {
                    "role": "user",
                    "content": [{
                        "type": "tool_result",
                        "tool_use_id": call_id,
                        "content": raw_record.get("content", ""),
                        "is_error": bool(raw_record.get("error")) or raw_record.get("status") == "ERROR"
                    }]
                }
            }
        return None

    def read_events(self, path: str) -> list:
        from .parse import compact_event, read_jsonl
        names = {}
        out = []
        open_tool_calls = []
        for raw in read_jsonl(path):
            cev = self.to_canonical_event(raw, open_tool_calls)
            if cev is not None:
                c = compact_event(cev, names)
                if c is not None:
                    out.append(c)
        return out

    def tail_candidates(self, root: str, project: str = "*") -> list:
        brain_dir = self._resolve_brain_root(root)
        pats = [
            os.path.join(brain_dir, "*", ".system_generated", "logs", "transcript.jsonl"),
            os.path.join(root, "*", ".system_generated", "logs", "transcript.jsonl"),
            os.path.join(root, "brain", "*", ".system_generated", "logs", "transcript.jsonl"),
        ]
        out = []
        seen = set()
        for pat in pats:
            for p in glob.glob(pat):
                if p not in seen:
                    seen.add(p)
                    out.append(p)
        return out

    def classify_tail_path(self, root: str, path: str) -> Optional[Tuple[str, str, Optional[str]]]:
        norm = os.path.normpath(path)
        parts = norm.split(os.sep)
        # Expected: .../brain/<conversation-id>/.system_generated/logs/transcript.jsonl
        if len(parts) >= 4 and parts[-1] == "transcript.jsonl" and parts[-3] == ".system_generated":
            session_id = parts[-4]
            summaries = self.load_summaries()
            meta = summaries.get(session_id, {})
            project = meta.get("project") or "antigravity"
            parent = meta.get("parent") or None
            agent_id = session_id if parent else None
            effective_sid = parent if parent else session_id
            return effective_sid, project, agent_id
        return None

    def normalize_record(self, raw_record: dict, state: Any = None) -> list:
        """Streaming translation for LiveState.ingest."""
        if not hasattr(state, "_antigravity_open_tools"):
            state._antigravity_open_tools = []
        cev = self.to_canonical_event(raw_record, state._antigravity_open_tools)
        return [cev] if cev is not None else []


# Registry of harnesses
HARNESSES: Dict[str, Harness] = {
    "claude": ClaudeHarness(),
    "antigravity": AntigravityHarness(),
}


def get_harness(name: Optional[str] = None, root: Optional[str] = None, path: Optional[str] = None) -> Harness:
    """Resolve the appropriate harness adapter by name, path/root sniffing, or auto-detection."""
    # 1. Explicit name or environment variable
    h_name = name or os.environ.get("TOKENSCOPE_HARNESS")
    if h_name and h_name.lower() in HARNESSES:
        return HARNESSES[h_name.lower()]

    # 2. Check path or root hint
    target = path or root
    if target:
        if HARNESSES["antigravity"].detect(target):
            return HARNESSES["antigravity"]
        if HARNESSES["claude"].detect(target):
            return HARNESSES["claude"]
        return HARNESSES["claude"]

    # 3. Environment Auto-detection
    # Prefer Antigravity if available and has brain records, else Claude
    ag = HARNESSES["antigravity"]
    cl = HARNESSES["claude"]
    if ag.is_available() and not cl.is_available():
        return ag
    if cl.is_available() and not ag.is_available():
        return cl
    if ag.is_available() and cl.is_available():
        # Check which has sessions
        ag_has = bool(ag.discover())
        cl_has = bool(cl.discover())
        if ag_has and not cl_has:
            return ag
        if cl_has and not ag_has:
            return cl

    return HARNESSES["antigravity"] if ag.is_available() else HARNESSES["claude"]
