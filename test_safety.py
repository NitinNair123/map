import time
import unittest
from datetime import datetime

from safety import Area, is_open

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
        way(1, [1, 2], [A, B], highway="footway"),
        way(2, [1, 10, 11, 12, 13, 2], NORTH, highway="residential"),
    ]
    for i, ((a1, o1), (a2, o2)) in enumerate(zip(NORTH, NORTH[1:])):
        for k in range(4):  # a lamp about every 35 m
            els.append(node(100 + 4 * i + k, (a1 + (a2 - a1) * k / 4 + 0.0001, o1 + (o2 - o1) * k / 4), highway="street_lamp"))
        els.append(node(200 + i, ((a1 + a2) / 2 + 0.0002, (o1 + o2) / 2), shop="convenience", opening_hours="Mo-Su 08:00-23:00"))
    return {"elements": els}


class RouteTest(unittest.TestCase):
    def setUp(self):
        self.area = Area(fixture())

    def kinds(self, when):
        return {r["kind"]: r for r in self.area.routes(A, B, when)}

    def test_night_safest_takes_lit_street(self):
        r = self.kinds(datetime(2026, 10, 25, 22, 0))
        self.assertGreater(r["safest"]["length_m"], 350)
        self.assertLess(r["fastest"]["length_m"], 300)
        self.assertGreater(r["safest"]["score"], r["fastest"]["score"])
        self.assertTrue(any("open places" in s for s in r["safest"]["reasons"]))

    def test_shops_closed_lowers_score(self):
        late = self.kinds(datetime(2026, 10, 26, 2, 0))["safest"]["score"]
        evening = self.kinds(datetime(2026, 10, 25, 22, 0))["safest"]["score"]
        self.assertLess(late, evening)

    def test_harassment_report_lowers_score(self):
        when = datetime(2026, 10, 25, 22, 0)
        before = self.kinds(when)["safest"]["score"]
        self.area.add_report({"lat": 28.0005, "lon": 77.0015, "type": "harassment", "ts": time.time()})
        self.assertLess(self.kinds(when)["safest"]["score"], before)


class OpeningHoursTest(unittest.TestCase):
    def test_forms(self):
        sat_1am, mon_noon = datetime(2026, 10, 24, 1, 0), datetime(2026, 10, 26, 12, 0)
        self.assertTrue(is_open("24/7", sat_1am))
        self.assertTrue(is_open("Mo-Fr 09:00-18:00", mon_noon))
        self.assertFalse(is_open("Mo-Fr 09:00-18:00", sat_1am))
        self.assertTrue(is_open("Fr 22:00-02:00", sat_1am))  # Friday night runs past midnight
        self.assertFalse(is_open("Mo-Su 09:00-21:00; Mo off", mon_noon))
        self.assertTrue(is_open("09:00-13:00, 14:00-20:00", mon_noon))
        self.assertIsNone(is_open("sunrise-sunset", mon_noon))


if __name__ == "__main__":
    unittest.main()
