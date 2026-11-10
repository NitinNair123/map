"""Exports training rows for the AI street model from every source this machine has keys or packages for:
OpenStreetMap (import or Overpass), NASA night lights, Mapillary lamps and cameras, Geoapify and Overture places,
Overture buildings, official lists in data/layers, GTFS timetables, app reports and the street survey, and the
bus counts from ml/collect.py. Google Places and Gemini hours are left out: Google's terms forbid training on
its data, and Gemini's hours are estimates, not measurements.

Usage:
  py ml/dataset.py                 the default spots across Delhi NCR, 1.5 km around each
  py ml/dataset.py --all           every tile of the OSM import (py import_osm.py first); hours, can be stopped and resumed
  py ml/dataset.py --points 28.47,77.50;28.63,77.22 --radius 2000
  py ml/dataset.py --world         ~40 areas of other Indian and world cities (OSM lit tags are plentiful there), marked
                                   region=other so training can test whether they help Delhi NCR (they are never scored)
Writes ml/data/blocks/<block>.edges.csv.gz and .slots.csv.gz; blocks already written are skipped.
"""
import argparse
import csv
import gzip
import importlib.util
import os
import random
import sys
import threading
import time
from collections import defaultdict

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import gtfs  # noqa: E402
import features  # noqa: E402
import osm_extra  # noqa: E402
import rasters  # noqa: E402
from features import NCR, STATIC, load_bus_logs, load_traffic_logs, slot_rows, static_rows  # noqa: E402
from rating import REPORT_TYPES  # noqa: E402
from reports import Reports  # noqa: E402
from tiles import (CHUNK, TileStore, geoapify, mapillary, nightlights, overture_buildings, overture_places, tiles_near,  # noqa: E402
                   warm_overture)

DATA = os.path.join(ROOT, "data")
OUT = os.path.join(ROOT, "ml", "data")
# spread over Delhi, Noida, Greater Noida, Gurugram, Ghaziabad and Faridabad: markets, campuses, housing, industry, outskirts
SPOTS = [(28.4741, 77.4998), (28.6315, 77.2167), (28.5355, 77.3910), (28.4595, 77.0266), (28.6692, 77.4538),
         (28.4089, 77.3178), (28.6519, 77.1909), (28.5677, 77.2433), (28.7041, 77.1025), (28.5921, 77.0460),
         (28.6129, 77.2295), (28.4950, 77.0890), (28.5800, 77.3200), (28.6400, 77.3000), (28.4700, 77.5400)]


# Cities whose OSM has many street-light tags, to learn what a lit street looks like. Real map data, no estimates.
WORLD = [
    ("Mumbai", 19.0760, 72.8777), ("Bengaluru", 12.9716, 77.5946), ("Chennai", 13.0827, 80.2707), ("Kolkata", 22.5726, 88.3639),
    ("Hyderabad", 17.3850, 78.4867), ("Pune", 18.5204, 73.8567), ("Jaipur", 26.9124, 75.7873), ("Lucknow", 26.8467, 80.9462),
    ("Chandigarh", 30.7333, 76.7794), ("Ahmedabad", 23.0225, 72.5714), ("Kochi", 9.9312, 76.2673), ("Bhopal", 23.2599, 77.4126),
    ("London", 51.5074, -0.1278), ("Manchester", 53.4808, -2.2426), ("Edinburgh", 55.9533, -3.1883), ("Dublin", 53.3498, -6.2603),
    ("Berlin", 52.5200, 13.4050), ("Hamburg", 53.5511, 9.9937), ("Munich", 48.1351, 11.5820), ("Vienna", 48.2082, 16.3738),
    ("Amsterdam", 52.3676, 4.9041), ("Rotterdam", 51.9244, 4.4777), ("Copenhagen", 55.6761, 12.5683), ("Stockholm", 59.3293, 18.0686),
    ("Paris", 48.8566, 2.3522), ("Lyon", 45.7640, 4.8357), ("Barcelona", 41.3874, 2.1686), ("Madrid", 40.4168, -3.7038),
    ("Rome", 41.9028, 12.4964), ("Warsaw", 52.2297, 21.0122), ("Prague", 50.0755, 14.4378), ("Zurich", 47.3769, 8.5417),
    ("New York", 40.7128, -74.0060), ("Chicago", 41.8781, -87.6298), ("Toronto", 43.6532, -79.3832), ("Melbourne", -37.8136, 144.9631),
    ("Sydney", -33.8688, 151.2093), ("Tokyo", 35.6762, 139.6503), ("Seoul", 37.5665, 126.9780), ("Singapore", 1.3521, 103.8198),
]


def region(lat, lon):
    return "ncr" if NCR[0] <= lat <= NCR[2] and NCR[1] <= lon <= NCR[3] else "other"


def key(name, env):
    path = os.path.join(DATA, name)
    return os.environ.get(env) or (open(path).read().strip() if os.path.exists(path) else "")


def rows(path, types=None, **extra):
    """Rows of lat,lon,type,when from data/survey.csv or data/incidents.csv, as server.py reads them; bad rows are skipped."""
    from datetime import datetime
    out = []
    if not os.path.exists(path):
        return out
    with open(path, newline="", encoding="utf-8-sig") as f:
        for n, row in enumerate(csv.DictReader(f), 2):
            try:
                t, kind = datetime.fromisoformat(row["when"]), row["type"].strip().lower()
                if types and kind not in types:
                    raise ValueError(f"unknown type {kind!r}")
                out.append({"lat": float(row["lat"]), "lon": float(row["lon"]), "type": kind, "hour": t.hour,
                            "weekday": t.weekday(), "ts": t.timestamp(), **extra})
            except (KeyError, ValueError, TypeError, AttributeError) as err:
                print(f"{os.path.basename(path)} line {n} skipped: {err}", flush=True)
    return out


def breaker(fetch, name, limit=3):
    """A source that failed `limit` times in a row is skipped for the rest of the run instead of timing out on every
    tile (Mapillary timing out made one block take an hour). Its tiles stay without it, so its columns are unknown."""
    fails = [0]

    def call(*a):
        if fails[0] >= limit:
            raise OSError(f"{name} skipped for this run after {limit} failures in a row")
        try:
            out = fetch(*a)
        except Exception:
            fails[0] += 1
            if fails[0] == limit:
                print(f"{name}: {limit} failures in a row, skipped for the rest of this run", flush=True)
            raise
        fails[0] = 0
        return out
    return call


def store(world=False):
    """A TileStore set up like server.py's, except that every source loads before an area is built (no background)
    and streets come from OpenStreetMap, which carries the lit tags."""
    extras = {"nightlights": nightlights}
    used = ["OpenStreetMap", "NASA night lights"]
    if key("mapillary_token.txt", "MAPILLARY_TOKEN"):
        os.environ["MAPILLARY_TOKEN"] = key("mapillary_token.txt", "MAPILLARY_TOKEN")
        extras["mapillary"] = breaker(mapillary, "Mapillary")
        used.append("Mapillary")
    if key("geoapify_key.txt", "GEOAPIFY_KEY") and not world:  # its free daily quota is kept for Delhi NCR
        os.environ["GEOAPIFY_KEY"] = key("geoapify_key.txt", "GEOAPIFY_KEY")
        extras["geoapify"] = breaker(geoapify, "Geoapify")
        used.append("Geoapify")
    if importlib.util.find_spec("pyarrow"):
        extras.update(places=overture_places, buildings=overture_buildings)
        threading.Thread(target=warm_overture, daemon=True).start()
        used.append("Overture places and buildings")
    stops = gtfs.load(os.path.join(DATA, "gtfs"))
    features.TIMETABLES = bool(stops)
    reports = Reports(os.path.join(DATA, "reports.db")).all() if os.path.exists(os.path.join(DATA, "reports.db")) else []
    survey = rows(os.path.join(DATA, "survey.csv"), REPORT_TYPES, source="Street survey")
    incidents = rows(os.path.join(DATA, "incidents.csv"))
    features.REPORTS, features.INCIDENTS = bool(reports or survey), bool(incidents)
    print(f"Sources: {', '.join(used)}; {len(stops)} timetabled stops; {len(reports)} app reports; {len(survey)} survey rows; {len(incidents)} incidents", flush=True)
    return TileStore(os.path.join(DATA, "tiles"), reports=reports + survey, incidents=incidents, extras=extras, stops=stops,
                     layers=os.path.join(DATA, "layers"))


def blocks(tiles):
    """Tiles grouped into CHUNK x CHUNK blocks, so each block is one download per source."""
    out = defaultdict(set)
    for t in tiles:
        out[(t[0] // CHUNK, t[1] // CHUNK)].add(t)
    return out


def write(path, rows, columns):
    tmp = path + ".part"
    with gzip.open(tmp, "wt", newline="") as f:
        w = csv.DictWriter(f, columns, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    os.replace(tmp, path)  # a block stopped half way is redone next run, never half used


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--points")
    ap.add_argument("--world", action="store_true")
    ap.add_argument("--radius", type=float, default=1500)
    ap.add_argument("--redo", action="store_true", help="rebuild blocks already written (new data since)")
    ap.add_argument("--hours", type=float, default=0, help="stop starting new blocks after this many hours (0 = no limit); blocks come in random order, so any stop point is a fair spread of the area")
    args = ap.parse_args()
    ts = store(args.world)
    if args.all:
        if not ts.extracted:
            sys.exit("No OSM import found: run py import_osm.py first, or leave out --all")
        tiles = ts.extracted
    else:
        pts = [(la, lo) for _, la, lo in WORLD] if args.world else \
            [tuple(map(float, p.split(","))) for p in args.points.split(";")] if args.points else SPOTS
        tiles = set().union(*(tiles_near(p, p, args.radius) for p in pts))
    bus = load_bus_logs(os.path.join(OUT, "bus_counts.csv"), os.path.join(OUT, "bus_hours.csv"))
    tr = load_traffic_logs(os.path.join(OUT, "traffic.csv"))
    print("Traffic logs:", f"{sum(map(len, tr.values()))} readings" if tr else "none yet", flush=True)
    print("Bus logs:", f"{sum(bus['watched'].values())} hours watched" if bus else "none yet (run py ml/collect.py)", flush=True)
    folder = os.path.join(OUT, "blocks")
    os.makedirs(folder, exist_ok=True)
    todo = blocks(tiles)
    edges = slots = 0
    order = sorted(todo.items())
    random.Random(0).shuffle(order)  # the same shuffle every run, so a stopped and resumed run carries on through the same order
    began = time.time()
    for n, (b, group) in enumerate(order, 1):
        if args.hours and time.time() - began > args.hours * 3600:
            print(f"Time limit of {args.hours} h reached after {n - 1} of {len(order)} blocks; run again to continue", flush=True)
            break
        name = os.path.join(folder, f"{b[0]}_{b[1]}")
        if os.path.exists(name + ".slots.csv.gz") and not args.redo:
            continue
        started = time.time()
        try:
            area = ts.area(group)
        except (OSError, ValueError, KeyError) as err:
            print(f"[{n}/{len(todo)}] block {b}: skipped ({err})", flush=True)
            continue
        e_rows, s_rows = static_rows(area), slot_rows(area, bus, tr)
        got = rasters.add_land_cover(e_rows)
        extra = osm_extra.add(e_rows, osm_extra.load(group))
        used = ", ".join(sorted(area.sources) + (["ESA WorldCover"] if got else []) + (["OSM walls, drains, parking, toilets"] if extra else []))
        for r in e_rows:
            r["sources"] = used  # which downloads this block really has: a source that failed leaves a gap, and the log shows it
            r["region"] = region(r["lat"], r["lon"])
            if r["region"] != "ncr":
                r["gtfs_stops"] = float("nan")  # only Delhi timetables are loaded: unknown elsewhere, not zero
        write(name + ".edges.csv.gz", e_rows, ["edge_id", "tile", "lat", "lon", "region", "sources", *STATIC, "lit", "lit_source"])
        write(name + ".slots.csv.gz", s_rows, ["edge_id", "hour", "weekend", "daylight", "gtfs_deps", "open_share", "open_known", "bus_per_hour", "traffic_ratio"])
        ts.areas.clear()  # one block at a time in memory
        edges, slots = edges + len(e_rows), slots + len(s_rows)
        print(f"[{n}/{len(todo)}] block {b}: {len(e_rows)} streets, {sum(r['lit'] is not None for r in e_rows)} with a lit label, "
              f"{len(s_rows)} hour rows, sources: {used} ({time.time() - started:.0f} s)", flush=True)
    print(f"Done: {edges} streets and {slots} hour rows written this run", flush=True)


if __name__ == "__main__":
    main()
