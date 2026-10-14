"""tokenscope live server: static pages, JSON API and Server-Sent Events over a background tailer.

Endpoints (see docs/live-spec.md):
  GET /                          static/board.html
  GET /s/<session_id>            static/session.html
  GET /follow                    static/follow.html: up to 4 pinned sessions side by side (?s=<id>,<id>,...)
  GET /static/<file>             files from tokenscope/live/static/
  GET /api/board                 all sessions in a stable order (state.board_order: by start, ended ones last;
                                 idle > 6 h dropped)
  GET /api/follow?s=<id>,...     the follow view's tiles (state.follow): up to 4 sessions' latest steps with $,
                                 totals and status in one request; subagent branches, and while one runs "folds"
                                 of finished main steps (state.follow_branches / follow_folds)
  GET /api/session/<id>          full session model (+ "seq"); ?since=<seq> -> only new / updated steps
  GET /api/session/<id>/alerts   the session's alerts (tokenscope/alerts.py) with "active", plus the config in use
  GET /api/session/<id>/compact  small payload for a Claude Code mod (and the session page: ?ops=all -> every
                                 intent step with its calls)
  GET /api/session/<id>/flow     the session canvas payload: compact + every call + header + subagent sub-flows;
                                 ?replay=1[&restart=1][&duration=<s>]: the same payload from a replay of the
                                 session's transcripts in a fresh state, advancing with wall time (replay.LiveReplay)
  GET /api/top                   top spenders of a window: ?window=5h|24h|7d|30d (day, week, month)
                                 [&by=intent|session|goal|turn] (intent: sessions, intents and steps; else just the
                                 sessions, goals or turns) [&now=<ISO>][&limit=N], from the cross-session index
                                 (tokenscope/top.py); while the index builds in the background: {"pending": true}
  GET /events                    SSE: `board` (1 s, when changed), `step` per new step, heartbeat every 15 s
"""
import argparse
import functools
import json
import mimetypes
import os
import queue
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from .. import alerts as alertsmod
from ..parse import resolve_root
from . import goalname
from .goalname import HaikuNamer, mask
from .hookevents import HookEvents
from .state import LiveState
from .tail import Tailer
from ..stepnames import StepNamer, label_batch

STATIC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
POLL_S = 0.5
HEARTBEAT_S = 15
TOP_REFRESH_S = 60   # /api/top rebuilds the cross-session index (incrementally) at most this often


def _no_model(text):
    return None


class Live:
    """Tailer + hook events + state behind one lock, plus SSE subscriber queues."""

    def __init__(self, root, redact=True, since_minutes=120, haiku=False,
                 events_path=None, hooks=True, project="*", xhints=False, index_root=None, skills_dirs=(),
                 cached_labels=False, odd_goals=False, cached_goals=False, goal_namer=None, alerts_config=None):
        self.lock = threading.Lock()
        # goal names from Haiku (disk-cached by prompt): every goal with --haiku; with --haiku-odd (odd_goals) only
        # those whose prompt reads oddly as a label (goalname.reads_oddly), goalname.goal_label until it arrives;
        # else (cached_goals) no model call: an odd prompt only gets the name an earlier run cached for it
        self.namer = None
        if haiku or odd_goals or cached_goals:
            namer = goal_namer or goalname.haiku_name
            self.namer = HaikuNamer(self._named, namer=namer if haiku or odd_goals else _no_model,
                                    only_odd=not haiku, cache_path=goalname.cache_path())
        # step labels (docs/intents-spec.md): the model with --haiku; else (cached_labels) only the labels another
        # server's model calls left in the step-label cache, so this page shows what the live pane showed
        self.step_namer = None
        if haiku:
            self.step_namer = StepNamer(self._steps_named)
        elif cached_labels:
            self.step_namer = StepNamer(self._steps_named, labeller=functools.partial(label_batch, cache_only=True))
        self.state = LiveState(redact=redact, on_goal=self.namer.request if self.namer else None,
                               on_prompt=self._name_prompt if self.namer else None,
                               model_labels=self.step_namer is not None, alerts_config=alerts_config)
        self.suggester = None
        if xhints:   # cross-session recap hints (tokenscope/xindex.py): the index builds in the background
            from ..xindex import Suggester
            self.suggester = Suggester(index_root or os.environ.get("TOKENSCOPE_INDEX_ROOT") or root, skills_dirs)
            self.state.xhints = self.suggester
        self.tailer = Tailer(root, since_minutes=since_minutes, project=project)
        self.hooks = HookEvents(events_path) if hooks else None
        self.subscribers = []
        self.stop = threading.Event()
        self.replays = {}   # session id -> LiveReplay (the session page's ?replay=1)
        self.replay_lock = threading.Lock()
        self.top_root = index_root or os.environ.get("TOKENSCOPE_INDEX_ROOT") or root
        self.top_index = None    # xindex.XIndex for /api/top, built in the background
        self.top_built = 0.0     # when its last build finished
        self.top_building = False
        self.top_lock = threading.Lock()

    def _name_prompt(self, sid, n, text):
        self.namer.request((sid, n), text)   # cached per (session, prompt n); keyword name until it arrives

    def _named(self, key, name):
        with self.lock:
            if isinstance(key, tuple):
                self.state.set_prompt_name(key[0], key[1], name, "haiku")
            else:
                self.state.set_goal(key, name, "haiku")

    def _steps_named(self, sid, idx, res):
        with self.lock:
            self.state.add_step_labels(sid, idx, res)

    def _label_steps(self):
        """Hand closed unlabelled steps to the background model labeller (only with --haiku); without it closed
        steps keep their rule label (locked)."""
        if self.step_namer is None:
            return
        for sid, batch, goal in self.state.label_requests():
            if not self.step_namer.request(sid, batch, goal):
                self.state.sessions[sid].labeller.release([x["i"] for x in batch])

    def poll_once(self):
        """One tail step: ingest new transcript lines and hook events, fan new steps out to subscribers."""
        got = self.tailer.poll()
        evs = self.hooks.poll() if self.hooks else []
        with self.lock:
            for tf, recs, reset in got:
                self.state.ingest(tf, recs, reset)
            for ev in evs:
                self.state.apply_hook(ev)
            self._label_steps()
            new = self.state.drain()
            msgs = [("step", {"session": sid, "step": self.state.public_step(st)}) for sid, st in new]
        if msgs:
            for q in list(self.subscribers):
                for m in msgs:
                    q.put(m)

    def run(self):
        if self.suggester is not None:
            threading.Thread(target=self.suggester.run, args=(self.stop,), daemon=True).start()
        while not self.stop.is_set():
            try:
                self.poll_once()
            except Exception as e:  # noqa: BLE001 - keep tailing whatever one bad file does
                print("tokenscope live: poll error: %r" % (e,), file=sys.stderr)
            self.stop.wait(POLL_S)

    def replay_flow(self, path, sid, restart=False, duration=90.0):
        """The /flow payload of a replay of the session transcript at `path` (one replay per session, made on the
        first request or with restart); cross-session hints from this server's index, cached step labels only."""
        from .replay import LiveReplay
        with self.replay_lock:
            rp = self.replays.get(sid)
            if rp is None or restart:
                rp = self.replays[sid] = LiveReplay(
                    path, redact=self.state.redact, duration=duration, xhints=self.suggester,
                    labeller=functools.partial(label_batch, cache_only=True), alerts_config=self.state.alerts_config)
            return rp.flow()

    def _build_top(self):
        from ..xindex import XIndex
        try:
            x = XIndex(self.top_root)
            x.build()
        except Exception as e:  # noqa: BLE001 - the board works without the panel
            print("tokenscope live: top spenders index: %r" % (e,), file=sys.stderr)
            x = None
        with self.top_lock:
            self.top_index = x or self.top_index
            self.top_built = time.time()
            self.top_building = False

    def top(self, window, now=None, limit=10, by="intent"):
        """tokenscope.top payload of `window` ending at `now` grouped `by`, or {"pending": true} until the index is
        first built; a stale index (> TOP_REFRESH_S) is rebuilt in the background while the old one answers."""
        from ..top import top_spenders
        with self.top_lock:
            if time.time() - self.top_built > TOP_REFRESH_S and not self.top_building:
                self.top_building = True
                threading.Thread(target=self._build_top, daemon=True).start()
            x = self.top_index
        if x is None:
            return {"pending": True, "window": window, "by": by}
        return top_spenders(window, now=now, limit=limit, index=x, by=by)

    def subscribe(self):
        q = queue.Queue()
        self.subscribers.append(q)
        return q

    def unsubscribe(self, q):
        if q in self.subscribers:
            self.subscribers.remove(q)


class Handler(BaseHTTPRequestHandler):
    live = None   # set on the subclass made by make_server

    def log_message(self, fmt, *args):  # quiet: never log request paths (they carry session ids)
        pass

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        data = body if isinstance(body, bytes) else body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj, separators=(",", ":")))

    def _static(self, name):
        path = os.path.realpath(os.path.join(STATIC, name))
        if not path.startswith(os.path.realpath(STATIC) + os.sep) or not os.path.isfile(path):
            return self._json({"error": "not found"}, 404)
        with open(path, "rb") as f:
            data = f.read()
        ctype = mimetypes.guess_type(path)[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype.endswith("javascript"):
            ctype += "; charset=utf-8"
        self._send(200, data, ctype)

    def do_GET(self):  # noqa: N802 - http.server API
        u = urlparse(self.path)
        parts = [p for p in u.path.split("/") if p]
        live, st = self.live, self.live.state
        if not parts:
            return self._static("board.html")
        if parts[0] == "s" and len(parts) == 2:
            return self._static("session.html")
        if parts == ["follow"]:
            return self._static("follow.html")
        if parts[0] == "static" and len(parts) == 2:
            return self._static(parts[1])
        if parts == ["api", "board"]:
            with live.lock:
                return self._json(st.board())
        if parts == ["api", "follow"]:
            ids = [x for v in parse_qs(u.query).get("s", []) for x in v.split(",") if x.strip()]
            with live.lock:
                return self._json(st.follow(ids))
        if parts[:2] == ["api", "session"] and len(parts) == 4 and parts[3] == "flow" and \
                parse_qs(u.query).get("replay") in (["1"], ["true"]):
            return self._replay(parts[2], parse_qs(u.query))
        if parts[:2] == ["api", "session"] and len(parts) in (3, 4):
            with live.lock:
                s = st.find(parts[2])
                if s is None:
                    return self._json({"error": "unknown session"}, 404)
                if len(parts) == 4 and parts[3] == "flow":
                    return self._json(st.session_flow(s))
                if len(parts) == 4 and parts[3] == "alerts":
                    return self._json({"alerts": st.alerts_public(s), "config": st.alerts_config})
                if len(parts) == 4:
                    if parts[3] != "compact":
                        return self._json({"error": "not found"}, 404)
                    ops = parse_qs(u.query).get("ops") == ["all"]   # every step's calls, not only the live one
                    return self._json(st.session_compact(s, all_ops=ops))
                since = parse_qs(u.query).get("since")
                if since:
                    try:
                        n = int(since[0])
                    except ValueError:
                        return self._json({"error": "bad since"}, 400)
                    return self._json(st.session_since(s, n))
                return self._json(st.session_full(s))
        if parts == ["api", "top"]:
            return self._top(parse_qs(u.query))
        if parts == ["events"]:
            return self._events()
        return self._json({"error": "not found"}, 404)

    def _replay(self, sid, q):
        with self.live.lock:
            s = self.live.state.find(sid)
            path, sid = (s.path, s.id) if s is not None else (None, None)
        if not path:
            return self._json({"error": "unknown session"}, 404)
        try:
            dur = min(1800.0, max(5.0, float((q.get("duration") or ["90"])[0])))
        except ValueError:
            dur = 90.0
        return self._json(self.live.replay_flow(path, sid, restart=q.get("restart") == ["1"], duration=dur))

    def _top(self, q):
        from ..top import parse_by, parse_now, window_seconds
        try:
            window = window_seconds((q.get("window") or ["5h"])[0])[0]
            by = parse_by((q.get("by") or ["intent"])[0])
            now = parse_now(q["now"][0]) if q.get("now") else None
            limit = min(100, max(1, int((q.get("limit") or ["10"])[0])))
        except ValueError as e:
            return self._json({"error": str(e)}, 400)
        r = self.live.top(window, now, limit, by)
        st = self.live.state
        if st.redact and not r.get("pending"):
            with self.live.lock:
                for key in ("sessions", "intents", "steps", "goals", "turns"):
                    for x in r.get(key, ()):
                        label = mask(x["label"]) if key == "turns" else x["label"]   # a turn's label is prompt text
                        x.update(label=st._lab(label), project=st._project(x["project"]),
                                 session_label=st._lab(x["session_label"]))
                        if "goal" in x:
                            x["goal"] = st._lab(x["goal"])
        r.pop("index", None)
        return self._json(r)

    def _events(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        live = self.live
        q = live.subscribe()
        last_version, last_beat, last_board = None, time.time(), 0.0
        try:
            while not live.stop.is_set():
                out = []
                now = time.time()
                if now - last_board >= 1.0:
                    last_board = now
                    with live.lock:
                        if live.state.version != last_version:
                            last_version = live.state.version
                            out.append("event: board\ndata: %s\n\n" % json.dumps(live.state.board(),
                                                                                separators=(",", ":")))
                try:
                    while True:
                        kind, payload = q.get_nowait()
                        out.append("event: %s\ndata: %s\n\n" % (kind, json.dumps(payload, separators=(",", ":"))))
                except queue.Empty:
                    pass
                if not out and now - last_beat >= HEARTBEAT_S:
                    out.append(": heartbeat\n\n")
                if out:
                    last_beat = now
                    self.wfile.write("".join(out).encode("utf-8"))
                    self.wfile.flush()
                live.stop.wait(0.25)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            live.unsubscribe(q)


def ensure_static():
    """Create a minimal placeholder board page if the real static pages are not installed yet."""
    path = os.path.join(STATIC, "board.html")
    if not os.path.exists(path):
        os.makedirs(STATIC, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(PLACEHOLDER)


PLACEHOLDER = """<!doctype html><meta charset="utf-8"><title>tokenscope live</title>
<h1>tokenscope live</h1><p>The live pages are not installed. JSON: <a href="/api/board">/api/board</a></p>
"""


def make_server(live, host="127.0.0.1", port=8765):
    handler = type("LiveHandler", (Handler,), {"live": live})
    srv = ThreadingHTTPServer((host, port), handler)
    srv.daemon_threads = True
    return srv


def add_arguments(p):
    p.add_argument("--root", default=argparse.SUPPRESS, help="transcripts root (default ~/.claude/projects or $TOKENSCOPE_ROOT)")
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--no-redact", action="store_true",
                   help="show prompts, project names, file names and descriptions (default: redacted)")
    p.add_argument("--haiku", action="store_true",
                   help="name goals with `claude -p --model haiku` (sends the masked first 240 prompt chars)")
    p.add_argument("--haiku-odd", action="store_true",
                   help="name with Haiku only the goals whose prompt reads oddly as a label (long, multi-line, "
                        "filler, paths, code; masked first 240 chars, names cached on disk); without it they get a "
                        "cleaned-up keyword name, or a name an earlier run cached")
    p.add_argument("--project", default="*", metavar="GLOB",
                   help="only watch project dirs matching this glob, e.g. '*myrepo*' (default: all)")
    p.add_argument("--since-minutes", type=int, default=120,
                   help="at startup, track transcripts modified within this many minutes (default 120)")
    p.add_argument("--index-root", default=None, metavar="DIR",
                   help="transcripts the cross-session hints compare with (default $TOKENSCOPE_INDEX_ROOT, else "
                        "the transcripts root)")
    p.add_argument("--skills-dir", action="append", default=[], metavar="DIR",
                   help="extra skills dir for 'use skill' hints, besides ~/.claude/skills and <project>/.claude/skills "
                        "(repeatable)")
    p.add_argument("--no-xhints", action="store_true", help="no cross-session recap hints (no index is built)")
    alertsmod.add_arguments(p)
    return p


def main(args=None):
    """Run the live server; `args` is an argparse namespace (from the CLI) or an argv list."""
    if args is None or isinstance(args, list):
        args = add_arguments(argparse.ArgumentParser(prog="tokenscope live")).parse_args(args)
    ensure_static()
    try:
        alerts_config = alertsmod.from_args(args)
    except (OSError, ValueError) as e:
        print("tokenscope live: %s" % (e,), file=sys.stderr)
        return 2
    live = Live(resolve_root(getattr(args, "root", None)), redact=not args.no_redact,
                since_minutes=args.since_minutes, project=args.project, haiku=args.haiku,
                xhints=not args.no_xhints, index_root=args.index_root, skills_dirs=args.skills_dir,
                cached_labels=True, odd_goals=getattr(args, "haiku_odd", False), cached_goals=True,
                alerts_config=alerts_config)
    srv = make_server(live, args.host, args.port)
    threading.Thread(target=live.run, daemon=True).start()
    print("tokenscope live: http://%s:%d/  (redaction %s; Ctrl-C to stop)" % (
        args.host, srv.server_address[1], "off" if args.no_redact else "on"), flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        live.stop.set()
        srv.server_close()
    return 0
