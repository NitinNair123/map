"""One street model, one training and retraining script.

The model answers one question for any street at any hour: what is this street like right now? It gives four numbers,
each learned only from a real measurement taken from the internet or our own logs (no human raters, no AI-made labels):
  lit            is the street lit at night          (OpenStreetMap lit tags)
  open_share     share of nearby places open         (published opening hours)
  bus_per_hour   buses passing                       (logged Delhi OTD feed)
  traffic_ratio  current / free-flow speed           (logged TomTom)
It does NOT say a street is safe. The app's written rule decides routes; this model fills the rule's gaps with estimates.

Why it is one model and not four: every part first forms a belief about a street from the other three (a street with many
buses and open places at night is more likely to be lit, and the other way round), and the final answer uses those
beliefs. So the 7,000 streets with a lighting label borrow from the millions of bus, traffic and opening-hour rows.
That borrowing is kept for a part only when it scores better on held-out areas; otherwise the part falls back to plain.

How it is kept honest:
  1. A fixed 20% of ~3 km blocks is locked away (the same blocks as ml/train.py). Nothing is tuned, chosen or
     calibrated on them. They are scored once.
  2. Everything else is decided by cross-validation that holds out whole blocks. The beliefs passed between parts are
     always made by a model that never saw that block, so no answer leaks.
  3. The model may say "unknown". The cut-offs for "sure" come from held-out predictions, then get checked on the locked
     blocks, including the costly mistake: a dark street called lit.
  4. Hourly numbers come with a range that should hold the truth 80% of the time; the locked blocks check that it does.
  5. Lighting labels sit on unusual streets (mapped by volunteers). The report re-weights the locked score to what it
     would be on typical streets, and says how few labelled streets that estimate really rests on.
  6. Release gate, part by part: beat the simple baseline with the whole 95% interval, pass the checks above, and be no
     worse than the model in use on the very same locked rows. A part that fails keeps its current version.

Usage (on Windows: py instead of python)
  python ml/unified.py train              train a candidate in ml/out/unified/candidates/<time>/ ; changes nothing in use
  python ml/unified.py retrain            train only if the data changed, then promote the parts that pass the gate
  python ml/unified.py check <run>        show the gate verdict for a candidate
  python ml/unified.py promote <run>      promote the parts that pass (the model in use is backed up first)
  python ml/unified.py rollback           put back the model from before the last promotion
  python ml/unified.py status             what is in use and its scores
  options for train/retrain: --trials N  --budget SECONDS  --quick (a fast smoke run, not for release)  --force

Use from code:
  model = pickle.load(open("ml/out/unified/current/street_model.pkl", "rb"))     # needs ml/ on sys.path
  streets, hours = model.predict(edges_table, slots_table)
"""
import argparse
import glob
import hashlib
import json
import os
import pickle
import shutil
import sys
import time
import zlib

import numpy as np
import pandas as pd
from scipy.stats import spearmanr
from sklearn.isotonic import IsotonicRegression
from sklearn.model_selection import ParameterSampler

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import train  # noqa: E402  only its stable helpers: load, locked, make, bag, predict, metrics
from features import CATEGORICAL, STATIC, TIME  # noqa: E402

ML = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.join(ML, "out", "unified")
DATA = os.path.join(ML, "data", "blocks")
HEADS = {"lit": "clf", "open_share": "reg", "bus_per_hour": "reg", "traffic_ratio": "reg"}
HOURLY = [h for h, k in HEADS.items() if k == "reg"]
K = 4                    # cross-validation folds, whole blocks each
STAGE1_ROWS = 200_000    # rows a belief model learns from: it only has to be roughly right, the final model does the rest
LIT_SLOT_ROWS = 500_000  # hour rows read to summarise a lit-labelled street's buses, traffic and open places
TRIALS, BUDGET_S = 20, 1800
DEFAULT = {"learning_rate": 0.06, "max_leaf_nodes": 31, "max_iter": 600, "min_samples_leaf": 50, "l2_regularization": 1.0}
# Design choices, not measurements. Change them here and the report and the gate follow.
SURE_ERROR = 0.05        # "sure" cut-offs are set so that at most 5% of held-out sure answers are wrong
SURE_ERROR_MAX = 0.10    # gate: on the locked blocks a sure "lit" may be wrong at most this often
INTERVAL = 0.80          # hourly ranges aim to hold the truth this often
COVERAGE_OK = (0.70, 0.90)  # gate: what the locked blocks must actually show for that 80%
PROFILE_MIN = 0.8        # gate: predicted and real hour-of-day curves must rise and fall together (Spearman)
WORSE_TOLERANCE = 0.01   # gate: may be at most 1% worse than the model in use on the same locked rows (noise allowance)


# ---------------------------------------------------------------- data

def data_hash(folder):
    """One hash over every block file's name and size: says whether there is new data since the last run."""
    h = hashlib.sha256()
    for p in sorted(glob.glob(os.path.join(folder, "*.csv.gz"))):
        h.update(f"{os.path.basename(p)}|{os.path.getsize(p)}\n".encode())
    return h.hexdigest()


def fold_of(blocks):
    """-1 for the locked blocks (same rule as ml/train.py), else a fixed fold 0..K-1 per block."""
    lock = train.locked(blocks)
    return np.where(lock, -1, [zlib.crc32(f"fold{b}".encode()) % K for b in blocks])


def base_cols(head, d):
    return [c for c in (STATIC if head == "lit" else STATIC + TIME) if c in d]


def rows_for(head, e, s):
    """The rows that carry a real measurement for this head, with the street's parameters and its fold."""
    if head == "lit":
        d = e[e["lit"].notna()].copy()
    elif s.empty or head not in s:
        d = e.iloc[:0].copy()
        d[head] = []
    else:
        d = s[s[head].notna()]
        if len(d) > train.MAX_ROWS:
            d = d.sample(n=train.MAX_ROWS, random_state=0)  # a random sample of rows; blocks are still held out whole
        d = d.merge(e, on="edge_id")
    num = [c for c in STATIC + TIME if c not in CATEGORICAL and c in d]
    d[num] = d[num].astype("float32")  # the full NCR build has to fit in a laptop's memory
    d["fold"] = fold_of(d["block"]) if len(d) else []
    return d.reset_index(drop=True)


def target(head, d):
    y = d[head].to_numpy(float)
    return y.astype(int) if HEADS[head] == "clf" else y


# ---------------------------------------------------------------- beliefs passed between the parts

def belief_cols(head):
    if head == "lit":
        return [f"s1_{g}_{w}" for g in HOURLY for w in ("mean", "night")]
    return ["s1_lit"] + [f"s1_{g}" for g in HOURLY if g != head]


def add_beliefs(head, d, s, believe):
    """Adds what the other parts believe about each row. believe(g, table) -> part g's prediction for every row of table
    (NaN where it has none). In training it is cross-fitted; in use it is the stored belief model."""
    for c in belief_cols(head):
        d[c] = np.nan
    if not len(d):
        return d
    if head != "lit":
        d["s1_lit"] = believe("lit", d)
        for g in HOURLY:
            if g != head:
                d[f"s1_{g}"] = believe(g, d)
        return d
    if s is None or s.empty:
        return d
    hours = s[s["edge_id"].isin(set(d["edge_id"]))][["edge_id"] + [c for c in TIME if c in s]]
    if len(hours) > LIT_SLOT_ROWS:
        hours = hours.sample(n=LIT_SLOT_ROWS, random_state=0)
    hours = hours.merge(d.drop(columns=[c for c in TIME if c in d], errors="ignore"), on="edge_id")
    if not len(hours):
        return d
    night = (hours["daylight"] < 0.5).to_numpy() if "daylight" in hours else np.zeros(len(hours), bool)
    for g in HOURLY:
        p = pd.Series(believe(g, hours), index=hours["edge_id"].to_numpy())
        d[f"s1_{g}_mean"] = d["edge_id"].map(p.groupby(level=0).mean()).to_numpy()
        d[f"s1_{g}_night"] = d["edge_id"].map(p[night].groupby(level=0).mean()).to_numpy()
    return d


def fit_one(head, d, cols, params, seed=0):
    """One quick model for a belief: a sample of the rows, one seed."""
    if len(d) > STAGE1_ROWS:
        d = d.sample(n=STAGE1_ROWS, random_state=seed)
    return train.make(HEADS[head], cols, params, seed).fit(d[cols], target(head, d))


def enough(head, d):
    """Why this head can't be trained on these rows, or None."""
    if len(d) < train.MIN_ROWS or d["block"].nunique() < train.MIN_BLOCKS:
        return "too few labelled rows or areas"
    if HEADS[head] == "clf" and len(set(target(head, d))) < 2:
        return "only one class recorded"
    return None


def stage1(tables, params):
    """Belief models per head: one per fold (fitted without that fold and without the locked blocks), "outside" (fitted on
    every unlocked block, used for the locked ones) and "all" (fitted on everything, stored in the finished model)."""
    out = {}
    for head, d in tables.items():
        un = d[d["fold"] >= 0] if len(d) else d
        if enough(head, un):
            continue
        cols, p = base_cols(head, d), params.get(head) or DEFAULT
        folds = [fit_one(head, un[un["fold"] != k], cols, p) for k in range(K)]
        out[head] = {"cols": cols, "folds": folds, "outside": fit_one(head, un, cols, p), "all": fit_one(head, d, cols, p)}
        print(f"beliefs: {head} ready ({len(un):,} rows)", flush=True)
    return out


def crossfit(s1):
    """believe() for training: each row is predicted by the belief model that never saw its block."""
    def believe(g, table):
        out = np.full(len(table), np.nan)
        if g not in s1:
            return out
        fold = table["fold"].to_numpy() if "fold" in table else fold_of(table["block"])
        for k in [-1, *range(K)]:
            rows = fold == k
            if rows.any():
                m = s1[g]["outside"] if k < 0 else s1[g]["folds"][k]
                out[rows] = train.predict(HEADS[g], m, table.loc[rows, s1[g]["cols"]])
        return out
    return believe


# ---------------------------------------------------------------- one head

def tune_rows(d, un):
    """The unlocked rows the search looks at: every block when small, a fixed random set of blocks when large."""
    if un.sum() <= train.MAX_TUNE_ROWS:
        return un
    blocks = np.array(sorted(set(d.loc[un, "block"])))
    keep = set(np.random.default_rng(0).permutation(blocks)[:max(train.MIN_BLOCKS, int(len(blocks) * train.MAX_TUNE_ROWS / un.sum()))])
    return un & d["block"].isin(keep).to_numpy()


def cv_oof(head, d, cols, params, rows):
    """Held-out predictions for `rows`: each fold predicted by a model fitted on the other folds."""
    kind, y, fold = HEADS[head], target(head, d), d["fold"].to_numpy()
    oof = np.full(len(d), np.nan)
    for k in range(K):
        tr, te = rows & (fold != k), rows & (fold == k)
        if te.any() and tr.any():
            oof[te] = train.predict(kind, train.make(kind, cols, params).fit(d.loc[tr, cols], y[tr]), d.loc[te, cols])
    return oof


def sure_cutoffs(y, p):
    """(at or below: sure dark, at or above: sure lit). Each is the widest cut-off whose held-out answers are wrong at
    most SURE_ERROR of the time; None when no cut-off manages that, and then the model never claims to be sure."""
    grid = np.round(np.arange(0.5, 1.0, 0.01), 2)
    lit = next((float(t) for t in grid if (p >= t).sum() >= 30 and (y[p >= t] == 0).mean() <= SURE_ERROR), None)
    dark = next((float(1 - t) for t in grid if (p <= 1 - t).sum() >= 30 and (y[p <= 1 - t] == 1).mean() <= SURE_ERROR), None)
    return dark, lit


def interval_fit(y, p):
    """Range half-widths by size of the prediction: within each fifth of held-out predictions, the miss that INTERVAL of rows stay under."""
    edges = np.unique(np.quantile(p, [0.2, 0.4, 0.6, 0.8]))
    idx = np.searchsorted(edges, p)
    return edges, np.array([np.quantile(np.abs(y - p)[idx == b], INTERVAL) if (idx == b).any() else np.nan for b in range(len(edges) + 1)])


def interval_width(interval, p):
    edges, q = interval
    return q[np.searchsorted(edges, p)]


def typical_weights(e, d, cols):
    """How much each labelled street should count so that the labelled set looks like all NCR streets: a cross-fitted
    model tells labelled from unlabelled streets, and streets that look unlabelled count for more. Clipped to 0.1-10."""
    ncr = e[e["region"] == "ncr"] if "region" in e else e
    pool = ncr.assign(has=ncr["lit"].notna().astype(int), fold=fold_of(ncr["block"]))
    if pool["has"].nunique() < 2:
        return None
    if len(pool) > STAGE1_ROWS:
        pool = pd.concat([pool[pool["has"] == 1], pool[pool["has"] == 0].sample(n=min(STAGE1_ROWS, (pool["has"] == 0).sum()), random_state=0)])
    p = pd.Series(np.nan, index=pool["edge_id"].to_numpy())
    for k in sorted(set(pool["fold"])):
        tr, te = pool[pool["fold"] != k], pool[pool["fold"] == k]
        if tr["has"].nunique() < 2:
            continue
        p[te["edge_id"].to_numpy()] = train.predict("clf", train.make("clf", cols, DEFAULT).fit(tr[cols], tr["has"].to_numpy()), te[cols])
    prior = pool["has"].mean()
    odds = ((1 - p) / p.clip(1e-3)) * (prior / (1 - prior))  # 1 when a street looks as labelled as average
    return d["edge_id"].map(odds.clip(0.1, 10)).to_numpy()


def lit_extras(y, p, groups, cuts, w):
    dark_cut, lit_cut = cuts
    sure_lit = p >= lit_cut if lit_cut is not None else np.zeros(len(p), bool)
    sure_dark = p <= dark_cut if dark_cut is not None else np.zeros(len(p), bool)
    out = {"sure_cutoffs (dark at or below, lit at or above)": [dark_cut, lit_cut],
           "share_answered_sure": round(float((sure_lit | sure_dark).mean()), 3),
           "share_left_unknown": round(float(1 - (sure_lit | sure_dark).mean()), 3),
           "sure_lit_rows": int(sure_lit.sum()), "sure_dark_rows": int(sure_dark.sum()),
           "dark_called_sure_lit": round(float((y[sure_lit] == 0).mean()), 4) if sure_lit.any() else None,
           "lit_called_sure_dark": round(float((y[sure_dark] == 1).mean()), 4) if sure_dark.any() else None,
           "share_of_dark_streets_called_sure_lit": round(float(sure_lit[y == 0].mean()), 4) if (y == 0).any() else None}
    if w is not None and np.isfinite(w).any():
        ok = np.isfinite(w)
        wrong = ((p >= 0.5) != y).astype(float)
        out["error_rate_on_typical_streets (re-weighted estimate)"] = round(float(np.average(wrong[ok], weights=w[ok])), 4)
        out["labelled_streets_that_estimate_rests_on (effective)"] = int(w[ok].sum() ** 2 / (w[ok] ** 2).sum())
    return out


def hourly_extras(d, y, p, interval):
    half = interval_width(interval, p)
    key = d["hour"].astype(int).to_numpy() if "hour" in d else np.zeros(len(d), int)
    real, pred = pd.Series(y).groupby(key).mean(), pd.Series(p).groupby(key).mean()
    profile = float(spearmanr(real, pred).statistic) if len(real) >= 6 and real.nunique() > 1 and pred.nunique() > 1 else None
    return {f"range_holds_truth (aim {INTERVAL})": round(float((np.abs(y - p) <= half).mean()), 3),
            "median_range_half_width": round(float(np.nanmedian(half)), 4),
            "hour_of_day_curve_match (Spearman)": None if profile is None or np.isnan(profile) else round(profile, 3)}


def fit_head(head, d, e, trials, budget, incumbent, seeds):
    """Search, choose with or without beliefs, calibrate, score once on the locked blocks, then fit on everything."""
    kind, info = HEADS[head], {"head": head, "rows": int(len(d)), "blocks": int(d["block"].nunique()) if len(d) else 0}
    fold = d["fold"].to_numpy() if len(d) else np.array([])
    un = fold >= 0
    why = enough(head, d[un]) if len(d) else "no rows"
    if why:
        print(f"{head}: skipped ({why})", flush=True)
        return None, {**info, "skipped": why}
    y, ncr = target(head, d), (d["region"] == "ncr").to_numpy()
    base, stacked = base_cols(head, d), base_cols(head, d) + belief_cols(head)
    rows, score = tune_rows(d, un), train.score_fn(kind)
    cv = lambda oof: float(score(y[rows & ncr], np.clip(oof[rows & ncr], 0, None) if kind == "reg" else oof[rows & ncr]))
    print(f"Training {head}: {len(d):,} rows in {info['blocks']} areas", flush=True)

    space = getattr(train, "SPACE_CLF", train.SPACE) if kind == "clf" else train.SPACE
    tried = [dict(incumbent)] if incumbent else [dict(DEFAULT)]
    tried += list(ParameterSampler(space, n_iter=trials, random_state=0)) if trials else []
    started, best = time.time(), None
    for n, params in enumerate(tried):
        if n >= 2 and time.time() - started > budget:
            break
        try:
            oof = cv_oof(head, d, base, params, rows)
            s = cv(oof)
        except (TypeError, ValueError) as err:  # a parameter this sklearn does not know: skip the set, don't lose the run
            print(f"{head} trial {n + 1}: skipped ({err})", flush=True)
            continue
        print(f"{head} trial {n + 1} of {len(tried)}: score {s:.5f}", flush=True)
        if best is None or s < best[0]:
            best = (s, params, oof)
    if best is None:
        return None, {**info, "skipped": "no parameter set could be fitted"}
    s_base, params, oof = best
    oof_stack = cv_oof(head, d, stacked, params, rows)
    s_stack = cv(oof_stack)
    use_beliefs = s_stack < s_base
    cols, oof = (stacked, oof_stack) if use_beliefs else (base, oof)
    info.update(params=params, cv_score_plain=round(s_base, 5), cv_score_with_beliefs=round(s_stack, 5), uses_beliefs=bool(use_beliefs))
    print(f"{head}: plain {s_base:.5f}, with beliefs {s_stack:.5f} -> {'with beliefs' if use_beliefs else 'plain'}", flush=True)

    ok = rows & ncr & ~np.isnan(oof)
    saved = {"kind": kind, "cols": cols, "uses_beliefs": bool(use_beliefs)}
    if kind == "clf":
        saved["calibrator"] = IsotonicRegression(out_of_bounds="clip", y_min=0.0, y_max=1.0).fit(oof[ok], y[ok])
        # shortcut: cut-offs come from the same held-out rows the calibrator was fitted on (slightly optimistic);
        # the locked blocks below are the real check. Split the rows if the two ever disagree.
        saved["sure"] = sure_cutoffs(y[ok], saved["calibrator"].predict(oof[ok]))
    else:
        saved["interval"] = interval_fit(y[ok], np.clip(oof[ok], 0, None))

    test = (fold == -1) & ncr
    if test.sum() >= train.MIN_ROWS and d.loc[test, "block"].nunique() >= 3 and (kind == "reg" or len(set(y[test])) == 2):
        X, groups = d[cols], d["block"].to_numpy()
        fitted = bagged(kind, X[un], y[un], params, seeds)
        p = train.predict(kind, fitted, X[test])
        keys = d.loc[test, [c for c in ("edge_id", "hour", "weekend", "highway", "block") if c in d]].reset_index(drop=True)
        if kind == "clf":
            p = np.clip(saved["calibrator"].predict(p), 0.0, 1.0)
            basep = train.baseline_lit(d[un], y[un], d[test])
            t = train.lit_metrics(y[test], p, groups[test], basep)
            w = typical_weights(e, d[test], base)
            t.update(lit_extras(y[test], p, groups[test], saved["sure"], w))
            t["gain_over_road_type_ci95"] = train.block_ci(((basep >= 0.5) != y[test]).astype(float) - ((p >= 0.5) != y[test]), groups[test])
        else:
            p = np.clip(p, 0, None)
            basep = train.baseline_hour(d[un], y[un], d[test])
            t = train.hourly_metrics(y[test], p, groups[test], basep)
            t.update(hourly_extras(d[test], y[test], p, saved["interval"]))
        info["locked_test"] = t
        saved["locked"] = keys.assign(y=y[test], p=p, baseline=basep)
    else:
        info["locked_test"] = "not scored: fewer than 3 locked NCR blocks have rows yet, collect more areas"
    saved["model"] = bagged(kind, d[cols], y, params, seeds)  # the locked rows join only now
    return saved, info


def bagged(kind, X, y, params, seeds):
    return train.Bag([train.make(kind, list(X.columns), params, seed).fit(X, y) for seed in range(seeds)])


# ---------------------------------------------------------------- the model

class StreetModel:
    """The one model. heads: {name: {"kind", "cols", "model", "calibrator"|"interval", "sure", "beliefs": {name: (model, cols)}}}."""

    def __init__(self, heads, made=None):
        self.heads, self.made = heads, made or {}

    def _table(self, head, e, s):
        h = self.heads[head]
        d = e.copy() if head == "lit" else s.merge(e, on="edge_id")
        if h["uses_beliefs"]:
            def believe(g, table):
                if g not in h["beliefs"]:
                    return np.full(len(table), np.nan)
                m, cols = h["beliefs"][g]
                return train.predict(HEADS[g], m, table[cols])
            d = add_beliefs(head, d, s, believe)
        return d

    def predict(self, e, s=None):
        """e: street rows (edge_id + the STATIC columns). s: hour rows (edge_id + the TIME columns), or None.
        Returns (streets, hours). streets: edge_id, p_lit, lit ("lit" / "dark" / "unknown").
        hours: edge_id, hour, weekend, and for each hourly part its value with _lo and _hi (the 80% range)."""
        streets = pd.DataFrame({"edge_id": e["edge_id"].to_numpy()})
        if "lit" in self.heads:
            h = self.heads["lit"]
            d = self._table("lit", e, s)
            p = np.clip(h["calibrator"].predict(train.predict("clf", h["model"], d[h["cols"]])), 0.0, 1.0)
            dark_cut, lit_cut = h["sure"]
            streets["p_lit"] = p
            streets["lit"] = np.where(p >= (2 if lit_cut is None else lit_cut), "lit", np.where(p <= (-1 if dark_cut is None else dark_cut), "dark", "unknown"))
        hours = None
        if s is not None and len(s):
            hours = s[["edge_id"] + [c for c in ("hour", "weekend") if c in s]].reset_index(drop=True).copy()
            for head in HOURLY:
                if head in self.heads:
                    h = self.heads[head]
                    d = self._table(head, e, s.reset_index(drop=True))
                    p = np.clip(train.predict("reg", h["model"], d[h["cols"]]), 0, None)
                    half = interval_width(h["interval"], p)
                    # the merge keeps the order of s for rows whose street is in e; align by key to be safe
                    out = d[["edge_id"] + [c for c in ("hour", "weekend") if c in d]].assign(**{head: p, f"{head}_lo": np.clip(p - half, 0, None), f"{head}_hi": p + half})
                    hours = hours.merge(out.drop_duplicates(["edge_id"] + [c for c in ("hour", "weekend") if c in out]), how="left",
                                        on=["edge_id"] + [c for c in ("hour", "weekend") if c in out])
        return streets, hours


# ---------------------------------------------------------------- gate, promotion, rollback

def current_dir():
    return os.path.join(ROOT, "current")


def read_metrics(folder):
    p = os.path.join(folder, "metrics.json")
    return json.load(open(p)) if os.path.exists(p) else {}


def locked_table(folder, head):
    p = os.path.join(folder, "locked", f"{head}.csv.gz")
    return pd.read_csv(p) if os.path.exists(p) else None


def versus_current(head, cand, cur):
    """Mean error of the candidate minus the model in use, on the locked rows both answered, scored against the newest
    measurement. Neither model saw those rows. Returns (difference, current error, rows) or None when nothing is shared."""
    if cand is None or cur is None:
        return None
    keys = [c for c in ("edge_id", "hour", "weekend") if c in cand and c in cur]
    both = cand.merge(cur[keys + ["p"]], on=keys, suffixes=("", "_cur"))
    if len(both) < 30:
        return None
    err = (lambda p: ((p >= 0.5) != both["y"]).astype(float)) if HEADS[head] == "clf" else (lambda p: (both["y"] - p).abs())
    return float(err(both["p"]).mean() - err(both["p_cur"]).mean()), float(err(both["p_cur"]).mean()), int(len(both))


def gate_head(head, m, cand_locked=None, cur_locked=None):
    """(passed, reasons). Every reason is a failed check; an empty list means the part may replace the one in use."""
    t = m.get("locked_test")
    if "skipped" in m or not isinstance(t, dict):
        return False, [f"not trained or not scored ({m.get('skipped') or t})"]
    why = []
    if HEADS[head] == "clf":
        if not t["gain_over_road_type_ci95"][0] > 0:
            why.append(f"does not clearly beat the road-type guess (gain 95% interval {t['gain_over_road_type_ci95']})")
        bad = t.get("dark_called_sure_lit")
        if bad is not None and bad > SURE_ERROR_MAX:
            why.append(f"{bad:.0%} of streets it called surely lit were dark (limit {SURE_ERROR_MAX:.0%})")
    else:
        if not t["mae_gain_over_baseline_ci95"][0] > 0:
            why.append(f"does not clearly beat the hour-of-day average (gain 95% interval {t['mae_gain_over_baseline_ci95']})")
        cover = t.get(f"range_holds_truth (aim {INTERVAL})")
        if cover is not None and not COVERAGE_OK[0] <= cover <= COVERAGE_OK[1]:
            why.append(f"its 80% range held the truth {cover:.0%} of the time (allowed {COVERAGE_OK[0]:.0%} to {COVERAGE_OK[1]:.0%})")
        prof = t.get("hour_of_day_curve_match (Spearman)")
        if prof is not None and prof < PROFILE_MIN:
            why.append(f"its hour-of-day curve does not follow the real one (Spearman {prof}, need {PROFILE_MIN})")
    vs = versus_current(head, cand_locked, cur_locked)
    if vs and vs[0] > WORSE_TOLERANCE * max(vs[1], 1e-9):
        why.append(f"worse than the model in use on the same {vs[2]} locked rows (error {vs[1] + vs[0]:.4f} vs {vs[1]:.4f})")
    return not why, why


def check(run):
    """{head: (passed, reasons)} for a candidate, against the model in use."""
    folder = os.path.join(ROOT, "candidates", run)
    if not os.path.isdir(folder):
        sys.exit(f"No candidate {run} in {os.path.join(ROOT, 'candidates')}")
    cur = current_dir()
    return {head: gate_head(head, m, locked_table(folder, head), locked_table(cur, head))
            for head, m in read_metrics(folder).get("heads", {}).items()}


def promote(run):
    """The parts that pass replace the ones in use; the rest keep their current version. Returns the verdicts."""
    folder, cur = os.path.join(ROOT, "candidates", run), current_dir()
    verdict = check(run)
    passed = [h for h, (ok, _) in verdict.items() if ok]
    if not passed:
        print("Nothing promoted: no part passed the gate. The model in use is unchanged.")
        return verdict
    cand = pickle.load(open(os.path.join(folder, "street_model.pkl"), "rb"))
    old = pickle.load(open(os.path.join(cur, "street_model.pkl"), "rb")) if os.path.exists(os.path.join(cur, "street_model.pkl")) else None
    old_metrics, new_metrics = read_metrics(cur), read_metrics(folder)
    heads = {**(old.heads if old else {}), **{h: cand.heads[h] for h in passed}}
    metrics = {"heads": {**old_metrics.get("heads", {}), **{h: new_metrics["heads"][h] for h in passed}},
               "data_sha256": new_metrics.get("data_sha256"), "promoted": time.strftime("%Y-%m-%d %H:%M:%S"), "from_run": run}
    nxt = os.path.join(ROOT, "next")
    shutil.rmtree(nxt, ignore_errors=True)
    os.makedirs(os.path.join(nxt, "locked"))
    for h in heads:  # each part's locked answers travel with it, so the next gate compares like with like
        src = os.path.join(folder if h in passed else cur, "locked", f"{h}.csv.gz")
        if os.path.exists(src):
            shutil.copy2(src, os.path.join(nxt, "locked"))
    pickle.dump(StreetModel(heads, {"promoted": metrics["promoted"], "from_run": run}), open(os.path.join(nxt, "street_model.pkl"), "wb"))
    json.dump(metrics, open(os.path.join(nxt, "metrics.json"), "w"), indent=1, default=str)
    if os.path.isdir(cur):
        os.replace(cur, os.path.join(ROOT, "backup-" + time.strftime("%Y%m%d-%H%M%S")))
    os.replace(nxt, cur)
    hist = os.path.join(ROOT, "history.json")
    log = json.load(open(hist)) if os.path.exists(hist) else []
    log.append({"when": metrics["promoted"], "run": run, "promoted": passed, "kept": {h: why for h, (ok, why) in verdict.items() if not ok}})
    json.dump(log, open(hist, "w"), indent=1)
    print(f"Promoted {passed}. Kept as they were: {[h for h in verdict if h not in passed] or 'none'}.")
    return verdict


def rollback():
    backups = sorted(glob.glob(os.path.join(ROOT, "backup-*")))
    if not backups:
        sys.exit("No backup to roll back to")
    cur = current_dir()
    if os.path.isdir(cur):
        os.replace(cur, os.path.join(ROOT, "rolledback-" + time.strftime("%Y%m%d-%H%M%S")))
    os.replace(backups[-1], cur)
    print(f"Rolled back to {os.path.basename(backups[-1])}")


# ---------------------------------------------------------------- train

def report(metrics, verdict):
    lines = ["# SafeRoute street model: training report", "", f"{time.strftime('%Y-%m-%d %H:%M')}. One model, four answers per street and hour. "
             "Every score below is from areas the model never saw. It predicts lighting and activity; it does not say a street is safe.", ""]
    for head, m in metrics["heads"].items():
        ok, why = verdict[head]
        lines += [f"## {head}: {'PASSES the release gate' if ok else 'does NOT pass'}", ""]
        lines += [f"- failed check: {w}" for w in why]
        if "skipped" not in m:
            lines.append(f"- {m['rows']:,} rows in {m['blocks']} areas. Borrowing from the other parts "
                         f"{'helped and is used' if m['uses_beliefs'] else 'did not help and is off'} "
                         f"(held-out score {m['cv_score_with_beliefs']} with, {m['cv_score_plain']} without; lower is better).")
            if isinstance(m["locked_test"], dict):
                lines += [f"- {k}: {v}" for k, v in m["locked_test"].items() if k != "error_when_sure"]
            else:
                lines.append(f"- {m['locked_test']}")
        lines.append("")
    lines.append("Labels are OpenStreetMap tags, published opening hours and logged bus and traffic feeds. They can be out of date, "
                 "so these scores say how well the model predicts those records, not what a person would find on the street tonight.")
    return "\n".join(lines)


def run_training(trials=TRIALS, budget=BUDGET_S, seeds=None, folder=None):
    """Trains a candidate and writes it to ml/out/unified/candidates/<time>/. Returns the run name."""
    folder = folder or DATA
    seeds = seeds or train.SEEDS
    run = time.strftime("%Y%m%d-%H%M%S")
    out = os.path.join(ROOT, "candidates", run)
    os.makedirs(os.path.join(out, "locked"), exist_ok=True)
    e, s = train.load(folder)
    old = read_metrics(current_dir()).get("heads", {})
    params = {h: m.get("params") for h, m in old.items() if m.get("params")}
    tables = {h: rows_for(h, e, s) for h in HEADS}
    s1 = stage1(tables, params)
    believe = crossfit(s1)
    heads, metrics = {}, {}
    for head in HEADS:
        d = add_beliefs(head, tables[head], s, believe)
        tables[head] = None  # free the memory before the next head
        saved, metrics[head] = fit_head(head, d, e, trials, budget, params.get(head), seeds)
        if saved is None:
            continue
        locked = saved.pop("locked", None)
        if locked is not None:
            locked.to_csv(os.path.join(out, "locked", f"{head}.csv.gz"), index=False)
        saved["beliefs"] = {g: (s1[g]["all"], s1[g]["cols"]) for g in s1 if g != head} if saved["uses_beliefs"] else {}
        heads[head] = saved
    full = {"heads": metrics, "data_sha256": data_hash(folder), "trials": trials, "seeds": seeds, "run": run}
    pickle.dump(StreetModel(heads, {"run": run}), open(os.path.join(out, "street_model.pkl"), "wb"))
    json.dump(full, open(os.path.join(out, "metrics.json"), "w"), indent=1, default=str)
    text = report(full, check(run))
    open(os.path.join(out, "report.md"), "w", encoding="utf-8").write(text)
    print(text)
    print(f"\nCandidate: {out}", flush=True)
    return run


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("command", choices=["train", "retrain", "check", "promote", "rollback", "status"])
    ap.add_argument("run", nargs="?")
    ap.add_argument("--trials", type=int, default=TRIALS)
    ap.add_argument("--budget", type=int, default=BUDGET_S, help="most seconds spent searching parameters for one part")
    ap.add_argument("--quick", action="store_true", help="2 trials and 1 seed: checks the script runs; never promote a quick run")
    ap.add_argument("--force", action="store_true", help="retrain even when the data has not changed")
    a = ap.parse_args(argv)
    trials, seeds = (2, 1) if a.quick else (a.trials, None)
    if a.command == "train":
        run_training(trials, a.budget, seeds)
    elif a.command == "retrain":
        if a.quick:
            sys.exit("A quick run is a smoke test and is never promoted. Use train --quick, or retrain without --quick.")
        if not a.force and read_metrics(current_dir()).get("data_sha256") == data_hash(DATA):
            print("No new data since the model in use was trained. Nothing to do (use --force to retrain anyway).")
            return
        promote(run_training(trials, a.budget, seeds))
    elif a.command in ("check", "promote"):
        if not a.run:
            sys.exit(f"Which run? e.g. python ml/unified.py {a.command} 20261112-220000")
        verdict = promote(a.run) if a.command == "promote" else check(a.run)
        for head, (ok, why) in verdict.items():
            print(f"{head}: {'pass' if ok else 'FAIL'}" + "".join(f"\n   {w}" for w in why))
    elif a.command == "rollback":
        rollback()
    else:
        m = read_metrics(current_dir())
        if not m:
            print("No model in use yet. Run: python ml/unified.py retrain")
        for head, x in m.get("heads", {}).items():
            t = x.get("locked_test")
            print(head, "|", {k: t[k] for k in ("error_rate", "auroc", "mae", "spearman") if k in t} if isinstance(t, dict) else t)


if __name__ == "__main__":
    import unified  # so the saved model is unified.StreetModel, loadable from other scripts (not __main__.StreetModel)
    unified.main()
