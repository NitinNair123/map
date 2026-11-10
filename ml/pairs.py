"""Route pairs for learning what people judge safer: two real routes between the same two points at the same hour,
each described only by what was measured along it (never by SafeRoute's own score, or the model would just copy it).

Usage: py ml/pairs.py --n 5000     appends to ml/data/pairs.jsonl; run again to add more
Then:  py ml/label_llm.py          a local Qwen picks the safer route of each pair (free, overnight)
       py ml/rate_pairs.py         a page where people answer the same question
       py ml/rank.py               learns how much each measure matters from those answers
"""
import argparse
import json
import math
import os
import random
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from features import SLOT_DAYS  # noqa: E402
from tiles import TILE  # noqa: E402

OUT = os.path.join(ROOT, "ml", "data", "pairs.jsonl")
HOURS = [7, 10, 13, 16, 18, 20, 22, 0, 2]
PLACES = ["food", "shop", "pharmacy", "hospital", "fuel", "other", "transit"]
# measure -> what a person (or the labelling model) is shown
MEASURES = {"length_m": "Length (m)", "lit_pct": "Lit streets (%)", "dark_pct": "Unlit streets (%)", "no_light_data_pct": "No lighting data (%)",
            "main_pct": "On main roads (%)", "open_now": "Places open as you pass", "isolated_m": "Longest isolated stretch (m)",
            "nothing_open_m": "Longest stretch with nothing open (m)", "help_gap_m": "Longest stretch far from help (m)",
            "cctv": "CCTV cameras", "police_m": "Nearest police station (m, 5000 = none known)", "unrated_pct": "Too little data to rate (%)",
            **{f"{g}_found": f"{g.capitalize()} places along it" for g in PLACES}}
FEATURES = list(MEASURES)


def route_features(r):
    """What was measured along one route from Area.routes(); no score, rank or kind."""
    s, groups = r["stats"], r["look"]["groups"]
    f = {"length_m": r["length_m"], "unrated_pct": r["unrated_pct"], "no_light_data_pct": max(0, 100 - s["lit_pct"] - s["dark_pct"]),
         "police_m": 5000 if s["police_m"] is None else min(5000, s["police_m"])}
    f.update({k: s[k] for k in ("lit_pct", "dark_pct", "main_pct", "open_now", "isolated_m", "nothing_open_m", "help_gap_m", "cctv")})
    f.update({f"{g}_found": groups.get(g, {}).get("found", 0) for g in PLACES})
    return f


def make_pair(area, a, b, when, rng):
    """Two different candidate routes from a to b in random order, or None if the map offers only one way."""
    seen, routes = set(), []
    for r in area.routes(a, b, when):
        k = json.dumps(r["coords"])
        if r["length_m"] and k not in seen:
            seen.add(k)
            routes.append(r)
    if len(routes) < 2:
        return None
    x, y = rng.sample(routes, 2)
    mid = ((a[0] + b[0]) / 2, (a[1] + b[1]) / 2)
    return {"hour": when.hour, "weekend": int(when.weekday() >= 5), "daylight": x["daylight"],
            "block": f"{int(mid[0] // TILE) // 3}_{int(mid[1] // TILE) // 3}",  # the ~3 km area held out together, as in train.py
            "A": route_features(x), "B": route_features(y), "A_coords": x["coords"], "B_coords": y["coords"]}


def main():
    import dataset
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=2000)
    ap.add_argument("--seed", type=int, default=None)
    args = ap.parse_args()
    rng, ts = random.Random(args.seed), dataset.store()
    have = sum(1 for _ in open(OUT)) if os.path.exists(OUT) else 0
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    made = tries = 0
    with open(OUT, "a") as f:
        while made < args.n and tries < args.n * 5:
            tries += 1
            c = rng.choice(dataset.SPOTS)
            a = (c[0] + rng.uniform(-0.02, 0.02), c[1] + rng.uniform(-0.02, 0.02))
            d, ang = rng.uniform(800, 3000), rng.uniform(0, 2 * math.pi)
            b = (a[0] + d * math.cos(ang) / 111000, a[1] + d * math.sin(ang) / 111000 / math.cos(math.radians(a[0])))
            weekend = rng.random() < 2 / 7
            when = SLOT_DAYS[int(weekend)].replace(hour=rng.choice(HOURS))
            try:
                pair = make_pair(ts.for_trip(a, b), a, b, when, rng)
            except Exception as err:  # no road near a point, a tile that wouldn't load: try another trip
                print(f"trip skipped ({type(err).__name__}: {err})", flush=True)
                continue
            if pair:
                made += 1
                f.write(json.dumps({"id": have + made, **pair}) + "\n")
                f.flush()
                if made % 50 == 0:
                    print(f"{made} of {args.n} pairs", flush=True)
    print(f"{made} pairs added, {have + made} in {OUT}", flush=True)


def load(path=None):
    path = path or OUT
    return [json.loads(l) for l in open(path)] if os.path.exists(path) else []


if __name__ == "__main__":
    main()
