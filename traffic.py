"""Live road traffic from TomTom's Traffic Flow API (free key from developer.tomtom.com, 2,500 calls a day).

Slow, heavy traffic means many vehicles on that road right now, a rough sign it isn't deserted. Free-flowing
traffic says nothing either way (a fast road can be full or empty), so it is never counted as empty.
"""
import json
import threading
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor

from buses import _cell, is_now

URL = "https://api.tomtom.com/traffic/services/4/flowSegmentData/absolute/10/json?point={},{}&unit=KMPH&key={}"
FRESH_S = 600
DAILY_CAP = 2000  # stay under the free 2,500 calls a day
SAMPLES = 6  # main roads looked up per route request
JAMMED = 0.8  # moving slower than this share of the free-flow speed


class LiveTraffic:
    def __init__(self, key=""):
        self.key = key
        self.cells = {}  # cell -> (current / free-flow speed, time checked)
        self.calls = []  # times of calls in the last day
        self.lock = threading.Lock()

    def record(self, flow, now=None):
        """Keep one flowSegmentData answer for every cell its road segment passes."""
        now = now or time.time()
        f = flow.get("flowSegmentData") or {}
        if f.get("roadClosure") or not f.get("freeFlowSpeed") or f.get("confidence", 0) < 0.5:
            return  # low confidence means TomTom filled in its usual speed, not a live one
        ratio = f["currentSpeed"] / f["freeFlowSpeed"]
        with self.lock:
            for c in (f.get("coordinates") or {}).get("coordinate", []):
                self.cells[_cell(c["latitude"], c["longitude"])] = (ratio, now)

    def jammed(self, pt, when=None, now=None):
        """True when a fresh live reading says traffic at pt is slow and heavy; None when unknown."""
        now = now or time.time()
        if not self.cells or not is_now(when, now):
            return None
        got = self.cells.get(_cell(*pt))
        return got[0] < JAMMED if got and now - got[1] < FRESH_S else None

    def _fetch(self, pt):
        try:
            with urllib.request.urlopen(URL.format(pt[0], pt[1], self.key), timeout=5) as r:
                self.record(json.load(r))
        except Exception as err:  # traffic is a bonus: a failed call just leaves the road unknown
            print("Live traffic:", type(err).__name__, flush=True)

    def warm(self, points):
        """Look up the given main-road points that have no fresh reading, within the daily cap."""
        if not self.key:
            return
        now = time.time()
        with self.lock:
            self.calls = [t for t in self.calls if now - t < 86400]
            todo = [p for p in points if now - self.cells.get(_cell(*p), (0, 0))[1] >= FRESH_S][:max(0, DAILY_CAP - len(self.calls))]
            self.calls += [now] * len(todo)
        with ThreadPoolExecutor(SAMPLES) as pool:
            list(pool.map(self._fetch, todo))


def sample(area, a, b):
    """Up to SAMPLES main-road points along the straight line from a to b, at least 400 m apart."""
    from safety import dist, seg_dist
    import rating
    picks = []
    for e in sorted(area.edges, key=lambda e: dist(a, e["mid"])):
        if e["highway"] in rating.MAIN_ROADS and seg_dist(e["mid"], a, b) <= 300 and all(dist(e["mid"], p) >= 400 for p in picks):
            picks.append(e["mid"])
            if len(picks) == SAMPLES:
                break
    return picks


LIVE = LiveTraffic()  # off until server.py gives it a key
