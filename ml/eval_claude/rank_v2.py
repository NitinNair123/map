"""Route ranker v2: same labels, but weights are sign-constrained by the safety criteria (criteria.md), features are capped and
scaled to 0-1 so no single measure can swing the score, unknown lighting is neutral, and day/night each have their own weights.
Usage: py ml/eval_claude/rank_v2.py   -> writes ml/out/route_ranker_v2.json and prints held-out numbers."""
import csv, json, os, sys
import numpy as np
from scipy.optimize import minimize
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import *
import pairs as pr

CAP = {"lit_pct": 100, "dark_pct": 100, "main_pct": 100, "open_now": 10, "isolated_m": 1500, "nothing_open_m": 1500, "help_gap_m": 1500,
       "cctv": 5, "police_m": 3000, "unrated_pct": 100, "food_found": 10, "shop_found": 50, "pharmacy_found": 5, "hospital_found": 5,
       "fuel_found": 3, "other_found": 15, "transit_found": 5}
SIGN = {"lit_pct": 1, "dark_pct": -1, "main_pct": 1, "open_now": 1, "isolated_m": -1, "nothing_open_m": -1, "help_gap_m": -1, "cctv": 1,
        "police_m": -1, "unrated_pct": -1, "food_found": 1, "shop_found": 1, "pharmacy_found": 1, "hospital_found": 1, "fuel_found": 1,
        "other_found": 1, "transit_found": 1, "extra_length": -1}
KEYS = list(SIGN)
L2 = 1.0


def vec(p):
    a, b = p["A"], p["B"]
    short = max(1.0, min(a["length_m"], b["length_m"]))
    x = [min(a[k], c) / c - min(b[k], c) / c for k, c in CAP.items()]
    x.append(min(1.0, (a["length_m"] - b["length_m"]) / short))  # relative extra length of A over B, signed, capped
    return np.array(x)


def design(rows):
    X = np.array([vec(p) for p in rows]); dark = np.array([p["daylight"] < 0.5 for p in rows])
    return X, dark


def fit(X, dark, y):
    n = X.shape[1]
    def unpack(w): return w[:n], w[n:]
    def f(w):
        wd, wn = unpack(w)
        z = np.where(dark, X @ wn, X @ wd)
        z = np.concatenate([z, -z]); t = np.concatenate([y, 1 - y])  # antisymmetric: no A/B bias
        loss = np.mean(np.logaddexp(0, -np.where(t == 1, z, -z))) + L2 * 0.01 * np.sum(w ** 2) / len(w)
        return loss
    bounds = [((0, 8) if SIGN[k] > 0 else (-8, 0)) for k in KEYS] * 2
    # extra_length is the A-minus-B length difference: it must hurt A, so its sign is negative in both regimes (kept).
    r = minimize(f, np.zeros(2 * n), method="L-BFGS-B", bounds=bounds)
    return r.x


def predict(w, X, dark):
    n = X.shape[1]
    return np.where(dark, X @ w[n:], X @ w[:n])


def save(w, path):
    n = len(KEYS)
    json.dump({"features": KEYS, "cap": CAP, "day": dict(zip(KEYS, w[:n].round(4).tolist())), "night": dict(zip(KEYS, w[n:].round(4).tolist())),
               "note": "score>0 means route A safer; features are capped/scaled to 0-1; sign-constrained; labels are Qwen's (not people's)"},
              open(path, "w"), indent=1)


if __name__ == "__main__":
    from sklearn.model_selection import GroupKFold
    P = load_pairs(); Q = qwen(); byid = {p["id"]: p for p in P}
    lab = {i: r["safer"] for i, r in Q.items() if r["safer"] in ("A", "B")}
    rows = [byid[i] for i in lab]; y = np.array([lab[p["id"]] == "A" for p in rows], float)
    X, dark = design(rows); g = np.array([p["block"] for p in rows])
    oof = np.zeros(len(y))
    for tr, te in GroupKFold(5).split(X, y, g):
        oof[te] = predict(fit(X[tr], dark[tr], y[tr]), X[te], dark[te])
    print("v2 OOF agreement with Qwen (held-out areas): %.3f n=%d" % (((oof > 0) == (y == 1)).mean(), len(y)))
    hand = {int(r["id"]): r["claude_hand"] for r in csv.DictReader(open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "hand_judgments.csv")))}
    keep = np.array([p["id"] not in hand for p in rows])
    w = fit(X[keep], dark[keep], y[keep])
    hid = [i for i in hand if hand[i] in ("A", "B")]
    Xh, dh = design([byid[i] for i in hid]); ph = predict(w, Xh, dh)
    v = [("A" if s > 0 else "B") == hand[i] for s, i in zip(ph, hid)]
    print("v2 vs Claude hand calls (trained without them): %.3f n=%d" % (np.mean(v), len(v)))
    full = fit(X, dark, y)
    out = os.path.join(ML, "out", "route_ranker_v2.json"); save(full, out); print("saved", out)
    n = len(KEYS)
    print("max |logit| on real pairs: %.2f ; share |logit|>10: %.3f" % (np.abs(predict(full, X, dark)).max(), (np.abs(predict(full, X, dark)) > 10).mean()))
    # sanity scenarios
    base = {k: float(np.median([p["A"][k] for p in P])) for k in pr.FEATURES}
    def sc(a, b, night):
        A = {**base, **a}; B = {**base, **b}
        return round(float(predict(full, np.array([vec({"A": A, "B": B})]), np.array([night]))[0]), 2)
    cases = {"identical": ({}, {}), "A lit 80% vs 0": ({"lit_pct": 80}, {}), "A 5 open places": ({"open_now": 5}, {}),
             "A police 100m vs 3000m": ({"police_m": 100}, {"police_m": 3000}), "A isolated 0 vs 800": ({"isolated_m": 0}, {"isolated_m": 800}),
             "A 2x longer": ({"length_m": 2 * base["length_m"]}, {}), "A 10km vs B 2km": ({"length_m": 10000}, {"length_m": 2000}),
             "A fuel open": ({"fuel_found": 2}, {}), "A CCTV 10": ({"cctv": 10}, {})}
    for nm, (a, b) in cases.items(): print(f"  {nm:26s} day {sc(a,b,False):+6.2f} night {sc(a,b,True):+6.2f}")
    # noise robustness
    rng = np.random.default_rng(0); fl = []
    def noisy(f):
        g_ = dict(f)
        for k in ("lit_pct", "dark_pct", "main_pct", "unrated_pct"): g_[k] = float(np.clip(f[k] * rng.uniform(.8, 1.2), 0, 100))
        for k in ("open_now", "cctv", "isolated_m", "nothing_open_m", "help_gap_m", "police_m", "length_m"): g_[k] = f[k] * rng.uniform(.9, 1.1)
        return g_
    for p in rows[:300]:
        s0 = predict(full, np.array([vec(p)]), np.array([p["daylight"] < .5]))[0] > 0
        fl.append(np.mean([(predict(full, np.array([vec({**p, "A": noisy(p["A"]), "B": noisy(p["B"])})]), np.array([p["daylight"] < .5]))[0] > 0) != s0 for _ in range(20)]))
    print("v2 mean flip rate under noise: %.3f ; pairs with >10%% flip chance: %.3f" % (np.mean(fl), np.mean(np.array(fl) > .1)))
    print("night weights:", {k: round(float(v), 2) for k, v in zip(KEYS, full[n:])})
    print("day weights:", {k: round(float(v), 2) for k, v in zip(KEYS, full[:n])})
