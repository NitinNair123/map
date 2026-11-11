"""More street parameters from the data the app already pulls, found by looking around each street instead of at it.

Each one is a measured count or distance on the mapped data, with the same rule as everywhere else: where a source has
nothing mapped, the value says "none within 500 m" only if places are mapped there at all; otherwise it is unknown.
  counts   lamps, cameras, places, nightlife (bars, liquor shops) and stops within 100 / 250 / 500 m
  nearest  distance (up to 500 m) to the nearest place, food, pharmacy, hospital, petrol pump, stop, camera, lamp, guard
           post, emergency phone, nightlife, blackspot, waterlogging spot and main road
  streets  road length, main-road / footpath / service-lane share, junctions and dead-end share within ~300 m
  buildings buildings within 100 m and 500 m
Whether each helps is not assumed: ml/train.py reports how much the held-out score drops when a column is scrambled.
"""
import math
from collections import defaultdict

import rating

NAN = float("nan")
CAP = 500.0
NEAREST = {"d_place": ("place", "haven"), "d_food": ("food",), "d_pharmacy": ("pharmacy",), "d_hospital": ("hospital",), "d_fuel": ("fuel",),
           "d_transit": ("transit",), "d_cctv": ("cctv",), "d_lamp": ("lamp",), "d_guard": ("guard",), "d_sos": ("sos_phone",),
           "d_nightlife": ("nightlife",), "d_blackspot": ("blackspot",), "d_waterlogging": ("waterlogging",)}
COUNTS = {"lamps_100": ("lamp", 100), "lamps_250": ("lamp", 250), "cctv_250": ("cctv", 250), "cctv_500": ("cctv", 500),
          "places_250": ("place", 250), "places_500": ("place", 500), "nightlife_250": ("nightlife", 250),
          "nightlife_500": ("nightlife", 500), "transit_250": ("transit", 250), "transit_500": ("transit", 500)}
STREETS = ["road_km_300", "main_share_300", "foot_share_300", "service_share_300", "junctions_300", "dead_end_share_300", "d_main"]
BUILDINGS = ["bld_100", "bld_500"]
CHECKS = defaultdict(list)  # map-object kind -> [(count column, radius in metres)]
for _name, (_kind, _radius) in COUNTS.items():
    for _k in ((_kind, "haven") if _kind == "place" else (_kind,)):
        CHECKS[_k].append((_name, _radius))
COLUMNS = list(COUNTS) + list(NEAREST) + STREETS + BUILDINGS
FOOT = {"footway", "path", "steps", "cycleway", "pedestrian"}


def _meters(a, b):
    dy = (a[0] - b[0]) * 111000
    dx = (a[1] - b[1]) * 111000 * math.cos(math.radians((a[0] + b[0]) / 2))
    return math.hypot(dx, dy)


def add(area, rows):
    """Fills the context columns of the street rows in place; rows[i] belongs to area.edges[i]."""
    cell = area._cell
    grid = defaultdict(list)
    main_grid = defaultdict(list)
    for i, e in enumerate(area.edges):
        grid[cell(e["mid"])].append(i)
        if e["highway"] in rating.MAIN_ROADS:
            main_grid[cell(e["mid"])].append(i)
    deg = {n: len(v) for n, v in area.adj.items()}
    for i, (e, r) in enumerate(zip(area.edges, rows)):
        mid = e["mid"]
        mapped = e.get("places_mapped", True)  # with no place mapped within 500 m the block is unmapped, not empty
        best, count = {}, defaultdict(int)
        for pi, d in area._near(mid, CAP):
            p = area.pois[pi]
            for kind in {p["kind"], p["group"]}:
                if d < best.get(kind, CAP + 1):
                    best[kind] = d
            for name, radius in CHECKS.get(p["kind"], ()):
                if d <= radius:
                    count[name] += 1
        for name, (kind, _) in COUNTS.items():
            r[name] = count[name] if (mapped or kind not in ("place",)) else NAN
        for name, kinds in NEAREST.items():
            d = min((best[k] for k in kinds if k in best), default=CAP)  # nothing within 500 m: the cap
            r[name] = round(d) if (mapped or not name.startswith(("d_place", "d_food", "d_pharmacy", "d_hospital", "d_fuel"))) else NAN
        ci, cj = cell(mid)
        near = [j for di in (-1, 0, 1) for dj in (-1, 0, 1) for j in grid.get((ci + di, cj + dj), ())]
        total = sum(area.edges[j]["length"] for j in near) or 1.0
        r["road_km_300"] = round(total / 1000, 3)
        r["main_share_300"] = round(sum(area.edges[j]["length"] for j in near if area.edges[j]["highway"] in rating.MAIN_ROADS) / total, 3)
        r["foot_share_300"] = round(sum(area.edges[j]["length"] for j in near if area.edges[j]["highway"] in FOOT) / total, 3)
        r["service_share_300"] = round(sum(area.edges[j]["length"] for j in near if area.edges[j]["highway"] == "service") / total, 3)
        r["junctions_300"] = sum((deg.get(area.edges[j]["a"], 0) >= 3) + (deg.get(area.edges[j]["b"], 0) >= 3) for j in near) // 2
        r["dead_end_share_300"] = round(sum(area.edges[j]["dead_ends"] > 0 for j in near) / len(near), 3)
        k = int(CAP / 100) + 1
        r["d_main"] = round(min((_meters(mid, area.edges[j]["mid"]) for di in range(-k, k + 1) for dj in range(-k, k + 1)
                                 for j in main_grid.get((ci + di, cj + dj), ())), default=CAP))
        r["d_main"] = min(r["d_main"], CAP)
        r["bld_100"] = area.building_cells.get((ci, cj), 0)
        r["bld_500"] = sum(area.building_cells.get((ci + di, cj + dj), 0) for di in range(-2, 3) for dj in range(-2, 3))
    return rows
