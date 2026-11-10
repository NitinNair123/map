"""Exports training rows for the AI street model from every source this machine has keys or packages for:
OpenStreetMap (import or Overpass), NASA night lights, Mapillary lamps and cameras, Geoapify and Overture places,
Overture buildings, official lists in data/layers, GTFS timetables, app reports and the street survey, and the
bus counts from ml/collect.py. Google Places and Gemini hours are left out: Google's terms forbid training on
its data, and Gemini's hours are estimates, not measurements.

Usage:
  py ml/dataset.py                 the default spots across Delhi NCR, 1.5 km around each
  py ml/dataset.py --all           every tile of the OSM import (py import_osm.py first); hours, can be stopped and resumed
  py ml/dataset.py --points 28.47,77.50;28.63,77.22 --radius 2000
Writes ml/data/blocks/<block>.edges.csv.gz and .slots.csv.gz; blocks already written are skipped.
"""
import argparse
import csv
import gzip
import importlib.util
import os
import sys
import threading
import time
from collections import defaultdict

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import gtfs  # noqa: E402
from features import STATIC, load_bus_logs, load_traffic_logs, slot_rows, static_rows  # noqa: E402
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


def key(name, env):
    path = os.path.join(DATA, name)
    return os.environ.get(env) or (open(path).read().strip() if os.path.exists(path) else "")


def store():
    """A TileStore set up like server.py's, except that every source loads before an area is built (no background)
    and streets come from OpenStreetMap, which carries the lit tags."""
    extras = {"nightlights": nightlights}
    used = ["OpenStreetMap", "NASA night lights"]
    if key("mapillary_token.txt", "MAPILLARY_TOKEN"):
        os.environ["MAPILLARY_TOKEN"] = key("mapillary_token.txt", "MAPILLARY_TOKEN")
        extras["mapillary"] = mapillary
        used.append("Mapillary")
    if key("geoapify_key.txt", "GEOAPIFY_KEY"):
        os.environ["GEOAPIFY_KEY"] = key("geoapify_key.txt", "GEOAPIFY_KEY")
        extras["geoapify"] = geoapify
        used.append("Geoapify")
    if importlib.util.find_spec("pyarrow"):
        extras.update(places=overture_places, buildings=overture_buildings)
        threading.Thread(target=warm_overture, daemon=True).start()
        used.append("Overture places and buildings")
    stops = gtfs.load(os.path.join(DATA, "gtfs"))
    reports = Reports(os.path.join(DATA, "reports.db")).all() if os.path.exists(os.path.join(DATA, "reports.db")) else []
    survey = []
    if os.path.exists(os.path.join(DATA, "survey.csv")):
        from datetime import datetime
        with open(os.path.join(DATA, "survey.csv"), newline="", encoding="utf-8-sig") as f:
            for row in csv.DictReader(f):
                try:
                    t = datetime.fromisoformat(row["when"])
                    if row["type"].strip().lower() in REPORT_TYPES:
                        survey.append({"lat": float(row["lat"]), "lon": float(row["lon"]), "type": row["type"].strip().lower(),
                                       "hour": t.hour, "weekday": t.weekday(), "ts": t.timestamp(), "source": "Street survey"})
                except (KeyError, ValueError, TypeError):
                    pass
    print(f"Sources: {', '.join(used)}; {len(stops)} timetabled stops; {len(reports)} app reports; {len(survey)} survey rows", flush=True)
    return TileStore(os.path.join(DATA, "tiles"), reports=reports + survey, extras=extras, stops=stops,
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
    ap.add_argument("--radius", type=float, default=1500)
    ap.add_argument("--redo", action="store_true", help="rebuild blocks already written (new data since)")
    args = ap.parse_args()
    ts = store()
    if args.all:
        if not ts.extracted:
            sys.exit("No OSM import found: run py import_osm.py first, or leave out --all")
        tiles = ts.extracted
    else:
        pts = [tuple(map(float, p.split(","))) for p in args.points.split(";")] if args.points else SPOTS
        tiles = set().union(*(tiles_near(p, p, args.radius) for p in pts))
    bus = load_bus_logs(os.path.join(OUT, "bus_counts.csv"), os.path.join(OUT, "bus_hours.csv"))
    tr = load_traffic_logs(os.path.join(OUT, "traffic.csv"))
    print("Traffic logs:", f"{sum(map(len, tr.values()))} readings" if tr else "none yet", flush=True)
    print("Bus logs:", f"{sum(bus['watched'].values())} hours watched" if bus else "none yet (run py ml/collect.py)", flush=True)
    folder = os.path.join(OUT, "blocks")
    os.makedirs(folder, exist_ok=True)
    todo = blocks(tiles)
    edges = slots = 0
    for n, (b, group) in enumerate(sorted(todo.items()), 1):
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
        write(name + ".edges.csv.gz", e_rows, ["edge_id", "tile", "lat", "lon", *STATIC, "lit", "lit_source"])
        write(name + ".slots.csv.gz", s_rows, ["edge_id", "hour", "weekend", "daylight", "gtfs_deps", "open_share", "open_known", "bus_per_hour", "traffic_ratio"])
        ts.areas.clear()  # one block at a time in memory
        edges, slots = edges + len(e_rows), slots + len(s_rows)
        print(f"[{n}/{len(todo)}] block {b}: {len(e_rows)} streets, {sum(r['lit'] is not None for r in e_rows)} with a lit label, "
              f"{len(s_rows)} hour rows ({time.time() - started:.0f} s)", flush=True)
    print(f"Done: {edges} streets and {slots} hour rows written this run", flush=True)


if __name__ == "__main__":
    main()
