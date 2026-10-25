"""Fetch OpenStreetMap data on demand in fixed tiles, cache them on disk, and build scored areas for any trip."""
import json
import math
import os
import threading
import time
import urllib.parse
import urllib.request
from collections import OrderedDict, defaultdict
from concurrent.futures import ThreadPoolExecutor

from safety import Area, dist, seg_dist

TILE = 0.01  # degrees, about 1.1 km
CHUNK = 3  # missing tiles are fetched in blocks of up to 3x3, one Overpass request each
TILE_MAX_AGE = 7 * 86400
MAX_TRIP_M = 10000
MIRRORS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
]
QUERY = """
[out:json][timeout:25];
way["highway"~"^(trunk|primary|secondary|tertiary|primary_link|secondary_link|tertiary_link|unclassified|residential|living_street|pedestrian|service|footway|path|cycleway|steps|track)$"]["foot"!="no"]["access"!~"^(private|no)$"]({bbox});
out body geom qt;
(
  way["landuse"~"^(residential|commercial|retail|construction|brownfield|greenfield|cemetery|industrial|farmland|quarry|railway)$"]({bbox});
  way["natural"~"^(wood|scrub)$"]({bbox});
  way["leisure"="park"]({bbox});
  way["railway"="rail"]({bbox});
);
out tags geom;
(
  node["highway"~"^(street_lamp|bus_stop)$"]({bbox});
  nwr["man_made"="surveillance"]({bbox});
  nwr["amenity"~"^(police|bar|pub|nightclub|bus_station|taxi|hospital|clinic|pharmacy|fuel|restaurant|cafe|fast_food|atm|bank|marketplace|food_court|ice_cream|cinema|theatre|school|college|university|place_of_worship|community_centre|doctors|dentist|post_office|library|events_venue|coworking_space|bureau_de_change)$"]({bbox});
  nwr["office"]({bbox});
  nwr["shop"]({bbox});
  nwr["tourism"="hotel"]({bbox});
  nwr["railway"~"^(station|subway_entrance)$"]({bbox});
);
out center tags;
"""


class TooFar(ValueError):
    pass


def overpass(s, w, n, e):
    """Query Overpass for one box, falling back to the next mirror when one is busy or down."""
    data = urllib.parse.urlencode({"data": QUERY.format(bbox=f"{s},{w},{n},{e}")}).encode()
    first = int(abs(s + w) * 1000) % len(MIRRORS)  # spread parallel chunks across mirrors to dodge per-IP limits
    order = MIRRORS[first:] + MIRRORS[:first]
    started = time.time()
    for url in order + order[:1]:
        try:
            req = urllib.request.Request(url, data, {"User-Agent": "SafeRoute/1.0"})
            with urllib.request.urlopen(req, timeout=35) as r:
                osm = json.load(r)
            if "runtime error" in osm.get("remark", ""):  # Overpass ran out of time and sent partial data
                raise ValueError(osm["remark"])
            print(f"map {s:.2f},{w:.2f} from {url.split('/')[2]} in {time.time() - started:.0f}s", flush=True)
            return osm
        except (OSError, ValueError) as err:  # URLError, timeouts, dropped connections, bad JSON
            print(f"map {s:.2f},{w:.2f}: {url.split('/')[2]} failed ({err}), trying next", flush=True)
    raise ConnectionError("Map data servers are busy, please try again in a minute")


class TileStore:
    def __init__(self, folder, fetch=overpass, reports=None, incidents=(), cache_size=6):
        self.folder = folder
        self.fetch = fetch
        self.reports = reports if reports is not None else []
        self.incidents = list(incidents)
        self.areas = OrderedDict()
        self.cache_size = cache_size
        self.locks = defaultdict(threading.Lock)
        self.lock = threading.Lock()
        os.makedirs(folder, exist_ok=True)

    def _path(self, t):
        return os.path.join(self.folder, f"{t[0]}_{t[1]}.json")

    def _fresh(self, t):
        p = self._path(t)
        return os.path.exists(p) and time.time() - os.path.getmtime(p) < TILE_MAX_AGE

    def _fetch_chunk(self, tiles):
        """One Overpass request for a block of missing tiles, split back into per-tile cache files."""
        with self.locks[min(tiles)]:  # two trips needing the same block download it once
            tiles = [t for t in tiles if not self._fresh(t)]
            if not tiles:
                return
            osm = self.fetch(min(t[0] for t in tiles) * TILE, min(t[1] for t in tiles) * TILE,
                             (max(t[0] for t in tiles) + 1) * TILE, (max(t[1] for t in tiles) + 1) * TILE)
            parts = {t: [] for t in tiles}
            for el in osm["elements"]:
                pts = el.get("geometry") or [el.get("center") or el]
                lats = [p["lat"] for p in pts if "lat" in p]
                lons = [p["lon"] for p in pts if "lon" in p]
                if not lats:
                    continue
                for i in range(math.floor(min(lats) / TILE), math.floor(max(lats) / TILE) + 1):
                    for j in range(math.floor(min(lons) / TILE), math.floor(max(lons) / TILE) + 1):
                        if (i, j) in parts:  # ways crossing tile edges go in each tile; merging dedupes
                            parts[(i, j)].append(el)
            for t, els in parts.items():
                with open(self._path(t) + ".tmp", "w") as f:
                    json.dump({"elements": els}, f)
                os.replace(self._path(t) + ".tmp", self._path(t))

    def tile(self, t):
        with open(self._path(t)) as f:
            return json.load(f)

    def area(self, tiles):
        key = frozenset(tiles)
        with self.lock:
            if key in self.areas:
                self.areas.move_to_end(key)
                return self.areas[key]
        chunks = defaultdict(list)
        for t in sorted(key):
            if not self._fresh(t):
                chunks[(t[0] // CHUNK, t[1] // CHUNK)].append(t)
        with ThreadPoolExecutor(2) as ex:  # Overpass rate-limits per IP, so few parallel requests
            list(ex.map(self._fetch_chunk, chunks.values()))
        parts = [self.tile(t) for t in sorted(key)]
        seen, elements = set(), []
        for osm in parts:
            for el in osm["elements"]:
                k = (el["type"], el["id"])  # ways crossing a tile edge come back in both tiles
                if k not in seen:
                    seen.add(k)
                    elements.append(el)
        if not any(el["type"] == "way" and "highway" in el.get("tags", {}) for el in elements):
            raise ValueError("No walkable streets are mapped around there yet")
        s, w = min(t[0] for t in key) * TILE, min(t[1] for t in key) * TILE
        n, e = (max(t[0] for t in key) + 1) * TILE, (max(t[1] for t in key) + 1) * TILE
        inside = lambda r: s <= r["lat"] <= n and w <= r["lon"] <= e
        area = Area({"elements": elements}, [r for r in self.reports if inside(r)], [i for i in self.incidents if inside(i)])
        with self.lock:
            self.areas[key] = area
            while len(self.areas) > self.cache_size:
                self.areas.popitem(last=False)
        return area

    def add_report(self, r):
        with self.lock:
            self.reports.append(r)
            for area in self.areas.values():
                s, w, n, e = area.bounds
                if s <= r["lat"] <= n and w <= r["lon"] <= e:
                    area.add_report(r)

    def for_trip(self, a, b):
        """The area covering a walk from a to b, with room for safer detours."""
        d = dist(a, b)
        if d > MAX_TRIP_M:
            raise TooFar(f"That's a {d / 1000:.0f} km walk. SafeRoute plans walks up to {MAX_TRIP_M // 1000} km.")
        return self.area(tiles_near(a, b, max(400, 0.25 * d)))

    def around(self, pt, radius_m=500):
        return self.area(tiles_near(pt, pt, radius_m))


def tiles_near(a, b, pad_m):
    """Tiles within pad_m of the straight line a-b."""
    pad_lat = pad_m / 111000
    pad_lon = pad_m / (111000 * math.cos(math.radians((a[0] + b[0]) / 2)))
    half_diag = TILE * 111000 * 0.71  # a tile counts if any part of it may be within pad_m
    out = set()
    for i in range(math.floor((min(a[0], b[0]) - pad_lat) / TILE), math.floor((max(a[0], b[0]) + pad_lat) / TILE) + 1):
        for j in range(math.floor((min(a[1], b[1]) - pad_lon) / TILE), math.floor((max(a[1], b[1]) + pad_lon) / TILE) + 1):
            if seg_dist(((i + 0.5) * TILE, (j + 0.5) * TILE), a, b) <= pad_m + half_diag:
                out.add((i, j))
    return out
