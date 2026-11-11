"""A local language model (Qwen through Ollama, free, on this PC) picks the safer route of each pair in ml/data/pairs.jsonl.
Each pair is asked twice with A and B swapped; only answers that agree both ways are kept. These labels are the model's
opinion, not measured safety: ml/rank.py checks them against people's answers before trusting them.

Usage: py ml/label_llm.py [--model auto|qwen3:4b|gpt-oss:120b-cloud] [--workers 6] [--limit 10000]
needs Ollama running. auto uses an Ollama cloud model when Ollama is signed in with cloud turned on (much faster), else qwen3:4b.
"""
import argparse
import csv
import json
import os
import sys
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pairs  # noqa: E402

OUT = os.path.join(os.path.dirname(pairs.OUT), "labels_llm.csv")
PROMPT = """A woman is walking alone in Delhi NCR at {hour}:00 on a {day}. Two routes go between the same two points.
Judge only from these measurements which route is safer for her. More open places, lighting, main roads with people,
CCTV and nearby police or hospitals usually help; long isolated or unlit stretches usually hurt; a much longer walk is also exposure.
{table}
Answer in JSON: {{"safer": "A" or "B" or "same", "reason": "one short sentence"}}"""


def prompt(p, first="A"):
    x, y = (p["A"], p["B"]) if first == "A" else (p["B"], p["A"])
    rows = "\n".join(f"{label}: A = {x[k]}, B = {y[k]}" for k, label in pairs.MEASURES.items())
    return PROMPT.format(hour=p["hour"], day="weekend" if p["weekend"] else "weekday", table=rows)


def ollama(text, model):
    body = json.dumps({"model": model, "messages": [{"role": "user", "content": text}], "stream": False, "format": "json",
                       "think": False, "options": {"temperature": 0, "num_ctx": 2048, "num_gpu": 99}}).encode()
    req = urllib.request.Request("http://localhost:11434/api/chat", body, {"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=300) as r:
        return json.load(r)["message"]["content"]


def judge(p, ask):
    """("A" | "B" | "same" | None, reason): None when the two orders disagree or an answer can't be read."""
    got = []
    for first in ("A", "B"):
        try:
            a = json.loads(ask(prompt(p, first)))
            s = str(a.get("safer", "")).strip().upper()
        except (ValueError, AttributeError):
            return None, "unreadable answer"
        if s not in ("A", "B", "SAME"):
            return None, "unreadable answer"
        got.append((s if first == "A" or s == "SAME" else "AB"[s == "A"], str(a.get("reason", ""))[:200]))  # undo the swap
    if got[0][0] != got[1][0]:
        return None, "changed its answer when A and B were swapped"
    return got[0][0].replace("SAME", "same"), got[0][1]


LOCAL = "qwen3:4b"
MAX_PROBES = 5  # cloud models tried per run; each probe is one tiny request
# Ollama's cloud models run on Ollama's servers once Ollama is signed in and cloud is turned on in its settings; only the route
# measures in the prompt leave this PC. Model names change, so the list is read from Ollama's public catalogue each run, never hard-coded.


def cloud_candidates():
    """Cloud model names to try, smallest first: from ollama.com's public model list ("name:cloud", or "name:tag-cloud")."""
    try:
        with urllib.request.urlopen("https://ollama.com/api/tags", timeout=10) as r:
            models = json.load(r)["models"]
    except Exception as err:  # offline or the catalogue moved: no cloud this run
        print(f"could not read Ollama's cloud model list ({str(err)[:60]})", flush=True)
        return []
    names = [m["name"] for m in sorted(models, key=lambda m: m.get("size") or 0) if m.get("size")]
    return [n + "-cloud" if ":" in n else n + ":cloud" for n in names]


def pick_model():
    """The model to label with: $SAFEROUTE_LLM if set, else the first Ollama cloud model that answers, else local Qwen.
    HTTP 402 (the account has no cloud access on its plan, or the free allowance is used up) applies to every cloud model,
    so the first 402 ends the search; 404/410 (name retired or unknown) moves on to the next name."""
    names = ([os.environ["SAFEROUTE_LLM"]] if os.environ.get("SAFEROUTE_LLM") else []) + cloud_candidates()
    for m in names[:MAX_PROBES + 1]:
        try:
            ollama('Reply in JSON: {"ok": true}', m)
            return m
        except urllib.error.HTTPError as err:
            print(f"{m} not available (HTTP {err.code})", flush=True)
            if err.code == 402:
                print("Ollama cloud is not usable on this account right now (402: plan or free allowance). Using local Qwen.", flush=True)
                break
        except Exception as err:  # 403 cloud turned off, offline, timeout: try the next name
            print(f"{m} not available ({str(err)[:80]})", flush=True)
    return LOCAL


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="auto", help="an Ollama model name, or auto (cloud model if available, else qwen3:4b)")
    ap.add_argument("--workers", type=int, default=0, help="pairs labelled at once; default 6 for a cloud model, 1 on this PC")
    ap.add_argument("--limit", type=int, default=10000)
    args = ap.parse_args()
    done = {int(r["id"]) for r in csv.DictReader(open(OUT))} if os.path.exists(OUT) else set()
    every = pairs.load()
    if not every:
        sys.exit(f"No route pairs yet ({pairs.OUT} is empty or missing): run 'Make route pairs' first. Nothing was labelled.")
    todo = [p for p in every if p["id"] not in done][:args.limit]
    model = pick_model() if args.model == "auto" and todo else (LOCAL if args.model == "auto" else args.model)
    workers = args.workers or (6 if model.endswith("-cloud") else 1)
    print(f"{len(todo)} pairs to label with {model} ({workers} at a time), {len(done)} already done", flush=True)
    new, usable = not os.path.exists(OUT), 0

    def one(p):
        try:
            return p, judge(p, lambda t: ollama(t, model))
        except Exception as err:  # a timeout or rate limit on one pair must not stop the rest; it is retried next run
            return p, ("skip", f"request failed: {str(err)[:120]}")

    with open(OUT, "a", newline="", encoding="utf-8") as f, ThreadPoolExecutor(workers) as pool:
        w = csv.writer(f)
        if new:
            w.writerow(["id", "safer", "reason", "model"])
        for n, (p, (safer, reason)) in enumerate(pool.map(one, todo), 1):
            if safer != "skip":  # failed requests are not written, so the pair is asked again next run
                w.writerow([p["id"], safer or "", reason, model])
                f.flush()
                usable += safer is not None
            if n % 25 == 0:
                print(f"{n} of {len(todo)}", flush=True)
    print(f"Labelled {usable} of {len(todo)} pairs this run ({len(todo) - usable} unusable: unreadable or changed with the order)", flush=True)
    if todo and not usable:
        sys.exit("Qwen gave no usable answer for any pair: check the model and the prompt.")


if __name__ == "__main__":
    main()
