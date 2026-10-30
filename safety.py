"""Time-of-day safety scoring and safest-route search over an OSM walking graph."""
import heapq
import math
import re
import time
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone

import buses
import rating
import traffic
from rating import LABELS, SURROUNDINGS

# Average speeds for travel times: walking, a city bike and a car in city traffic
MODES = {"walk": 1.3, "bike": 4.2, "car": 6.9}
WALK_MPS = MODES["walk"]
CAR_ROADS = rating.VEHICLE_ROADS | {"service", "living_street", "trunk_link", "motorway", "motorway_link"}
ONEWAY = {"yes": 1, "1": 1, "true": 1, "-1": -1, "reverse": -1}
RATE_BUCKET_S = 300  # streets are rated for the 5 minutes in which the trip passes them
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
# taps from people on the street that say how busy it is, shown on the route card
FOOT_TAPS = {"people_around": "said it's busy", "shops_open": "saw shops open", "deserted": "said it's deserted", "shops_closed": "saw shops shut"}
HELP_GROUPS = {"pharmacy", "hospital", "fuel", "police"}  # places to step into for help: open ones, and police at any hour
# Places a user can ask for more of along the way (weight 0-3 each, from the app's settings). Open ones count; stops have no hours.
PRIORITIES = {"pharmacy": "medical stores", "hospital": "hospitals", "food": "places to eat", "shop": "shops", "fuel": "petrol pumps", "transit": "bus and metro stops"}
PRIORITY_CAP = 10  # most a route's score can gain from priorities, so safety still decides

# Extra signals other features plug in (live event intel, for example): fn(pt, when) -> None or
# {"delta": points added to that street's score, "warning" or "reason": text shown on the route}.
# Use delta 0 for awareness only. Physical and situational conditions only: never who lives somewhere.
SIGNALS = []
NEAR_M = {"lamp": 20, "cctv": 50, "transit": 150, "crossing": 15, "sos_phone": 100, "blackspot": 100, "waterlogging": 50}


def fmt_m(m):
    return f"{round(m / 10) * 10} m" if m < 1000 else f"{m / 1000:.1f} km"


def number(v):
    """Leading number of an OSM value such as "4" or "50 km/h", None when there is none."""
    m = re.match(r"\s*(\d+)", v or "")
    return int(m.group(1)) if m else None


def why_ahead(r, others, fastest):
    """Plain reasons a route ranks where it does, measured against the best of the other scanned routes."""
    if not others:
        return ["It's the only sensible way there on the map."]
    rival = others[0]
    a, b = r["stats"], rival["stats"]
    if not a or not b:
        return []
    ahead = r["score"] >= rival["score"]
    out = [f"Scores {r['score']} against {rival['score']} for the {'next best' if ahead else 'top'} of the {len(others) + 1} ways scanned."]
    diffs = [
        (a["lit_pct"] - b["lit_pct"], 10, lambda d: f"{'More' if d > 0 else 'Less'} of it is lit: {a['lit_pct']}% vs {b['lit_pct']}%."),
        (a["open_now"] - b["open_now"], 1, lambda d: f"{a['open_now']} places confirmed open on the way, vs {b['open_now']}."),
        (b["isolated_m"] - a["isolated_m"], 50, lambda d: f"Longest poorly rated stretch is {a['isolated_m']} m, vs {b['isolated_m']} m."),
        (b["nothing_open_m"] - a["nothing_open_m"], 100, lambda d: f"Longest stretch with nothing open is {a['nothing_open_m']} m, vs {b['nothing_open_m']} m."),
        (rival["unrated_pct"] - r["unrated_pct"], 10, lambda d: f"{r['unrated_pct']}% of it is unrated, vs {rival['unrated_pct']}%."),
        (a["cctv"] - b["cctv"], 1, lambda d: f"{a['cctv']} CCTV cameras along it, vs {b['cctv']}."),
    ]
    good, bad = [], []
    for d, step, text in diffs:
        if abs(d) >= step:
            (good if d > 0 else bad).append(text(d))
    for k, f in r["factors"].items():
        g = rival["factors"].get(k)
        if f and g and abs(f["score"] - g["score"]) >= 10:
            (good if f["score"] > g["score"] else bad).append(f"{k}: {f['score']} vs {g['score']}.")
    out += good[:4] + [f"But: {t[0].lower()}{t[1:]}" for t in bad[:2]]
    extra = r["minutes"] - fastest["minutes"]
    if extra > 0:
        out.append(f"It takes {extra} min longer than the fastest way.")
    return out


def headline(r, fastest):
    """One plain line on what the extra time buys, against the fastest way."""
    extra = r["minutes"] - fastest["minutes"]
    if r is fastest or not r["stats"] or not fastest["stats"]:
        return None
    a, b = r["stats"], fastest["stats"]
    per_km = lambda x, s: s["open_now"] / max(0.1, x["length_m"] / 1000)
    gains = []
    if b["nothing_open_m"] - a["nothing_open_m"] >= 100:
        gains.append(f"a shorter stretch with nothing open ({a['nothing_open_m']} m vs {b['nothing_open_m']} m)")
    if per_km(r, a) - per_km(fastest, b) >= 0.5:
        gains.append(f"more places open per km ({per_km(r, a):.1f} vs {per_km(fastest, b):.1f})")
    if a["lit_pct"] - b["lit_pct"] >= 10:
        gains.append(f"more of it lit ({a['lit_pct']}% vs {b['lit_pct']}%)")
    if a["main_pct"] - b["main_pct"] >= 10:
        gains.append(f"more on main roads ({a['main_pct']}% vs {b['main_pct']}%)")
    if a["isolated_m"] < b["isolated_m"] - 50:
        gains.append(f"a shorter poorly rated stretch ({a['isolated_m']} m vs {b['isolated_m']} m)")
    if not gains:
        return None
    lead = f"For {extra} extra {'minute' if extra == 1 else 'minutes'}" if extra > 0 else "For no extra time"
    return f"{lead}, this way has {', '.join(gains[:-1]) + ' and ' + gains[-1] if len(gains) > 1 else gains[0]}."


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


def place_group(tags, kind):
    """What a place is for the map pins and the closer-look counts: pharmacy, hospital, fuel, food, shop, transit, police or other."""
    a, cat = tags.get("amenity"), tags.get("overture", "")
    if kind in ("police", "transit", "cctv"):
        return kind
    if a in ("pharmacy", "clinic", "doctors", "dentist") or tags.get("shop") in ("chemist", "medical_supply") or "pharmacy" in cat or "clinic" in cat:
        return "pharmacy"
    if a == "hospital" or "hospital" in cat:
        return "hospital"
    if a == "fuel" or "gas_station" in cat:
        return "fuel"
    if a in ("restaurant", "cafe", "fast_food", "food_court", "ice_cream") or any(w in cat for w in ("restaurant", "cafe", "food", "bakery")):
        return "food"
    if tags.get("shop") or tags.get("overture"):
        return "shop"
    return "other"


def surroundings_kind(tags):
    for key in ("landuse", "natural", "leisure"):
        if tags.get(key) in SURROUNDINGS:
            return tags[key]
    if tags.get("railway") == "rail":
        return "railway"
    return None


class Area:
    def __init__(self, osm, reports=(), incidents=(), stops=(), unloaded=None):
        self.unloaded = unloaded or {}  # factor -> (s, w, n, e) boxes whose data hasn't loaded yet: unknown there, not zero
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
                    self.pois.append({"kind": kind, "pt": pt, "hours": tags.get("opening_hours"), "group": place_group(tags, kind),
                                      "source": tags.get("layer") or {"mapillary": "Mapillary", "geoapify": "Geoapify"}.get(tags.get("source"))
                                      or ("Overture" if "overture" in tags else "OpenStreetMap"),
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
        # per way of travel: None = not allowed, 0 = both directions, 1 = only a to b, -1 = only b to a (walking ignores oneway)
        oneway = 1 if tags.get("junction") == "roundabout" else ONEWAY.get(tags.get("oneway"), 0)
        car = hw in CAR_ROADS and not {tags.get("motor_vehicle"), tags.get("motorcar")} & {"no", "private"}
        bike = hw != "steps" and tags.get("bicycle") not in ("no", "dismount") and (
            hw not in ("footway", "pedestrian") or tags.get("bicycle") in ("yes", "designated", "permissive"))
        modes = {"walk": 0, "bike": (0 if tags.get("oneway:bicycle") == "no" or hw == "cycleway" else oneway) if bike else None,
                 "car": oneway if car else None}
        for (a, pa), (b, pb) in zip(zip(el["nodes"], pts), zip(el["nodes"][1:], pts[1:])):
            self.nodes[a], self.nodes[b] = pa, pb
            ei = len(self.edges)
            self.edges.append({"a": a, "b": b, "length": dist(pa, pb), "highway": hw, "name": tags.get("name"),
                               "lit": tags.get("lit"), "sidewalk": sidewalk, "underpass": underpass,
                               "overbridge": overbridge, "reports": [], "incidents": [], "modes": modes, **detail})
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
        e["places_mapped"] = any(self.pois[i]["kind"] in ("place", "haven") for i, _ in self._near(mid, 500))
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
        if rating.OBSERVATIONS.get(r["type"], ("",))[0] == "lighting" and "dark" not in r:
            # nobody can tell whether a street light works in daylight, so only reports made after dark count
            r = {**r, "dark": daylight(r["lat"], r["lon"], datetime.fromtimestamp(r["ts"], timezone.utc)) < 0.5}
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

    def ratings(self, when, rain=False, mode="walk"):
        """(score, confidence) for every segment at this time."""
        light = self.light(when)
        # to the minute: opening hours end at any minute; live buses and traffic change, so entries last 5 minutes at most
        key = (when.weekday(), when.hour, when.minute, round(light, 1), rain, mode, int(time.time() // 300))
        if key not in self._cache:
            if len(self._cache) > 200:
                self._cache.clear()
            now = time.time()
            self._cache[key] = [rating.rate(self, e, when, light, rain, now, mode)[:2] for e in self.edges]
        return self._cache[key]

    # ---- routing ----

    def nearest(self, pt, mode="walk"):
        """The closest street node this way of travel can use (a car starts on the nearest road, not a footpath)."""
        # shortcut: linear scan, fine for a campus-sized area; use the grid if the area grows to a city
        usable = [n for n in self.routable if any(self.edges[ei]["modes"][mode] is not None for _, ei in self.adj[n])]
        if not usable:
            raise ValueError(f"No streets usable by {mode} are mapped around there")
        return min(usable, key=lambda n: dist(pt, self.nodes[n]))

    def _search(self, src, dst, lam, credits, penalty=None, mode="walk"):
        best, prev = {src: 0.0}, {}
        heap = [(0.0, src)]
        while heap:
            d, u = heapq.heappop(heap)
            if u == dst:
                break
            if d > best[u]:
                continue
            for v, ei in self.adj[u]:
                rule = self.edges[ei]["modes"][mode]
                if rule is None or (rule and (u == self.edges[ei]["a"]) != (rule == 1)):
                    continue  # not allowed this way, or against a one-way street
                nd = d + self.edges[ei]["length"] * (1 + lam * (1 - credits[ei] / 100)) * (1 + (penalty or {}).get(ei, 0))
                if nd < best.get(v, math.inf):
                    best[v], prev[v] = nd, (u, ei)
                    heapq.heappush(heap, (nd, v))
        if dst not in prev and dst != src:
            raise ValueError(f"No way through the mapped streets by {mode} between these two points")
        path, n = [], dst
        while n != src:
            n, ei = prev[n]
            path.append((n, ei))
        path.reverse()
        return path

    def candidates(self, src, dst, credits, mode="walk", max_extra_m=None):
        """Distinct ways from src to dst: one per safety weighting, plus detours that avoid the streets already found.
        Ways longer than MAX_DETOUR times the shortest, or more than max_extra_m longer, are dropped."""
        found = []
        for lam in SEARCH_LAMBDAS:
            found.append(self._search(src, dst, lam, credits, mode=mode))
        for lam in (0, 3, 10):
            penalty = defaultdict(float)
            path = found[SEARCH_LAMBDAS.index(lam)]
            for _ in range(DETOUR_ROUNDS):
                for _, ei in path:
                    penalty[ei] += 0.6
                path = self._search(src, dst, lam, credits, penalty, mode)
                found.append(path)
        length = lambda p: sum(self.edges[ei]["length"] for _, ei in p)
        fastest = min(length(p) for p in found) or 1
        kept = []  # (path, its edges, its length), shortest first
        for p in sorted(found, key=length):
            edges, lp = {ei for _, ei in p}, length(p)
            if lp > MAX_DETOUR * fastest or (max_extra_m is not None and lp > fastest + max_extra_m):
                continue
            # sharing 90% of its length with a kept candidate makes it the same way with a small wiggle
            if any(sum(self.edges[ei]["length"] for ei in edges & k) >= 0.9 * min(lp, lk) for _, k, lk in kept):
                continue
            kept.append((p, edges, lp))
        return [p for p, _, _ in kept]

    def routes(self, start, end, when, rain=False, mode="walk", max_extra_min=None, via=(), prefer=None):
        """Every candidate route is scored; returns the safest, a balanced one (at most 25% longer), the fastest,
        then every other scanned route by rank (kind "#4" and so on).
        Each street is rated for the moment the trip reaches it, so a shop that shuts before you get there doesn't count.
        via: stops visited in order. prefer: {place group: 0-3} the user wants more of on the way."""
        rated = self.ratings(when, rain, mode)
        credits = [rating.credit(s, c) for s, c in rated]
        stops = [self.nearest(p, mode) for p in (start, *via, end)]
        dst = stops[-1]
        max_extra_m = None if max_extra_min is None else max_extra_min * 60 * MODES[mode]
        legs = [self.candidates(a, b, credits, mode, max_extra_m) for a, b in zip(stops, stops[1:])]
        # shortcut: the k-th candidate of every leg joined, not every combination; enough variety for a few stops
        paths = [[step for leg in legs for step in leg[min(k, len(leg) - 1)]] for k in range(max(map(len, legs)))]
        fastest_len = sum(self.edges[ei]["length"] for _, ei in paths[0]) or 1  # each leg's candidates come shortest first
        passing = {}  # (edge, 5-minute slot) -> (score, confidence), shared by all candidates
        scored = [self._summary("", p, when, fastest_len, dst, rain, mode, passing, prefer) for p in paths]
        ranked = sorted(scored, key=lambda r: (-r["score"], r["length_m"]))  # the score shown is the score ranked
        fastest = scored[0]
        safest = ranked[0]
        balanced = next(r for r in ranked if r["length_m"] <= BALANCED_DETOUR * fastest["length_m"] or r is fastest)
        compared = [{"rank": i + 1, "score": r["score"], "confidence_level": r["confidence_level"], "unrated_pct": r["unrated_pct"],
                     "minutes": r["minutes"], "length_m": r["length_m"], "stats": r["stats"], "factors": r["factors"]}
                    for i, r in enumerate(ranked)]
        out = []
        picks = [("safest", safest), ("fastest", fastest), ("balanced", balanced)]  # a repeat points at safest or fastest
        picks += [(f"#{i + 1}", r) for i, r in enumerate(ranked) if all(r is not p for _, p in picks)]  # the rest, to look at on request
        for kind, r in picks:
            out.append({**r, "kind": kind, "mode": mode, "checked": len(scored), "rank": ranked.index(r) + 1, "compared": compared,
                        "max_extra_min": max_extra_min, "why": why_ahead(r, [x for x in ranked if x is not r], fastest),
                        "headline": headline(r, fastest),
                        "same_as": next((o["kind"] for o in out if o["coords"] == r["coords"]), None)})
        return [out[0], out[2], out[1], *out[3:]]

    def _summary(self, kind, path, when, fastest_len, dst, rain=False, mode="walk", passing=None, prefer=None):
        length = sum(self.edges[ei]["length"] for _, ei in path)
        coords = [self.nodes[n] for n, _ in path] + [self.nodes[dst]]
        light = self.light(when)
        speed = MODES[mode]
        base = {"kind": kind, "coords": coords, "length_m": round(length), "minutes": max(1, round(length / speed / 60)),
                "daylight": round(light, 2)}
        if not path:
            return {**base, "minutes": 0, "score": 100, "confidence": 1.0, "confidence_level": "high", "unrated_pct": 0,
                    "reasons": [], "warnings": [], "factors": {}, "sources": [], "weakest": None, "havens": [], "places": [], "stats": {}, "turns": 0,
                    "steps": [], "segments": [], "look": {}, "strip": {}}
        now = time.time()
        passing = {} if passing is None else passing
        # when the trip reaches each street, in 5-minute slots, and the street's rating for that moment
        at, travelled = [], 0.0
        for _, ei in path:
            at.append(when + timedelta(seconds=RATE_BUCKET_S * int((travelled + self.edges[ei]["length"] / 2) / speed // RATE_BUCKET_S)))
            travelled += self.edges[ei]["length"]
        rated = {}
        for (_, ei), t in zip(path, at):
            key = (ei, t)
            if key not in passing:
                passing[key] = rating.rate(self, self.edges[ei], t, self.light(t), rain, now, mode)[:2]
            rated[ei] = passing[key]
        signal_m = defaultdict(float)  # (field, text) -> metres of route it applies to
        for (_, ei), t in zip(path, at):
            for fn in SIGNALS:
                try:
                    s = fn(self.edges[ei]["mid"], t)
                except Exception as err:  # one broken plug-in must not break routing
                    print(f"signal failed: {err}", flush=True)
                    continue
                if not s:
                    continue
                if s.get("delta"):
                    rated[ei] = (max(0, min(100, rated[ei][0] + s["delta"])), rated[ei][1])
                for field in ("reason", "warning"):
                    if s.get(field):
                        signal_m[(field, s[field])] += self.edges[ei]["length"]
        rated_len = sum(self.edges[ei]["length"] for _, ei in path if rated[ei][1] >= rating.MIN_CONFIDENCE) or 0.0
        known = [(i, ei) for i, (_, ei) in enumerate(path) if rated[ei][1] >= rating.MIN_CONFIDENCE]
        avg = sum(rated[ei][0] * self.edges[ei]["length"] for _, ei in known) / (rated_len or 1)
        conf = sum(rated[ei][1] * self.edges[ei]["length"] for _, ei in path) / (length or 1)
        weak_i = min((i for i, _ in known), key=lambda i: rated[path[i][1]][0], default=None)

        isolated = run = 0.0
        haven_gap = gap = help_gap = hgap = 0.0
        factor_sum, factor_len, factor_q = defaultdict(float), defaultdict(float), defaultdict(float)
        places, havens, cctv, unknown_hours, mapped, sources = set(), {}, set(), set(), set(), set()
        place_at, place_open = {}, {}  # place -> metres into the trip, open when the trip passes it
        gaps, darks, dark_from = [], [], None
        lit_len = dark_len = main_len = 0.0
        police_m = math.inf
        underpasses = 0
        zones = set()
        done = 0.0
        bus_ids, live_buses, jammed, said = set(), False, 0.0, {}  # said: report -> its type, once per report
        for (_, ei), t in zip(path, at):
            e = self.edges[ei]
            lt = self.light(t)
            feats = rating.factors(self, e, t, lt, now)
            for k, f in feats.items():
                if f:
                    factor_sum[k] += f["value"] * e["length"]
                    factor_len[k] += e["length"]
                    factor_q[k] += f["quality"] * e["length"]
                    sources.update(f["sources"])
            run = run + e["length"] if rated[ei][1] >= rating.MIN_CONFIDENCE and rated[ei][0] < 40 else 0.0
            isolated = max(isolated, run)
            near_places = e["near"]["place"] + e["near"]["haven"] + e["near"]["transit"] + e["near"]["cctv"]
            for i in near_places:
                if i not in place_at:
                    place_at[i] = done + e["length"] / 2
                    place_open[i] = self.is_place_open(self.pois[i], t)
            open_here = [i for i in e["near"]["place"] + e["near"]["haven"] if place_open[i]]
            if open_here:
                if gap >= 300:
                    gaps.append([round((done - gap) / length, 3), round(done / length, 3)])
                gap = 0.0
            else:
                gap += e["length"]
            haven_gap = max(haven_gap, gap)
            helped = any(self.pois[i]["group"] in HELP_GROUPS and place_open[i] for i in open_here) or e["police_m"] <= 150
            hgap = 0.0 if helped else hgap + e["length"]
            help_gap = max(help_gap, hgap)
            places.update(open_here)
            for i in open_here:
                havens.setdefault(i, len(havens))
            cctv.update(e["near"]["cctv"])
            mapped.update(e["near"]["place"] + e["near"]["haven"])
            unknown_hours.update(i for i in e["near"]["place"] + e["near"]["haven"] if place_open[i] is None)
            street_light = feats["lighting"] and feats["lighting"]["street"]
            dark_here = bool(street_light) and street_light["value"] < 0.5 and lt < 1
            if street_light:
                lit_len += e["length"] if street_light["value"] >= 0.5 else 0
                dark_len += e["length"] if street_light["value"] < 0.5 else 0
            if dark_here and dark_from is None:
                dark_from = done
            elif not dark_here and dark_from is not None:
                if done - dark_from >= 100:
                    darks.append([round(dark_from / length, 3), round(done / length, 3)])
                dark_from = None
            main_len += e["length"] if e["highway"] in rating.MAIN_ROADS else 0
            police_m = min(police_m, e["police_m"])
            underpasses += e["underpass"]
            zones.update(e["zones"])
            passed = buses.LIVE.near(e["mid"], t, now)
            live_buses |= passed is not None
            bus_ids |= passed or set()
            jammed += e["length"] if e["highway"] in rating.MAIN_ROADS and traffic.LIVE.jammed(e["mid"], t, now) else 0
            said.update((id(r), r["type"]) for r in rating.votes(e["reports"]) if r["type"] in FOOT_TAPS and rating.same_time(r, t))
            done += e["length"]
        if gap >= 300:
            gaps.append([round((done - gap) / length, 3), 1.0])
        if dark_from is not None and done - dark_from >= 100:
            darks.append([round(dark_from / length, 3), 1.0])

        if weak_i is None:
            score = 0  # nothing on this route is measured well enough to rate
        else:
            score = 0.6 * avg + 0.4 * rated[path[weak_i][1]][0]  # weakest link drags the whole route down
            score -= min(15, isolated / 50)
            score -= min(10, max(0, haven_gap - 300) / 50) if places else 0
            score -= max(0, length / fastest_len - 1.3) * 20
            score *= rated_len / length  # streets too thinly mapped to rate earn nothing, so a mostly unknown way can't top the list
        wanted, bonus = [], 0.0
        for group, w in (prefer or {}).items():
            n = sum(1 for i in place_at if self.pois[i]["group"] == group and (place_open[i] or group == "transit"))
            if w and n and group in PRIORITIES:
                bonus += w * min(n, 3) * 1.5
                wanted.append(f"{n} {PRIORITIES[group]} on the way (your priority)")
        if weak_i is not None:
            score += min(PRIORITY_CAP, bonus)

        steps = self._steps(path, coords)
        turns = sum(st["turn"] not in ("straight", "arrive", "depart") for st in steps)

        unrated_pct = round(100 * (1 - rated_len / length)) if length else 0
        no_light = length - lit_len - dark_len
        reasons = [f"{len(mapped)} shops and places along the way", f"{len(places)} confirmed open as you pass"]
        if unknown_hours:
            reasons.append(f"{len(unknown_hours)} with no published hours")
        if main_len >= 0.1 * length:
            reasons.append(f"{round(100 * main_len / length)}% on main roads")
        if light < 1:
            if lit_len + dark_len:
                reasons.append(f"{round(100 * lit_len / length)}% lit, {round(100 * dark_len / length)}% unlit")
            if no_light >= 1:
                reasons.append(f"No lighting data for {round(100 * no_light / length)}%")
        if live_buses:
            reasons.append(f"{len(bus_ids)} {'bus' if len(bus_ids) == 1 else 'buses'} passed in the last 30 min")
        if jammed:
            reasons.append(f"Busy traffic on {round(100 * jammed / length)}% (TomTom, live)")
        if cctv:
            reasons.append(f"{len(cctv)} CCTV cameras")
        if police_m < 800:
            reasons.append(f"Police within {round(police_m)} m")
        reasons += wanted
        warnings = []
        for (field, text), m in signal_m.items():
            (reasons if field == "reason" else warnings).append(f"{text} ({fmt_m(m)})")
        taps = defaultdict(int)
        for kind in said.values():
            taps[kind] += 1
        for kind, text in FOOT_TAPS.items():
            if taps[kind]:
                (warnings if kind in ("deserted", "shops_closed") else reasons).append(f"{taps[kind]} {'person' if taps[kind] == 1 else 'people'} {text} at this time")
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
            t = at[weak_i]
            w = rating.weights(self.light(t), mode)
            wf = rating.factors(self, weak, t, self.light(t), now)
            why = max((k for k, f in wf.items() if f and w[k] > 0), key=lambda k: w[k] * (1 - wf[k]["value"]))
            weakest = {"coords": [self.nodes[weak["a"]], self.nodes[weak["b"]]], "score": round(rated[path[weak_i][1]][0]),
                       "name": weak["name"] or weak["highway"].replace("_", " "), "why": LABELS[why]}
        groups = defaultdict(lambda: {"found": 0, "open": 0, "no_hours": 0})
        for i in place_at:
            g = groups[self.pois[i]["group"]]
            g["found"] += 1
            g["open"] += place_open[i] is True
            g["no_hours"] += place_open[i] is None and self.pois[i]["group"] not in ("transit", "cctv")  # stops and cameras have no hours
        at_place = lambda i: {"name": self.pois[i]["name"], "pt": self.pois[i]["pt"], "group": self.pois[i]["group"],
                              "open": place_open[i], "frac": round(place_at[i] / length, 3),
                              "minutes": round(place_at[i] / speed / 60), "source": self.pois[i]["source"]}
        return {
            **base,
            "score": round(max(0, min(100, score))),
            "confidence": round(conf, 2),
            "confidence_level": rating.level(conf),
            "unrated_pct": unrated_pct,
            # plain measures for comparing routes side by side
            "stats": {"lit_pct": round(100 * lit_len / length), "dark_pct": round(100 * dark_len / length), "open_now": len(places),
                      "isolated_m": round(isolated), "nothing_open_m": round(haven_gap), "help_gap_m": round(help_gap), "cctv": len(cctv),
                      "main_pct": round(100 * main_len / length), "police_m": None if police_m == math.inf else round(police_m)},
            # what is along the route, by kind, and whether it is open when the trip passes it
            "look": {"groups": dict(groups), "nothing_open_m": round(haven_gap), "help_gap_m": round(help_gap), "isolated_m": round(isolated),
                     "arrive": (when + timedelta(seconds=length / speed)).isoformat(timespec="minutes")},
            # positions along the route (0 = start, 1 = end) for the route strip
            "strip": {"gaps": gaps, "dark": darks},
            "reasons": reasons,
            "warnings": warnings,
            # per factor: average value and how well it was measured along the route, None when nothing measured it
            "factors": {LABELS[k]: {"score": round(100 * factor_sum[k] / factor_len[k]),
                                    "measured": round(100 * factor_q[k] / length)} if factor_len[k] else None
                        for k in rating.WEIGHTS if rating.weights(light, mode)[k] > 0},
            "sources": sorted(sources),
            "turns": turns,
            "steps": steps,
            "segments": [[*self.nodes[self.edges[ei]["a"]], *self.nodes[self.edges[ei]["b"]], round(rated[ei][0]), round(rated[ei][1], 2)]
                         for _, ei in path if rated[ei][1] < rating.MIN_CONFIDENCE],
            "weakest": weakest,
            "havens": [{"name": self.pois[i]["name"], "pt": self.pois[i]["pt"]} for i in list(havens)[:30]],
            # every place along the way with its kind; open is None when it publishes no hours
            "places": [at_place(i) for i in sorted(place_at, key=place_at.get)[:400]],
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
