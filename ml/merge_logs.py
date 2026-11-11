"""Adds the logger files copied from the server (ml/data/server/) into ml/data/, keeping every row once.
Usage: py ml/merge_logs.py   (pull-logs.bat runs it after copying the files)"""
import os

DATA = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
NAMES = ["bus_counts.csv", "bus_hours.csv", "traffic.csv"]


def merge(ours, theirs):
    """Rows of both files, each once, ours first; one header. Returns how many rows were new."""
    if not os.path.exists(theirs):
        return 0
    old = open(ours, encoding="utf-8").read().splitlines() if os.path.exists(ours) else []
    new = open(theirs, encoding="utf-8").read().splitlines()
    if not new:
        return 0
    header, seen = (old or new)[0], set(old)
    add = [r for r in new[1:] if r and r not in seen and not seen.add(r)]
    tmp = ours + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join((old or [header]) + add) + "\n")
    os.replace(tmp, ours)  # never leaves a half-written log behind
    return len(add)


if __name__ == "__main__":
    for n in NAMES:
        print(f"{n}: {merge(os.path.join(DATA, n), os.path.join(DATA, 'server', n))} new rows from the server")
