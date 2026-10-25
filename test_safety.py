import os
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone

from safety import Area, daylight, is_open
from tiles import TILE, TileStore, TooFar, tiles_near

IST = timezone(timedelta(hours=5, minutes=30))
NIGHT, LATE, NOON = (datetime(2026, 10, 25, h, 0, tzinfo=IST) for h in (22, 2, 12))

# A sits west of B. The direct way is a short, dark footway. The 1.4x detour north is a
# lit residential street with lamps and shops that are open until 23:00.
A, B = (28.0, 77.0), (28.0, 77.003)
NORTH = [(28.0, 77.0), (28.0005, 77.0), (28.0005, 77.001), (28.0005, 77.002), (28.0005, 77.003), (28.0, 77.003)]


def way(wid, ids, pts, **tags):
    return {"type": "way", "id": wid, "nodes": ids, "geometry": [{"lat": a, "lon": b} for a, b in pts], "tags": tags}


def node(nid, pt, **tags):
    return {"type": "node", "id": nid, "lat": pt[0], "lon": pt[1], "tags": tags}


def fixture():
    els = [
        way(1, [1, 2], [A, B], highway="footway", lit="no"),
        way(2, [1, 10, 11, 12, 13, 2], NORTH, highway="residential", name="Market Road"),
        way(3, [11, 20], [(28.0005, 77.001), (28.0012, 77.001)], highway="service"),  # dead-end lane off the street
    ]
    for i, ((a1, o1), (a2, o2)) in enumerate(zip(NORTH, NORTH[1:])):
        for k in range(4):  # a lamp about every 35 m
            els.append(node(100 + 4 * i + k, (a1 + (a2 - a1) * k / 4 + 0.0001, o1 + (o2 - o1) * k / 4), highway="street_lamp"))
        els.append(node(200 + i, ((a1 + a2) / 2 + 0.0002, (o1 + o2) / 2), shop="convenience",
                        opening_hours="Mo-Su 08:00-23:00", name=f"Shop {i}"))
    els.append(node(300, (28.0007, 77.0025), amenity="pharmacy", opening_hours="24/7", name="City Pharmacy"))
    return {"elements": els}


class RouteTest(unittest.TestCase):
    def setUp(self):
        self.area = Area(fixture())

    def kinds(self, when):
        return {r["kind"]: r for r in self.area.routes(A, B, when)}

    def test_night_safest_takes_lit_street(self):
        r = self.kinds(NIGHT)
        self.assertGreater(r["safest"]["length_m"], 350)
        self.assertLess(r["fastest"]["length_m"], 300)
        self.assertGreater(r["safest"]["score"], r["fastest"]["score"])
        self.assertTrue(any("open places" in s for s in r["safest"]["reasons"]))
        self.assertIn("Lighting", r["safest"]["factors"])

    def test_identical_routes_are_flagged(self):
        r = self.kinds(NIGHT)
        self.assertEqual(r["balanced"]["same_as"], "safest")
        self.assertIsNone(r["safest"]["same_as"])

    def test_shops_closed_lowers_score(self):
        self.assertLess(self.kinds(LATE)["safest"]["score"], self.kinds(NIGHT)["safest"]["score"])

    def test_harassment_report_lowers_score(self):
        before = self.kinds(NIGHT)["safest"]["score"]
        self.area.add_report({"lat": 28.0005, "lon": 77.0015, "type": "harassment", "ts": time.time()})
        self.assertLess(self.kinds(NIGHT)["safest"]["score"], before)

    def test_incident_at_same_hour_counts_more(self):
        when = NIGHT.timestamp()
        evening = Area(fixture(), incidents=[{"lat": 28.0005, "lon": 77.0015, "type": "assault", "hour": 22, "ts": when}])
        morning = Area(fixture(), incidents=[{"lat": 28.0005, "lon": 77.0015, "type": "assault", "hour": 9, "ts": when}])
        s = lambda a: {r["kind"]: r for r in a.routes(A, B, NIGHT)}["safest"]["score"]
        self.assertLess(s(evening), s(morning))

    def test_dead_end_lane_has_no_way_out(self):
        lane = next(e for e in self.area.edges if e["highway"] == "service")
        feats, _ = self.area.factors(lane, NIGHT, 0)
        self.assertEqual(feats["escape"], 0.2)

    def test_park_hurts_at_night_only(self):
        osm = fixture()
        park = [(28.0004, 77.0), (28.0004, 77.003), (27.9996, 77.003), (27.9996, 77.0), (28.0004, 77.0)]
        osm["elements"].append(way(9, list(range(900, 905)), park, leisure="park"))
        area = Area(osm)
        foot = next(e for e in area.edges if e["highway"] == "footway")
        self.assertEqual(foot["zones"], ["park"])
        night, _ = area.factors(foot, NIGHT, 0)
        day, _ = area.factors(foot, NOON, 1)
        self.assertLess(night["surroundings"], day["surroundings"])

    def test_turn_by_turn(self):
        r = self.area.routes(A, (28.0012, 77.001), NIGHT)[0]
        texts = [st["text"] for st in r["steps"]]
        self.assertEqual(texts[0], "Head north on Market Road")
        self.assertIn("Turn left onto the lane", texts)
        self.assertEqual(r["steps"][-1]["turn"], "arrive")
        self.assertEqual(r["turns"], 1)

    def test_havens(self):
        h = self.area.havens_near((28.0007, 77.0024), LATE)
        self.assertEqual(h[0]["name"], "City Pharmacy")  # the only place open at 2 am


class TimeTest(unittest.TestCase):
    def test_daylight(self):
        self.assertEqual(daylight(28.6, 77.2, NOON), 1.0)
        self.assertEqual(daylight(28.6, 77.2, NIGHT), 0.0)
        dusk = datetime(2026, 10, 25, 17, 45, tzinfo=IST)  # sunset in Delhi is about 17:35 in late October
        self.assertTrue(0 < daylight(28.6, 77.2, dusk) < 1)

    def test_opening_hours(self):
        sat_1am, mon_noon = datetime(2026, 10, 24, 1, 0), datetime(2026, 10, 26, 12, 0)
        self.assertTrue(is_open("24/7", sat_1am))
        self.assertTrue(is_open("Mo-Fr 09:00-18:00", mon_noon))
        self.assertFalse(is_open("Mo-Fr 09:00-18:00", sat_1am))
        self.assertTrue(is_open("Fr 22:00-02:00", sat_1am))  # Friday night runs past midnight
        self.assertFalse(is_open("Mo-Su 09:00-21:00; Mo off", mon_noon))
        self.assertTrue(is_open("09:00-13:00, 14:00-20:00", mon_noon))
        self.assertIsNone(is_open("sunrise-sunset", mon_noon))


class TileTest(unittest.TestCase):
    def setUp(self):
        self.calls = []

        def fake_fetch(s, w, n, e):
            self.calls.append((s, w))
            return fixture()  # every tile returns the same streets, so merging must drop duplicates

        self.dir = tempfile.mkdtemp()
        self.store = TileStore(self.dir, fetch=fake_fetch)

    def test_any_spot_is_fetched_once_and_merged(self):
        area = self.store.for_trip(A, B)
        self.assertEqual(len(area.edges), len(Area(fixture()).edges))
        n = len(self.calls)
        self.assertLess(n, len(os.listdir(self.dir)))  # missing tiles are fetched in blocks, not one by one
        self.store.for_trip(A, B)  # cached area
        self.assertEqual(len(self.calls), n)
        TileStore(self.dir, fetch=lambda *a: self.fail("tile should come from disk")).for_trip(A, B)

    def test_route_anywhere(self):
        r = {x["kind"]: x for x in self.store.for_trip(A, B).routes(A, B, NIGHT)}
        self.assertGreater(r["safest"]["score"], r["fastest"]["score"])

    def test_too_far(self):
        with self.assertRaises(TooFar):
            self.store.for_trip(A, (28.5, 77.5))

    def test_tiles_follow_the_line(self):
        diag = tiles_near((28.0, 77.0), (28.1, 77.1), 800)
        box = (0.1 / TILE + 1) ** 2
        self.assertLess(len(diag), box * 0.7)  # a corridor, not the whole bounding box

    def test_no_streets(self):
        store = TileStore(tempfile.mkdtemp(), fetch=lambda *a: {"elements": []})
        with self.assertRaisesRegex(ValueError, "No walkable streets"):
            store.for_trip(A, B)

    def test_report_reaches_cached_area(self):
        area = self.store.for_trip(A, B)
        self.store.add_report({"lat": 28.0005, "lon": 77.0015, "type": "harassment", "ts": time.time()})
        self.assertTrue(any(e["reports"] for e in area.edges))


class LightingGuessTest(unittest.TestCase):
    def test_untagged_streets_get_a_lighting_guess(self):
        els = [way(1, [1, 2], [A, B], highway="primary"), way(2, [3, 4], [(28.001, 77.0), (28.001, 77.003)], highway="track")]
        area = Area({"elements": els})
        light = lambda i: area.factors(area.edges[i], NIGHT, 0)[0]["lighting"]
        self.assertGreater(light(0), 0.5)  # main roads in town are almost always lit
        self.assertLess(light(1), 0.2)


if __name__ == "__main__":
    unittest.main()
