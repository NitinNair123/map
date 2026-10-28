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
        city = [{"name": "Mumbai", "pt": [19.07, 72.88], "kind": "Maharashtra", "big": True}]
        shops = [{"name": f"Mumbai Dhaba {i}", "pt": [28.47 + i / 1000, 77.49], "kind": "restaurant"} for i in range(9)]
        self.assertEqual(server.rank("mumbai", HOME, city + shops)[-1]["name"], "Mumbai")  # the city isn't buried

    def test_metro_finds_stations_near_the_user(self):
        old = server.photon_get
        calls = []

        def fake(path, params):
            calls.append((path, params.get("osm_tag")))
            if path == "reverse":
                return [{"name": "Knowledge Park II", "pt": [28.46, 77.49], "kind": "Greater Noida", "tag": "railway:station"},
                        {"name": "Some Shop", "pt": [28.47, 77.49], "kind": "Greater Noida", "tag": "shop:convenience"}]
            return [{"name": "Metro Dhaba", "pt": [28.6, 77.3], "kind": "Delhi", "tag": "amenity:restaurant"}]
        server.photon_get = fake
        try:
            out = server.rank("metro", HOME, server.photon("metro", HOME))
        finally:
            server.photon_get = old
        self.assertEqual(calls, [("api/", None), ("reverse", "railway:station")])
        self.assertEqual([r["name"] for r in out], ["Knowledge Park II", "Metro Dhaba"])

    def test_overture_places_near_user_are_found_and_one_source_down_is_survived(self):
        old = server.store, server.photon, server.GEOAPIFY_KEY, server.SEARCH_DB
        with tempfile.TemporaryDirectory() as d:
            server.store = TileStore(d)
            with open(server.store._path((2847, 7749), "places"), "w") as f:
                json.dump({"elements": [{"lat": 28.475, "lon": 77.495, "tags": {"name": "Sharma Tea Stall", "overture": "cafe"}},
                                        {"lat": 28.476, "lon": 77.495, "tags": {"name": "Gupta Sweets", "overture": "bakery"}},
                                        {"lat": 28.477, "lon": 77.495, "tags": {"name": "SBI", "overture": "atm"}}]}, f)

            def down(q, near):
                raise OSError("no network")
            server.photon, server.GEOAPIFY_KEY, server.SEARCH_DB = down, "", os.path.join(d, "none.db")  # not the laptop's real index
            try:
                out = server.geocode("tea st", HOME)
                atms = server.nearby_places("atm", HOME)  # categories match too, not just names
            finally:
                server.store, server.photon, server.GEOAPIFY_KEY, server.SEARCH_DB = old
        self.assertEqual([(r["name"], r["kind"]) for r in out], [("Sharma Tea Stall", "cafe")])
        self.assertEqual([r["name"] for r in atms], ["SBI"])
        self.assertNotIn(("tea st", (28.47, 77.49)), server.geocode_cache)  # offline answers aren't cached


if __name__ == "__main__":
    unittest.main()
