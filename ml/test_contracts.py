"""Feature contract checks report problems instead of fixing them. Run: py -m unittest ml.test_contracts"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import contracts  # noqa: E402

GOOD = {"length_m": 900, "lit_pct": 60, "dark_pct": 10, "no_light_data_pct": 30, "main_pct": 20, "open_now": 3, "isolated_m": 100,
        "nothing_open_m": 200, "hours_unknown_m": 300, "help_gap_m": 400, "cctv": 1, "police_m": 800, "unrated_pct": 5,
        **{k: 0 for k in ("food_found", "shop_found", "pharmacy_found", "hospital_found", "fuel_found", "other_found", "transit_found")}}


class ContractTest(unittest.TestCase):
    def test_a_clean_route_has_no_problems(self):
        problems, status = contracts.check_route(GOOD)
        self.assertEqual(problems, [])
        self.assertEqual(status["nothing_open_m"], "observed")

    def test_out_of_range_and_missing_values_are_reported_not_fixed(self):
        bad = {**GOOD, "lit_pct": 140}
        del bad["main_pct"]
        problems, _ = contracts.check_route(bad)
        self.assertTrue(any("lit_pct=140" in p for p in problems))
        self.assertTrue(any("main_pct: missing" in p for p in problems))
        self.assertEqual(bad["lit_pct"], 140)  # untouched

    def test_no_police_mapped_is_unknown_not_a_problem(self):
        problems, status = contracts.check_route({**GOOD, "police_m": None})
        self.assertEqual(problems, [])
        self.assertEqual(status["police_m"], "unknown")

    def test_lighting_shares_must_add_up(self):
        problems, _ = contracts.check_route({**GOOD, "no_light_data_pct": 0})
        self.assertTrue(any("should be 100%" in p for p in problems))


if __name__ == "__main__":
    unittest.main()
