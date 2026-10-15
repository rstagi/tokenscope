"""Smoke test for the live web UI (node-free): the session and board views in static/live.js read only payload
fields the server produces, and the pages carry the element ids live.js looks up."""
import os
import re
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

STATIC = os.path.join(ROOT, "tokenscope", "live", "static")
PRODUCERS = [os.path.join(ROOT, "tokenscope", *p) for p in
             (("live", "state.py"), ("live", "recap.py"), ("xindex.py",), ("steps.py",), ("intents.py",), ("top.py",))]

# Payload objects in the session / board code, by the variable names live.js gives them
PAYLOAD_VARS = ("C", "rc", "it", "cur", "o", "w", "t", "a", "x", "s", "g", "h", "c", "sl", "m")
# DOM / JS names those variables also take (an element called `h`, a string `s`, ...)
NOT_FIELDS = {"appendChild", "style", "className", "classList", "textContent", "title", "onclick", "innerHTML",
              "lastChild", "firstChild", "children", "length", "slice", "map", "filter", "forEach", "indexOf",
              "join", "push", "replace", "split", "concat", "sort", "some", "toFixed", "set", "node", "head",
              "lastElementChild", "_g", "_al", "_cw", "_k", "_st", "_s", "_ic", "_cw", "_usd", "_now", "up", "down", "board", "step",
              "parentNode", "getBoundingClientRect", "remove", "display", "flexGrow", "background", "width",
              "animation", "href", "borderLeftColor", "boxShadow", "paddingLeft", "color", "data", "test", "matches"}


def session_and_board_code():
    with open(os.path.join(STATIC, "live.js"), encoding="utf-8") as f:
        js = f.read()
    board = js[js.index("// ---------- board ----------"):js.index("// ---------- session ----------")]
    session = js[js.index("// ---------- session ----------"):js.index("window.TSLive")]
    return js, board + session


class TestLiveStatic(unittest.TestCase):
    def test_fields_exist_in_the_server_payloads(self):
        _, code = session_and_board_code()
        src = ""
        for p in PRODUCERS:
            with open(p, encoding="utf-8") as f:
                src += f.read()
        used = set()
        for v in PAYLOAD_VARS:
            used |= set(re.findall(r"(?<![\w.$])%s\.([a-z_][a-z0-9_]*)\b" % re.escape(v), code))
        used -= NOT_FIELDS
        missing = sorted(f for f in used if not re.search(r"""["']%s["']|\b%s=""" % (f, f), src))
        self.assertEqual(missing, [], "live.js reads fields no server payload has")
        # the fields this view is about are among them
        for f in ("intents", "recap", "asked", "now", "tests", "files_edited", "skills", "ops", "dur_s", "hints",
                  "evidence", "stub_path", "kind", "costliest", "slowest", "pct", "passed_trend"):
            self.assertIn(f, used)

    def test_declare_intent_never_an_op_row(self):
        _, code = session_and_board_code()
        self.assertIn("!isDeclare(o.tool)", code)

    def test_pages_have_the_ids_live_js_looks_up(self):
        js, _ = session_and_board_code()
        ids = set(re.findall(r"""\$\("#([\w-]+)", root\)""", js))
        with open(os.path.join(STATIC, "session.html"), encoding="utf-8") as f:
            session = f.read()
        with open(os.path.join(STATIC, "board.html"), encoding="utf-8") as f:
            board = f.read()
        with open(os.path.join(STATIC, "demo.html"), encoding="utf-8") as f:
            demo = f.read()
        with open(os.path.join(STATIC, "follow.html"), encoding="utf-8") as f:
            follow = f.read()
        # the card-list session view in live.js now runs only on the demo page; /s/<id> is the canvas (canvas.js)
        have = set(re.findall(r'id="([\w-]+)"', demo + board + follow))
        self.assertEqual(sorted(ids - have), [])
        self.assertNotIn("fmap", session)   # the phase flow map is gone: the intents replace it

    def test_session_page_is_the_vendored_canvas(self):
        with open(os.path.join(STATIC, "session.html"), encoding="utf-8") as f:
            page = f.read()
        self.assertIn('src="/static/canvas.js"', page)
        self.assertIn('href="/static/canvas.css"', page)
        self.assertIn('id="canvas"', page)
        for name in ("canvas.js", "canvas.css", "canvas.js.LEGAL.txt"):
            self.assertTrue(os.path.getsize(os.path.join(STATIC, name)) > 0, name)
        for name in os.listdir(STATIC):   # no CDN at runtime: nothing loads from another origin
            with open(os.path.join(STATIC, name), encoding="utf-8") as f:
                text = f.read()
            self.assertIsNone(re.search(r"""(src|href)=["']?(https?:)?//""", text), name)
        with open(os.path.join(ROOT, "THIRD_PARTY_NOTICES.md"), encoding="utf-8") as f:
            notices = f.read()
        for lib in ("@xyflow/react", "@xyflow/system", "react-dom", "zustand", "d3-zoom"):
            self.assertIn(lib, notices)

    def test_canvas_reads_fields_the_flow_payload_has(self):
        with open(os.path.join(ROOT, "web", "canvas", "src", "main.jsx"), encoding="utf-8") as f:
            code = f.read()
        src = ""
        for p in PRODUCERS + [os.path.join(ROOT, "tokenscope", "live", "replay.py")]:
            with open(p, encoding="utf-8") as f:
                src += f.read()
        used = set()
        for v in ("C", "H", "T", "a", "x", "g", "s", "o", "rc", "h", "t", "w", "c", "sl", "m", "f", "L"):
            used |= set(re.findall(r"(?<![\w.$])%s\.([a-z_][a-z0-9_]*)\b" % re.escape(v), code))
        own = {"slots", "steps", "stepSlot", "group", "groupIndex", "first", "lane", "key", "ts0", "agent", "id",
               "current", "type", "position", "data", "length", "map", "filter", "slice", "join", "indexOf", "push",
               "find", "some", "forEach", "sort", "replace", "split", "concat", "toFixed", "kind", "t", "n", "a", "sl",
               "x", "y", "zoom"}   # viewport fields (onMoveEnd)
        missing = sorted(f for f in used - own - NOT_FIELDS
                         if not re.search(r"""["']%s["']|\b%s=""" % (f, f), src))
        self.assertEqual(missing, [], "canvas reads fields no server payload has")
        for f in ("flow", "launch", "launch_ts", "header", "intents", "recap", "ops", "replay", "now_ts", "last_ts"):
            self.assertIn(f, used)

    def test_no_machine_paths_in_static_files(self):
        for name in os.listdir(STATIC):
            with open(os.path.join(STATIC, name), encoding="utf-8") as f:
                text = f.read()
            self.assertIsNone(re.search(r"/(home|Users)/[a-z]\w+/|/workspace/|/tmp/", text), name)


if __name__ == "__main__":
    unittest.main()
