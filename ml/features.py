"""Training rows for the AI street model, built from a SafeRoute Area (the same street graph and sources the app uses).

Two kinds of row:
- one per street segment: what is mapped on and around it, plus the "lit" label where someone recorded it;
- one per segment and hour (a typical weekday and a Saturday): the time, plus labels measured at that hour.
A row's inputs never include the thing its label measures (no lit tag or light reports in the lit model's inputs,
no opening hours in the open-places model's, no buses in the bus model's), so the model can't just copy the answer.
"""
import math
from collections import defaultdict
from datetime import datetime, timedelta, timezone

import rating
from safety import SURROUNDINGS, daylight, is_open
from tiles import TILE

IST = timezone(timedelta(hours=5, minutes=30))
SLOT_DAYS = {0: datetime(2026, 11, 11, tzinfo=IST), 1: datetime(2026, 11, 14, tzinfo=IST)}  # weekend flag -> a Wednesday, a Saturday
HIGHWAYS = sorted(set(rating.ROAD_SCORE) | rating.MAIN_ROADS | {"motorway", "motorway_link", "trunk_link", "service"})
SIDEWALKS = {None: 0, "no": 1, "none": 1}  # anything else mapped means a footpath on at least one side (2)
NEAR_KINDS = ["lamp", "cctv", "guard", "sos_phone", "crossing", "blackspot", "waterlogging", "nightlife", "transit"]
GROUPS = ["food", "shop", "pharmacy", "hospital", "fuel", "other"]
ZONES = sorted(SURROUNDINGS)
CATEGORICAL = ["highway", "sidewalk"]
STATIC = (["highway", "sidewalk", "length", "lanes", "maxspeed", "unpaved", "underpass", "overbridge", "gap_m", "dead_ends",
           "buildings_ha", "nightlight", "police_m", "places_mapped_500m", "gtfs_stops"]
          + [f"n_{k}" for k in NEAR_KINDS] + ["lamps_per_100m"] + [f"places_{g}" for g in GROUPS] + ["places_total"]
          + [f"zone_{z}" for z in ZONES] + ["nb_lamps_per_100m", "nb_places", "nb_main_share", "degree"])
TIME = ["hour", "weekend", "daylight", "gtfs_deps"]
NAN = float("nan")


def edge_id(e):
    a, b = sorted((e["a"], e["b"]), key=str)
    return f"{a}-{b}"


def tile_of(e):
    return f"{int(e['mid'][0] // TILE)}_{int(e['mid'][1] // TILE)}"


def lit_label(e):
    """1 lit / 0 dark from people who looked: after-dark light reports win over the OSM lit tag. None when nobody recorded it."""
    seen = [rating.OBSERVATIONS[r["type"]][1] for r in rating.votes(e["reports"])
            if rating.OBSERVATIONS.get(r["type"], ("",))[0] == "lighting" and r.get("dark") is not False]
    if seen:
        return round(sum(seen) / len(seen)), "reports"
    if e["lit"] in rating.LIT_TAGS:
        return int(rating.LIT_TAGS[e["lit"]] >= 0.5), "osm"
    return None, None


def static_rows(area):
    """[dict] one per segment, inputs from STATIC plus id, tile, position and the lit label."""
    pois = area.pois
    deg = {n: len(v) for n, v in area.adj.items()}
    rows = []
    for e in area.edges:
        near = e["near"]
        places = [pois[i] for i in near["place"] + near["haven"]]
        r = {"edge_id": edge_id(e), "tile": tile_of(e), "lat": round(e["mid"][0], 6), "lon": round(e["mid"][1], 6),
             "highway": HIGHWAYS.index(e["highway"]) if e["highway"] in HIGHWAYS else len(HIGHWAYS),
             "sidewalk": SIDEWALKS.get(e["sidewalk"], 2), "length": round(e["length"], 1),
             "lanes": e.get("lanes") or NAN, "maxspeed": e.get("maxspeed") or NAN,
             "unpaved": int(e.get("surface") in rating.UNPAVED) if e.get("surface") else NAN,
             "underpass": int(e["underpass"]), "overbridge": int(e["overbridge"]), "gap_m": round(e["gap_m"], 1),
             "dead_ends": e["dead_ends"], "buildings_ha": round(e["buildings_ha"], 2),
             "nightlight": NAN if e.get("nightlight") is None else e["nightlight"],
             "police_m": min(5000, round(min(e["police_m"], 5000))), "places_mapped_500m": int(e["places_mapped"]),
             "gtfs_stops": len(e.get("stops", ()))}
        for k in NEAR_KINDS:
            r[f"n_{k}"] = len(near[k])
        r["lamps_per_100m"] = round(100 * len(near["lamp"]) / max(e["length"], 10), 3)
        for g in GROUPS:
            r[f"places_{g}"] = sum(p["group"] == g for p in places)
        r["places_total"] = len(places)
        for z in ZONES:
            r[f"zone_{z}"] = int(z in e["zones"])
        r["degree"] = deg.get(e["a"], 0) + deg.get(e["b"], 0)
        r["lit"], r["lit_source"] = lit_label(e)
        rows.append(r)
    # what the neighbouring streets look like: segments that share a junction with this one (a one-step graph view)
    by_node = defaultdict(list)
    for i, e in enumerate(area.edges):
        by_node[e["a"]].append(i)
        by_node[e["b"]].append(i)
    for i, e in enumerate(area.edges):
        nb = {j for n in (e["a"], e["b"]) for j in by_node[n]} - {i}
        r = rows[i]
        r["nb_lamps_per_100m"] = round(sum(rows[j]["lamps_per_100m"] for j in nb) / len(nb), 3) if nb else NAN
        r["nb_places"] = sum(rows[j]["places_total"] for j in nb)
        r["nb_main_share"] = round(sum(area.edges[j]["highway"] in rating.MAIN_ROADS for j in nb) / len(nb), 3) if nb else NAN
    return rows


def bus_rate(bus, mid, weekend, hour):
    """Buses an hour that moved through the cells around this point at this hour, averaged over the hours the
    logger watched; None when it never watched that hour."""
    if not bus or not bus["watched"].get((weekend, hour)):
        return None
    ci, cj = int(mid[0] // 0.001), int(mid[1] // 0.001)  # same ~110 m cells as buses.py
    total = sum(bus["counts"].get((ci + di, cj + dj, weekend, hour), 0) for di in (-1, 0, 1) for dj in (-1, 0, 1))
    return total / bus["watched"][(weekend, hour)]


def traffic_ratio(tr, mid, weekend, hour):
    """Mean current / free-flow speed TomTom measured at this hour in this point's cell, or None."""
    if not tr:
        return None
    got = tr.get((int(mid[0] // 0.001), int(mid[1] // 0.001), weekend, hour))
    return sum(got) / len(got) if got else None


def slot_rows(area, bus=None, tr=None):
    """[dict] one per segment and hour that has at least one measured label: open_share (share of nearby places with
    published hours that are open), bus_per_hour and traffic_ratio (from ml/collect.py logs). gtfs_deps (timetabled
    bus and metro departures nearby that hour) is an input."""
    rows = []
    for weekend, day in SLOT_DAYS.items():
        for hour in range(24):
            when = day.replace(hour=hour)
            light = round(daylight(*area.center, when), 3)
            wd = when.weekday()
            for e in area.edges:
                hours = [is_open(area.pois[i]["hours"], when) for i in e["near"]["place"] + e["near"]["haven"]]
                known = [h for h in hours if h is not None]
                b = bus_rate(bus, e["mid"], weekend, hour)
                t = traffic_ratio(tr, e["mid"], weekend, hour) if e["highway"] in rating.MAIN_ROADS else None
                if not known and b is None and t is None:
                    continue
                rows.append({"edge_id": edge_id(e), "hour": hour, "weekend": weekend, "daylight": light,
                             "gtfs_deps": sum(area.stops[i]["deps"][wd][hour] for i in e.get("stops", ())),
                             "open_share": round(sum(known) / len(known), 3) if known else NAN, "open_known": len(known),
                             "bus_per_hour": NAN if b is None else round(b, 3), "traffic_ratio": NAN if t is None else round(t, 3)})
    return rows


def load_bus_logs(counts_path, hours_path):
    """The logger's files -> {"counts": {(ci, cj, weekend, hour): buses summed over watched hours}, "watched": {(weekend, hour): n}}."""
    import csv
    import os
    if not (os.path.exists(counts_path) and os.path.exists(hours_path)):
        return None
    watched = defaultdict(int)
    with open(hours_path, newline="") as f:
        for row in csv.DictReader(f):
            t = datetime.fromisoformat(row["hour_start"])
            watched[(int(t.weekday() >= 5), t.hour)] += 1
    counts = defaultdict(int)
    with open(counts_path, newline="") as f:
        for row in csv.DictReader(f):
            t = datetime.fromisoformat(row["hour_start"])
            counts[(int(row["cell_i"]), int(row["cell_j"]), int(t.weekday() >= 5), t.hour)] += int(row["buses"])
    return {"counts": counts, "watched": watched} if watched else None


def load_traffic_logs(path):
    """traffic.csv -> {(ci, cj, weekend, hour): [speed ratios]}, or None."""
    import csv
    import os
    if not os.path.exists(path):
        return None
    out = defaultdict(list)
    with open(path, newline="") as f:
        for row in csv.DictReader(f):
            t = datetime.fromisoformat(row["hour_start"])
            out[(int(row["cell_i"]), int(row["cell_j"]), int(t.weekday() >= 5), t.hour)].append(float(row["speed_ratio"]))
    return out or None


def finite(x):
    return x is not None and not (isinstance(x, float) and math.isnan(x))
