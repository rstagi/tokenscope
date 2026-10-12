"""Transparent turn-intent rules: tool call (+args) -> atomic intent; turn -> highest-priority intent."""
import os
import re

from .goals import bash_write_targets
from .patterns import PAGERS, bash_file_arg, bash_segments
from .understand import test_runner

# priority order (first = wins when a turn mixes intents)
PRIORITY = ["edit", "test", "commit_pr", "delegate", "use_skill", "external_write", "run_build", "compute", "browse_ui",
            "research", "query_data", "gather_context", "review_changes", "read_code", "read_docs", "locate",
            "discover_tools", "wait", "report", "communicate", "coordinate", "plan", "setup", "shell_other", "respond"]
RANK = {k: i for i, k in enumerate(PRIORITY)}
GROUP = {  # coarse phase each intent belongs to
    "locate": "investigate", "read_code": "investigate", "read_docs": "investigate", "review_changes": "investigate",
    "gather_context": "investigate", "compute": "investigate", "query_data": "investigate", "research": "research", "discover_tools": "overhead",
    "edit": "change", "setup": "change", "external_write": "change", "test": "verify", "run_build": "verify",
    "browse_ui": "verify", "commit_pr": "ship", "delegate": "delegate", "use_skill": "delegate", "wait": "wait",
    "report": "report", "communicate": "report", "plan": "plan", "coordinate": "plan", "shell_other": "other", "respond": "respond"}
LABELS = {  # human words for the live view
    "locate": "searching the code", "read_code": "reading code", "read_docs": "reading docs", "edit": "editing files",
    "test": "running tests", "run_build": "running a build/command", "compute": "running a script",
    "review_changes": "checking git state", "commit_pr": "committing / PR", "setup": "setting up",
    "delegate": "delegating to a subagent", "research": "researching the web", "browse_ui": "driving the browser",
    "query_data": "querying data", "external_write": "writing to an external service",
    "gather_context": "gathering context", "discover_tools": "looking up tools", "use_skill": "using a skill",
    "plan": "planning", "wait": "waiting", "communicate": "asking you", "report": "reporting",
    "shell_other": "running a shell command", "coordinate": "coordinating"}


def label(intent):
    """Human label of an intent ("editing files"); unknown -> the intent with _ -> space."""
    return LABELS.get(intent) or (str(intent).replace("_", " ") if intent else None)


DOC_EXT = {".md", ".mdx", ".txt", ".rst", ".pdf", ".png", ".jpg", ".jpeg", ".csv", ".log", ".html"}
HARNESS = set(os.environ.get("TOKENSCOPE_HARNESS_CLIS", "").split(",")) - {""}  # CLIs of an agent harness -> "coordinate"
FILTERS = {"head", "tail", "cut", "sort", "uniq", "wc", "tr", "column", "grep", "egrep", "awk", "sed", "jq", "xargs", "cat", "nl", "less", "rg", "date", "sleep", "echo", "printf", "true"}
SKIP = {"cd", "export", "source", "set", "true", "pwd", "which", "type", "command", "env", "date", "printf", "disown", "wait"}
BROWSER = re.compile(r"browser|chrome|playwright|puppeteer")
WRITE_VERB = re.compile(r"(create|update|post|send|write|add|delete|move|insert|upload|comment|edit|set_|patch|reply|invoke)")
DATA_SRV = re.compile(r"posthog|grafana|sentry|datadog|supabase|postgres|sql|bigquery|stripe|linear")
RESEARCH = re.compile(r"perplexity|search_web|web_search|exa|tavily|context7|docs")


def _ext(p):
    return os.path.splitext(p or "")[1].lower()


SCRIPT_WRITE = re.compile(r"""open\([^)]*,\s*['"][wax]b?\+?['"]|\.write_text\(|\.write_bytes\(|writeFileSync|fs\.writeFile|"""
                          r"""fs\.promises\.writeFile|File\.write\(""")


def bash_intent(t):
    cmd = t.get("cmd") or ""
    if test_runner(cmd):
        # "patch && run tests" in one command: the edit is what changes the code
        return "edit" if SCRIPT_WRITE.search(cmd) else "test"
    best = _bash_intent(t, cmd)
    if best == "compute" and SCRIPT_WRITE.search(cmd):
        return "edit"   # an inline python/node script that writes files is an edit
    return best


def _bash_intent(t, cmd):
    best = None
    for k, seg in enumerate(t.get("segs") or [cmd]):
        w = seg.split()
        if k and best is not None and w and os.path.basename(w[0]) in FILTERS and not (w[0] == "sed" and "-i" in w):
            continue
        if not w:
            continue
        prog = os.path.basename(w[0])
        sub = w[1] if len(w) > 1 else ""
        it = None
        if prog in SKIP or prog == "echo" and ">" not in seg:
            continue
        if prog == "git":
            it = "commit_pr" if sub in ("add", "commit", "push", "merge", "rebase", "cherry-pick", "tag") else \
                "setup" if sub in ("stash", "reset", "checkout", "switch", "worktree", "branch", "pull", "fetch", "clone", "init", "config", "restore", "rm", "mv") else "review_changes"
        elif prog == "gh":
            it = "commit_pr" if (sub == "pr" and len(w) > 2 and w[2] in ("create", "merge", "edit", "close", "comment", "ready")) or sub in ("release",) else "review_changes"
        elif prog in ("grep", "rg", "find", "ls", "tree", "fd", "ag", "wc", "du", "stat", "file", "egrep", "realpath", "dirname", "basename"):
            it = "locate"
        elif prog in ("cat", "sed", "head", "tail", "less", "nl", "awk", "bat", "jq", "more", "diff", "cut", "sort", "uniq", "xxd", "strings", "column", "tr"):
            if (prog == "sed" and "-i" in w) or ">" in seg.replace("2>", "").replace(">&", "") and prog in ("cat", "echo"):
                it = "edit"
            else:
                f = t.get("file") or (w[-1] if len(w) > 1 else "")
                it = "read_docs" if _ext(f) in DOC_EXT or "SKILL" in f or "CLAUDE" in f else "read_code"
        elif prog in ("sleep", "timeout", "wait", "watch"):
            it = "wait"
        elif prog in ("curl", "wget", "http", "httpie"):
            it = "research" if re.search(r"https?://(?!localhost|127\.|0\.0)", seg) and not re.search(r"-X ?(POST|PUT|PATCH|DELETE)", seg) else "run_build"
        elif prog in ("python", "python3", "node", "deno", "bun", "ruby", "perl") and (len(w) == 1 or sub in ("-c", "-e", "-", "-u") or "<<" in seg or sub.startswith("/tmp") or sub.startswith("-")):
            it = "compute"
        elif prog in ("npm", "pnpm", "yarn", "bun", "npx", "node", "tsx", "ts-node", "python", "python3", "uv", "pip", "docker", "make", "cargo", "go", "deno", "vite", "next", "turbo", "tsc", "eslint", "prettier", "biome", "bash", "sh", "zsh", "chromium", "playwright", "ffmpeg", "vercel", "wrangler", "supabase", "psql", "sqlite3", "kubectl", "terraform", "fly", "railway", "modal"):
            it = "run_build"
        elif prog in ("mkdir", "cp", "mv", "rm", "chmod", "ln", "touch", "tar", "unzip", "zip", "rsync", "tee", "install", "brew", "apt", "apt-get"):
            it = "setup"
        elif prog in HARNESS:
            it = "coordinate"
        elif prog in ("ps", "pgrep", "pkill", "kill", "lsof", "top", "uptime"):
            it = "wait"
        elif prog == "agent-browser":
            it = "browse_ui"
        elif re.search(r"poll|watch|wait", prog):
            it = "wait"
        elif prog == "echo":
            it = "edit"
        else:
            it = "shell_other"
        if bash_write_targets(seg) and it in ("read_code", "read_docs", "run_build", "compute", "shell_other", "locate"):
            it = "edit"
        if best is None or RANK[it] < RANK[best]:
            best = it
    return best or "shell_other"


def tool_intent(t):
    n = t["name"]
    if n in ("Edit", "Write", "MultiEdit", "NotebookEdit"):
        return "edit"
    if n == "Bash":
        return bash_intent(t)
    if n == "Read":
        f = t.get("file") or t.get("target") or ""
        return "read_docs" if _ext(f) in DOC_EXT or "SKILL" in f or "CLAUDE" in f else "read_code"
    if n in ("Grep", "Glob", "LS"):
        return "locate"
    if n in ("WebSearch", "WebFetch"):
        return "research"
    if n == "ToolSearch":
        return "discover_tools"
    if n == "Skill":
        return "use_skill"
    if n in ("Agent", "Task", "Workflow", "SendMessage"):
        return "delegate"
    if n in ("TaskOutput", "Monitor", "ScheduleWakeup", "TaskStop", "ListAgents", "ReadNotifications", "BashOutput", "KillShell"):
        return "wait"
    if n in ("StructuredOutput", "Artifact"):
        return "report"
    if n in ("AskUserQuestion", "PushNotification", "SendUserFile"):
        return "communicate"
    if n in ("TodoWrite", "ExitPlanMode", "TaskCreate", "TaskUpdate", "TaskList"):
        return "plan"
    if n.startswith("mcp__"):
        parts = n.split("__")
        srv, op = parts[1].lower(), parts[-1].lower()
        if op == "declare_intent":
            return "plan"   # tokenscope's own intent marker (intent_mcp.py): bookkeeping, not an external write
        if BROWSER.search(srv + "__") or BROWSER.search(op):
            return "browse_ui"
        if RESEARCH.search(srv) or RESEARCH.search(op):
            return "research"
        if WRITE_VERB.search(op) and not re.search(r"(get|list|search|fetch|read|query|find)", op.split("_")[0]):
            return "query_data" if DATA_SRV.search(srv) and "invoke" in op else "external_write"
        if DATA_SRV.search(srv):
            return "query_data"
        return "gather_context"
    return "shell_other"


def mcp_server(name):
    return name.split("__")[1] if name.startswith("mcp__") else None


def turns(tools):
    """Group a transcript's tool records by assistant call -> list of (call, [tools])."""
    out, cur, key = [], [], None
    for t in tools:
        if t["call"] != key and cur:
            out.append((key, cur))
            cur = []
        key = t["call"]
        cur.append(t)
    if cur:
        out.append((key, cur))
    return out


def turn_intent(ts):
    its = [tool_intent(t) for t in ts]
    return (min(its, key=RANK.get) if its else "shell_other"), its


# Claude Code's own output-capture files: a large tool result persisted under <session>/tool-results/, a background
# task's output under .../tasks/, or a random-named .txt / .output in a temp or ~/.claude dir. Reading one back is
# part of the call that produced it, never a file of the project: no target, no step file, no recap fact.
CAPTURE = re.compile(r"(?:^|/)(?:tool-results|tasks)/[^/]+\.(?:txt|output|log|json)$")
CAPTURE_DIR = re.compile(r"(?:^|/)(?:tmp|\.claude)/")
CAPTURE_NAME = re.compile(r"(?=[a-z0-9]*\d)[a-z0-9]{8,12}\.(?:txt|output)")


def is_capture_file(path):
    """True for a Claude Code output-capture file (see CAPTURE)."""
    p = str(path or "").replace("\\", "/")
    if not p:
        return False
    return bool(CAPTURE.search(p) or (CAPTURE_DIR.search(p) and CAPTURE_NAME.fullmatch(os.path.basename(p))))


def record(name, inp):
    """Tool record (the shape the rules read) from a raw tool_use name + input; an output-capture file
    (is_capture_file) is dropped from "file" / "target" and flagged "capture"."""
    inp = inp if isinstance(inp, dict) else {}
    t = {"name": name or "?"}
    if name == "Bash":
        cmd = " ".join(str(inp.get("command", "")).split())
        t["cmd"] = cmd[:4000]   # classification only; never displayed
        t["segs"] = bash_segments(cmd) or [cmd]
        if t["segs"] and t["segs"][0].split() and t["segs"][0].split()[0] in PAGERS:
            t["file"] = bash_file_arg(cmd)
    elif name in ("Read", "Edit", "Write", "MultiEdit", "NotebookEdit"):
        t["file"] = str(inp.get("file_path") or inp.get("notebook_path") or "")
        t["target"] = t["file"]
    elif name in ("Grep", "Glob"):
        t["target"] = str(inp.get("pattern", ""))[:120]
    elif name in ("Agent", "Task"):
        t["target"] = str(inp.get("subagent_type") or "general-purpose")
    elif name == "Skill":
        t["target"] = str(inp.get("skill") or inp.get("name") or inp.get("command") or "")
    elif name in ("WebFetch",):
        t["target"] = str(inp.get("url", ""))[:200]
    elif name in ("WebSearch",):
        t["target"] = str(inp.get("query", ""))[:200]
    if t.get("file") and is_capture_file(t["file"]):
        t["capture"] = True
        t["file"] = ""
        t.pop("target", None)
    return t
