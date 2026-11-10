"""Checks for the AI street model pipeline. Run from the repo folder: python -m unittest ml.test_ml"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import features  # noqa: E402
from safety import Area  # noqa: E402
from test_safety import fixture  # noqa: E402


class Features(unittest.TestCase):
    def setUp(self):
        self.area = Area(fixture())
        self.rows = {r["edge_id"]: r for r in features.static_rows(self.area)}

    def test_lit_label_comes_from_the_tag_and_never_feeds_the_inputs(self):
        footway = next(r for r in self.rows.values() if r["highway"] == features.HIGHWAYS.index("footway"))
        self.assertEqual((footway["lit"], footway["lit_source"]), (0, "osm"))
        self.assertNotIn("lit", features.STATIC)
        self.assertTrue(all(r["lit"] is None for r in self.rows.values() if r["highway"] == features.HIGHWAYS.index("residential")))

    def test_inputs_count_what_is_mapped(self):
        market = [r for r in self.rows.values() if r["highway"] == features.HIGHWAYS.index("residential")]
        self.assertTrue(all(r["n_lamp"] >= 3 for r in market))
        self.assertTrue(sum(r["places_shop"] for r in market) >= 5)
        self.assertTrue(all(set(features.STATIC) <= set(r) for r in self.rows.values()))

    def test_open_share_follows_published_hours(self):
        slots = features.slot_rows(self.area)
        at = {(r["hour"], r["weekend"]): r["open_share"] for r in slots if r["open_known"] and r["edge_id"] in
              {k for k, v in self.rows.items() if v["places_shop"]}}
        self.assertEqual(at[(12, 0)], 1.0)  # shops open 08:00-23:00
        self.assertEqual(at[(3, 0)], 0.0)
        self.assertTrue(all("open" not in c for c in features.STATIC + features.TIME))

    def test_bus_rate_averages_watched_hours_only(self):
        bus = {"counts": {(28600, 77201, 0, 22): 6}, "watched": {(0, 22): 2}}
        self.assertEqual(features.bus_rate(bus, (28.6005, 77.2015), 0, 22), 3.0)
        self.assertIsNone(features.bus_rate(bus, (28.6005, 77.2015), 0, 9))  # never watched: unknown, not zero
        self.assertIsNone(features.bus_rate(bus, (51.5, -0.12), 0, 22))  # London: the feed doesn't cover it, so no count is not zero


class Training(unittest.TestCase):
    def setUp(self):
        try:
            import numpy as np
            import pandas as pd
            import train
        except ImportError:
            self.skipTest("pip install -r ml/requirements.txt")
        self.np, self.pd, self.train = np, pd, train
        rng = np.random.default_rng(0)
        n = 4000
        e = pd.DataFrame({c: rng.random(n) for c in features.STATIC})
        e["highway"], e["sidewalk"] = rng.integers(0, 5, n), rng.integers(0, 3, n)
        e["edge_id"], e["tile"] = [str(i) for i in range(n)], [f"{i % 40}_0" for i in range(n)]
        e["block"] = e["tile"].map(train.block_of)
        e["region"] = "ncr"
        e["lit"] = (e["n_lamp"] + 0.2 * rng.random(n) > 0.6).astype(float)  # lamps decide it, as on a real street
        e["lit_source"] = "osm"
        self.e, self.rng = e, rng

    def test_lit_head_is_scored_once_on_locked_blocks(self):
        saved, m = self.train.train_lit(self.e, trials=2, budget=60)
        t = m["locked_test"]
        self.assertGreater(t["auroc"], 0.9)
        self.assertLess(t["error_rate"], t["always_guess_majority_error"])
        locked = set(self.e["block"][self.train.locked(self.e["block"])])
        self.assertTrue(locked and locked.isdisjoint(set(self.e["block"][~self.train.locked(self.e["block"])])))
        self.assertEqual(m["chosen"], "ncr_only")
        p = saved["calibrator"].predict(saved["model"].predict_proba(self.e[features.STATIC])[:, 1])
        self.assertTrue(((p >= 0) & (p <= 1)).all())

    def test_other_regions_can_help_but_are_never_scored(self):
        np, pd = self.np, self.pd
        other = self.e.copy()
        other["region"], other["edge_id"] = "other", "o" + other["edge_id"]
        other["tile"] = [f"{900 + i % 40}_0" for i in range(len(other))]
        other["block"] = other["tile"].map(self.train.block_of)
        _, m = self.train.train_lit(pd.concat([self.e, other]), trials=2, budget=60)
        self.assertEqual(set(m["variants_cv_score_on_ncr (lower is better)"]), {"ncr_only", "ncr_plus_other_regions"})
        self.assertEqual(m["locked_test"]["rows"], int(self.train.locked(self.e["block"]).sum()))

    def test_hourly_head_beats_the_hour_average(self):
        np, pd = self.np, self.pd
        e = self.e
        s = pd.DataFrame({"edge_id": np.repeat(e["edge_id"].to_numpy(), 2), "hour": np.tile([9, 22], len(e)), "weekend": 0,
                          "daylight": 0.5, "gtfs_deps": 0.0})
        s["open_share"] = np.where(s["hour"] == 9, 1.0, 0.0) * np.repeat(e["places_shop"].to_numpy() > 0.5, 2)
        _, m = self.train.train_hourly(e, s, "open_share", trials=2, budget=60)
        self.assertTrue(m["beats_baseline"])

    def test_too_little_data_is_reported_not_guessed(self):
        _, m = self.train.train_lit(self.e.head(50), trials=1)
        self.assertIn("skipped", m)


if __name__ == "__main__":
    unittest.main()
