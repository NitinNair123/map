"""Feature contracts: what every route feature means, where it comes from, and whether a value is observed, inferred,
unknown or stale. Validation turns rows into a report instead of silently fixing them.

  py ml/contracts.py            checks ml/data/pairs.jsonl and the street blocks, writes ml/out/audit/data_contracts.md

Status meanings
  observed       measured from a source (an OSM lit tag, a published opening time, a mapped police station)
  inferred       estimated by a model or rule (a street model's lighting guess, a typical-hours guess)
  unknown        no evidence either way; must never be read as "no", "dark" or "closed"
  stale          evidence older than its source's freshness limit (live buses older than 15 minutes, ...)
  contradictory  sources disagree (e.g. an OSM lit=yes street that a report calls dark)
"""
import glob
import gzip
import json
import math
import os
import sys

ML = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(ML, "out")

# route-level features the route ranker sees (ml/pairs.py, computed by safety.py's route summary)
ROUTE = {
    "length_m": {"unit": "m", "source": "street geometry (OSM / Overture)", "kind": "observed", "range": (1, 50_000)},
    "lit_pct": {"unit": "% of length", "source": "OSM lit tags, lamps, reports (rating.py lighting factor)", "kind": "observed+inferred",
                "range": (0, 100), "unknown_in": "no_light_data_pct"},
    "dark_pct": {"unit": "% of length", "source": "as lit_pct", "kind": "observed+inferred", "range": (0, 100), "unknown_in": "no_light_data_pct"},
    "no_light_data_pct": {"unit": "% of length", "source": "streets with no lighting evidence", "kind": "unknown share", "range": (0, 100)},
    "main_pct": {"unit": "% of length", "source": "OSM highway class", "kind": "observed", "range": (0, 100)},
    "open_now": {"unit": "count", "source": "places with published hours that are open when passed", "kind": "observed", "range": (0, 10_000)},
    "isolated_m": {"unit": "m", "source": "longest stretch rated under 40 with enough confidence", "kind": "inferred", "range": (0, 50_000)},
    "nothing_open_m": {"unit": "m", "source": "longest stretch with no place known to be open; unknown hours end a stretch "
                       "(fixed 12 Nov 2026, safety.py)", "kind": "observed", "range": (0, 50_000), "unknown_in": "hours_unknown_m"},
    "hours_unknown_m": {"unit": "m", "source": "length past places with no published hours", "kind": "unknown share", "range": (0, 50_000),
                        "optional": True},
    "help_gap_m": {"unit": "m", "source": "longest stretch with no open help place and no police within 150 m", "kind": "observed",
                   "range": (0, 50_000)},
    "cctv": {"unit": "count", "source": "mapped CCTV (OSM surveillance, official lists); working state unknown", "kind": "observed",
             "range": (0, 10_000)},
    "police_m": {"unit": "m", "source": "nearest mapped police station or booth, straight line", "kind": "observed", "range": (0, 100_000),
                 "none_means": "unknown (no police mapped nearby)"},
    "unrated_pct": {"unit": "% of length", "source": "streets without enough evidence to rate", "kind": "unknown share", "range": (0, 100)},
}
for k in ("food_found", "shop_found", "pharmacy_found", "hospital_found", "fuel_found", "other_found", "transit_found"):
    ROUTE[k] = {"unit": "count", "source": "mapped places by group (OSM, Overture, Geoapify)", "kind": "observed", "range": (0, 10_000)}

# street-level columns that must hold an "unknown" (NaN) rather than 0 when there is no evidence
STREET_UNKNOWN_NOT_ZERO = ["lit"]
NCR_BBOX = (27.9, 76.7, 29.0, 77.9)  # Delhi, Gurugram, Noida, Ghaziabad, Faridabad with margin


def check_route(stats):
    """(problems, status) for one route's stats dict: out-of-range values are problems, never silently clipped."""
    problems, status = [], {}
    for k, spec in ROUTE.items():
        v = stats.get(k)
        if v is None:
            if not spec.get("optional"):
                status[k] = "unknown"
                if "none_means" not in spec:
                    problems.append(f"{k}: missing")
            continue
        if isinstance(v, float) and math.isnan(v):
            status[k] = "unknown"
            continue
        lo, hi = spec["range"]
        if not lo <= v <= hi:
            problems.append(f"{k}={v} outside {lo}..{hi}")
        status[k] = "observed" if spec["kind"] == "observed" else spec["kind"]
    if all(k in stats for k in ("lit_pct", "dark_pct", "no_light_data_pct")):
        total = stats["lit_pct"] + stats["dark_pct"] + stats["no_light_data_pct"]
        if abs(total - 100) > 2:
            problems.append(f"lit+dark+no-data = {total}%, should be 100%")
    return problems, status


def check_pairs(path):
    out = {"pairs": 0, "routes_with_problems": 0, "problems": {}, "unknown_share": {}, "missing_hours_unknown_m": 0}
    unknown = {k: 0 for k in ROUTE}
    n_routes = 0
    for line in open(path, encoding="utf-8"):
        p = json.loads(line)
        out["pairs"] += 1
        for side in ("A", "B"):
            n_routes += 1
            probs, st = check_route(p[side])
            out["missing_hours_unknown_m"] += "hours_unknown_m" not in p[side]
            for k, s in st.items():
                unknown[k] += s == "unknown"
            if probs:
                out["routes_with_problems"] += 1
                for pr in probs:
                    key = pr.split("=")[0].split(":")[0]
                    out["problems"][key] = out["problems"].get(key, 0) + 1
    out["unknown_share"] = {k: round(v / max(1, n_routes), 3) for k, v in unknown.items() if v}
    return out


def check_blocks(folder, limit=None):
    import pandas as pd
    files = sorted(glob.glob(os.path.join(folder, "*.edges.csv.gz")))[:limit]
    seen, dup, bad_xy, rows, lit_zero_from_missing = set(), 0, 0, 0, 0
    for f in files:
        d = pd.read_csv(f, usecols=lambda c: c in ("edge_id", "lat", "lon", "lit", "lit_source"), dtype={"lit_source": str, "edge_id": str})
        rows += len(d)
        ids = set(d["edge_id"].astype(str))
        dup += len(ids & seen)
        seen |= ids
        s, w, n, e = NCR_BBOX
        bad_xy += int((~d["lat"].between(s, n) | ~d["lon"].between(w, e)).sum())
        if "lit_source" in d:
            lit_zero_from_missing += int(((d["lit"] == 0) & d["lit_source"].isna()).sum())
    return {"files": len(files), "streets": rows, "duplicate_edge_ids_across_blocks": dup, "outside_ncr": bad_xy,
            "lit_0_without_a_source (unknown read as dark)": lit_zero_from_missing}


def main():
    res = {"route_pairs": check_pairs(os.path.join(ML, "data", "pairs.jsonl")),
           "street_blocks": check_blocks(os.path.join(ML, "data", "blocks"))}
    os.makedirs(os.path.join(OUT, "audit"), exist_ok=True)
    lines = ["# Data contract check", "", "Feature definitions: `ml/contracts.py` (ROUTE). Problems are reported, never silently fixed.", "",
             "```json", json.dumps(res, indent=1), "```", "",
             "- `missing_hours_unknown_m`: route pairs generated before the unknown-hours fix; regenerate pairs to get it.",
             "- `duplicate_edge_ids_across_blocks`: a street on a block edge exported twice; train.py drops duplicates on load."]
    open(os.path.join(OUT, "audit", "data_contracts.md"), "w", encoding="utf-8").write("\n".join(lines) + "\n")
    print(json.dumps(res, indent=1))


if __name__ == "__main__":
    sys.exit(main())
