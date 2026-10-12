import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools"))

import intents_offline as io  # noqa: E402


class TestHelpers(unittest.TestCase):
    def test_plan_batches_expensive_first_within_budget(self):
        sess = {"cheap": {"cw": 10, "pending": [{"i": 0}]},
                "big": {"cw": 900, "pending": [{"i": k} for k in range(5)]},
                "none": {"cw": 5000, "pending": []}}
        plan = io.plan_batches(sess, max_calls=2, batch=2)
        self.assertEqual([(sid, [x["i"] for x in b]) for sid, b in plan], [("big", [0, 1]), ("big", [2, 3])])
        plan = io.plan_batches(sess, max_calls=10, batch=2)
        self.assertEqual([sid for sid, _ in plan], ["big", "big", "big", "cheap"])
        self.assertEqual(io.plan_batches(sess, max_calls=0, batch=2), [])

    def test_totals_by_cat(self):
        steps = [{"cat": "change", "cw": 10.0, "usd": 0.1}, {"cat": "verify", "cw": 30.0, "usd": 0.2},
                 {"cat": "change", "cw": 25.0, "usd": 0.05}]
        t = io.totals_by_cat(steps)
        self.assertEqual(list(t), ["change", "verify"])
        self.assertEqual(t["change"], {"cw": 35.0, "usd": 0.15, "steps": 2})


if __name__ == "__main__":
    unittest.main()
