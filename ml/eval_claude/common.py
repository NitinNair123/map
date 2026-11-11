import csv, json, os, sys
import numpy as np
ML = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ML, "data")
sys.path.insert(0, ML)


def load_pairs():
    out = []
    with open(os.path.join(DATA, "pairs.jsonl")) as f:
        for l in f:
            p = json.loads(l)
            p["ends"] = [p["A_coords"][0], p["A_coords"][-1]]
            p["A_n"] = len(p.pop("A_coords")); p.pop("B_coords")
            out.append(p)
    return out


def qwen():
    return {int(r["id"]): r for r in csv.DictReader(open(os.path.join(DATA, "labels_llm.csv"), encoding="utf-8"))}


def S(f, dark, shorter):
    L = 1.5 if dark else 0.3
    w = 1.5 if dark else 1.0
    m = lambda x, c: min(x, c) / c
    s = L * (2.0 * f["lit_pct"] / 100 - 2.0 * f["dark_pct"] / 100 - 0.5 * f["no_light_data_pct"] / 100)
    s += w * (0.8 * m(f["open_now"], 10) + 0.3 * m(f["food_found"] + f["shop_found"] + f["other_found"], 15) + 0.5 * f["main_pct"] / 100
              - 1.0 * m(f["isolated_m"], 1500) - 0.7 * m(f["nothing_open_m"], 1500) - 0.7 * m(f["help_gap_m"], 1500))
    s += 0.3 * m(f["cctv"], 5) - 0.3 * m(f["police_m"], 3000)
    s += 0.15 * m(f["pharmacy_found"] + f["hospital_found"] + f["fuel_found"], 3) + 0.2 * m(f["transit_found"], 3)
    s -= 0.3 * f["unrated_pct"] / 100
    s -= (0.8 if dark else 0.3) * min(1.0, (f["length_m"] - shorter) / max(shorter, 1))
    return s


def rubric(p):
    dark = p["daylight"] < 0.5
    sh = min(p["A"]["length_m"], p["B"]["length_m"])
    d = S(p["A"], dark, sh) - S(p["B"], dark, sh)
    return "A" if d > 0.15 else "B" if d < -0.15 else "same", d
