"""SafeRoute model scorecard: the way AI models are rated (held-out benchmark, baselines, calibration, slices,
behavioural tests, human agreement, efficiency), adapted to "which walking route is safer, day and night".

Every number comes from data the model never trained on:
  street heads  ml/out/locked/<head>.csv.gz (locked 20% of blocks, written by ml/train.py), else ml/out/metrics.json
  route model   grouped cross-validation over whole ~3 km areas, re-run here with ml/rank_v2.py's own code

Grades: A excellent, B good, C usable with care, D weak, F fails, I = not enough evidence to grade (too few samples).
The thresholds are in GRADES below and printed in the report. Nothing here tunes or changes a model.

Usage: py ml/scorecard.py [--out ml/out/scorecard.md]     writes scorecard.md and scorecard.json
"""
import json
import os
import pickle
import sys
import time

import numpy as np
import pandas as pd

ML = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(ML, "out")
sys.path.insert(0, ML)
sys.path.insert(0, os.path.dirname(ML))
if "--qwen-taught" not in sys.argv:
    sys.argv.append("--qwen-taught")  # score the route model the app uses (rank_v2 --qwen-taught)

NIGHT = set(range(0, 6)) | set(range(19, 24))
MIN_N = 30  # fewer samples than this: grade I

# (higher_is_better, [A, B, C, D] cut-offs); anything worse than D is F
GRADES = {
    "auroc": (True, [0.90, 0.85, 0.75, 0.65]),                 # ranking lit vs dark streets (plan bar 0.85)
    "ece": (False, [0.03, 0.05, 0.10, 0.15]),                  # stated chance vs what happens (plan bar 0.05)
    "error_vs_baseline": (True, [0.40, 0.25, 0.10, 0.0]),      # share of the dumb baseline's mistakes removed
    "false_safe": (False, [0.02, 0.05, 0.10, 0.20]),           # dark streets called lit with >= 80% confidence
    "spearman": (True, [0.75, 0.60, 0.45, 0.30]),              # ordering of hourly values
    "worst_slice_gap": (False, [0.02, 0.05, 0.10, 0.20]),      # accuracy lost on the hardest slice
    "agreement": (True, [0.90, 0.85, 0.75, 0.65]),             # route picks matching a judge on unseen areas
    "behaviour": (True, [1.0, 0.95, 0.85, 0.70]),              # share of must-pass behaviour tests passed
    "people_agreement": (True, [0.90, 0.80, 0.70, 0.60]),      # route picks matching real people
    "coverage": (True, [0.50, 0.20, 0.05, 0.01]),              # share of streets with a real measurement behind them
}


def grade(metric, value, n=None):
    if value is None or (n is not None and n < MIN_N):
        return "I"
    hi, cuts = GRADES[metric]
    for letter, c in zip("ABCD", cuts):
        if (value >= c) if hi else (value <= c):
            return letter
    return "F"


def ece(y, p, bins=10):
    idx = np.minimum((p * bins).astype(int), bins - 1)
    return float(sum(abs(y[idx == b].mean() - p[idx == b].mean()) * (idx == b).mean() for b in range(bins) if (idx == b).any()))


def auroc(y, p):
    from sklearn.metrics import roc_auc_score
    return float(roc_auc_score(y, p)) if len(set(y)) == 2 else None


# ------------------------------------------------------------------ street heads

def lit_card(metrics):
    path = os.path.join(OUT, "locked", "lit.csv.gz")
    m = metrics.get("lit", {})
    t = m.get("locked_test") if isinstance(m.get("locked_test"), dict) else {}
    card = {"source": "locked test (never trained on)", "rows": t.get("rows")}
    if os.path.exists(path):
        d = pd.read_csv(path)
        y, p = d["y"].to_numpy(int), d["p"].to_numpy(float)
        err, base_err = float(((p >= 0.5) != y).mean()), float(((d["baseline"] >= 0.5) != y).mean())
        dark = y == 0
        card.update(rows=len(d), blocks=int(d["block"].nunique()), auroc=auroc(y, p), ece=ece(y, p), error=err, baseline_error=base_err,
                    false_safe=float((p[dark] >= 0.8).mean()) if dark.any() else None, dark_streets=int(dark.sum()),
                    missed_dark=float((p[dark] >= 0.5).mean()) if dark.any() else None)
        sl = {}
        for h, g in d.groupby("highway"):
            if len(g) >= MIN_N:
                sl[str(h)] = {"n": len(g), "error": round(float(((g["p"] >= 0.5) != g["y"]).mean()), 3)}
        card["by_road_type"] = sl
        card["worst_slice_gap"] = max([v["error"] for v in sl.values()], default=err) - err
    else:
        card.update(auroc=t.get("auroc"), ece=t.get("ece"), error=t.get("error_rate"),
                    baseline_error=t.get("baseline_error_rate (road type only)"), false_safe=None,
                    note="slice scores need ml/out/locked/lit.csv.gz (re-run ml/train.py --heads lit)")
    be = card.get("baseline_error")
    card["error_vs_baseline"] = (be - card["error"]) / be if be and card.get("error") is not None else None
    card["grades"] = {k: grade(k, card.get(k), card.get("rows")) for k in ("auroc", "ece", "error_vs_baseline", "false_safe", "worst_slice_gap")}
    card["plan_bar (AUROC>=0.85 and ECE<=0.05)"] = bool(card.get("auroc") and card["auroc"] >= 0.85 and card.get("ece") is not None and card["ece"] <= 0.05)
    return card


def hourly_card(name, metrics):
    m = metrics.get(name, {})
    t = m.get("locked_test") if isinstance(m.get("locked_test"), dict) else {}
    card = {"rows": t.get("rows"), "mae": t.get("mae"), "baseline_mae": t.get("baseline_mae (mean per hour)"), "spearman": t.get("spearman"),
            "beats_baseline (whole 95% interval better)": m.get("beats_baseline")}
    path = os.path.join(OUT, "locked", f"{name}.csv.gz")
    if os.path.exists(path):
        d = pd.read_csv(path)
        d["night"] = d["hour"].astype(int).isin(NIGHT)
        sl = {}
        for nm, g in d.groupby("night"):
            e, b = (g["y"] - g["p"]).abs().mean(), (g["y"] - g["baseline"]).abs().mean()
            sl["night" if nm else "day"] = {"n": len(g), "mae": round(float(e), 4), "baseline_mae": round(float(b), 4),
                                             "beats_baseline": bool(e < b)}
        card["day_vs_night"] = sl
    bm, mae = card.get("baseline_mae"), card.get("mae")
    card["error_vs_baseline"] = (bm - mae) / bm if bm and mae is not None else None
    card["grades"] = {"error_vs_baseline": grade("error_vs_baseline", card["error_vs_baseline"], card["rows"]),
                      "spearman": grade("spearman", card["spearman"], card["rows"])}
    return card


# ------------------------------------------------------------------ route model

def route_card():
    import rank_v2 as R
    import pairs
    from sklearn.model_selection import GroupKFold
    pl = {p["id"]: p for p in pairs.load()}
    lab, info, human, qwen, claude = R.labels()
    lab = {i: v for i, v in lab.items() if i in pl}
    rows = [pl[i] for i in lab]
    X = np.array([R.vec(p) for p in rows]); dark = np.array([p["daylight"] < 0.5 for p in rows])
    y = np.array([lab[p["id"]][0] == "A" for p in rows], float); wt = np.array([lab[p["id"]][1] for p in rows])
    src = np.array([lab[p["id"]][2] for p in rows]); g = np.array([p["block"] for p in rows]); ids = np.array([p["id"] for p in rows])
    oof = np.zeros(len(y))
    for tr, te in GroupKFold(min(5, len(set(g)))).split(X, y, g):
        oof[te] = R.margin(R.fit(X[tr], dark[tr], y[tr], wt[tr]), X[te], dark[te])
    hit = (oof > 0) == (y == 1)
    card = {"pairs": len(y), "areas": int(len(set(g))), "label_sources": {k: int((src == k).sum()) for k in set(src)}}
    card["agreement_all_labels"] = float(hit.mean())
    card["day"] = {"n": int((~dark).sum()), "agreement": float(hit[~dark].mean()) if (~dark).any() else None}
    card["night"] = {"n": int(dark.sum()), "agreement": float(hit[dark].mean()) if dark.any() else None}
    k = R.KEYS.index("extra_length")
    card["baseline_shorter_route"] = float(((X[:, k] < 0) == (y == 1)).mean())
    hand = np.array([claude.get(i, ("", ""))[1] == "claude_hand" for i in ids])
    card["vs_claude_hand"] = {"n": int(hand.sum()), "agreement": float(hit[hand].mean()) if hand.any() else None}
    ppl = np.array([i in human for i in ids])
    card["vs_people"] = {"n": int(ppl.sum()), "agreement": float(hit[ppl].mean()) if ppl.any() else None}
    rubric = src == "claude_only"
    card["share_of_labels_from_written_rules"] = float(np.mean([claude.get(i, ("", ""))[1] == "claude_rubric" for i in ids]))
    by_area = pd.DataFrame({"a": g, "h": hit}).groupby("a")["h"].agg(["mean", "size"])
    big = by_area[by_area["size"] >= 20]
    card["worst_area"] = {"agreement": float(big["mean"].min()), "n": int(big.loc[big["mean"].idxmin(), "size"])} if len(big) else None
    card["worst_slice_gap"] = card["agreement_all_labels"] - min(x for x in [card["day"]["agreement"], card["night"]["agreement"],
                                                                              card["worst_area"] and card["worst_area"]["agreement"]] if x is not None)
    p = 1 / (1 + np.exp(-oof))
    card["ece"] = ece(y, p)
    # behaviour tests on the model fitted on everything (the one the app loads)
    w = R.fit(X, dark, y, wt)
    tests = behaviour_tests(R, w)
    card["behaviour_tests"] = tests
    card["behaviour"] = float(np.mean([t["pass"] for t in tests.values()]))
    rng = np.random.default_rng(0)
    sv = {"features": R.KEYS, "day": dict(zip(R.KEYS, w[:len(R.KEYS)])), "night": dict(zip(R.KEYS, w[len(R.KEYS):]))}
    sym = []
    for j in rng.choice(len(rows), min(200, len(rows)), replace=False):
        a, b, dl = rows[j]["A"], rows[j]["B"], rows[j]["daylight"]
        sym.append(abs(R.score(sv, a, b, dl) + R.score(sv, b, a, dl)) < 1e-9)
    card["swap_consistency (A vs B == -(B vs A))"] = float(np.mean(sym))
    t0 = time.perf_counter()
    for j in range(min(1000, len(rows))):
        R.score(sv, rows[j]["A"], rows[j]["B"], rows[j]["daylight"])
    card["ms_per_route_pair"] = round((time.perf_counter() - t0) * 1000 / min(1000, len(rows)), 3)
    card["grades"] = {"agreement": grade("agreement", card["agreement_all_labels"], card["pairs"]),
                      "vs_claude_hand": grade("agreement", card["vs_claude_hand"]["agreement"], card["vs_claude_hand"]["n"]),
                      "people_agreement": grade("people_agreement", card["vs_people"]["agreement"], card["vs_people"]["n"]),
                      "night": grade("agreement", card["night"]["agreement"], card["night"]["n"]),
                      "worst_slice_gap": grade("worst_slice_gap", card["worst_slice_gap"], card["pairs"]),
                      "ece": grade("ece", card["ece"], card["pairs"]),
                      "behaviour": grade("behaviour", card["behaviour"], len(tests) * 10)}
    return card


def behaviour_tests(R, w):
    """CheckList-style must-pass tests: change one thing a safety expert agrees on, the score must move the right way."""
    import pairs
    sv = {"features": R.KEYS, "day": dict(zip(R.KEYS, w[:len(R.KEYS)])), "night": dict(zip(R.KEYS, w[len(R.KEYS):]))}
    base = {k: 0.0 for k in pairs.FEATURES} | {"length_m": 2500.0, "police_m": 1000.0, "lit_pct": 40, "main_pct": 40}
    one = {"more lit": ({"lit_pct": 90}, {}), "more open places": ({"open_now": 6}, {}), "police closer": ({"police_m": 200}, {"police_m": 2500}),
           "less isolated": ({}, {"isolated_m": 900}), "CCTV": ({"cctv": 4}, {}), "shorter": ({}, {"length_m": 4000}),
           "less time with nothing open": ({}, {"nothing_open_m": 1200}), "smaller gap to help": ({}, {"help_gap_m": 1200}),
           "more main road": ({"main_pct": 85}, {}), "less dark": ({}, {"dark_pct": 60})}
    out = {}
    for nm, (a, b) in one.items():
        for when, dl in (("day", 1.0), ("night", 0.0)):
            s = R.score(sv, base | a, base | b, dl)
            out[f"{nm} ({when})"] = {"score": round(s, 3), "pass": s > 0}
    # use-case tests: what a person walking at night expects
    lit_long = base | {"lit_pct": 95, "main_pct": 85, "open_now": 4, "length_m": 2750}
    dark_short = base | {"lit_pct": 20, "dark_pct": 60, "main_pct": 10, "isolated_m": 900, "nothing_open_m": 1200, "length_m": 2500}
    s = R.score(sv, lit_long, dark_short, 0.0)
    out["night: 10% longer but lit, busy, main roads beats short dark isolated"] = {"score": round(s, 3), "pass": s > 0}
    s_day, s_night = R.score(sv, base | {"lit_pct": 90}, base, 1.0), R.score(sv, base | {"lit_pct": 90}, base, 0.0)
    out["lighting matters more at night than by day"] = {"score": round(s_night - s_day, 3), "pass": s_night > s_day}
    far = base | {"length_m": 7500, "lit_pct": 100, "main_pct": 100}
    s = R.score(sv, far, base, 1.0)
    out["day: a 3x longer route does not win only because it is lit"] = {"score": round(s, 3), "pass": s < 0}
    return out


# ------------------------------------------------------------------ efficiency and coverage

def efficiency():
    res = {}
    for f in sorted(os.listdir(os.path.join(OUT, "models"))) if os.path.isdir(os.path.join(OUT, "models")) else []:
        res[f] = {"mb": round(os.path.getsize(os.path.join(OUT, "models", f)) / 2**20, 1)}
    try:
        import glob
        from features import STATIC
        lit = pickle.load(open(os.path.join(OUT, "models", "lit.pkl"), "rb"))
        e = pd.concat([pd.read_csv(p) for p in sorted(glob.glob(os.path.join(ML, "data", "blocks", "*.edges.csv.gz")))[:5]])
        X = e[STATIC].head(10000)
        t0 = time.perf_counter()
        lit["calibrator"].predict(lit["model"].predict_proba(X)[:, 1])
        res["lit.pkl"]["ms_per_1000_streets"] = round((time.perf_counter() - t0) * 1000 / len(X) * 1000, 1)
    except Exception as ex:  # efficiency is informative, never a reason to fail the card
        res["note"] = f"timing skipped: {type(ex).__name__}: {ex}"
    return res


def coverage(metrics):
    lit = metrics.get("lit", {})
    labelled, unl = lit.get("rows"), lit.get("unlabelled_streets_predicted")
    out = {"streets_with_a_lit_label": labelled, "streets_predicted": unl}
    if labelled and unl:
        out["share_with_real_lit_label"] = labelled / (labelled + unl)
        out["grade"] = grade("coverage", out["share_with_real_lit_label"])
    return out


# ------------------------------------------------------------------ report

def verdict(c):
    lit, rt = c["street"]["lit"], c["route"]
    people = rt["vs_people"]
    if people["n"] >= 200 and people["agreement"] >= 0.8 and lit.get("plan_bar (AUROC>=0.85 and ECE<=0.05)"):
        return "Pilot-ready: checked against real people and the street model meets the plan bar."
    if rt["behaviour"] == 1.0 and rt["grades"]["agreement"] in "AB" and lit["grades"]["error_vs_baseline"] in "ABC":
        return ("Demo-ready, not pilot-ready: route choices behave sensibly and beat simple rules on unseen areas, but they "
                f"are checked against only {people['n']} answers from people, and the lit-street model is below the plan bar "
                "(AUROC 0.85, ECE 0.05).")
    return "Research prototype: fails a must-pass behaviour test or does not beat simple rules clearly."


def pct(x):
    return "n/a" if x is None else f"{x:.1%}"


def report(c):
    L = ["# SafeRoute model scorecard", "", f"{c['when']}. Every number is on areas the model never trained on.", "",
         f"**Verdict:** {c['verdict']}", "", "## Grades at a glance", "", "| Part | What is measured | Result | Grade |", "|---|---|---|---|"]
    lit, rt = c["street"]["lit"], c["route"]
    rows = [("Route choice", "agrees with its labels on unseen areas", pct(rt["agreement_all_labels"]), rt["grades"]["agreement"]),
            ("Route choice", "agrees with Claude's hand answers", f"{pct(rt['vs_claude_hand']['agreement'])} (n={rt['vs_claude_hand']['n']})", rt["grades"]["vs_claude_hand"]),
            ("Route choice", "agrees with real people", f"{pct(rt['vs_people']['agreement'])} (n={rt['vs_people']['n']})", rt["grades"]["people_agreement"]),
            ("Route choice", "night-time pairs", f"{pct(rt['night']['agreement'])} (n={rt['night']['n']})", rt["grades"]["night"]),
            ("Route choice", "must-pass behaviour tests", f"{sum(t['pass'] for t in rt['behaviour_tests'].values())} of {len(rt['behaviour_tests'])}", rt["grades"]["behaviour"]),
            ("Route choice", "hardest slice (day, night, worst area) vs overall", f"-{rt['worst_slice_gap']:.1%}", rt["grades"]["worst_slice_gap"]),
            ("Route choice", "calibration of its confidence (ECE)", f"{rt['ece']:.3f}", rt["grades"]["ece"]),
            ("Lit streets", "AUROC (plan bar 0.85)", f"{lit['auroc']:.3f}" if lit.get("auroc") else "n/a", lit["grades"]["auroc"]),
            ("Lit streets", "calibration ECE (plan bar 0.05)", f"{lit['ece']:.3f}" if lit.get("ece") is not None else "n/a", lit["grades"]["ece"]),
            ("Lit streets", "mistakes removed vs road-type guess", pct(lit.get("error_vs_baseline")), lit["grades"]["error_vs_baseline"]),
            ("Lit streets", "dark streets called lit with 80%+ confidence", pct(lit.get("false_safe")), lit["grades"]["false_safe"]),
            ("Lit streets", "worst road type vs overall", f"+{lit['worst_slice_gap']:.1%} errors" if lit.get("worst_slice_gap") is not None else "n/a", lit["grades"]["worst_slice_gap"])]
    for h in ("open_share", "bus_per_hour", "traffic_ratio"):
        hc = c["street"][h]
        rows.append((h, "error removed vs hour-of-day average", pct(hc["error_vs_baseline"]), hc["grades"]["error_vs_baseline"]))
        rows.append((h, "ordering (Spearman)", f"{hc['spearman']}", hc["grades"]["spearman"]))
    cov = c["coverage"]
    if "share_with_real_lit_label" in cov:
        rows.append(("Data", "streets with a real lit measurement", f"{cov['share_with_real_lit_label']:.1%} of {cov['streets_with_a_lit_label'] + cov['streets_predicted']:,}", cov["grade"]))
    L += [f"| {a} | {b} | {r} | **{g}** |" for a, b, r, g in rows]
    L += ["", "A excellent, B good, C usable with care, D weak, F fails, I = not enough samples to grade (fewer than 30).", "",
          "## Route choice in detail", "",
          f"- {rt['pairs']} labelled pairs in {rt['areas']} areas; label sources {rt['label_sources']}.",
          f"- {rt['share_of_labels_from_written_rules']:.0%} of the labels come from Claude's written criteria applied by code, so "
          "agreement with them partly measures how well the model copies those rules.",
          f"- Day {pct(rt['day']['agreement'])} (n={rt['day']['n']}), night {pct(rt['night']['agreement'])} (n={rt['night']['n']}); "
          f"baseline 'always the shorter route' {pct(rt['baseline_shorter_route'])}.",
          f"- Worst area: {pct(rt['worst_area']['agreement'])} of {rt['worst_area']['n']} pairs." if rt.get("worst_area") else "",
          f"- Swapping A and B flips the answer exactly: {pct(rt['swap_consistency (A vs B == -(B vs A))'])}. "
          f"Speed: {rt['ms_per_route_pair']} ms per pair.", "", "| Behaviour test | Score | Pass |", "|---|---|---|"]
    L += [f"| {k} | {v['score']:+} | {'yes' if v['pass'] else '**NO**'} |" for k, v in rt["behaviour_tests"].items()]
    L += ["", "## Lit streets in detail", ""]
    L += [f"- {k}: {v}" for k, v in lit.items() if k not in ("grades", "by_road_type")]
    if lit.get("by_road_type"):
        L += ["", "| Road type code | Streets | Error |", "|---|---|---|"] + [f"| {k} | {v['n']} | {v['error']:.1%} |" for k, v in lit["by_road_type"].items()]
    L += ["", "## Hourly heads", ""]
    for h in ("open_share", "bus_per_hour", "traffic_ratio"):
        hc = c["street"][h]
        L.append(f"- **{h}**: MAE {hc['mae']} vs baseline {hc['baseline_mae']}, Spearman {hc['spearman']}, "
                 f"beats baseline with its whole 95% interval: {hc['beats_baseline (whole 95% interval better)']}"
                 + (f"; day/night: {hc['day_vs_night']}" if hc.get("day_vs_night") else ""))
    L += ["", "## Efficiency", ""] + [f"- {k}: {v}" for k, v in c["efficiency"].items()]
    L += ["", "## What this does not show", "",
          "- Real-world safety. Labels are lighting tags, opening hours, bus logs and AI or rule judgments of which route looks "
          "safer, not records of what happened to people.",
          "- Generalisation outside Delhi NCR.",
          "", "Thresholds: " + "; ".join(f"{k} {'>=' if hi else '<='} A {c_[0]}, B {c_[1]}, C {c_[2]}, D {c_[3]}" for k, (hi, c_) in GRADES.items())]
    return "\n".join(l for l in L if l is not None) + "\n"


def main():
    metrics = {m["head"]: m for m in json.load(open(os.path.join(OUT, "metrics.json")))}
    c = {"when": time.strftime("%Y-%m-%d %H:%M"),
         "street": {"lit": lit_card(metrics), **{h: hourly_card(h, metrics) for h in ("open_share", "bus_per_hour", "traffic_ratio")}},
         "route": route_card(), "efficiency": efficiency(), "coverage": coverage(metrics)}
    c["verdict"] = verdict(c)
    out = next((a.split("=", 1)[1] for a in sys.argv if a.startswith("--out=")), os.path.join(OUT, "scorecard.md"))
    open(out, "w", encoding="utf-8").write(report(c))
    json.dump(c, open(out.replace(".md", ".json"), "w"), indent=1, default=str)
    print(report(c))


if __name__ == "__main__":
    main()
