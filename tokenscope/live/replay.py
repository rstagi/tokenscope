"""Replay one transcript (+ its subagents) through the live state offline (the `tokenscope intents` command)."""
import glob
import os
import time

from .state import LiveState, parse_ts
from .tail import Tailer, TrackedFile, classify


def find_transcript(arg, root):
    """A main transcript path from a path or a session id / unique prefix under root; raises LookupError."""
    if os.path.isfile(arg):
        return os.path.abspath(arg)
    hits = sorted(glob.glob(os.path.join(root, "*", glob.escape(arg) + "*.jsonl")))
    exact = [h for h in hits if os.path.basename(h) == arg + ".jsonl"]
    hits = exact or hits
    if len(hits) != 1:
        raise LookupError("%s session matches %r" % ("no" if not hits else "more than one", arg))
    return hits[0]


def replay(path, redact=False, model_labels=False):
    """(LiveState, Session) after ingesting the transcript at `path` and its subagent transcripts."""
    files, root = transcript_files(path)
    state = LiveState(redact=redact, model_labels=model_labels)
    tailer = Tailer(root)
    s = None
    for tf in files:
        got = tailer._read(tf)
        if got is not None:
            s = state.ingest(tf, got[0], got[1])
    return state, s


def transcript_files(path):
    """[TrackedFile] of a main transcript and its subagent transcripts, and the transcripts root."""
    path = os.path.abspath(path)
    root = os.path.dirname(os.path.dirname(path))
    c = classify(root, path) or (os.path.basename(path)[:-len(".jsonl")], os.path.basename(os.path.dirname(path)), None)
    sid, proj, _ = c
    files = [TrackedFile(path, sid, proj)]
    for sub in sorted(glob.glob(os.path.join(os.path.dirname(path), sid, "subagents", "**", "agent-*.jsonl"),
                                recursive=True)):
        sc = classify(root, sub)
        if sc:
            files.append(TrackedFile(sub, sc[0], sc[1], sc[2]))
    return files, root


def schedule(times, duration, max_gap):
    """Replay offsets (seconds from the start) of records at transcript times `times` (sorted): every gap is
    divided by one speed-up factor chosen so the whole replay lasts about `duration` s, and no gap exceeds
    `max_gap` replay seconds (long waits for the user shrink most)."""
    gaps = [max(0.0, b - a) for a, b in zip(times, times[1:])]

    def total(k):
        return sum(min(g / k, max_gap) for g in gaps)
    lo, hi = 1.0, 1e6
    if total(lo) <= duration:
        hi = lo
    for _ in range(60):
        if hi - lo < 1e-3 * lo:
            break
        mid = (lo + hi) / 2
        lo, hi = (lo, mid) if total(mid) <= duration else (mid, hi)
    out, t = [0.0], 0.0
    for g in gaps:
        t += min(g / hi, max_gap)
        out.append(t)
    return out, hi


class LiveReplay:
    """A recorded session played back as if it were live (the web page's ?replay=1): its transcripts' records are
    fed in time order into a fresh LiveState whose clock is the transcript time at the replay position, on a
    compressed timeline (`duration` s overall, at most `max_gap` s between two records). After the last record the
    clock keeps running (sped up), so the session goes "waiting" and the idle recap appears as it did live.
    Step labels: only cached model labels (stepnames cache) -- a replay never calls the model."""

    def __init__(self, path, redact=True, duration=90.0, max_gap=3.0, clock=time.time, xhints=None,
                 labeller=None):
        self.wall = clock
        files, self.root = transcript_files(path)
        tailer = Tailer(self.root)
        items = []
        for n, tf in enumerate(files):
            got = tailer._read(tf)
            prev = None
            for k, r in enumerate(got[0] if got else ()):
                t = parse_ts(r.get("timestamp")) if isinstance(r, dict) else None
                t = t if t is not None else prev
                prev = t
                items.append((t, n, k, tf, r))
        t0 = min((x[0] for x in items if x[0] is not None), default=0.0)
        items = [(x[0] if x[0] is not None else t0,) + x[1:] for x in items]
        items.sort(key=lambda x: (x[0], x[1], x[2]))
        self.items = items
        self.times = [x[0] for x in items]
        self.offsets, self.speed = schedule(self.times, duration, max_gap)
        self.state = LiveState(redact=redact, clock=self.now, model_labels=labeller is not None)
        self.state.xhints = xhints
        self.labeller = labeller
        self.k = 0               # records fed so far
        self.start = self.wall()
        self.sid = files[0].session_id

    def elapsed(self):
        return self.wall() - self.start

    def now(self):
        """Transcript time at the replay position (interpolated between records; sped up after the last)."""
        if not self.items:
            return self.wall()
        e, k = self.elapsed(), max(0, self.k - 1)
        if self.k >= len(self.items):
            return self.times[-1] + max(0.0, e - self.offsets[-1]) * self.speed
        a, b = self.offsets[k], self.offsets[min(k + 1, len(self.items) - 1)]
        ta, tb = self.times[k], self.times[min(k + 1, len(self.items) - 1)]
        if b <= a:
            return ta
        return ta + (tb - ta) * min(1.0, max(0.0, (e - a) / (b - a)))

    def advance(self):
        """Feed every record due by now (consecutive records of one transcript in one ingest); -> records fed."""
        e, n0 = self.elapsed(), self.k
        while self.k < len(self.items) and self.offsets[self.k] <= e:
            tf, batch = self.items[self.k][3], []
            while self.k < len(self.items) and self.offsets[self.k] <= e and self.items[self.k][3] is tf:
                batch.append(self.items[self.k][4])
                self.k += 1
            self.state.ingest(tf, batch)
        if self.labeller is not None:
            for sid, batch, goal in self.state.label_requests():
                self.state.add_step_labels(sid, [x["i"] for x in batch], self.labeller(batch, goal))
        self.state.drain()
        return self.k - n0

    def done(self):
        return self.k >= len(self.items)

    def session(self):
        return self.state.sessions.get(self.sid)

    def flow(self):
        """The /flow payload of the replayed session at the replay position (+ "replay": progress)."""
        self.advance()
        s = self.session()
        prog = {"fed": self.k, "records": len(self.items), "done": self.done(), "elapsed_s": round(self.elapsed(), 1),
                "duration_s": round(self.offsets[-1], 1) if self.offsets else 0, "speed": round(self.speed, 1)}
        if s is None:
            return {"replay": prog, "status": "idle", "intents": None, "flow": {"agents": []}, "header": None}
        d = self.state.session_flow(s)
        d["replay"] = prog
        return d
