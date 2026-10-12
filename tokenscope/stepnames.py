"""Model labels for closed steps (docs/intents-spec.md section 4): one `claude -p --model haiku` call per batch of
<= 12 steps, which also proposes intent groups. The input is masked (goalname.mask), the call runs in the neutral
temp dir used for goal naming, answers are cached by a hash of the batch input. Never raises.

Cache: $TOKENSCOPE_CACHE_DIR/step-labels.json (default ~/.cache/tokenscope/step-labels.json).
"""
import hashlib
import json
import os
import re
import subprocess
import threading

from .live import goalname
from .steps import clip_words, summary

BATCH = 12
STATS = {"calls": 0, "cache_hits": 0, "tokens": 0, "usd": 0.0, "errors": 0}
_LOCK = threading.Lock()

PROMPT = """You label the steps of a coding assistant's session. For each step give a short label (3-7 words,
imperative or noun phrase, e.g. "Read store and model code", "Add per-warehouse quantities"), using only what the
step data says. Do not guess project or product names. Then optionally propose groups of >= 2 consecutive steps that
share one purpose, with a short label for the group.
Answer with JSON only: {"labels": {"<i>": "<label>"}, "groups": [{"label": "...", "first": <i>, "last": <i>}]}
%sSteps (i = step index, cat = category, text = the assistant's words, masked):
%s"""


def cache_path():
    d = os.environ.get("TOKENSCOPE_CACHE_DIR") or os.path.join(os.path.expanduser("~"), ".cache", "tokenscope")
    return os.path.join(d, "step-labels.json")


def _load(path):
    try:
        with open(path, encoding="utf-8") as f:
            c = json.load(f)
        return c if isinstance(c, dict) else {}
    except (OSError, ValueError):
        return {}


def _save(path, key, value):
    with _LOCK:
        c = _load(path)
        c[key] = value
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(c, f)
            os.replace(tmp, path)
        except OSError:
            pass


def batch_input(steps, goal=None, batch=BATCH):
    rows = []
    for s in steps[:batch]:
        rows.append({"i": s["i"], "cat": s.get("cat"), "tools": summary(s), "files": (s.get("files") or [])[:6],
                     "cmds": (s.get("cmds") or [])[:4], "text": goalname.mask(s.get("text") or "")[:300]})
    return {"goal": goalname.mask(goal)[:120] if goal else None, "steps": rows}


def _json_obj(text):
    text = str(text or "")
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        return None
    try:
        out = json.loads(m.group(0))
    except ValueError:
        return None
    return out if isinstance(out, dict) else None


def _clean(ans, ids):
    labels, groups = {}, []
    raw = ans.get("labels") if isinstance(ans.get("labels"), dict) else {}
    for k, v in raw.items():
        try:
            i = int(k)
        except (TypeError, ValueError):
            continue
        if i in ids and isinstance(v, str) and v.strip():
            labels[i] = clip_words(v)
    for g in ans.get("groups") if isinstance(ans.get("groups"), list) else []:
        if not isinstance(g, dict) or not isinstance(g.get("label"), str):
            continue
        try:
            a, b = int(g.get("first")), int(g.get("last"))
        except (TypeError, ValueError):
            continue
        if a in ids and b in ids and b > a and g["label"].strip():
            groups.append({"label": clip_words(g["label"]), "first": a, "last": b})
    return labels, groups


def label_batch(steps, goal=None, runner=subprocess.run, cache=None, timeout=90, batch=BATCH):
    """{"labels": {i: label}, "groups": [{"label", "first", "last"}], "usage": {"calls", "tokens", "usd",
    "cached"}} for up to `batch` (default 12) closed steps; empty labels / groups on any failure."""
    empty = {"labels": {}, "groups": [], "usage": {"calls": 0, "tokens": 0, "usd": 0.0, "cached": False}}
    try:
        inp = batch_input(steps, goal, batch)
        ids = {r["i"] for r in inp["steps"]}
        if not ids:
            return empty
        blob = json.dumps(inp, sort_keys=True)
        key = hashlib.sha256(blob.encode("utf-8")).hexdigest()[:32]
        path = cache or cache_path()
        hit = _load(path).get(key)
        if isinstance(hit, dict):
            labels, groups = _clean(hit, ids)
            with _LOCK:
                STATS["cache_hits"] += 1
            return {"labels": labels, "groups": groups, "usage": dict(empty["usage"], cached=True)}
        goal_line = "Session goal (masked): %s\n" % inp["goal"] if inp["goal"] else ""
        rows = "\n".join(json.dumps(r) for r in inp["steps"])
        p = runner(["claude", "-p", "--model", "haiku", "--tools", "", "--output-format", "json",
                    PROMPT % (goal_line, rows)], capture_output=True, text=True, timeout=timeout,
                   cwd=goalname._neutral_dir())
        out = json.loads(p.stdout) if p.returncode == 0 else None
        if not isinstance(out, dict):
            raise ValueError("bad CLI output")
        u = out.get("usage") if isinstance(out.get("usage"), dict) else {}
        tokens = sum(int(u.get(k) or 0) for k in ("input_tokens", "output_tokens", "cache_creation_input_tokens",
                                                   "cache_read_input_tokens"))
        usd = float(out.get("total_cost_usd") or out.get("cost_usd") or 0)
        usage = {"calls": 1, "tokens": tokens, "usd": round(usd, 6), "cached": False}
        with _LOCK:
            STATS["calls"] += 1
            STATS["tokens"] += tokens
            STATS["usd"] = round(STATS["usd"] + usd, 6)
        ans = _json_obj(out.get("result"))
        if ans is None:
            return dict(empty, usage=usage)
        labels, groups = _clean(ans, ids)
        if labels:
            _save(path, key, {"labels": {str(k): v for k, v in labels.items()}, "groups": groups})
        return {"labels": labels, "groups": groups, "usage": usage}
    except Exception:  # noqa: BLE001 - labelling must never break the caller
        with _LOCK:
            STATS["errors"] += 1
        return empty


class StepNamer:
    """Background batches for the live server: `request(key, steps, goal)` runs label_batch in a thread and calls
    `done(key, result)`; one batch in flight per key."""

    def __init__(self, done, labeller=label_batch):
        self.done, self.labeller = done, labeller
        self.busy, self.lock = set(), threading.Lock()

    def request(self, key, steps, goal=None):
        with self.lock:
            if key in self.busy:
                return False
            self.busy.add(key)
        threading.Thread(target=self._run, args=(key, steps, goal), daemon=True).start()
        return True

    def _run(self, key, steps, goal):
        try:
            res = self.labeller(steps, goal)
        except Exception:  # noqa: BLE001
            res = {"labels": {}, "groups": [], "usage": {}}
        with self.lock:
            self.busy.discard(key)
        self.done(key, [s["i"] for s in steps], res)
