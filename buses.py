"""Live bus positions from Delhi Open Transit Data (GTFS-realtime, free key from otd.delhi.gov.in).

Every moving bus is remembered in the ~110 m grid cells it passes for WINDOW_S, so a street can say how many
different buses went by in the last half hour: a sign that people are waiting, getting off and walking there.
"""
import struct
import threading
import time
import urllib.request
from datetime import datetime

URL = "https://otd.delhi.gov.in/api/realtime/VehiclePositions.pb?key={}"
POLL_S = 10  # how often the feed updates
WINDOW_S = 30 * 60
CELL = 0.001  # degrees, about 110 m
MOVED_M = 20  # a bus that hasn't moved this far between fixes is parked (depots, breakdowns) and doesn't count
STALE_S = 300  # a fix older than this is an old position, not a bus on the street now


def _fields(b):
    """(field number, value) for one protobuf message; value is an int (varint) or bytes."""
    i = 0
    while i < len(b):
        key, i = _varint(b, i)
        num, wire = key >> 3, key & 7
        if wire == 0:
            v, i = _varint(b, i)
        elif wire == 1:
            v, i = b[i:i + 8], i + 8
        elif wire == 2:
            n, i = _varint(b, i)
            v, i = b[i:i + n], i + n
        elif wire == 5:
            v, i = b[i:i + 4], i + 4
        else:
            raise ValueError(f"unsupported protobuf wire type {wire}")
        yield num, v


def _varint(b, i):
    out = shift = 0
    while True:
        c = b[i]
        out |= (c & 0x7F) << shift
        i += 1
        if c < 0x80:
            return out, i
        shift += 7


def decode(data):
    """[(vehicle id, lat, lon, timestamp or None)] from a GTFS-realtime FeedMessage."""
    out = []
    for num, entity in _fields(data):
        if num != 2:  # FeedMessage.entity
            continue
        eid, vp = None, None
        for n, v in _fields(entity):
            if n == 1:
                eid = v.decode("utf-8", "replace")
            elif n == 4:  # FeedEntity.vehicle
                vp = v
        if vp is None:
            continue
        vid, lat, lon, ts = eid, None, None, None
        for n, v in _fields(vp):
            if n == 2:  # VehiclePosition.position
                for pn, pv in _fields(v):
                    if pn == 1:
                        lat = struct.unpack("<f", pv)[0]
                    elif pn == 2:
                        lon = struct.unpack("<f", pv)[0]
            elif n == 5:
                ts = v
            elif n == 8:  # VehicleDescriptor: id, else label
                d = dict(_fields(v))
                vid = (d.get(1) or d.get(2) or b"").decode("utf-8", "replace") or vid
        if vid and lat and lon:
            out.append((vid, lat, lon, ts))
    return out


def is_now(when, now):
    """Live readings describe now, so they count only for a time within 20 minutes of it."""
    return when is None or abs((when - datetime.fromtimestamp(now, when.tzinfo)).total_seconds()) <= 1200


def _cell(lat, lon):
    return int(lat // CELL), int(lon // CELL)


class LiveBuses:
    def __init__(self, key=""):
        self.key = key
        self.cells = {}  # cell -> {vehicle id: last time seen there}
        self.last = {}  # vehicle id -> (lat, lon)
        self.started = None  # first successful poll
        self.ok_at = 0.0  # last successful poll
        self.lock = threading.Lock()

    def record(self, buses, now=None):
        now = now or time.time()
        with self.lock:
            for vid, lat, lon, ts in buses:
                if ts and now - ts > STALE_S:
                    continue
                prev = self.last.get(vid)
                self.last[vid] = (lat, lon)
                if prev is None or abs(prev[0] - lat) * 111000 + abs(prev[1] - lon) * 102000 < MOVED_M:
                    continue  # first fix or standing still
                self.cells.setdefault(_cell(lat, lon), {})[vid] = now
            self.started = self.started or now
            self.ok_at = now

    def prune(self, now=None):
        cut = (now or time.time()) - WINDOW_S
        with self.lock:
            for c in list(self.cells):
                seen = {v: t for v, t in self.cells[c].items() if t > cut}
                if seen:
                    self.cells[c] = seen
                else:
                    del self.cells[c]

    def live(self, now=None):
        """True once the feed has been watched for a whole window and is still arriving."""
        now = now or time.time()
        return self.started is not None and now - self.started >= WINDOW_S and now - self.ok_at < 120

    def near(self, pt, when=None, now=None):
        """Ids of buses that moved through the cells around pt in the last WINDOW_S, or None when there is no live
        count for that moment (feed off, warming up, or a trip planned for another time)."""
        now = now or time.time()
        if not self.live(now) or not is_now(when, now):
            return None
        c = _cell(*pt)
        cut = now - WINDOW_S
        with self.lock:
            return {v for di in (-1, 0, 1) for dj in (-1, 0, 1) for v, t in self.cells.get((c[0] + di, c[1] + dj), {}).items() if t > cut}

    def run(self):
        last_prune = 0.0
        while True:
            try:
                with urllib.request.urlopen(URL.format(self.key), timeout=15) as r:
                    self.record(decode(r.read()))
            except Exception as err:  # the feed goes down at times; keep polling
                print("Live buses:", type(err).__name__, flush=True)
                time.sleep(60)
            if time.time() - last_prune > 60:
                self.prune()
                last_prune = time.time()
            time.sleep(POLL_S)


LIVE = LiveBuses()  # off until server.py gives it a key and starts run()
