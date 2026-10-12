"""Replay one transcript (+ its subagents) through the live state offline (the `tokenscope intents` command)."""
import glob
import os

from .state import LiveState
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
    path = os.path.abspath(path)
    root = os.path.dirname(os.path.dirname(path))
    c = classify(root, path) or (os.path.basename(path)[:-len(".jsonl")], os.path.basename(os.path.dirname(path)), None)
    sid, proj, _ = c
    state = LiveState(redact=redact, model_labels=model_labels)
    files = [TrackedFile(path, sid, proj)]
    for sub in sorted(glob.glob(os.path.join(os.path.dirname(path), sid, "subagents", "**", "agent-*.jsonl"),
                                recursive=True)):
        sc = classify(root, sub)
        if sc:
            files.append(TrackedFile(sub, sc[0], sc[1], sc[2]))
    tailer = Tailer(root)
    s = None
    for tf in files:
        got = tailer._read(tf)
        if got is not None:
            s = state.ingest(tf, got[0], got[1])
    return state, s
