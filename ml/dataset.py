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
Writes ml/data/blocks/<block>.edges.csv.gz and .slots.csv.gz; blocks already written are skipped. Blocks written while
Overpass or Mapillary was down are rebuilt at the end of the run (or alone with --repair), never left with zeros.
"""
import argparse
import csv
import json
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
from spots import SPOTS  # noqa: E402
from features import NCR, STATIC, load_bus_logs, load_traffic_logs, slot_rows, static_rows  # noqa: E402
from rating import REPORT_TYPES  # noqa: E402
from reports import Reports  # noqa: E402
import tiles  # noqa: E402
from tiles import (CHUNK, TILE, TileStore, geoapify, mapillary, nightlights, overture_buildings, overture_places, overture_prefetch,  # noqa: E402
                   tiles_near)

DATA = os.path.join(ROOT, "data")
OUT = os.path.join(ROOT, "ml", "data")


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


def breaker(fetch, name, limit=3, cool=300):
    """A source that failed `limit` times in a row is skipped for `cool` seconds instead of timing out on every tile
    (Mapillary timing out made one block take an hour), then tried again; each failed retry doubles the pause (up to
    30 min), a success resets it. Tiles skipped meanwhile stay without the source, so its columns are unknown, and the
    repair pass at the end of main() fetches them later."""
    st = {"fails": 0, "until": 0.0, "cool": cool}
    lock = threading.Lock()

    def call(*a):
        with lock:
            wait = st["until"] - time.time()
        if wait > 0:
            err = OSError(f"{name} skipped, resting for {wait:.0f} more seconds after {limit} failures in a row")
            err.skipped = True
            raise err
        try:
            out = fetch(*a)
        except Exception:
            with lock:
                st["fails"] += 1
                if st["fails"] >= limit:  # (the refusals during the rest are counted as skipped, not as failed downloads)
                    st["until"], st["cool"] = time.time() + st["cool"], min(st["cool"] * 2, 1800)
                    print(f"{name}: {st['fails']} failures in a row, resting for {st['until'] - time.time():.0f} s", flush=True)
                    st["fails"] = limit - 1  # one more failure after the rest re-opens the pause, longer
            raise
        with lock:
            st.update(fails=0, until=0.0, cool=cool)
        return out
    call.state = st
    return call

def store(world=False):
    """A TileStore set up like server.py's, except that every source loads before an area is built (no background)
    and streets come from OpenStreetMap, which carries the lit tags."""
    extras = {"nightlights": nightlights}
    used = ["OpenStreetMap", "NASA night lights"]
    if key("mapillary_token.txt", "MAPILLARY_TOKEN"):
        os.environ["MAPILLARY_TOKEN"] = key("mapillary_token.txt", "MAPILLARY_TOKEN")
        if key("mapillary_token_2.txt", "MAPILLARY_TOKEN_2"):  # optional second token, used when the first is refused or rate-limited
            os.environ["MAPILLARY_TOKEN_2"] = key("mapillary_token_2.txt", "MAPILLARY_TOKEN_2")
        extras["mapillary"] = breaker(mapillary, "Mapillary")
        used.append("Mapillary")
    if key("geoapify_key.txt", "GEOAPIFY_KEY") and not world:  # its free daily quota is kept for Delhi NCR
        os.environ["GEOAPIFY_KEY"] = key("geoapify_key.txt", "GEOAPIFY_KEY")
        extras["geoapify"] = breaker(geoapify, "Geoapify")
        used.append("Geoapify")
    if importlib.util.find_spec("pyarrow"):
        extras.update(places=overture_places, buildings=overture_buildings)  # saved locally first by prefetch() in main()
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


LIVE = os.path.join(ROOT, "ml", "out", "studio", "build_live.json")


class Live:
    """Real counters of this run, written every second to ml/out/studio/build_live.json for the Studio: every number
    there comes from a download or a block that really happened, so the Studio shows nothing that didn't."""

    def __init__(self, total):
        self.lock = threading.Lock()
        self.info = {"pid": os.getpid(), "started": time.time(), "phase": "starting", "blocks_total": total, "blocks_on_disk": 0,
                     "blocks_built": 0, "blocks_failed": 0, "streets": 0, "lit": 0, "first_block_at": None, "block_seconds": [],
                     "current": None, "prefetch": None, "last_event": time.time()}
        threading.Thread(target=self.loop, daemon=True).start()

    def set(self, **kw):
        with self.lock:
            self.info.update(kw, last_event=time.time())

    def block_done(self, streets, lit, seconds):
        with self.lock:
            i = self.info
            i.update(blocks_built=i["blocks_built"] + 1, blocks_on_disk=i["blocks_on_disk"] + 1, streets=i["streets"] + streets,
                     lit=i["lit"] + lit, block_seconds=(i["block_seconds"] + [round(seconds, 1)])[-50:], current=None, last_event=time.time())

    def write(self):
        with self.lock:
            snap = {**self.info, "updated": time.time(), "sources": tiles.stats_snapshot()}
        snap["last_event"] = max([snap["last_event"]] + [s["last"] or 0 for s in snap["sources"].values()])
        os.makedirs(os.path.dirname(LIVE), exist_ok=True)
        with open(LIVE + ".tmp", "w") as f:
            json.dump(snap, f)
        try:
            os.replace(LIVE + ".tmp", LIVE)
        except OSError:  # the Studio had it open this instant (Windows): next second's write lands
            pass

    def loop(self):
        said = False
        while True:
            try:
                self.write()
            except Exception as err:  # never let the counters die silently: the Studio would call the build frozen
                if not said:
                    print(f"live counters: {err!r}", flush=True)
                    said = True
            time.sleep(1)


def prefetch(tile_set, live):
    """Overture places and buildings for the whole region, saved once (see tiles.overture_prefetch). Only for one
    compact region like Delhi NCR; --world areas are spread over the globe and keep querying online per block."""
    if not importlib.util.find_spec("pyarrow") or not tile_set:
        return
    s, n = min(t[0] for t in tile_set) * TILE, (max(t[0] for t in tile_set) + 1) * TILE
    w, e = min(t[1] for t in tile_set) * TILE, (max(t[1] for t in tile_set) + 1) * TILE
    if (n - s) * (e - w) > 4:
        return
    live.set(phase="Downloading Overture places and buildings for the whole area (once, then every block reads them from disk)")
    print(f"Overture: places and buildings for {s:.2f},{w:.2f} to {n:.2f},{e:.2f}, saved in ml/data/overture", flush=True)
    overture_prefetch(os.path.join(OUT, "overture"), s, w, n, e,
                      progress=lambda kind, done, of: live.set(prefetch={"kind": kind, "done": done, "of": of}))


def gaps(folder, order, wanted):
    """Blocks already on disk whose `sources` column lacks something in `wanted` (a block with no streets has no row to
    read and counts as done): a list of (position, block, tiles) to rebuild."""
    out = []
    for n, (b, group) in enumerate(order, 1):
        path = os.path.join(folder, f"{b[0]}_{b[1]}.edges.csv.gz")
        if not (os.path.exists(path) and os.path.exists(path[:-len('.edges.csv.gz')] + ".slots.csv.gz")):
            continue
        with gzip.open(path, "rt", newline="") as f:
            row = next(csv.DictReader(f), None)
        if row is not None and any(w not in row.get("sources", "") for w in wanted):
            out.append((n, b, group))
    return out

def older_than_traffic(folder, order, tr, traffic_path):
    """Blocks saved before the newest traffic readings arrived that those readings reach: a traffic cell is a 0.001 degree
    square on a main road, so its tile is cell // 10 and its block is tile // CHUNK. Returns (position, block, tiles) to
    rebuild; a rebuilt block is newer than the file, so it is not asked for twice."""
    if not tr or not os.path.exists(traffic_path):
        return []
    reached = {((k[0] // 10) // CHUNK, (k[1] // 10) // CHUNK) for k in tr}
    newest = os.path.getmtime(traffic_path)
    out = []
    for n, (b, group) in enumerate(order, 1):
        slots = os.path.join(folder, f"{b[0]}_{b[1]}.slots.csv.gz")
        if b in reached and os.path.exists(slots) and os.path.getmtime(slots) < newest:
            out.append((n, b, group))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--points")
    ap.add_argument("--world", action="store_true")
    ap.add_argument("--radius", type=float, default=1500)
    ap.add_argument("--repair", action="store_true", help="only re-fetch what finished blocks are missing (a source was down when they were built)")
    ap.add_argument("--redo", action="store_true", help="rebuild blocks already written (new data since)")
    ap.add_argument("--hours", type=float, default=0, help="stop starting new blocks after this many hours (0 = no limit); blocks come in random order, so any stop point is a fair spread of the area")
    args = ap.parse_args()
    tiles.VERBOSE = True  # a timestamped line per download, so the log shows work between blocks
    ts = store(args.world)
    if args.all:
        if not ts.extracted:
            sys.exit("No OSM import found: run py import_osm.py first, or leave out --all")
        tile_set = ts.extracted
    else:
        pts = [(la, lo) for _, la, lo in WORLD] if args.world else \
            [tuple(map(float, p.split(","))) for p in args.points.split(";")] if args.points else SPOTS
        tile_set = set().union(*(tiles_near(p, p, args.radius) for p in pts))
    bus = load_bus_logs(os.path.join(OUT, "bus_counts.csv"), os.path.join(OUT, "bus_hours.csv"))
    tr = load_traffic_logs(os.path.join(OUT, "traffic.csv"))
    print("Traffic logs:", f"{sum(map(len, tr.values()))} readings" if tr else "none yet", flush=True)
    print("Bus logs:", f"{sum(bus['watched'].values())} hours watched" if bus else "none yet (run py ml/collect.py)", flush=True)
    folder = os.path.join(OUT, "blocks")
    os.makedirs(folder, exist_ok=True)
    todo = blocks(tile_set)
    edges = slots = 0
    order = sorted(todo.items())
    random.Random(0).shuffle(order)  # the same shuffle every run, so a stopped and resumed run carries on through the same order
    live = Live(len(order))
    live.set(blocks_on_disk=sum(os.path.exists(os.path.join(folder, f"{b[0]}_{b[1]}.slots.csv.gz")) for b, _ in order))
    if not args.world:
        prefetch(tile_set, live)
    live.set(phase="Measuring streets block by block")
    began = time.time()
    total = len(order)

    def build(n, b, group):
        """Measures and writes one block; returns (streets, hour rows), or None when its streets can't be loaded."""
        name = os.path.join(folder, f"{b[0]}_{b[1]}")
        started = time.time()
        live.set(current={"n": n, "block": list(b), "started": started})
        if live.info["first_block_at"] is None:
            live.set(first_block_at=started)
        try:
            area = ts.area(group)
        except (OSError, ValueError, KeyError) as err:
            print(f"[{n}/{total}] block {b}: skipped ({err})", flush=True)
            live.set(blocks_failed=live.info["blocks_failed"] + 1, current=None)
            return None
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
        live.block_done(len(e_rows), sum(r["lit"] is not None for r in e_rows), time.time() - started)
        print(f"[{n}/{total}] block {b}: {len(e_rows)} streets, {sum(r['lit'] is not None for r in e_rows)} with a lit label, "
              f"{len(s_rows)} hour rows, sources: {used} ({time.time() - started:.0f} s)", flush=True)
        return len(e_rows), len(s_rows)

    def out_of_time():
        return args.hours and time.time() - began > args.hours * 3600

    if not args.repair:
        for n, (b, group) in enumerate(order, 1):
            if out_of_time():
                print(f"Time limit of {args.hours} h reached after {n - 1} of {total} blocks; run again to continue", flush=True)
                break
            if os.path.exists(os.path.join(folder, f"{b[0]}_{b[1]}.slots.csv.gz")) and not args.redo:
                continue
            done = build(n, b, group)
            if done:
                edges, slots = edges + done[0], slots + done[1]
    # Traffic readings keep arriving (ml/collect.py, the logger): blocks saved before the latest ones get their hour rows now.
    for n, b, group in older_than_traffic(folder, order, tr, os.path.join(OUT, "traffic.csv")):
        if out_of_time():
            break
        if n == 1 or not live.info.get("traffic_redo"):
            print("Traffic: rebuilding the blocks saved before the newest traffic readings", flush=True)
        live.set(phase="Adding new traffic readings to older blocks", traffic_redo=True)
        build(n, b, group)
    # Repair: a block written while a source was down has that source's columns unknown. Ask again now, a few times, since
    # Overpass and Mapillary usually come back within the hour. The source's own rest (see breaker, osm_extra.HEALTH) keeps
    # a source that is still down from costing more than a moment per block.
    wanted = [label for label, on in (("OSM walls", True), ("Mapillary", "mapillary" in ts.extras)) if on]
    for attempt in range(1, 4):
        if out_of_time():
            break
        todo_fix = gaps(folder, order, wanted)
        live.set(gaps=len(todo_fix))
        if not todo_fix:
            break
        live.set(phase=f"Repairing {len(todo_fix)} blocks that missed a source (try {attempt} of 3)")
        print(f"Repair try {attempt}: {len(todo_fix)} blocks lack {', '.join(wanted)}", flush=True)
        fixed = 0
        for n, b, group in todo_fix:
            if out_of_time():
                break
            done = build(n, b, group)
            fixed += bool(done)
        left = len(gaps(folder, order, wanted))
        live.set(gaps=left)
        print(f"Repair try {attempt}: {left} blocks still have gaps", flush=True)
        if left == 0 or attempt == 3 or out_of_time():
            break
        time.sleep(300)  # let the servers recover before the next try
    live.set(phase="Done", current=None)
    live.write()
    print(f"Done: {edges} streets and {slots} hour rows written this run", flush=True)


if __name__ == "__main__":
    main()
