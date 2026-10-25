"""Web app + JSON API. Usage: python server.py [data/osm.json] [port]"""
import json
import os
import sys
import threading
import time
from datetime import datetime
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from safety import REPORT_TYPES, Area

OSM_PATH = sys.argv[1] if len(sys.argv) > 1 else "data/osm.json"
REPORTS_PATH = os.path.join(os.path.dirname(OSM_PATH) or ".", "reports.json")
STATIC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
lock = threading.Lock()


def load():
    with open(OSM_PATH) as f:
        osm = json.load(f)
    reports = json.load(open(REPORTS_PATH)) if os.path.exists(REPORTS_PATH) else []
    return Area(osm, reports)


area = load()


def latlon(s):
    lat, lon = (float(v) for v in s.split(","))
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        raise ValueError("bad coordinates")
    return lat, lon


def in_area(pt, pad=0.01):
    s, w, n, e = area.bounds
    return s - pad <= pt[0] <= n + pad and w - pad <= pt[1] <= e + pad


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw):
        super().__init__(*a, directory=STATIC, **kw)

    def send_json(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def when(self, q):
        return datetime.fromisoformat(q["at"][0]) if "at" in q else datetime.now()

    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        try:
            if u.path == "/api/area":
                return self.send_json({"bounds": area.bounds})
            if u.path == "/api/heat":
                return self.send_json(area.heat(self.when(q)))
            if u.path == "/api/route":
                a, b = latlon(q["from"][0]), latlon(q["to"][0])
                if not (in_area(a) and in_area(b)):
                    return self.send_json({"error": "Start and destination must be inside the mapped area"}, 400)
                return self.send_json(area.routes(a, b, self.when(q)))
        except (KeyError, ValueError) as e:
            return self.send_json({"error": f"bad request: {e}"}, 400)
        super().do_GET()

    def do_POST(self):
        if urlparse(self.path).path != "/api/report":
            return self.send_json({"error": "not found"}, 404)
        try:
            n = int(self.headers.get("Content-Length", 0))
            if n > 10_000:
                raise ValueError("too large")
            body = json.loads(self.rfile.read(n))
            r = {"lat": float(body["lat"]), "lon": float(body["lon"]), "type": body["type"], "ts": time.time()}
            if r["type"] not in REPORT_TYPES or not in_area((r["lat"], r["lon"]), 0):
                raise ValueError("unknown type or outside area")
        except (KeyError, ValueError, TypeError) as e:
            return self.send_json({"error": f"bad report: {e}"}, 400)
        with lock:
            area.add_report(r)
            with open(REPORTS_PATH + ".tmp", "w") as f:
                json.dump(area.reports, f)
            os.replace(REPORTS_PATH + ".tmp", REPORTS_PATH)  # never leave a half-written file
        self.send_json({"ok": True})


if __name__ == "__main__":
    port = int(sys.argv[2]) if len(sys.argv) > 2 else 8000
    print(f"{len(area.edges)} road segments loaded, open http://localhost:{port}")
    ThreadingHTTPServer(("", port), Handler).serve_forever()
