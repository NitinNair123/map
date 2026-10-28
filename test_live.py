"""Live buses and traffic: feed decoding, parked buses, warm-up, and how they reach the activity factor."""
import struct
import time
import unittest
from datetime import datetime

import buses
import rating
import traffic
from test_safety import A, B, way
from safety import Area


def varint(n):
    out = b""
    while True:
        out += bytes([(n & 0x7F) | (0x80 if n > 0x7F else 0)])
        n >>= 7
        if not n:
            return out


def field(num, payload):
    return varint(num << 3 | 2) + varint(len(payload)) + payload


def entity(vid, lat, lon, ts):
    pos = varint(1 << 3 | 5) + struct.pack("<f", lat) + varint(2 << 3 | 5) + struct.pack("<f", lon)
    vp = field(2, pos) + varint(5 << 3) + varint(ts) + field(8, field(1, vid.encode()))
    return field(2, field(1, b"e" + vid.encode()) + field(4, vp))


class LiveBusTest(unittest.TestCase):
    def test_decode(self):
        feed = field(1, varint(3 << 3) + varint(1)) + entity("DL1PC1", 28.61, 77.21, 1700000000)
        [(vid, lat, lon, ts)] = buses.decode(feed)
        self.assertEqual((vid, ts), ("DL1PC1", 1700000000))
        self.assertAlmostEqual(lat, 28.61, 4)
        self.assertAlmostEqual(lon, 77.21, 4)

    def test_counts_moving_buses_after_warm_up(self):
        live, t0, pt = buses.LiveBuses(), 1_000_000.0, (28.6100, 77.2100)
        live.record([("bus", pt[0] - 0.001, pt[1], None), ("parked", *pt, None)], t0)
        live.record([("bus", *pt, None), ("parked", *pt, None)], t0 + 10)
        self.assertIsNone(live.near(pt, now=t0 + 20))  # not a full window watched yet
        t1 = t0 + 20 + buses.WINDOW_S
        live.record([], t1)
        self.assertEqual(live.near(pt, now=t1), set())  # the bus went by too long ago
        live.record([("bus", pt[0] + 0.0005, pt[1], None)], t1 + 5)
        self.assertEqual(live.near(pt, now=t1 + 6), {"bus"})  # the parked bus never counts
        self.assertIsNone(live.near(pt, datetime.fromtimestamp(t1 + 3600), now=t1 + 6))  # a trip planned for later gets no live count

    def test_live_buses_raise_activity(self):
        area = Area({"elements": [way(1, [1, 2], [A, B], highway="residential")]})
        e, now = area.edges[0], time.time()
        old = buses.LIVE
        buses.LIVE = buses.LiveBuses()
        try:
            buses.LIVE.started = now - buses.WINDOW_S
            buses.LIVE.ok_at = now
            buses.LIVE.cells[buses._cell(*e["mid"])] = {"a": now, "b": now, "c": now}
            act = rating.factors(area, e, datetime.now(), 0, now)["activity"]
            self.assertIn("Live buses (Delhi Open Transit Data)", act["sources"])
            self.assertIn("3 buses passed in the last 30 min", area.routes(A, B, datetime.now())[0]["reasons"])
        finally:
            buses.LIVE = old


class FootTapTest(unittest.TestCase):
    def test_taps_show_on_route_card(self):
        area = Area({"elements": [way(1, [1, 2], [A, B], highway="residential")]})
        for by, kind in (("x", "people_around"), ("y", "people_around"), ("z", "deserted")):
            area.add_report({"lat": A[0], "lon": A[1] + 0.001, "type": kind, "ts": time.time(), "by": by})
        r = area.routes(A, B, datetime.now())[0]
        self.assertIn("2 people said it's busy at this time", r["reasons"])
        self.assertIn("1 person said it's deserted at this time", r["warnings"])


class LiveTrafficTest(unittest.TestCase):
    def test_only_slow_confident_traffic_counts(self):
        live, now, pt = traffic.LiveTraffic(), time.time(), (28.61, 77.21)
        seg = lambda cur, conf=1.0: {"flowSegmentData": {"currentSpeed": cur, "freeFlowSpeed": 40, "confidence": conf,
                                                        "coordinates": {"coordinate": [{"latitude": pt[0], "longitude": pt[1]}]}}}
        live.record(seg(15, 0.3), now)
        self.assertIsNone(live.jammed(pt, now=now))  # TomTom's usual speed, not a live one
        live.record(seg(38), now)
        self.assertFalse(live.jammed(pt, now=now))
        live.record(seg(15), now)
        self.assertTrue(live.jammed(pt, now=now))
        self.assertIsNone(live.jammed(pt, now=now + traffic.FRESH_S))


if __name__ == "__main__":
    unittest.main()
