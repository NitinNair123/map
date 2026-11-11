"""Learns what makes one route safer than another from answers to "A or B?": people's (ml/rate_pairs.py) and a local
language model's (ml/label_llm.py). The weights are learned, not typed in, separately for daylight and dark.

The language model's labels are only trusted if they agree with people: with 50+ pairs answered by both, agreement under
75% means training uses people's answers alone. The score reported is on ~3 km areas the model never saw, and on people's
answers it was not trained on. Writes ml/out/route_ranker.json and route_report.md.

Usage: py ml/rank.py
"""
import csv
import json
import os
import sys
from collections import Counter, defaultdict

import numpy as np
from sklearn.linear_model import LogisticRegression
from sklearn.model_selection import GroupKFold

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pairs  # noqa: E402

DATA, OUT = os.path.dirname(pairs.OUT), os.path.join(pairs.ROOT, "ml", "out")
MIN_PAIRS, MIN_BLOCKS, MIN_BOTH, MIN_AGREE = 100, 5, 50, 0.75
COLUMNS = [f"{k}{t}" for t in ("", "_dark") for k in pairs.FEATURES]


def human_labels(path):
    """{pair id: "A" | "B"} by majority of the people who answered; ties and "same" are left out."""
    votes = defaultdict(Counter)
    latest = {}
    if os.path.exists(path):
        for r in csv.DictReader(open(path, encoding="utf-8")):
            latest[(int(r["id"]), r["who"])] = r["safer"]  # a person's last answer for a pair counts
    for (i, _), s in latest.items():
        votes[i][s] += 1
    return {i: "A" if v["A"] > v["B"] else "B" for i, v in votes.items() if v["A"] != v["B"]}


def llm_labels(path):
    return {int(r["id"]): r["safer"] for r in csv.DictReader(open(path, encoding="utf-8")) if r["safer"] in ("A", "B")} if os.path.exists(path) else {}


def diff(p):
    """A minus B for every measure, and again counted only after dark, so a measure can matter differently at night."""
    d = np.array([p["A"][k] - p["B"][k] for k in pairs.FEATURES], float)
    return np.concatenate([d, d * (p["daylight"] < 0.5)])


def fit(X, y, scale):
    """No intercept and every pair also seen swapped: which route is called A can't matter."""
    Xs = X / scale
    return LogisticRegression(fit_intercept=False, C=1.0, max_iter=2000).fit(np.vstack([Xs, -Xs]), np.concatenate([y, 1 - y]))


def train(pair_list, labels):
    """(saved model or None, info) from {pair id: "A"/"B"} labels; whole areas are held out to score it."""
    rows = [p for p in pair_list if p["id"] in labels]
    info = {"pairs": len(rows), "areas": len({p["block"] for p in rows})}
    if len(rows) < MIN_PAIRS or info["areas"] < MIN_BLOCKS:
        return None, {**info, "skipped": f"needs {MIN_PAIRS} answered pairs in {MIN_BLOCKS} areas"}
    X, y = np.array([diff(p) for p in rows]), np.array([labels[p["id"]] == "A" for p in rows], int)
    groups = np.array([p["block"] for p in rows])
    scale = X.std(0) + 1e-9
    hits = np.zeros(len(y))
    for tr, te in GroupKFold(min(5, info["areas"])).split(X, y, groups):
        m = fit(X[tr], y[tr], scale)
        hits[te] = ((X[te] / scale) @ m.coef_[0] > 0) == y[te]
    k = pairs.FEATURES.index("length_m")
    info.update(accuracy_on_unseen_areas=round(float(hits.mean()), 3),
                baseline_always_the_shorter_route=round(float(((X[:, k] < 0) == y).mean()), 3))
    model = fit(X, y, scale)
    weights = dict(zip(COLUMNS, (model.coef_[0] / scale).round(6).tolist()))
    strength = dict(sorted(zip(COLUMNS, np.abs(model.coef_[0]).round(3).tolist()), key=lambda kv: -kv[1]))  # per standard deviation
    return {"weights": weights, "features": pairs.FEATURES, "metrics": info}, {**info, "what_matters_most": dict(list(strength.items())[:12])}


def score(saved, route, other, daylight):
    """> 0 when route is judged safer than other: what the app would call for every pair of candidate routes."""
    w = saved["weights"]
    return sum((route[k] - other[k]) * (w[k] + w[f"{k}_dark"] * (daylight < 0.5)) for k in saved["features"])


def main():
    pl = pairs.load()
    human, llm = human_labels(os.path.join(DATA, "labels_human.csv")), llm_labels(os.path.join(DATA, "labels_llm.csv"))
    both = [i for i in human if i in llm]
    agree = sum(human[i] == llm[i] for i in both) / len(both) if both else None
    info = {"pairs_generated": len(pl), "answered_by_people": len(human), "labelled_by_language_model": len(llm), "answered_by_both": len(both),
            "language_model_agrees_with_people": None if agree is None else round(agree, 3)}
    trust_llm = len(both) >= MIN_BOTH and agree >= MIN_AGREE
    info["trained_on"] = "language model labels, checked against people" if trust_llm else "people's answers only"
    if not trust_llm and llm:
        info["why_not_the_language_model"] = (f"only {len(both)} pairs answered by both, {MIN_BOTH} needed" if len(both) < MIN_BOTH
                                              else f"it agreed with people on {agree:.0%}, under {MIN_AGREE:.0%}")
    unchecked = not human and bool(llm)  # nobody answered: Qwen alone is the labeller, so say plainly it was never checked against people
    if unchecked:
        info["trained_on"] = "language model labels, NOT checked against people (nobody answered any pairs)"
        info.pop("why_not_the_language_model", None)
    labels = llm if unchecked else {i: s for i, s in llm.items() if i not in human} if trust_llm else human  # people's pairs stay out of training when they are the test
    saved, m = train(pl, labels)
    info.update(m)
    if saved and trust_llm:
        test = [p for p in pl if p["id"] in human]
        info["accuracy_on_peoples_answers_not_trained_on"] = round(sum((score(saved, p["A"], p["B"], p["daylight"]) > 0) == (human[p["id"]] == "A") for p in test) / len(test), 3)
    os.makedirs(OUT, exist_ok=True)
    if saved:
        json.dump({**saved, "metrics": info}, open(os.path.join(OUT, "route_ranker.json"), "w"), indent=1)
    lines = ["# What makes a route safer, learned from answers", ""] + [f"- {k}: {v}" for k, v in info.items() if k != "what_matters_most"]
    if "what_matters_most" in info:
        lines += ["", "What matters most (bigger = moves the choice more; `_dark` = extra effect after dark):", ""]
        lines += [f"- {k}: {v} ({'more is safer' if saved['weights'][k] > 0 else 'more is less safe'})" for k, v in info["what_matters_most"].items()]
    text = "\n".join(lines) + "\n"
    open(os.path.join(OUT, "route_report.md"), "w").write(text)
    print(text)
    if not saved:  # a stage that learned nothing must not show "done"
        sys.exit(f"Nothing learned: {info.get('skipped', 'no model')}")


if __name__ == "__main__":
    main()
