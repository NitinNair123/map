"""Release gate: a weak candidate is rejected and the model in use stays byte-for-byte the same; a better one is promoted
and can be rolled back. Run: py -m unittest ml.test_registry"""
import json
import os
import sys
import tempfile
import unittest
import unittest.mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import registry  # noqa: E402

DATA = "same-data-hash"


def lit(auroc, ece, err, base=0.26):
    return {"head": "lit", "locked_test": {"auroc": auroc, "ece": ece, "error_rate": err, "baseline_error_rate (road type only)": base}}


def traffic(mae, beats):
    return {"head": "traffic_ratio", "beats_baseline": beats, "locked_test": {"mae": mae, "mae_gain_over_baseline_ci95": [-0.01, 0.02]}}


class GateTest(unittest.TestCase):
    def setUp(self):
        self.out = tempfile.mkdtemp()
        for name, val in (("OUT", self.out), ("CAND", os.path.join(self.out, "candidates")), ("REG", os.path.join(self.out, "registry"))):
            p = unittest.mock.patch.object(registry, name, val)
            p.start()
            self.addCleanup(p.stop)
        os.makedirs(os.path.join(self.out, "models"))
        self.write(self.out, [lit(0.74, 0.09, 0.21), traffic(0.074, False)], {"lit": b"in-use-lit", "traffic_ratio": b"in-use-traffic"})
        # the in-use settings re-scored on today's data: the reference a candidate is compared with
        self.run_dir("rescore", [lit(0.74, 0.09, 0.21), traffic(0.074, False)], trials=0)

    def write(self, folder, metrics, models):
        os.makedirs(os.path.join(folder, "models"), exist_ok=True)
        json.dump(metrics, open(os.path.join(folder, "metrics.json"), "w"))
        for h, b in models.items():
            open(os.path.join(folder, "models", f"{h}.pkl"), "wb").write(b)

    def run_dir(self, name, metrics, trials=50, data=DATA):
        d = os.path.join(self.out, "candidates", name)
        self.write(d, metrics, {m["head"]: f"{name}-{m['head']}".encode() for m in metrics})
        json.dump({"heads": [m["head"] for m in metrics], "trials": trials, "data_blocks_sha256": data}, open(os.path.join(d, "run.json"), "w"))

    def in_use(self, head):
        return open(os.path.join(self.out, "models", f"{head}.pkl"), "rb").read()

    def test_weaker_candidate_is_rejected_and_the_model_in_use_is_untouched(self):
        self.run_dir("weak", [lit(0.69, 0.10, 0.22), traffic(0.080, False)])
        entry = registry.promote("weak")
        self.assertEqual(entry["promoted"], [])
        self.assertIn("lower than the model in use", " ".join(entry["gate"]["lit"]["reasons"]))
        self.assertIn("not clear", " ".join(entry["gate"]["traffic_ratio"]["reasons"]))
        self.assertEqual(self.in_use("lit"), b"in-use-lit")
        self.assertEqual(self.in_use("traffic_ratio"), b"in-use-traffic")

    def test_better_candidate_is_promoted_only_where_it_passes_and_can_be_rolled_back(self):
        self.run_dir("better", [lit(0.80, 0.08, 0.18), traffic(0.070, False)])
        entry = registry.promote("better")
        self.assertEqual(entry["promoted"], ["lit"])  # traffic still does not beat its baseline
        self.assertEqual(self.in_use("lit"), b"better-lit")
        self.assertEqual(self.in_use("traffic_ratio"), b"in-use-traffic")
        registry.rollback()
        self.assertEqual(self.in_use("lit"), b"in-use-lit")

    def test_different_data_is_not_comparable_so_nothing_is_promoted(self):
        self.run_dir("other", [lit(0.95, 0.01, 0.05)], data="new-data-hash")
        entry = registry.promote("other")
        self.assertEqual(entry["promoted"], [])
        self.assertIn("not comparable", " ".join(entry["gate"]["lit"]["reasons"]))

    def test_candidate_must_beat_its_baseline(self):
        self.run_dir("nobase", [lit(0.80, 0.08, 0.27)])
        self.assertIn("baseline", " ".join(registry.check("nobase")[2]["lit"]["reasons"]))


if __name__ == "__main__":
    unittest.main()
