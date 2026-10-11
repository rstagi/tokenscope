import io
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import synth  # noqa: E402
from tokenscope import cli  # noqa: E402

SID = "cafebabe-1234-5678-9abc-def012345678"
SID2 = "deadbeef-1234-5678-9abc-def012345678"
CWD = "/work/quokkaproj"
SENSITIVE = ["zanzibar", "invoicing", "marmalade", "quokkaproj", "secretdir", "cafebabe", "deadbeef", SID]


def make(root):
    old = synth.CWD
    synth.CWD = CWD
    try:
        for sid in (SID, SID2):
            w = synth._W(sid)
            w.prompt(0, "Refactor the zanzibar invoicing widget please")
            w.call(1, "m0", (5, 25000, 0, 50), ("Read", {"file_path": CWD + "/secretdir/marmalade_helper.py"}))
            w.result(2, "m0", "a" * 2000)
            w.call(3, "m1", (0, 600, 25005, 50), ("Bash", {"command": "grep zanzibar secretdir/marmalade_helper.py"}))
            w.result(4, "m1", "x" * 500)
            w.call(5, "m2", (0, 100, 25605, 50))
            w.write(root, slug="-work-quokkaproj")
    finally:
        synth.CWD = old
    return root


class TestRedactReport(unittest.TestCase):
    def run_cli(self, *argv):
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = cli.main(list(argv))
        self.assertEqual(rc, 0)
        return buf.getvalue()

    def test_report_redacted(self):
        with tempfile.TemporaryDirectory() as d:
            root = make(os.path.join(d, "projects"))
            out = os.path.join(d, "r.html")
            self.run_cli("report", "--root", root, "--no-cache", "-o", out)
            with open(out, encoding="utf-8") as f:
                plain = f.read().lower()
            for s in ("zanzibar", "quokkaproj", "marmalade", SID[:8]):
                self.assertIn(s, plain)   # the fixture really carries these strings
            self.run_cli("report", "--root", root, "--no-cache", "--redact", "-o", out)
            with open(out, encoding="utf-8") as f:
                doc = f.read().lower()
            for s in SENSITIVE:
                self.assertNotIn(s.lower(), doc, s)
            got = self.run_cli("goals", "--root", root, "--no-cache", "--redact", "--json").lower()
            for s in SENSITIVE:
                self.assertNotIn(s.lower(), got, s)


if __name__ == "__main__":
    unittest.main()


class TestGoalUnits(unittest.TestCase):
    def test_follow_up_and_chores(self):
        from tokenscope import goals as gl
        for t in ("yes please", "it's terrible haha maybe a little slimmer?",
                  "how's it going?"):
            self.assertTrue(gl.is_follow_up(t), t)
        self.assertFalse(gl.is_follow_up("Add pagination to the invoices table in the billing dashboard"))
        reqs = [{"id": "s%d:1" % i, "session_id": "s%d" % i, "prompt": p}
                for i, p in enumerate(["commit, push and open a PR", "commit push and open the PR",
                                       "push, commit and open a PR", "rewrite the parser"])]
        ch = gl.detect_chores(reqs)
        self.assertEqual(sorted(ch), ["s0:1", "s1:1", "s2:1"])
        threads = gl.build_threads([dict(r, session_id="x", start=str(i)) for i, r in enumerate(reqs)], ch)
        self.assertEqual([len(t) for t in threads], [1, 1, 1, 1])
