"""Claude CLI plumbing shared by `--name-with-claude` (goals.name_with_claude).

One batched request goes through the local `claude` CLI (`claude -p --model haiku --output-format json
--no-session-persistence --tools ""`), run in the cache dir with stdin closed and a timeout, falling back to the
default model when haiku is unavailable. The reply's first JSON object is parsed; callers cache results and fall back
to names from terms on any failure.

This is the only part of tokenscope that sends anything anywhere: the goal evidence goes to Claude via your own CLI
login.
"""
import json
import os
import re
import subprocess

CACHE_DIR = os.path.join(os.path.expanduser("~"), ".cache", "tokenscope")
TIMEOUT = 180
CLI_FLAGS = ["--output-format", "json", "--no-session-persistence"]


def load_cache(path):
    try:
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def save_cache(path, cache):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cache, f, indent=1, ensure_ascii=False)
    os.replace(tmp, path)


def extract_json(text):
    """The first JSON object in `text` (tolerates code fences and surrounding prose)."""
    text = re.sub(r"```(?:json)?", "", text or "")
    start = text.find("{")
    while start >= 0:
        depth, in_str, esc = 0, False, False
        for i in range(start, len(text)):
            ch = text[i]
            if in_str:
                esc = not esc and ch == "\\"
                if ch == '"' and not esc:
                    in_str = False
                if ch != "\\":
                    esc = False
                continue
            if ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    try:
                        obj = json.loads(text[start:i + 1])
                        if isinstance(obj, dict):
                            return obj
                    except ValueError:
                        pass
                    break
        start = text.find("{", start + 1)
    return None


def _model_of(out, fallback):
    mu = out.get("modelUsage") if isinstance(out, dict) else None
    if isinstance(mu, dict) and mu:
        return max(mu, key=lambda m: (mu[m] or {}).get("outputTokens", 0) if isinstance(mu[m], dict) else 0)
    return fallback


def _run(cmd, runner, cwd):
    """Run one CLI attempt -> (result text, model or None, error or None)."""
    try:
        p = runner(cmd, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=TIMEOUT, cwd=cwd)
    except FileNotFoundError:
        return None, None, "claude CLI not found"
    except subprocess.TimeoutExpired:
        return None, None, "claude CLI timed out after %ds" % TIMEOUT
    except OSError as e:
        return None, None, "claude CLI failed to start: %s" % e
    if p.returncode != 0:
        msg = " ".join(((p.stderr or "") + " " + (p.stdout or "")).split())[:200]
        return None, None, "claude CLI exit %s: %s" % (p.returncode, msg)
    try:
        out = json.loads(p.stdout)
    except ValueError:
        return p.stdout, None, None
    if not isinstance(out, dict):
        return None, None, "unexpected CLI output"
    if out.get("is_error"):
        return None, None, "claude CLI error: %s" % str(out.get("result") or out.get("subtype"))[:200]
    return str(out.get("result") or ""), _model_of(out, None), None


def call_claude(request_text, runner=None, cwd=None):
    """-> (parsed dict, model, error). Tries --model haiku, then the default model.

    Runs in the cache dir (not the user's repo) with --no-session-persistence, so no transcript is written and the
    call doesn't show up as a session in tokenscope itself."""
    runner = runner or subprocess.run
    cwd = cwd or CACHE_DIR
    os.makedirs(cwd, exist_ok=True)
    errors = []
    for model in ("haiku", None):
        # prompt right after -p: --tools takes a variable number of values, so it goes last
        cmd = ["claude", "-p", request_text] + CLI_FLAGS + (["--model", model] if model else []) + ["--tools", ""]
        text, used, err = _run(cmd, runner, cwd)
        if err:
            errors.append(("haiku: " if model else "default model: ") + err)
            continue
        obj = extract_json(text)
        if obj is None:
            errors.append(("haiku: " if model else "default model: ") + "no JSON object in reply")
            continue
        return obj, used or (model or "default"), None
    return None, None, "; ".join(errors)


def _source(model):
    m = str(model or "").lower()
    for fam in ("haiku", "sonnet", "opus", "fable"):
        if fam in m:
            return "claude-" + fam
    return "claude-" + (m or "default")
