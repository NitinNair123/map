"""Police-recorded crime against women per district, shown beside routes as background. It never changes a score:
NCRB counts are yearly, district-wide and a year or more old, so they say nothing about one street tonight.

python crime.py downloads NCRB's district tables (India Data Portal mirror, CSV) and the district outlines
(OpenStreetMap, via Overpass and Nominatim) into data/crime/districts.json. The server reads that file."""
import csv
import io
import json
import os
import re
import time
import urllib.parse
import urllib.request

from tiles import MIRRORS

DATA = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "crime", "districts.json")
PORTAL = "https://ckandev.indiadataportal.com/dataset/e311a510-ce48-4f4c-baf6-0ec5f9278285/resource/"
TABLES = [  # "Districtwise Crime Against Women 2017 onwards" and "... 2024 Onwards"
    PORTAL + "c8d3ea7e-4855-45f3-9c26-557419e93b6a/download/districtwise-crime-against-women-2017-onwards.csv",
    PORTAL + "d8bda878-37a7-4320-8413-36d741bd9b27/download/districtwise-crime-against-women-2024-onwards.csv",
]
BOX = (28.2, 76.8, 28.95, 77.75)  # Delhi NCR
STATES = {"Delhi", "NCT of Delhi", "Uttar Pradesh", "Haryana"}
# The two street-harassment heads: assault to outrage modesty (IPC 354 with 354A-D, BNS 74-78) and insulting modesty
# (IPC 509, BNS 79). The 2024 table splits 354 into five heads with new names, so both spellings are listed.
COUNTS = {"assault": ("assault_on_womenabove_18", "assault_on_women_below_18", "assault_outrage_modesty_women",
                      "assault_women_outrage_modesty_girls", "assault_criminal_force_disrobe_women", "assault_criminal_force_disrobe_girls",
                      "sexual_harassment_women", "sexual_harassment_girls", "voyeurism_women", "voyeurism_girls", "stalking_women", "stalking_girls"),
          "insult": ("insult_to_the_modesty_of_women_above_18", "insult_to_the_modesty_of_women_below_18", "insult_modesty_women",
                     "insult_modesty_women_girls")}
UA = {"User-Agent": "SafeRoute/1.0 (student safety app; github.com/NitinNair123/map)"}


def key(name):
    """'South-East', 'South East Delhi' and 'Gautambudh Nagar' / 'Gautam Buddha Nagar' match."""
    k = re.sub(r"[^a-z]", "", name.lower().replace("district", ""))
    return {"gautambudhnagar": "gautambuddhanagar", "gurgaon": "gurugram"}.get(k, k.removesuffix("delhi") or k)


def counts(rows):
    """{key(police district): {year: {"assault", "insult"}}} for NCR states. Rows are per police district
    ('registration_circles'): in Delhi these are Delhi Police districts, elsewhere they equal the district."""
    out, seen = {}, set()
    for r in rows:
        row = (r.get("year"), r.get("state_name"), r.get("registration_circles"))
        if r.get("state_name") not in STATES or row in seen:  # the two tables may share a year
            continue
        seen.add(row)
        if not any(c in r for cols in COUNTS.values() for c in cols):
            continue  # a table with none of these columns says nothing, rather than zero
        year = out.setdefault(key(r["registration_circles"]), {}).setdefault(r["year"], {"assault": 0, "insult": 0})
        for name, cols in COUNTS.items():
            year[name] += sum(int(float(r.get(c) or 0)) for c in cols)
    return out


def get(url, data=None):
    with urllib.request.urlopen(urllib.request.Request(url, data, UA), timeout=60) as r:
        return r.read()


def fetch():
    rows = []
    for url in TABLES:
        try:
            rows += csv.DictReader(io.StringIO(get(url).decode("utf-8-sig")))
        except OSError as err:
            print(f"{url.rsplit('/', 1)[1]}: {err}", flush=True)
    if not rows:
        raise SystemExit("couldn't download the NCRB tables")
    by_district = counts(rows)
    q = f'[out:json][timeout:60];rel["boundary"="administrative"]["admin_level"="5"]({",".join(map(str, BOX))});out tags;'
    for url in MIRRORS:  # a busy mirror answers 502 or 504
        try:
            rels = json.loads(get(url, urllib.parse.urlencode({"data": q}).encode()))["elements"]
            break
        except (OSError, ValueError) as err:
            print(f"{url}: {err}", flush=True)
    else:
        raise SystemExit("no Overpass mirror answered, try again later")
    ids = [f"R{e['id']}" for e in rels if key(e["tags"].get("name", "")) in by_district]
    print("no NCRB rows for:", sorted(e["tags"].get("name", "?") for e in rels if f"R{e['id']}" not in ids), flush=True)
    districts = []
    for i in range(0, len(ids), 40):
        time.sleep(1)  # Nominatim allows one request a second
        q = urllib.parse.urlencode({"osm_ids": ",".join(ids[i:i + 40]), "format": "json", "polygon_geojson": 1, "polygon_threshold": 0.0005})
        for p in json.loads(get("https://nominatim.openstreetmap.org/lookup?" + q)):
            name = p.get("name") or p["display_name"].split(",")[0]
            districts.append({"name": name, "shape": p["geojson"], "years": by_district[key(name)]})
    out = {"source": "NCRB Crime in India, district tables (via India Data Portal); district outlines © OpenStreetMap contributors",
           "fetched": time.strftime("%Y-%m-%d"), "districts": districts}
    os.makedirs(os.path.dirname(DATA), exist_ok=True)
    with open(DATA, "w") as f:
        json.dump(out, f)
    for d in districts:
        y = max(d["years"])
        print(f"{d['name']}: {y} assault {d['years'][y]['assault']}, insult {d['years'][y]['insult']}", flush=True)
    return out


def inside(pt, shape):
    """Even-odd ray test over every ring, so holes and multipolygons need no special case."""
    lat, lon = pt
    polys = shape["coordinates"] if shape["type"] == "MultiPolygon" else [shape["coordinates"]] if shape["type"] == "Polygon" else []
    hit = False
    for ring in (r for p in polys for r in p):
        for (x1, y1), (x2, y2) in zip(ring, ring[1:] + ring[:1]):
            if (y1 > lat) != (y2 > lat) and lon < x1 + (lat - y1) * (x2 - x1) / (y2 - y1):
                hit = not hit
    return hit


def load(path=DATA):
    if not os.path.exists(path):
        return None
    with open(path) as f:
        return json.load(f)


def around(data, pts):
    """Background figures for the districts these points fall in, latest year first, or [] without the file."""
    out = []
    for d in (data or {}).get("districts", []):
        if any(inside(p, d["shape"]) for p in pts) and d["years"]:
            years = sorted(d["years"], reverse=True)
            out.append({"district": d["name"], "year": int(years[0]), **d["years"][years[0]],
                        "previous": {"year": int(years[1]), **d["years"][years[1]]} if len(years) > 1 else None,
                        "source": data["source"]})
    return out


if __name__ == "__main__":
    fetch()
