"""Website, app and JSON API. Usage: python server.py [port]"""
import csv
import functools
import importlib.util
import ipaddress
import json
import math
import os
import re
import secrets
import socket
import subprocess
import sys
import threading
import time
import traceback
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlencode, urlparse
from urllib.request import Request, urlopen

import buses
import crime
import gemini
import gtfs
import traffic
from import_osm import search as offline_search
from rating import REPORT_TYPES
from reports import Reports
from safety import MODES, SIGNALS, dist, seg_dist
from tiles import (TileStore, TooFar, geoapify, mapillary, nightlights, overture_buildings, overture_places, overture_roads, tiles_near,
                   warm_overture)

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
LAYERS_DIR = os.path.join(DATA_DIR, "layers")
SEARCH_DB = os.path.join(DATA_DIR, "tiles", "search.db")
REIMPORT_DAYS = 30
INCIDENTS_PATH = os.path.join(DATA_DIR, "incidents.csv")
SURVEY_PATH = os.path.join(DATA_DIR, "survey.csv")
CRIME = crime.load()  # data/crime/districts.json from python crime.py, or None
GTFS_DIR = os.path.join(DATA_DIR, "gtfs")
STATIC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
TRIP_TTL = 12 * 3600  # a live trip is wiped 12 hours after it started, whatever happens
ENDED_TTL = 30 * 60  # an ended trip keeps only "ended" (no locations) so friends see it finished
MAX_TRIPS = 20000
# requests allowed per network address in a window of seconds, so one script can't flood reports or fill the trip table.
# One address can be many phones (mobile carriers share them), so reports are also limited per device in reports.py.
LIMITS = {"report": (60, 3600), "trip": (60, 3600), "update": (120, 600), "read": (300, 600)}
# map-building requests running at once; the rest wait, so a burst of users can't run a small server out of memory
BUSY = threading.BoundedSemaphore(6)
HEAVY = ("/api/heat", "/api/explain", "/api/havens", "/api/route")
PUBLIC = os.environ.get("SAFEROUTE_PUBLIC") == "1"  # serve clients outside the home network directly (only behind HTTPS)
lock = threading.Lock()
# shortcut: live trips and limits live in memory and vanish on restart, move to Redis when running more than one server
trips = {}
hits = {}  # (limit name, address) -> recent request times
weather = {}  # rounded (lat, lon) -> {"rain", "checked"}
geocode_cache = OrderedDict()


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
GEOAPIFY_KEY = os.environ.get("GEOAPIFY_KEY", "")
if GEOAPIFY_KEY:
    EXTRAS["geoapify"] = geoapify
if importlib.util.find_spec("pyarrow"):
    EXTRAS.update(places=overture_places, buildings=overture_buildings)
    threading.Thread(target=warm_overture, daemon=True).start()
print("Overture streets, places and buildings:", "on" if "places" in EXTRAS else "off (pip install pyarrow), using slower Overpass for streets", flush=True)
print("Mapillary lights and cameras:", "on" if os.environ.get("MAPILLARY_TOKEN") else "off (no token)", flush=True)
print("Geoapify places:", "on" if os.environ.get("GEOAPIFY_KEY") else "off (no key in data/geoapify_key.txt)", flush=True)
def key_file(name, env):
    """A free API key from the environment or data/<name> (gitignored), or ""."""
    path = os.path.join(DATA_DIR, name)
    return os.environ.get(env) or (open(path).read().strip() if os.path.exists(path) else "")


buses.LIVE.key = key_file("otd_key.txt", "OTD_KEY")  # free key from otd.delhi.gov.in
if buses.LIVE.key:
    threading.Thread(target=buses.LIVE.run, daemon=True).start()
traffic.LIVE.key = key_file("tomtom_key.txt", "TOMTOM_KEY")  # free key from developer.tomtom.com
print("Live buses:", "on (counts start after 30 min)" if buses.LIVE.key else "off (no key in data/otd_key.txt)", flush=True)
print("Live traffic:", "on" if traffic.LIVE.key else "off (no key in data/tomtom_key.txt)", flush=True)
gemini.LIVE = gemini.Gemini(key_file("gemini_key.txt", "GEMINI_KEY"), os.path.join(DATA_DIR, "gemini_hours.json"))
SIGNALS.append(gemini.signal)
print("Gemini shop hours and live events:", "on" if gemini.LIVE.key else "off (no key in data/gemini_key.txt)", flush=True)
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
    if not (-85 <= lat <= 85 and math.isfinite(lon)):  # near the poles a 500 m circle spans thousands of map tiles
        raise ValueError("bad coordinates")
    return lat, lon if -180 <= lon <= 180 else (lon + 180) % 360 - 180  # a map panned round the world sends 77.4 as -642.6


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


# words people type for a kind of place, as the OpenStreetMap tag Photon can search around the user for
CATEGORIES = {"metro": "railway:station", "station": "railway:station", "hospital": "amenity:hospital", "atm": "amenity:atm",
              "pharmacy": "amenity:pharmacy", "chemist": "amenity:pharmacy", "police": "amenity:police", "petrol": "amenity:fuel",
              "bus stop": "highway:bus_stop", "toilet": "amenity:toilets", "bank": "amenity:bank", "hotel": "tourism:hotel"}


def photon_get(path, params):
    with urlopen(Request(f"https://photon.komoot.io/{path}?" + urlencode(params), headers={"User-Agent": "SafeRoute/1.0"}), timeout=6) as r:
        features = json.load(r)["features"]
    out = []
    for f in features:
        p, (lon, lat) = f["properties"], f["geometry"]["coordinates"]
        name = p.get("name") or " ".join(x for x in (p.get("housenumber"), p.get("street")) if x) or p.get("city")
        where = ", ".join(x for x in (p.get("street") if p.get("name") else None, p.get("district") or p.get("locality"), p.get("city"), p.get("state")) if x)
        if name:
            out.append({"name": name, "pt": [lat, lon], "kind": where or p.get("osm_value", "place").replace("_", " "),
                        "tag": f"{p.get('osm_key')}:{p.get('osm_value')}", "big": p.get("type") in ("city", "county", "state", "country")})
    return out


def photon(q, near):
    """Photon (OpenStreetMap, free, no key). A low location_bias_scale ranks nearby places above famous far ones."""
    params = {"q": q, "limit": 10, "lang": "en"}
    if near:
        params.update(lat=near[0], lon=near[1], zoom=14, location_bias_scale=0.1)
    out = photon_get("api/", params)
    tag = CATEGORIES.get(q.lower().rstrip("s"))
    if tag and near:  # "metro" means stations near me, whose names don't contain "metro"
        try:
            hits = photon_get("reverse", {"lat": near[0], "lon": near[1], "radius": 5, "limit": 10, "osm_tag": tag, "lang": "en"})
            out += [dict(r, cat=True) for r in hits if r["tag"] == tag]  # the tag check drops anything the filter let through
        except (OSError, ValueError, KeyError) as err:  # keep the name matches
            print(f"search: photon nearby {tag} failed ({type(err).__name__})", flush=True)
    return out


def geoapify_search(q, near):
    """Geoapify autocomplete (OSM plus OpenAddresses and Who's On First; free tier 3000 requests a day, needs a key)."""
    params = {"text": q, "limit": 8, "format": "json", "lang": "en", "apiKey": GEOAPIFY_KEY}
    if near:
        params["bias"] = f"proximity:{near[1]},{near[0]}"
    with urlopen(Request("https://api.geoapify.com/v1/geocode/autocomplete?" + urlencode(params), headers={"User-Agent": "SafeRoute/1.0"}), timeout=6) as r:
        results = json.load(r)["results"]
    out = [{"name": p.get("name") or p.get("address_line1") or p["formatted"], "pt": [p["lat"], p["lon"]],
            "kind": p.get("address_line2") or p.get("result_type", "place")} for p in results]
    # it matches names only and its bias is weak: "hospital" from Greater Noida returns towns in Uganda, so keep it local
    return [r for r in out if not near or dist(near, r["pt"]) < 100000]


def nearby_places(q, near, radius_m=5000):
    """Overture businesses already downloaded for trips near the user, by name or category ("atm", "pizza", "hospital")."""
    words = q.lower().split()
    out = []
    for t in tiles_near(near, near, radius_m):
        path = store._path(t, "places")
        if not os.path.exists(path):
            continue
        with open(path) as f:
            for el in json.load(f)["elements"]:
                name, kind = el["tags"].get("name", ""), el["tags"]["overture"]
                have = name.lower().split() + kind.split("_")
                if name and all(any(h.startswith(w) for h in have) for w in words):
                    out.append({"name": name, "pt": [el["lat"], el["lon"]], "kind": kind,
                                "cat": all(any(k.startswith(w) for k in kind.split("_")) for w in words)})
    return out


def geocode(q, near):
    """Place search anywhere, from the offline index and every free source at once, with places near the user first."""
    key = (q.lower(), near and (round(near[0], 2), round(near[1], 2)))
    hit = geocode_cache.get(key)  # another thread may drop the key between a check and a read
    if hit is not None:
        return hit
    sources = [photon] + ([geoapify_search] if GEOAPIFY_KEY else [])
    with ThreadPoolExecutor(len(sources)) as ex:
        jobs = [ex.submit(fetch, q, near) for fetch in sources]
    found, ok = [], False
    for fetch, job in zip(sources, jobs):
        try:
            found += job.result()
            ok = True
        except (OSError, ValueError, KeyError) as err:  # one source down must not empty the list
            print(f"search: {fetch.__name__} failed ({type(err).__name__})", flush=True)
    if near:
        found += nearby_places(q, near)
    if os.path.exists(SEARCH_DB):  # the offline OpenStreetMap index answers even with no internet
        found = offline_search(SEARCH_DB, q, near) + found
        ok = True
    out = rank(q, near, found)
    if ok:  # don't remember an answer made while the internet was down
        geocode_cache[key] = out
        if len(geocode_cache) > 1000:
            geocode_cache.popitem(last=False)
    return out


def rank(q, near, found, limit=8):
    """Merge sources: drop repeats (same name within 200 m), then rank by how well the name matches and how near it is."""
    q = q.lower()
    out = []
    for r in found:
        if near:
            r["km"] = round(dist(near, r["pt"]) / 1000, 1)
        if not any(r["name"].lower() == o["name"].lower() and dist(r["pt"], o["pt"]) < 200 for o in out):
            out.append(r)
    match = lambda r: 0 if r.get("cat") or r["name"].lower().startswith(q) else 1 if any(w.startswith(q.split()[0]) for w in r["name"].lower().split()) else 2
    # each 10x further away costs as much as a worse name match, so a "Hospital" town 5000 km off loses to Sharda Hospital 3 km away
    out.sort(key=lambda r: match(r) + math.log10(r.get("km", 0) + 1))
    # a city or state Photon ranks first ("mumbai") stays even when nearby shops with that word fill the list
    top = found[0] if found and found[0].get("big") else None
    if top and top in out and top not in out[:limit]:
        return out[:limit - 1] + [top]
    return out[:limit]


@functools.lru_cache(512)
def place_name(pt):
    """"Connaught Place, New Delhi" style name for a point (rounded to ~100 m), for Gemini prompts."""
    try:
        hit = photon_get("reverse", {"lat": pt[0], "lon": pt[1], "limit": 1, "lang": "en"})
        return f"{hit[0]['name']}, {hit[0]['kind']}" if hit else f"{pt[0]:.4f},{pt[1]:.4f}"
    except (OSError, ValueError, KeyError):
        return f"{pt[0]:.4f},{pt[1]:.4f}"


def ai_extras(area, stops, when):
    """Gemini opening hours for places along the trip (start, stops, end) and live event alerts near it; [] without a key."""
    if not gemini.LIVE.key:
        return []
    a, b = stops[0], stops[-1]
    where = f"{place_name((round(a[0], 3), round(a[1], 3)))} to {place_name((round(b[0], 3), round(b[1], 3)))}"
    legs = list(zip(stops, stops[1:]))
    near = lambda p, m: any(seg_dist(p, x, y) <= m for x, y in legs)

    def locate(q):
        hit = next((h for h in geocode(q, ((a[0] + b[0]) / 2, (a[1] + b[1]) / 2)) if not h.get("big")), None)
        return hit["pt"] if hit and near(hit["pt"], 3000) else None  # far away means it matched the wrong place

    with ThreadPoolExecutor(2) as ex:
        jobs = [ex.submit(gemini.LIVE.fill_hours, area, lambda p: near(p, 300), where),
                ex.submit(gemini.LIVE.find_events, a, b, when, where, locate)]
    try:
        jobs[0].result()
        return jobs[1].result()
    except Exception:  # a bonus: a bug here must not cost the trip
        traceback.print_exc()
        return []


def expire_trips():
    now = time.time()
    for k in [k for k, t in trips.items() if now - t["started"] > TRIP_TTL or t["ended"] and now - t["updated"] > ENDED_TTL]:
        del trips[k]


def keeps_links(text, out):
    """A reworded alert that dropped or changed a location link is worse than the plain one."""
    return bool(out) and all(u in out for u in re.findall(r"https?://\S+", text))


def reword_sos(text):
    """Gemini puts the facts a paramedic needs first. It only rewords; the user still presses Send."""
    if not gemini.LIVE.key:
        return text
    prompt = ("Rewrite this emergency text message for family and ambulance responders. Put allergies and conditions that "
              "change treatment first. Keep every fact and every link exactly; add nothing. Plain text, under 400 characters.\n\n" + text)
    # no search grounding or thinking: an SOS can't wait, and the default thinking alone takes over 4 s
    body = {"contents": [{"parts": [{"text": prompt}]}], "generationConfig": {"temperature": 0, "thinkingConfig": {"thinkingBudget": 0}}}
    req = Request(gemini.URL, json.dumps(body).encode(), {"Content-Type": "application/json", "x-goog-api-key": gemini.LIVE.key})
    try:
        with urlopen(req, timeout=4) as r:
            out = json.load(r)["candidates"][0]["content"]["parts"][0]["text"].strip()
    except Exception:
        return text
    return out if keeps_links(text, out) else text


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
    # HTTP/1.1 ends each reply by its length rather than by closing the connection: filters such as AdGuard cut
    # HTTP/1.0 replies on localhost short, which left the page loading forever
    protocol_version = "HTTP/1.1"
    timeout = 30  # a client that stops sending can't hold a server thread for ever
    extensions_map = {**SimpleHTTPRequestHandler.extensions_map, ".webmanifest": "application/manifest+json", ".js": "text/javascript"}

    def __init__(self, *a, **kw):
        super().__init__(*a, directory=STATIC, **kw)

    def end_headers(self):
        if self.path.startswith("/api/"):
            self.send_header("Cache-Control", "no-store")
        # other sites only ever see "localhost:8001", never a page address with a trip id; OpenStreetMap's tile
        # servers refuse map pictures that come with no referrer at all
        self.send_header("Referrer-Policy", "strict-origin-when-cross-origin")
        self.send_header("X-Content-Type-Options", "nosniff")
        super().end_headers()

    def log_request(self, code="-", size="-"):
        self.log_message("%s %s %s", self.command, redact(self.path), code)

    def log_message(self, fmt, *args):
        sys.stderr.write(f"{self.log_date_time_string()} {fmt % args}\n")  # no client address

    def list_directory(self, path):
        self.send_error(404)

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
        if not 0 <= n <= 10_000 or "Transfer-Encoding" in self.headers:  # a chunked body isn't read, so refuse it
            self.close_connection = True  # the unread body would otherwise be taken for the next request
            raise ValueError("bad length")
        body = json.loads(self.rfile.read(n) or b"{}")
        if not isinstance(body, dict):
            raise ValueError("expected an object")
        return body

    def address(self):
        """The caller's address. Behind a tunnel or Caddy every request comes from this machine, and the proxy adds the real
        one last to X-Forwarded-For. Cf-Connecting-IP comes second: behind Caddy a caller could set it to anything."""
        forwarded = self.headers.get("X-Forwarded-For", "").split(",")[-1].strip() or self.headers.get("Cf-Connecting-IP")
        ip = self.client_address[0].removeprefix("::ffff:")  # IPv4 callers of the dual-stack socket look like ::ffff:1.2.3.4
        if forwarded and ip in ("127.0.0.1", "::1"):
            return forwarded
        return ip

    def when(self, q):
        return datetime.fromisoformat(q["at"][0]) if q.get("at", [""])[0] else datetime.now()

    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        if u.path.startswith("/api/") and not allowed("read", self.address()):
            return self.send_json({"error": "Too many requests, please wait a minute."}, 429)
        heavy = u.path in HEAVY
        if heavy and not BUSY.acquire(timeout=60):
            return self.send_json({"error": "SafeRoute is busy right now, please try again in a minute."}, 503)
        try:
            if u.path == "/api/search":
                text = q.get("q", [""])[0].strip()[:80]
                return self.send_json(geocode(text, latlon(q["near"][0]) if q.get("near") else None) if len(text) >= 2 else [])
            if u.path == "/api/heat":
                s, w, n, e = (float(v) for v in q["bbox"][0].split(","))
                (s, w), (n, e) = latlon((s, w)), latlon((n, e))
                if not (s < n and w < e):
                    raise ValueError("bbox must be south,west,north,east")
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
                via = [latlon(v) for v in q.get("via", [""])[0].split(";") if v][:5]  # stops, in order
                prefer = {k: min(3, max(0, int(w))) for k, _, w in (p.partition(":") for p in q.get("prefer", [""])[0].split(",") if p)}
                when = self.when(q)
                rain = is_raining(a, when)
                mode = q.get("mode", ["walk"])[0]
                if mode not in MODES:
                    raise ValueError("mode must be walk, bike or car")
                extra = max(0, int(q["extra"][0])) if q.get("extra", [""])[0] else None  # longest detour to consider, in minutes
                area = store.for_trip(a, b, via)
                traffic.LIVE.warm(traffic.sample(area, a, b) if traffic.is_now(when, time.time()) else [])
                alerts = ai_extras(area, [a, *via, b], when)
                return self.send_json({"routes": area.routes(a, b, when, rain, mode, extra, via, prefer), "rain": rain, "alerts": alerts,
                                       "area": crime.around(CRIME, [a, b]), "sources": area.sources})
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
        finally:
            if heavy:
                BUSY.release()
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
            if not allowed(kind, self.address()):
                return self.send_json({"error": "Too many requests from this network, please try again later."}, 429)
            if path == "/api/report":
                return self.report(body)
            if path == "/api/trip":
                return self.start_trip(body)
            if path.startswith("/api/trip/"):
                return self.update_trip(path.rsplit("/", 1)[1], body)
            if path == "/api/sos-text":
                return self.send_json({"text": reword_sos(str(body["text"])[:1000])})
        except (KeyError, ValueError, TypeError) as e:
            return self.send_json({"error": f"bad request: {e}"}, 400)
        except Exception:
            return self.server_error()
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
        r = REPORTS.add(lat, lon, body["type"], str(body.get("device") or address)[:64], seen)
        if not r:
            return self.send_json({"error": "Thanks! You've sent a lot of reports this hour, please try again later."}, 429)
        store.add_report(r)
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
