"""Check the imported Delhi NCR map: what the import found, routes around IILM at 10 pm and how long they take.
Run after python import_osm.py. GitHub Actions runs it for commits whose message contains [ncr-check]."""
import os
import time
from datetime import datetime, timedelta, timezone

import import_osm
from tiles import TileStore, nightlights

IST = timezone(timedelta(hours=5, minutes=30))
NIGHT = datetime.now(IST).replace(hour=22, minute=0, second=0, microsecond=0)
TRIPS = {"campus": ((28.4741, 77.4998), (28.4700, 77.5050)), "8 km": ((28.4741, 77.4998), (28.5355, 77.4500))}
folder = os.path.join(import_osm.DATA, "tiles")


def fail(*a):
    raise AssertionError("imported tiles must not be fetched")


store = TileStore(folder, fetch=fail, extras={"nightlights": nightlights})
for name, (a, b) in TRIPS.items():
    t = time.time()
    area = store.for_trip(a, b)
    loaded = time.time() - t
    t = time.time()
    routes = area.routes(a, b, NIGHT)
    took = time.time() - t
    e = area.edges
    print(f"\n== {name}: {len(e)} street segments, area loaded in {loaded:.1f}s, routes in {took:.2f}s")
    print(f"   with lanes {sum(1 for x in e if x['lanes'])}, maxspeed {sum(1 for x in e if x['maxspeed'])},"
          f" surface {sum(1 for x in e if x['surface'])}, near a crossing {sum(1 for x in e if x['near']['crossing'])},"
          f" near a guard post {sum(1 for x in e if x['near']['guard'])}; buildings {sum(area.building_cells.values())}")
    for r in routes:
        print(f"   {r['kind']:9} score {r['score']:>3} confidence {r['confidence']:.2f} ({r['confidence_level']})"
              f" {r['length_m']} m, rank {r['rank']} of {r['checked']}, same as {r['same_as']}, unrated {r['unrated_pct']}%")
db = os.path.join(folder, "search.db")
print(f"\nsearch.db {os.path.getsize(db) / 1e6:.1f} MB")
for q in ("IILM", "Knowledge Park", "Pari Chowk"):
    print(q, "->", [(h["name"], h["kind"]) for h in import_osm.search(db, q, near=(28.47, 77.50))[:5]])
