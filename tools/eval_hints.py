#!/usr/bin/env python3
"""Offline check of the idle-recap hint rules (tokenscope/live/recap.py) over a transcripts tree.

    python3 tools/eval_hints.py [--root DIR] [--limit N] [--jobs N] [--sample N --seed S [--fill RULE]]

Every main transcript under DIR (<project>/<session>.jsonl) is replayed with its subagents (<project>/<session>/
subagents/**/agent-*.jsonl) through the same tailer + LiveState code the live server uses, all records merged in
timestamp order. The recap is evaluated at each idle point: the end of a main-thread run of turns right before the
next user prompt, and the end of the transcript -- where the pane would show "done, waiting for you". Prints per
rule: idle points evaluated, firings, sessions affected, firing rate. --sample N prints N random firings (rule,
text, evidence, session id prefix, idle index); with --fill RULE the other rules' firings are sampled first (all of
them when there are fewer than N) and RULE's firings fill the rest; paths are reduced to basenames and prompt text is never printed.
"""
import argparse
import glob
import heapq
import os
import random
import sys
from multiprocessing import Pool

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tokenscope.live import recap as recapmod  # noqa: E402
from tokenscope.live.state import WAITING_S, LiveState, parse_ts  # noqa: E402
from tokenscope.live.tail import Tailer, TrackedFile, classify  # noqa: E402
from tokenscope.parse import real_prompt_text  # noqa: E402

RULES = ["reread", "search_heavy", "test_repeat", "toolsearch", "context", "shared_reads", "polling", "outlier"]
DEFAULT_ROOT = os.path.expanduser("~/.claude/projects")


def sessions(root):
    for path in sorted(glob.glob(os.path.join(root, "*", "*.jsonl"))):
        c = classify(root, path)
        if c and c[2] is None:
            yield path, c


def _is_prompt(r):
    """A record LiveState turns into a user prompt (a person typing, not a notification)."""
    text = real_prompt_text(r)
    if text is None:
        return False
    origin = r.get("origin") if isinstance(r.get("origin"), dict) else {}
    return not text.lstrip().startswith("<task-notification>") and origin.get("kind") in (None, "human")


def _records(tailer, tf, k):
    got = tailer._read(tf)
    last = 0.0
    for n, r in enumerate(got[0] if got else ()):
        last = parse_ts(r.get("timestamp")) or last
        yield (last, k, n, r)


def replay(root, path, c):
    """[(idle index, recap)] for one session: the recap at each idle point (None-free)."""
    sid, proj, _ = c
    tailer = Tailer(root)
    clock = {"now": 0.0}
    state = LiveState(redact=False, clock=lambda: clock["now"])
    files = [TrackedFile(path, sid, proj)]
    for sub in sorted(glob.glob(os.path.join(root, proj, sid, "subagents", "**", "agent-*.jsonl"), recursive=True)):
        sc = classify(root, sub)
        if sc and sc[2]:
            files.append(TrackedFile(sub, sc[0], sc[1], sc[2]))
    merged = heapq.merge(*[_records(tailer, tf, k) for k, tf in enumerate(files)])
    out, batch, s = [], [], None

    def flush():
        nonlocal s, batch
        if batch:
            s = state.ingest(files[batch[0][0]], [r for _, r in batch])
        batch = []

    def idle_point():
        if s is None or not s.main.turn_steps or (s.prompt_at and len(s.main.turn_steps) <= s.prompt_at[-1]):
            return   # no main-thread work since the last prompt: the pane has nothing to recap
        clock["now"] = (s.last_ts or 0) + WAITING_S + 1
        r = state.recap(s)
        if r:
            out.append((len(out), r))

    for _, k, _, r in merged:
        if k == 0 and r.get("type") == "user" and _is_prompt(r):
            flush()
            idle_point()
        if batch and batch[0][0] != k:
            flush()
        batch.append((k, r))
    flush()
    idle_point()
    return sid, out


def _job(args):
    root, path, c = args
    try:
        return replay(root, path, c)
    except Exception as e:   # one broken transcript must not stop the run
        return c[0], e


def main(argv=None):
    p = argparse.ArgumentParser(prog="eval_hints")
    p.add_argument("--root", default=DEFAULT_ROOT)
    p.add_argument("--limit", type=int, default=0)
    p.add_argument("--jobs", type=int, default=os.cpu_count() or 1)
    p.add_argument("--sample", type=int, default=0)
    p.add_argument("--seed", type=int, default=0)
    p.add_argument("--fill", default=None, help="sample other rules' firings first, fill up with this rule's")
    a = p.parse_args(argv)
    root = os.path.abspath(os.path.expanduser(a.root))
    todo = [(root, path, c) for path, c in sessions(root)]
    if a.limit:
        todo = todo[:a.limit]
    with Pool(max(1, a.jobs)) as pool:
        results = pool.map(_job, todo, chunksize=1)
    points = n = errors = 0
    per = {r: {"firings": 0, "sessions": set()} for r in RULES}
    with_any = set()
    firings = []
    for sid, res in results:
        if isinstance(res, Exception):
            errors += 1
            print("error in %s: %s" % (sid[:8], type(res).__name__), file=sys.stderr)
            continue
        n += 1
        points += len(res)
        for idx, r in res:
            for h in r["hints"]:
                per.setdefault(h["rule"], {"firings": 0, "sessions": set()})
                per[h["rule"]]["firings"] += 1
                per[h["rule"]]["sessions"].add(sid)
                with_any.add(sid)
                firings.append((sid, idx, h))
    print("root: %s" % root)
    print("sessions replayed: %d (errors: %d)   idle points evaluated: %d" % (n, errors, points))
    print("thresholds: " + ", ".join("%s=%s" % (k, getattr(recapmod, k)) for k in dir(recapmod)
                                    if k[:2] in ("A_", "B_", "C_", "D_", "E_", "F_", "G_", "H_") or k in
                                    ("MAX_HINTS", "HINT_MIN_CW")))
    print("%-14s %12s %9s %9s %12s" % ("rule", "idle points", "firings", "sessions", "firing rate"))
    for rule, v in per.items():
        print("%-14s %12d %9d %9d %11.1f%%" % (rule, points, v["firings"], len(v["sessions"]),
                                                 100.0 * v["firings"] / points if points else 0.0))
    nf = sum(1 for _, _, r in [(s, i, r) for s, res in results if not isinstance(res, Exception) for i, r in res]
             if r["hints"])
    print("idle points with >= 1 hint: %d/%d (%.1f%%)   sessions with >= 1 hint: %d/%d"
          % (nf, points, 100.0 * nf / points if points else 0.0, len(with_any), n))
    if a.sample:
        rng = random.Random(a.seed)
        if a.fill:
            rest = [f for f in firings if f[2]["rule"] != a.fill]
            filler = [f for f in firings if f[2]["rule"] == a.fill]
            pick = rng.sample(rest, min(a.sample, len(rest)))
            pick += rng.sample(filler, min(a.sample - len(pick), len(filler)))
            rng.shuffle(pick)
        else:
            pick = rng.sample(firings, min(a.sample, len(firings)))
        print("\nsample of %d firings (seed %d%s):" % (len(pick), a.seed, ", %s fills" % a.fill if a.fill else ""))
        for sid, idx, h in pick:
            print("- %s idle#%d [%s] %s\n    evidence: %s\n    cw_at_stake: %s (%s)"
                  % (sid[:8], idx, h["rule"], h["text"], h["evidence"], recapmod.fmt_k(h["cw_at_stake"]),
                     h["estimate"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
