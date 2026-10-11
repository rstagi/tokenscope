import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tokenscope.live.statusline import fmt_tokens, line  # noqa: E402
from tokenscope.live.tail import Tailer  # noqa: E402


class TestStatusline(unittest.TestCase):
    def test_line(self):
        d = {"goal": {"name": "Fix: tax"}, "current_intent": "read_code", "cw": 1_234_000, "usd": 3.1,
             "flags": [{"flag": "reread_file", "count": 1}]}
        self.assertEqual(line(d), "◆ Fix: tax · read code · 1.2M CW · $3.10 · ⚠ reread file")
        self.assertEqual(fmt_tokens(950), "950")
        self.assertEqual(line({}), "◆ session · 0 CW")


class TestProjectFilter(unittest.TestCase):
    def test_glob(self):
        with tempfile.TemporaryDirectory() as root:
            for proj in ("-home-u-alpha", "-home-u-beta"):
                os.makedirs(os.path.join(root, proj))
                with open(os.path.join(root, proj, "s-%s.jsonl" % proj[-4:]), "w") as f:
                    f.write(json.dumps({"type": "user", "message": {"content": "hi"}}) + "\n")
            t = Tailer(root, project="*alpha*")
            self.assertEqual([os.path.basename(p) for p in t._candidates()], ["s-lpha.jsonl"])
            self.assertEqual(len(Tailer(root)._candidates()), 2)


if __name__ == "__main__":
    unittest.main()


class TestScriptWrites(unittest.TestCase):
    def test_inline_script_that_writes_is_edit(self):
        from tokenscope import intents as I
        w = "python3 - <<'EOF'\np='src/a.js'; s=open(p).read()\nopen(p,'w').write(s)\nEOF"
        self.assertEqual(I.tool_intent(I.record("Bash", {"command": w})), "edit")
        self.assertEqual(I.tool_intent(I.record("Bash", {"command": "node -e 'console.log(1)'"})), "compute")
        self.assertEqual(I.tool_intent(I.record("Bash", {"command": "node -e \"require('fs').writeFileSync('x','y')\""})), "edit")
