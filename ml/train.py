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
TRIALS = 75             # parameter sets tried per head (was 25; a wider search, still scored once on the locked blocks)
BUDGET_S = 3600         # most time spent tuning one head (was 1500); a few trials always run
MAX_ROWS = 3_000_000    # street-hour rows kept per hourly head, so the full NCR build fits in a laptop's memory
MAX_TUNE_ROWS = 300_000  # tuning looks at a random subset of blocks beyond this; the final fit uses everything
SEEDS = 5
SPACE = {"learning_rate": [0.015, 0.03, 0.06, 0.1], "max_leaf_nodes": [7, 15, 31, 63, 127], "max_iter": [300, 600, 1000, 1500],
         "min_samples_leaf": [20, 50, 100, 200, 400], "l2_regularization": [0.0, 1.0, 10.0, 30.0], "max_features": [0.3, 0.5, 0.8, 1.0]}
SPACE_CLF = {**SPACE, "class_weight": [None, "balanced"]}  # the lit head: dark streets are the minority and the costly mistake
ENGINE = "sklearn"  # --engine xgb: XGBoost, on the GPU when CUDA is available (scikit-learn cannot use a GPU)
XGB_DEVICE = "cpu"
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


def xgb_device():
    """'cuda' when XGBoost was built with CUDA and a GPU answers a tiny fit, else 'cpu'."""
    try:
        import xgboost as xgb
        if not xgb.build_info().get("USE_CUDA"):
            return "cpu"
        xgb.XGBRegressor(n_estimators=2, device="cuda", tree_method="hist").fit(np.random.rand(64, 3), np.random.rand(64))
        return "cuda"
    except Exception:
        return "cpu"


def blocks_hash(folder):
    """One hash over every block file's name, size and time: says whether two runs saw the same data."""
    import hashlib
    h = hashlib.sha256()
    for p in sorted(glob.glob(os.path.join(folder, "*"))):
        st = os.stat(p)
        h.update(f"{os.path.basename(p)}|{st.st_size}|{int(st.st_mtime)}\n".encode())
    return h.hexdigest()


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


class Safe:
    """One gradient-boosted model that drops columns holding a single value in the rows it is fitted on. sklearn's
    binning crashes ("window shape cannot be larger than input array shape") on such a column, and in a small
    cross-validation fold a sparse column (e.g. all zero lamps) can be constant even when the whole set is not."""

    def __init__(self, kind, cols, params, seed):
        self.kind, self.cols, self.params, self.seed = kind, list(cols), params, seed

    def fit(self, X, y):
        self.keep = [c for c in self.cols if X[c].nunique(dropna=True) > 1] or self.cols[:1]
        self.const, stop = None, True
        if self.kind == "clf":
            _, counts = np.unique(np.asarray(y), return_counts=True)
            if len(counts) < 2:  # a fold with one class only: every street gets that class
                self.const = float(np.asarray(y)[0])
                return self
            stop = counts.min() >= 2  # early stopping holds out a stratified slice, which needs two of every class
        if ENGINE == "xgb":
            return self._fit_xgb(X, y, stop)
        params = {k: v for k, v in self.params.items() if k in SPACE_CLF}
        kw = dict(early_stopping=stop, validation_fraction=0.1, n_iter_no_change=25, random_state=self.seed,
                  categorical_features=[c in CATEGORICAL for c in self.keep], **params)
        self.model = (HistGradientBoostingClassifier if self.kind == "clf" else HistGradientBoostingRegressor)(**kw)
        self.model.fit(X[self.keep], y)
        return self

    def _xgb_frame(self, X):
        X = X[self.keep].copy()
        for c in self.keep:
            if c in CATEGORICAL:  # road class and footpath codes: the same fixed categories at fit and predict time
                X[c] = X[c].astype(pd.CategoricalDtype(categories=list(range(64))))
            elif X[c].dtype == object:  # e.g. hour/weekend read back as text after a merge: XGBoost needs numbers
                X[c] = pd.to_numeric(X[c], errors="coerce").astype("float32")
        return X

    def _fit_xgb(self, X, y, stop):
        """XGBoost on the GPU (CUDA) when there is one, else on the CPU. The same search space, translated."""
        import xgboost as xgb
        p, y = self.params, np.asarray(y)
        kw = dict(n_estimators=p.get("max_iter", 600), learning_rate=p.get("learning_rate", 0.06), grow_policy="lossguide",
                  max_depth=0, max_leaves=p.get("max_leaf_nodes", 31), min_child_weight=p.get("min_samples_leaf", 20),
                  reg_lambda=p.get("l2_regularization", 1.0), colsample_bynode=p.get("max_features", 1.0), tree_method="hist",
                  device=XGB_DEVICE, enable_categorical=True, max_cat_to_onehot=1, random_state=self.seed, max_bin=256)
        if self.kind == "clf" and p.get("class_weight") == "balanced":
            kw["scale_pos_weight"] = float((y == 0).sum()) / max(1.0, float((y == 1).sum()))
        Xf = self._xgb_frame(X)
        rng = np.random.default_rng(self.seed)
        val = rng.random(len(y)) < 0.1
        if self.kind == "clf":
            stop = stop and len(set(y[val])) == 2 and len(set(y[~val])) == 2
        if stop and val.sum() >= 20:
            kw["early_stopping_rounds"] = 25
            self.model = (xgb.XGBClassifier if self.kind == "clf" else xgb.XGBRegressor)(**kw)
            self.model.fit(Xf[~val], y[~val], eval_set=[(Xf[val], y[val])], verbose=False)
        else:
            self.model = (xgb.XGBClassifier if self.kind == "clf" else xgb.XGBRegressor)(**kw)
            self.model.fit(Xf, y, verbose=False)
        return self

    def _x(self, X):
        return self._xgb_frame(X) if ENGINE == "xgb" else X[self.keep]

    def predict(self, X):
        if self.const is not None:
            return np.full(len(X), int(self.const))
        return self.model.predict(self._x(X))

    def predict_proba(self, X):
        if self.const is not None:
            return np.column_stack([np.full(len(X), 1 - self.const), np.full(len(X), self.const)])
        return self.model.predict_proba(self._x(X))


def make(kind, cols, params, seed=0):
    return Safe(kind, cols, params, seed)


def bag(kind, X, y, params):
    return Bag([make(kind, list(X.columns), params, seed).fit(X, y) for seed in range(SEEDS)])


def predict(kind, model, X):
    return model.predict_proba(X)[:, 1] if kind == "clf" else model.predict(X)


def score_fn(kind):
    if kind == "clf":
        return lambda y, p: log_loss(y, np.clip(p, 1e-4, 1 - 1e-4), labels=[0, 1])
    return mean_absolute_error


def tune(kind, X, y, groups, mask=None, trials=None, budget=None, name="model", incumbent=None):
    """Random search scored by block-grouped cross-validation. mask picks the rows that count for the score
    (NCR streets, when other regions only help to learn). incumbent (the last run's best parameters) is tried first, so a
    re-tune can only keep or beat it on cross-validation. Returns (best params, its out-of-fold predictions, table)."""
    trials, budget = (TRIALS if trials is None else trials), budget or BUDGET_S
    if trials == 0 and not incumbent:
        raise ValueError(f"{name}: --trials 0 re-scores the model in use, but it has no saved parameters")
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
    sampled = list(ParameterSampler(SPACE_CLF if kind == "clf" else SPACE, n_iter=trials, random_state=0)) if trials else []
    for n, params in enumerate(([dict(incumbent)] if incumbent else []) + sampled):
        if n >= 3 and time.time() - started > budget:
            break
        oof = np.zeros(len(yt))
        for tr, te in cv:
            oof[te] = predict(kind, make(kind, list(X.columns), params).fit(Xt.iloc[tr], yt[tr]), Xt.iloc[te])
        s = float(score(yt[mt], oof[mt]))
        table.append({**params, "cv_score": round(s, 5)})
        print(f"{name} trial {n + 1} of {trials}: score {s:.5f}", flush=True)  # the Studio draws these as the learning curve
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


def importance(kind, model, X, y, repeats=3, rows=20000):
    """How much the held-out score drops when one column is scrambled: the honest answer to 'does this parameter help?'"""
    rng = np.random.default_rng(0)
    if len(X) > rows:
        pick = rng.choice(len(X), rows, replace=False)
        X, y = X.iloc[pick], y[pick]
    metric = roc_auc_score if kind == "clf" else (lambda a, b: -mean_absolute_error(a, b))  # higher is better
    base = metric(y, predict(kind, model, X))
    out = {}
    for c in X.columns:
        drops = []
        for _ in range(repeats):
            Xp = X.copy()
            Xp[c] = rng.permutation(Xp[c].to_numpy())
            drops.append(base - metric(y, predict(kind, model, Xp)))
        out[c] = float(np.mean(drops))
    return dict(sorted(out.items(), key=lambda kv: -kv[1]))


# ---------------------------------------------------------------- one head

SLICE_COLS = ["highway", "hour", "weekend", "lanes", "bld_500", "sidewalk"]  # kept next to locked predictions for ml/scorecard.py


def save_locked(name, d, y, p, base, cal=None):
    """The locked-test answers, so ml/scorecard.py can grade slices (night, road type, density) without retraining."""
    out = pd.DataFrame({"y": y, "p": p, "baseline": base, "block": d["block"].to_numpy()})
    for c in SLICE_COLS:
        if c in d:
            out[c] = d[c].to_numpy()
    os.makedirs(os.path.join(OUT, "locked"), exist_ok=True)
    out.to_csv(os.path.join(OUT, "locked", f"{name}.csv.gz"), index=False)


def run_head(name, kind, d, cols, target, trials=None, budget=None, incumbent=None):
    """Tune, choose NCR-only vs all regions, score once on the locked blocks, then fit the final model on everything."""
    d = d.reset_index(drop=True)
    if d.empty:  # no labelled rows at all (e.g. no bus or traffic logs): ~lock below would fail on an empty array
        print(f"Training {name}: 0 rows, skipped", flush=True)
        return None, {"head": name, "rows": 0, "skipped": "no rows"}
    y = d[target].to_numpy(float)
    lock = locked(d["block"])
    ncr = (d["region"] == "ncr").to_numpy()
    info = {"head": name, "rows": int(len(d)), "blocks": int(d["block"].nunique()), "regions": d["region"].value_counts().to_dict()}
    print(f"Training {name}: {len(d)} rows in {info['blocks']} areas", flush=True)
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
        params, oof, cv, table = tune(kind, d.iloc[idx][cols], y[idx], d["block"].to_numpy()[idx], ncr[idx], trials, budget, name,
                                      incumbent)
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
        if TRIALS != 0:  # a re-score (--trials 0) only needs the locked score; the importance scan is the slowest step
            imp = importance(kind, fitted, X[test], y[test])
            info["most_useful_parameters"] = {k: round(x, 4) for k, x in list(imp.items())[:15]}
            info["parameters_with_no_measurable_effect"] = [k for k, x in imp.items() if x <= 0.0002]
        if kind == "clf":
            ok = ~np.isnan(v["oof"]) & ncr[idx]
            cal = IsotonicRegression(out_of_bounds="clip", y_min=0.0, y_max=1.0).fit(v["oof"][ok], y[idx][ok])
            info["locked_test_before_calibration"] = lit_metrics(y[test], p, groups[test], baseline_lit(d.iloc[idx], y[idx], d[test]))
            p = np.clip(cal.predict(p), 0.0, 1.0)
            base = baseline_lit(d.iloc[idx], y[idx], d[test])
            info["locked_test"] = lit_metrics(y[test], p, groups[test], base)
            save_locked(name, d[test], y[test], p, base)
            t = info["locked_test"]
            info["meets_plan_bar"] = bool(t["auroc"] >= LIT_AUROC and t["ece"] <= LIT_ECE)
            info["meets_target_error_below_1_percent"] = bool(t["error_rate_ci95"][1] < TARGET_ERROR)
        else:
            base = baseline_hour(d.iloc[idx], y[idx], d[test])
            info["locked_test"] = hourly_metrics(y[test], p, groups[test], base)
            save_locked(name, d[test], y[test], p, base)
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


def train_lit(e, trials=None, budget=None, incumbent=None):
    d = e[e["lit"].notna()].copy()
    return run_head("lit", "clf", d, STATIC, "lit", trials, budget, incumbent)


def train_hourly(e, s, target, trials=None, budget=None, incumbent=None):
    if s.empty or target not in s:
        return None, {"head": target, "rows": 0, "skipped": "no rows"}
    d = s[s[target].notna()].merge(e, on="edge_id")
    if len(d) > MAX_ROWS:
        d = d.sample(n=MAX_ROWS, random_state=0)  # a random sample of rows; blocks are still held out whole
    d[[c for c in STATIC + TIME if c not in CATEGORICAL and c in d]] = d[[c for c in STATIC + TIME if c not in CATEGORICAL and c in d]].astype("float32")
    return run_head(target, "reg", d, STATIC + TIME, target, trials, budget, incumbent)


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
                lines += ["", "How often the model is wrong, counting only the streets it is sure about", ""] + table(t["error_when_sure"])
            if "most_useful_parameters" in m:
                lines += ["", "Parameters that moved the held-out score most (drop when scrambled):", ""]
                lines += table([{"parameter": k, "score_drop": v} for k, v in m["most_useful_parameters"].items()])
                lines += ["", f"{len(m['parameters_with_no_measurable_effect'])} parameters had no measurable effect here: "
                          + ", ".join(m["parameters_with_no_measurable_effect"][:40])]
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
    global TRIALS, BUDGET_S, OUT, ENGINE, XGB_DEVICE
    ap = argparse.ArgumentParser()
    ap.add_argument("--trials", type=int, default=TRIALS)
    ap.add_argument("--budget", type=int, default=BUDGET_S, help="most seconds spent tuning one head")
    ap.add_argument("--heads", default="all", help="e.g. lit,traffic_ratio: retrain only these; the others keep their saved "
                                                    "model and metrics (fine-tuning one head without a full run)")
    ap.add_argument("--engine", choices=["sklearn", "xgb"], default="sklearn",
                    help="xgb: XGBoost, on the GPU when CUDA is available (the GPU must not be busy with Ollama)")
    a = ap.parse_args()
    TRIALS, BUDGET_S, ENGINE = a.trials, a.budget, a.engine
    if ENGINE == "xgb":
        XGB_DEVICE = xgb_device()
        print(f"Engine: XGBoost on {XGB_DEVICE}", flush=True)
    heads = ["lit"] + HOURLY if a.heads == "all" else [h.strip() for h in a.heads.split(",")]
    bad = [h for h in heads if h not in ["lit"] + HOURLY]
    if bad:
        sys.exit(f"Unknown head(s) {bad}: choose from {['lit'] + HOURLY}")
    old = {}
    if os.path.exists(os.path.join(OUT, "metrics.json")):
        old = {m["head"]: m for m in json.load(open(os.path.join(OUT, "metrics.json")))}
    # every run is a candidate: models, metrics and locked answers go to ml/out/candidates/<run>/, never over the models
    # in use. ml/registry.py checks a candidate against the release gate and only then promotes it.
    OUT = os.path.join(OUT, "candidates", time.strftime("%Y%m%d-%H%M%S"))
    os.makedirs(OUT, exist_ok=True)
    json.dump({"heads": heads, "trials": TRIALS, "budget_s": BUDGET_S, "engine": ENGINE,
               "device": XGB_DEVICE if ENGINE == "xgb" else "cpu", "started": time.strftime("%Y-%m-%d %H:%M:%S"),
               "data_blocks_sha256": blocks_hash(os.path.join(ML, "data", "blocks"))},
              open(os.path.join(OUT, "run.json"), "w"), indent=1)
    print(f"Candidate run: {OUT}", flush=True)
    e, s = load(os.path.join(ML, "data", "blocks"))
    os.makedirs(os.path.join(OUT, "models"), exist_ok=True)
    metrics = {}
    if "lit" in heads:
        lit, m = train_lit(e, incumbent=old.get("lit", {}).get("params"))
        metrics["lit"] = m
    else:
        lit = None
    if lit is not None:
        pickle.dump(lit, open(os.path.join(OUT, "models", "lit.pkl"), "wb"))
        unknown = e[e["lit"].isna()]
        if len(unknown):
            p = lit["calibrator"].predict(lit["model"].predict_proba(unknown[STATIC])[:, 1])
            pd.DataFrame({"edge_id": unknown["edge_id"], "lat": unknown["lat"], "lon": unknown["lon"], "region": unknown["region"],
                          "p_lit": p.round(3)}).to_csv(os.path.join(OUT, "lit_predictions.csv.gz"), index=False)
            m["unlabelled_streets_predicted"] = int(len(unknown))
    for target in [h for h in HOURLY if h in heads]:
        mod, m = train_hourly(e, s, target, incumbent=old.get(target, {}).get("params"))
        metrics[target] = m
        if mod is not None:
            pickle.dump(mod, open(os.path.join(OUT, "models", f"{target}.pkl"), "wb"))
    # heads not retrained this run keep their last metrics (and saved model)
    metrics = [metrics.get(h) or old[h] for h in ["lit"] + HOURLY if h in metrics or h in old]
    json.dump(metrics, open(os.path.join(OUT, "metrics.json"), "w"), indent=1, default=str)
    text = report(metrics, e, s)
    open(os.path.join(OUT, "report.md"), "w").write(text)
    print(text)


if __name__ == "__main__":
    main()
