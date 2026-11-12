"""Release test: trips Nitin knows personally, and the road the recommended route must follow on each.

Usage: python known_places.py data/known_places.csv   (on Windows: py known_places.py data\\known_places.csv)

known_places.csv has one row per trip, with columns
name,start_lat,start_lon,end_lat,end_lon,when,must_use,must_avoid
  when        ISO date-time of the walk, e.g. 2026-11-12T22:00+05:30
  must_use    a road name the recommended route has to follow (as written on the map), or empty
  must_avoid  a road name it must not follow, or empty
Exits with an error when any trip fails, so it can gate a release. Unnamed streets can't be checked by name.
"""
import csv
import os
import sys
from datetime import datetime


def check(area_for, rows):
    """[(row name, what went wrong)] for the trips whose recommended route breaks its rule; empty when all pass."""
    failed = []
    for row in rows:
        a, b = (float(row["start_lat"]), float(row["start_lon"])), (float(row["end_lat"]), float(row["end_lon"]))
        try:
            best = area_for(a, b).routes(a, b, datetime.fromisoformat(row["when"]))[0]  # the recommended route comes first
        except Exception as err:  # no map here, too far, no way through: a failed trip, not a crashed run
            failed.append((row["name"], f"no route: {err}"))
            continue
        roads = " | ".join(s["text"] for s in best["steps"]).lower()
        use, avoid = (row.get("must_use") or "").strip().lower(), (row.get("must_avoid") or "").strip().lower()
        if use and use not in roads:
            failed.append((row["name"], f"does not follow {row['must_use']}"))
        if avoid and avoid in roads:
            failed.append((row["name"], f"follows {row['must_avoid']}"))
    return failed


if __name__ == "__main__":
    import import_osm
    from tiles import TileStore, nightlights

    with open(sys.argv[1], newline="", encoding="utf-8-sig") as f:
        rows = [r for r in csv.DictReader(f) if (r.get("name") or "").strip()]
    store = TileStore(os.path.join(import_osm.DATA, "tiles"), extras={"nightlights": nightlights})
    failed = check(store.for_trip, rows)
    for name, why in failed:
        print(f"FAIL {name}: {why}")
    print(f"{len(rows) - len({n for n, _ in failed})} of {len(rows)} known trips pass")
    sys.exit(1 if failed or not rows else 0)
