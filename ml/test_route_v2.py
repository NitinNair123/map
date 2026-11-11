"""Checks for Claude's labeller and the route model v2.  Run: python -m unittest ml.test_route_v2"""
import os
import random
import sys
import unittest

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import label_claude  # noqa: E402
import pairs  # noqa: E402
import rank_v2  # noqa: E402


def route(**kw):
    f = {k: 0 for k in pairs.FEATURES} | {"length_m": 2000, "police_m": 1000, "no_light_data_pct": 100}
    return f | kw


class ClaudeLabels(unittest.TestCase):
    def test_lit_busy_route_wins_at_night(self):
        p = {"daylight": 0.0, "A": route(lit_pct=90, no_light_data_pct=10, open_now=5, main_pct=80), "B": route(isolated_m=900)}
        self.assertEqual(label_claude.judge(p)[0], "A")

    def test_swap_flips_and_identical_is_same(self):
        p = {"daylight": 1.0, "A": route(cctv=4), "B": route(isolated_m=600)}
        q = {"daylight": 1.0, "A": p["B"], "B": p["A"]}
        self.assertEqual({label_claude.judge(p)[0], label_claude.judge(q)[0]}, {"A", "B"})
        self.assertEqual(label_claude.judge({"daylight": 0.0, "A": route(), "B": route()})[0], "same")


class RouteModelV2(unittest.TestCase):
    def test_signs_hold_even_on_contrary_labels(self):
        rng = random.Random(0)
        rows = [{"daylight": rng.random(), "A": route(lit_pct=rng.randint(0, 100), open_now=rng.randint(0, 9)),
                 "B": route(lit_pct=rng.randint(0, 100), open_now=rng.randint(0, 9))} for _ in range(300)]
        X = np.array([rank_v2.vec(p) for p in rows]); dark = np.array([p["daylight"] < .5 for p in rows])
        y = np.array([p["A"]["lit_pct"] < p["B"]["lit_pct"] for p in rows], float)  # labels say darker is safer
        w = rank_v2.fit(X, dark, y, np.ones(len(y)))
        n = len(rank_v2.KEYS)
        for j, s in enumerate(rank_v2.SIGN):
            self.assertGreaterEqual(s * w[j], -1e-9)
            self.assertGreaterEqual(s * w[n + j], -1e-9)

    def test_score_is_antisymmetric_and_bounded(self):
        k = rank_v2.KEYS
        sv = {"features": k, "day": {x: (4.0 if s > 0 else -4.0) for x, s in zip(k, rank_v2.SIGN)}, "night": {x: 0.0 for x in k}}
        a, b = route(lit_pct=100, police_m=50000, length_m=99999), route()
        self.assertAlmostEqual(rank_v2.score(sv, a, b, 1.0), -rank_v2.score(sv, b, a, 1.0))
        self.assertLessEqual(abs(rank_v2.score(sv, a, b, 1.0)), 4.0 * len(k))  # capped measures: no single input runs away


if __name__ == "__main__":
    unittest.main()
