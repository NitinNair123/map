"""The safety rating: every factor comes from measured data with a named source, a quality and a confidence.

A factor with no measurement is left out, never filled in. The weight of factors that are missing or weakly
measured becomes lost confidence, and a street below MIN_CONFIDENCE is shown as "Not rated".
"""
import time
from collections import defaultdict

import buses
import traffic

# (full daylight, full dark) weight per factor; each column sums to 100. Twilight blends the two.
# A stated design choice based on Safetipin's audit parameters, to be refitted once audited streets exist.
WEIGHTS = {
    "lighting": (0, 30),
    "activity": (30, 30),
    "frontage": (20, 15),
    "security": (10, 10),
    "walkability": (40, 15),
}
# Other ways to travel weigh the same evidence differently: in a car the footpath matters little and help points
# (open places, police) matter most; on a bike the road surface and lighting matter about as much as for walking.
MODE_WEIGHTS = {
    "walk": WEIGHTS,
    "bike": {"lighting": (0, 30), "activity": (30, 25), "frontage": (20, 15), "security": (10, 10), "walkability": (40, 20)},
    "car": {"lighting": (0, 15), "activity": (45, 40), "frontage": (20, 15), "security": (25, 20), "walkability": (10, 10)},
}
LABELS = {"lighting": "Lighting", "activity": "People and open places", "frontage": "Buildings and surroundings",
          "security": "Police and CCTV", "walkability": "Footpath and ways out"}

# How directly each kind of evidence measures its factor (1 = someone checked this street)
QUALITY = {
    "observed": 1.0,      # survey or app report on this street
    "gtfs": 0.9,          # timetable: buses or trains at this stop this hour
    "live_bus": 0.9,      # buses that actually drove past in the last half hour
    "osm_tag": 0.8,       # lit=yes/no mapped on the street
    "hours": 0.8,         # published opening hours of places on the street
    "buildings": 0.8,     # building footprints and land use
    "police": 0.8,        # police station locations
    "road": 0.8,          # road type, footpath, dead ends from the map
    "lamps": 0.7,         # street lamps mapped or seen in Mapillary photos
    "nothing_mapped": 0.5,  # no shop or service of any kind mapped within 60 m
    "traffic": 0.35,      # live slow traffic: many vehicles, which says little about people on foot
    "main_road": 0.35,    # a main road usually has traffic passing, but that alone doesn't make it safe
    "nightlight": 0.3,    # NASA satellite brightness, 2016, about 500 m per pixel
}
HIGH, MEDIUM, MIN_CONFIDENCE = 0.7, 0.5, 0.35
OBSERVATION_RADIUS_M = 50

LIT_TAGS = {"yes": 1.0, "24/7": 1.0, "automatic": 1.0, "sunset-sunrise": 1.0, "interval": 0.7, "limited": 0.6,
            "no": 0.0, "disused": 0.0}
# Main roads have people and traffic; alleys, tracks and steps are isolated
ROAD_SCORE = {
    "trunk": 0.8, "primary": 0.95, "secondary": 1.0, "tertiary": 0.9,
    "primary_link": 0.85, "secondary_link": 0.9, "tertiary_link": 0.8,
    "residential": 0.7, "living_street": 0.75, "pedestrian": 0.85, "unclassified": 0.55,
    "service": 0.4, "footway": 0.45, "cycleway": 0.4, "path": 0.3, "track": 0.1, "steps": 0.25,
}
MAIN_ROADS = {"trunk", "primary", "secondary", "tertiary", "trunk_link", "primary_link", "secondary_link", "tertiary_link"}
VEHICLE_ROADS = {"trunk", "primary", "secondary", "tertiary", "primary_link", "secondary_link", "tertiary_link", "unclassified", "residential"}
# Land next to the road: (score in daylight, score in the dark)
SURROUNDINGS = {
    "construction": (0.4, 0.1), "brownfield": (0.4, 0.1), "greenfield": (0.5, 0.2), "railway": (0.5, 0.2),
    "wood": (0.4, 0.1), "scrub": (0.4, 0.1), "park": (0.8, 0.25), "cemetery": (0.5, 0.15),
    "industrial": (0.6, 0.3), "farmland": (0.6, 0.25), "quarry": (0.3, 0.1),
}

# Observation types: what they measure and the value they record
OBSERVATIONS = {
    "lights_on": ("lighting", 1.0), "lights_off": ("lighting", 0.0), "broken_light": ("lighting", 0.0),
    "people_around": ("activity", 1.0), "shops_open": ("activity", 1.0),
    "deserted": ("activity", 0.0), "shops_closed": ("activity", 0.0),
    "no_footpath": ("walkability", 0.0),
}
# Score points a recent report adds near it; halves every 30 days
REPORT_POINTS = {"felt_unsafe": -25, "harassment": -40, "felt_safe": 15, "dogs": -15, "flooded": -15}
UNPAVED = {"unpaved", "dirt", "earth", "ground", "gravel", "mud", "sand", "grass", "compacted", "fine_gravel"}
ROUGH = {"bad", "very_bad", "horrible", "very_horrible", "impassable"}
REPORT_TYPES = set(OBSERVATIONS) | set(REPORT_POINTS)
INCIDENT_SEVERITY = {"assault": 1.0, "robbery": 0.9, "harassment": 0.8, "stalking": 0.7, "snatching": 0.7,
                     "theft": 0.4, "other": 0.5}


def freshness(ts, now):
    """1 for evidence under 180 days old, falling to 0.3 at five years."""
    days = (now - ts) / 86400
    return 1.0 if days < 180 else max(0.3, 1 - 0.7 * (days - 180) / (5 * 365 - 180))


def same_time(obs, when):
    """An observation counts for activity only at a similar hour on the same kind of day (weekday or weekend)."""
    gap = abs(obs["hour"] - when.hour)
    return min(gap, 24 - gap) <= 1 and (obs["weekday"] >= 5) == (when.weekday() >= 5)


def combine(evidence):
    """[(value, quality, source)] -> {"value", "quality", "sources"}; better evidence counts for more."""
    if not evidence:
        return None
    q = sum(e[1] for e in evidence)
    return {"value": sum(v * w for v, w, _ in evidence) / q, "quality": max(e[1] for e in evidence),
            "sources": sorted({s for _, _, s in evidence})}


def votes(reports):
    """One vote per person per report type on a street: their latest. Survey rows have no voter and all count."""
    latest = {}
    for r in reports:
        k = (r.get("by") or id(r), r["type"])
        if k not in latest or r["ts"] > latest[k]["ts"]:
            latest[k] = r
    return list(latest.values())


def street_lighting(area, e, reports):
    """Lighting evidence about this street itself: reports, the OSM lit tag and mapped lamps, without satellite brightness."""
    lighting = list(reports)
    if e["lit"] in LIT_TAGS:
        lighting.append((LIT_TAGS[e["lit"]], QUALITY["osm_tag"], "OpenStreetMap lit tag"))
    if e["near"]["lamp"]:
        src = {area.pois[i].get("source", "OpenStreetMap") for i in e["near"]["lamp"]}
        lighting.append((e["lamp_level"], QUALITY["lamps"], f"Street lamps ({', '.join(sorted(src))})"))
    return lighting


def factors(area, e, when, light, now=None):
    """Every factor for one street segment at one time, each None when nothing measures it."""
    now = now or time.time()
    dark = 1 - light
    near = e["near"]
    out = {}

    obs = defaultdict(list)
    for r in votes(e["reports"]):
        if r["type"] in OBSERVATIONS:
            factor, value = OBSERVATIONS[r["type"]]
            if factor == "activity" and not same_time(r, when):
                continue
            if factor == "lighting" and r.get("dark") is False:
                continue  # nobody can tell whether a street light works in daylight
            obs[factor].append((value, QUALITY["observed"] * freshness(r["ts"], now), r.get("source", "Reports from people walking here")))

    street = street_lighting(area, e, obs["lighting"])
    lighting = street + ([(e["nightlight"], QUALITY["nightlight"], "NASA night lights 2016 (area)")] if e.get("nightlight") is not None else [])
    out["lighting"] = combine(lighting)
    if out["lighting"]:
        out["lighting"]["street"] = combine(street)  # what "% lit" counts: area brightness alone can't say a street is lit

    activity = list(obs["activity"])
    places = near["place"] + near["haven"]
    known = [o for o in (area.is_place_open(area.pois[i], when) for i in places) if o is not None]
    if known:
        activity.append((min(1, sum(known) / 3), QUALITY["hours"], "Opening hours (OpenStreetMap)"))
    served = [area.stops[i]["deps"][when.weekday()][when.hour] for i in e.get("stops", ())]
    if served:
        activity.append((min(1, sum(served) / 6), QUALITY["gtfs"], "Bus and metro timetables"))
    passed = buses.LIVE.near(e["mid"], when, now)
    if passed:  # no bus is no evidence: most streets aren't on a bus route
        activity.append((min(1, len(passed) / 3), QUALITY["live_bus"], "Live buses (Delhi Open Transit Data)"))
    if e["highway"] in MAIN_ROADS and traffic.LIVE.jammed(e["mid"], when, now):
        activity.append((1.0, QUALITY["traffic"], "Live traffic (TomTom)"))
    if not places and not activity and e.get("places_mapped", True):  # with nothing mapped within 500 m the block is just unmapped
        activity.append((0.0, QUALITY["nothing_mapped"], "No shops or services mapped nearby"))
    if e["highway"] in MAIN_ROADS:
        activity.append((0.6, QUALITY["main_road"], "Main road (OpenStreetMap)"))
    out["activity"] = combine(activity)

    frontage = []
    unloaded = e.get("unloaded", ())  # data still loading here is unknown, not a zero
    zone = None if "surroundings" in unloaded else min((SURROUNDINGS[k][0] * light + SURROUNDINGS[k][1] * dark for k in e["zones"]), default=None)
    if area.building_cells and "overlooked" not in unloaded:
        built = min(1, e["buildings_ha"] / 20)
        frontage.append((built if zone is None else min(built, zone), QUALITY["buildings"], "Building footprints and land use"))
    elif zone is not None:
        frontage.append((zone, QUALITY["buildings"], "Land use (OpenStreetMap)"))
    out["frontage"] = combine(frontage)

    police = 1.0 if e["police_m"] < 400 else 0.5 if e["police_m"] < 800 else 0.0
    guarded = 0.3 * len(near["cctv"]) + 0.3 * min(1, len(near["guard"])) + 0.2 * min(1, len(near["sos_phone"]))
    security = [(min(1, police + guarded), QUALITY["police"], "Police, guard posts, emergency phones and CCTV")]
    out["security"] = combine(security) if area.police or guarded else None  # no police mapped at all is a data gap

    path = ROAD_SCORE.get(e["highway"], 0.5)
    if e["highway"] in VEHICLE_ROADS and e["sidewalk"]:
        path += -0.3 if e["sidewalk"] == "no" and e["highway"] not in ("residential", "unclassified") else 0.15
    if e.get("surface") in UNPAVED or e.get("smoothness") in ROUGH:
        path -= 0.2
    fast = (e.get("lanes") or 0) >= 4 or (e.get("maxspeed") or 0) >= 50 or e["highway"] == "trunk"
    if fast and e["highway"] in VEHICLE_ROADS and e["sidewalk"] not in ("yes", "both", "left", "right", "separate"):
        path -= 0.2  # four lanes or 50 km/h, and no footpath mapped beside it
    if fast and near["crossing"]:
        path += 0.1
    if near["blackspot"]:
        path -= 0.3  # on the official list of accident blackspots
    if e["underpass"]:
        path = 0.1
    elif e["overbridge"]:
        path = path * light + 0.25 * dark
    escape = 0.0 if e["dead_ends"] == 2 else 0.2 if e["dead_ends"] == 1 else max(0.0, 1 - e["gap_m"] / 500)
    walk = [(max(0.0, min(1.0, 0.6 * path + 0.4 * escape)), QUALITY["road"], "Street map (OpenStreetMap)")]
    if near["blackspot"]:
        walk[0] = walk[0][:2] + ("Street map (OpenStreetMap) and accident blackspots",)
    out["walkability"] = combine(walk + obs["walkability"])
    return out


def weights(light, mode="walk"):
    return {k: d * light + n * (1 - light) for k, (d, n) in MODE_WEIGHTS[mode].items()}


def rate(area, e, when, light, rain=False, now=None, mode="walk"):
    """(score 0-100, confidence 0-1, factors) for one segment. Score uses only measured factors."""
    now = now or time.time()
    feats = factors(area, e, when, light, now)
    w = weights(light, mode)
    have = {k: f for k, f in feats.items() if f and w[k] > 0}
    confidence = sum(w[k] * f["quality"] for k, f in have.items()) / sum(w.values())
    if not have:
        return 0.0, 0.0, feats
    s = 100 * sum(w[k] * f["quality"] * f["value"] for k, f in have.items()) / sum(w[k] * f["quality"] for k, f in have.items())

    # Penalties from recorded events; they lower the score but don't count as evidence of safety
    dark = 1 - light
    s -= 10 * dark * min(2, len(e["near"]["nightlife"]))
    if rain:
        s -= 5  # rain empties streets and cuts visibility
        if e["near"]["waterlogging"]:
            s -= 15  # on the list of streets that flood
    s += sum(REPORT_POINTS[r["type"]] * 0.5 ** ((now - r["ts"]) / 86400 / 30) for r in votes(e["reports"]) if r["type"] in REPORT_POINTS)
    hits = 0.0
    for i in e["incidents"]:
        gap = abs(i["hour"] - when.hour)
        hits += 25 * INCIDENT_SEVERITY.get(i["type"], 0.5) * 0.5 ** ((now - i["ts"]) / 86400 / 180) * (1.0 if min(gap, 24 - gap) <= 2 else 0.4)
    s -= min(35, hits)
    return max(0.0, min(100.0, s)), confidence, feats


def credit(score, confidence):
    """What a segment is worth to the safest-route search: unrated streets get none, weakly known ones less."""
    return 0.0 if confidence < MIN_CONFIDENCE else score * min(1.0, confidence / HIGH)


def level(confidence):
    return "high" if confidence >= HIGH else "medium" if confidence >= MEDIUM else "low" if confidence >= MIN_CONFIDENCE else "none"
