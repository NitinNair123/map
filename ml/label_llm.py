"""A local language model (Qwen through Ollama, free, on this PC) picks the safer route of each pair in ml/data/pairs.jsonl.
Each pair is asked twice with A and B swapped; only answers that agree both ways are kept. These labels are the model's
opinion, not measured safety: ml/rank.py checks them against people's answers before trusting them.

Usage: py ml/label_llm.py [--model qwen3:4b] [--limit 10000]     needs Ollama running (ollama pull qwen3:4b)
"""
import argparse
import csv
import json
import os
import sys
import urllib.request

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
                       "think": False, "options": {"temperature": 0, "num_ctx": 2048}}).encode()
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


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="qwen3:4b")
    ap.add_argument("--limit", type=int, default=10000)
    args = ap.parse_args()
    done = {int(r["id"]) for r in csv.DictReader(open(OUT))} if os.path.exists(OUT) else set()
    todo = [p for p in pairs.load() if p["id"] not in done][:args.limit]
    print(f"{len(todo)} pairs to label with {args.model}, {len(done)} already done", flush=True)
    new = not os.path.exists(OUT)
    with open(OUT, "a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if new:
            w.writerow(["id", "safer", "reason", "model"])
        for n, p in enumerate(todo, 1):
            safer, reason = judge(p, lambda t: ollama(t, args.model))
            w.writerow([p["id"], safer or "", reason, args.model])
            f.flush()
            if n % 25 == 0:
                print(f"{n} of {len(todo)}", flush=True)


if __name__ == "__main__":
    main()
