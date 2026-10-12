"""Steps and intent groups of every main session under a projects root, offline (docs/intents-spec.md).

Replays each main transcript (+ its subagents) through the live state, segments its steps, labels them from the
agent's own signals (todo / subagent / declared / plan) and then, within a budget, with Haiku (stepnames), the most
expensive sessions first. Steps left without a model label lock their rule label. Haiku answers are cached
(TOKENSCOPE_CACHE_DIR), so a rerun with the same budget makes no new calls.

    python3 tools/intents_offline.py [--root DIR] [--out FILE] [--max-haiku-calls 80] [--batch 25] [--jobs N]

Output: {session_id: {"steps": [...], "groups": [...], "totals_by_cat": {cat: {"cw", "usd", "steps"}}}}.
"""
import argparse
import glob
import json
import os
import sys
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from multiprocessing import Pool

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("TOKENSCOPE_CACHE_DIR", os.path.expanduser("~/.cache/tokenscope"))

from tokenscope import stepnames, steps as sp  # noqa: E402
from tokenscope.live.replay import replay  # noqa: E402

STEP_KEYS = ("i", "cat", "label", "source", "cw", "usd", "turns", "ts0", "ts1", "tested", "prompt_before",
             "turn_first", "turn_last")


def main_transcripts(root):
    return sorted(glob.glob(os.path.join(root, "*", "*.jsonl")))


def replay_one(path):
    """(session id, goal name, closed steps, declare_intent calls) of one transcript; None if it has no turns."""
    try:
        state, s = replay(path, redact=False, model_labels=True)
    except Exception as e:  # noqa: BLE001 - one bad transcript must not stop the run
        print("replay failed: %s: %s" % (path, e), file=sys.stderr)
        return None
    if s is None or not s.main.turn_steps:
        return None
    turns, events = state.segment_input(s)
    steps = sp.segment(turns, events, open_last=False)
    decl = sum(1 for t in s.main.turn_steps for x in t.get("_raw") or () if sp.is_declare(x["name"]))
    return s.id, (s.goal or {}).get("name"), steps, decl


def plan_batches(sessions, max_calls, batch):
    """[(sid, steps batch)] of steps still needing a model label, most expensive sessions first, at most max_calls
    batches. `sessions`: {sid: {"cw", "pending": [steps]}}."""
    out = []
    for sid in sorted(sessions, key=lambda k: (-sessions[k]["cw"], k)):
        pend = sessions[sid]["pending"]
        for j in range(0, len(pend), batch):
            if len(out) >= max_calls:
                return out
            out.append((sid, pend[j:j + batch]))
    return out


def totals_by_cat(steps):
    out = {}
    for x in steps:
        t = out.setdefault(x["cat"], {"cw": 0.0, "usd": 0.0, "steps": 0})
        t["cw"] += x["cw"]
        t["usd"] += x["usd"]
        t["steps"] += 1
    return {k: {"cw": round(v["cw"], 1), "usd": round(v["usd"], 4), "steps": v["steps"]}
            for k, v in sorted(out.items(), key=lambda kv: -kv[1]["cw"])}


def step_row(x, group):
    row = {k: x.get(k) for k in STEP_KEYS}
    row.update(summary=sp.summary(x), group=group, usd=round(x["usd"], 4))
    return row


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--root", default=os.path.expanduser("~/.claude/projects"))
    ap.add_argument("--out", default="intents_offline.json")
    ap.add_argument("--max-haiku-calls", type=int, default=80, help="Haiku batches (cached ones count too, so a "
                                                                       "rerun labels the same steps)")
    ap.add_argument("--batch", type=int, default=25, help="steps per Haiku call")
    ap.add_argument("--jobs", type=int, default=os.cpu_count() or 4, help="replay processes")
    ap.add_argument("--haiku-jobs", type=int, default=6, help="concurrent Haiku calls")
    a = ap.parse_args(argv)

    t0 = time.time()
    paths = main_transcripts(a.root)
    with Pool(max(1, a.jobs)) as pool:
        replays = [r for r in pool.imap_unordered(replay_one, paths, chunksize=1) if r]
    t_replay = time.time() - t0

    sess = {}
    for sid, goal, steps, decl in replays:
        lab = sp.Labeller()
        lab.assign(steps)
        sess[sid] = {"goal": goal, "steps": steps, "lab": lab, "decl": decl, "cw": sum(x["cw"] for x in steps),
                     "pending": lab.pending(steps)}
    need = {sid for sid, v in sess.items() if v["pending"]}
    plan = plan_batches(sess, a.max_haiku_calls, a.batch)

    def run(item):
        sid, batch = item
        return sid, batch, stepnames.label_batch(batch, sess[sid]["goal"], timeout=240, batch=a.batch)

    with ThreadPoolExecutor(max(1, a.haiku_jobs)) as ex:
        results = list(ex.map(run, plan))
    for sid, batch, res in results:
        lab = sess[sid]["lab"]
        lab.add_model_labels(res.get("labels"), res.get("groups"), res.get("usage"))
        if not res.get("labels"):
            lab.give_up([x["i"] for x in batch])

    out, src_steps, src_groups = {}, Counter(), Counter()
    full = part = 0
    for sid, v in sess.items():
        gs = v["lab"].finalize(v["steps"])
        gi = {i: n for n, g in enumerate(gs) for i in range(g["first"], g["last"] + 1)}
        src_steps.update(x["source"] for x in v["steps"])
        src_groups.update(g["source"] for g in gs)
        if sid in need:
            got = sum(1 for x in v["pending"] if x["source"] == "haiku")
            full += got == len(v["pending"])
            part += 0 < got < len(v["pending"])
        out[sid] = {"goal": v["goal"], "declared_calls": v["decl"],
                    "steps": [step_row(x, gi.get(x["i"])) for x in v["steps"]],
                    "groups": [{k: g[k] for k in ("label", "source", "first", "last", "cat", "cw", "usd", "steps",
                                                  "summary")} for g in gs],
                    "totals_by_cat": totals_by_cat(v["steps"])}
    with open(a.out, "w", encoding="utf-8") as f:
        json.dump(out, f, separators=(",", ":"))

    calls = sum(1 for _, _, r in results if not r["usage"].get("cached"))
    cached = sum(1 for _, _, r in results if r["usage"].get("cached"))
    st = Counter()
    for v in sess.values():
        st.update(v["lab"].stats)
    n_steps = sum(len(v["steps"]) for v in sess.values())
    print("transcripts: %d, sessions with steps: %d (replay %.0fs on %d procs, total %.0fs)" % (
        len(paths), len(sess), t_replay, a.jobs, time.time() - t0))
    print("steps: %d, by label source: %s" % (n_steps, dict(src_steps.most_common())))
    print("groups: %d, by source: %s" % (sum(src_groups.values()), dict(src_groups.most_common())))
    print("haiku batches: %d of budget %d (batch %d): %d calls, %d cache hits, %d failed; tokens %d, "
          "API-equivalent $%.4f" % (len(plan), a.max_haiku_calls, a.batch, calls, cached,
                                    sum(1 for _, _, r in results if not r.get("labels")), st["haiku_tokens"],
                                    st["haiku_usd"]))
    print("sessions needing a model label: %d; fully model-labelled: %d, partly: %d, none: %d" % (
        len(need), full, part, len(need) - full - part))
    print("CW covered by sessions fully model-labelled or signal-only: %.0f%% of %.1fM" % (
        100.0 * sum(v["cw"] for sid, v in sess.items() if sid not in need or all(
            x["source"] == "haiku" for x in v["pending"])) / max(1.0, sum(v["cw"] for v in sess.values())),
        sum(v["cw"] for v in sess.values()) / 1e6))
    print("wrote %s" % a.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
