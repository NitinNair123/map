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
        bus = {"counts": {(28000, 77001, 0, 22): 6}, "watched": {(0, 22): 2}}
        self.assertEqual(features.bus_rate(bus, (28.0005, 77.0015), 0, 22), 3.0)
        self.assertIsNone(features.bus_rate(bus, (28.0005, 77.0015), 0, 9))  # never watched: unknown, not zero


class Training(unittest.TestCase):
    def test_trains_and_checks_on_held_out_tiles(self):
        try:
            import numpy as np
            import pandas as pd
            import train
        except ImportError:
            self.skipTest("pip install -r ml/requirements.txt")
        rng = np.random.default_rng(0)
        n = 1200
        e = pd.DataFrame({c: rng.random(n) for c in features.STATIC})
        e["highway"], e["sidewalk"] = rng.integers(0, 5, n), rng.integers(0, 3, n)
        e["edge_id"], e["tile"] = [str(i) for i in range(n)], [str(i % 20) for i in range(n)]
        e["lit"] = (e["n_lamp"] + 0.2 * rng.random(n) > 0.6).astype(float)  # lamps decide it, as on a real street
        e["lit_source"] = "osm"
        model, m = train.train_lit(e)
        self.assertGreater(m["auroc"], 0.9)
        self.assertEqual(m["tiles"], 20)
        with tempfile.TemporaryDirectory():
            self.assertIsNotNone(model.predict_proba(e[features.STATIC])[:, 1])


if __name__ == "__main__":
    unittest.main()
