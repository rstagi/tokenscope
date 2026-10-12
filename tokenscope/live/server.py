"""tokenscope live server: static pages, JSON API and Server-Sent Events over a background tailer.

Endpoints (see docs/live-spec.md):
  GET /                          static/board.html
  GET /s/<session_id>            static/session.html
  GET /static/<file>             files from tokenscope/live/static/
  GET /api/board                 all sessions (sorted by last activity; idle > 6 h dropped)
  GET /api/session/<id>          full session model (+ "seq"); ?since=<seq> -> only new / updated steps
  GET /api/session/<id>/compact  small payload for a Claude Code mod (and the session page: ?ops=all -> every
                                 intent step with its calls)
  GET /events                    SSE: `board` (1 s, when changed), `step` per new step, heartbeat every 15 s
"""
import argparse
import json
import mimetypes
import os
import queue
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from ..parse import resolve_root
from .goalname import HaikuNamer
from .hookevents import HookEvents
from .state import LiveState
from .tail import Tailer
from ..stepnames import StepNamer

STATIC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
POLL_S = 0.5
HEARTBEAT_S = 15


class Live:
    """Tailer + hook events + state behind one lock, plus SSE subscriber queues."""

    def __init__(self, root, redact=True, since_minutes=120, haiku=False,
                 events_path=None, hooks=True, project="*", xhints=False, index_root=None, skills_dirs=()):
        self.lock = threading.Lock()
        self.namer = HaikuNamer(self._named) if haiku else None
        self.step_namer = StepNamer(self._steps_named) if haiku else None   # step labels (docs/intents-spec.md)
        self.state = LiveState(redact=redact, on_goal=self.namer.request if self.namer else None,
                               on_prompt=self._name_prompt if self.namer else None, model_labels=haiku)
        self.suggester = None
        if xhints:   # cross-session recap hints (tokenscope/xindex.py): the index builds in the background
            from ..xindex import Suggester
            self.suggester = Suggester(index_root or os.environ.get("TOKENSCOPE_INDEX_ROOT") or root, skills_dirs)
            self.state.xhints = self.suggester
        self.tailer = Tailer(root, since_minutes=since_minutes, project=project)
        self.hooks = HookEvents(events_path) if hooks else None
        self.subscribers = []
        self.stop = threading.Event()

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
        if parts[0] == "static" and len(parts) == 2:
            return self._static(parts[1])
        if parts == ["api", "board"]:
            with live.lock:
                return self._json(st.board())
        if parts[:2] == ["api", "session"] and len(parts) in (3, 4):
            with live.lock:
                s = st.find(parts[2])
                if s is None:
                    return self._json({"error": "unknown session"}, 404)
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
        if parts == ["events"]:
            return self._events()
        return self._json({"error": "not found"}, 404)

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
    return p


def main(args=None):
    """Run the live server; `args` is an argparse namespace (from the CLI) or an argv list."""
    if args is None or isinstance(args, list):
        args = add_arguments(argparse.ArgumentParser(prog="tokenscope live")).parse_args(args)
    ensure_static()
    live = Live(resolve_root(getattr(args, "root", None)), redact=not args.no_redact,
                since_minutes=args.since_minutes, project=args.project, haiku=args.haiku,
                xhints=not args.no_xhints, index_root=args.index_root, skills_dirs=args.skills_dir)
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
