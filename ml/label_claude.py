"""Claude's answers to "which route is safer, A or B?" for every pair in ml/data/pairs.jsonl.

Two kinds, kept apart in the file:
- claude_hand:   pairs Claude judged one by one in a chat (ml/data/labels_claude_hand.csv). Add more rows there any time.
- claude_rubric: every other pair, judged by the written criteria Claude set down first (ml/eval_claude/criteria.md, based on
                 Safetipin's audit parameters), applied here by code. This is Claude's rule, not a live Claude answer, and not people.
Re-run after new pairs are made: it labels every pair again, so changing the rubric or adding hand answers takes effect.

Usage: py ml/label_claude.py      -> ml/data/labels_claude.csv (id, safer, reason, source)
"""
import csv
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pairs  # noqa: E402

DATA = os.path.dirname(pairs.OUT)
OUT = os.path.join(DATA, "labels_claude.csv")
HAND = os.path.join(DATA, "labels_claude_hand.csv")
TIE = 0.15  # score gap below this = "same"


def m(x, cap):
    return min(x, cap) / cap


def terms(f, dark, shorter):
    """Each criterion's contribution to one route's score (higher = safer). Weights follow Safetipin's order: lighting first."""
    L, w = (1.5, 1.5) if dark else (0.3, 1.0)
    return {
        "lighting": L * (2.0 * f["lit_pct"] / 100 - 2.0 * f["dark_pct"] / 100 - 0.5 * f["no_light_data_pct"] / 100),
        "open places": w * 0.8 * m(f["open_now"], 10),
        "places along it": w * 0.3 * m(f["food_found"] + f["shop_found"] + f["other_found"], 15),
        "main roads": w * 0.5 * f["main_pct"] / 100,
        "isolated stretch": -w * 1.0 * m(f["isolated_m"], 1500),
        "nothing open stretch": -w * 0.7 * m(f["nothing_open_m"], 1500),
        "far from help": -w * 0.7 * m(f["help_gap_m"], 1500),
        "CCTV": 0.3 * m(f["cctv"], 5),
        "police distance": -0.3 * m(f["police_m"], 3000),
        "pharmacy/hospital/fuel": 0.15 * m(f["pharmacy_found"] + f["hospital_found"] + f["fuel_found"], 3),
        "transit": 0.2 * m(f["transit_found"], 3),
        "unrated": -0.3 * f["unrated_pct"] / 100,
        "extra length": -(0.8 if dark else 0.3) * min(1.0, (f["length_m"] - shorter) / max(shorter, 1)),
    }


def judge(p):
    """("A" | "B" | "same", gap, reason) by the written criteria."""
    dark = p["daylight"] < 0.5
    sh = min(p["A"]["length_m"], p["B"]["length_m"])
    ta, tb = terms(p["A"], dark, sh), terms(p["B"], dark, sh)
    d = {k: ta[k] - tb[k] for k in ta}
    gap = sum(d.values())
    safer = "A" if gap > TIE else "B" if gap < -TIE else "same"
    if safer == "same":
        return safer, gap, "the measures roughly cancel out"
    sign = 1 if safer == "A" else -1
    top = [k for k, v in sorted(d.items(), key=lambda kv: -sign * kv[1]) if sign * v > 0.02][:3]
    return safer, gap, f"{safer} is better on " + ", ".join(top) + (" (after dark)" if dark else "")


def hand_labels():
    if not os.path.exists(HAND):
        return {}
    return {int(r["id"]): r for r in csv.DictReader(open(HAND, encoding="utf-8-sig")) if r.get("safer") in ("A", "B", "same")}


def load(path=OUT):
    """{pair id: (safer, source)} from labels_claude.csv."""
    if not os.path.exists(path):
        return {}
    return {int(r["id"]): (r["safer"], r["source"]) for r in csv.DictReader(open(path, encoding="utf-8"))}


def main():
    every = pairs.load()
    if not every:
        sys.exit(f"No route pairs yet ({pairs.OUT}): run 'Make route pairs' first.")
    hand = hand_labels()
    counts = {"claude_hand": 0, "claude_rubric": 0}
    tmp = OUT + ".tmp"
    with open(tmp, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["id", "safer", "reason", "source"])
        for n, p in enumerate(every, 1):
            if p["id"] in hand:
                h = hand[p["id"]]
                w.writerow([p["id"], h["safer"], h.get("reason") or "judged by Claude pair by pair", "claude_hand"])
                counts["claude_hand"] += 1
            else:
                safer, _, reason = judge(p)
                w.writerow([p["id"], safer, reason, "claude_rubric"])
                counts["claude_rubric"] += 1
            if n % 250 == 0:
                print(f"{n} of {len(every)}", flush=True)
    os.replace(tmp, OUT)
    print(f"Claude labelled {len(every)} pairs: {counts['claude_hand']} judged by hand, {counts['claude_rubric']} by Claude's written criteria. -> {OUT}", flush=True)


if __name__ == "__main__":
    main()
