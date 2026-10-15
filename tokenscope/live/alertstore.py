"""Alert history: every alert the live server saw, per session, with its acknowledgement state, kept on disk so it
survives page reloads and server restarts.

A record is a session's one alert of a kind (tokenscope.alerts.collapse: at most one cost, one off-task and one
looping alert per session), keyed by its stable id alerts.alert_id(session, kind):

    {"id", "sid", "n" (1, 2, 3 per session, in the order first seen: the pane's "#n"), "kind", "rule", "severity",
     "message", "step", "first", "agent", "reasons", "members", "count" (triggers so far), "triggers", "ts" (first
     seen, server clock), "at" (the latest trigger's step time in the transcript), "updated", "intent" ({"cat",
     "label"} of that step), "project", "goal", "state": "new" | "acked" | "dismissed", "acked_ts", "dismissed_ts",
     "raised" (times it came back), "v": 3}

A new trigger of the same kind updates the record in place: the latest trigger's rule, message, step and reasons,
"count", and a severity that only ever rises. An acknowledged or dismissed alert comes back ("new" again, "raised"
+ 1) only when its severity rises; a trigger at the same (or a lower) severity updates it and leaves it acknowledged.

Storage: JSON lines, append-only ({"op": "put", "rec": {...}} | {"op": "ack" | "dismiss", "ids": [...], "ts"} |
{"op": "drop", "ids": [...]}), replayed at load and compacted (one put per record) when mostly superseded. Records
of an older history (v2: one per kind, thread and step) are merged at load into one per session and kind (count:
how many there were; the worst severity; the newest one's message and step; "new" if any was) and the file is
rewritten. Default file: $TOKENSCOPE_ALERTS_HISTORY, else $TOKENSCOPE_CACHE_DIR/alerts-history.jsonl
(~/.cache/tokenscope/). Messages are the alerts' own privacy-safe text (alerts.scrub ran over them); labels are as
the server showed them.
"""
import json
import os
import sys
import threading
import time

from ..alerts import SEV_RANK, alert_id

STATES = ("new", "acked", "dismissed")
MAX_RECORDS = 5000   # oldest dismissed, then oldest acknowledged records go first beyond this


def default_path():
    """$TOKENSCOPE_ALERTS_HISTORY, else alerts-history.jsonl in the tokenscope cache dir."""
    if os.environ.get("TOKENSCOPE_ALERTS_HISTORY"):
        return os.path.expanduser(os.environ["TOKENSCOPE_ALERTS_HISTORY"])
    d = os.environ.get("TOKENSCOPE_CACHE_DIR") or os.path.join(os.path.expanduser("~"), ".cache", "tokenscope")
    return os.path.join(d, "alerts-history.jsonl")


FIELDS = ("kind", "rule", "message", "step", "first", "agent", "reasons", "members", "triggers")
VERSION = 3


class AlertStore:
    """Alert records in memory, mirrored to a JSONL file when `path` is set (None: memory only)."""

    def __init__(self, path=None, clock=time.time, max_records=MAX_RECORDS):
        self.path, self.clock, self.max_records = path, clock, max_records
        self.recs = {}          # id -> record
        self.by_sid = {}        # session id -> {record id}
        self.seq = {}           # sid -> last "n" given
        self.version = 0        # bumps on every change (board / SSE signatures)
        self.lock = threading.RLock()
        self._lines = 0
        self._warned = False
        if path:
            self._load()

    # ------------------------------------------------------------ disk
    def _load(self):
        try:
            f = open(self.path, encoding="utf-8")
        except OSError:
            return
        with f:
            for line in f:
                try:
                    op = json.loads(line)
                except ValueError:
                    continue   # a line cut short by a crash
                self._lines += 1
                self._apply(op)
        if self._migrate() or self._lines > 2 * len(self.recs) + 200:
            self._compact()

    def _migrate(self):
        """Merge older records (v2: one per kind, thread and step) into one per session and kind. Returns whether
        any were merged (the file is then rewritten)."""
        old = {}
        for r in self.recs.values():
            if r.get("v") != VERSION:
                old.setdefault((r["sid"], r.get("kind") or "?"), []).append(r)
        if not old:
            return False
        for sid in {k[0] for k in old}:
            self.seq.pop(sid, None)
        for (sid, kind), rs in sorted(old.items(), key=lambda kv: min(r.get("ts") or 0 for r in kv[1])):
            rid = alert_id(sid, kind)
            cur = self.recs.get(rid) if self.recs.get(rid, {}).get("v") == VERSION else None
            allr = rs + ([cur] if cur else [])
            allr.sort(key=lambda r: (r.get("step") if r.get("step") is not None else -1, r.get("ts") or 0))
            lead = allr[-1]
            states = {r.get("state", "new") for r in allr}
            m = dict(lead, id=rid, v=VERSION,
                     severity=max((r.get("severity") for r in allr), key=lambda v: SEV_RANK.get(v, 0)),
                     count=sum(r.get("count") or 1 for r in allr),
                     triggers=[t for r in allr for t in r.get("triggers") or [
                         {k: r.get(k) for k in ("id", "rule", "severity", "message", "step", "first", "agent")}]],
                     members=[i for r in allr for i in r.get("members") or [r["id"]]],
                     ts=min(r.get("ts") or 0 for r in allr), updated=max(r.get("updated") or 0 for r in allr),
                     raised=max(r.get("raised") or 0 for r in allr),
                     state="new" if "new" in states else "acked" if "acked" in states else "dismissed")
            m.pop("n", None)
            for r in rs:
                self._unindex(r["id"])
            self._index(m)
        for sid in {k[0] for k in old}:   # renumber: #1.. in the order first seen
            for n, rid in enumerate(sorted(self.by_sid.get(sid, ()), key=lambda i: (self.recs[i].get("ts") or 0,
                                                                                     i)), 1):
                self.recs[rid]["n"] = n
            self.seq[sid] = len(self.by_sid.get(sid, ()))
        self.version += 1
        return True

    def _apply(self, op):
        kind = op.get("op")
        if kind == "put" and isinstance(op.get("rec"), dict) and op["rec"].get("id"):
            r = op["rec"]
            self._index(r)
        elif kind in ("ack", "dismiss"):
            for i in op.get("ids") or ():
                r = self.recs.get(i)
                if r is not None:
                    self._set_state(r, "acked" if kind == "ack" else "dismissed", op.get("ts"))
        elif kind == "drop":
            for i in op.get("ids") or ():
                self._unindex(i)

    def _index(self, r):
        self.recs[r["id"]] = r
        self.by_sid.setdefault(r["sid"], set()).add(r["id"])
        if r.get("n"):
            self.seq[r["sid"]] = max(self.seq.get(r["sid"], 0), r["n"])

    def _unindex(self, rid):
        r = self.recs.pop(rid, None)
        if r is not None:
            self.by_sid.get(r["sid"], set()).discard(rid)

    @staticmethod
    def _set_state(r, state, ts):
        r["state"] = state
        r["acked_ts" if state == "acked" else "dismissed_ts"] = ts

    def _write(self, ops):
        if not ops:
            return
        self.version += 1
        if not self.path:
            return
        try:
            os.makedirs(os.path.dirname(os.path.abspath(self.path)), exist_ok=True)
            with open(self.path, "a", encoding="utf-8") as f:
                for op in ops:
                    f.write(json.dumps(op, separators=(",", ":"), sort_keys=True) + "\n")
            self._lines += len(ops)
            if self._lines > 4 * len(self.recs) + 1000:
                self._compact()
        except OSError as e:
            if not self._warned:
                self._warned = True
                print("tokenscope live: cannot write the alert history %s: %r" % (self.path, e), file=sys.stderr)

    def _compact(self):
        """Rewrite the file as one put per record (atomically: a temp file renamed over it)."""
        if not self.path:
            return
        self._trim()
        tmp = self.path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                for r in sorted(self.recs.values(), key=lambda r: (r.get("ts") or 0, r["id"])):
                    f.write(json.dumps({"op": "put", "rec": r}, separators=(",", ":"), sort_keys=True) + "\n")
            os.replace(tmp, self.path)
            self._lines = len(self.recs)
        except OSError:
            pass

    def _trim(self):
        extra = len(self.recs) - self.max_records
        if extra <= 0:
            return
        order = sorted(self.recs.values(), key=lambda r: (STATES.index(r.get("state", "new")) * -1, r.get("ts") or 0))
        for r in order[:extra]:
            if r.get("state") != "new":
                self._unindex(r["id"])

    # ------------------------------------------------------------ sync from the live model
    def sync(self, sid, alerts, meta=None):
        """Upsert a session's current collapsed alerts (alerts.collapse: one per kind, each may carry "at" and
        "intent"); meta: {"project", "goal"}. Returns the ids of new or re-raised records."""
        meta = meta or {}
        now = self.clock()
        ops, fresh = [], []
        with self.lock:
            for a in alerts:
                rid = a["id"]
                r = self.recs.get(rid)
                if r is None:
                    n = self.seq.get(sid, 0) + 1
                    self.seq[sid] = n
                    r = {"id": rid, "sid": sid, "n": n, "ts": now, "state": "new", "acked_ts": None,
                         "dismissed_ts": None, "raised": 0, "v": VERSION, "severity": a["severity"], "count": 0}
                    fresh.append(rid)
                    changed = True
                else:
                    changed = any(r.get(k) != a.get(k) for k in FIELDS) or (a.get("count") or 1) > (r.get("count") or 0)
                worse = SEV_RANK.get(a["severity"], 0) > SEV_RANK.get(r.get("severity"), 0)
                if worse and r.get("state") != "new":   # escalated: back as new
                    r.update(state="new", raised=(r.get("raised") or 0) + 1)
                    fresh.append(rid)
                changed = changed or worse
                if not changed and (a.get("intent") is None or r.get("intent") == a.get("intent")):
                    continue
                r.update({k: a.get(k) for k in FIELDS}, updated=now,
                         severity=a["severity"] if worse else r["severity"],
                         count=max(r.get("count") or 0, a.get("count") or 1),
                         at=a.get("at") if a.get("at") is not None else r.get("at"),
                         intent=a.get("intent") or r.get("intent"),
                         project=meta.get("project", r.get("project")), goal=meta.get("goal", r.get("goal")))
                self._index(r)
                ops.append({"op": "put", "rec": r})
            self._write(ops)
        return fresh

    # ------------------------------------------------------------ acknowledge / dismiss
    def _of(self, sids):
        """The records of `sids` (None: every session)."""
        if sids is None:
            return list(self.recs.values())
        return [self.recs[i] for sid in set(sids) for i in self.by_sid.get(sid, ())]

    def _select(self, ids=None, sids=None, states=("new",)):
        if ids is not None:
            return [self.recs[i] for i in set(ids) if i in self.recs]
        return [r for r in self._of(sids or None) if r.get("state", "new") in states]

    def ack(self, ids=None, sids=None):
        """Acknowledge alerts by id, or (ids None) every new one (of `sids`, else of every session): they stay in
        the history, no longer counted as needing attention. Returns how many changed."""
        return self._mark("ack", "acked", self._select(ids, sids, ("new",)))

    def dismiss(self, ids=None, sids=None):
        """Dismiss alerts by id, or (ids None) every new and acknowledged one: they leave the history views."""
        return self._mark("dismiss", "dismissed", self._select(ids, sids, ("new", "acked")))

    def reopen(self, ids):
        """Back to "new" (an acknowledgement undone)."""
        with self.lock:
            rs = [r for r in self._select(ids) if r.get("state") != "new"]
            for r in rs:
                r["state"] = "new"
            self._write([{"op": "put", "rec": r} for r in rs])
            return len(rs)

    def _mark(self, op, state, rs):
        with self.lock:
            now = self.clock()
            rs = [r for r in rs if r.get("state") != state]
            for r in rs:
                self._set_state(r, state, now)
            if rs:
                self._write([{"op": op, "ids": [r["id"] for r in rs], "ts": now}])
            return len(rs)

    # ------------------------------------------------------------ read
    def get(self, rid):
        return self.recs.get(rid)

    def records(self, sids=None, states=("new", "acked")):
        """Records of `sids` (else every session) in `states`, newest first (by first seen)."""
        with self.lock:
            out = [r for r in self._of(sids or None) if r.get("state", "new") in states]
        out.sort(key=lambda r: (-(r.get("ts") or 0), r.get("n") or 0))   # one sync: in alerts.run's order
        return out

    def summary(self, sid):
        """{"new", "acked", "total" (not dismissed), "by_kind": {kind: new}, "worst": worst new severity}."""
        with self.lock:
            rs = [r for r in self._of([sid]) if r.get("state") != "dismissed"]
        new = [r for r in rs if r.get("state", "new") == "new"]
        by = {}
        for r in new:
            by[r["kind"]] = by.get(r["kind"], 0) + 1
        worst = max((r["severity"] for r in new), key=lambda s: SEV_RANK.get(s, 0), default=None)
        return {"new": len(new), "acked": len(rs) - len(new), "total": len(rs), "by_kind": by, "worst": worst}

    def sessions(self):
        with self.lock:
            return sorted(sid for sid, ids in self.by_sid.items() if ids)
