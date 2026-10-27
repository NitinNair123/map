"""Every API endpoint, its failure cases, and the privacy and abuse rules. Usage: python -m unittest test_server"""
import json
import os
import tempfile
import threading
import unittest
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
from unittest import mock

import server
from test_safety import A, B, NIGHT, fixture
from tiles import TileStore


class ServerTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # offline: streets from the test fixture, no weather or place search over the network
        cls.patches = [mock.patch.object(server, "store", TileStore(tempfile.mkdtemp(), fetch=lambda *a: fixture())),
                       mock.patch.object(server, "is_raining", lambda *a: False),
                       mock.patch.object(server, "geocode", lambda q, near: [{"name": "City Pharmacy", "pt": [28.0007, 77.0025], "kind": "pharmacy"}]),
                       mock.patch.object(server, "REPORTS_PATH", os.path.join(tempfile.mkdtemp(), "reports.json"))]
        for p in cls.patches:
            p.start()
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        for p in cls.patches:
            p.stop()

    def setUp(self):
        server.trips.clear()
        server.hits.clear()

    def call(self, path, body=None, headers=None):
        c = HTTPConnection("127.0.0.1", self.httpd.server_address[1], timeout=5)
        data = None if body is None else json.dumps(body)
        c.request("GET" if body is None else "POST", path, data, headers or {})
        r = c.getresponse()
        out = r.status, json.loads(r.read() or b"null") if r.getheader("Content-Type") == "application/json" else None
        c.close()
        return out

    def test_route(self):
        at = NIGHT.isoformat().replace("+", "%2B")
        code, out = self.call(f"/api/route?from={A[0]},{A[1]}&to={B[0]},{B[1]}&at={at}")
        self.assertEqual(code, 200)
        self.assertEqual({r["kind"] for r in out["routes"]} >= {"safest", "fastest"}, True)
        self.assertTrue(all(0 <= r["score"] <= 100 and r["coords"] for r in out["routes"]))

    def test_heat_havens_search(self):
        code, heat = self.call(f"/api/heat?bbox=27.999,76.999,28.001,77.004")
        self.assertEqual(code, 200)
        self.assertTrue(heat and all(len(h) == 5 for h in heat))
        code, havens = self.call(f"/api/havens?near={A[0]},{A[1]}&at={NIGHT.isoformat().replace('+', '%2B')}")
        self.assertEqual(code, 200)
        self.assertIn("City Pharmacy", [h["name"] for h in havens])  # open 24/7
        self.assertEqual(self.call("/api/search?q=c")[1], [])  # too short to search
        self.assertEqual(self.call("/api/search?q=city&near=28,77")[1][0]["name"], "City Pharmacy")

    def test_bad_input_is_a_400_with_a_message(self):
        for path in ["/api/route?from=28,77", "/api/route?from=91,77&to=28,77", "/api/route?from=nan,77&to=28,77",
                     "/api/route?from=28,77&to=28.001,77&at=yesterday", "/api/heat?bbox=28.001,77,28,77.001",
                     "/api/heat?bbox=28,77,inf,77.1", "/api/heat?bbox=1,2,3", "/api/havens", "/api/search?q=ab&near=x"]:
            code, out = self.call(path)
            self.assertEqual(code, 400, path)
            self.assertIn("error", out)
        for body in [{"lat": 28, "lon": 77, "type": "aliens"}, {"lat": 28, "type": "felt_safe"}, {"lat": "x", "lon": 77, "type": "felt_safe"},
                     {"lat": 28, "lon": 77, "type": ["felt_safe"]}]:
            self.assertEqual(self.call("/api/report", body)[0], 400, body)
        self.assertEqual(self.call("/api/trip", {"dest": 5})[0], 400)
        self.assertEqual(self.call("/api/trip", [1, 2])[0], 400)

    def test_far_walk_and_map_service_down(self):
        self.assertIn("km walk", self.call("/api/route?from=28,77&to=28.5,77")[1]["error"])
        def down(*a):
            raise ConnectionError("Map data servers are busy, please try again in a minute")
        with mock.patch.object(server, "store", TileStore(tempfile.mkdtemp(), fetch=down)):
            code, out = self.call("/api/route?from=28.1,77.1&to=28.101,77.1")
            self.assertEqual((code, out["error"]), (503, "Map data servers are busy, please try again in a minute"))
        def offline(*a):
            raise OSError("no network")
        with mock.patch.object(server, "store", TileStore(tempfile.mkdtemp(), fetch=offline)):
            self.assertEqual(self.call("/api/route?from=28.1,77.1&to=28.101,77.1")[0], 503)

    def test_a_bug_still_answers_json(self):
        with mock.patch.object(server, "geocode", side_effect=RuntimeError("bug")):
            code, out = self.call("/api/search?q=city")
        self.assertEqual(code, 500)
        self.assertIn("our side", out["error"])

    def test_report_is_saved_and_limited(self):
        self.assertEqual(self.call("/api/report", {"lat": 28.0005, "lon": 77.0015, "type": "harassment"})[0], 200)
        with open(server.REPORTS_PATH) as f:
            self.assertEqual(json.load(f)[-1]["type"], "harassment")
        codes = [self.call("/api/report", {"lat": 28.0005, "lon": 77.0015, "type": "felt_safe"})[0] for _ in range(10)]
        self.assertEqual(codes[-1], 429)

    def test_pages_are_served(self):
        for path in ["/", "/app/", "/t/?id=x", "/manifest.webmanifest", "/sw.js", "/app/app.js"]:
            self.assertEqual(self.call(path)[0], 200, path)
        self.assertEqual(self.call("/../server.py")[0], 404)

    def test_ending_a_trip_deletes_its_locations(self):
        _, t = self.call("/api/trip", {"dest": [28.6, 77.2], "eta": 10})
        self.assertEqual(self.call(f"/api/trip/{t['id']}", {"key": t["key"], "lat": 28.61, "lon": 77.21})[0], 200)
        self.assertEqual(self.call(f"/api/trip/{t['id']}", {"key": "wrong", "lat": 1, "lon": 1})[0], 404)
        self.call(f"/api/trip/{t['id']}", {"key": t["key"], "ended": True})
        code, view = self.call(f"/api/trip/{t['id']}")
        self.assertEqual((code, view["ended"], view["points"], view["dest"]), (200, True, [], None))
        # an ended trip can't be restarted or moved again
        self.assertEqual(self.call(f"/api/trip/{t['id']}", {"key": t["key"], "lat": 28.6, "lon": 77.2})[0], 404)

    def test_trips_expire(self):
        _, t = self.call("/api/trip", {"dest": [28.6, 77.2]})
        server.trips[t["id"]]["started"] -= server.TRIP_TTL + 1
        self.assertEqual(self.call(f"/api/trip/{t['id']}")[0], 404)

    def test_one_client_cannot_fill_the_trip_table(self):
        codes = [self.call("/api/trip", {"dest": [28.6, 77.2]}, {"X-Forwarded-For": "1.2.3.4"})[0] for _ in range(11)]
        self.assertEqual(codes, [200] * 10 + [429])
        # someone else behind the same tunnel still gets a trip
        self.assertEqual(self.call("/api/trip", {"dest": [28.6, 77.2]}, {"X-Forwarded-For": "5.6.7.8"})[0], 200)

    def test_bad_lengths_and_values(self):
        self.assertEqual(self.call("/api/trip", {"dest": [28.6, 77.2], "eta": "inf"})[0], 400)
        c = HTTPConnection("127.0.0.1", self.httpd.server_address[1], timeout=5)
        c.putrequest("POST", "/api/trip"); c.putheader("Content-Length", "-1"); c.endheaders()
        self.assertEqual(c.getresponse().status, 400)
        c.close()

    def test_no_directory_listing(self):
        self.assertEqual(self.call("/icons/")[0], 404)

    def test_outside_clients_refused_unless_public(self):
        h = mock.Mock(client_address=("8.8.8.8", 1))
        self.assertFalse(server.Handler.private_client(h))
        for ip in ("192.168.1.5", "::ffff:10.0.0.2", "127.0.0.1", "fe80::1%eth0"):
            h.client_address = (ip, 1)
            self.assertTrue(server.Handler.private_client(h), ip)

    def test_logs_leave_out_places_and_trip_ids(self):
        self.assertEqual(server.redact("/api/route?from=28.6,77.2&to=28.7,77.3"), "/api/route")
        self.assertEqual(server.redact("/api/trip/abc123"), "/api/trip/<id>")


if __name__ == "__main__":
    unittest.main()
