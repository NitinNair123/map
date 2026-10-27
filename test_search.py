import json
import os
import tempfile
import unittest

import server
from tiles import TileStore

HOME = (28.47, 77.49)


class Search(unittest.TestCase):
    def test_rank_puts_near_prefix_matches_first_and_drops_repeats(self):
        found = [{"name": "Delhi", "pt": [28.61, 77.21], "kind": "city"},
                 {"name": "New Delhi Cafe", "pt": [28.471, 77.49], "kind": "cafe"},
                 {"name": "Delhi Public School", "pt": [28.48, 77.49], "kind": "school"},
                 {"name": "delhi public school", "pt": [28.4801, 77.4901], "kind": "school"}]
        out = server.rank("delhi", HOME, found)
        self.assertEqual([r["name"] for r in out], ["Delhi Public School", "New Delhi Cafe", "Delhi"])
        self.assertEqual(out[0]["km"], 1.1)
        far = server.rank("hospital", HOME, [{"name": "Hospital", "pt": [0.3, 32.5], "kind": "town"},
                                             {"name": "Sharda Hospital", "pt": [28.47, 77.52], "kind": "hospital"}])
        self.assertEqual(far[0]["name"], "Sharda Hospital")

    def test_overture_places_near_user_are_found_and_one_source_down_is_survived(self):
        old = server.store, server.photon, server.GEOAPIFY_KEY
        with tempfile.TemporaryDirectory() as d:
            server.store = TileStore(d)
            with open(server.store._path((2847, 7749), "places"), "w") as f:
                json.dump({"elements": [{"lat": 28.475, "lon": 77.495, "tags": {"name": "Sharma Tea Stall", "overture": "cafe"}},
                                        {"lat": 28.476, "lon": 77.495, "tags": {"name": "Gupta Sweets", "overture": "bakery"}},
                                        {"lat": 28.477, "lon": 77.495, "tags": {"name": "SBI", "overture": "atm"}}]}, f)

            def down(q, near):
                raise OSError("no network")
            server.photon, server.GEOAPIFY_KEY = down, ""
            try:
                out = server.geocode("tea st", HOME)
                atms = server.nearby_places("atm", HOME)  # categories match too, not just names
            finally:
                server.store, server.photon, server.GEOAPIFY_KEY = old
        self.assertEqual([(r["name"], r["kind"]) for r in out], [("Sharma Tea Stall", "cafe")])
        self.assertEqual([r["name"] for r in atms], ["SBI"])
        self.assertNotIn(("tea st", (28.47, 77.49)), server.geocode_cache)  # offline answers aren't cached


if __name__ == "__main__":
    unittest.main()
