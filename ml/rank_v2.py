"""Route model v2: learns what makes one route safer than another from Qwen's answers (ml/label_llm.py), Claude's answers
(ml/label_claude.py) and people's (ml/rate_pairs.py, when there are any).

Fixes from the evaluation (ml/eval_claude/report.md): every weight's sign follows the safety criteria (more lighting, open places,
main roads, CCTV help; isolation, distance from police and extra length hurt), each measure is capped and scaled to 0-1 so none
can swing the score alone, unknown lighting is neutral, and day and night get their own weights.

Training labels, per pair:  people's answer if any > Claude's hand answer > Qwen and Claude's criteria when they agree
(weight 1) > only one of them answered (weight 0.5). Pairs where Qwen and Claude's criteria disagree are left out and counted.
Scores are on ~3 km areas never trained on, against each labeller separately. Writes ml/out/route_ranker_v2.json and
ml/out/route_report.md.

Usage: py ml/rank_v2.py
"""
import csv
import json
import os
import sys
from collections import Counter

import numpy as np
from scipy.optimize import minimize
from sklearn.model_selection import GroupKFold

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import label_claude  # noqa: E402
import pairs  # noqa: E402
import rank  # noqa: E402  (people's and Qwen's label readers)

DATA, OUT = os.path.dirname(pairs.OUT), os.path.join(pairs.ROOT, "ml", "out")
# measure -> (cap for 0-1 scaling, +1 if more is safer / -1 if more is less safe). Training can shrink a weight to 0, never flip it.
PARAMS = {"lit_pct": (100, 1), "dark_pct": (100, -1), "main_pct": (100, 1), "open_now": (10, 1), "isolated_m": (1500, -1),
          "nothing_open_m": (1500, -1), "help_gap_m": (1500, -1), "cctv": (5, 1), "police_m": (3000, -1), "unrated_pct": (100, -1),
          "food_found": (10, 1), "shop_found": (50, 1), "pharmacy_found": (5, 1), "hospital_found": (5, 1), "fuel_found": (3, 1),
          "other_found": (15, 1), "transit_found": (5, 1)}
KEYS = list(PARAMS) + ["extra_length"]  # extra_length: A's length over B's, relative to the shorter, capped at 1 (more is less safe)
SIGN = [PARAMS[k][1] for k in PARAMS] + [-1]
MAX_W, L2 = 8.0, 0.01
MIN_PAIRS, MIN_AREAS = 100, 5


def vec(p):
    a, b = p["A"], p["B"]
    short = max(1.0, min(a["length_m"], b["length_m"]))
    x = [min(a[k], c) / c - min(b[k], c) / c for k, (c, _) in PARAMS.items()]
    return np.array(x + [max(-1.0, min(1.0, (a["length_m"] - b["length_m"]) / short))])


def fit(X, dark, y, wt):
    """Sign-bounded logistic regression with separate day and night weights; every pair also counted swapped (no A/B bias)."""
    n = X.shape[1]
    Z, D = np.vstack([X, -X]), np.concatenate([dark, dark])
    T, W = np.concatenate([y, 1 - y]), np.concatenate([wt, wt])

    def loss(w):
        z = np.where(D, Z @ w[n:], Z @ w[:n])
        s = np.where(T == 1, z, -z)
        return np.sum(W * np.logaddexp(0, -s)) / W.sum() + L2 * np.mean(w ** 2)

    bounds = [((0, MAX_W) if s > 0 else (-MAX_W, 0)) for s in SIGN] * 2
    return minimize(loss, np.zeros(2 * n), method="L-BFGS-B", bounds=bounds).x


def margin(w, X, dark):
    n = X.shape[1]
    return np.where(dark, X @ w[n:], X @ w[:n])


def score(saved, route, other, daylight):
    """> 0 when route is judged safer than other (what the app would call for two candidate routes)."""
    n = len(saved["features"])
    w = np.array([saved["day"][k] for k in saved["features"]] + [saved["night"][k] for k in saved["features"]])
    return float(margin(w, vec({"A": route, "B": other})[None, :], np.array([daylight < 0.5]))[0])


CLAUDE_ONLY = "--claude-only" in sys.argv
WHO = "Claude" if CLAUDE_ONLY else "Qwen and Claude"


def labels():
    """{pair id: (safer "A"/"B", weight, source)} plus counts of what went in and what was left out."""
    human = rank.human_labels(os.path.join(DATA, "labels_human.csv"))
    # --claude-only: learn from Claude's answers (and people's) alone, e.g. while Qwen is kept off for memory
    qwen = {} if CLAUDE_ONLY else rank.llm_labels(os.path.join(DATA, "labels_llm.csv"))
    claude = label_claude.load()
    out, info = {}, Counter()
    for i in set(human) | set(qwen) | set(claude):
        c, src = claude.get(i, (None, None))
        if i in human:
            out[i] = (human[i], 3.0, "people"); info["people"] += 1
        elif src == "claude_hand" and c in ("A", "B"):
            out[i] = (c, 3.0, "claude_hand"); info["claude_hand"] += 1
        elif src == "claude_hand":
            info["claude_hand_same"] += 1  # Claude said "about the same": not used as A or B
        elif i in qwen and c in ("A", "B"):
            if qwen[i] == c:
                out[i] = (c, 1.0, "qwen+claude"); info["qwen_and_claude_agree"] += 1
            else:
                info["qwen_and_claude_disagree_left_out"] += 1
        elif i in qwen:
            out[i] = (qwen[i], 0.5, "qwen_only"); info["qwen_only"] += 1
        elif c in ("A", "B"):
            out[i] = (c, 0.5, "claude_only"); info["claude_criteria_only"] += 1
    return out, dict(info), human, qwen, claude


def sanity(w):
    """Plain checks the evaluation found broken in v1: each must come out with the right sign, day and night."""
    base = {k: 0.0 for k in pairs.FEATURES} | {"length_m": 2500.0, "police_m": 1000.0}
    cases = {"more lit": ({"lit_pct": 80}, {}), "more open places": ({"open_now": 5}, {}), "police closer": ({"police_m": 100}, {"police_m": 3000}),
             "less isolated": ({}, {"isolated_m": 800}), "CCTV": ({"cctv": 5}, {}), "shorter": ({}, {"length_m": 5000})}
    sv = {"features": KEYS, "day": dict(zip(KEYS, w[:len(KEYS)])), "night": dict(zip(KEYS, w[len(KEYS):]))}
    res = {}
    for nm, (a, b) in cases.items():
        d, n = score(sv, base | a, base | b, 1.0), score(sv, base | a, base | b, 0.0)
        res[nm] = {"day": round(d, 2), "night": round(n, 2), "ok": d >= 0 and n >= 0}
    return res


def main():
    pl = {p["id"]: p for p in pairs.load()}
    lab, info, human, qwen, claude = labels()
    lab = {i: v for i, v in lab.items() if i in pl}
    info.update(pairs_generated=len(pl), answered_by_people=len(human), labelled_by_qwen=len(qwen), labelled_by_claude=len(claude))
    rows = [pl[i] for i in lab]
    areas = len({p["block"] for p in rows})
    if len(rows) < MIN_PAIRS or areas < MIN_AREAS:
        sys.exit(f"Nothing learned: needs {MIN_PAIRS} labelled pairs in {MIN_AREAS} areas, have {len(rows)} in {areas}")
    X = np.array([vec(p) for p in rows]); dark = np.array([p["daylight"] < 0.5 for p in rows])
    y = np.array([lab[p["id"]][0] == "A" for p in rows], float); wt = np.array([lab[p["id"]][1] for p in rows])
    src = np.array([lab[p["id"]][2] for p in rows]); g = np.array([p["block"] for p in rows])
    print(f"Training on {len(rows)} pairs in {areas} areas: {dict(Counter(src))}", flush=True)
    oof = np.zeros(len(y))
    folds = list(GroupKFold(min(5, areas)).split(X, y, g))
    for k, (tr, te) in enumerate(folds, 1):
        oof[te] = margin(fit(X[tr], dark[tr], y[tr], wt[tr]), X[te], dark[te])
        print(f"route fold {k} of {len(folds)}", flush=True)
    hit = (oof > 0) == (y == 1)
    held = {"all_training_labels": round(float(hit.mean()), 3)}
    ids = np.array([p["id"] for p in rows])
    for name, lab_of in (("qwen", qwen), ("people", human)):
        sel = [j for j, i in enumerate(ids) if i in lab_of]
        if sel:
            held[f"vs_{name}"] = {"agree": round(float(np.mean([(oof[j] > 0) == (lab_of[ids[j]] == "A") for j in sel])), 3), "n": len(sel)}
    hand = [j for j, i in enumerate(ids) if claude.get(i, ("", ""))[1] == "claude_hand"]
    if hand:
        held["vs_claude_hand"] = {"agree": round(float(hit[hand].mean()), 3), "n": len(hand)}
    k = KEYS.index("extra_length")
    held["baseline_always_the_shorter_route"] = round(float(((X[:, k] < 0) == (y == 1)).mean()), 3)
    both = [i for i in qwen if claude.get(i, ("", ""))[0] in ("A", "B")]
    info["qwen_agrees_with_claude"] = round(sum(qwen[i] == claude[i][0] for i in both) / len(both), 3) if both else None
    w = fit(X, dark, y, wt)
    checks = sanity(w)
    n = len(KEYS)
    saved = {"features": KEYS, "caps": {k: c for k, (c, _) in PARAMS.items()}, "day": dict(zip(KEYS, w[:n].round(4).tolist())),
             "night": dict(zip(KEYS, w[n:].round(4).tolist())),
             "metrics": {**info, "held_out_areas": held, "sanity_checks": checks},
             "note": f"score > 0 means route A is safer. Learned from {WHO} answers (and people's when present). Not checked against people."}
    os.makedirs(OUT, exist_ok=True)
    json.dump(saved, open(os.path.join(OUT, "route_ranker_v2.json"), "w"), indent=1)
    lines = [f"# Route model v2: learned from {WHO} answers", "",
             "Who answered which route is safer:", "",
             f"- People: {len(human)} pairs" + ("" if human else " (nobody yet: the A/B page is how people answer)"),
             f"- Qwen (local language model): {len(qwen)} pairs with an A or B answer",
             f"- Claude by hand: {info.get('claude_hand', 0)} pairs (+{info.get('claude_hand_same', 0)} called 'about the same')",
             f"- Claude's written criteria (Safetipin-based, applied by code): {sum(1 for v in claude.values() if v[1] == 'claude_rubric')} pairs",
             f"- Qwen and Claude agree on {info['qwen_agrees_with_claude']:.0%} of pairs both answered" if info.get("qwen_agrees_with_claude") is not None else "",
             f"- Used for training: {len(rows)} pairs; left out because Qwen and Claude disagreed: {info.get('qwen_and_claude_disagree_left_out', 0)}",
             "", "Scored on areas the model never trained on:", ""]
    lines += [f"- {k}: {v}" for k, v in held.items()]
    lines += ["", "Sanity checks (score > 0 = the safer-looking route wins; all must pass):", ""]
    lines += [f"- {nm}: day {c['day']:+}, night {c['night']:+} {'ok' if c['ok'] else 'FAILED'}" for nm, c in checks.items()]
    lines += ["", "Weights (per full 0-1 range of each measure; day / night):", ""]
    lines += [f"- {kk}: {w[j]:+.2f} / {w[n + j]:+.2f}" for j, kk in sorted(enumerate(KEYS), key=lambda t: -abs(w[t[0]]) - abs(w[n + t[0]]))]
    lines += ["", f"These are {WHO}'s judgments from the measured numbers, not people's, and not proof a route is safe."]
    text = "\n".join(l for l in lines if l is not None) + "\n"
    open(os.path.join(OUT, "route_report.md"), "w", encoding="utf-8").write(text)
    print(text, flush=True)
    if not all(c["ok"] for c in checks.values()):
        sys.exit("A sanity check failed: see route_report.md")


if __name__ == "__main__":
    main()
