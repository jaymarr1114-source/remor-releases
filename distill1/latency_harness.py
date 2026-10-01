#!/usr/bin/env python3
"""DISTILL-1 mandate 1: latency harness. Measures a short chat turn for a
candidate GGUF on THIS host. The student choice follows from measurements.

Usage:
  latency_harness.py --gguf <path> --prompt "hi there" --reps 3 [--max-tokens 40]

Output: per-rep wall seconds, llama.cpp-reported tok/s, and the verdict
against the ~2s budget. No assertions about untested architectures.
"""
import argparse
import re
import subprocess
import sys
import time

LLAMA_CLI = "/home/hatch/workspace/tools/llama.cpp-b11284/llama-b11284/llama-cli"
BUDGET_S = 2.0


def run_turn(gguf, prompt, max_tokens, threads=2):
    cmd = [LLAMA_CLI, "-m", gguf, "-p", prompt, "-n", str(max_tokens),
           "-c", "512", "-t", str(threads), "--no-display-prompt",
           "--temp", "0.2", "--log-disable", "--single-turn",
           "--reasoning", "off"]
    start = time.monotonic()
    proc = subprocess.run(cmd, capture_output=True, text=True,
                          timeout=900, stdin=subprocess.DEVNULL)
    wall = time.monotonic() - start
    if proc.returncode != 0:
        return {"ok": False, "wall_s": wall,
                "error": proc.stderr[-300:]}
    # llama.cpp prints a timing footer like "Generation: 12.3 t/s"
    m = re.search(r"Generation:\s*([\d.]+)\s*t/s", proc.stdout)
    toks = float(m.group(1)) if m else None
    return {"ok": True, "wall_s": wall, "tok_per_s": toks}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--gguf", required=True)
    ap.add_argument("--prompt", required=True)
    ap.add_argument("--reps", type=int, default=3)
    ap.add_argument("--max-tokens", type=int, default=40)
    args = ap.parse_args()

    print(f"model: {args.gguf}")
    print(f"prompt: {args.prompt!r}  max_tokens={args.max_tokens} "
          f"reps={args.reps} budget={BUDGET_S}s")
    walls = []
    for i in range(args.reps):
        r = run_turn(args.gguf, args.prompt, args.max_tokens)
        if not r["ok"]:
            print(f"rep {i}: FAILED wall={r['wall_s']:.1f}s {r['error']}")
            sys.exit(2)
        walls.append(r["wall_s"])
        print(f"rep {i}: wall={r['wall_s']:.2f}s tok/s={r['tok_per_s']}")
    mean = sum(walls) / len(walls)
    mx = max(walls)
    verdict = "WITHIN BUDGET" if mx <= BUDGET_S else "OVER BUDGET"
    print(f"mean_wall={mean:.2f}s max_wall={mx:.2f}s -> {verdict}")
    sys.exit(0 if mx <= BUDGET_S else 1)


if __name__ == "__main__":
    main()
