"""Website, app and JSON API. Usage: python server.py [port]"""
import csv
import importlib.util
import json
import os
import secrets
import socket
import subprocess
import sys
import threading
import time
from collections import OrderedDict, defaultdict
from datetime import datetime
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlencode, urlparse
from urllib.request import Request, urlopen

import crime
import gtfs
from import_osm import search as offline_search
from rating import REPORT_TYPES
from reports import Reports
from safety import MODES, dist
from tiles import TileStore, TooFar, geoapify, mapillary, nightlights, overture_buildings, overture_places, overture_roads, warm_overture

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
LAYERS_DIR = os.path.join(DATA_DIR, "layers")
SEARCH_DB = os.path.join(DATA_DIR, "tiles", "search.db")
REIMPORT_DAYS = 30
INCIDENTS_PATH = os.path.join(DATA_DIR, "incidents.csv")
SURVEY_PATH = os.path.join(DATA_DIR, "survey.csv")
CRIME = crime.load()  # data/crime/districts.json from python crime.py, or None
GTFS_DIR = os.path.join(DATA_DIR, "gtfs")
STATIC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
TRIP_TTL = 12 * 3600
MAX_TRIPS = 20000
PER_ADDRESS_HOUR = {"trip": 60, "report": 60}  # one network address can be many phones (mobile carriers share them)
lock = threading.Lock()
# shortcut: live trips live in memory and vanish on restart, move to Redis when running more than one server
trips = {}
weather = {}  # rounded (lat, lon) -> {"rain", "checked"}
geocode_cache = OrderedDict()
# shortcut: limits live in memory and reset on restart; fine for one server process
recent = defaultdict(list)  # (kind, address) -> request times in the last hour


def allow(kind, address):
    """True while this address made fewer than PER_ADDRESS_HOUR[kind] such requests in the last hour."""
    now = time.time()
    with lock:
        times = recent[(kind, address)] = [t for t in recent[(kind, address)] if now - t < 3600]
        if len(times) >= PER_ADDRESS_HOUR[kind]:
            return False
        times.append(now)
        return True


def load_csv(path, types=None, **extra):
    """Rows of lat,lon,type,when (ISO date-time), e.g. incidents from police open data, or a street survey."""
    if not os.path.exists(path):
        return []
    out = []
    with open(path, newline="", encoding="utf-8-sig") as f:
        for n, row in enumerate(csv.DictReader(f), 2):
            try:
                when = datetime.fromisoformat(row["when"])
                kind = row["type"].strip().lower()
                if types and kind not in types:
                    raise ValueError(f"unknown type {kind!r}")
                out.append({"lat": float(row["lat"]), "lon": float(row["lon"]), "type": kind,
                            "hour": when.hour, "weekday": when.weekday(), "ts": when.timestamp(), **extra})
            except (KeyError, ValueError, TypeError, AttributeError) as e:  # a missing column reads as None
                print(f"{os.path.basename(path)} line {n} skipped: {e}")
    return out


os.makedirs(DATA_DIR, exist_ok=True)
TOKEN_PATH = os.path.join(DATA_DIR, "mapillary_token.txt")  # data/ is gitignored, so the token never reaches git
if not os.environ.get("MAPILLARY_TOKEN") and os.path.exists(TOKEN_PATH):
    os.environ["MAPILLARY_TOKEN"] = open(TOKEN_PATH).read().strip()
EXTRAS = {"nightlights": nightlights}
if os.environ.get("MAPILLARY_TOKEN"):
    EXTRAS["mapillary"] = mapillary
GEOAPIFY_PATH = os.path.join(DATA_DIR, "geoapify_key.txt")  # free key from myprojects.geoapify.com; gitignored like the Mapillary token
if not os.environ.get("GEOAPIFY_KEY") and os.path.exists(GEOAPIFY_PATH):
    os.environ["GEOAPIFY_KEY"] = open(GEOAPIFY_PATH).read().strip()
if os.environ.get("GEOAPIFY_KEY"):
    EXTRAS["geoapify"] = geoapify
if importlib.util.find_spec("pyarrow"):
    EXTRAS.update(places=overture_places, buildings=overture_buildings)
    threading.Thread(target=warm_overture, daemon=True).start()
print("Overture streets, places and buildings:", "on" if "places" in EXTRAS else "off (pip install pyarrow), using slower Overpass for streets", flush=True)
print("Mapillary lights and cameras:", "on" if os.environ.get("MAPILLARY_TOKEN") else "off (no token)", flush=True)
print("Geoapify places:", "on" if os.environ.get("GEOAPIFY_KEY") else "off (no key in data/geoapify_key.txt)", flush=True)
SURVEY = load_csv(SURVEY_PATH, REPORT_TYPES, source="Street survey")
STOPS = gtfs.load(GTFS_DIR)
print(f"Street survey: {len(SURVEY)} observations. Timetables: {len(STOPS)} stops (put GTFS zips in data/gtfs).", flush=True)
REPORTS = Reports(os.path.join(DATA_DIR, "reports.db"))
store = TileStore(os.path.join(DATA_DIR, "tiles"), reports=REPORTS.all() + SURVEY, incidents=load_csv(INCIDENTS_PATH),
                  extras=EXTRAS, background={"buildings", "mapillary", "geoapify"}, roads=overture_roads if "places" in EXTRAS else None,
                  stops=STOPS, layers=LAYERS_DIR)
print("OpenStreetMap import:", f"{len(store.extracted)} tiles from {store.imported}" if store.extracted else "none yet (python import_osm.py makes OSM detail instant)", flush=True)


def reimport():
    """Re-run the OpenStreetMap import in the background when it is a month old, so new edits reach the app."""
    if not store.imported or not importlib.util.find_spec("osmium"):
        return
    if (datetime.now() - datetime.fromisoformat(store.imported)).days < REIMPORT_DAYS:
        return
    print("OpenStreetMap import is a month old, refreshing it in the background", flush=True)
    done = subprocess.run([sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "import_osm.py"), "--refresh"])
    if done.returncode == 0:
        store.load_extract()
    else:
        print("OpenStreetMap refresh failed; the old import stays in use", flush=True)


def reimport_daily():
    while True:
        reimport()
        time.sleep(86400)


threading.Thread(target=reimport_daily, daemon=True).start()


def latlon(s):
    lat, lon = (float(v) for v in s.split(",")) if isinstance(s, str) else (float(s[0]), float(s[1]))
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        raise ValueError("bad coordinates")
    return lat, lon


def is_raining(pt, when):
    """Live rain from Open-Meteo (free, no key), refreshed every 15 min; only applies to trips starting about now."""
    if abs((when.astimezone() - datetime.now().astimezone()).total_seconds()) > 3600:
        return False
    w = weather.setdefault((round(pt[0], 1), round(pt[1], 1)), {"rain": False, "checked": 0.0})
    if time.time() - w["checked"] > 900:
        w["checked"] = time.time()
        try:
            q = urlencode({"latitude": pt[0], "longitude": pt[1], "current": "precipitation"})
            with urlopen(f"https://api.open-meteo.com/v1/forecast?{q}", timeout=3) as r:
                w["rain"] = json.load(r)["current"]["precipitation"] > 0.2
        except Exception:
            w["rain"] = False  # no network: score without weather rather than fail
    return w["rain"]


def geocode(q, near):
    """Place search: the offline OpenStreetMap index first, then Photon (komoot, free) for anything it lacks."""
    key = (q.lower(), near and (round(near[0], 1), round(near[1], 1)))
    if key in geocode_cache:
        return geocode_cache[key]
    local = offline_search(SEARCH_DB, q, near) if os.path.exists(SEARCH_DB) else []
    if len(local) >= 5:
        return local
    try:
        online = photon(q, near)
    except OSError:
        if local:
            return local  # offline: what the index has is better than an error
        raise
    seen = {(p["name"].lower(), round(p["pt"][0], 3), round(p["pt"][1], 3)) for p in local}
    out = (local + [p for p in online if (p["name"].lower(), round(p["pt"][0], 3), round(p["pt"][1], 3)) not in seen])[:8]
    geocode_cache[key] = out
    if len(geocode_cache) > 1000:
        geocode_cache.popitem(last=False)
    return out


def photon(q, near):
    params = {"q": q, "limit": 8}
    if near:
        params.update(lat=near[0], lon=near[1])
    with urlopen(Request("https://photon.komoot.io/api/?" + urlencode(params), headers={"User-Agent": "SafeRoute/1.0"}), timeout=6) as r:
        features = json.load(r)["features"]
    out = []
    for f in features:
        p, (lon, lat) = f["properties"], f["geometry"]["coordinates"]
        name = p.get("name") or " ".join(x for x in (p.get("housenumber"), p.get("street")) if x) or p.get("city")
        where = ", ".join(x for x in (p.get("street") if p.get("name") else None, p.get("district") or p.get("locality"), p.get("city"), p.get("state")) if x)
        if name:
            out.append({"name": name, "pt": [lat, lon], "kind": where or p.get("osm_value", "place").replace("_", " ")})
    return out


def expire_trips():
    now = time.time()
    for k in [k for k, t in trips.items() if now - t["updated"] > TRIP_TTL]:
        del trips[k]


class Handler(SimpleHTTPRequestHandler):
    # HTTP/1.1 ends each reply by its length rather than by closing the connection: filters such as AdGuard cut
    # HTTP/1.0 replies on localhost short, which left the page loading forever
    protocol_version = "HTTP/1.1"
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
            self.close_connection = True  # the unread body would otherwise be taken for the next request
            raise ValueError("too large")
        body = json.loads(self.rfile.read(n) or b"{}")
        if not isinstance(body, dict):
            raise ValueError("expected an object")
        return body

    def address(self):
        """The caller's address. Behind a tunnel every request comes from this machine, and the tunnel adds the real one last."""
        forwarded = self.headers.get("X-Forwarded-For")
        ip = self.client_address[0].removeprefix("::ffff:")  # IPv4 callers of the dual-stack socket look like ::ffff:1.2.3.4
        if forwarded and ip in ("127.0.0.1", "::1"):
            return forwarded.split(",")[-1].strip()
        return ip

    def when(self, q):
        return datetime.fromisoformat(q["at"][0]) if q.get("at", [""])[0] else datetime.now()

    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        try:
            if u.path == "/api/search":
                text = q.get("q", [""])[0].strip()[:80]
                return self.send_json(geocode(text, latlon(q["near"][0]) if q.get("near") else None) if len(text) >= 2 else [])
            if u.path == "/api/heat":
                s, w, n, e = (float(v) for v in q["bbox"][0].split(","))
                if dist((s, w), (n, e)) > 6000:
                    return self.send_json({"error": "Zoom in to see the safety map"}, 400)
                when, mid = self.when(q), ((s + n) / 2, (w + e) / 2)
                area = store.for_trip((s, w), (n, e))
                return self.send_json(area.heat(when, is_raining(mid, when), (s, w, n, e)))
            if u.path == "/api/explain":
                pt = latlon(q["at_pt"][0])
                when = self.when(q)
                return self.send_json(store.around(pt, 300).explain(pt, when, is_raining(pt, when)))
            if u.path == "/api/havens":
                pt = latlon(q["near"][0])
                return self.send_json(store.around(pt).havens_near(pt, self.when(q)))
            if u.path == "/api/route":
                a, b = latlon(q["from"][0]), latlon(q["to"][0])
                when = self.when(q)
                rain = is_raining(a, when)
                mode = q.get("mode", ["walk"])[0]
                if mode not in MODES:
                    raise ValueError("mode must be walk, bike or car")
                extra = int(q["extra"][0]) if q.get("extra", [""])[0] else None  # longest detour to consider, in minutes
                return self.send_json({"routes": store.for_trip(a, b).routes(a, b, when, rain, mode, extra), "rain": rain,
                                       "area": crime.around(CRIME, [a, b])})
            if u.path.startswith("/api/trip/"):
                t = trips.get(u.path.rsplit("/", 1)[1])
                if not t:
                    return self.send_json({"error": "This trip has ended or the link is wrong."}, 404)
                return self.send_json({k: t[k] for k in ("name", "dest", "eta", "points", "started", "updated", "ended")})
        except TooFar as e:
            return self.send_json({"error": str(e)}, 400)
        except ConnectionError as e:
            return self.send_json({"error": str(e)}, 503)
        except OSError:
            return self.send_json({"error": "Couldn't reach the map service, check your connection and try again"}, 503)
        except (KeyError, ValueError) as e:
            msg = str(e) if "streets" in str(e) else f"bad request: {e}"
            return self.send_json({"error": msg}, 400)
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
        lat, lon = latlon((body["lat"], body["lon"]))
        if body["type"] not in REPORT_TYPES:
            raise ValueError("unknown report type")
        # the walker's own clock, so "deserted at 22:00" stays 22:00 on a server in another time zone
        seen = datetime.fromisoformat(body["at"]) if body.get("at") else datetime.now()
        if abs(seen.timestamp() - time.time()) > 3600:
            raise ValueError("report time is not now")
        address = self.address()
        r = allow("report", address) and REPORTS.add(lat, lon, body["type"], str(body.get("device") or address)[:64], seen)
        if not r:
            return self.send_json({"error": "Thanks! You've sent a lot of reports this hour, please try again later."}, 429)
        store.add_report(r)
        self.send_json({"ok": True})

    def start_trip(self, body):
        dest = latlon(body["dest"])
        if not allow("trip", self.address()):
            return self.send_json({"error": "Too many live trips from this network, try again shortly."}, 429)
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


class Server(ThreadingHTTPServer):
    def handle_error(self, request, client_address):
        if not isinstance(sys.exc_info()[1], (ConnectionResetError, BrokenPipeError, ConnectionAbortedError)):
            super().handle_error(request, client_address)  # a browser dropping an idle connection is normal


class DualStackServer(Server):
    """Listens on IPv6 and IPv4 together, since browsers try localhost as ::1 first."""
    address_family = socket.AF_INET6

    def server_bind(self):
        self.socket.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
        super().server_bind()


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8000
    print(f"SafeRoute running, open http://localhost:{port}", flush=True)
    try:
        server = DualStackServer(("::", port), Handler)
    except OSError:  # no IPv6 on this machine
        server = Server(("", port), Handler)
    server.serve_forever()
