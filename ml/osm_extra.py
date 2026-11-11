"""Street surroundings from OpenStreetMap that the app does not use, fetched here for training only:
walls and fences (nobody can see over them), drains and canals, parking lots, public toilets, vending machines and
bus shelters. One Overpass request per block of tiles, cached in ml/data/osm_extra/ so a re-run costs nothing.
A block that can't be fetched leaves its columns unknown (NaN), never zero.
"""
import json
import math
import os
import threading
import time
import urllib.parse
import urllib.request
from collections import defaultdict

import tiles
from tiles import MIRRORS, TILE

NAN = float("nan")
QUERY = """
[out:json][timeout:50];
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


# the app's mirrors plus lz4.overpass-api.de (the main project's second server; the others are slow volunteers)
HOSTS = [MIRRORS[0], "https://lz4.overpass-api.de/api/interpreter", *MIRRORS[1:]]
ATTEMPT_S = 60      # one request to one mirror; the query itself is limited to 50 s, so a mirror that sits on it is given up on
DEADLINE_S = 240    # the most one box may cost in total, so a bad hour on Overpass can't freeze a block for ten minutes
COOL_AFTER = 2      # a mirror that failed this many times in a row is left alone for a while ...
COOL_S = 120        # ... first for this long, doubling each time it fails again (up to 15 minutes)
HEALTH = {}         # url -> {"fails": in a row, "until": when it may be tried again, "cool": current pause}
_lock = threading.Lock()


def _mark(url, ok):
    with _lock:
        h = HEALTH.setdefault(url, {"fails": 0, "until": 0.0, "cool": COOL_S})
        if ok:
            h.update(fails=0, until=0.0, cool=COOL_S)
        else:
            h["fails"] += 1
            if h["fails"] >= COOL_AFTER:
                h.update(until=time.time() + h["cool"], cool=min(h["cool"] * 2, 900))


def _ready():
    """Mirrors that are not cooling down, the one that succeeded most recently (fails == 0) first."""
    now = time.time()
    with _lock:
        return sorted((u for u in HOSTS if HEALTH.get(u, {}).get("until", 0) <= now), key=lambda u: HEALTH.get(u, {}).get("fails", 0))


def _ask(box, url):
    """One request to one mirror; raises OSError or ValueError when it fails. Counted for the Studio, so a slow
    request shows as 'in flight' instead of the build looking frozen."""
    data = urllib.parse.urlencode({"data": QUERY.format(bbox=",".join(f"{x:.5f}" for x in box))}).encode()
    token = tiles.stats_begin("osm_extra", f"{url.split('/')[2]} {box[0]:.2f},{box[1]:.2f}")
    try:
        req = urllib.request.Request(url, data, {"User-Agent": "SafeRoute/1.0 (student safety app; ml training)"})
        with urllib.request.urlopen(req, timeout=ATTEMPT_S) as r:
            osm = json.load(r)
        if "runtime error" in osm.get("remark", ""):
            raise ValueError(osm["remark"])
    except (OSError, ValueError) as err:  # URLError, HTTP 502/504, timeouts, cut connections, bad JSON
        tiles.stats_end(token, err=err)
        _mark(url, False)
        raise
    tiles.stats_end(token, items=len(osm["elements"]))
    _mark(url, True)
    return osm


def _quarters(box):
    s, w, n, e = box
    ms, mw = round((s + n) / 2, 5), round((w + e) / 2, 5)
    return [(s, w, ms, mw), (s, mw, ms, e), (ms, w, n, mw), (ms, mw, n, e)]


def _try(box, until):
    """Every mirror that is not cooling down, healthiest first, with a growing pause between rounds, until `until`."""
    pause = 3
    while time.time() < until:
        ready = _ready()
        if not ready:  # all cooling down: wait for the first to come back, if that is soon enough
            soonest = min(HEALTH[u]["until"] for u in HOSTS)
            if soonest > until:
                return None
            time.sleep(max(1, soonest - time.time()))
            continue
        for url in ready:
            if time.time() >= until:
                break
            try:
                return _ask(box, url)
            except (OSError, ValueError) as err:
                print(f"osm_extra {box[0]:.2f},{box[1]:.2f}: {url.split('/')[2]} failed ({err})", flush=True)
                time.sleep(pause)
                pause = min(pause * 2, 20)
    return None


def fetch(box, deadline=DEADLINE_S):
    """The raw Overpass answer for one (south, west, north, east) box: healthy mirrors first, a mirror that keeps failing
    is rested, and if the whole box can't be had in time it is asked for as four quarters (smaller queries get through
    when the servers are overloaded). None if any part could not be fetched: the block's columns then stay unknown (NaN)
    and the repair pass in dataset.py asks again later."""
    until = time.time() + deadline
    osm = _try(box, time.time() + deadline * 0.45)
    if osm is not None:
        return osm
    parts = []
    for q in _quarters(box):
        part = _try(q, until)
        if part is None:
            return None
        parts.append(part)
    seen, elements = set(), []
    for part in parts:
        for el in part["elements"]:
            k = (el["type"], el["id"])  # a way crossing a quarter's edge comes back in both
            if k not in seen:
                seen.add(k)
                elements.append(el)
    return {"elements": elements}


def load(tiles_, folder=FOLDER, getter=fetch):
    """The extra map objects for a group of tiles: cached on disk, fetched once otherwise."""
    box = (min(t[0] for t in tiles_) * TILE, min(t[1] for t in tiles_) * TILE, (max(t[0] for t in tiles_) + 1) * TILE, (max(t[1] for t in tiles_) + 1) * TILE)
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
