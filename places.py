"""Opening hours from Google Places (key in data/google_places_key.txt or GOOGLE_PLACES_KEY, never sent to browsers).

For named places along a trip that publish no opening_hours: one Text Search per place (at most 15 a trip, 300 a day),
used only when Google's match is within 150 m. Runs before Gemini, so Gemini only guesses what Google didn't know.
Nothing from Google is drawn on the map; it only feeds the "places open" part of the score.
"""
import json
import os
import re
import threading
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor

from safety import DAYS, dist

URL = "https://places.googleapis.com/v1/places:searchText"
FIELDS = "places.displayName,places.location,places.regularOpeningHours.periods"
DAILY_CAP = 300  # Text Search with hours is a paid tier past the monthly free calls
MAX_PLACES = 15
MATCH_M = 150
# shortcut: Google's terms limit caching Places content; 30 days suits a demo, cut to a day (or none) before a real launch
HOURS_DAYS = 30


def to_osm(periods):
    """Google's weekly periods as an OSM opening_hours string, or "" when there are none."""
    if not periods:
        return ""
    if any("close" not in p for p in periods):
        return "24/7"  # Google marks always-open as a single period with no close
    days = {}
    for p in periods:
        o, c = p["open"], p["close"]
        days.setdefault((o["day"] + 6) % 7, []).append(f"{o['hour']:02d}:{o.get('minute', 0):02d}-{c['hour']:02d}:{c.get('minute', 0):02d}")
    return "; ".join(f"{DAYS[d]} {','.join(h)}" for d, h in sorted(days.items()))


def same_name(a, b):
    """Share a real word: the search box can return whatever business is nearest when the name isn't there."""
    words = lambda s: {w for w in re.findall(r"\w+", s.lower()) if len(w) > 2}
    return bool(words(a) & words(b))


class Places:
    def __init__(self, key="", path=None):
        self.key = key
        self.path = path
        self.hours = {}  # "name|lat|lon" -> [opening_hours or "", time asked]
        self.calls = []
        self.lock = threading.Lock()
        if path and os.path.exists(path):
            with open(path) as f:
                self.hours = json.load(f)

    def search(self, p):
        """Google's best match for a place near p, or None when the call fails or the daily cap is spent."""
        now = time.time()
        with self.lock:
            self.calls = [t for t in self.calls if now - t < 86400]
            if not self.key or len(self.calls) >= DAILY_CAP:
                return None
            self.calls.append(now)
        # a restriction, not a bias: a bias still returns the same name across town
        d = MATCH_M / 111000
        body = {"textQuery": p["name"], "maxResultCount": 1,
                "locationRestriction": {"rectangle": {"low": {"latitude": p["pt"][0] - d, "longitude": p["pt"][1] - d},
                                                      "high": {"latitude": p["pt"][0] + d, "longitude": p["pt"][1] + d}}}}
        req = urllib.request.Request(URL, json.dumps(body).encode(),
                                     {"Content-Type": "application/json", "X-Goog-Api-Key": self.key, "X-Goog-FieldMask": FIELDS})
        try:
            with urllib.request.urlopen(req, timeout=8) as r:
                return (json.load(r).get("places") or [{}])[0]
        except Exception as err:  # a bonus: never break a trip
            print("Google Places:", type(err).__name__, getattr(err, "code", ""), flush=True)
            if getattr(err, "code", 0) == 429:  # the Google project's own daily quota is spent: stop asking for a day
                with self.lock:
                    self.calls = [now] * DAILY_CAP
            return None

    def lookup(self, p):
        """OSM hours for p, "" if Google has none or matched something too far away, None if the call failed."""
        hit = self.search(p)
        if hit is None:
            return None
        loc = hit.get("location") or {}
        if not loc or dist(p["pt"], (loc["latitude"], loc["longitude"])) > MATCH_M or not same_name(p["name"], (hit.get("displayName") or {}).get("text", "")):
            return ""
        return to_osm((hit.get("regularOpeningHours") or {}).get("periods"))

    @staticmethod
    def _key(p):
        return f"{p['name']}|{p['pt'][0]:.4f}|{p['pt'][1]:.4f}"

    def fill_hours(self, area, near):
        """Give opening hours to named places without any, near the trip. Returns how many were filled."""
        if not self.key:
            return 0
        now, filled, todo = time.time(), 0, []
        for p in area.pois:
            if p["hours"] or not p.get("named") or p["group"] in ("transit", "cctv", "police") or not near(p["pt"]):
                continue
            got = self.hours.get(self._key(p))
            if got and now - got[1] < HOURS_DAYS * 86400:
                if got[0]:
                    p["hours"], p["google_hours"] = got[0], True
                    filled += 1
            else:
                todo.append(p)
        # spread the calls along the trip: the first 15 in map order all sit at one end of it
        todo = [todo[i * len(todo) // MAX_PLACES] for i in range(MAX_PLACES)] if len(todo) > MAX_PLACES else todo
        with ThreadPoolExecutor(5) as pool:
            answers = list(pool.map(self.lookup, todo))
        for p, oh in zip(todo, answers):
            if oh is None:
                continue  # a failed call is not cached, so it is asked again next time
            self.hours[self._key(p)] = [oh, now]
            if oh:
                p["hours"], p["google_hours"] = oh, True
                filled += 1
        if todo:
            self._save()
        if filled:
            area._cache.clear()
        return filled

    def _save(self):
        if not self.path:
            return
        with self.lock:
            tmp = self.path + ".tmp"
            with open(tmp, "w") as f:
                json.dump(self.hours, f)
            os.replace(tmp, self.path)


LIVE = Places()  # off until server.py gives it a key
