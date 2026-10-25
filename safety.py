"""Time-of-day safety scoring and safest-route search over an OSM walking graph."""
import heapq
import math
import re
import time
from collections import defaultdict, deque
from datetime import datetime, timezone

import rating
from rating import LABELS, SURROUNDINGS

WALK_MPS = 1.3
CELL = 0.001  # spatial grid cell in degrees, roughly 100 m

# How much "safer" weighs against distance in the search: cost = length * (1 + lambda * (1 - credit/100)).
# Each lambda finds one candidate; more come from re-searching with the streets already used made costlier.
SEARCH_LAMBDAS = (0, 1, 3, 6, 10, 20)
DETOUR_ROUNDS = 3
MAX_DETOUR = 1.8  # candidates longer than this times the fastest route are dropped
BALANCED_DETOUR = 1.25

COMPASS = ["north", "northeast", "east", "southeast", "south", "southwest", "west", "northwest"]
ROAD_WORDS = {"footway": "the footpath", "path": "the path", "steps": "the steps", "cycleway": "the cycle path",
              "pedestrian": "the pedestrian street", "service": "the lane", "track": "the track", "living_street": "the lane"}
OSM_ROAD_DETAIL = {"lit", "sidewalk", "sidewalk:both", "sidewalk:left", "sidewalk:right", "footway", "tunnel", "bridge", "layer",
                   "surface", "smoothness", "lanes", "maxspeed"}
REPORT_RADIUS_M = 50
INCIDENT_RADIUS_M = 100
STOP_RADIUS_M = 150
NIGHT_CELL = 0.005  # NASA night-light pixels, about 500 m
NEAR_M = {"lamp": 20, "cctv": 50, "transit": 150, "crossing": 15, "sos_phone": 100, "blackspot": 100, "waterlogging": 50}


def number(v):
    """Leading number of an OSM value such as "4" or "50 km/h", None when there is none."""
    m = re.match(r"\s*(\d+)", v or "")
    return int(m.group(1)) if m else None


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


OVERTURE_NIGHTLIFE = {"bar", "pub", "night_club", "liquor_store", "beer_bar", "cocktail_bar", "wine_bar", "lounge", "hookah_bar"}


def classify_overture(cat):
    if "police" in cat:
        return "police"
    if cat in OVERTURE_NIGHTLIFE:
        return "nightlife"
    if "station" in cat and "gas" not in cat or cat in ("bus_stop", "taxi_stand", "transportation"):
        return "transit"
    if any(w in cat for w in ("hospital", "pharmacy", "clinic", "emergency", "gas_station", "hotel")):
        return "haven"
    return "place"


def classify(tags):
    a, shop = tags.get("amenity"), tags.get("shop")
    if "overture" in tags:
        return classify_overture(tags["overture"])
    if tags.get("building") == "guardhouse" or a == "security_booth":
        return "guard"
    if tags.get("highway") == "street_lamp":
        return "lamp"
    if tags.get("highway") in ("crossing", "traffic_signals"):
        return "crossing"
    if tags.get("man_made") == "surveillance":
        return "cctv"
    if tags.get("emergency") == "phone":
        return "sos_phone"
    if a == "police":
        return "police"
    if tags.get("hazard") in ("blackspot", "waterlogging"):
        return tags["hazard"]  # from data/layers
    if shop == "alcohol" or a in ("bar", "pub", "nightclub"):
        return "nightlife"
    if a in ("bus_station", "taxi") or tags.get("highway") == "bus_stop" or tags.get("railway") in ("station", "subway_entrance"):
        return "transit"
    if a in ("hospital", "clinic", "pharmacy", "fuel", "fire_station") or tags.get("tourism") == "hotel":
        return "haven"
    if shop or tags.get("office") or a in PLACE_AMENITIES:
        return "place"
    if tags.get("building"):
        return "building"  # counted per grid cell, not kept as a place; a shop mapped as a building stays a shop
    return None


def surroundings_kind(tags):
    for key in ("landuse", "natural", "leisure"):
        if tags.get(key) in SURROUNDINGS:
            return tags[key]
    if tags.get("railway") == "rail":
        return "railway"
    return None


class Area:
    def __init__(self, osm, reports=(), incidents=(), stops=()):
        self.nodes = {}
        self.adj = defaultdict(list)
        self.edges = []
        self.pois = []
        self.grid = defaultdict(list)
        self.building_cells = defaultdict(int)  # Overture footprints (OSM plus Google and Microsoft)
        self.osm_building_cells = defaultdict(int)
        self.night_cells = {}  # NASA night-light pixel -> brightness 0..1
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
                if "nightlight" in tags:
                    self.night_cells[(int(pt[0] // NIGHT_CELL), int(pt[1] // NIGHT_CELL))] = float(tags["nightlight"])
                elif kind == "building":  # imported OSM outlines come pre-counted per cell
                    cells = self.building_cells if str(el["id"]).startswith("ovb") else self.osm_building_cells
                    cells[self._cell(pt)] += int(tags.get("building_count", 1))
                elif kind:
                    self.grid[self._cell(pt)].append(len(self.pois))
                    self.pois.append({"kind": kind, "pt": pt, "hours": tags.get("opening_hours"),
                                      "source": tags.get("layer") or ("Mapillary" if tags.get("source") == "mapillary" else "Overture" if "overture" in tags else "OpenStreetMap"),
                                      "name": tags.get("name") or (tags.get("shop") or tags.get("amenity") or tags.get("overture") or kind).replace("_", " ")})

        if not self.building_cells:  # Overture already includes OSM buildings, so never add both
            self.building_cells = self.osm_building_cells
        self.police = [p["pt"] for p in self.pois if p["kind"] == "police"]
        self.stops = [s for s in stops if any(s["deps"][d][h] for d in range(7) for h in range(24))]
        self.stop_grid = defaultdict(list)
        for i, st in enumerate(self.stops):
            self.stop_grid[self._cell((st["lat"], st["lon"]))].append(i)
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
        detail = {"surface": tags.get("surface"), "smoothness": tags.get("smoothness"),
                  "lanes": number(tags.get("lanes")), "maxspeed": number(tags.get("maxspeed"))}
        if detail["maxspeed"] and "mph" in tags["maxspeed"]:
            detail["maxspeed"] *= 1.6
        for (a, pa), (b, pb) in zip(zip(el["nodes"], pts), zip(el["nodes"][1:], pts[1:])):
            self.nodes[a], self.nodes[b] = pa, pb
            ei = len(self.edges)
            self.edges.append({"a": a, "b": b, "length": dist(pa, pb), "highway": hw, "name": tags.get("name"),
                               "lit": tags.get("lit"), "sidewalk": sidewalk, "underpass": underpass,
                               "overbridge": overbridge, "reports": [], "incidents": [], **detail})
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
            if p["kind"] != "police" and seg_dist(p["pt"], pa, pb) <= NEAR_M.get(p["kind"], 60):
                near[p["kind"]].append(i)
        e["near"] = near
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
        e["nightlight"] = self.night_cells.get((int(mid[0] // NIGHT_CELL), int(mid[1] // NIGHT_CELL)))
        k = int(STOP_RADIUS_M / 100) + 1
        e["stops"] = [i for di in range(-k, k + 1) for dj in range(-k, k + 1) for i in self.stop_grid.get((c[0] + di, c[1] + dj), ())
                      if seg_dist((self.stops[i]["lat"], self.stops[i]["lon"]), pa, pb) <= STOP_RADIUS_M]

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
        if "hour" not in r:  # when it was seen, for observations that only hold at that time of day
            t = datetime.fromtimestamp(r["ts"])
            r = {**r, "hour": t.hour, "weekday": t.weekday()}
        self.reports.append(r)
        self._attach(r, REPORT_RADIUS_M, "reports")

    def add_incident(self, i):
        self.incidents.append(i)
        self._attach(i, INCIDENT_RADIUS_M, "incidents")

    # ---- scoring ----

    def is_place_open(self, p, when):
        """True or False from the place's published opening hours, None when it has none."""
        return is_open(p["hours"], when)

    def light(self, when):
        return daylight(*self.center, when)

    def ratings(self, when, rain=False):
        """(score, confidence) for every segment at this time."""
        light = self.light(when)
        key = (when.weekday(), when.hour, round(light, 1), rain)
        if key not in self._cache:
            if len(self._cache) > 200:
                self._cache.clear()
            now = time.time()
            self._cache[key] = [rating.rate(self, e, when, light, rain, now)[:2] for e in self.edges]
        return self._cache[key]

    # ---- routing ----

    def nearest(self, pt):
        # shortcut: linear scan, fine for a campus-sized area; use the grid if the area grows to a city
        return min(self.routable, key=lambda n: dist(pt, self.nodes[n]))

    def _search(self, src, dst, lam, credits, penalty=None):
        best, prev = {src: 0.0}, {}
        heap = [(0.0, src)]
        while heap:
            d, u = heapq.heappop(heap)
            if u == dst:
                break
            if d > best[u]:
                continue
            for v, ei in self.adj[u]:
                nd = d + self.edges[ei]["length"] * (1 + lam * (1 - credits[ei] / 100)) * (1 + (penalty or {}).get(ei, 0))
                if nd < best.get(v, math.inf):
                    best[v], prev[v] = nd, (u, ei)
                    heapq.heappush(heap, (nd, v))
        path, n = [], dst
        while n != src:
            n, ei = prev[n]
            path.append((n, ei))
        path.reverse()
        return path

    def candidates(self, src, dst, credits):
        """Distinct ways from src to dst: one per safety weighting, plus detours that avoid the streets already found."""
        found = []
        for lam in SEARCH_LAMBDAS:
            found.append(self._search(src, dst, lam, credits))
        for lam in (0, 3, 10):
            penalty = defaultdict(float)
            path = found[SEARCH_LAMBDAS.index(lam)]
            for _ in range(DETOUR_ROUNDS):
                for _, ei in path:
                    penalty[ei] += 0.6
                path = self._search(src, dst, lam, credits, penalty)
                found.append(path)
        length = lambda p: sum(self.edges[ei]["length"] for _, ei in p)
        fastest = min(length(p) for p in found) or 1
        kept = []  # (path, its edges, its length), shortest first
        for p in sorted(found, key=length):
            edges, lp = {ei for _, ei in p}, length(p)
            if lp > MAX_DETOUR * fastest:
                continue
            # sharing 90% of its length with a kept candidate makes it the same way with a small wiggle
            if any(sum(self.edges[ei]["length"] for ei in edges & k) >= 0.9 * min(lp, lk) for _, k, lk in kept):
                continue
            kept.append((p, edges, lp))
        return [p for p, _, _ in kept]

    def routes(self, start, end, when, rain=False):
        """Every candidate route is scored; returns the safest, a balanced one (at most 25% longer) and the fastest."""
        rated = self.ratings(when, rain)
        credits = [rating.credit(s, c) for s, c in rated]
        src, dst = self.nearest(start), self.nearest(end)
        paths = self.candidates(src, dst, credits)
        fastest_len = sum(self.edges[ei]["length"] for _, ei in paths[0]) or 1  # candidates come shortest first
        scored = [self._summary("", p, rated, when, fastest_len, dst) for p in paths]
        ranked = sorted(scored, key=lambda r: (-r["score"], r["length_m"]))
        fastest = scored[0]
        safest = ranked[0]
        balanced = next(r for r in ranked if r["length_m"] <= BALANCED_DETOUR * fastest["length_m"] or r is fastest)
        out = []
        for kind, r in (("safest", safest), ("fastest", fastest), ("balanced", balanced)):  # a repeat points at safest or fastest
            out.append({**r, "kind": kind, "checked": len(scored), "rank": ranked.index(r) + 1,
                        "same_as": next((o["kind"] for o in out if o["coords"] == r["coords"]), None)})
        return [out[0], out[2], out[1]]

    def _summary(self, kind, path, rated, when, fastest_len, dst):
        length = sum(self.edges[ei]["length"] for _, ei in path)
        coords = [self.nodes[n] for n, _ in path] + [self.nodes[dst]]
        light = self.light(when)
        base = {"kind": kind, "coords": coords, "length_m": round(length), "minutes": max(1, round(length / WALK_MPS / 60)),
                "daylight": round(light, 2)}
        if not path:
            return {**base, "minutes": 0, "score": 100, "confidence": 1.0, "confidence_level": "high", "unrated_pct": 0,
                    "reasons": [], "warnings": [], "factors": {}, "sources": [], "weakest": None, "havens": [], "places": [], "turns": 0,
                    "steps": [], "segments": []}
        now = time.time()
        rated_len = sum(self.edges[ei]["length"] for _, ei in path if rated[ei][1] >= rating.MIN_CONFIDENCE) or 0.0
        known = [(i, ei) for i, (_, ei) in enumerate(path) if rated[ei][1] >= rating.MIN_CONFIDENCE]
        avg = sum(rated[ei][0] * self.edges[ei]["length"] for _, ei in known) / (rated_len or 1)
        conf = sum(rated[ei][1] * self.edges[ei]["length"] for _, ei in path) / (length or 1)
        weak_i = min((i for i, _ in known), key=lambda i: rated[path[i][1]][0], default=None)

        isolated = run = 0.0
        haven_gap = gap = 0.0
        factor_sum, factor_len, factor_q = defaultdict(float), defaultdict(float), defaultdict(float)
        places, havens, cctv, unknown_hours, mapped, sources = set(), {}, set(), set(), set(), set()
        lit_len = dark_len = 0.0
        police_m = math.inf
        underpasses = 0
        zones = set()
        for _, ei in path:
            e = self.edges[ei]
            feats = rating.factors(self, e, when, light, now)
            for k, f in feats.items():
                if f:
                    factor_sum[k] += f["value"] * e["length"]
                    factor_len[k] += e["length"]
                    factor_q[k] += f["quality"] * e["length"]
                    sources.update(f["sources"])
            run = run + e["length"] if rated[ei][1] >= rating.MIN_CONFIDENCE and rated[ei][0] < 40 else 0.0
            isolated = max(isolated, run)
            open_here = [i for i in e["near"]["place"] + e["near"]["haven"] if self.is_place_open(self.pois[i], when)]
            gap = 0.0 if open_here else gap + e["length"]
            haven_gap = max(haven_gap, gap)
            places.update(open_here)
            for i in open_here:
                havens.setdefault(i, len(havens))
            cctv.update(e["near"]["cctv"])
            mapped.update(e["near"]["place"] + e["near"]["haven"])
            unknown_hours.update(i for i in e["near"]["place"] + e["near"]["haven"] if self.is_place_open(self.pois[i], when) is None)
            if feats["lighting"]:
                lit_len += e["length"] if feats["lighting"]["value"] >= 0.5 else 0
                dark_len += e["length"] if feats["lighting"]["value"] < 0.5 else 0
            police_m = min(police_m, e["police_m"])
            underpasses += e["underpass"]
            zones.update(e["zones"])

        if weak_i is None:
            score = 0  # nothing on this route is measured well enough to rate
        else:
            score = 0.6 * avg + 0.4 * rated[path[weak_i][1]][0]  # weakest link drags the whole route down
            score -= min(15, isolated / 50)
            score -= min(10, max(0, haven_gap - 300) / 50) if places else 0
            score -= max(0, length / fastest_len - 1.3) * 20

        steps = self._steps(path, coords)
        turns = sum(st["turn"] not in ("straight", "arrive", "depart") for st in steps)

        unrated_pct = round(100 * (1 - rated_len / length)) if length else 0
        no_light = length - lit_len - dark_len
        reasons = [f"{len(mapped)} shops and places along the way", f"{len(places)} confirmed open now"]
        if unknown_hours:
            reasons.append(f"{len(unknown_hours)} with no published hours")
        main = sum(self.edges[ei]["length"] for _, ei in path if self.edges[ei]["highway"] in rating.MAIN_ROADS)
        if main >= 0.1 * length:
            reasons.append(f"{round(100 * main / length)}% on main roads")
        if light < 1:
            reasons.append(f"{round(100 * lit_len / length)}% lit, {round(100 * dark_len / length)}% unlit")
            if no_light >= 1:
                reasons.append(f"No lighting data for {round(100 * no_light / length)}%")
        if cctv:
            reasons.append(f"{len(cctv)} CCTV cameras")
        if police_m < 800:
            reasons.append(f"Police within {round(police_m)} m")
        warnings = []
        if unrated_pct >= 10:
            warnings.append(f"{unrated_pct}% of this route has too little data to rate")
        if isolated >= 100:
            warnings.append(f"{round(isolated)} m isolated stretch")
        if places and haven_gap >= 300:
            warnings.append(f"{round(haven_gap)} m with nowhere known to be open")
        if underpasses:
            warnings.append("Goes through an underpass")
        for z in sorted(zones):
            if SURROUNDINGS[z][1] * (1 - light) + SURROUNDINGS[z][0] * light < 0.5:
                warnings.append(f"Passes {z.replace('_', ' ')} land")
        weakest = None
        if weak_i is not None:
            weak = self.edges[path[weak_i][1]]
            w = rating.weights(light)
            wf = rating.factors(self, weak, when, light, now)
            why = max((k for k, f in wf.items() if f and w[k] > 0), key=lambda k: w[k] * (1 - wf[k]["value"]))
            weakest = {"coords": [self.nodes[weak["a"]], self.nodes[weak["b"]]], "score": round(rated[path[weak_i][1]][0]),
                       "name": weak["name"] or weak["highway"].replace("_", " "), "why": LABELS[why]}
        return {
            **base,
            "score": round(max(0, min(100, score))),
            "confidence": round(conf, 2),
            "confidence_level": rating.level(conf),
            "unrated_pct": unrated_pct,
            "reasons": reasons,
            "warnings": warnings,
            # per factor: average value and how well it was measured along the route, None when nothing measured it
            "factors": {LABELS[k]: {"score": round(100 * factor_sum[k] / factor_len[k]),
                                    "measured": round(100 * factor_q[k] / length)} if factor_len[k] else None
                        for k in rating.WEIGHTS if rating.weights(light)[k] > 0},
            "sources": sorted(sources),
            "turns": turns,
            "steps": steps,
            "segments": [[*self.nodes[self.edges[ei]["a"]], *self.nodes[self.edges[ei]["b"]], round(rated[ei][0]), round(rated[ei][1], 2)]
                         for _, ei in path if rated[ei][1] < rating.MIN_CONFIDENCE],
            "weakest": weakest,
            "havens": [{"name": self.pois[i]["name"], "pt": self.pois[i]["pt"]} for i in list(havens)[:30]],
            # every shop and place along the way; open is None when it publishes no hours
            "places": [{"name": self.pois[i]["name"], "pt": self.pois[i]["pt"], "open": self.is_place_open(self.pois[i], when)}
                       for i in sorted(mapped)[:300]],
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
        """[lat1, lon1, lat2, lon2, score, confidence] per segment in view."""
        rated = self.ratings(when, rain)
        s, w, n, e = bbox or self.bounds
        return [[*self.nodes[ed["a"]], *self.nodes[ed["b"]], round(sc), round(c, 2)] for ed, (sc, c) in zip(self.edges, rated)
                if s <= ed["mid"][0] <= n and w <= ed["mid"][1] <= e]

    def explain(self, pt, when, rain=False):
        """The nearest segment's score, confidence and every factor with its sources."""
        light = self.light(when)
        ei = min(range(len(self.edges)), key=lambda i: seg_dist(pt, self.nodes[self.edges[i]["a"]], self.nodes[self.edges[i]["b"]]))
        e = self.edges[ei]
        score, conf, feats = rating.rate(self, e, when, light, rain)
        w = rating.weights(light)
        return {"name": e["name"] or e["highway"].replace("_", " "), "score": round(score), "confidence": round(conf, 2),
                "confidence_level": rating.level(conf), "coords": [self.nodes[e["a"]], self.nodes[e["b"]]],
                "factors": [{"factor": LABELS[k], "weight": round(w[k]), "score": round(100 * f["value"]) if f else None,
                             "quality": round(f["quality"], 2) if f else 0, "sources": f["sources"] if f else []}
                            for k, f in feats.items() if w[k] > 0]}
