"""Website, app and JSON API. Usage: python server.py [port]"""
import csv
import importlib.util
import ipaddress
import json
import os
import secrets
import sys
import threading
import time
import traceback
from collections import OrderedDict
from datetime import datetime
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlencode, urlparse
from urllib.request import Request, urlopen

from safety import REPORT_TYPES, dist
from tiles import TileStore, TooFar, mapillary, overture_buildings, overture_places, overture_roads, warm_overture

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
REPORTS_PATH = os.path.join(DATA_DIR, "reports.json")
INCIDENTS_PATH = os.path.join(DATA_DIR, "incidents.csv")
STATIC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
TRIP_TTL = 12 * 3600  # a live trip is wiped 12 hours after it started, whatever happens
ENDED_TTL = 30 * 60  # an ended trip keeps only "ended" (no locations) so friends see it finished
MAX_TRIPS = 2000
# requests allowed per client in a window of seconds, so one script can't flood reports or fill the trip table
LIMITS = {"report": (10, 3600), "trip": (10, 3600), "update": (120, 600), "read": (300, 600)}
PUBLIC = os.environ.get("SAFEROUTE_PUBLIC") == "1"  # serve clients outside the home network directly (only behind HTTPS)
lock = threading.Lock()
# shortcut: live trips live in memory and vanish on restart, move to Redis when running more than one server
trips = {}
hits = {}  # (limit name, client) -> recent request times, kept in memory only
weather = {}  # rounded (lat, lon) -> {"rain", "checked"}
geocode_cache = OrderedDict()


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


os.makedirs(DATA_DIR, exist_ok=True)
TOKEN_PATH = os.path.join(DATA_DIR, "mapillary_token.txt")  # data/ is gitignored, so the token never reaches git
if not os.environ.get("MAPILLARY_TOKEN") and os.path.exists(TOKEN_PATH):
    os.environ["MAPILLARY_TOKEN"] = open(TOKEN_PATH).read().strip()
EXTRAS = {"mapillary": mapillary} if os.environ.get("MAPILLARY_TOKEN") else {}
if importlib.util.find_spec("pyarrow"):
    EXTRAS.update(places=overture_places, buildings=overture_buildings)
    threading.Thread(target=warm_overture, daemon=True).start()
print("Overture streets, places and buildings:", "on" if "places" in EXTRAS else "off (pip install pyarrow), using slower Overpass for streets", flush=True)
print("Mapillary lights and cameras:", "on" if os.environ.get("MAPILLARY_TOKEN") else "off (no token)", flush=True)
store = TileStore(os.path.join(DATA_DIR, "tiles"),
                  reports=json.load(open(REPORTS_PATH)) if os.path.exists(REPORTS_PATH) else [],
                  incidents=load_incidents(),
                  extras=EXTRAS, background={"buildings"}, roads=overture_roads if "places" in EXTRAS else None)


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
            q = urlencode({"latitude": round(pt[0], 1), "longitude": round(pt[1], 1), "current": "precipitation"})  # ~10 km, not the walker's spot
            with urlopen(f"https://api.open-meteo.com/v1/forecast?{q}", timeout=3) as r:
                w["rain"] = json.load(r)["current"]["precipitation"] > 0.2
        except Exception:
            w["rain"] = False  # no network: score without weather rather than fail
    return w["rain"]


def geocode(q, near):
    """Place search anywhere via Photon (OpenStreetMap-based, free), biased towards where the map is."""
    key = (q.lower(), near and (round(near[0], 1), round(near[1], 1)))
    if key in geocode_cache:
        return geocode_cache[key]
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
    geocode_cache[key] = out
    if len(geocode_cache) > 1000:
        geocode_cache.popitem(last=False)
    return out


def expire_trips():
    now = time.time()
    for k in [k for k, t in trips.items() if now - t["started"] > TRIP_TTL or t["ended"] and now - t["updated"] > ENDED_TTL]:
        del trips[k]


def end_trip(t):
    """Delete every location of a trip as soon as it ends; only the name and "ended" stay for the viewer."""
    t.update(points=[], dest=None, eta=0, ended=True, updated=time.time())


def allowed(kind, client):
    """Sliding-window rate limit; drops clients whose window has emptied so the table can't grow forever."""
    limit, window = LIMITS[kind]
    now = time.time()
    with lock:
        for k in [k for k, v in hits.items() if now - v[-1] > window and k[0] == kind]:
            del hits[k]
        recent = [t for t in hits.get((kind, client), []) if now - t < window]
        if len(recent) >= limit:
            return False
        hits[(kind, client)] = recent + [now]
        return True


def redact(path):
    """Log lines keep the endpoint only: no coordinates, search text or trip ids."""
    path = urlparse(path).path
    return "/api/trip/<id>" if path.startswith("/api/trip/") else path


class Handler(SimpleHTTPRequestHandler):
    timeout = 30  # a client that stops sending can't hold a server thread for ever
    extensions_map = {**SimpleHTTPRequestHandler.extensions_map, ".webmanifest": "application/manifest+json", ".js": "text/javascript"}

    def __init__(self, *a, **kw):
        super().__init__(*a, directory=STATIC, **kw)

    def end_headers(self):
        if self.path.startswith("/api/"):
            self.send_header("Cache-Control", "no-store")
        self.send_header("Referrer-Policy", "no-referrer")  # the viewer URL holds the trip id
        self.send_header("X-Content-Type-Options", "nosniff")
        super().end_headers()

    def log_request(self, code="-", size="-"):
        self.log_message("%s %s %s", self.command, redact(self.path), code)

    def log_message(self, fmt, *args):
        sys.stderr.write(f"{self.log_date_time_string()} {fmt % args}\n")  # no client address

    def list_directory(self, path):
        self.send_error(404)

    def client(self):
        """Who is asking: behind a local tunnel or proxy every request comes from 127.0.0.1, so use its header."""
        ip = self.client_address[0]
        if ip in ("127.0.0.1", "::1", "::ffff:127.0.0.1"):
            fwd = self.headers.get("Cf-Connecting-IP") or self.headers.get("X-Forwarded-For", "").split(",")[0].strip()
            return fwd or ip
        return ip

    def private_client(self):
        a = ipaddress.ip_address(self.client_address[0].split("%")[0])
        return (getattr(a, "ipv4_mapped", None) or a).is_private

    def parse_request(self):
        if not super().parse_request():
            return False
        if PUBLIC or self.private_client():
            return True
        self.send_error(403, "SafeRoute only answers this network. Put it behind HTTPS and set SAFEROUTE_PUBLIC=1 to open it up.")
        return False

    def send_json(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def read_json(self):
        n = int(self.headers.get("Content-Length", 0))
        if not 0 <= n <= 10_000:
            raise ValueError("bad length")
        body = json.loads(self.rfile.read(n) or b"{}")
        if not isinstance(body, dict):
            raise ValueError("expected an object")
        return body

    def when(self, q):
        return datetime.fromisoformat(q["at"][0]) if q.get("at", [""])[0] else datetime.now()

    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        if u.path.startswith("/api/") and not allowed("read", self.client()):
            return self.send_json({"error": "Too many requests, please wait a minute."}, 429)
        try:
            if u.path == "/api/search":
                text = q.get("q", [""])[0].strip()[:80]
                return self.send_json(geocode(text, latlon(q["near"][0]) if q.get("near") else None) if len(text) >= 2 else [])
            if u.path == "/api/heat":
                s, w, n, e = (float(v) for v in q["bbox"][0].split(","))
                latlon((s, w)), latlon((n, e))
                if not (s < n and w < e):
                    raise ValueError("bbox must be south,west,north,east")
                if dist((s, w), (n, e)) > 6000:
                    return self.send_json({"error": "Zoom in to see the safety map"}, 400)
                when, mid = self.when(q), ((s + n) / 2, (w + e) / 2)
                area = store.for_trip((s, w), (n, e))
                return self.send_json(area.heat(when, is_raining(mid, when), (s, w, n, e)))
            if u.path == "/api/havens":
                pt = latlon(q["near"][0])
                return self.send_json(store.around(pt).havens_near(pt, self.when(q)))
            if u.path == "/api/route":
                a, b = latlon(q["from"][0]), latlon(q["to"][0])
                when = self.when(q)
                rain = is_raining(a, when)
                return self.send_json({"routes": store.for_trip(a, b).routes(a, b, when, rain), "rain": rain})
            if u.path.startswith("/api/trip/"):
                with lock:
                    expire_trips()
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
        except Exception:
            return self.server_error()
        super().do_GET()

    def server_error(self):
        """A bug must still answer with JSON, or the app waits for its 90 s timeout."""
        traceback.print_exc()
        self.send_json({"error": "Something went wrong on our side, please try again."}, 500)

    def do_POST(self):
        path = urlparse(self.path).path
        try:
            body = self.read_json()
            kind = "report" if path == "/api/report" else "trip" if path == "/api/trip" else "update"
            if not allowed(kind, self.client()):
                return self.send_json({"error": "Too many requests from this phone, please try again later."}, 429)
            if path == "/api/report":
                return self.report(body)
            if path == "/api/trip":
                return self.start_trip(body)
            if path.startswith("/api/trip/"):
                return self.update_trip(path.rsplit("/", 1)[1], body)
        except (KeyError, ValueError, TypeError) as e:
            return self.send_json({"error": f"bad request: {e}"}, 400)
        except Exception:
            return self.server_error()
        self.send_json({"error": "not found"}, 404)

    def report(self, body):
        r = {"lat": float(body["lat"]), "lon": float(body["lon"]), "type": body["type"], "ts": time.time()}
        latlon((r["lat"], r["lon"]))
        if r["type"] not in REPORT_TYPES:
            raise ValueError("unknown report type")
        with lock:
            store.add_report(r)
            with open(REPORTS_PATH + ".tmp", "w") as f:
                json.dump(store.reports, f)
            os.replace(REPORTS_PATH + ".tmp", REPORTS_PATH)  # never leave a half-written file
        self.send_json({"ok": True})

    def start_trip(self, body):
        dest = latlon(body["dest"])
        eta = float(body.get("eta", 0))
        if not 0 <= eta <= 24 * 60:
            raise ValueError("bad eta")
        with lock:
            expire_trips()
            if len(trips) >= MAX_TRIPS:
                return self.send_json({"error": "Too many live trips right now, try again shortly."}, 503)
            tid, key = secrets.token_urlsafe(9), secrets.token_urlsafe(18)
            now = time.time()
            trips[tid] = {"key": key, "name": str(body.get("name", ""))[:40], "dest": dest,
                          "eta": eta, "points": [], "started": now, "updated": now, "ended": False}
        self.send_json({"id": tid, "key": key})

    def update_trip(self, tid, body):
        t = trips.get(tid)
        # the viewer link only carries the id; only the walker holds the key that can move the dot
        if not t or t["ended"] or not secrets.compare_digest(str(body.get("key", "")), t["key"]):
            return self.send_json({"error": "unknown trip"}, 404)
        with lock:
            if body.get("ended"):
                end_trip(t)
            elif "lat" in body:
                t["points"] = (t["points"] + [latlon((body["lat"], body["lon"])) + (time.time(),)])[-300:]
                t["updated"] = time.time()
        self.send_json({"ok": True})


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8000
    print(f"SafeRoute running, open http://localhost:{port}", flush=True)
    ThreadingHTTPServer(("", port), Handler).serve_forever()
