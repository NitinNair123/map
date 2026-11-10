"""Keeps logging live data for the AI street model, for as long as it runs (best on the always-on server):
- Delhi OTD live buses (DTC and cluster): how many buses move through each ~110 m cell every hour. Counts only,
  no vehicle ids, routes or tracks.
- TomTom traffic flow on main roads around the training spots: current / free-flow speed per cell each hour.
Stop with Ctrl+C; files are appended, so it carries on where it left off.
Usage: py ml/collect.py   (keys in data/otd_key.txt and data/tomtom_key.txt, or OTD_KEY / TOMTOM_KEY)
"""
import csv
import os
import sys
import threading
import time
import urllib.request
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import buses  # noqa: E402
import traffic  # noqa: E402
from dataset import SPOTS  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "ml", "data")
IST = timezone(timedelta(hours=5, minutes=30))
MIN_POLLS = 0.8 * 3600 / buses.POLL_S  # an hour counts only if the feed answered for at least 80% of it
# TomTom's free tier is 2,500 calls a day and the app's server uses the same key (up to traffic.DAILY_CAP), so the
# logger takes a small share: 20 points an hour = 480 a day. Raise it with TRAFFIC_PER_HOUR if the logger has its own key.
TRAFFIC_PER_HOUR = int(os.environ.get("TRAFFIC_PER_HOUR", 20))
OFFSETS = [(0, 0), (0.008, 0), (-0.008, 0), (0, 0.008), (0, -0.008), (0.006, 0.006), (-0.006, -0.006), (0.006, -0.006), (-0.006, 0.006)]
TRAFFIC_POINTS = [(round(a + da, 4), round(b + db, 4)) for a, b in SPOTS for da, db in OFFSETS]  # TomTom snaps each to the nearest road


def hour_counts(live, start, end):
    """{cell: buses that moved through it between start and end}, from LiveBuses' cells."""
    with live.lock:
        return {c: n for c, seen in live.cells.items() if (n := sum(start <= t < end for t in seen.values()))}


def append(path, header, rows):
    new = not os.path.exists(path)
    with open(path, "a", newline="") as f:
        w = csv.writer(f)
        if new:
            w.writerow(header)
        w.writerows(rows)


def flush(live, hour_start, polls):
    end = hour_start + 3600
    stamp = datetime.fromtimestamp(hour_start, IST).isoformat(timespec="minutes")
    if polls >= MIN_POLLS:
        counts = hour_counts(live, hour_start, end)
        append(os.path.join(OUT, "bus_counts.csv"), ["hour_start", "cell_i", "cell_j", "buses"],
               [(stamp, c[0], c[1], n) for c, n in sorted(counts.items())])
        append(os.path.join(OUT, "bus_hours.csv"), ["hour_start", "polls", "cells"], [(stamp, polls, len(counts))])
        print(f"{stamp}: {sum(counts.values())} bus passes in {len(counts)} cells", flush=True)
    else:
        print(f"{stamp}: feed answered {polls} times, too few, hour not used", flush=True)
    live.prune(end + buses.WINDOW_S)  # prune() keeps the last WINDOW_S before the time it's given


def key(name, env):
    path = os.path.join(ROOT, "data", name)
    return os.environ.get(env) or (open(path).read().strip() if os.path.exists(path) else "")


def log_traffic(tomtom, hour_n):
    """This hour's share of the traffic points (rotating through all of them), written as soon as they answer."""
    if not tomtom.key:
        return
    k = TRAFFIC_PER_HOUR
    picks = [TRAFFIC_POINTS[(hour_n * k + i) % len(TRAFFIC_POINTS)] for i in range(min(k, len(TRAFFIC_POINTS)))]
    tomtom.cells.clear()
    for p in picks:
        tomtom._fetch(p)
    stamp = datetime.now(IST).replace(minute=0, second=0, microsecond=0).isoformat(timespec="minutes")
    append(os.path.join(OUT, "traffic.csv"), ["hour_start", "cell_i", "cell_j", "speed_ratio"],
           [(stamp, c[0], c[1], round(r, 3)) for c, (r, _) in sorted(tomtom.cells.items())])
    print(f"{stamp}: traffic on {len(tomtom.cells)} cells from {len(picks)} points", flush=True)


def main():
    key_otd = key("otd_key.txt", "OTD_KEY")
    tomtom = traffic.LiveTraffic(key("tomtom_key.txt", "TOMTOM_KEY"))
    if not key_otd and not tomtom.key:
        sys.exit("No keys: put them in data/otd_key.txt and data/tomtom_key.txt")
    print("Live buses:", "on" if key_otd else "off", "| Traffic:", f"on, {TRAFFIC_PER_HOUR} points an hour" if tomtom.key else "off", flush=True)
    os.makedirs(OUT, exist_ok=True)
    live = buses.LiveBuses(key_otd)
    threading.Thread(target=traffic_loop, args=(tomtom,), daemon=True).start()
    if not key_otd:
        threading.Event().wait()
    hour_start, polls = time.time() // 3600 * 3600 + 3600, 0  # the first, partial hour only warms up positions
    print("Logging bus counts; the first full hour starts at", datetime.fromtimestamp(hour_start, IST).strftime("%H:%M"), flush=True)
    while True:
        now = time.time()
        if now >= hour_start + 3600:
            flush(live, hour_start, polls)
            hour_start, polls = now // 3600 * 3600, 0
        try:
            with urllib.request.urlopen(buses.URL.format(key_otd), timeout=15) as r:
                live.record(buses.decode(r.read()), now)
            polls += now >= hour_start
        except Exception as err:  # the feed goes down at times; keep polling
            print("feed:", type(err).__name__, flush=True)
        if len(live.cells) > 50000:
            live.prune(hour_start + buses.WINDOW_S)  # keep only this hour's passes in memory
        time.sleep(buses.POLL_S)


def traffic_loop(tomtom):
    while True:
        log_traffic(tomtom, int(time.time() // 3600))
        time.sleep(3600 - time.time() % 3600 + 60)  # once each hour, a minute past


if __name__ == "__main__":
    main()
