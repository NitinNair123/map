"""Departures per stop, weekday and hour from GTFS timetables (e.g. Delhi's Open Transit Data, DMRC metro)."""
import csv
import io
import json
import os
import zipfile
from collections import defaultdict

DAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]


def _rows(z, name):
    with z.open(name) as f:
        yield from csv.DictReader(io.TextIOWrapper(f, "utf-8-sig"))


def read_feed(path):
    """[{"lat", "lon", "name", "deps": [7][24] departures}] for every stop in one GTFS zip."""
    with zipfile.ZipFile(path) as z:
        names = set(z.namelist())
        days = {}  # service_id -> days of the week it runs
        if "calendar.txt" in names:
            for r in _rows(z, "calendar.txt"):
                days[r["service_id"]] = [d for d in range(7) if r.get(DAYS[d]) == "1"]
        # shortcut: services only in calendar_dates.txt count every day; read its dates if a feed relies on it
        trip_days = {r["trip_id"]: days.get(r["service_id"], list(range(7))) for r in _rows(z, "trips.txt")}
        deps = defaultdict(lambda: [[0] * 24 for _ in range(7)])
        for r in _rows(z, "stop_times.txt"):
            t = r.get("departure_time") or r.get("arrival_time")
            if not t:
                continue
            h = int(t.split(":")[0])
            for d in trip_days.get(r["trip_id"], ()):
                deps[r["stop_id"]][(d + h // 24) % 7][h % 24] += 1  # 25:10 is 01:10 the next day
        return [{"lat": float(r["stop_lat"]), "lon": float(r["stop_lon"]), "name": r.get("stop_name", ""), "deps": deps[r["stop_id"]]}
                for r in _rows(z, "stops.txt") if r["stop_id"] in deps and r.get("stop_lat")]


def load(folder):
    """Stops from every .zip in folder, cached beside them until a zip changes."""
    if not os.path.isdir(folder):
        return []
    zips = sorted(f for f in os.listdir(folder) if f.endswith(".zip"))
    stamp = [[f, os.path.getmtime(os.path.join(folder, f))] for f in zips]
    cache = os.path.join(folder, "stops.json")
    if os.path.exists(cache):
        with open(cache) as f:
            saved = json.load(f)
        if saved["stamp"] == stamp:
            return saved["stops"]
    stops = []
    for f in zips:
        try:
            stops += read_feed(os.path.join(folder, f))
            print(f"timetable {f}: {len(stops)} stops so far", flush=True)
        except (KeyError, ValueError, zipfile.BadZipFile) as err:
            print(f"timetable {f} skipped: {err}", flush=True)
    with open(cache + ".tmp", "w") as out:
        json.dump({"stamp": stamp, "stops": stops}, out)
    os.replace(cache + ".tmp", cache)
    return stops
