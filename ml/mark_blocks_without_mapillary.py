"""Deletes the done-marker (and block files) of every saved block whose 'sources' column does not include Mapillary,
so the next `ml/dataset.py --all` run rebuilds just those blocks with Mapillary. Run with the build stopped.
Usage: py -I mark_blocks_without_mapillary.py <path to ml/data/blocks> [--dry]"""
import glob
import gzip
import csv
import os
import sys

folder, dry = sys.argv[1], "--dry" in sys.argv
redo = keep = 0
for edges in sorted(glob.glob(os.path.join(folder, "*.edges.csv.gz"))):
    with gzip.open(edges, "rt", newline="") as f:
        row = next(csv.DictReader(f), None)
    if row is None or "Mapillary" in row.get("sources", ""):
        keep += 1
        continue
    redo += 1
    if not dry:
        stem = edges[:-len(".edges.csv.gz")]
        for part in (".slots.csv.gz", ".edges.csv.gz"):
            if os.path.exists(stem + part):
                os.remove(stem + part)
print(f"{redo} blocks without Mapillary {'would be ' if dry else ''}removed for rebuilding, {keep} kept")
