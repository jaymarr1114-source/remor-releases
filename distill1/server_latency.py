#!/usr/bin/env python3
"""DISTILL-2: latency measurement through the persistent llama-server.

Same methodology as DISTILL-1's latency_harness.py (short chat turns,
40 max tokens, temp 0.2, per-turn wall seconds, verdict against the 2s
budget) but turns go through the resident server instead of a fresh
llama-cli process per turn.

Usage:
  server_latency.py --turns distill1/turns_heldout.json [--policy distill1/policy.txt]
                    [--out distill1/server_latency.json]

Output: per-turn wall seconds, tok/s, mean/max, verdict vs budget.
"""
import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from server_client import turn, health

BUDGET_S = 2.0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--turns", required=True)
    ap.add_argument("--policy", default=None)
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    if not health():
        print("server not healthy at 127.0.0.1:18080 — start it first")
        sys.exit(2)

    policy = ""
    if args.policy:
        with open(args.policy) as fh:
            policy = fh.read().strip() + "\n\n"
    with open(args.turns) as fh:
        turns = json.load(fh)["turns"]

    results = []
    for t in turns:
        prompt = f"{policy}Chat turn: {t['text']}\nResponse:"
        text, wall, tps, err = turn(prompt)
        if err or not text:
            print(f"{t['id']}: FAILED wall={wall:.1f}s {err or 'empty'}")
            sys.exit(2)
        tps_s = f"{tps:.1f}" if tps else "n/a"
        results.append({"turn_id": t["id"], "turn_class": t["class"],
                        "turn_text": t["text"], "response": text,
                        "wall_s": round(wall, 2), "tok_per_s": tps})
        print(f"{t['id']}: wall={wall:.2f}s tok/s={tps_s} :: {text[:70]}",
              flush=True)

    walls = [r["wall_s"] for r in results]
    mean, mx = sum(walls) / len(walls), max(walls)
    verdict = "WITHIN BUDGET" if mx <= BUDGET_S else "OVER BUDGET"
    print(f"n={len(results)} mean_wall={mean:.2f}s max_wall={mx:.2f}s "
          f"-> {verdict}")
    if args.out:
        with open(args.out, "w") as fh:
            json.dump({"measured_at": time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                                    time.gmtime()),
                       "budget_s": BUDGET_S, "mean_wall_s": round(mean, 2),
                       "max_wall_s": round(mx, 2), "verdict": verdict,
                       "turns": results}, fh, indent=1)
        print(f"wrote {args.out}")
    sys.exit(0 if mx <= BUDGET_S else 1)


if __name__ == "__main__":
    main()
