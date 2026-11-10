"""Stage 2 of training, run after the base stage (ml/train.py and ml/rank.py). A simulated city for testing route choices: walkers take each candidate route on the real map, and on every street a
bad event can happen with a chance set by that street's conditions at that hour. Arriving earns a reward, an event a
punishment, and each extra minute a small cost.

The event chances are ASSUMPTIONS written in HAZARD below, not measurements: nobody publishes street-level incident
rates for Delhi. So this tests whether a route chooser avoids the dangers we define, and whether the detour is worth it;
it is not evidence of real-world safety. Replace HAZARD from real data (felt-unsafe reports, incident files, people's
A/B answers through ml/rank.py) when there is enough of it.

What it takes from the base stage:
  - the lit model's estimate for streets with no lighting data (ml/out/lit_predictions.csv.gz), so fewer streets are unknown;
  - the route weights learned from people's answers (ml/out/route_ranker.json), raced as a fourth way of choosing a route.

Usage: py ml/sim.py --trips 500 [--around 28.4741,77.4998]     writes ml/out/sim_report.md
"""
import argparse
import csv
import gzip
import json
import math
import os
import random
import sys
from datetime import timedelta

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import rating  # noqa: E402
from features import SLOT_DAYS  # noqa: E402

# Share of the danger each poor condition brings (assumption). A factor nothing measures counts as halfway.
HAZARD = {"lighting": 0.35, "activity": 0.3, "frontage": 0.15, "security": 0.1, "walkability": 0.1}
EVENTS_PER_KM_AT_WORST = 0.05  # assumption: a fully dark, empty, unwatched street; scales every chance
REWARD = {"arrive": 1.0, "event": -10.0, "per_minute": -0.01}
STRATEGIES = ("fastest", "balanced", "safest")
OUT = os.path.join(ROOT, "ml", "out")


def load_base(folder=OUT):
    """What the base stage produced: ({street id: lit probability}, learned route weights or None)."""
    lit, ranker = {}, None
    path = os.path.join(folder, "lit_predictions.csv.gz")
    if os.path.exists(path):
        with gzip.open(path, "rt") as f:
            lit = {r["edge_id"]: float(r["p_lit"]) for r in csv.DictReader(f)}
    if os.path.exists(os.path.join(folder, "route_ranker.json")):
        ranker = json.load(open(os.path.join(folder, "route_ranker.json")))
    return lit, ranker


def known_factors(area, e, when, lit=None):
    """{factor: 0-1 value} for the factors something measured on one street at one moment.
    lit: the base stage's lit estimates, used only where nothing measured the lighting (scaled by how dark it is)."""
    f = rating.factors(area, e, when, area.light(when))
    known = {k: v["value"] for k, v in f.items() if v and k in HAZARD}
    key = "-".join(sorted((str(e["a"]), str(e["b"]))))  # the street id of ml/features.py
    if "lighting" not in known and lit and key in lit:
        known["lighting"] = 1 - (1 - lit[key]) * (1 - area.light(when))
    return known


def danger(area, e, when, lit=None):
    """0 (good on every factor) to 1 (poor on every factor) for one street at one moment, and how much of it was unknown."""
    known = known_factors(area, e, when, lit)
    return sum(w * (1 - known.get(k, 0.5)) for k, w in HAZARD.items()), sum(w for k, w in HAZARD.items() if k not in known)


def route_edges(area, route):
    """The street indexes of a route, from the points it passes."""
    at = {}
    for i, e in enumerate(area.edges):
        a, b = tuple(area.nodes[e["a"]]), tuple(area.nodes[e["b"]])
        if at.get((a, b)) is None or e["length"] < area.edges[at[(a, b)]]["length"]:  # two streets between the same corners: the shorter
            at[(a, b)] = at[(b, a)] = i
    pts = [tuple(c) for c in route["coords"]]
    return [at[(p, q)] for p, q in zip(pts, pts[1:]) if (p, q) in at]


def learned_choice(routes, ranker):
    """The candidate the learned weights rate best (ml/rank.py: a route's total of measure x weight)."""
    import pairs
    w = ranker["weights"]
    value = lambda r: sum(pairs.route_features(r)[k] * (w[k] + w[f"{k}_dark"] * (r["daylight"] < 0.5)) for k in ranker["features"])
    return max(routes, key=value)


def walk(area, route, when, rng, speed, lit=None):
    """One simulated walk: (event happened, chance of an event over the whole route, share of danger left unknown)."""
    t, ok, unknown, length, p_safe = when, True, 0.0, 0.0, 1.0
    for i in route_edges(area, route):
        e = area.edges[i]
        d, u = danger(area, e, t, lit)
        p = 1 - math.exp(-EVENTS_PER_KM_AT_WORST * d * e["length"] / 1000)
        p_safe *= 1 - p
        ok = ok and rng.random() >= p
        unknown, length = unknown + u * e["length"], length + e["length"]
        t += timedelta(seconds=e["length"] / speed)
    return not ok, 1 - p_safe, unknown / length if length else 0.0


def simulate(area, trips, rng, speed=1.3, lit=None, ranker=None):
    """[(start, end, when)] -> per strategy: trips, events, expected events, extra minutes over the fastest, reward."""
    names = STRATEGIES + (("learned",) if ranker else ())
    out = {s: {"trips": 0, "events": 0, "expected_events": 0.0, "extra_minutes": 0.0, "reward": 0.0, "unknown": 0.0} for s in names}
    for a, b, when in trips:
        try:
            found = area.routes(a, b, when)
        except Exception:  # no street near one end: not a trip
            continue
        routes = {r["kind"]: r for r in found}
        if not all(s in routes for s in STRATEGIES):
            continue
        if ranker:
            routes["learned"] = learned_choice(found, ranker)
        for s in names:
            r, o = routes[s], out[s]
            hit, chance, unknown = walk(area, r, when, rng, speed, lit)
            extra = (r["length_m"] - routes["fastest"]["length_m"]) / speed / 60
            o["trips"] += 1
            o["events"] += hit
            o["expected_events"] += chance
            o["extra_minutes"] += extra
            o["unknown"] += unknown
            o["reward"] += REWARD["event"] if hit else REWARD["arrive"]
            o["reward"] += REWARD["per_minute"] * r["length_m"] / speed / 60
    for o in out.values():
        n = o["trips"] or 1
        o.update(events_per_1000=round(1000 * o["events"] / n, 2), expected_per_1000=round(1000 * o["expected_events"] / n, 2),
                 extra_minutes=round(o["extra_minutes"] / n, 1), reward=round(o["reward"] / n, 3), unknown=round(o["unknown"] / n, 3))
    return out


def random_trips(center, n, rng):
    trips = []
    for _ in range(n):
        a = (center[0] + rng.uniform(-0.008, 0.008), center[1] + rng.uniform(-0.008, 0.008))
        d, ang = rng.uniform(600, 2000), rng.uniform(0, 2 * math.pi)
        b = (a[0] + d * math.cos(ang) / 111000, a[1] + d * math.sin(ang) / 111000 / math.cos(math.radians(a[0])))
        trips.append((a, b, SLOT_DAYS[int(rng.random() < 2 / 7)].replace(hour=rng.choice([7, 13, 19, 22, 0, 2]))))
    return trips


def main():
    import dataset
    ap = argparse.ArgumentParser()
    ap.add_argument("--trips", type=int, default=500)
    ap.add_argument("--around", default="28.4741,77.4998")
    args = ap.parse_args()
    center = tuple(map(float, args.around.split(",")))
    lit, ranker = load_base()
    if not lit and not ranker:
        sys.exit("Stage 2 runs after base training: run py ml/train.py (and py ml/rank.py once pairs are answered) first")
    rng = random.Random(0)
    area = dataset.store().around(center, 3500)
    res = simulate(area, random_trips(center, args.trips, rng), rng, lit=lit, ranker=ranker)
    lines = ["# Stage 2: simulated walks", "", "Event chances are assumptions (see HAZARD in ml/sim.py), not measured safety.",
             f"From base training: lit estimates for {len(lit)} streets; learned route weights: {'yes' if ranker else 'not yet'}.", "",
             "| Route | Trips | Bad events per 1000 trips | Expected per 1000 | Extra minutes vs fastest | Reward per trip | Danger left unknown |",
             "| --- | --- | --- | --- | --- | --- | --- |"]
    lines += [f"| {s} | {o['trips']} | {o['events_per_1000']} | {o['expected_per_1000']} | {o['extra_minutes']} | {o['reward']} | {o['unknown']:.0%} |"
              for s, o in res.items()]
    os.makedirs(OUT, exist_ok=True)
    text = "\n".join(lines) + "\n"
    open(os.path.join(OUT, "sim_report.md"), "w").write(text)
    print(text)


if __name__ == "__main__":
    main()
