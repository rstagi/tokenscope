#!/usr/bin/env python3
"""Offline run of the alert detectors (tokenscope/alerts.py) over recorded traces, privacy-safe.

    python3 tools/alerts_eval.py --root DIR [--root DIR ...] [--events FILE ...] [--off-task] [--alert KEY=VALUE]
                                 [--config FILE] [--limit N] [--per-trace] [--sample N] [--json FILE]

--root: a Claude Code transcripts tree (<project>/<session>.jsonl, subagents under <project>/<session>/subagents/
at any depth), ingested through the same tailer + LiveState code as `tokenscope live`. --events: a JSONL of
per-session `llm_request` / `invoke` events (another harness's trace export: usage per request, tool name + args +
error + result size per call), turned into transcript records first; no prompts there, and a call's result is only
known by its size, so "same result" means "same size".

Prints per trace (--per-trace) and in total the alerts per kind and rule. Traces are named by an ordinal and their
session id prefix, never by project or path; messages are the alerts' own (already privacy-safe) text. --sample N
prints up to N alerts per rule with their step and details, for hand-checking.
"""
import argparse
import copy
import glob
import json
import os
import random
import sys
from collections import Counter, defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tokenscope import alerts as alertsmod  # noqa: E402
from tokenscope.live.state import LiveState  # noqa: E402
from tokenscope.live.tail import Tailer, TrackedFile, classify  # noqa: E402


def transcripts(root):
    """(main path, [(subagent path, agent id)], session id, project dir) per session under root."""
    for path in sorted(glob.glob(os.path.join(root, "*", "*.jsonl"))):
        c = classify(root, path)
        if not c or c[2] is not None:
            continue
        sid, proj, _ = c
        subs = []
        for sub in sorted(glob.glob(os.path.join(root, proj, sid, "subagents", "**", "agent-*.jsonl"), recursive=True)):
            sc = classify(root, sub)
            if sc and sc[2]:
                subs.append((sub, sc[2]))
        yield path, subs, sid, proj


def ingest_transcript(state, tailer, path, subs, sid, proj):
    s = None
    for p, aid in [(path, None)] + subs:
        tf = TrackedFile(p, sid, proj, aid)
        got = tailer._read(tf)
        if got is not None:
            s = state.ingest(tf, got[0], got[1])
    return s


class _Events:
    """A fake TrackedFile for event-export traces."""

    def __init__(self, sid):
        self.path, self.session_id, self.project_dir, self.agent_id = "events:" + sid, sid, "events", None

    def load_meta(self):
        return None


def event_records(evs):
    """Transcript-shaped records from one session's llm_request / invoke events (in time order)."""
    recs, by_turn = [], defaultdict(list)
    for e in evs:
        if e.get("type") == "invoke":
            by_turn[str(e.get("turn"))].append(e)
    for e in evs:
        if e.get("type") != "llm_request":
            continue
        calls = by_turn.get(str(e.get("turn")), [])
        blocks = [{"type": "tool_use", "id": c.get("invocation_id") or "%s:%d" % (e.get("turn"), k),
                   "name": c.get("tool_id") or "?", "input": c.get("args") if isinstance(c.get("args"), dict) else {}}
                  for k, c in enumerate(calls)]
        recs.append({"type": "assistant", "timestamp": e.get("ts"), "message": {
            "id": "turn:%s" % e.get("turn"), "model": e.get("model"), "content": blocks,
            "stop_reason": e.get("stop_reason"), "usage": e.get("usage") or {}}})
        for b, c in zip(blocks, calls):
            err = str(c.get("error")).lower() == "true"
            recs.append({"type": "user", "timestamp": c.get("ts_end") or c.get("ts"), "message": {
                "role": "user", "content": [{"type": "tool_result", "tool_use_id": b["id"], "is_error": err,
                                             "content": "result of %s chars" % c.get("result_chars")}]}})
    return recs


def event_sessions(path):
    by = defaultdict(list)
    with open(path, encoding="utf-8") as f:
        for line in f:
            try:
                e = json.loads(line)
            except ValueError:
                continue
            by[str(e.get("session_id"))].append(e)
    for sid, evs in sorted(by.items()):
        yield sid, sorted(evs, key=lambda e: str(e.get("ts")))


def evaluate(state, s, source, n):
    steps, _ = state.steps(s, final=True) if s.main.turn_steps else ([], [])
    al = state.alerts_public(s)
    return {"trace": "%s#%d" % (source, n), "sid": s.id[:8], "steps": len(steps), "agents": len(s.agents),
            "usd": round(s.totals["usd"], 2), "alerts": al}


def main(argv=None):
    p = argparse.ArgumentParser(prog="alerts_eval")
    p.add_argument("--root", action="append", default=[])
    p.add_argument("--events", action="append", default=[])
    p.add_argument("--config", default=None)
    p.add_argument("--alert", action="append", default=[], metavar="KEY=VALUE")
    p.add_argument("--off-task", action="store_true")
    p.add_argument("--limit", type=int, default=0, help="traces per source (0: all)")
    p.add_argument("--per-trace", action="store_true")
    p.add_argument("--sample", type=int, default=0)
    p.add_argument("--seed", type=int, default=7)
    p.add_argument("--json", default=None, help="write every trace's alerts (privacy-safe) to this file")
    a = p.parse_args(argv)
    cfg = alertsmod.load_config(a.config, a.alert, off_task=True if a.off_task else None) if a.config else \
        _defaults(a.alert, a.off_task)
    rows = []
    for k, root in enumerate(a.root):
        root = os.path.abspath(os.path.expanduser(root))
        tailer = Tailer(root)
        src = "root%d" % (k + 1)
        for n, (path, subs, sid, proj) in enumerate(transcripts(root)):
            if a.limit and n >= a.limit:
                break
            state = LiveState(redact=True, alerts_config=cfg)
            s = ingest_transcript(state, tailer, path, subs, sid, proj)
            if s is not None and s.totals["turns"]:
                rows.append(evaluate(state, s, src, n + 1))
    for k, path in enumerate(a.events):
        src = "events%d" % (k + 1)
        for n, (sid, evs) in enumerate(event_sessions(os.path.expanduser(path))):
            if a.limit and n >= a.limit:
                break
            state = LiveState(redact=True, alerts_config=cfg)
            s = state.ingest(_Events(sid), event_records(evs))
            if s.totals["turns"]:
                rows.append(evaluate(state, s, src, n + 1))
    report(rows, a, cfg)
    if a.json:
        with open(a.json, "w", encoding="utf-8") as f:
            json.dump(rows, f, indent=1)
    return 0


def _defaults(overrides, off_task):
    """Built-in defaults + --alert overrides (never the user's own config file unless --config names it)."""
    cfg = copy.deepcopy(alertsmod.DEFAULTS)
    for o in overrides:
        k, v = o.split("=", 1)
        alertsmod.set_key(cfg, k.strip(), v.strip())
    if off_task:
        cfg["off_task"]["enabled"] = True
    return cfg


def report(rows, a, cfg):
    kinds = alertsmod.KINDS
    by_src = defaultdict(list)
    for r in rows:
        by_src[r["trace"].split("#")[0]].append(r)
    print("alerts eval: off_task %s; %d traces" % ("on" if cfg["off_task"]["enabled"] else "off", len(rows)))
    if a.per_trace:
        print("\n%-12s %-9s %6s %5s %9s  %s" % ("trace", "session", "steps", "agts", "usd", "  ".join(kinds)))
        for r in rows:
            c = Counter(x["kind"] for x in r["alerts"])
            rules = Counter(x["rule"] for x in r["alerts"])
            print("%-12s %-9s %6d %5d %9.2f  %s   %s" % (
                r["trace"], r["sid"], r["steps"], r["agents"], r["usd"],
                "  ".join("%*d" % (len(k), c.get(k, 0)) for k in kinds),
                " ".join("%s=%d" % kv for kv in sorted(rules.items()))))
    print()
    for src, rs in sorted(by_src.items()):
        steps = sum(r["steps"] for r in rs)
        print("%s: %d traces, %d main steps" % (src, len(rs), steps))
        per = defaultdict(lambda: [0, 0])
        for r in rs:
            for rule in set((x["kind"], x["rule"]) for x in r["alerts"]):
                per[rule][0] += 1
            for x in r["alerts"]:
                per[(x["kind"], x["rule"])][1] += 1
        for kind in kinds:
            sub = {k: v for k, v in per.items() if k[0] == kind}
            tot = sum(v[1] for v in sub.values())
            ses = sum(1 for r in rs if any(x["kind"] == kind for x in r["alerts"]))
            print("  %-9s %4d alerts in %3d traces  %s" % (kind, tot, ses, "  ".join(
                "%s %d (%d traces)" % (k[1], v[1], v[0]) for k, v in sorted(sub.items()))))
    if a.sample:
        rnd = random.Random(a.seed)
        pool = defaultdict(list)
        for r in rows:
            for x in r["alerts"]:
                pool[(x["kind"], x["rule"])].append((r, x))
        print("\nsample (up to %d per rule):" % a.sample)
        for key in sorted(pool):
            got = pool[key]
            pick = rnd.sample(got, min(a.sample, len(got)))
            print("  %s/%s (%d)" % (key[0], key[1], len(got)))
            for r, x in pick:
                print("    %s %s step %s%s [%s] %s  %s" % (
                    r["trace"], r["sid"], x["first"] if x["first"] == x["step"] else "%s-%s" % (x["first"], x["step"]),
                    " agent" if x["agent"] else "", x["severity"], x["message"],
                    json.dumps({k: v for k, v in x["details"].items() if k not in ("target",)})))


if __name__ == "__main__":
    sys.exit(main())
