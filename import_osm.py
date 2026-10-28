"""One-time import of OpenStreetMap for a region, so every street, lamp, shop and opening hour loads instantly.

Usage: python import_osm.py [file.osm.pbf or URL ...] [--bbox south,west,north,east]
       python import_osm.py --refresh     (download again and re-import what was imported last time)
With no files it downloads the Geofabrik extracts that cover Delhi NCR (northern zone, plus central zone for Noida
and Greater Noida). Needs: pip install osmium. The server re-runs it on its own when the import is a month old.
It also writes building counts (so "buildings along the street" works from OSM alone) and data/tiles/search.db,
an offline index of named places and streets for the search box.
"""
import json
import os
import re
import sqlite3
import sys
import time
import urllib.request
from collections import Counter

from safety import CELL
from tiles import QUERY, TILE, write_tiles

NCR = (28.35, 76.85, 28.90, 77.60)
GEOFABRIK = ["https://download.geofabrik.de/asia/india/northern-zone-latest.osm.pbf",
             "https://download.geofabrik.de/asia/india/central-zone-latest.osm.pbf"]
DATA = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
KEYS = ("highway", "landuse", "natural", "leisure", "railway", "man_made", "amenity", "office", "shop", "tourism", "building", "emergency")
DOWNLOAD_MAX_AGE = 7 * 86400  # Geofabrik rebuilds the extracts daily

# The same selection as the Overpass query in tiles.py, so imported tiles and fetched tiles look alike
ROAD = re.compile(re.search(r'way\["highway"~"([^"]+)"\]', QUERY).group(1))
POI = {k: re.compile(v) for k, v in re.findall(r'nwr\["(\w+)"~"([^"]+)"\]', QUERY)}
ZONE = {k: re.compile(v) for k, v in re.findall(r'way\["(\w+)"~"([^"]+)"\]', QUERY) if k != "highway"}


def wanted(tags):
    """'road', 'zone', 'poi' or None, matching the Overpass query."""
    if ROAD.match(tags.get("highway", "")) and tags.get("foot") != "no" and tags.get("access") not in ("private", "no"):
        return "road"
    if any(rx.match(tags.get(k, "")) for k, rx in ZONE.items()) or tags.get("leisure") == "park" or tags.get("railway") == "rail":
        return "zone"
    if (tags.get("highway") in ("street_lamp", "bus_stop", "crossing", "traffic_signals") or "office" in tags or "shop" in tags or tags.get("tourism") == "hotel"
            or tags.get("man_made") == "surveillance" or any(rx.match(tags.get(k, "")) for k, rx in POI.items())):
        return "poi"
    if "building" in tags:
        return "building"
    return None


def read(path, box, out, buildings, names):
    """Append matching objects to out, count building outlines per grid cell, collect (name, kind, lat, lon)."""
    import osmium  # optional dependency: pip install osmium
    s, w, n, e = box
    inside = lambda lat, lon: s <= lat <= n and w <= lon <= e
    started, count = time.time(), 0
    fp = osmium.FileProcessor(path).with_locations().with_filter(osmium.filter.KeyFilter(*KEYS))
    for obj in fp:
        tags = dict(obj.tags)
        kind = wanted(tags)
        if not kind:
            continue
        if obj.is_node():
            if kind == "building" or not obj.location.valid() or not inside(obj.location.lat, obj.location.lon):
                continue
            lat, lon = obj.location.lat, obj.location.lon
            if kind == "poi":
                out.append({"type": "node", "id": obj.id, "lat": lat, "lon": lon, "tags": tags})
        elif obj.is_way():
            try:
                pts = [(nd.lat, nd.lon) for nd in obj.nodes]
            except osmium.InvalidLocationError:
                continue  # the way leaves the downloaded file
            if not pts or not any(inside(*p) for p in pts):
                continue
            lat, lon = sum(p[0] for p in pts) / len(pts), sum(p[1] for p in pts) / len(pts)
            if kind == "building":
                buildings[(int(lat // CELL), int(lon // CELL))] += 1
                continue
            if kind == "poi":
                out.append({"type": "way", "id": obj.id, "center": {"lat": lat, "lon": lon}, "tags": tags})
            else:
                el = {"type": "way", "id": obj.id, "geometry": [{"lat": a, "lon": b} for a, b in pts], "tags": tags}
                if kind == "road":
                    el["nodes"] = [nd.ref for nd in obj.nodes]
                    lat, lon = pts[len(pts) // 2]
                out.append(el)
        else:
            continue
        if tags.get("name") and tags.get("highway") not in ("street_lamp", "crossing", "traffic_signals"):
            label = "street" if kind == "road" else (tags.get("amenity") or tags.get("shop") or tags.get("tourism") or tags.get("leisure")
                                                     or tags.get("railway") or tags.get("highway") or kind).replace("_", " ")
            names.append((tags["name"], label, lat, lon))
        count += 1
        if count % 200000 == 0:
            print(f"  {count} map objects read, {time.time() - started:.0f}s", flush=True)
    print(f"{os.path.basename(path)}: {count} objects in {time.time() - started:.0f}s", flush=True)


def write_search(path, names):
    """Offline place search: one row per named place, and per street name every ~5 km so long roads show once per area."""
    seen, rows = set(), []
    for name, kind, lat, lon in names:
        key = (name.lower(), kind, round(lat / 0.05), round(lon / 0.05)) if kind == "street" else None
        if key and key in seen:
            continue
        seen.add(key)
        rows.append((name, name.lower(), kind, lat, lon))
    db = sqlite3.connect(path + ".tmp")
    db.execute("DROP TABLE IF EXISTS places")
    db.execute("CREATE TABLE places (name TEXT, lower TEXT, kind TEXT, lat REAL, lon REAL)")
    db.executemany("INSERT INTO places VALUES (?, ?, ?, ?, ?)", rows)
    db.commit()
    db.close()
    os.replace(path + ".tmp", path)
    return len(rows)


def search(path, q, near=None, limit=8):
    """Named places matching q from the offline index, names starting with q first, then the nearest."""
    db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        q = q.lower().replace("%", "").replace("_", "")
        rows = db.execute("SELECT name, kind, lat, lon, lower LIKE ? FROM places WHERE lower LIKE ? LIMIT 2000",
                          (q + "%", "%" + q + "%")).fetchall()
    finally:
        db.close()
    rows.sort(key=lambda r: (not r[4], (r[2] - near[0]) ** 2 + (r[3] - near[1]) ** 2 if near else 0))
    return [{"name": n, "pt": [lat, lon], "kind": k} for n, k, lat, lon, _ in rows[:limit]]


def main(args):
    folder = os.path.join(DATA, "tiles")
    marker = os.path.join(folder, "extract.json")
    last = json.load(open(marker)) if os.path.exists(marker) else {}
    box = tuple(last.get("bbox", NCR))
    if "--refresh" in args:
        args = [a for a in args if a != "--refresh"] or last.get("sources", [])
    if "--bbox" in args:
        i = args.index("--bbox")
        box = tuple(float(v) for v in args[i + 1].split(","))
        args = args[:i] + args[i + 2:]
    sources = args or GEOFABRIK
    os.makedirs(DATA, exist_ok=True)
    files = []
    for src in sources:
        if src.startswith("http"):
            path = os.path.join(DATA, src.rsplit("/", 1)[1])
            if not os.path.exists(path) or time.time() - os.path.getmtime(path) > DOWNLOAD_MAX_AGE:
                print(f"downloading {src} (a few hundred MB)", flush=True)
                urllib.request.urlretrieve(src, path + ".part")
                os.replace(path + ".part", path)
            files.append(path)
        else:
            files.append(src)
    elements, buildings, names = [], Counter(), []
    for f in files:
        read(f, box, elements, buildings, names)
    seen, unique = set(), []
    for el in elements:  # the zones overlap at their borders
        if (el["type"], el["id"]) not in seen:
            seen.add((el["type"], el["id"]))
            unique.append(el)
    roads = sum(1 for el in unique if "nodes" in el)
    # shortcut: a building on the border of two extracts is counted twice; there are few, and only the cell count moves
    unique += [{"type": "node", "id": f"b{i}_{j}", "lat": (i + 0.5) * CELL, "lon": (j + 0.5) * CELL,
                "tags": {"building": "yes", "building_count": str(c)}} for (i, j), c in buildings.items()]
    s, w, n, e = box
    tiles = {(i, j) for i in range(int(s // TILE), int(n // TILE) + 1) for j in range(int(w // TILE), int(e // TILE) + 1)}
    os.makedirs(folder, exist_ok=True)
    write_tiles(folder, unique, tiles)
    found = write_search(os.path.join(folder, "search.db"), names)
    old = last.get("tiles", []) if tuple(last.get("bbox", box)) == box else []
    with open(marker + ".tmp", "w") as f:
        json.dump({"tiles": sorted({tuple(t) for t in old} | tiles), "imported": time.strftime("%Y-%m-%d"), "files": files,
                   "sources": sources, "bbox": box}, f)
    os.replace(marker + ".tmp", marker)
    print(f"Done: {roads} streets, {sum(buildings.values())} buildings, {len(unique) - roads - len(buildings)} other map objects"
          f" and {found} searchable names in {len(tiles)} tiles.", flush=True)
    return unique


if __name__ == "__main__":
    main(sys.argv[1:])
