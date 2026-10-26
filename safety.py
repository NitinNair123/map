"""Time-of-day safety scoring and safest-route search over an OSM walking graph."""
import heapq
import math
import re
import time
from collections import defaultdict, deque
from datetime import datetime, timezone

WALK_MPS = 1.3
CELL = 0.001  # spatial grid cell in degrees, roughly 100 m

# How much "safer" weighs against distance: cost = length * (1 + lambda * (1 - score/100))
ROUTE_LAMBDA = {"safest": 10, "balanced": 3, "fastest": 0}

# (full daylight, full dark) weight per factor. Twilight blends the two; factors with no data are left out.
WEIGHTS = {
    "lighting": (5, 25),
    "activity": (25, 25),
    "security": (10, 12),
    "path": (25, 13),
    "surroundings": (15, 15),
    "escape": (10, 5),
    "transit": (10, 5),
    "overlooked": (5, 10),
}
COMPASS = ["north", "northeast", "east", "southeast", "south", "southwest", "west", "northwest"]
ROAD_WORDS = {"footway": "the footpath", "path": "the path", "steps": "the steps", "cycleway": "the cycle path",
              "pedestrian": "the pedestrian street", "service": "the lane", "track": "the track", "living_street": "the lane"}
FACTOR_LABELS = {
    "lighting": "Lighting", "activity": "Open places", "security": "Police & CCTV", "path": "Footpath & road",
    "surroundings": "Surroundings", "escape": "Ways out", "transit": "Transport", "overlooked": "Buildings around",
}

# Main roads have people and traffic; alleys, tracks and steps are isolated
ROAD_SCORE = {
    "trunk": 0.8, "primary": 0.95, "secondary": 1.0, "tertiary": 0.9,
    "primary_link": 0.85, "secondary_link": 0.9, "tertiary_link": 0.8,
    "residential": 0.7, "living_street": 0.75, "pedestrian": 0.85, "unclassified": 0.55,
    "service": 0.4, "footway": 0.45, "cycleway": 0.4, "path": 0.3, "track": 0.1, "steps": 0.25,
}
VEHICLE_ROADS = {"trunk", "primary", "secondary", "tertiary", "primary_link", "secondary_link", "tertiary_link", "unclassified", "residential"}

OSM_ROAD_DETAIL = {"lit", "sidewalk", "sidewalk:both", "sidewalk:left", "sidewalk:right", "footway", "tunnel", "bridge", "layer"}

# OSM lit=* values; a street with no tag, no lamps and no reports has unknown lighting and is left out, never guessed
LIT_TAGS = {"yes": 1.0, "24/7": 1.0, "automatic": 1.0, "sunset-sunrise": 1.0, "interval": 0.7, "limited": 0.6,
            "no": 0.0, "disused": 0.0}
# Land next to the road: (score in daylight, score in the dark). Missing means ordinary built-up area (1.0).
SURROUNDINGS = {
    "construction": (0.4, 0.1), "brownfield": (0.4, 0.1), "greenfield": (0.5, 0.2), "railway": (0.5, 0.2),
    "wood": (0.4, 0.1), "scrub": (0.4, 0.1), "park": (0.8, 0.25), "cemetery": (0.5, 0.15),
    "industrial": (0.6, 0.3), "farmland": (0.6, 0.25), "quarry": (0.3, 0.1),
}

# Score points a recent community report adds near it; halves every 30 days
REPORT_TYPES = {"felt_unsafe": -25, "harassment": -40, "broken_light": 0, "felt_safe": 15}
REPORT_RADIUS_M = 50

# Imported incident history (police data, news): severity, halves every 180 days, counts most at the same time of day
INCIDENT_SEVERITY = {"assault": 1.0, "robbery": 0.9, "harassment": 0.8, "stalking": 0.7, "snatching": 0.7,
                     "theft": 0.4, "other": 0.5}
INCIDENT_RADIUS_M = 100


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


def in_polygon(p, poly):
    inside = False
    for (y1, x1), (y2, x2) in zip(poly, poly[1:] + poly[:1]):
        if (y1 > p[0]) != (y2 > p[0]) and p[1] < x1 + (p[0] - y1) * (x2 - x1) / (y2 - y1):
            inside = not inside
    return inside


def aware(when):
    return when if when.tzinfo else when.astimezone()  # naive times are the server's local time


def sun_elevation(lat, lon, when):
    """Degrees above the horizon (low-precision solar position, good to about a degree)."""
    d = (aware(when).astimezone(timezone.utc) - datetime(2000, 1, 1, 12, tzinfo=timezone.utc)).total_seconds() / 86400
    g = math.radians((357.529 + 0.98560028 * d) % 360)
    q = (280.459 + 0.98564736 * d) % 360
    lam = math.radians((q + 1.915 * math.sin(g) + 0.020 * math.sin(2 * g)) % 360)
    eps = math.radians(23.439 - 0.00000036 * d)
    ra = math.atan2(math.cos(eps) * math.sin(lam), math.cos(lam))
    dec = math.asin(math.sin(eps) * math.sin(lam))
    gmst = (18.697374558 + 24.06570982441908 * d) % 24
    ha = math.radians(gmst * 15 + lon) - ra
    la = math.radians(lat)
    return math.degrees(math.asin(math.sin(la) * math.sin(dec) + math.cos(la) * math.cos(dec) * math.cos(ha)))


def daylight(lat, lon, when):
    """1 in daylight, 0 in the dark, in between through civil twilight."""
    return max(0.0, min(1.0, (sun_elevation(lat, lon, when) + 6) / 12))


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


PLACE_AMENITIES = {"restaurant", "cafe", "fast_food", "atm", "bank", "marketplace", "food_court", "ice_cream", "cinema",
                   "theatre", "school", "college", "university", "place_of_worship", "community_centre", "doctors",
                   "dentist", "post_office", "library", "events_venue", "coworking_space", "bureau_de_change"}


OVERTURE_NIGHTLIFE = {"bar", "pub", "night_club", "liquor_store", "beer_bar", "cocktail_bar", "wine_bar", "lounge", "hookah_bar",
                      "sports_bar", "dive_bar", "gay_bar", "karaoke"}
NOT_TRANSIT = ("gas", "fire", "charging", "radio", "television", "power")


def classify_overture(cat):
    if "police" in cat:
        return "police"
    if cat in OVERTURE_NIGHTLIFE:
        return "nightlife"
    if "station" in cat and not any(w in cat for w in NOT_TRANSIT) or cat in ("bus_stop", "taxi_stand", "transportation"):
        return "transit"
    if "animal" in cat or "veterinar" in cat:
        return "place"
    if any(w in cat for w in ("hospital", "pharmacy", "clinic", "emergency", "gas_station", "fire_station", "hotel")):
        return "haven"
    return "place"


def classify(tags):
    a, shop = tags.get("amenity"), tags.get("shop")
    if "overture" in tags:
        return classify_overture(tags["overture"])
    if tags.get("building"):
        return "building"  # counted per grid cell, not kept as a place
    if tags.get("highway") == "street_lamp":
        return "lamp"
    if tags.get("man_made") == "surveillance":
        return "cctv"
    if a == "police":
        return "police"
    if shop == "alcohol" or a in ("bar", "pub", "nightclub"):
        return "nightlife"
    if a in ("bus_station", "taxi") or tags.get("highway") == "bus_stop" or tags.get("railway") in ("station", "subway_entrance"):
        return "transit"
    if a in ("hospital", "clinic", "pharmacy", "fuel") or tags.get("tourism") == "hotel":
        return "haven"
    if shop or tags.get("office") or a in PLACE_AMENITIES:
        return "place"
    return None


def surroundings_kind(tags):
    for key in ("landuse", "natural", "leisure"):
        if tags.get(key) in SURROUNDINGS:
            return tags[key]
    if tags.get("railway") == "rail":
        return "railway"
    return None


class Area:
    def __init__(self, osm, reports=(), incidents=(), unloaded=None):
        self.unloaded = unloaded or {}  # factor -> (s, w, n, e) boxes whose data hasn't loaded yet: unknown there, not zero
        self.nodes = {}
        self.adj = defaultdict(list)
        self.edges = []
        self.pois = []
        self.grid = defaultdict(list)
        self.building_cells = defaultdict(int)
        self.zones = []  # isolating land: {"kind", "poly" or "line"}
        self.zone_grid = defaultdict(list)
        self.reports = []
        self.incidents = []
        self._cache = {}

        # With Overture roads, OSM ways only lend their tags (lit, sidewalk, foot access) to the matching segment
        overture = any("overture_road" in el.get("tags", {}) for el in osm["elements"])
        osm_ways = {el["id"]: el["tags"] for el in osm["elements"]
                    if overture and el["type"] == "way" and "highway" in el.get("tags", {}) and "overture_road" not in el["tags"]}
        for el in osm["elements"]:
            tags = el.get("tags", {})
            if el["type"] == "way" and "highway" in tags and "geometry" in el:
                if "overture_road" in tags:
                    o = osm_ways.get(tags.get("osm_way"), {})
                    if o.get("foot") == "no" or o.get("access") in ("private", "no"):
                        continue
                    tags = {**{k: v for k, v in o.items() if k in OSM_ROAD_DETAIL}, **tags, "name": o.get("name") or tags.get("name")}
                elif overture:
                    continue
                self._add_road(el, tags)
            elif (kind := surroundings_kind(tags)) and "geometry" in el:
                self._add_zone(el, kind)
            elif "lat" in el or "center" in el:
                pt = (el["lat"], el["lon"]) if "lat" in el else (el["center"]["lat"], el["center"]["lon"])
                kind = classify(tags)
                if kind == "building":
                    self.building_cells[self._cell(pt)] += 1
                elif kind:
                    self.grid[self._cell(pt)].append(len(self.pois))
                    self.pois.append({"kind": kind, "pt": pt, "hours": tags.get("opening_hours"),
                                      "name": tags.get("name") or (tags.get("shop") or tags.get("amenity") or tags.get("overture") or kind).replace("_", " ")})

        self.police = [p["pt"] for p in self.pois if p["kind"] == "police"]
        for e in self.edges:
            self._near_features(e)
        self._junction_gaps()
        self.routable = self._largest_component()
        lats = [p[0] for p in self.nodes.values()]
        lons = [p[1] for p in self.nodes.values()]
        self.bounds = (min(lats), min(lons), max(lats), max(lons))
        self.center = ((self.bounds[0] + self.bounds[2]) / 2, (self.bounds[1] + self.bounds[3]) / 2)

        for r in reports:
            self.add_report(r)
        for i in incidents:
            self.add_incident(i)

    # ---- building the graph ----

    def _add_road(self, el, tags):
        pts = [(g["lat"], g["lon"]) for g in el["geometry"]]
        hw = tags["highway"]
        underpass = tags.get("tunnel") in ("yes", "building_passage") or tags.get("footway") == "subway" or tags.get("layer", "0").startswith("-")
        overbridge = tags.get("bridge") == "yes" and hw in ("footway", "path", "steps")
        sidewalk = next((tags[k] for k in ("sidewalk", "sidewalk:both", "sidewalk:left", "sidewalk:right") if tags.get(k)), None)
        if tags.get("footway") in ("sidewalk", "crossing"):
            sidewalk = "yes"
        for (a, pa), (b, pb) in zip(zip(el["nodes"], pts), zip(el["nodes"][1:], pts[1:])):
            self.nodes[a], self.nodes[b] = pa, pb
            ei = len(self.edges)
            self.edges.append({"a": a, "b": b, "length": dist(pa, pb), "highway": hw, "name": tags.get("name"),
                               "lit": tags.get("lit"), "sidewalk": sidewalk, "underpass": underpass,
                               "overbridge": overbridge, "reports": [], "incidents": []})
            self.adj[a].append((b, ei))
            self.adj[b].append((a, ei))  # walking ignores oneway

    def _add_zone(self, el, kind):
        pts = [(g["lat"], g["lon"]) for g in el["geometry"]]
        closed = len(pts) > 3 and pts[0] == pts[-1]
        zi = len(self.zones)
        self.zones.append({"kind": kind, "pts": pts, "closed": closed})
        lats, lons = [p[0] for p in pts], [p[1] for p in pts]
        for i in range(int(min(lats) // CELL) - 1, int(max(lats) // CELL) + 2):
            for j in range(int(min(lons) // CELL) - 1, int(max(lons) // CELL) + 2):
                self.zone_grid[(i, j)].append(zi)

    def _near_features(self, e):
        pa, pb = self.nodes[e["a"]], self.nodes[e["b"]]
        e["mid"] = mid = ((pa[0] + pb[0]) / 2, (pa[1] + pb[1]) / 2)
        near = defaultdict(list)
        for i, _ in self._near(mid, e["length"] / 2 + 150):
            p = self.pois[i]
            if p["kind"] != "police" and seg_dist(p["pt"], pa, pb) <= {"lamp": 20, "cctv": 50, "transit": 150}.get(p["kind"], 60):
                near[p["kind"]].append(i)
        e["near"] = near
        e["unloaded"] = {f for f, boxes in self.unloaded.items() if any(s <= mid[0] <= n and w <= mid[1] <= x for s, w, n, x in boxes)}
        c = self._cell(mid)
        e["buildings_ha"] = sum(self.building_cells.get((c[0] + i, c[1] + j), 0) for i in (-1, 0, 1) for j in (-1, 0, 1)) / 9.8
        e["police_m"] = min((dist(mid, p) for p in self.police), default=math.inf)
        e["lamp_level"] = min(1, len(near["lamp"]) * 35 / max(e["length"], 35))  # one lamp per ~35 m is fully lit
        kinds = set()
        for zi in self.zone_grid.get(self._cell(mid), ()):
            z = self.zones[zi]
            pts = z["pts"]
            if (z["closed"] and in_polygon(mid, pts)) or any(seg_dist(mid, p, q) <= 30 for p, q in zip(pts, pts[1:])):
                kinds.add(z["kind"])
        e["zones"] = sorted(kinds)

    def _junction_gaps(self):
        """For each edge, the length of the junction-free stretch it sits on and whether that stretch is a dead end."""
        deg = {n: len(v) for n, v in self.adj.items()}
        done = set()
        for ei, e in enumerate(self.edges):
            if ei in done:
                continue
            chain, ends = [ei], []
            members = {ei}
            for start, came in ((e["a"], ei), (e["b"], ei)):
                n, prev = start, came
                while deg[n] == 2:
                    nxt = next(((v, j) for v, j in self.adj[n] if j != prev), None)
                    if nxt is None or nxt[1] in members:  # closed loop
                        break
                    n, prev = nxt
                    chain.append(prev)
                    members.add(prev)
                ends.append(deg[n])
            total = sum(self.edges[j]["length"] for j in chain)
            for j in chain:
                done.add(j)
                self.edges[j]["gap_m"] = total
                self.edges[j]["dead_ends"] = sum(d == 1 for d in ends)

    def _largest_component(self):
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
        return best

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

    def _attach(self, item, radius, key):
        pt = (item["lat"], item["lon"])
        for e in self.edges:
            if dist(pt, e["mid"]) <= radius + e["length"] / 2 and seg_dist(pt, self.nodes[e["a"]], self.nodes[e["b"]]) <= radius:
                e[key].append(item)
        self._cache.clear()

    def add_report(self, r):
        self.reports.append(r)
        self._attach(r, REPORT_RADIUS_M, "reports")

    def add_incident(self, i):
        self.incidents.append(i)
        self._attach(i, INCIDENT_RADIUS_M, "incidents")

    # ---- scoring ----

    def is_place_open(self, p, when):
        """True or False from the place's published opening hours, None when it has none."""
        return is_open(p["hours"], when)

    def factors(self, e, when, light):
        """Each factor from 0 (worst) to 1 (best) for this edge at this time, or None when there is no data for it."""
        near = e["near"]
        dark = 1 - light
        status = [(i, self.is_place_open(self.pois[i], when)) for i in near["place"] + near["haven"]]
        open_places = [i for i, o in status if o]
        if any(r["type"] == "broken_light" for r in e["reports"]):
            lighting = 0.0
        elif e["lit"] in LIT_TAGS:
            lighting = LIT_TAGS[e["lit"]]
        elif near["lamp"]:
            lighting = e["lamp_level"]
        else:
            lighting = None
        if open_places or not status:
            activity = min(1, len(open_places) / 3)
        else:
            activity = None if any(o is None for _, o in status) else 0.0  # places nearby, but no published hours
        police = 1.0 if e["police_m"] < 400 else 0.5 if e["police_m"] < 800 else 0.0
        path = ROAD_SCORE.get(e["highway"], 0.5)
        if e["highway"] in VEHICLE_ROADS and e["sidewalk"]:
            path += -0.3 if e["sidewalk"] == "no" and e["highway"] not in ("residential", "unclassified") else 0.15
        if e["underpass"]:
            path = 0.1
        elif e["overbridge"]:
            path = path * light + 0.25 * dark
        surroundings = min((SURROUNDINGS[k][0] * light + SURROUNDINGS[k][1] * dark for k in e["zones"]), default=1.0)
        if "surroundings" in e["unloaded"]:
            surroundings = None  # parks and wasteland come with the OSM detail, still loading here
        if e["dead_ends"] == 2:
            escape = 0.0
        elif e["dead_ends"] == 1:
            escape = 0.2
        else:
            escape = max(0.0, 1 - e["gap_m"] / 500)  # no side street for 500 m means nowhere to turn off
        return {
            "lighting": lighting,
            "activity": activity,
            "security": min(1, police + 0.3 * len(near["cctv"])),
            "path": max(0.0, min(1.0, path)),
            "surroundings": surroundings,
            "escape": escape,
            "transit": 1.0 if near["transit"] else 0.0,
            # homes and shops overlooking the street; unknown until building footprints are loaded for this area
            "overlooked": min(1, e["buildings_ha"] / 20) if self.building_cells and "overlooked" not in e["unloaded"] else None,
        }, open_places

    def edge_score(self, e, when, light, rain=False):
        feats, _ = self.factors(e, when, light)
        dark = 1 - light
        weights = {k: WEIGHTS[k][0] * light + WEIGHTS[k][1] * dark for k, v in feats.items() if v is not None}
        s = 100 * sum(w * feats[k] for k, w in weights.items()) / sum(weights.values())  # unknown factors are left out
        s -= 10 * dark * min(2, len(e["near"]["nightlife"]))
        if rain:
            s -= 5  # rain empties streets and cuts visibility
        now = time.time()
        s += sum(REPORT_TYPES[r["type"]] * 0.5 ** ((now - r["ts"]) / 86400 / 30) for r in e["reports"])
        hits = 0.0
        for i in e["incidents"]:
            hour_gap = abs(i["hour"] - when.hour)
            same_time = 1.0 if min(hour_gap, 24 - hour_gap) <= 2 else 0.4
            hits += 25 * INCIDENT_SEVERITY.get(i["type"], 0.5) * 0.5 ** ((now - i["ts"]) / 86400 / 180) * same_time
        s -= min(35, hits)
        return max(0.0, min(100.0, s))

    def light(self, when):
        return daylight(*self.center, when)

    def scores(self, when, rain=False):
        light = self.light(when)
        key = (when.weekday(), when.hour, round(light, 1), rain)
        if key not in self._cache:
            if len(self._cache) > 200:
                self._cache.clear()
            self._cache[key] = [self.edge_score(e, when, light, rain) for e in self.edges]
        return self._cache[key]

    # ---- routing ----

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

    def routes(self, start, end, when, rain=False):
        """Safest, balanced and fastest routes between two (lat, lon) points."""
        scores = self.scores(when, rain)
        src, dst = self.nearest(start), self.nearest(end)
        paths = {k: self._search(src, dst, lam, scores) for k, lam in ROUTE_LAMBDA.items()}
        fastest_len = sum(self.edges[ei]["length"] for _, ei in paths["fastest"]) or 1
        out = [self._summary(k, p, scores, when, fastest_len, dst) for k, p in paths.items()]
        for i, r in enumerate(out):
            r["same_as"] = next((o["kind"] for o in out[:i] if o["coords"] == r["coords"]), None)
        return out

    def _summary(self, kind, path, scores, when, fastest_len, dst):
        length = sum(self.edges[ei]["length"] for _, ei in path)
        coords = [self.nodes[n] for n, _ in path] + [self.nodes[dst]]
        light = self.light(when)
        base = {"kind": kind, "coords": coords, "length_m": round(length), "minutes": max(1, round(length / WALK_MPS / 60)),
                "daylight": round(light, 2)}
        if not path:
            return {**base, "minutes": 0, "score": 100, "low_data": False, "reasons": [], "warnings": [],
                    "factors": {}, "weakest": None, "havens": [], "turns": 0, "steps": []}
        avg = sum(scores[ei] * self.edges[ei]["length"] for _, ei in path) / (length or 1)
        weak_i = min(range(len(path)), key=lambda i: scores[path[i][1]])
        weak = self.edges[path[weak_i][1]]

        isolated = run = 0.0
        haven_gap = gap = 0.0
        factor_sum, factor_len = defaultdict(float), defaultdict(float)
        places, havens, lamps, cctv, unknown_hours, mapped = set(), {}, set(), set(), set(), set()
        lit_len = dark_len = 0.0
        police_m = math.inf
        underpasses = 0
        zones = set()
        for _, ei in path:
            e = self.edges[ei]
            feats, open_here = self.factors(e, when, light)
            for k, v in feats.items():
                if v is not None:
                    factor_sum[k] += v * e["length"]
                    factor_len[k] += e["length"]
            run = run + e["length"] if scores[ei] < 40 else 0.0
            isolated = max(isolated, run)
            gap = 0.0 if open_here else gap + e["length"]
            haven_gap = max(haven_gap, gap)
            places.update(open_here)
            for i in open_here:
                havens.setdefault(i, len(havens))
            lamps.update(e["near"]["lamp"])
            cctv.update(e["near"]["cctv"])
            mapped.update(e["near"]["place"] + e["near"]["haven"])
            unknown_hours.update(i for i in e["near"]["place"] + e["near"]["haven"] if self.is_place_open(self.pois[i], when) is None)
            if feats["lighting"] is not None:
                lit_len += e["length"] if feats["lighting"] >= 0.5 else 0
                dark_len += e["length"] if feats["lighting"] < 0.5 else 0
            police_m = min(police_m, e["police_m"])
            underpasses += e["underpass"]
            zones.update(e["zones"])

        score = 0.6 * avg + 0.4 * scores[path[weak_i][1]]  # weakest link drags the whole route down
        score -= min(15, isolated / 50)
        score -= min(10, max(0, haven_gap - 300) / 50)
        score -= max(0, length / fastest_len - 1.3) * 20

        steps = self._steps(path, coords)
        turns = sum(st["turn"] not in ("straight", "arrive", "depart") for st in steps)

        no_light = length - lit_len - dark_len
        reasons = [f"{len(mapped)} shops and places along the way", f"{len(places)} confirmed open now"]
        if unknown_hours:
            reasons.append(f"{len(unknown_hours)} with no published hours")
        reasons.append(f"{round(100 * lit_len / length)}% lit, {round(100 * dark_len / length)}% unlit")
        if no_light >= 1:
            reasons.append(f"No lighting data for {round(100 * no_light / length)}%")
        if cctv:
            reasons.append(f"{len(cctv)} CCTV cameras")
        if police_m < 800:
            reasons.append(f"Police within {round(police_m)} m")
        warnings = []
        if isolated >= 100:
            warnings.append(f"{round(isolated)} m isolated stretch")
        if haven_gap >= 300:
            warnings.append(f"{round(haven_gap)} m with nowhere known to be open")
        if underpasses:
            warnings.append("Goes through an underpass")
        for z in sorted(zones):
            if SURROUNDINGS[z][1] * (1 - light) + SURROUNDINGS[z][0] * light < 0.5:
                warnings.append(f"Passes {z.replace('_', ' ')} land")
        weak_feats, _ = self.factors(weak, when, light)
        weak_why = max((k for k, v in weak_feats.items() if v is not None),
                       key=lambda k: (WEIGHTS[k][0] * light + WEIGHTS[k][1] * (1 - light)) * (1 - weak_feats[k]))
        return {
            **base,
            "score": round(max(0, min(100, score))),
            "low_data": no_light / length > 0.5,
            "reasons": reasons,
            "warnings": warnings,
            "factors": {FACTOR_LABELS[k]: round(100 * factor_sum[k] / factor_len[k]) if factor_len[k] else None for k in WEIGHTS},
            "turns": turns,
            "steps": steps,
            "weakest": {"coords": [self.nodes[weak["a"]], self.nodes[weak["b"]]], "score": round(scores[path[weak_i][1]]),
                        "name": weak["name"] or weak["highway"].replace("_", " "), "why": FACTOR_LABELS[weak_why]},
            "havens": [{"name": self.pois[i]["name"], "pt": self.pois[i]["pt"]} for i in list(havens)[:30]],
        }

    def _steps(self, path, coords):
        """Turn-by-turn instructions: a step wherever the street name changes or the walk turns at a junction."""
        steps, travelled, prev_name, prev_bearing = [], 0.0, None, None
        for k, (n, ei) in enumerate(path):
            e = self.edges[ei]
            a, b = coords[k], coords[k + 1]
            bearing = math.degrees(math.atan2((b[1] - a[1]) * math.cos(math.radians(a[0])), b[0] - a[0])) % 360
            name = e["name"] or ROAD_WORDS.get(e["highway"], "the road")
            if k == 0:
                steps.append({"pt": a, "dist": 0, "turn": "depart", "text": f"Head {COMPASS[round(bearing / 45) % 8]} on {name}"})
            else:
                turn = (bearing - prev_bearing + 540) % 360 - 180  # positive is clockwise, i.e. right
                junction = len(self.adj[n]) >= 3
                if (junction and abs(turn) >= 30) or name != prev_name:
                    side = "right" if turn > 0 else "left"
                    kind = ("straight" if abs(turn) < 30 else f"slight {side}" if abs(turn) < 60
                            else side if abs(turn) < 140 else "u-turn")
                    verb = {"straight": "Continue", "u-turn": "Turn around"}.get(kind, f"Turn {kind}")
                    text = f"{verb} onto {name}" if name != prev_name else verb
                    if steps and travelled - steps[-1]["dist"] < 10 and steps[-1]["turn"] == kind == "straight":
                        steps[-1]["text"] = text  # tiny unnamed link between two streets: one instruction, not two
                    else:
                        steps.append({"pt": a, "dist": round(travelled), "turn": kind, "text": text})
            travelled += e["length"]
            prev_name, prev_bearing = name, bearing
        steps.append({"pt": coords[-1], "dist": round(travelled), "turn": "arrive", "text": "You have arrived"})
        return steps

    def havens_near(self, pt, when, limit=5):
        """Closest places that are open right now and where someone can step in for help."""
        found = [(d, i) for i, d in self._near(pt, 1000)
                 if self.pois[i]["kind"] in ("haven", "place", "police") and (self.pois[i]["kind"] == "police" or self.is_place_open(self.pois[i], when) is True)]
        found.sort()
        return [{"name": self.pois[i]["name"], "kind": self.pois[i]["kind"], "pt": self.pois[i]["pt"], "distance_m": round(d)}
                for d, i in found[:limit]]

    def heat(self, when, rain=False, bbox=None):
        scores = self.scores(when, rain)
        s, w, n, e = bbox or self.bounds
        return [[*self.nodes[ed["a"]], *self.nodes[ed["b"]], round(sc)] for ed, sc in zip(self.edges, scores)
                if s <= ed["mid"][0] <= n and w <= ed["mid"][1] <= e]
