"""Model registry and release gate for the street models. A training run (ml/train.py) only ever makes a candidate in
ml/out/candidates/<run>/; this decides, head by head, whether it may replace the model in use, and can roll back.

  py ml/registry.py list                  candidates and the promotion history
  py ml/registry.py check <run>           gate each retrained head against the model in use; changes nothing
  py ml/registry.py promote <run>         promote only the heads that pass (backs up the models in use first)
  py ml/registry.py rollback              put back the models from before the last promotion

Gate per head (all must hold; thresholds in GATE):
  lit (classifier)   beats the road-type baseline; AUROC not lower than the model in use; ECE not more than 0.02 worse
  hourly heads       whole 95% interval of the gain over the hour-of-day baseline above 0; MAE not worse than the model in use
  every head         trained on the same data as the model in use, so the locked scores are comparable; otherwise the
                     verdict is "not comparable" and nothing is promoted (re-score the model in use on the new data first)
History: ml/out/registry/registry.json (who, when, which head, hashes, verdict). Backups: ml/out/registry/backup-<time>/.
"""
import hashlib
import json
import os
import shutil
import sys
import time

ML = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(ML, "out")
CAND = os.path.join(OUT, "candidates")
REG = os.path.join(OUT, "registry")
GATE = {"auroc_drop_max": 0.0, "ece_rise_max": 0.02, "mae_rise_max": 0.0}


def sha(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def load_metrics(folder):
    p = os.path.join(folder, "metrics.json")
    return {m["head"]: m for m in json.load(open(p))} if os.path.exists(p) else {}


def history():
    p = os.path.join(REG, "registry.json")
    return json.load(open(p)) if os.path.exists(p) else []


def save_history(h):
    os.makedirs(REG, exist_ok=True)
    json.dump(h, open(os.path.join(REG, "registry.json"), "w"), indent=1)


def gate_head(head, cand, prod, same_data):
    """(passed, reasons) for one head."""
    why = []
    t, pt = cand.get("locked_test"), (prod or {}).get("locked_test")
    if "skipped" in cand or not isinstance(t, dict):
        return False, [f"not trained or not scored ({cand.get('skipped') or t})"]
    if not same_data:
        why.append("not comparable: trained on different data than the model in use")
    if head == "lit":
        if not t["error_rate"] < t["baseline_error_rate (road type only)"]:
            why.append(f"does not beat the road-type baseline ({t['error_rate']} vs {t['baseline_error_rate (road type only)']})")
        if isinstance(pt, dict):
            if t["auroc"] < pt["auroc"] - GATE["auroc_drop_max"]:
                why.append(f"AUROC {t['auroc']} is lower than the model in use ({pt['auroc']})")
            if t["ece"] > pt["ece"] + GATE["ece_rise_max"]:
                why.append(f"calibration worse: ECE {t['ece']} vs {pt['ece']}")
    else:
        if not cand.get("beats_baseline"):
            why.append(f"gain over the hour baseline not clear (95% interval {t.get('mae_gain_over_baseline_ci95')})")
        if isinstance(pt, dict) and t["mae"] > pt["mae"] + GATE["mae_rise_max"]:
            why.append(f"MAE {t['mae']} is worse than the model in use ({pt['mae']})")
    return not why, why


def check(run):
    folder = os.path.join(CAND, run)
    if not os.path.isdir(folder):
        sys.exit(f"No candidate {run} in {CAND}")
    info = json.load(open(os.path.join(folder, "run.json"))) if os.path.exists(os.path.join(folder, "run.json")) else {}
    cand = load_metrics(folder)
    res = {}
    for head in info.get("heads") or list(cand):
        if head not in cand or not os.path.exists(os.path.join(folder, "models", f"{head}.pkl")):
            continue
        ref, ref_name = reference(head, info.get("data_blocks_sha256"), run)
        ok, why = gate_head(head, cand[head], ref, ref is not None)
        res[head] = {"pass": ok, "reasons": why, "compared_with": ref_name}
    return folder, info, res


def reference(head, data_hash, run):
    """Locked-test metrics of the model in use, measured on the same data as the candidate. The model in use counts if it
    was promoted on this data; otherwise a re-score run (ml/train.py --heads <head> --trials 0: the in-use settings
    only, refit on today's data) stands in for it. None when neither exists: the scores are not comparable."""
    if not data_hash:
        return None, None
    last = next((e for e in reversed(history()) if head in e.get("promoted", [])), None)
    if last and last.get("data_blocks_sha256") == data_hash:
        return load_metrics(OUT).get(head), "model in use"
    for r in sorted(os.listdir(CAND), reverse=True) if os.path.isdir(CAND) else []:
        p = os.path.join(CAND, r, "run.json")
        if r == run or not os.path.exists(p):
            continue
        i = json.load(open(p))
        if i.get("trials") == 0 and head in i.get("heads", []) and i.get("data_blocks_sha256") == data_hash:
            m = load_metrics(os.path.join(CAND, r)).get(head)
            if m:
                return m, f"re-score of the model in use ({r})"
    return None, None


def promote(run):
    folder, info, res = check(run)
    passed = [h for h, r in res.items() if r["pass"]]
    stamp = time.strftime("%Y%m%d-%H%M%S")
    entry = {"time": time.strftime("%Y-%m-%d %H:%M:%S"), "action": "promote", "candidate": run, "gate": res,
             "data_blocks_sha256": info.get("data_blocks_sha256")}
    if passed:
        backup = os.path.join(REG, f"backup-{stamp}")
        os.makedirs(os.path.join(backup, "models"), exist_ok=True)
        for f in ("metrics.json", "report.md", "lit_predictions.csv.gz"):
            if os.path.exists(os.path.join(OUT, f)):
                shutil.copy2(os.path.join(OUT, f), backup)
        prod = load_metrics(OUT)
        cand = load_metrics(folder)
        for h in passed:
            src = os.path.join(OUT, "models", f"{h}.pkl")
            if os.path.exists(src):
                shutil.copy2(src, os.path.join(backup, "models"))
            shutil.copy2(os.path.join(folder, "models", f"{h}.pkl"), src)
            if os.path.exists(os.path.join(folder, "locked", f"{h}.csv.gz")):
                os.makedirs(os.path.join(OUT, "locked"), exist_ok=True)
                shutil.copy2(os.path.join(folder, "locked", f"{h}.csv.gz"), os.path.join(OUT, "locked", f"{h}.csv.gz"))
            if h == "lit" and os.path.exists(os.path.join(folder, "lit_predictions.csv.gz")):
                shutil.copy2(os.path.join(folder, "lit_predictions.csv.gz"), OUT)
            prod[h] = cand[h]
        json.dump(list(prod.values()), open(os.path.join(OUT, "metrics.json"), "w"), indent=1, default=str)
        entry.update(promoted=passed, backup=backup, hashes={h: sha(os.path.join(OUT, "models", f"{h}.pkl")) for h in passed})
    else:
        entry.update(promoted=[], note="no head passed the gate: the models in use are unchanged")
    h = history()
    h.append(entry)
    save_history(h)
    return entry


def rollback():
    h = history()
    last = next((e for e in reversed(h) if e.get("action") == "promote" and e.get("backup")), None)
    if not last:
        sys.exit("Nothing to roll back")
    b = last["backup"]
    for f in os.listdir(os.path.join(b, "models")):
        shutil.copy2(os.path.join(b, "models", f), os.path.join(OUT, "models", f))
    for f in ("metrics.json", "report.md", "lit_predictions.csv.gz"):
        if os.path.exists(os.path.join(b, f)):
            shutil.copy2(os.path.join(b, f), OUT)
    for head in last.get("promoted", []):  # locked answers belonged to the rolled-back model
        p = os.path.join(OUT, "locked", f"{head}.csv.gz")
        if os.path.exists(p):
            os.remove(p)
    h.append({"time": time.strftime("%Y-%m-%d %H:%M:%S"), "action": "rollback", "restored_from": b, "heads": last.get("promoted")})
    save_history(h)
    return b


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "list"
    if cmd == "list":
        runs = sorted(os.listdir(CAND)) if os.path.isdir(CAND) else []
        print("Candidates:", *runs, sep="\n  ")
        print("History:", json.dumps(history(), indent=1))
    elif cmd == "check":
        _, _, res = check(sys.argv[2])
        print(json.dumps(res, indent=1))
    elif cmd == "promote":
        print(json.dumps(promote(sys.argv[2]), indent=1))
    elif cmd == "rollback":
        print("Restored from", rollback())
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main()
