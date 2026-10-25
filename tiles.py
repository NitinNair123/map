"""Fetch OpenStreetMap data on demand in fixed tiles, cache them on disk, and build scored areas for any trip."""
import json
import math
import os
import threading
import time
import urllib.parse
import urllib.request
from collections import Counter, OrderedDict, defaultdict
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
  way["landuse"~"^(construction|brownfield|greenfield|cemetery|industrial|farmland|quarry|railway)$"]({bbox});
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


# Objects Mapillary detects in street-level photos, as the OSM tags the scorer already understands
MAPILLARY_KINDS = {"object--street-light": {"highway": "street_lamp"}, "object--cctv-camera": {"man_made": "surveillance"}}


def mapillary(s, w, n, e):
    """Street lights and CCTV cameras that Mapillary found in street-level photos of one tile."""
    q = urllib.parse.urlencode({"access_token": os.environ["MAPILLARY_TOKEN"], "fields": "id,object_value,geometry",
                                "bbox": f"{w},{s},{e},{n}", "object_values": ",".join(MAPILLARY_KINDS), "limit": 2000})
    url, out = f"https://graph.mapillary.com/map_features?{q}", []
    for _ in range(5):  # at most 10,000 objects per tile
        with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "SafeRoute/1.0"}), timeout=20) as r:
            page = json.load(r)
        for f in page.get("data", []):
            if f.get("object_value") in MAPILLARY_KINDS:
                lon, lat = f["geometry"]["coordinates"]
                out.append({"type": "node", "id": f"mly{f['id']}", "lat": lat, "lon": lon,
                            "tags": {**MAPILLARY_KINDS[f["object_value"]], "source": "mapillary"}})
        url = page.get("paging", {}).get("next")
        if not url:
            break
    return {"elements": out}


OVERTURE = "overturemaps-us-west-2/release/2026-10-21.0"  # shortcut: pinned release, bump monthly from stac.overturemaps.org


def _overture(theme, s, w, n, e, columns):
    import pyarrow.compute as pc  # optional dependency: pip install pyarrow
    import pyarrow.dataset as ds
    import pyarrow.fs as fs
    data = ds.dataset(f"{OVERTURE}/theme={theme}/type={theme[:-1]}/", format="parquet",
                      filesystem=fs.S3FileSystem(anonymous=True, region="us-west-2"))
    x, y = pc.field("bbox", "xmin"), pc.field("bbox", "ymin")
    return data.to_table(filter=(x >= w) & (x <= e) & (y >= s) & (y <= n), columns=columns + ["id", "bbox"]).to_pylist()


def overture_places(s, w, n, e):
    """Businesses and services from Overture Maps (Meta, Microsoft and others), minus closed and badly placed ones."""
    rows = _overture("places", s, w, n, e, ["names", "basic_category", "confidence", "operating_status"])
    stacked = Counter((r["bbox"]["xmin"], r["bbox"]["ymin"]) for r in rows)
    out = []
    for r in rows:
        at = (r["bbox"]["xmin"], r["bbox"]["ymin"])
        if (r["confidence"] or 0) < 0.5 or r["operating_status"] in ("permanently_closed", "temporarily_closed"):
            continue
        if stacked[at] >= 3:  # many places on one exact point were placed at a locality centre, not their door
            continue
        out.append({"type": "node", "id": f"ovp{r['id']}", "lat": at[1], "lon": at[0],
                    "tags": {"overture": r["basic_category"] or "place", "name": (r["names"] or {}).get("primary") or ""}})
    return {"elements": out}


def overture_buildings(s, w, n, e):
    """Building footprints (OSM, Google and Microsoft) as centre points, to measure how built-up a street is."""
    rows = _overture("buildings", s, w, n, e, [])
    return {"elements": [{"type": "node", "id": f"ovb{r['id']}", "lat": (r["bbox"]["ymin"] + r["bbox"]["ymax"]) / 2,
                          "lon": (r["bbox"]["xmin"] + r["bbox"]["xmax"]) / 2, "tags": {"building": "yes"}} for r in rows]}


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
    def __init__(self, folder, fetch=overpass, reports=None, incidents=(), cache_size=6, extras=None, background=()):
        self.folder = folder
        self.fetch = fetch
        self.extras = extras or {}  # name -> fetch(s, w, n, e) for extra sources, cached beside the OSM tile
        self.background = set(background)  # slow extras: fetched after the trip is answered, used from the next one
        self.pool = ThreadPoolExecutor(1)
        self.reports = reports if reports is not None else []
        self.incidents = list(incidents)
        self.areas = OrderedDict()
        self.cache_size = cache_size
        self.locks = defaultdict(threading.Lock)
        self.lock = threading.Lock()
        os.makedirs(folder, exist_ok=True)

    def _path(self, t, source=""):
        return os.path.join(self.folder, f"{t[0]}_{t[1]}{'.' + source if source else ''}.json")

    def _fresh(self, t, source=""):
        p = self._path(t, source)
        return os.path.exists(p) and time.time() - os.path.getmtime(p) < TILE_MAX_AGE

    def _fetch_chunk(self, tiles, source=""):
        """One request for a block of missing tiles from one source, split back into per-tile cache files."""
        with self.locks[(min(tiles), source)]:  # two trips needing the same block download it once
            tiles = [t for t in tiles if not self._fresh(t, source)]
            if not tiles:
                return
            fetch = self.extras[source] if source else self.fetch
            box = (min(t[0] for t in tiles) * TILE, min(t[1] for t in tiles) * TILE,
                   (max(t[0] for t in tiles) + 1) * TILE, (max(t[1] for t in tiles) + 1) * TILE)
            try:
                osm = fetch(*box)
            except (OSError, ValueError, KeyError) as err:
                if not source:
                    raise
                print(f"{source} {box}: failed ({err})", flush=True)  # an optional source being down must not block trips
                return
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
                path = self._path(t, source)
                with open(path + ".tmp", "w") as f:
                    json.dump({"elements": els}, f)
                os.replace(path + ".tmp", path)
        if source in self.background:
            with self.lock:  # rebuild cached areas so the next trip sees the new data
                for key in [k for k in self.areas if k & set(tiles)]:
                    del self.areas[key]

    def tile(self, t):
        with open(self._path(t)) as f:
            osm = json.load(f)
        for name in self.extras:
            if os.path.exists(self._path(t, name)):
                with open(self._path(t, name)) as f:
                    osm["elements"] += json.load(f)["elements"]
        return osm

    def _missing(self, tiles, source=""):
        chunks = defaultdict(list)
        for t in sorted(tiles):
            if not self._fresh(t, source):
                chunks[(t[0] // CHUNK, t[1] // CHUNK)].append(t)
        return list(chunks.values())

    def area(self, tiles):
        key = frozenset(tiles)
        with self.lock:
            if key in self.areas:
                self.areas.move_to_end(key)
                return self.areas[key]
        jobs = [(c, "") for c in self._missing(key)]
        jobs += [(c, name) for name in self.extras if name not in self.background for c in self._missing(key, name)]
        with ThreadPoolExecutor(3) as ex:  # Overpass rate-limits per IP, so few parallel requests
            list(ex.map(lambda job: self._fetch_chunk(*job), jobs))
        for name in self.background:
            for c in self._missing(key, name):
                self.pool.submit(self._fetch_chunk, c, name)
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
