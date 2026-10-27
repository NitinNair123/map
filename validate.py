"""Check the safety scores against independent field audits.

Usage: python validate.py data/audits.csv   (on Windows: py validate.py data/audits.csv)

audits.csv has one row per spot a person audited on foot, with columns
lat,lon,when,safety,lit
  when    ISO date-time of the audit, e.g. 2026-10-27T22:15+05:30
  safety  the auditor's overall rating from 1 (unsafe) to 5 (safe), e.g. Safetipin's or Jagori's checklist
  lit     yes / no for whether the street lights were on, or empty if not checked
Auditors should not look at SafeRoute's scores first, or the check stops being independent.
Writes audits_scored.csv beside the input with the model's score for every row.
"""
import csv
import math
import os
import sys
from datetime import datetime

from safety import seg_dist

MIN_AUDITS = 30  # fewer than this and the correlation is mostly noise


def ranks(xs):
    """Ranks from 1, ties sharing their average rank."""
    order = sorted(range(len(xs)), key=lambda i: xs[i])
    out = [0.0] * len(xs)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and xs[order[j + 1]] == xs[order[i]]:
            j += 1
        for k in range(i, j + 1):
            out[order[k]] = (i + j) / 2 + 1
        i = j + 1
    return out


def spearman(xs, ys):
    """Rank correlation: 1 means the model orders spots exactly like the auditors, 0 means no relation."""
    rx, ry = ranks(xs), ranks(ys)
    mx, my = sum(rx) / len(rx), sum(ry) / len(ry)
    cov = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
    sd = math.sqrt(sum((a - mx) ** 2 for a in rx) * sum((b - my) ** 2 for b in ry))
    return cov / sd if sd else None


def score_audits(rows, store):
    """The model's score and lighting for the street nearest each audited spot."""
    out = []
    for row in rows:
        pt, when = (float(row["lat"]), float(row["lon"])), datetime.fromisoformat(row["when"])
        area = store.around(pt)
        light = area.light(when)
        e = min(area.edges, key=lambda e: seg_dist(pt, area.nodes[e["a"]], area.nodes[e["b"]]))
        lighting = area.factors(e, when, light)[0]["lighting"]
        out.append({**row, "street": e["name"] or e["highway"], "score": round(area.edge_score(e, when, light)),
                    "model_lit": "" if lighting is None else "yes" if lighting >= 0.5 else "no"})
    return out


def summary(scored):
    rated = [r for r in scored if r.get("safety", "").strip()]
    lit = [r for r in scored if r.get("lit", "").strip().lower() in ("yes", "no")]
    known = [r for r in lit if r["model_lit"]]
    return {
        "audits": len(scored),
        "spearman": spearman([r["score"] for r in rated], [float(r["safety"]) for r in rated]) if len(rated) >= 3 else None,
        "enough": len(rated) >= MIN_AUDITS,
        "lighting_checked": len(lit),
        "lighting_no_data": len(lit) - len(known),
        "lighting_agree": sum(r["model_lit"] == r["lit"].strip().lower() for r in known),
        "lighting_known": len(known),
    }


def report(s):
    lines = [f"{s['audits']} audited spots."]
    if s["spearman"] is None:
        lines.append("Not enough safety ratings to compare.")
    else:
        lines.append(f"Score vs auditors' rating: Spearman {s['spearman']:.2f} (1 = same order, 0 = unrelated)."
                     + ("" if s["enough"] else f" Under {MIN_AUDITS} ratings, so treat this as a first look."))
    if s["lighting_checked"]:
        lines.append(f"Lighting: the model had data for {s['lighting_known']} of {s['lighting_checked']} checked spots"
                     + (f" and agreed on {s['lighting_agree']} ({100 * s['lighting_agree'] // s['lighting_known']}%)." if s["lighting_known"] else "."))
    return "\n".join(lines)


if __name__ == "__main__":
    import server  # the same data sources and cache as the running app

    path = sys.argv[1]
    with open(path, newline="") as f:
        rows = list(csv.DictReader(f))
    scored = score_audits(rows, server.store)
    out = os.path.join(os.path.dirname(os.path.abspath(path)), "audits_scored.csv")
    with open(out, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(scored[0]))
        w.writeheader()
        w.writerows(scored)
    print(report(summary(scored)))
    print(f"Every row with its score: {out}")
