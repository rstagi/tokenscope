"""Incremental transcript tailer.

Watches `<root>/*/*.jsonl` (main sessions) and `<root>/*/<session-id>/subagents/**/agent-*.jsonl` (subagents). Per
file it keeps a byte offset and a buffer for a partial trailing line, so only complete JSON lines are returned. A
file that shrinks (truncation / rotation) is re-read from 0 and reported with `reset=True`.
"""
import glob
import json
import os
import time


class TrackedFile:
    """One transcript file being tailed."""

    def __init__(self, path, session_id, project_dir, agent_id=None):
        self.path = path
        self.session_id = session_id
        self.project_dir = project_dir
        self.agent_id = agent_id
        self.offset = 0
        self.buf = b""
        self.meta = None          # subagent meta.json contents (once readable)
        self.malformed = 0

    @property
    def meta_path(self):
        return self.path[:-len(".jsonl")] + ".meta.json" if self.agent_id else None

    def load_meta(self):
        """Read the sibling agent-<id>.meta.json if present (it may appear after the transcript)."""
        if self.meta is None and self.agent_id:
            try:
                with open(self.meta_path, encoding="utf-8") as f:
                    m = json.load(f)
                self.meta = m if isinstance(m, dict) else {}
            except (OSError, ValueError):
                pass
        return self.meta


def classify(root, path):
    """(session_id, project_dir, agent_id) for a transcript path under root, or None if it is not one."""
    from ..harness import get_harness
    h = get_harness(root=root, path=path)
    res = h.classify_tail_path(root, path)
    if res is not None:
        return res
    rel = os.path.relpath(path, root).split(os.sep)
    name = rel[-1]
    if len(rel) == 2 and name.endswith(".jsonl"):
        return name[:-6], rel[0], None
    if len(rel) >= 4 and rel[2] == "subagents" and name.startswith("agent-") and name.endswith(".jsonl"):
        return rel[1], rel[0], name[len("agent-"):-6]
    return None


class Tailer:
    def __init__(self, root, since_minutes=120, clock=time.time, project="*"):
        self.root = root
        self.project = project or "*"   # glob on the project dir name (e.g. "*myrepo*")
        self.clock = clock
        self.files = {}           # path -> TrackedFile
        self.seen = {}            # path -> (mtime, size) of untracked files seen at startup
        self.malformed = 0
        self.first = True
        self.since_s = since_minutes * 60

    def _candidates(self):
        from ..harness import get_harness
        h = get_harness(root=self.root)
        cands = h.tail_candidates(self.root, self.project)
        pats = (os.path.join(self.root, self.project, "*.jsonl"),
                os.path.join(self.root, self.project, "*", "subagents", "**", "agent-*.jsonl"))
        out = list(cands)
        seen = set(out)
        for p in pats:
            for match in glob.glob(p, recursive=True):
                if match not in seen:
                    seen.add(match)
                    out.append(match)
        return out

    def _discover(self):
        now = self.clock()
        for path in self._candidates():
            if path in self.files:
                continue
            try:
                st = os.stat(path)
            except OSError:
                continue
            key = (st.st_mtime, st.st_size)
            if self.first:
                # at startup only recently modified files are tracked; older ones once they change
                if now - st.st_mtime > self.since_s:
                    self.seen[path] = key
                    continue
            elif path in self.seen and self.seen[path] == key:
                continue
            c = classify(self.root, path)
            if c:
                self.files[path] = TrackedFile(path, *c)
                self.seen.pop(path, None)
        self.first = False

    def poll(self):
        """Read everything appended since the last poll -> list of (TrackedFile, records, reset)."""
        self._discover()
        out = []
        # main transcripts first so a fan-out is known before its subagent's records arrive
        for tf in sorted(self.files.values(), key=lambda f: (f.agent_id is not None, f.path)):
            got = self._read(tf)
            if got is not None:
                out.append((tf, got[0], got[1]))
        return out

    def _read(self, tf):
        try:
            size = os.path.getsize(tf.path)
        except OSError:
            return None
        reset = False
        if size < tf.offset:
            tf.offset, tf.buf, reset = 0, b"", True
        if size == tf.offset:
            return ([], True) if reset else None
        try:
            with open(tf.path, "rb") as f:
                f.seek(tf.offset)
                data = f.read(size - tf.offset)
        except OSError:
            return None
        tf.offset += len(data)
        data = tf.buf + data
        lines = data.split(b"\n")
        tf.buf = lines.pop()      # partial trailing line (b"" when data ended with a newline)
        recs = []
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except ValueError:
                tf.malformed += 1
                self.malformed += 1
                continue
            if isinstance(r, dict):
                recs.append(r)
        if tf.agent_id:
            tf.load_meta()
        return recs, reset
