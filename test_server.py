"""Privacy and abuse checks for the HTTP server. Usage: python -m unittest test_server"""
import json
import threading
import unittest
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
from unittest import mock

import server


class ServerTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()

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
