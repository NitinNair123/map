"""The one street model on made-up data with a known pattern: it trains, beats the baselines on locked blocks, says
"unknown" when unsure, and the gate promotes, refuses and rolls back. Run: python -m unittest ml.test_unified"""
import os
import pickle
import sys
import tempfile
import unittest
import unittest.mock

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import train  # noqa: E402
import unified  # noqa: E402
from features import STATIC  # noqa: E402


def make_blocks(folder, blocks=40, streets=60, seed=0, traffic=True):
    """Each street has a hidden 'busy' level that drives lighting, buses, open places and traffic, so the parts can
    learn from each other. Only a third of streets carry a lit label, and busy streets are labelled more often."""
    rng = np.random.default_rng(seed)
    for b in range(blocks):
        tile = f"{3 * b}_{3 * (b % 7)}"
        busy = rng.random(streets)
        e = pd.DataFrame({c: rng.random(streets) for c in STATIC if c not in ("highway", "sidewalk")})
        e["highway"] = np.where(busy > 0.6, "primary", np.where(busy > 0.3, "residential", "service"))
        e["sidewalk"] = rng.choice(["yes", "no"], streets)
        e["lanes"] = np.round(1 + 3 * busy + rng.normal(0, 0.3, streets))
        e["gtfs_stops"] = np.round(4 * busy * rng.random(streets))
        e.insert(0, "edge_id", [f"{tile}-{i}" for i in range(streets)])
        e["tile"], e["lat"], e["lon"], e["region"], e["sources"] = tile, 28.5, 77.2, "ncr", "osm"
        lit = (busy + rng.normal(0, 0.25, streets) > 0.45).astype(float)
        e["lit"] = np.where(rng.random(streets) < 0.2 + 0.5 * busy, lit, np.nan)
        e["lit_source"] = "osm"
        rows = []
        for i in range(streets):
            for hour in range(0, 24, 3):
                day = 1.0 if 6 <= hour < 18 else 0.0
                wave = 0.3 + 0.7 * np.sin(np.pi * hour / 24) ** 2
                rows.append({"edge_id": e["edge_id"][i], "hour": hour, "weekend": 0, "daylight": day, "gtfs_deps": e["gtfs_stops"][i] * wave,
                             "open_share": float(np.clip(busy[i] * wave + rng.normal(0, 0.1), 0, 1)), "open_known": 1,
                             "bus_per_hour": max(0.0, 30 * busy[i] * wave + rng.normal(0, 2)),
                             "traffic_ratio": float(np.clip(1 - 0.5 * busy[i] * wave + rng.normal(0, 0.05), 0, 1)) if traffic else np.nan})
        e.to_csv(os.path.join(folder, f"{b}.edges.csv.gz"), index=False)
        pd.DataFrame(rows).to_csv(os.path.join(folder, f"{b}.slots.csv.gz"), index=False)


class UnifiedTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        cls.data, cls.root = os.path.join(cls.tmp, "blocks"), os.path.join(cls.tmp, "unified")
        os.makedirs(cls.data)
        make_blocks(cls.data)
        cls.patches = [unittest.mock.patch.object(unified, "ROOT", cls.root), unittest.mock.patch.object(unified, "DATA", cls.data)]
        for p in cls.patches:
            p.start()
        cls.run_name = unified.run_training(trials=1, budget=60, seeds=1)
        cls.folder = os.path.join(cls.root, "candidates", cls.run_name)
        cls.metrics = unified.read_metrics(cls.folder)["heads"]

    @classmethod
    def tearDownClass(cls):
        for p in cls.patches:
            p.stop()

    def test_1_every_part_trains_and_beats_its_baseline_on_locked_blocks(self):
        self.assertEqual(set(self.metrics), set(unified.HEADS))
        for head, m in self.metrics.items():
            self.assertIsInstance(m["locked_test"], dict, head)
        lit = self.metrics["lit"]["locked_test"]
        self.assertLess(lit["error_rate"], lit["always_guess_majority_error"])
        self.assertIn("dark_called_sure_lit", lit)
        self.assertIn("error_rate_on_typical_streets (re-weighted estimate)", lit)
        for head in unified.HOURLY:
            t = self.metrics[head]["locked_test"]
            self.assertGreater(t["mae_gain_over_baseline_ci95"][0], 0, head)
            self.assertTrue(0.6 <= t["range_holds_truth (aim 0.8)"] <= 0.95, (head, t))

    def test_2_no_locked_block_was_used_for_beliefs_or_training_scores(self):
        e, s = train.load(self.data)
        fold = unified.fold_of(e["block"])
        self.assertTrue((fold[train.locked(e["block"])] == -1).all())
        self.assertTrue((fold[~train.locked(e["block"])] >= 0).all())
        d = unified.rows_for("lit", e, s)
        s1 = unified.stage1({"lit": d}, {})
        # the belief for a locked street comes from the model fitted without any locked block
        believe = unified.crossfit(s1)
        got = believe("lit", d)
        lock = (d["fold"] == -1).to_numpy()
        want = train.predict("clf", s1["lit"]["outside"], d.loc[lock, s1["lit"]["cols"]])
        np.testing.assert_allclose(got[lock], want)

    def test_3_model_file_predicts_and_can_say_unknown(self):
        model = pickle.load(open(os.path.join(self.folder, "street_model.pkl"), "rb"))
        self.assertEqual(type(model).__module__, "unified")
        e, s = train.load(self.data)
        streets, hours = model.predict(e.head(200), s[s["edge_id"].isin(e["edge_id"].head(200))])
        self.assertEqual(len(streets), 200)
        self.assertTrue(set(streets["lit"]) <= {"lit", "dark", "unknown"})
        self.assertTrue(streets["p_lit"].between(0, 1).all())
        for head in unified.HOURLY:
            self.assertTrue((hours[f"{head}_lo"] <= hours[head]).all() and (hours[head] <= hours[f"{head}_hi"]).all())
        self.assertEqual(len(hours), len(s[s["edge_id"].isin(e["edge_id"].head(200))]))
        only_streets, none = model.predict(e.head(5))  # no hour rows: lighting still answers
        self.assertEqual(len(only_streets), 5)
        self.assertIsNone(none)

    def test_4_gate_promotes_refuses_and_rolls_back(self):
        verdict = unified.promote(self.run_name)
        passed = {h for h, (ok, _) in verdict.items() if ok}
        self.assertTrue(set(unified.HOURLY) <= passed, verdict)
        cur = unified.current_dir()
        before = open(os.path.join(cur, "street_model.pkl"), "rb").read()
        # the same data again: retrain has nothing to do
        with unittest.mock.patch.object(unified, "run_training", side_effect=AssertionError("must not train")):
            unified.main(["retrain"])
        # a candidate whose bus answers are scrambled is worse than the model in use on the same locked rows: refused
        bad = os.path.join(self.root, "candidates", "bad")
        os.makedirs(os.path.join(bad, "locked"))
        for name in ("street_model.pkl", "metrics.json"):
            open(os.path.join(bad, name), "wb").write(open(os.path.join(self.folder, name), "rb").read())
        for head in unified.HEADS:
            t = unified.locked_table(self.folder, head)
            if head == "bus_per_hour":
                t["p"] = np.random.default_rng(0).permutation(t["p"].to_numpy())
            t.to_csv(os.path.join(bad, "locked", f"{head}.csv.gz"), index=False)
        ok, why = unified.check("bad")["bus_per_hour"]
        self.assertFalse(ok)
        self.assertIn("worse than the model in use", " ".join(why))
        # a part that fails its own checks is refused whatever the model in use is
        m = {"head": "lit", "locked_test": {"gain_over_road_type_ci95": [-0.01, 0.03], "dark_called_sure_lit": 0.3}}
        ok, why = unified.gate_head("lit", m)
        self.assertFalse(ok)
        self.assertEqual(len(why), 2)
        self.assertEqual(unified.gate_head("lit", {"head": "lit", "skipped": "no rows"})[0], False)
        # promote again, then roll back: the first promoted file is back byte for byte
        unified.promote(self.run_name)
        unified.rollback()
        self.assertEqual(open(os.path.join(cur, "street_model.pkl"), "rb").read(), before)

    def test_5_a_part_with_no_data_is_skipped_not_guessed(self):
        folder = os.path.join(self.tmp, "blocks2")
        os.makedirs(folder)
        make_blocks(folder, blocks=24, streets=30, traffic=False)
        with unittest.mock.patch.object(unified, "ROOT", os.path.join(self.tmp, "u2")):
            run = unified.run_training(trials=0, budget=30, seeds=1, folder=folder)
            m = unified.read_metrics(os.path.join(self.tmp, "u2", "candidates", run))["heads"]
            model = pickle.load(open(os.path.join(self.tmp, "u2", "candidates", run, "street_model.pkl"), "rb"))
        self.assertIn("skipped", m["traffic_ratio"])
        self.assertNotIn("traffic_ratio", model.heads)


if __name__ == "__main__":
    unittest.main()
