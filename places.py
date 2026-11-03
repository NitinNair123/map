"""Opening hours from Google Places (key in data/google_places_key.txt or GOOGLE_PLACES_KEY, never sent to browsers).

For named places along a trip that publish no opening_hours: Nearby Search circles along the trip, each returning the
20 nearest Google places (at most 40 calls a trip, 300 a day). A place takes Google's hours only when a result within
80 m shares a word of its name. Runs on the shops the scanned routes pass, then the routes are scored again.
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

URL = "https://places.googleapis.com/v1/places:searchNearby"
FIELDS = "places.displayName,places.location,places.regularOpeningHours.periods"
DAILY_CAP = 300  # Nearby Search with hours is a paid tier past the monthly free calls
MAX_CALLS = 40  # per trip; each covers a 50 m circle (busy streets have more than 20 places in 100 m)
RADIUS_M = 50
MATCH_M = 80  # map data and Google can place the same shop a few doors apart
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

    def nearby(self, center):
        """Up to 20 Google places nearest to center (within RADIUS_M), or None when the call fails or the cap is spent."""
        now = time.time()
        with self.lock:
            self.calls = [t for t in self.calls if now - t < 86400]
            if not self.key or len(self.calls) >= DAILY_CAP:
                return None
            self.calls.append(now)
        body = {"maxResultCount": 20, "rankPreference": "DISTANCE",
                "locationRestriction": {"circle": {"center": {"latitude": center[0], "longitude": center[1]}, "radius": float(RADIUS_M)}}}
        req = urllib.request.Request(URL, json.dumps(body).encode(),
                                     {"Content-Type": "application/json", "X-Goog-Api-Key": self.key, "X-Goog-FieldMask": FIELDS})
        try:
            with urllib.request.urlopen(req, timeout=8) as r:
                return json.load(r).get("places") or []
        except Exception as err:  # a bonus: never break a trip
            print("Google Places:", type(err).__name__, getattr(err, "code", ""), flush=True)
            if getattr(err, "code", 0) == 429:  # the Google project's own daily quota is spent: stop asking for a day
                with self.lock:
                    self.calls = [now] * DAILY_CAP
            return None

    @staticmethod
    def _key(p):
        return f"{p['name']}|{p['pt'][0]:.4f}|{p['pt'][1]:.4f}"

    def fill_hours(self, area, near):
        """Give opening hours to named places without any, near the trip. Returns how many were filled."""
        if not self.key:
            return 0
        now, filled, todo = time.time(), 0, []
        for p in area.pois:
            # Google's hours replace a Gemini guess
            if p["hours"] and not p.get("ai_hours") or not p.get("named") or p["group"] in ("transit", "cctv", "police") or not near(p["pt"]):
                continue
            got = self.hours.get(self._key(p))
            if got and now - got[1] < HOURS_DAYS * 86400:
                if got[0]:
                    p["hours"], p["google_hours"], p["ai_hours"] = got[0], True, False
                    filled += 1
            else:
                todo.append(p)
        # cover the places with circles: each call answers for every place within RADIUS_M of its centre
        circles = []
        for p in todo:
            c = next((c for c in circles if dist(c[0], p["pt"]) <= RADIUS_M), None)
            if c:
                c[1].append(p)
            elif len(circles) < MAX_CALLS:
                circles.append((p["pt"], [p]))
        with ThreadPoolExecutor(5) as pool:
            answers = list(pool.map(lambda c: self.nearby(c[0]), circles))
        for (_, group), found in zip(circles, answers):
            if found is None:
                continue  # a failed call is not cached, so it is asked again next time
            for p in group:
                hit = next((h for h in found if h.get("location") and same_name(p["name"], (h.get("displayName") or {}).get("text", ""))
                            and dist(p["pt"], (h["location"]["latitude"], h["location"]["longitude"])) <= MATCH_M), None)
                if not hit and len(found) == 20:
                    continue  # a full answer may have left it out: ask again on a later trip, from another centre
                oh = to_osm((hit.get("regularOpeningHours") or {}).get("periods")) if hit else ""
                self.hours[self._key(p)] = [oh, now]
                if oh:
                    p["hours"], p["google_hours"], p["ai_hours"] = oh, True, False
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
