"""Baseline snapshot: a reproducible record of exactly what the ML system is right now. Changes nothing.

Writes ml/out/baselines/<YYYYmmdd-HHMM>/ with
  manifest.json   environment (Python, packages, GPU, driver, CUDA), SHA-256 of every data file, model and report,
                  label provenance counts, and the code files' hashes
  metrics.json, scorecard.md, report.md, route_report.md, route_ranker_v2.json   copies of the current reports
Never reads or records anything from key or token files.

Usage: py ml/snapshot.py
"""
import csv
import glob
import hashlib
import importlib.metadata as md
import json
import os
import platform
import shutil
import subprocess
import sys
import time
from collections import Counter

ML = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(ML)
OUT = os.path.join(ML, "out")
SKIP = ("key", "token", "secret", "password", "api_keys")  # never hash, copy or list credential files
PACKAGES = ["numpy", "pandas", "scikit-learn", "scipy", "xgboost", "pyarrow", "duckdb", "shapely"]


def sha(path, block=1 << 20):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(block), b""):
            h.update(chunk)
    return h.hexdigest()


def files(pattern):
    return sorted(p for p in glob.glob(pattern, recursive=True) if os.path.isfile(p) and not any(s in os.path.basename(p).lower() for s in SKIP))


def rel(p):
    return os.path.relpath(p, ROOT).replace("\\", "/")


def gpu():
    try:
        q = subprocess.run(["nvidia-smi", "--query-gpu=name,memory.total,driver_version", "--format=csv,noheader"],
                           capture_output=True, text=True, timeout=20).stdout.strip()
        cuda = subprocess.run(["nvidia-smi"], capture_output=True, text=True, timeout=20).stdout
        ver = cuda.split("CUDA Version:")[1].split()[0] if "CUDA Version:" in cuda else None
        return {"nvidia_smi": q, "driver_cuda_version": ver}
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}


def label_provenance():
    out = {}
    data = os.path.join(ML, "data")
    for name in ("labels_claude.csv", "labels_claude_hand.csv", "labels_human.csv", "labels_llm.csv", "labels_qwen_taught.csv"):
        p = os.path.join(data, name)
        if not os.path.exists(p):
            continue
        rows = list(csv.DictReader(open(p, encoding="utf-8", errors="replace")))
        col = next((c for c in ("safer", "label", "answer", "choice") if rows and c in rows[0]), None)
        src = next((c for c in ("source", "model", "who") if rows and c in rows[0]), None)
        out[name] = {"rows": len(rows), "answers": dict(Counter(r[col] for r in rows)) if col else None,
                     "by_source": dict(Counter(r[src] for r in rows)) if src else None}
    return out


def main():
    stamp = time.strftime("%Y%m%d-%H%M")
    dest = os.path.join(OUT, "baselines", stamp)
    os.makedirs(dest, exist_ok=True)
    pkgs = {}
    for p in PACKAGES:
        try:
            pkgs[p] = md.version(p)
        except md.PackageNotFoundError:
            pkgs[p] = None
    try:
        import xgboost
        xgb_cuda = xgboost.build_info().get("USE_CUDA")
    except Exception:
        xgb_cuda = None
    blocks = files(os.path.join(ML, "data", "blocks", "*"))
    manifest = {
        "created": time.strftime("%Y-%m-%d %H:%M:%S"),
        "source_tree": {"path": ROOT, "git": "not a git checkout (no .git on this laptop); folder snapshot of PR #23 branch "
                        "claude/project-thread-tsndpt plus local changes", "upstream": "github.com/NitinNair123/map"},
        "environment": {"python": sys.version.split()[0], "platform": platform.platform(), "packages": pkgs,
                        "xgboost_built_with_cuda": xgb_cuda, "gpu": gpu()},
        "code": {rel(p): sha(p) for p in files(os.path.join(ML, "*.py"))},
        "models": {rel(p): {"sha256": sha(p), "bytes": os.path.getsize(p)} for p in files(os.path.join(OUT, "models", "*"))},
        "route_model": {rel(p): sha(p) for p in files(os.path.join(OUT, "route_ranker*.json"))},
        "data": {rel(p): {"sha256": sha(p), "bytes": os.path.getsize(p)} for p in files(os.path.join(ML, "data", "*.*"))},
        "blocks": {"files": len(blocks), "bytes": sum(os.path.getsize(p) for p in blocks),
                   "combined_sha256": hashlib.sha256("".join(sha(p) for p in blocks).encode()).hexdigest()},
        "locked_test_answers": {rel(p): sha(p) for p in files(os.path.join(OUT, "locked", "*"))},
        "label_provenance": label_provenance(),
    }
    json.dump(manifest, open(os.path.join(dest, "manifest.json"), "w"), indent=1)
    for name in ("metrics.json", "report.md", "route_report.md", "route_ranker_v2.json", "scorecard.md", "scorecard.json"):
        if os.path.exists(os.path.join(OUT, name)):
            shutil.copy2(os.path.join(OUT, name), dest)
    print(f"Baseline snapshot written to {dest}")
    print(json.dumps({k: manifest[k] for k in ("environment", "blocks", "label_provenance")}, indent=1))


if __name__ == "__main__":
    main()
