"""Women's felt-safety rater, the fallback version (Free to Be Delhi is gone, so there is no Delhi women's safe/unsafe
label to learn a judge from). It learns two things from real people and reports them separately; it never calls them a
safety score and it never picks routes (rules decide, AI estimates):

  W  women's share of people on a street at an hour, learned from women and men counted in real Delhi street images:
     StreetSight (Street View, daytime), Streetscope (wearable camera, 06-22 h, with GPS and time), women-count (phone
     photos, 10-19 h). Answers "are women out on streets like this at this hour?" (Safetipin's gender-usage parameter).
  L  how safe a street LOOKS to women, learned from SPECS: 400 street images scored "safe" by women (5 countries, none in
     India, daytime). Inputs are the image's scene make-up (share of road, footpath, wall, trees, people...). Applied to
     the Delhi street images that have the same scene make-up (NUS Global Streetscapes). Streets with no image get none.

Honesty rules, the same as ml/train.py: the fixed 20% of ~3 km blocks are locked and scored once (W); L is scored on a
city it never saw, each city in turn. Each part must beat a simple baseline with the whole 95% interval to be kept.

Usage (Windows: py instead of python), data in ml/data/incoming/<source>/ and ml/data/blocks/:
  python ml/rater.py labels     W label rows -> ml/out/rater/labels_w.csv (also prints counts per source)
  python ml/rater.py train      fit and test W and L -> ml/out/rater/{report.md, metrics.json, rater.pkl}
  python ml/rater.py score      L for Delhi streets -> ml/out/rater/delhi_looks_safe.csv (edge_id, score, images)
"""
import argparse
import glob
import json
import os
import pickle
import sys
from datetime import timedelta

import numpy as np
import pandas as pd
from scipy.spatial import cKDTree
from scipy.stats import spearmanr
from sklearn.linear_model import RidgeCV
from sklearn.model_selection import GroupKFold
from xgboost import XGBRegressor

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import train  # noqa: E402  stable helpers only: block_of, locked
from features import HIGHWAYS, NCR, STATIC  # noqa: E402

ML = os.path.dirname(os.path.abspath(__file__))
IN = os.path.join(ML, "data", "incoming")
BLOCKS = os.path.join(ML, "data", "blocks")
OUT = os.path.join(ML, "out", "rater")
SNAP_W_M = 30   # a counted image belongs to the nearest street within this distance, else it is dropped
SNAP_L_M = 25   # a Delhi image scores streets within this distance (the design's 25 m)
IST = timedelta(hours=5, minutes=30)
W_PARAMS = [{"max_depth": d, "learning_rate": lr, "n_estimators": 300, "min_child_weight": mcw, "reg_lambda": 1.0}
            for d in (2, 3, 4) for lr in (0.03, 0.08) for mcw in (20, 50)]  # min_child_weight counts people, not images
W_COLS = [c for c in STATIC if c not in ("incident_hits", "report_points")] + ["hour", "weekend"]  # app-only, empty here
# ~2,600 noisy counts can't support 100 inputs, so a short list of the inputs the design names is tried too; the
# cross-validation (never the locked blocks) picks between them
W_FEW = ["highway", "sidewalk", "lanes", "places_total", "places_shop", "places_food", "places_500", "d_transit",
         "transit_500", "d_main", "main_share_300", "bld_500", "zone_industrial", "zone_park", "hour", "weekend"]


def _find(sub, pattern):
    hits = sorted(glob.glob(os.path.join(IN, sub, "**", pattern), recursive=True))
    return hits


# ---------------------------------------------------------------- W labels

def streetsight():
    """Delhi Street View images, two annotators on some: their counts are averaged. No hour (daytime imagery)."""
    paths = _find("streetsight", "combined_annotations*.csv")
    if not paths:
        return pd.DataFrame()
    d = pd.read_csv(paths[0])
    d = d[d["city"] == "Delhi"]
    for c in ("women_count_num", "men_count_num"):
        d[c] = pd.to_numeric(d[c], errors="coerce").fillna(0)  # an empty count is "nobody of that sex in view"
    g = d.groupby("task_id").agg(lat=("lat", "first"), lon=("lon", "first"), women=("women_count_num", "mean"),
                                 men=("men_count_num", "mean")).reset_index()
    return pd.DataFrame({"source": "streetsight", "id": g["task_id"].astype(str), "lat": g["lat"], "lon": g["lon"],
                         "hour": np.nan, "weekend": np.nan, "women": g["women"], "people": g["women"] + g["men"]})


def women_count():
    """Phone photos, about 3 MTurk workers each: median people and median men, women = people - men (never below 0)."""
    paths = _find("women_count", "*batch*results*.csv")
    if not paths:
        return pd.DataFrame()
    d = pd.read_csv(paths[0], low_memory=False)
    for c in ("Answer.total_people", "Answer.total_men"):
        d[c] = pd.to_numeric(d[c], errors="coerce")
    d = d.dropna(subset=["Answer.total_people", "Answer.total_men"])
    g = d.groupby("Input.file_id").agg(lat=("Input.exif_lat", "first"), lon=("Input.exif_long", "first"),
                                       lat0=("Input.lat", "first"), lon0=("Input.long", "first"),
                                       date=("Input.exif_date", "first"), time=("Input.exif_time", "first"),
                                       people=("Answer.total_people", "median"), men=("Answer.total_men", "median")).reset_index()
    lat = pd.to_numeric(g["lat"], errors="coerce").fillna(pd.to_numeric(g["lat0"], errors="coerce"))
    lon = pd.to_numeric(g["lon"], errors="coerce").fillna(pd.to_numeric(g["lon0"], errors="coerce"))
    when = pd.to_datetime(g["date"].astype(str) + " " + g["time"].astype(str), errors="coerce")  # phone clock, IST
    people = g["people"].clip(lower=0)
    women = (people - g["men"]).clip(lower=0, upper=people)
    return pd.DataFrame({"source": "women_count", "id": g["Input.file_id"].astype(str), "lat": lat, "lon": lon,
                         "hour": when.dt.hour, "weekend": (when.dt.dayofweek >= 5).astype(float).where(when.notna()),
                         "women": women, "people": people})


def _ls_count(result, name):
    for r in result:
        if r.get("from_name") == name:
            try:
                return float(r["value"]["taxonomy"][0][0])
            except (KeyError, IndexError, TypeError, ValueError):
                return np.nan
    return 0.0  # not ticked: nobody of that sex in view


def streetscope():
    """Wearable-camera frames in Delhi. A frame is placed by its video's GPS track at gps_first_datetime + its second;
    GPS times are UTC, so the hour is shifted to IST. Frames more than 5 s from any GPS fix are dropped."""
    tasks = _find("streetscope", "*clean*.json")
    meta, vids = _find("streetscope", "frame_metadata.csv"), _find("streetscope", "video_metadata.parquet")
    gps = [p for p in _find("streetscope", "*.parquet") if p not in vids and "analysis_data" not in os.path.basename(p)]
    if not (tasks and meta and vids and gps):
        return pd.DataFrame()
    rows = {}
    for p in sorted(tasks, key=os.path.getsize, reverse=True):  # the bigger export first; an older one only adds new tasks
        for t in json.load(open(p, encoding="utf-8")):
            frame = os.path.basename(t.get("data", {}).get("image", ""))
            ann = [a for a in t.get("annotations", []) if not a.get("was_cancelled")]
            if not frame or not ann or frame in rows:
                continue
            w = np.nanmean([_ls_count(a["result"], "women_count") for a in ann])
            m = np.nanmean([_ls_count(a["result"], "men_count") for a in ann])
            rows[frame] = (w, m)
    lab = pd.DataFrame([(f, w, m) for f, (w, m) in rows.items()], columns=["frame_filename", "women", "men"])
    fm = pd.read_csv(meta[0])
    vm = pd.read_parquet(vids[0])
    g = pd.read_parquet(gps[0], columns=["video_id", "gps_datetime", "lat", "lon"])
    d = lab.merge(fm[["frame_filename", "video_id", "timestamp_sec"]], on="frame_filename")
    d = d.merge(vm[["base_video_id", "video_id", "gps_first_datetime"]].rename(columns={"video_id": "gps_video", "base_video_id": "video_id"}), on="video_id")
    d["t"] = pd.to_datetime(d["gps_first_datetime"]) + pd.to_timedelta(d["timestamp_sec"], unit="s")
    g["gps_datetime"] = pd.to_datetime(g["gps_datetime"])
    out = []
    for vid, part in d.groupby("gps_video"):
        track = g[g["video_id"] == vid].sort_values("gps_datetime")
        if track.empty:
            continue
        idx = np.searchsorted(track["gps_datetime"].values, part["t"].values).clip(0, len(track) - 1)
        prev = (idx - 1).clip(0)
        tv = track["gps_datetime"].values
        near = np.where(np.abs(tv[prev] - part["t"].values) < np.abs(tv[idx] - part["t"].values), prev, idx)
        gap = np.abs(tv[near] - part["t"].values) / np.timedelta64(1, "s")
        p = part.assign(lat=track["lat"].values[near], lon=track["lon"].values[near], gap=gap)
        out.append(p[p["gap"] <= 5])
    if not out:
        return pd.DataFrame()
    d = pd.concat(out)
    local = d["t"] + IST
    w, m = d["women"].fillna(0), d["men"].fillna(0)
    return pd.DataFrame({"source": "streetscope", "id": d["frame_filename"], "lat": d["lat"], "lon": d["lon"],
                         "hour": local.dt.hour, "weekend": (local.dt.dayofweek >= 5).astype(float),
                         "women": w, "people": w + m})


def labels():
    parts = [f() for f in (streetsight, women_count, streetscope)]
    d = pd.concat([p for p in parts if len(p)], ignore_index=True)
    d = d[(d["people"] > 0) & in_ncr(d)]
    return d.reset_index(drop=True)


def in_ncr(d):
    return d["lat"].between(NCR[0], NCR[2]) & d["lon"].between(NCR[1], NCR[3])


# ---------------------------------------------------------------- snapping to streets

def _xy(lat, lon, lat0=28.6):
    """Metres on a flat local grid (fine within NCR's 80 km)."""
    return np.c_[np.asarray(lon) * 111_320 * np.cos(np.radians(lat0)), np.asarray(lat) * 110_540]


def snap(points, max_m, cols=None):
    """Nearest street (edge midpoint) within max_m for each point, with that street's columns. Two passes over the
    block files so only the matched streets' full rows are held in memory."""
    files = sorted(glob.glob(os.path.join(BLOCKS, "*.edges.csv.gz")))
    if not files:
        sys.exit("No street blocks in ml/data/blocks (run py ml/dataset.py first)")
    ll = pd.concat([pd.read_csv(f, usecols=["edge_id", "lat", "lon"]).assign(file=n) for n, f in enumerate(files)],
                   ignore_index=True).drop_duplicates("edge_id")
    dist, i = cKDTree(_xy(ll["lat"], ll["lon"])).query(_xy(points["lat"], points["lon"]), distance_upper_bound=max_m)
    ok = np.isfinite(dist)
    hit = ll.iloc[i[ok]]
    want = set(hit["edge_id"])
    if not want:
        return points.iloc[:0].assign(edge_id=pd.Series(dtype=object), snap_m=pd.Series(dtype=float))
    full = [pd.read_csv(files[n], usecols=cols) for n in sorted(set(hit["file"]))]
    full = pd.concat([f[f["edge_id"].isin(want)] for f in full], ignore_index=True).drop_duplicates("edge_id")
    p = points[ok].copy()
    p["edge_id"], p["snap_m"] = hit["edge_id"].to_numpy(), dist[ok]
    return p.merge(full, on="edge_id", how="left", suffixes=("", "_edge"))


# ---------------------------------------------------------------- W: women's share

def _w_matrix(d, cols=W_COLS):
    X = d.reindex(columns=cols).copy()
    if X["highway"].dtype == object:  # blocks store a code already; test or older rows may hold the OSM name
        X["highway"] = X["highway"].map({h: i for i, h in enumerate(HIGHWAYS)})
    return X.apply(pd.to_numeric, errors="coerce").astype(float)


def _w_fit(X, y, w, params, seed=0):
    """Logistic loss on the women's share, each image weighted by the people in it (a binomial proportion).
    XGBoost, not sklearn: sklearn's weighted binning takes ~16 s per fit here, XGBoost 0.4 s."""
    keep = [c for c in X.columns if X[c].nunique(dropna=True) > 1]
    m = XGBRegressor(objective="binary:logistic", tree_method="hist", n_jobs=4, random_state=seed, subsample=0.8, **params)
    return keep, m.fit(X[keep], y, sample_weight=w)


def _w_bag(X, y, w, params):
    return [_w_fit(X, y, w, params, s) for s in range(5)]


def _w_pred(bag, X):
    return np.clip(np.mean([m.predict(X[k]) for k, m in bag], axis=0), 0, 1)


def wci(stat, w, groups, reps=1000):
    """95% interval of a people-weighted mean, resampling whole blocks."""
    codes, n = pd.factorize(pd.Series(groups))[0], len(set(groups))
    num, den = np.bincount(codes, stat * w, n), np.bincount(codes, w, n)
    pick = np.random.default_rng(1).integers(0, n, (reps, n))
    boot = num[pick].sum(1) / den[pick].sum(1)
    return [round(float(np.percentile(boot, 2.5)), 4), round(float(np.percentile(boot, 97.5)), 4)]


def _base_road(tr, te):
    """The baseline W must beat: people-weighted women's share per road type, learned on the training rows."""
    share = (tr["women"].groupby(tr["highway"]).sum() / tr["people"].groupby(tr["highway"]).sum())
    return te["highway"].map(share).fillna(tr["women"].sum() / tr["people"].sum()).to_numpy()


def _w_scores(y, p, base, w, groups, overall=None):
    err, berr = np.abs(y - p), np.abs(y - base)
    extra = {} if overall is None else {"overall_mean_mae": round(float(np.average(np.abs(y - overall), weights=w)), 4)}
    return {**extra, "rows": int(len(y)), "people": int(w.sum()), "blocks": int(len(set(groups))),
            "mae": round(float(np.average(err, weights=w)), 4), "baseline_mae (share per road type)": round(float(np.average(berr, weights=w)), 4),
            "gain_ci95": wci(berr - err, w, groups),
            "spearman": round(float(spearmanr(y, p).statistic), 3) if len(y) > 2 else None,
            "baseline_spearman": round(float(spearmanr(y, base).statistic), 3) if len(y) > 2 else None}


def train_w(d):
    d = d.copy()
    d["block"] = d["tile"].map(train.block_of)
    lock = train.locked(d["block"])
    y, w = (d["women"] / d["people"]).to_numpy(), d["people"].to_numpy(float)
    tr = ~lock
    groups = d["block"].to_numpy()
    cv = list(GroupKFold(n_splits=min(5, len(set(groups[tr])))).split(d[tr], y[tr], groups[tr]))
    best = None
    for name, cols in (("all", W_COLS), ("few", W_FEW)):
        Xc = _w_matrix(d, cols)
        for params in W_PARAMS:
            oof = np.zeros(tr.sum())
            for a, b in cv:
                k, m = _w_fit(Xc[tr].iloc[a], y[tr][a], w[tr][a], params)
                oof[b] = np.clip(m.predict(Xc[tr].iloc[b][k]), 0, 1)
            s = float(np.average(np.abs(y[tr] - oof), weights=w[tr]))
            if best is None or s < best[0]:
                best = (s, params, name, cols)
    params, cols = best[1], best[3]
    X = _w_matrix(d, cols)
    bag = _w_bag(X[tr], y[tr], w[tr], params)
    p = _w_pred(bag, X[lock])
    base = _base_road(d[tr], d[lock])
    m = {"params": params, "inputs": best[2], "cols": cols, "cv_mae": round(best[0], 4), "locked": _w_scores(y[lock], p, base, w[lock], groups[lock], overall=np.average(y[tr], weights=w[tr]))}
    m["passes"] = bool(m["locked"]["gain_ci95"][0] > 0)
    m["leave_one_source_out"] = {}
    for src in sorted(d["source"].unique()):
        out = (d["source"] == src).to_numpy()
        if out.all() or not out.any():
            continue
        b2 = _w_bag(X[~out], y[~out], w[~out], params)
        m["leave_one_source_out"][src] = _w_scores(y[out], _w_pred(b2, X[out]), _base_road(d[~out], d[out]), w[out], groups[out])
    hours = d[d["hour"].notna()]
    m["share_by_hour (measured)"] = {int(h): round(float(g["women"].sum() / g["people"].sum()), 3)
                                     for h, g in hours.groupby("hour") if g["people"].sum() >= 30}
    name = lambda h: HIGHWAYS[int(h)] if h == h and int(h) < len(HIGHWAYS) else "other"  # noqa: E731
    m["share_by_road (measured)"] = {name(h): [round(float(g["women"].sum() / g["people"].sum()), 3), int(g["people"].sum())]
                                     for h, g in d.groupby("highway")}
    final = _w_bag(X, y, w, params) if m["passes"] else None  # shipped model learns from every row once it has passed
    return m, final


# ---------------------------------------------------------------- L: looks safe to women

SEG_DROP = {"uuid", "source", "orig_id", "Unnamed: 0"}


def _shares(seg):
    cols = [c for c in seg.columns if c not in SEG_DROP]
    v = seg[cols].apply(pd.to_numeric, errors="coerce").fillna(0)
    return v.div(v.sum(axis=1).replace(0, np.nan), axis=0)


def specs():
    q = pd.read_csv(_find("specs_perception", "gender_qscores.csv")[0])
    meta = pd.read_csv(_find("specs_perception", "metadata.csv")[0])
    q = q[(q["country"] == "All") & (q["Question"] == "safe")]
    s = q.pivot_table(index="Image", columns="gender", values="Score")
    s = s.merge(meta[["Image number", "uuid", "city"]], left_index=True, right_on="Image number")
    segs = [pd.read_csv(p) for p in _find("specs_perception", "*segmentation*.csv") + _find("streetscapes", "segmentation.csv")]
    seg = pd.concat(segs, ignore_index=True).drop_duplicates("uuid")
    seg = pd.concat([seg[["uuid"]], _shares(seg)], axis=1)
    return s.merge(seg, on="uuid"), [c for c in seg.columns if c != "uuid"]


def _ridge():
    return RidgeCV(alphas=np.logspace(-3, 3, 13))


def train_l():
    d, cols = specs()
    d = d.dropna(subset=["female"])
    X, y = d[cols].fillna(0).to_numpy(), d["female"].to_numpy()
    pp = pd.read_csv(_find("streetscapes", "perception.csv")[0])[["uuid", "Safe"]] if _find("streetscapes", "perception.csv") else None
    p, base = np.zeros(len(y)), np.zeros(len(y))
    per = {}
    for city in sorted(d["city"].unique()):
        te = (d["city"] == city).to_numpy()
        p[te] = _ridge().fit(X[~te], y[~te]).predict(X[te])
        base[te] = y[~te].mean()
        per[city] = {"images": int(te.sum()), "spearman": round(float(spearmanr(y[te], p[te]).statistic), 3),
                     "mae": round(float(np.abs(y[te] - p[te]).mean()), 3), "baseline_mae (average)": round(float(np.abs(y[te] - base[te]).mean()), 3)}
    err, berr = np.abs(y - p), np.abs(y - base)
    groups = d["city"].to_numpy()
    m = {"images": int(len(y)), "cities": per,
         "pooled_spearman": round(float(spearmanr(y, p).statistic), 3),
         "mae": round(float(err.mean()), 3), "baseline_mae (average)": round(float(berr.mean()), 3),
         "gain_ci95": wci(berr - err, np.ones(len(y)), groups, reps=2000)}
    # bootstrap images (not cities: 5 cities is too few to resample) for the correlation's interval
    rng = np.random.default_rng(2)
    boots = [spearmanr(y[i], p[i]).statistic for i in (rng.integers(0, len(y), len(y)) for _ in range(1000))]
    m["pooled_spearman_ci95"] = [round(float(np.percentile(boots, 2.5)), 3), round(float(np.percentile(boots, 97.5)), 3)]
    if pp is not None:
        j = d[["uuid"]].merge(pp, on="uuid", how="left")["Safe"].to_numpy()
        ok = ~np.isnan(j)
        if ok.sum() > 10:
            m["place_pulse_model_spearman (same images)"] = round(float(spearmanr(y[ok], j[ok]).statistic), 3)
            m["our_spearman_on_those_images"] = round(float(spearmanr(y[ok], p[ok]).statistic), 3)
    m["passes"] = bool(m["gain_ci95"][0] > 0 and m["pooled_spearman_ci95"][0] > 0)
    final = None
    if m["passes"]:
        model = _ridge().fit(X, y)
        gap = _ridge().fit(X[d["male"].notna()], (d["female"] - d["male"]).dropna().to_numpy()) if "male" in d else None
        final = {"cols": cols, "model": model, "gap": gap, "train_scores": np.sort(y)}
        m["top_weights"] = dict(sorted(zip(cols, map(float, model.coef_)), key=lambda t: -abs(t[1]))[:10])
    return m, final


def looks_safe(final, seg):
    """0-100: where an image's predicted women's score falls among the 400 SPECS images' real scores."""
    X = _shares(seg).reindex(columns=final["cols"]).fillna(0).to_numpy()
    raw = final["model"].predict(X)
    return 100 * np.searchsorted(final["train_scores"], raw) / len(final["train_scores"])


# ---------------------------------------------------------------- commands

def cmd_labels():
    d = labels()
    os.makedirs(OUT, exist_ok=True)
    d.to_csv(os.path.join(OUT, "labels_w.csv"), index=False)
    for src, g in d.groupby("source"):
        h = g["hour"].dropna()
        print(f"{src}: {len(g)} images, {int(g['people'].sum())} people, women {g['women'].sum() / g['people'].sum():.3f}"
              + (f", hours {int(h.min())}-{int(h.max())}" if len(h) else ", no hour"))
    return d


def report(m):
    w, l = m.get("W"), m.get("L")
    out = ["# Women's felt-safety rater: test results", "",
           "Fallback design (no Delhi women's safe/unsafe label exists): two measured estimates, never one 'safety score'.", ""]
    if w:
        k = w["locked"]
        out += ["## W: women's share of people on the street", "",
                f"Labels: {w['rows']} counted images within {SNAP_W_M} m of a street ({w['snapped_share']:.0%} of {w['label_rows']}), {w['people']} people.",
                f"Locked blocks (never used for any choice): {k['rows']} images, {k['blocks']} blocks.", "",
                "| | model | baseline: share per road type |", "|---|---|---|",
                f"| people-weighted error | {k['mae']} | {k['baseline_mae (share per road type)']} |",
                f"| Spearman | {k['spearman']} | {k['baseline_spearman']} |", "",
                f"Inputs chosen by cross-validation: {w['inputs']}. One overall average would score {k.get('overall_mean_mae')} (does road type matter at all?).", "",
                f"Gain over baseline, 95% interval: {k['gain_ci95']}. **{'PASSES' if w['passes'] else 'FAILS'}** (needs the whole interval above 0).", "",
                "Leave one source out (train on the others, test on it):", "",
                "| source | rows | error | baseline | gain 95% |", "|---|---|---|---|---|"]
        out += [f"| {s} | {v['rows']} | {v['mae']} | {v['baseline_mae (share per road type)']} | {v['gain_ci95']} |" for s, v in w["leave_one_source_out"].items()]
        out += ["", f"Measured women's share by hour (hours with 30+ people): {w['share_by_hour (measured)']}", ""]
    if l:
        out += ["## L: looks safe to women (SPECS)", "",
                f"{l['images']} images, each city scored by a model that never saw it.", "",
                "| city | images | Spearman | error | baseline error |", "|---|---|---|---|---|"]
        out += [f"| {c} | {v['images']} | {v['spearman']} | {v['mae']} | {v['baseline_mae (average)']} |" for c, v in l["cities"].items()]
        out += ["", f"Pooled Spearman {l['pooled_spearman']} (95% {l['pooled_spearman_ci95']}); error {l['mae']} vs {l['baseline_mae (average)']}, "
                f"gain 95% {l['gain_ci95']}. **{'PASSES' if l['passes'] else 'FAILS'}**."]
        if "place_pulse_model_spearman (same images)" in l:
            out += [f"Existing Place Pulse 'Safe' prediction on the same images: Spearman {l['place_pulse_model_spearman (same images)']} (ours {l['our_spearman_on_those_images']})."]
        out += ["", "Limits: no Indian and no night images; applies only to streets with a street image nearby."]
    return "\n".join(out) + "\n"


def cmd_train():
    os.makedirs(OUT, exist_ok=True)
    m, models = {}, {}
    lab = labels()
    cols = ["edge_id", "tile", *[c for c in STATIC if c not in ("incident_hits", "report_points")]]
    d = snap(lab, SNAP_W_M, cols)
    m["W"], models["W"] = train_w(d)
    m["W"].update(rows=int(len(d)), label_rows=int(len(lab)), snapped_share=len(d) / max(len(lab), 1), people=int(d["people"].sum()))
    m["L"], models["L"] = train_l()
    json.dump(m, open(os.path.join(OUT, "metrics.json"), "w"), indent=1, default=str)
    open(os.path.join(OUT, "report.md"), "w", encoding="utf-8").write(report(m))
    pickle.dump({"W": models["W"], "W_cols": m["W"]["cols"], "L": models["L"]}, open(os.path.join(OUT, "rater.pkl"), "wb"))
    print(report(m))


def cmd_score():
    r = pickle.load(open(os.path.join(OUT, "rater.pkl"), "rb"))
    if not r["L"]:
        sys.exit("L did not pass its test, so no street gets a looks-safe score")
    img = pd.read_csv(_find("streetscapes", "images.csv")[0], usecols=["uuid", "lat", "lon"])
    seg = pd.read_csv(_find("streetscapes", "segmentation.csv")[0]).merge(img, on="uuid")
    seg = seg[in_ncr(seg)].reset_index(drop=True)
    seg["score"] = looks_safe(r["L"], seg)
    s = snap(seg[["uuid", "lat", "lon", "score"]], SNAP_L_M, ["edge_id"])
    out = s.groupby("edge_id")["score"].agg(["median", "size"]).round(1).reset_index()
    out.columns = ["edge_id", "looks_safe", "images"]
    out.to_csv(os.path.join(OUT, "delhi_looks_safe.csv"), index=False)
    print(f"{len(seg)} NCR images, {len(s)} within {SNAP_L_M} m of a street, {len(out)} streets scored")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", choices=["labels", "train", "score"])
    {"labels": cmd_labels, "train": cmd_train, "score": cmd_score}[ap.parse_args().command]()
