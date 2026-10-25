"""One-time import of OpenStreetMap for a region, so every street, lamp, shop and opening hour loads instantly.

Usage: python import_osm.py [file.osm.pbf or URL ...] [--bbox south,west,north,east]
With no files it downloads the Geofabrik extracts that cover Delhi NCR (northern zone, plus central zone for Noida
and Greater Noida). Needs: pip install osmium. Re-run it monthly to pick up new edits.
"""
import json
import os
import re
import sys
import time
import urllib.request

from tiles import QUERY, TILE, write_tiles

NCR = (28.35, 76.85, 28.90, 77.60)
GEOFABRIK = ["https://download.geofabrik.de/asia/india/northern-zone-latest.osm.pbf",
             "https://download.geofabrik.de/asia/india/central-zone-latest.osm.pbf"]
DATA = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
KEYS = ("highway", "landuse", "natural", "leisure", "railway", "man_made", "amenity", "office", "shop", "tourism")

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
    if (tags.get("highway") in ("street_lamp", "bus_stop") or "office" in tags or "shop" in tags or tags.get("tourism") == "hotel"
            or tags.get("man_made") == "surveillance" or any(rx.match(tags.get(k, "")) for k, rx in POI.items())):
        return "poi"
    return None


def read(path, box, out):
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
            if kind == "poi" and obj.location.valid() and inside(obj.location.lat, obj.location.lon):
                out.append({"type": "node", "id": obj.id, "lat": obj.location.lat, "lon": obj.location.lon, "tags": tags})
        elif obj.is_way():
            try:
                pts = [(nd.lat, nd.lon) for nd in obj.nodes]
            except osmium.InvalidLocationError:
                continue  # the way leaves the downloaded file
            if not pts or not any(inside(*p) for p in pts):
                continue
            if kind == "poi":
                lat, lon = sum(p[0] for p in pts) / len(pts), sum(p[1] for p in pts) / len(pts)
                out.append({"type": "way", "id": obj.id, "center": {"lat": lat, "lon": lon}, "tags": tags})
            else:
                el = {"type": "way", "id": obj.id, "geometry": [{"lat": a, "lon": b} for a, b in pts], "tags": tags}
                if kind == "road":
                    el["nodes"] = [nd.ref for nd in obj.nodes]
                out.append(el)
        count += 1
        if count % 200000 == 0:
            print(f"  {count} map objects read, {time.time() - started:.0f}s", flush=True)
    print(f"{os.path.basename(path)}: {count} objects in {time.time() - started:.0f}s", flush=True)


def main(args):
    box = NCR
    if "--bbox" in args:
        i = args.index("--bbox")
        box = tuple(float(v) for v in args[i + 1].split(","))
        args = args[:i] + args[i + 2:]
    os.makedirs(DATA, exist_ok=True)
    files = []
    for src in args or GEOFABRIK:
        if src.startswith("http"):
            path = os.path.join(DATA, src.rsplit("/", 1)[1])
            if not os.path.exists(path):
                print(f"downloading {src} (a few hundred MB, once)", flush=True)
                urllib.request.urlretrieve(src, path + ".part")
                os.replace(path + ".part", path)
            files.append(path)
        else:
            files.append(src)
    elements = []
    for f in files:
        read(f, box, elements)
    seen, unique = set(), []
    for el in elements:  # the zones overlap at their borders
        if (el["type"], el["id"]) not in seen:
            seen.add((el["type"], el["id"]))
            unique.append(el)
    s, w, n, e = box
    tiles = {(i, j) for i in range(int(s // TILE), int(n // TILE) + 1) for j in range(int(w // TILE), int(e // TILE) + 1)}
    folder = os.path.join(DATA, "tiles")
    os.makedirs(folder, exist_ok=True)
    write_tiles(folder, unique, tiles)
    marker = os.path.join(folder, "extract.json")
    old = json.load(open(marker))["tiles"] if os.path.exists(marker) else []
    with open(marker, "w") as f:
        json.dump({"tiles": sorted({tuple(t) for t in old} | tiles), "imported": time.strftime("%Y-%m-%d"), "files": files}, f)
    roads = sum(1 for el in unique if "nodes" in el)
    print(f"Done: {roads} streets and {len(unique) - roads} other map objects in {len(tiles)} tiles. Restart the server.")
    return unique


if __name__ == "__main__":
    main(sys.argv[1:])
