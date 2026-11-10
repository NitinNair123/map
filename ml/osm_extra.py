"""Street surroundings from OpenStreetMap that the app does not use, fetched here for training only:
walls and fences (nobody can see over them), drains and canals, parking lots, public toilets, vending machines and
bus shelters. One Overpass request per block of tiles, cached in ml/data/osm_extra/ so a re-run costs nothing.
A block that can't be fetched leaves its columns unknown (NaN), never zero.
"""
import json
import math
import os
import time
import urllib.parse
import urllib.request
from collections import defaultdict

from tiles import MIRRORS, TILE

NAN = float("nan")
QUERY = """
[out:json][timeout:90];
(
  way["barrier"~"^(wall|retaining_wall|fence)$"]({bbox});
  way["waterway"~"^(drain|canal|ditch|stream|river)$"]({bbox});
);
out tags geom;
nwr["amenity"~"^(parking|toilets|vending_machine|shelter)$"]({bbox});
out center tags;
"""
LINES = {"wall": ("wall_m_50", 50), "fence": ("fence_m_50", 50), "water": ("water_m_50", 50)}
POINTS = {"parking": ("parking_100", 100), "toilets": ("toilets_250", 250), "vending_machine": ("vending_250", 250),
          "shelter": ("shelter_250", 250)}
COLUMNS = [c for c, _ in LINES.values()] + ["water_m_150"] + [c for c, _ in POINTS.values()]
FOLDER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "osm_extra")


def _meters(a, b):
    return math.hypot((a[0] - b[0]) * 111000, (a[1] - b[1]) * 111000 * math.cos(math.radians((a[0] + b[0]) / 2)))


def fetch(box):
    """The raw Overpass answer for one (south, west, north, east) box, trying each mirror in turn; None if all fail."""
    data = urllib.parse.urlencode({"data": QUERY.format(bbox=",".join(f"{x:.5f}" for x in box))}).encode()
    for url in MIRRORS + MIRRORS[:1]:
        try:
            req = urllib.request.Request(url, data, {"User-Agent": "SafeRoute/1.0 (student safety app; ml training)"})
            with urllib.request.urlopen(req, timeout=120) as r:
                osm = json.load(r)
            if "runtime error" in osm.get("remark", ""):
                raise ValueError(osm["remark"])
            return osm
        except (OSError, ValueError) as err:
            print(f"osm_extra {box[0]:.2f},{box[1]:.2f}: {url.split('/')[2]} failed ({err})", flush=True)
            time.sleep(2)
    return None


def load(tiles, folder=FOLDER, getter=fetch):
    """The extra map objects for a group of tiles: cached on disk, fetched once otherwise."""
    box = (min(t[0] for t in tiles) * TILE, min(t[1] for t in tiles) * TILE, (max(t[0] for t in tiles) + 1) * TILE, (max(t[1] for t in tiles) + 1) * TILE)
    path = os.path.join(folder, "_".join(str(round(x / TILE)) for x in box) + ".json")
    if os.path.exists(path):
        with open(path) as f:
            return json.load(f)
    osm = getter(box)
    if osm is None:
        return None
    os.makedirs(folder, exist_ok=True)
    with open(path + ".part", "w") as f:
        json.dump(osm, f)
    os.replace(path + ".part", path)
    return osm


def add(rows, osm):
    """Fills the columns of the street rows in place (each has lat and lon) from an Overpass answer; returns True if it did."""
    for r in rows:
        r.update({c: NAN for c in COLUMNS})
    if not osm:
        return False
    cell = lambda p: (int(p[0] // 0.001), int(p[1] // 0.001))
    seg, pts = defaultdict(lambda: defaultdict(list)), defaultdict(lambda: defaultdict(list))
    for el in osm["elements"]:
        tags = el.get("tags", {})
        if el["type"] == "way" and "geometry" in el:
            kind = {"wall": "wall", "retaining_wall": "wall", "fence": "fence"}.get(tags.get("barrier")) or ("water" if "waterway" in tags else None)
            if kind:
                line = [(g["lat"], g["lon"]) for g in el["geometry"]]
                for a, b in zip(line, line[1:]):
                    mid = ((a[0] + b[0]) / 2, (a[1] + b[1]) / 2)
                    seg[kind][cell(mid)].append((mid, _meters(a, b)))
                continue
        kind = tags.get("amenity")
        if kind in POINTS:
            p = (el["lat"], el["lon"]) if "lat" in el else (el["center"]["lat"], el["center"]["lon"]) if "center" in el else None
            if p:
                pts[kind][cell(p)].append(p)
    for r in rows:
        here = (r["lat"], r["lon"])
        ci, cj = cell(here)

        def around(index, radius):
            k = int(radius / 100) + 1
            return [x for di in range(-k, k + 1) for dj in range(-k, k + 1) for x in index.get((ci + di, cj + dj), ())]
        for kind, (col, radius) in LINES.items():
            r[col] = round(sum(n for m, n in around(seg[kind], radius) if _meters(here, m) <= radius))
        r["water_m_150"] = round(sum(n for m, n in around(seg["water"], 150) if _meters(here, m) <= 150))
        for kind, (col, radius) in POINTS.items():
            r[col] = sum(_meters(here, p) <= radius for p in around(pts[kind], radius))
    return True
