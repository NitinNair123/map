"""Trains the AI street model on the rows from ml/dataset.py and tests it on areas it never saw.

Heads (each trained only where a real measurement exists):
  lit            is the street lit at night (OSM lit tag, after-dark light reports)
  open_share     share of nearby places open at that hour (published opening hours)
  bus_per_hour   buses passing at that hour (logged Delhi OTD feed)
  traffic_ratio  current / free-flow speed on main roads at that hour (logged TomTom)

How it is kept honest (the "no guessing" rule applied to the model's own score):
  1. A fixed 20% of ~3 km blocks is locked away before anything else. Nothing is tuned, chosen or calibrated on it;
     it is scored once, at the end.
  2. The other 80% is used for parameter search: random search over trees, scored by cross-validation where whole blocks
     are held out, so neighbouring streets never sit on both sides of a split.
  3. If other regions are present (ml/dataset.py --world), "Delhi NCR only" and "NCR plus other regions" are both trained
     and the one with the better cross-validation score on NCR streets is kept; the locked test is NCR streets only.
  4. Probabilities are calibrated (isotonic, on out-of-fold predictions) and the final model is an average of 5 seeds.
  5. The report gives plain error rates with 95% intervals over blocks, next to a dumb baseline, plus the error on only
     the streets the model is sure about. A model is not called good unless it beats the baseline by a margin.

Usage: py ml/train.py [--trials 25]     writes ml/out/report.md, metrics.json, models/*.pkl, lit_predictions.csv.gz
"""
import argparse
import glob
import json
import os
import pickle
import sys
import time
import zlib
from datetime import datetime

import numpy as np
import pandas as pd
from scipy.stats import spearmanr
from sklearn.ensemble import HistGradientBoostingClassifier, HistGradientBoostingRegressor
from sklearn.isotonic import IsotonicRegression
from sklearn.metrics import brier_score_loss, log_loss, mean_absolute_error, roc_auc_score
from sklearn.model_selection import GroupKFold, ParameterSampler

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from features import CATEGORICAL, STATIC, TIME  # noqa: E402

ML = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(ML, "out")
MIN_ROWS, MIN_BLOCKS = 200, 6
LOCKED = 0.2            # share of blocks kept out of every decision
TRIALS = 25             # parameter sets tried per head
BUDGET_S = 1500         # most time spent tuning one head; a few trials always run
MAX_ROWS = 3_000_000    # street-hour rows kept per hourly head, so the full NCR build fits in a laptop's memory
MAX_TUNE_ROWS = 300_000  # tuning looks at a random subset of blocks beyond this; the final fit uses everything
SEEDS = 5
SPACE = {"learning_rate": [0.03, 0.06, 0.1], "max_leaf_nodes": [15, 31, 63, 127], "max_iter": [300, 600, 1000],
         "min_samples_leaf": [20, 50, 100, 200], "l2_regularization": [0.0, 1.0, 10.0], "max_features": [0.5, 0.8, 1.0]}
# the bar from the plan (section 5c) for the lit head before it may be considered for the app
LIT_AUROC, LIT_ECE = 0.85, 0.05
TARGET_ERROR = 0.01
HOURLY = ["open_share", "bus_per_hour", "traffic_ratio"]


# ---------------------------------------------------------------- data

def load(folder):
    edges = [pd.read_csv(p) for p in sorted(glob.glob(os.path.join(folder, "*.edges.csv.gz")))]
    slots = [pd.read_csv(p) for p in sorted(glob.glob(os.path.join(folder, "*.slots.csv.gz")))]
    if not edges:
        sys.exit("No rows yet: run py ml/dataset.py first")
    e = pd.concat(edges).drop_duplicates("edge_id").reset_index(drop=True)  # a street on a block edge can be exported twice
    s = pd.concat(slots).drop_duplicates(["edge_id", "hour", "weekend"]).reset_index(drop=True) if slots else pd.DataFrame()
    if "region" not in e:
        e["region"] = "ncr"
    e["block"] = e["tile"].map(block_of)
    return e, s


def block_of(tile):
    """~3 km square a ~1 km tile belongs to: the unit that is held out together."""
    try:
        i, j = map(int, str(tile).split("_"))
        return f"{i // 3}_{j // 3}"
    except ValueError:
        return str(tile)


def locked(blocks):
    """True for the fixed 20% of blocks that no decision may look at (the same blocks on every run)."""
    return np.array([zlib.crc32(str(b).encode()) % 100 < LOCKED * 100 for b in blocks])


# ---------------------------------------------------------------- models

class Bag:
    """Average of several seeds, so one lucky split of the early-stopping data doesn't decide a street."""

    def __init__(self, models):
        self.models = models

    def predict(self, X):
        return np.mean([m.predict(X) for m in self.models], axis=0)

    def predict_proba(self, X):
        return np.mean([m.predict_proba(X) for m in self.models], axis=0)


def make(kind, cols, params, seed=0):
    kw = dict(early_stopping=True, validation_fraction=0.1, n_iter_no_change=25, random_state=seed,
              categorical_features=[c in CATEGORICAL for c in cols], **params)
    return HistGradientBoostingClassifier(**kw) if kind == "clf" else HistGradientBoostingRegressor(**kw)


def bag(kind, X, y, params):
    return Bag([make(kind, list(X.columns), params, seed).fit(X, y) for seed in range(SEEDS)])


def predict(kind, model, X):
    return model.predict_proba(X)[:, 1] if kind == "clf" else model.predict(X)


def score_fn(kind):
    if kind == "clf":
        return lambda y, p: log_loss(y, np.clip(p, 1e-4, 1 - 1e-4), labels=[0, 1])
    return mean_absolute_error


def tune(kind, X, y, groups, mask=None, trials=None, budget=None):
    """Random search scored by block-grouped cross-validation. mask picks the rows that count for the score
    (NCR streets, when other regions only help to learn). Returns (best params, its out-of-fold predictions, table)."""
    trials, budget = trials or TRIALS, budget or BUDGET_S
    mask = np.ones(len(y), bool) if mask is None else mask
    keep = np.arange(len(y))
    if len(y) > MAX_TUNE_ROWS:
        uniq = np.array(sorted(set(groups)))
        rng = np.random.default_rng(0)
        chosen = set(rng.permutation(uniq)[:max(MIN_BLOCKS, int(len(uniq) * MAX_TUNE_ROWS / len(y)))])
        keep = np.flatnonzero([g in chosen for g in groups])
    Xt, yt, gt, mt = X.iloc[keep], y[keep], np.asarray(groups)[keep], mask[keep]
    cv = list(GroupKFold(n_splits=min(5, len(set(gt)))).split(Xt, yt, gt))
    score, started, table, best = score_fn(kind), time.time(), [], None
    for n, params in enumerate(ParameterSampler(SPACE, n_iter=trials, random_state=0)):
        if n >= 3 and time.time() - started > budget:
            break
        oof = np.zeros(len(yt))
        for tr, te in cv:
            oof[te] = predict(kind, make(kind, list(X.columns), params).fit(Xt.iloc[tr], yt[tr]), Xt.iloc[te])
        s = float(score(yt[mt], oof[mt]))
        table.append({**params, "cv_score": round(s, 5)})
        if best is None or s < best[0]:
            best = (s, params, oof)
    s, params, oof = best
    full = np.full(len(y), np.nan)
    full[keep] = oof
    return params, full, s, sorted(table, key=lambda t: t["cv_score"])


# ---------------------------------------------------------------- metrics

def ece(y, p, bins=10):
    """Expected calibration error: how far stated probabilities are from how often it's actually lit."""
    idx = np.minimum((p * bins).astype(int), bins - 1)
    return float(sum(abs(y[idx == b].mean() - p[idx == b].mean()) * (idx == b).mean() for b in range(bins) if (idx == b).any()))


def block_ci(stat_by_row, groups, reps=500):
    """95% interval of a mean, resampling whole blocks (streets in a block aren't independent)."""
    codes, n = pd.factorize(pd.Series(groups))[0], len(set(groups))
    num, den = np.bincount(codes, stat_by_row, n), np.bincount(codes, minlength=n).astype(float)
    rng = np.random.default_rng(1)
    pick = rng.integers(0, n, (reps, n))
    boot = num[pick].sum(1) / den[pick].sum(1)
    return [round(float(np.percentile(boot, 2.5)), 4), round(float(np.percentile(boot, 97.5)), 4)]


def lit_metrics(y, p, groups, base_p):
    wrong = ((p >= 0.5) != y).astype(float)
    conf = np.maximum(p, 1 - p)
    m = {"rows": int(len(y)), "blocks": int(len(set(groups))), "share_lit": round(float(y.mean()), 3),
         "error_rate": round(float(wrong.mean()), 4), "error_rate_ci95": block_ci(wrong, groups),
         "baseline_error_rate (road type only)": round(float(((base_p >= 0.5) != y).mean()), 4),
         "always_guess_majority_error": round(float(min(y.mean(), 1 - y.mean())), 4),
         "auroc": round(float(roc_auc_score(y, p)), 3), "brier": round(float(brier_score_loss(y, p)), 4), "ece": round(ece(y, p), 4)}
    m["error_when_sure"] = []
    for t in (0.8, 0.9, 0.95, 0.99):
        sure = conf >= t
        m["error_when_sure"].append({"confidence_at_least": t, "share_of_streets": round(float(sure.mean()), 3),
                                     "error_rate": round(float(wrong[sure].mean()), 4) if sure.any() else None})
    return m


def hourly_metrics(y, p, groups, base_p):
    err, base = np.abs(y - p), np.abs(y - base_p)
    return {"rows": int(len(y)), "blocks": int(len(set(groups))), "mae": round(float(err.mean()), 4), "mae_ci95": block_ci(err, groups),
            "baseline_mae (mean per hour)": round(float(base.mean()), 4), "mae_gain_over_baseline_ci95": block_ci(base - err, groups),
            "spearman": round(float(spearmanr(y, p).statistic), 3), "baseline_spearman": round(float(spearmanr(y, base_p).statistic), 3)}


def baseline_lit(d_tr, y_tr, d_te):
    rate = pd.Series(y_tr).groupby(d_tr["highway"].to_numpy()).mean()
    return d_te["highway"].map(rate).fillna(y_tr.mean()).to_numpy()


def baseline_hour(d_tr, y_tr, d_te):
    key = lambda d: d["hour"].astype(int).astype(str) + "_" + d["weekend"].astype(int).astype(str)
    return key(d_te).map(pd.Series(y_tr).groupby(key(d_tr).to_numpy()).mean()).fillna(y_tr.mean()).to_numpy()


# ---------------------------------------------------------------- one head

def run_head(name, kind, d, cols, target, trials=None, budget=None):
    """Tune, choose NCR-only vs all regions, score once on the locked blocks, then fit the final model on everything."""
    d = d.reset_index(drop=True)
    y = d[target].to_numpy(float)
    lock = locked(d["block"])
    ncr = (d["region"] == "ncr").to_numpy()
    info = {"head": name, "rows": int(len(d)), "blocks": int(d["block"].nunique()), "regions": d["region"].value_counts().to_dict()}
    if kind == "clf":
        y = y.astype(int)
        if len(set(y)) < 2:
            return None, {**info, "skipped": "only one class recorded"}
    train, test = ~lock, lock & ncr
    if train.sum() < MIN_ROWS or d.loc[train, "block"].nunique() < MIN_BLOCKS:
        return None, {**info, "skipped": "too few labelled rows or areas"}
    variants = {"ncr_only": train & ncr}
    if (train & ~ncr).sum() >= MIN_ROWS:
        variants["ncr_plus_other_regions"] = train.copy()
    done = {}
    for vname, rows in variants.items():
        if d.loc[rows & ncr, "block"].nunique() < MIN_BLOCKS:
            continue
        idx = np.flatnonzero(rows)
        params, oof, cv, table = tune(kind, d.iloc[idx][cols], y[idx], d["block"].to_numpy()[idx], ncr[idx], trials, budget)
        done[vname] = {"idx": idx, "params": params, "oof": oof, "cv": cv, "table": table[:5]}
    if not done:
        return None, {**info, "skipped": "too few Delhi NCR areas to hold out"}
    info["variants_cv_score_on_ncr (lower is better)"] = {k: round(v["cv"], 5) for k, v in done.items()}
    pick = min(done, key=lambda k: done[k]["cv"])
    v = done[pick]
    info.update(chosen=pick, params=v["params"], top_parameter_sets=v["table"])
    idx = v["idx"]
    X, groups = d[cols], d["block"].to_numpy()

    if test.sum() >= MIN_ROWS and d.loc[test, "block"].nunique() >= 3:
        fitted = bag(kind, X.iloc[idx], y[idx], v["params"])
        p = predict(kind, fitted, X[test])
        if kind == "clf":
            ok = ~np.isnan(v["oof"]) & ncr[idx]
            cal = IsotonicRegression(out_of_bounds="clip", y_min=0.0, y_max=1.0).fit(v["oof"][ok], y[idx][ok])
            info["locked_test_before_calibration"] = lit_metrics(y[test], p, groups[test], baseline_lit(d.iloc[idx], y[idx], d[test]))
            p = np.clip(cal.predict(p), 0.0, 1.0)
            info["locked_test"] = lit_metrics(y[test], p, groups[test], baseline_lit(d.iloc[idx], y[idx], d[test]))
            t = info["locked_test"]
            info["meets_plan_bar"] = bool(t["auroc"] >= LIT_AUROC and t["ece"] <= LIT_ECE)
            info["meets_target_error_below_1_percent"] = bool(t["error_rate_ci95"][1] < TARGET_ERROR)
        else:
            info["locked_test"] = hourly_metrics(y[test], p, groups[test], baseline_hour(d.iloc[idx], y[idx], d[test]))
            gain = info["locked_test"]["mae_gain_over_baseline_ci95"]
            info["beats_baseline"] = bool(gain[0] > 0)  # the whole interval is on the better side
    else:
        info["locked_test"] = "not scored: fewer than 3 locked NCR blocks have rows yet, collect more areas"

    final_idx = np.flatnonzero(variants[pick] | (lock & (ncr if pick == "ncr_only" else True)))  # locked rows join only now
    final = bag(kind, X.iloc[final_idx], y[final_idx], v["params"])
    saved = {"model": final, "features": cols, "kind": kind, "metrics": info}
    if kind == "clf":
        ok = ~np.isnan(v["oof"]) & ncr[idx]
        saved["calibrator"] = IsotonicRegression(out_of_bounds="clip", y_min=0.0, y_max=1.0).fit(v["oof"][ok], y[idx][ok])
    return saved, info


def train_lit(e, trials=None, budget=None):
    d = e[e["lit"].notna()].copy()
    return run_head("lit", "clf", d, STATIC, "lit", trials, budget)


def train_hourly(e, s, target, trials=None, budget=None):
    if s.empty or target not in s:
        return None, {"head": target, "rows": 0, "skipped": "no rows"}
    d = s[s[target].notna()].merge(e, on="edge_id")
    if len(d) > MAX_ROWS:
        d = d.sample(n=MAX_ROWS, random_state=0)  # a random sample of rows; blocks are still held out whole
    d[[c for c in STATIC + TIME if c not in CATEGORICAL and c in d]] = d[[c for c in STATIC + TIME if c not in CATEGORICAL and c in d]].astype("float32")
    return run_head(target, "reg", d, STATIC + TIME, target, trials, budget)


# ---------------------------------------------------------------- report

def table(rows):
    keys = list(rows[0])
    return ["| " + " | ".join(keys) + " |", "|" + "---|" * len(keys)] + ["| " + " | ".join(str(r[k]) for k in keys) + " |" for r in rows]


def report(metrics, e, s):
    lines = ["# AI street model: training report", "",
             f"{datetime.now():%Y-%m-%d %H:%M}. {len(e):,} streets in {e['block'].nunique():,} blocks, {len(s):,} street-hour rows. "
             f"{int(LOCKED * 100)}% of blocks were locked away and scored once; nothing was tuned on them.", ""]
    for m in metrics:
        lines.append(f"## {m['head']}")
        if "skipped" in m:
            lines += [f"Not trained: {m['skipped']} ({m.get('rows', 0)} rows).", ""]
            continue
        t = m.get("locked_test")
        lines.append(f"Trained on {m['rows']:,} rows from {m['blocks']} blocks; regions {m['regions']}. Chosen: {m['chosen']}. "
                     f"Cross-validation scores: {m['variants_cv_score_on_ncr (lower is better)']}.")
        if isinstance(t, dict):
            lines += ["", "Locked test (areas the model never saw):", ""]
            lines += [f"- {k}: {v}" for k, v in t.items() if k != "error_when_sure"]
            if "error_when_sure" in t:
                lines += ["", "Error on only the streets the model is sure about:", ""] + table(t["error_when_sure"])
            for k in ("meets_plan_bar", "meets_target_error_below_1_percent", "beats_baseline"):
                if k in m:
                    lines.append(f"- **{k}: {m[k]}**")
        else:
            lines.append(f"Locked test: {t}")
        lines.append("")
    lines += ["The error rates are measured against recorded labels (OSM lit tags and after-dark reports), which are themselves "
              "imperfect: a tag can be years old. They say how well the model predicts lighting and activity, not that a street is safe.",
              "Not wired into the app."]
    return "\n".join(lines)


def main():
    global TRIALS
    ap = argparse.ArgumentParser()
    ap.add_argument("--trials", type=int, default=TRIALS)
    TRIALS = ap.parse_args().trials
    e, s = load(os.path.join(ML, "data", "blocks"))
    os.makedirs(os.path.join(OUT, "models"), exist_ok=True)
    metrics = []
    lit, m = train_lit(e)
    metrics.append(m)
    if lit is not None:
        pickle.dump(lit, open(os.path.join(OUT, "models", "lit.pkl"), "wb"))
        unknown = e[e["lit"].isna()]
        if len(unknown):
            p = lit["calibrator"].predict(lit["model"].predict_proba(unknown[STATIC])[:, 1])
            pd.DataFrame({"edge_id": unknown["edge_id"], "lat": unknown["lat"], "lon": unknown["lon"], "region": unknown["region"],
                          "p_lit": p.round(3)}).to_csv(os.path.join(OUT, "lit_predictions.csv.gz"), index=False)
            m["unlabelled_streets_predicted"] = int(len(unknown))
    for target in HOURLY:
        mod, m = train_hourly(e, s, target)
        metrics.append(m)
        if mod is not None:
            pickle.dump(mod, open(os.path.join(OUT, "models", f"{target}.pkl"), "wb"))
    json.dump(metrics, open(os.path.join(OUT, "metrics.json"), "w"), indent=1, default=str)
    text = report(metrics, e, s)
    open(os.path.join(OUT, "report.md"), "w").write(text)
    print(text)


if __name__ == "__main__":
    main()
