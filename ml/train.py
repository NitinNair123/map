"""Trains the AI street model on the rows from ml/dataset.py and checks it on areas it never saw.

Heads (each trained only where a real measurement exists):
  lit            is the street lit at night (OSM lit tag, after-dark light reports)
  open_share     share of nearby places open at that hour (published opening hours)
  bus_per_hour   buses passing at that hour (logged Delhi OTD feed)
  traffic_ratio  current / free-flow speed on main roads at that hour (logged TomTom)
Model: gradient-boosted trees (scikit-learn HistGradientBoosting), which handle missing values and small tabular data well.
Check: 5-fold cross-validation grouped by ~1 km tile, so neighbouring streets never sit on both sides of the split,
against a simple baseline (lit: rate per road type; hourly heads: mean per hour and day type).

Usage: py ml/train.py      writes ml/out/report.md, metrics.json, models/*.pkl and lit_predictions.csv.gz
"""
import glob
import json
import os
import pickle
import sys
from datetime import datetime

import numpy as np
import pandas as pd
from scipy.stats import spearmanr
from sklearn.ensemble import HistGradientBoostingClassifier, HistGradientBoostingRegressor
from sklearn.metrics import brier_score_loss, mean_absolute_error, roc_auc_score
from sklearn.model_selection import GroupKFold

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from features import CATEGORICAL, STATIC, TIME  # noqa: E402

ML = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(ML, "out")
MIN_ROWS, MIN_TILES = 200, 5
# the bar from the plan (section 5c) for the lit head before it may be considered for the app
LIT_AUROC, LIT_ECE = 0.85, 0.05
HOURLY = ["open_share", "bus_per_hour", "traffic_ratio"]


def load(folder):
    edges = [pd.read_csv(p) for p in sorted(glob.glob(os.path.join(folder, "*.edges.csv.gz")))]
    slots = [pd.read_csv(p) for p in sorted(glob.glob(os.path.join(folder, "*.slots.csv.gz")))]
    if not edges:
        sys.exit("No rows yet: run py ml/dataset.py first")
    e = pd.concat(edges).drop_duplicates("edge_id")  # a street on a block edge can be exported twice
    s = pd.concat(slots).drop_duplicates(["edge_id", "hour", "weekend"]) if slots else pd.DataFrame()
    return e, s


def ece(y, p, bins=10):
    """Expected calibration error: how far stated probabilities are from how often it's actually lit."""
    idx = np.minimum((p * bins).astype(int), bins - 1)
    return float(sum(abs(y[idx == b].mean() - p[idx == b].mean()) * (idx == b).mean() for b in range(bins) if (idx == b).any()))


def model(kind, cols):
    cat = [c in CATEGORICAL for c in cols]
    args = dict(max_iter=300, learning_rate=0.06, max_leaf_nodes=31, l2_regularization=1.0, categorical_features=cat, random_state=0)
    return HistGradientBoostingClassifier(**args) if kind == "clf" else HistGradientBoostingRegressor(**args)


def folds(groups):
    return GroupKFold(n_splits=min(5, groups.nunique())).split(groups, groups=groups)


def train_lit(e):
    d = e[e["lit"].notna()].reset_index(drop=True)
    if len(d) < MIN_ROWS or d["tile"].nunique() < MIN_TILES or d["lit"].nunique() < 2:
        return None, {"head": "lit", "rows": int(len(d)), "skipped": "too few labelled streets"}
    X, y = d[STATIC], d["lit"].astype(int).to_numpy()
    p, base = np.zeros(len(d)), np.zeros(len(d))
    for tr, te in folds(d["tile"]):
        p[te] = model("clf", STATIC).fit(X.iloc[tr], y[tr]).predict_proba(X.iloc[te])[:, 1]
        rate = pd.Series(y[tr]).groupby(d["highway"].iloc[tr].to_numpy()).mean()
        base[te] = d["highway"].iloc[te].map(rate).fillna(y[tr].mean()).to_numpy()
    sure = (p >= 0.9) | (p <= 0.1)
    m = {"head": "lit", "rows": int(len(d)), "tiles": int(d["tile"].nunique()), "share_lit": round(float(y.mean()), 3),
         "label_sources": d["lit_source"].value_counts().to_dict(),
         "auroc": round(roc_auc_score(y, p), 3), "baseline_auroc": round(roc_auc_score(y, base), 3),
         "brier": round(brier_score_loss(y, p), 4), "ece": round(ece(y, p), 4),
         "sure_share": round(float(sure.mean()), 3), "sure_accuracy": round(float(((p[sure] >= 0.5) == y[sure]).mean()), 3) if sure.any() else None}
    m["passes_bar"] = bool(m["auroc"] >= LIT_AUROC and m["ece"] <= LIT_ECE)
    return model("clf", STATIC).fit(X, y), m


def train_hourly(e, s, target):
    if s.empty or target not in s:
        return None, {"head": target, "rows": 0, "skipped": "no rows"}
    d = s[s[target].notna()].merge(e, on="edge_id").reset_index(drop=True)
    if len(d) < MIN_ROWS or d["tile"].nunique() < MIN_TILES:
        return None, {"head": target, "rows": int(len(d)), "skipped": "too few measured rows"}
    cols = STATIC + TIME
    X, y = d[cols], d[target].to_numpy()
    p, base = np.zeros(len(d)), np.zeros(len(d))
    for tr, te in folds(d["tile"]):
        p[te] = model("reg", cols).fit(X.iloc[tr], y[tr]).predict(X.iloc[te])
        key = d["hour"].astype(str) + "_" + d["weekend"].astype(str)
        mean = pd.Series(y[tr]).groupby(key.iloc[tr].to_numpy()).mean()
        base[te] = key.iloc[te].map(mean).fillna(y[tr].mean()).to_numpy()
    m = {"head": target, "rows": int(len(d)), "tiles": int(d["tile"].nunique()), "streets": int(d["edge_id"].nunique()),
         "mae": round(mean_absolute_error(y, p), 4), "baseline_mae": round(mean_absolute_error(y, base), 4),
         "spearman": round(float(spearmanr(y, p).statistic), 3), "baseline_spearman": round(float(spearmanr(y, base).statistic), 3)}
    m["beats_baseline"] = bool(m["mae"] < m["baseline_mae"] and m["spearman"] > m["baseline_spearman"])
    return model("reg", cols).fit(X, y), m


def report(metrics, e, s):
    lines = [f"# AI street model: training report", "", f"{datetime.now():%Y-%m-%d %H:%M}. {len(e):,} streets, {len(s):,} street-hour rows. "
             "Checked on ~1 km tiles held out from training (5-fold, grouped by tile).", ""]
    for m in metrics:
        lines.append(f"## {m['head']}")
        lines += [f"- {k}: {v}" for k, v in m.items() if k != "head"]
        lines.append("")
    lines.append("Not wired into the app. These numbers are measured on held-out areas; they say how well the model "
                 "predicts lighting and activity data, not that a street is safe.")
    return "\n".join(lines)


def main():
    e, s = load(os.path.join(ML, "data", "blocks"))
    os.makedirs(os.path.join(OUT, "models"), exist_ok=True)
    metrics = []
    lit, m = train_lit(e)
    metrics.append(m)
    if lit is not None:
        pickle.dump({"model": lit, "features": STATIC, "metrics": m}, open(os.path.join(OUT, "models", "lit.pkl"), "wb"))
        unknown = e[e["lit"].isna()]
        if len(unknown):
            pd.DataFrame({"edge_id": unknown["edge_id"], "lat": unknown["lat"], "lon": unknown["lon"],
                          "p_lit": lit.predict_proba(unknown[STATIC])[:, 1].round(3)}).to_csv(
                os.path.join(OUT, "lit_predictions.csv.gz"), index=False)
            m["unlabelled_streets_predicted"] = int(len(unknown))
    for target in HOURLY:
        mod, m = train_hourly(e, s, target)
        metrics.append(m)
        if mod is not None:
            pickle.dump({"model": mod, "features": STATIC + TIME, "metrics": m}, open(os.path.join(OUT, "models", f"{target}.pkl"), "wb"))
    json.dump(metrics, open(os.path.join(OUT, "metrics.json"), "w"), indent=1)
    text = report(metrics, e, s)
    open(os.path.join(OUT, "report.md"), "w").write(text)
    print(text)


if __name__ == "__main__":
    main()
