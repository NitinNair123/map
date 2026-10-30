"""Gemini with Google Search grounding (key in data/gemini_key.txt or GEMINI_KEY, never sent to browsers).

Two small jobs, both off without a key and both a bonus: a failed call leaves the trip scored on map data alone.
- Opening hours for named places along a trip that publish none: one batched call, answers kept on disk for 30 days.
- Live events (VIP movement, barricades, closures, protests) near a trip: one call per area and hour. Each alert must
  name a real street or landmark that place search can find, so guessed positions are dropped.
"""
import json
import os
import re
import threading
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

MODEL = "gemini-2.5-flash"  # flash-lite answered "unknown" for well-known Connaught Place shops
URL = f"https://generativelanguage.googleapis.com/v1beta/models/{MODEL}:generateContent"
DAILY_CAP = 300  # calls a day, both jobs together
MAX_PLACES = 15  # places per hours call
HOURS_DAYS = 30
EVENTS_S = 1800
LOCAL_M = 400  # alerts up to this radius steer routes; wider ones cover every route, so they only warn
PENALTY = 20
HOURS_FORM = re.compile(r"^(24/7|((Mo|Tu|We|Th|Fr|Sa|Su)[-,A-Za-z ]*)?\s*\d{1,2}:\d{2}-\d{1,2}:\d{2}.*)$")


def parse_json(text):
    """The first JSON value in a reply: grounded answers can't use JSON mode and sometimes add prose or fences."""
    m = re.search(r"[\[{].*[\]}]", text or "", re.S)
    try:
        return json.loads(m.group(0)) if m else None
    except ValueError:
        return None


class Gemini:
    def __init__(self, key="", path=None):
        self.key = key
        self.path = path
        self.hours = {}  # "name|lat|lon" -> [opening_hours or "", time asked]
        self.events = {}  # area key -> (alerts, time asked)
        self.calls = []
        self.lock = threading.Lock()
        if path and os.path.exists(path):
            with open(path) as f:
                self.hours = json.load(f)

    def ask(self, prompt, max_tokens):
        """Parsed JSON from one grounded call, or None. Counts against the daily cap."""
        now = time.time()
        with self.lock:
            self.calls = [t for t in self.calls if now - t < 86400]
            if not self.key or len(self.calls) >= DAILY_CAP:
                return None
            self.calls.append(now)
        body = {"contents": [{"parts": [{"text": prompt}]}], "tools": [{"google_search": {}}],
                "generationConfig": {"temperature": 0, "maxOutputTokens": max_tokens, "thinkingConfig": {"thinkingBudget": 0}}}
        req = urllib.request.Request(URL, json.dumps(body).encode(), {"Content-Type": "application/json", "x-goog-api-key": self.key})
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                parts = (json.load(r).get("candidates") or [{}])[0].get("content", {}).get("parts", [])
        except Exception as err:  # a bonus: never break a trip
            print("Gemini:", type(err).__name__, flush=True)
            return None
        return parse_json("".join(p.get("text", "") for p in parts))

    # ---- opening hours ----

    @staticmethod
    def _key(p):
        return f"{p['name']}|{p['pt'][0]:.4f}|{p['pt'][1]:.4f}"

    def fill_hours(self, area, near, where):
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
                    p["hours"], p["ai_hours"] = got[0], True
                    filled += 1
            elif len(todo) < MAX_PLACES:
                todo.append(p)
        if todo:
            lines = "\n".join(f"{i}|{p['name']}|{p['group']}" for i, p in enumerate(todo))
            ans = self.ask(f"""Find the usual opening hours of these places in {where}, from Google Maps, Justdial or their website.
Lines are index|name|type.
{lines}
Reply with only a JSON object mapping each index to OpenStreetMap opening_hours, like {{"0":"Mo-Sa 10:00-21:00; Su 11:00-20:00"}}.
Use "" only if you can't find the place.""", 60 * len(todo) + 100)
            if isinstance(ans, dict):  # a failed or garbled call is not cached, so it is asked again next time
                for i, p in enumerate(todo):
                    oh = str(ans.get(str(i)) or "").strip()
                    oh = oh if HOURS_FORM.match(oh) else ""
                    self.hours[self._key(p)] = [oh, now]
                    if oh:
                        p["hours"], p["ai_hours"] = oh, True
                        filled += 1
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

    # ---- live events ----

    def find_events(self, a, b, when, where, locate):
        """Alerts [{pt, r, what, place, when}] near the trip from a to b. locate(name) -> (lat, lon) or None."""
        if not self.key:
            return []
        k = (round(a[0], 2), round(a[1], 2), round(b[0], 2), round(b[1], 2), when.strftime("%Y-%m-%dT%H"))
        got = self.events.get(k)
        if got and time.time() - got[1] < EVENTS_S:
            return got[0]
        ans = self.ask(f"""Search the latest news and traffic police advisories for {when:%A %d %B %Y, %H:%M} in {where}:
VIP or convoy movement, police barricades, road or metro closures, diversions, protests, large gatherings.
Reply with only a JSON array like [{{"place":"Janpath and Tolstoy Marg crossing, New Delhi","radius_m":200,"what":"VIP convoy barricades","source":"Delhi Traffic Police"}}].
"place" must be a specific street, junction or landmark named in the source. Leave out anything without one. Reply [] if nothing is reported.""", 400)
        found = [h for h in (ans if isinstance(ans, list) else []) if isinstance(h, dict) and str(h.get("place", "")).strip()][:8]
        with ThreadPoolExecutor(4) as pool:
            pts = list(pool.map(lambda h: locate(str(h["place"])[:100]), found))
        out = []
        for h, pt in zip(found, pts):
            if not pt:
                continue  # place search can't find it: its position would be a guess
            try:
                r = min(max(float(h.get("radius_m") or 300), 50), 3000)
            except (TypeError, ValueError):
                r = 300
            out.append({"pt": [round(pt[0], 5), round(pt[1], 5)], "r": round(r), "what": str(h.get("what") or "Reported disruption")[:80],
                        "place": str(h["place"])[:100], "source": str(h.get("source") or "")[:60], "when": when.isoformat(timespec="minutes"),
                        "local": r <= LOCAL_M})
        if ans is not None:
            now = time.time()
            self.events = {key: v for key, v in self.events.items() if now - v[1] < 4 * EVENTS_S}
            self.events[k] = (out, now)
        return out

    def signal(self, pt, when):
        """safety.SIGNALS hook: a local alert within two hours of when costs points; a wide one only warns."""
        from safety import dist
        for alerts, _ in list(self.events.values()):
            for h in alerts:
                if dist(pt, h["pt"]) <= h["r"] and abs((datetime.fromisoformat(h["when"]) - when).total_seconds()) <= 7200:
                    return {"delta": -PENALTY if h["local"] else 0, "warning": f"{h['what']} near {h['place']} (Gemini, check before you go)"}
        return None


LIVE = Gemini()  # off until server.py gives it a key


def signal(pt, when):
    return LIVE.signal(pt, when)  # server.py replaces LIVE, so look it up on every call
