"""Tail the hook events file written by tokenscope.live.hook (optional, more precise status)."""
import json
import os

from .hook import events_path


class HookEvents:
    def __init__(self, path=None):
        self.path = path or events_path()
        self.offset = None        # None: start at the current end (old events say nothing about now)
        self.buf = b""

    def poll(self):
        """New event dicts appended since the last poll."""
        try:
            size = os.path.getsize(self.path)
        except OSError:
            return []
        if self.offset is None:
            self.offset = size
            return []
        if size < self.offset:
            self.offset, self.buf = 0, b""
        if size == self.offset:
            return []
        with open(self.path, "rb") as f:
            f.seek(self.offset)
            data = f.read(size - self.offset)
        self.offset += len(data)
        lines = (self.buf + data).split(b"\n")
        self.buf = lines.pop()
        out = []
        for line in lines:
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            if isinstance(ev, dict) and ev.get("session_id") and ev.get("event"):
                out.append(ev)
        return out
