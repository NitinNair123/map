"""Time-of-day safety scoring and safest-route search over an OSM walking graph."""
import heapq
import math
import re
import time
from collections import defaultdict, deque

WALK_MPS = 1.3
CELL = 0.001  # spatial grid cell in degrees, roughly 100 m

# How much "safer" weighs against distance: cost = length * (1 + lambda * (1 - score/100))
ROUTE_LAMBDA = {"safest": 10, "balanced": 3, "fastest": 0}

# (day, night) weight per factor; each column sums to 100
WEIGHTS = {
    "lighting": (5, 30),
    "active": (30, 30),
    "security": (15, 15),
    "path": (35, 15),
    "transit": (15, 10),
}

# Main roads have people and traffic; alleys, tracks and steps are isolated
ROAD_SCORE = {
    "trunk": 0.9, "primary": 1.0, "secondary": 1.0, "tertiary": 0.9,
    "primary_link": 0.9, "secondary_link": 0.9, "tertiary_link": 0.8,
    "residential": 0.7, "living_street": 0.7, "pedestrian": 0.8, "unclassified": 0.6,
    "service": 0.4, "footway": 0.4, "cycleway": 0.4, "path": 0.3, "track": 0.1, "steps": 0.2,
}

# Score points a recent community report adds near it; halves every 30 days
REPORT_TYPES = {"felt_unsafe": -25, "harassment": -40, "broken_light": 0, "felt_safe": 15}
REPORT_RADIUS_M = 50


def dist(a, b):
    """Metres between (lat, lon) points; equirectangular is accurate enough at city scale."""
    x = math.radians(b[1] - a[1]) * math.cos(math.radians((a[0] + b[0]) / 2))
    y = math.radians(b[0] - a[0])
    return 6371000 * math.hypot(x, y)


def seg_dist(p, a, b):
    """Metres from point p to the segment a-b."""
    k = math.cos(math.radians(a[0])) * 111320
    px, py, bx, by = (p[1] - a[1]) * k, (p[0] - a[0]) * 110540, (b[1] - a[1]) * k, (b[0] - a[0]) * 110540
    t = max(0, min(1, (px * bx + py * by) / (bx * bx + by * by or 1)))
    return math.hypot(px - t * bx, py - t * by)


def is_night(when):
    # shortcut: fixed 18:30-06:00 night, switch to a real sunrise/sunset calc when leaving one city
    m = when.hour * 60 + when.minute
    return m >= 18 * 60 + 30 or m < 6 * 60


DAYS = ["Mo", "Tu", "We", "Th", "Fr", "Sa", "Su"]
_DAY = "(?:Mo|Tu|We|Th|Fr|Sa|Su)"
_DAYSPEC = rf"{_DAY}(?:-{_DAY})?(?:,{_DAY}(?:-{_DAY})?)*"
_HOURS = r"\d\d:\d\d-\d\d:\d\d(?:,\d\d:\d\d-\d\d:\d\d)*"
_RULE = re.compile(rf"(?:({_DAYSPEC})\s+)?({_HOURS}|off|closed)")


def _has_day(days, d):
    for part in days.split(","):
        a, _, b = part.partition("-")
        i, j = DAYS.index(a), DAYS.index(b or a)
        if (i <= d <= j) if i <= j else (d >= i or d <= j):
            return True
    return False


def is_open(spec, when):
    """True/False for common OSM opening_hours forms, None when the format isn't understood."""
    if not spec:
        return None
    spec = spec.strip()
    if spec == "24/7":
        return True
    day, mins = when.weekday(), when.hour * 60 + when.minute
    result = None
    for rule in spec.split(";"):
        rule = re.sub(r"\s*,\s*", ",", rule.strip())
        if not rule:
            continue
        m = _RULE.fullmatch(rule)
        if not m:
            return None
        days, hours = m.groups()
        today = not days or _has_day(days, day)
        yesterday = not days or _has_day(days, (day - 1) % 7)
        if hours in ("off", "closed"):
            if today:
                result = False
            continue
        open_now = False
        for r in hours.split(","):
            s, e = (int(t[:2]) * 60 + int(t[3:]) for t in r.split("-"))
            if e > s:
                open_now |= today and s <= mins < e
            else:  # past midnight, e.g. 22:00-02:00
                open_now |= (today and mins >= s) or (yesterday and mins < e)
        if today:
            result = open_now  # later rules override earlier ones, as in OSM
        elif open_now:
            result = True
    return bool(result)


def classify(tags):
    a, shop = tags.get("amenity"), tags.get("shop")
    if tags.get("highway") == "street_lamp":
        return "lamp"
    if tags.get("man_made") == "surveillance":
        return "cctv"
    if a == "police":
        return "police"
    if shop == "alcohol" or a in ("bar", "pub", "nightclub"):
        return "nightlife"
    if a in ("bus_station", "taxi") or tags.get("highway") == "bus_stop":
        return "transit"
    if a in ("hospital", "clinic", "pharmacy", "fuel") or tags.get("tourism") == "hotel":
        return "haven"
    if shop or a:
        return "place"
    return None


class Area:
    def __init__(self, osm, reports=()):
        self.nodes = {}
        self.adj = defaultdict(list)
        self.edges = []
        self.pois = []
        self.grid = defaultdict(list)
        self.reports = []
        self._cache = {}

        for el in osm["elements"]:
            tags = el.get("tags", {})
            if el["type"] == "way" and "highway" in tags and "geometry" in el:
                pts = [(g["lat"], g["lon"]) for g in el["geometry"]]
                for (a, pa), (b, pb) in zip(zip(el["nodes"], pts), zip(el["nodes"][1:], pts[1:])):
                    self.nodes[a], self.nodes[b] = pa, pb
                    ei = len(self.edges)
                    self.edges.append({"a": a, "b": b, "length": dist(pa, pb), "highway": tags["highway"],
                                       "lit": tags.get("lit"), "reports": []})
                    self.adj[a].append((b, ei))
                    self.adj[b].append((a, ei))  # walking ignores oneway
            elif (kind := classify(tags)):
                pt = (el["lat"], el["lon"]) if "lat" in el else (el["center"]["lat"], el["center"]["lon"])
                self.grid[self._cell(pt)].append(len(self.pois))
                self.pois.append({"kind": kind, "pt": pt, "name": tags.get("name") or tags.get("shop") or tags.get("amenity") or kind,
                                  "hours": tags.get("opening_hours")})

        for e in self.edges:
            pa, pb = self.nodes[e["a"]], self.nodes[e["b"]]
            mid = ((pa[0] + pb[0]) / 2, (pa[1] + pb[1]) / 2)
            e["mid"] = mid
            near = defaultdict(list)
            police_m = math.inf
            for i, _ in self._near(mid, max(800, e["length"] / 2 + 150)):
                p = self.pois[i]
                k = p["kind"]
                d = seg_dist(p["pt"], pa, pb)
                if k == "police":
                    police_m = min(police_m, d)
                elif d <= {"lamp": 20, "cctv": 50, "transit": 150}.get(k, 60):
                    near[k].append(i)
            e["near"] = near
            e["police_m"] = police_m
            e["lamp_level"] = min(1, len(near["lamp"]) * 35 / max(e["length"], 35))  # one lamp per ~35 m is fully lit

        # Snap to the largest connected piece so routes never start on an isolated fragment
        seen, best = set(), []
        for n in self.adj:
            if n in seen:
                continue
            comp, q = [], deque([n])
            seen.add(n)
            while q:
                u = q.popleft()
                comp.append(u)
                for v, _ in self.adj[u]:
                    if v not in seen:
                        seen.add(v)
                        q.append(v)
            if len(comp) > len(best):
                best = comp
        self.routable = best
        lats = [p[0] for p in self.nodes.values()]
        lons = [p[1] for p in self.nodes.values()]
        self.bounds = (min(lats), min(lons), max(lats), max(lons))

        for r in reports:
            self.add_report(r)

    def _cell(self, pt):
        return (int(pt[0] // CELL), int(pt[1] // CELL))

    def _near(self, pt, radius_m):
        ci, cj = self._cell(pt)
        k = int(radius_m / 100) + 1
        for i in range(ci - k, ci + k + 1):
            for j in range(cj - k, cj + k + 1):
                for pi in self.grid.get((i, j), ()):
                    d = dist(pt, self.pois[pi]["pt"])
                    if d <= radius_m:
                        yield pi, d

    def add_report(self, r):
        self.reports.append(r)
        pt = (r["lat"], r["lon"])
        for e in self.edges:
            if dist(pt, e["mid"]) <= REPORT_RADIUS_M:
                e["reports"].append(r)
        self._cache.clear()

    def is_place_open(self, p, when):
        o = is_open(p["hours"], when)
        if o is not None:
            return o
        if p["kind"] == "haven":  # hospitals, pumps, pharmacies without hours are usually 24h
            return True
        return 9 <= when.hour < 21  # shortcut: guessed shop hours, replace with Places API hours

    def edge_score(self, e, when):
        near = e["near"]
        col = 1 if is_night(when) else 0
        broken = any(r["type"] == "broken_light" for r in e["reports"])
        if broken or e["lit"] == "no":
            lighting = 0.0
        elif e["lit"] == "yes":
            lighting = 1.0
        else:
            lighting = e["lamp_level"]
        open_places = [i for i in near["place"] + near["haven"] if self.is_place_open(self.pois[i], when)]
        police = 1.0 if e["police_m"] < 400 else 0.5 if e["police_m"] < 800 else 0.0
        feats = {
            "lighting": lighting,
            "active": min(1, len(open_places) / 3),
            "security": min(1, police + 0.3 * len(near["cctv"])),
            "path": ROAD_SCORE.get(e["highway"], 0.5),
            "transit": 1.0 if near["transit"] else 0.0,
        }
        s = sum(WEIGHTS[k][col] * v for k, v in feats.items())
        if col:
            s -= 10 * min(2, len(near["nightlife"]))
        now = time.time()
        s += sum(REPORT_TYPES[r["type"]] * 0.5 ** ((now - r["ts"]) / 86400 / 30) for r in e["reports"])
        return max(0.0, min(100.0, s))

    def scores(self, when):
        key = (when.weekday(), when.hour, is_night(when))
        if key not in self._cache:
            self._cache[key] = [self.edge_score(e, when) for e in self.edges]
        return self._cache[key]

    def has_data(self, e):
        return e["lit"] in ("yes", "no") or bool(e["near"]["lamp"] or e["near"]["place"] or e["near"]["haven"] or e["reports"])

    def nearest(self, pt):
        # shortcut: linear scan, fine for a campus-sized area; use the grid if the area grows to a city
        return min(self.routable, key=lambda n: dist(pt, self.nodes[n]))

    def _search(self, src, dst, lam, scores):
        best, prev = {src: 0.0}, {}
        heap = [(0.0, src)]
        while heap:
            d, u = heapq.heappop(heap)
            if u == dst:
                break
            if d > best[u]:
                continue
            for v, ei in self.adj[u]:
                nd = d + self.edges[ei]["length"] * (1 + lam * (1 - scores[ei] / 100))
                if nd < best.get(v, math.inf):
                    best[v], prev[v] = nd, (u, ei)
                    heapq.heappush(heap, (nd, v))
        path, n = [], dst
        while n != src:
            n, ei = prev[n]
            path.append((n, ei))
        path.reverse()
        return path

    def routes(self, start, end, when):
        """Safest, balanced and fastest routes between two (lat, lon) points, best first."""
        scores = self.scores(when)
        src, dst = self.nearest(start), self.nearest(end)
        paths = {k: self._search(src, dst, lam, scores) for k, lam in ROUTE_LAMBDA.items()}
        fastest_len = sum(self.edges[ei]["length"] for _, ei in paths["fastest"]) or 1
        return [self._summary(k, p, scores, when, fastest_len, dst) for k, p in paths.items()]

    def _summary(self, kind, path, scores, when, fastest_len, dst):
        length = sum(self.edges[ei]["length"] for _, ei in path)
        coords = [self.nodes[n] for n, _ in path] + [self.nodes[dst]]
        if not path:
            return {"kind": kind, "coords": coords, "length_m": 0, "minutes": 0, "score": 100,
                    "low_data": False, "reasons": [], "weakest": None}
        avg = sum(scores[ei] * self.edges[ei]["length"] for _, ei in path) / (length or 1)
        weak_i = min(range(len(path)), key=lambda i: scores[path[i][1]])
        weak = self.edges[path[weak_i][1]]

        isolated = run = 0.0
        for _, ei in path:
            run = run + self.edges[ei]["length"] if scores[ei] < 40 else 0.0
            isolated = max(isolated, run)
        score = 0.6 * avg + 0.4 * scores[path[weak_i][1]]  # weakest link drags the whole route down
        score -= min(15, isolated / 50)
        score -= max(0, length / fastest_len - 1.3) * 20

        places, lamps, cctv = set(), set(), set()
        lit_len, data_len, police_m = 0.0, 0.0, math.inf
        for _, ei in path:
            e = self.edges[ei]
            places.update(i for i in e["near"]["place"] + e["near"]["haven"] if self.is_place_open(self.pois[i], when))
            lamps.update(e["near"]["lamp"])
            cctv.update(e["near"]["cctv"])
            lit_len += e["length"] if (e["lit"] == "yes" or e["lamp_level"] >= 0.5) else 0
            data_len += e["length"] if self.has_data(e) else 0
            police_m = min(police_m, e["police_m"])
        reasons = [f"{len(places)} open places along the way", f"{round(100 * lit_len / length)}% well lit"]
        if lamps:
            reasons.append(f"{len(lamps)} street lights")
        if cctv:
            reasons.append(f"{len(cctv)} CCTV cameras")
        if police_m < 800:
            reasons.append(f"police station within {round(police_m)} m")
        if isolated >= 100:
            reasons.append(f"{round(isolated)} m isolated stretch")
        return {
            "kind": kind,
            "coords": coords,
            "length_m": round(length),
            "minutes": round(length / WALK_MPS / 60),
            "score": round(max(0, min(100, score))),
            "low_data": data_len / length < 0.5,
            "reasons": reasons,
            "weakest": {"coords": [self.nodes[weak["a"]], self.nodes[weak["b"]]],
                        "score": round(scores[path[weak_i][1]]), "highway": weak["highway"]},
        }

    def heat(self, when):
        scores = self.scores(when)
        return [[*self.nodes[e["a"]], *self.nodes[e["b"]], round(s)] for e, s in zip(self.edges, scores)]
