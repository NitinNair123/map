import os
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone

from safety import classify, Area, daylight, is_open
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
        self.assertTrue(any("confirmed open now" in s for s in r["safest"]["reasons"]))
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

    def test_extra_source_adds_cameras_and_survives_failure(self):
        cam = lambda *a: {"elements": [node("mly1", (28.0001, 77.0015), man_made="surveillance")]}
        area = TileStore(self.dir, fetch=lambda *a: fixture(), extras={"mapillary": cam}).for_trip(A, B)
        self.assertTrue(any(e["near"]["cctv"] for e in area.edges))
        def down(*a):
            raise OSError("down")
        TileStore(tempfile.mkdtemp(), fetch=lambda *a: fixture(), extras={"mapillary": down}).for_trip(A, B)

    def test_report_reaches_cached_area(self):
        area = self.store.for_trip(A, B)
        self.store.add_report({"lat": 28.0005, "lon": 77.0015, "type": "harassment", "ts": time.time()})
        self.assertTrue(any(e["reports"] for e in area.edges))


class NoGuessingTest(unittest.TestCase):
    def test_unknown_lighting_and_hours_are_left_out(self):
        els = [way(1, [1, 2], [A, B], highway="primary"), node(5, (28.0001, 77.0015), shop="bakery")]
        area = Area({"elements": els})
        feats, _ = area.factors(area.edges[0], NIGHT, 0)
        self.assertIsNone(feats["lighting"])  # no lit tag and no lamps: unknown, not guessed
        self.assertIsNone(feats["activity"])  # a shop with no published hours: unknown, not assumed open
        r = area.routes(A, B, NIGHT)[0]
        self.assertIsNone(r["factors"]["Lighting"])
        self.assertTrue(r["low_data"])
        self.assertIn("No lighting data for 100%", r["reasons"])

class LightReportTest(unittest.TestCase):
    def lighting(self, *reports):
        area = Area({"elements": [way(1, [1, 2], [A, B], highway="primary", lit="yes")]})
        for r in reports:
            area.add_report({"lat": 28.0, "lon": 77.0015, "ts": r[1].timestamp(), "type": r[0]})
        return area.factors(area.edges[0], NIGHT, 0)[0]["lighting"]

    def test_reports_after_dark_override_map_and_fade(self):
        recent = datetime.now(IST).replace(hour=23)
        if recent > datetime.now(IST):
            recent -= timedelta(days=1)
        self.assertEqual(self.lighting(("broken_light", recent)), 0.0)
        self.assertEqual(self.lighting(("broken_light", recent), ("lights_on", recent)), 1.0)  # a tie falls back to lit=yes
        self.assertEqual(self.lighting(("broken_light", recent - timedelta(days=90))), 1.0)  # an old report fades out
        self.assertEqual(self.lighting(("broken_light", recent.replace(hour=13))), 1.0)  # nobody can see it in daylight

    def test_old_mapillary_objects_dropped(self):
        from tiles import seen_years_ago
        self.assertAlmostEqual(seen_years_ago((time.time() - 2 * 365.25 * 86400) * 1000), 2, places=2)
        self.assertGreater(seen_years_ago("2016-05-01T10:00:00Z"), 5)
        self.assertIsNone(seen_years_ago(None))


class OpenDataTest(unittest.TestCase):
    def test_overture_categories(self):
        self.assertEqual(classify({"overture": "pharmacy_and_drug_store"}), "haven")
        self.assertEqual(classify({"overture": "bar"}), "nightlife")
        self.assertEqual(classify({"overture": "barber"}), "place")
        self.assertEqual(classify({"overture": "train_station"}), "transit")
        self.assertEqual(classify({"overture": "gas_station"}), "haven")
        self.assertEqual(classify({"overture": "fire_station"}), "haven")
        self.assertEqual(classify({"overture": "ev_charging_station"}), "place")
        self.assertEqual(classify({"overture": "animal_hospital"}), "place")
        self.assertEqual(classify({"overture": "sports_bar"}), "nightlife")

    def test_two_junctions_near_one_point_both_kept(self):
        import struct
        import tiles
        pts = [(77.0, 28.0), (77.001, 28.0), (77.002, 28.0)]
        wkb = struct.pack("<BII", 1, 2, len(pts)) + b"".join(struct.pack("<dd", *p) for p in pts)
        row = {"id": "s1", "geometry": wkb, "class": "residential", "subclass": None, "names": None, "road_flags": None,
               "sources": None, "connectors": [{"connector_id": "c0", "at": 0}, {"connector_id": "c1", "at": 0.5},
                                               {"connector_id": "c2", "at": 0.501}, {"connector_id": "c3", "at": 1}]}
        real = tiles._overture
        tiles._overture = lambda *a, **k: [row]
        try:
            nodes = tiles.overture_roads(0, 0, 1, 1)["elements"][0]["nodes"]
        finally:
            tiles._overture = real
        self.assertEqual(nodes, ["c0", "c1", "c2", "c3"])  # the 4th junction gets its own point instead of replacing one

    def test_buildings_overlook_street(self):
        els = fixture()["elements"] + [node(f"b{k}", (28.0004, 77.0005 + k * 0.0001), building="yes") for k in range(20)]
        area = Area({"elements": els})
        feats = lambda a: a.factors(a.edges[1], NIGHT, 0)[0]["overlooked"]
        self.assertIsNone(feats(Area(fixture())))  # no footprints loaded: unknown, not zero
        self.assertGreater(feats(area), 0)

    def test_slow_source_is_fetched_in_background(self):
        d = tempfile.mkdtemp()
        b = lambda *a: {"elements": [node("b1", (28.0004, 77.0005), building="yes")]}
        store = TileStore(d, fetch=lambda *a: fixture(), extras={"buildings": b}, background={"buildings"})
        store.for_trip(A, B)
        store.pool.shutdown(wait=True)
        self.assertTrue(store.for_trip(A, B).building_cells)


    def test_fast_roads_with_osm_detail_later(self):
        def roads(*a):  # the fixture streets as Overture segments, linked to their OSM ways
            out = []
            for el in fixture()["elements"]:
                if el["type"] == "way":
                    tags = {"highway": el["tags"]["highway"], "overture_road": "yes", "osm_way": el["id"]}
                    out.append({**el, "id": f"ovr{el['id']}", "tags": tags})
            return {"elements": out}
        osm_calls = []
        def osm(*a):
            osm_calls.append(a)
            return fixture()
        store = TileStore(tempfile.mkdtemp(), fetch=osm, roads=roads)
        first = store.for_trip(A, B)
        self.assertEqual(len(first.edges), len(Area(fixture()).edges))
        self.assertFalse(first.pois)  # OSM detail is still loading
        store.pool.shutdown(wait=True)
        self.assertTrue(osm_calls)
        later = store.for_trip(A, B)
        self.assertEqual(len(later.edges), len(Area(fixture()).edges))  # OSM ways lend tags, not duplicate streets
        self.assertTrue(later.pois)
        self.assertEqual([e["lit"] for e in later.edges if e["highway"] == "footway"], ["no"])
        surroundings = lambda area: area.factors(area.edges[1], NIGHT, 0)[0]["surroundings"]
        self.assertIsNone(surroundings(first))  # parks and wasteland not loaded yet: unknown, not safe
        self.assertIsNotNone(surroundings(later))

    def test_overpass_answers_when_overture_is_down(self):
        def roads(*a):
            raise OSError("S3 unreachable")
        store = TileStore(tempfile.mkdtemp(), fetch=lambda *a: fixture(), roads=roads)
        area = store.for_trip(A, B)
        self.assertEqual(len(area.edges), len(Area(fixture()).edges))
        self.assertTrue(area.pois)
        self.assertFalse(store.areas)  # not cached, so Overture is tried again next trip

    def test_buildings_not_loaded_here_are_unknown(self):
        els = fixture()["elements"] + [node("b1", (28.0004, 77.0005), building="yes")]
        area = Area({"elements": els}, unloaded={"overlooked": [(27, 76, 29, 78)]})
        self.assertIsNone(area.factors(area.edges[1], NIGHT, 0)[0]["overlooked"])

    def test_area_built_during_background_write_is_not_cached(self):
        import tiles
        store = TileStore(tempfile.mkdtemp(), fetch=lambda *a: fixture())
        real = tiles.Area
        def racing(*a):
            store.version += 1  # a background fetch lands while the area is being built
            return real(*a)
        tiles.Area = racing
        try:
            store.for_trip(A, B)
        finally:
            tiles.Area = real
        self.assertFalse(store.areas)


if __name__ == "__main__":
    unittest.main()
