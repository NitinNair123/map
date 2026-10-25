"""Website, app and JSON API. Usage: python server.py [data/osm.json] [port]"""
import csv
import json
import os
import secrets
import sys
import threading
import time
from datetime import datetime
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlencode, urlparse
from urllib.request import urlopen

from safety import REPORT_TYPES, Area

OSM_PATH = sys.argv[1] if len(sys.argv) > 1 else "data/osm.json"
DATA_DIR = os.path.dirname(OSM_PATH) or "."
REPORTS_PATH = os.path.join(DATA_DIR, "reports.json")
INCIDENTS_PATH = os.path.join(DATA_DIR, "incidents.csv")
STATIC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
TRIP_TTL = 12 * 3600
MAX_TRIPS = 2000
lock = threading.Lock()
# shortcut: live trips live in memory and vanish on restart, move to Redis when running more than one server
trips = {}
report_log = {}  # ip -> recent report times, to stop one person flooding an area
weather = {"rain": False, "checked": 0.0}


def load():
    with open(OSM_PATH) as f:
        osm = json.load(f)
    reports = json.load(open(REPORTS_PATH)) if os.path.exists(REPORTS_PATH) else []
    return Area(osm, reports, load_incidents())


def load_incidents():
    """incidents.csv columns: lat,lon,type,when (ISO date-time), e.g. from police open data or news."""
    if not os.path.exists(INCIDENTS_PATH):
        return []
    out = []
    with open(INCIDENTS_PATH, newline="") as f:
        for n, row in enumerate(csv.DictReader(f), 2):
            try:
                when = datetime.fromisoformat(row["when"])
                out.append({"lat": float(row["lat"]), "lon": float(row["lon"]), "type": row["type"].strip().lower(),
                            "hour": when.hour, "ts": when.timestamp()})
            except (KeyError, ValueError) as e:
                print(f"incidents.csv line {n} skipped: {e}")
    return out


area = load()


def latlon(s):
    lat, lon = (float(v) for v in s.split(",")) if isinstance(s, str) else (float(s[0]), float(s[1]))
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        raise ValueError("bad coordinates")
    return lat, lon


def in_area(pt, pad=0.01):
    s, w, n, e = area.bounds
    return s - pad <= pt[0] <= n + pad and w - pad <= pt[1] <= e + pad


def is_raining(when):
    """Live rain from Open-Meteo (free, no key), refreshed every 15 min; only applies to trips starting about now."""
    if abs((when.astimezone() - datetime.now().astimezone()).total_seconds()) > 3600:
        return False
    if time.time() - weather["checked"] > 900:
        weather["checked"] = time.time()
        try:
            q = urlencode({"latitude": area.center[0], "longitude": area.center[1], "current": "precipitation"})
            with urlopen(f"https://api.open-meteo.com/v1/forecast?{q}", timeout=3) as r:
                weather["rain"] = json.load(r)["current"]["precipitation"] > 0.2
        except Exception:
            weather["rain"] = False  # no network: score without weather rather than fail
    return weather["rain"]


def expire_trips():
    now = time.time()
    for k in [k for k, t in trips.items() if now - t["updated"] > TRIP_TTL]:
        del trips[k]


class Handler(SimpleHTTPRequestHandler):
    extensions_map = {**SimpleHTTPRequestHandler.extensions_map, ".webmanifest": "application/manifest+json", ".js": "text/javascript"}

    def __init__(self, *a, **kw):
        super().__init__(*a, directory=STATIC, **kw)

    def end_headers(self):
        if self.path.startswith("/api/"):
            self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def send_json(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def read_json(self):
        n = int(self.headers.get("Content-Length", 0))
        if n > 10_000:
            raise ValueError("too large")
        body = json.loads(self.rfile.read(n) or b"{}")
        if not isinstance(body, dict):
            raise ValueError("expected an object")
        return body

    def when(self, q):
        return datetime.fromisoformat(q["at"][0]) if q.get("at", [""])[0] else datetime.now()

    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        try:
            if u.path == "/api/area":
                return self.send_json({"bounds": area.bounds})
            if u.path == "/api/search":
                return self.send_json(area.search(q.get("q", [""])[0][:80]))
            if u.path == "/api/heat":
                when = self.when(q)
                return self.send_json(area.heat(when, is_raining(when)))
            if u.path == "/api/havens":
                return self.send_json(area.havens_near(latlon(q["near"][0]), self.when(q)))
            if u.path == "/api/route":
                a, b = latlon(q["from"][0]), latlon(q["to"][0])
                if not (in_area(a) and in_area(b)):
                    return self.send_json({"error": "SafeRoute doesn't cover that spot yet. Pick a start and destination inside the mapped area."}, 400)
                when = self.when(q)
                rain = is_raining(when)
                return self.send_json({"routes": area.routes(a, b, when, rain), "rain": rain})
            if u.path.startswith("/api/trip/"):
                t = trips.get(u.path.rsplit("/", 1)[1])
                if not t:
                    return self.send_json({"error": "This trip has ended or the link is wrong."}, 404)
                return self.send_json({k: t[k] for k in ("name", "dest", "eta", "points", "started", "updated", "ended")})
        except (KeyError, ValueError) as e:
            return self.send_json({"error": f"bad request: {e}"}, 400)
        super().do_GET()

    def do_POST(self):
        path = urlparse(self.path).path
        try:
            body = self.read_json()
            if path == "/api/report":
                return self.report(body)
            if path == "/api/trip":
                return self.start_trip(body)
            if path.startswith("/api/trip/"):
                return self.update_trip(path.rsplit("/", 1)[1], body)
        except (KeyError, ValueError, TypeError) as e:
            return self.send_json({"error": f"bad request: {e}"}, 400)
        self.send_json({"error": "not found"}, 404)

    def report(self, body):
        r = {"lat": float(body["lat"]), "lon": float(body["lon"]), "type": body["type"], "ts": time.time()}
        if r["type"] not in REPORT_TYPES or not in_area((r["lat"], r["lon"]), 0):
            raise ValueError("unknown type or outside area")
        ip = self.client_address[0]
        recent = [t for t in report_log.get(ip, []) if r["ts"] - t < 3600]
        if len(recent) >= 10:
            return self.send_json({"error": "Thanks! You've sent a lot of reports this hour, please try again later."}, 429)
        report_log[ip] = recent + [r["ts"]]
        with lock:
            area.add_report(r)
            with open(REPORTS_PATH + ".tmp", "w") as f:
                json.dump(area.reports, f)
            os.replace(REPORTS_PATH + ".tmp", REPORTS_PATH)  # never leave a half-written file
        self.send_json({"ok": True})

    def start_trip(self, body):
        dest = latlon(body["dest"])
        with lock:
            expire_trips()
            if len(trips) >= MAX_TRIPS:
                return self.send_json({"error": "Too many live trips right now, try again shortly."}, 503)
            tid, key = secrets.token_urlsafe(9), secrets.token_urlsafe(18)
            now = time.time()
            trips[tid] = {"key": key, "name": str(body.get("name", ""))[:40], "dest": dest,
                          "eta": float(body.get("eta", 0)), "points": [], "started": now, "updated": now, "ended": False}
        self.send_json({"id": tid, "key": key})

    def update_trip(self, tid, body):
        t = trips.get(tid)
        # the viewer link only carries the id; only the walker holds the key that can move the dot
        if not t or not secrets.compare_digest(str(body.get("key", "")), t["key"]):
            return self.send_json({"error": "unknown trip"}, 404)
        with lock:
            if "lat" in body:
                t["points"] = (t["points"] + [latlon((body["lat"], body["lon"])) + (time.time(),)])[-300:]
            t["ended"] = bool(body.get("ended", t["ended"]))
            t["updated"] = time.time()
        self.send_json({"ok": True})


if __name__ == "__main__":
    port = int(sys.argv[2]) if len(sys.argv) > 2 else 8000
    print(f"{len(area.edges)} road segments loaded, open http://localhost:{port}")
    ThreadingHTTPServer(("", port), Handler).serve_forever()
