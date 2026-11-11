"""Stage 2 virtual world: a walker learns, by reward and punishment, which streets to take on the real Delhi NCR street graph.

The map, the street features and the lighting estimates are real (the same Area the dataset build and the app use, plus
the base stage's lit estimates). The EVENTS are not: nobody publishes street-level incident rates for Delhi, so the chance
of a bad event on each street is the ASSUMPTION written in sim.HAZARD / sim.EVENTS_PER_KM_AT_WORST, scaled by that street's
conditions at that hour. What it shows is whether a walker that is rewarded for arriving and punished for events and slow
walking learns to avoid the dangers we define; it is not evidence of real-world safety.

The walker picks a street at every corner (softmax over a linear score of that street's features). REINFORCE with a running
baseline. Training uses the expected punishment of a street (chance x penalty), which has far less noise than rare sampled
events; sampled events are still counted in the log. Reward and penalties are sim.REWARD.

Run:  py ml/world.py --episodes 3000        Resumes from ml/out/world_state.json, so Pause and Resume in the Studio lose at most
25 episodes. Writes ml/out/world_policy.json (what the walker learned) and ml/out/world_report.md.
"""
import argparse
import json
import math
import os
import random
import sys
import time

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import sim  # noqa: E402
from safety import dist  # noqa: E402

OUT = sim.OUT
STATE, POLICY, REPORT = (os.path.join(OUT, n) for n in ("world_state.json", "world_policy.json", "world_report.md"))
CENTERS = [  # real places around Delhi NCR the walker is dropped into in turn (lat, lon)
    (28.4741, 77.4998), (28.6315, 77.2167), (28.5921, 77.0460), (28.4950, 77.0890), (28.6270, 77.3640),
    (28.5245, 77.2066), (28.7041, 77.1025), (28.5672, 77.2431),
]
FEATS = ["danger", "unknown", "lighting", "activity", "frontage", "security", "walkability", "length_100m", "progress", "backtrack"]
START = {"progress": 2.0}  # prior: head toward the goal; everything else starts at 0 and is learned
BATCH, LR, EPISODES_PER_AREA = 25, 0.05, 100


class World:
    """One area with the walker's rules: what a step costs, and what can happen on it."""

    def __init__(self, area, lit=None, seed=0):
        self.area, self.lit, self.rng = area, lit or {}, random.Random(seed)
        self.cache = {}
        self.nodes = [n for n in area.routable if any(area.edges[ei]["modes"]["walk"] is not None for _, ei in area.adj[n])]

    def street(self, i, when):
        """(feature values, chance of an event on the whole street) for street i, with conditions taken at the hour of `when`."""
        hour = when.replace(minute=0, second=0, microsecond=0)
        key = (i, hour)
        if key not in self.cache:
            e = self.area.edges[i]
            known = sim.known_factors(self.area, e, hour, self.lit)
            d, u = sim.danger(self.area, e, hour, self.lit)
            vals = {"danger": d, "unknown": u, **{k: known.get(k, 0.5) for k in ("lighting", "activity", "frontage", "security", "walkability")},
                    "length_100m": e["length"] / 100}
            self.cache[key] = (vals, 1 - math.exp(-sim.EVENTS_PER_KM_AT_WORST * d * e["length"] / 1000))
        return self.cache[key]

    def moves(self, u, prev, goal, when):
        """[(next node, street, feature vector, chance of an event)] for every way to go on foot from corner u."""
        out = []
        here = dist(self.area.nodes[u], self.area.nodes[goal])
        for v, i in self.area.adj[u]:
            e = self.area.edges[i]
            if e["modes"]["walk"] is None:
                continue
            vals, p = self.street(i, when)
            gain = (here - dist(self.area.nodes[v], self.area.nodes[goal])) / max(e["length"], 1)
            vals = {**vals, "progress": max(-1, min(1, gain)), "backtrack": float(v == prev)}
            out.append((v, i, np.array([vals[k] for k in FEATS]), p))
        return out

    def trip(self, rng):
        """A random start, goal and time: 600-2000 m apart, evenings and nights as well as days (sim.random_trips)."""
        while True:
            a = rng.choice(self.nodes)
            b = min(rng.sample(self.nodes, min(40, len(self.nodes))), key=lambda n: abs(dist(self.area.nodes[a], self.area.nodes[n]) - rng.uniform(600, 2000)))
            if a != b:
                when = sim.SLOT_DAYS[int(rng.random() < 2 / 7)].replace(hour=rng.choice([7, 13, 19, 22, 0, 2]))
                return a, b, when


def softmax(z):
    z = z - z.max()
    p = np.exp(z)
    return p / p.sum()


def episode(world, w, trip, rng, greedy=False):
    """Walk one trip. Returns the steps [(features of each option, chosen option)], the reward, whether it arrived, the expected
    and sampled events, and the minutes walked."""
    u, goal, when = trip
    prev, steps, reward, arrived, expected, hits, seconds = None, [], 0.0, False, 0.0, 0, 0.0
    cap = max(40, int(4 * dist(world.area.nodes[u], world.area.nodes[goal]) / 50))
    for _ in range(cap):
        options = world.moves(u, prev, goal, when)
        if not options:
            break
        probs = softmax(np.array([f @ w for _, _, f, _ in options]))
        k = int(probs.argmax()) if greedy else rng.choices(range(len(options)), probs)[0]
        v, i, _, p = options[k]
        length = world.area.edges[i]["length"]
        steps.append((np.array([o[2] for o in options]), probs, k))
        reward += sim.REWARD["event"] * p + sim.REWARD["per_minute"] * length / 1.3 / 60  # expected punishment, not the rare sampled one
        expected += p
        hits += rng.random() < p
        seconds += length / 1.3
        prev, u = u, v
        if u == goal:
            reward += sim.REWARD["arrive"]
            arrived = True
            break
    else:
        reward -= 1.0  # never got there
    return steps, reward, arrived, expected, hits, seconds / 60


def learn(batch, w, base):
    """One REINFORCE step. Each step is credited with the reward that came after it, compared with the running average."""
    grad = np.zeros_like(w)
    returns = np.array([ep["reward"] for ep in batch])  # one return per episode (no discounting)
    adv = returns - base
    scale = adv.std() or 1.0
    for ep, a in zip(batch, adv / scale):
        for feats, probs, k in ep["steps"]:
            grad += a * (feats[k] - probs @ feats) / max(len(ep["steps"]), 1)
    return w + LR * grad / len(batch), 0.9 * base + 0.1 * returns.mean()


def baselines(world, trips, when_rng):
    """What the app's own route choices cost on the same trips: (fastest, safest) per 1000 trips of expected events and extra minutes."""
    out = {s: {"events": 0.0, "minutes": 0.0, "n": 0} for s in ("fastest", "safest")}
    for a, b, when in trips:
        try:
            routes = {r["kind"]: r for r in world.area.routes(world.area.nodes[a], world.area.nodes[b], when)}
        except Exception:
            continue
        for s in out:
            if s in routes:
                _, chance, _ = sim.walk(world.area, routes[s], when, when_rng, 1.3, world.lit)
                out[s]["events"] += chance
                out[s]["minutes"] += routes[s]["length_m"] / 1.3 / 60
                out[s]["n"] += 1
    return out


def evaluate(world, w, trips, rng):
    res = {"n": 0, "arrived": 0, "events": 0.0, "minutes": 0.0}
    for t in trips:
        _, _, ok, expected, _, minutes = episode(world, w, t, rng)
        res["n"] += 1
        res["arrived"] += ok
        res["events"] += expected
        res["minutes"] += minutes
    return res


def load_state():
    try:
        with open(STATE) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {"w": [START.get(k, 0.0) for k in FEATS], "base": 0.0, "episodes": 0, "curve": [], "evals": []}


def save(state):
    os.makedirs(OUT, exist_ok=True)
    with open(STATE + ".tmp", "w") as f:
        json.dump(state, f)
    os.replace(STATE + ".tmp", STATE)  # a pause in the middle of writing never leaves a broken file


def write_report(state):
    w = dict(zip(FEATS, state["w"]))
    lines = ["# Stage 2: virtual world", "",
             "The streets, features and lighting estimates are real. The event chances are ASSUMPTIONS (sim.HAZARD, sim.EVENTS_PER_KM_AT_WORST), "
             "so this is a test of what a rewarded and punished walker learns, not evidence of real-world safety.", "",
             f"Episodes: {state['episodes']}. Reward: arrive {sim.REWARD['arrive']}, event {sim.REWARD['event']}, each minute {sim.REWARD['per_minute']}.", "",
             "## What the walker learned (weight per street feature; positive = it prefers more of it)", ""]
    lines += [f"- {k}: {v:+.2f}" for k, v in w.items()]
    lines += ["", "## Held-out checks (same trips, per area)", "",
              "| Area | Walker arrived | Walker events/1000 | Fastest events/1000 | Safest events/1000 | Walker minutes | Fastest minutes |", "| --- | --- | --- | --- | --- | --- | --- |"]
    for e in state["evals"][-12:]:
        lines.append(f"| {e['area']} | {e['arrived']:.0%} | {e['walker']:.1f} | {e['fastest']:.1f} | {e['safest']:.1f} | {e['walker_min']:.1f} | {e['fastest_min']:.1f} |")
    open(REPORT, "w", encoding="utf-8").write("\n".join(lines) + "\n")
    with open(POLICY, "w") as f:
        json.dump({"features": FEATS, "weights": w, "episodes": state["episodes"], "assumption": "event chances are assumptions, see ml/sim.py"}, f, indent=1)


def main():
    import dataset
    ap = argparse.ArgumentParser()
    ap.add_argument("--episodes", type=int, default=3000)
    ap.add_argument("--radius", type=int, default=3500)
    args = ap.parse_args()
    lit, _ = sim.load_base()
    state = load_state()
    store = dataset.store()
    w, base = np.array(state["w"]), state["base"]
    print(f"Virtual world: {state['episodes']} of {args.episodes} episodes done; event chances are assumptions", flush=True)
    while state["episodes"] < args.episodes:
        n = state["episodes"] // EPISODES_PER_AREA
        center = CENTERS[n % len(CENTERS)]
        try:
            world = World(store.around(center, args.radius), lit, seed=n)
        except Exception as err:  # one place can't load (network, no streets mapped): the next one
            print(f"area {center} skipped: {err}", flush=True)
            state["episodes"] = (n + 1) * EPISODES_PER_AREA
            continue
        rng = random.Random(n)
        held = [world.trip(random.Random(1000 + n * 31 + i)) for i in range(20)]
        while state["episodes"] < min(args.episodes, (n + 1) * EPISODES_PER_AREA):
            eps = []
            for _ in range(BATCH):
                steps, reward, ok, expected, hits, minutes = episode(world, w, world.trip(rng), rng)
                eps.append({"steps": steps, "reward": reward, "arrived": ok, "events": hits})
            w, base = learn(eps, w, base)
            state.update(w=w.tolist(), base=base, episodes=state["episodes"] + BATCH)
            mean = float(np.mean([e["reward"] for e in eps]))
            state["curve"].append(round(mean, 3))
            save(state)
            print(f"[{min(state['episodes'], args.episodes)}/{args.episodes}] episodes: mean reward {mean:.3f}, arrived "
                  f"{np.mean([e['arrived'] for e in eps]):.0%}, sampled events {sum(e['events'] for e in eps)}", flush=True)
        r, b = evaluate(world, w, held, rng), baselines(world, held, rng)
        per = lambda d, key, nn: 1000 * d[key] / nn if nn else float("nan")
        row = {"area": f"{center[0]:.3f},{center[1]:.3f}", "arrived": r["arrived"] / max(r["n"], 1), "walker": per(r, "events", r["n"]),
               "fastest": per(b["fastest"], "events", b["fastest"]["n"]), "safest": per(b["safest"], "events", b["safest"]["n"]),
               "walker_min": r["minutes"] / max(r["n"], 1), "fastest_min": b["fastest"]["minutes"] / max(b["fastest"]["n"], 1)}
        state["evals"].append(row)
        save(state)
        write_report(state)
        print(f"held-out {row['area']}: walker arrived {row['arrived']:.0%}, events/1000 walker {row['walker']:.1f} fastest {row['fastest']:.1f} "
              f"safest {row['safest']:.1f}", flush=True)
    write_report(state)
    print("Virtual world done: ml/out/world_report.md", flush=True)


if __name__ == "__main__":
    main()
