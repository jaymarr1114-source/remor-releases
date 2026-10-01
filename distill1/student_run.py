#!/usr/bin/env python3
"""DISTILL-1: run the student (small local GGUF +/- distilled technique pack)
on a turn set. The student is ALWAYS local: llama-cli on a GGUF on disk.

Usage:
  student_run.py --gguf <path> --turns turns_heldout.json --out out.json
                 [--policy policy.txt] [--max-tokens 40]

--policy: text file rendered as the system preamble (the distilled
technique pack). Omit for the stock base model baseline.
Output: JSON list of {turn_id, turn_class, turn_text, response, wall_s}.
"""
import argparse
import json
import os
import subprocess
import sys
import time

LLAMA_CLI = "/home/hatch/workspace/tools/llama.cpp-b11284/llama-b11284/llama-cli"


def extract(stdout):
    idx = stdout.rfind("\n> ")
    if idx == -1:
        tail = stdout.strip()
    else:
        tail = stdout[idx + 3:]
        nl = tail.find("\n")
        tail = tail[nl + 1:].strip() if nl != -1 else ""
    for marker in ("\n[ Prompt:", "\nExiting..."):
        cut = tail.find(marker)
        if cut != -1:
            tail = tail[:cut]
    tail = tail.strip()
    # Strip echoed prompt: "Chat turn: ... Response:" or leading "Response:"
    resp_idx = tail.find("Response:")
    if resp_idx != -1:
        tail = tail[resp_idx + len("Response:"):].strip()
    elif tail.startswith("Response:"):
        tail = tail[len("Response:"):].strip()
    return tail


def run_turn(gguf, prompt, max_tokens):
    cmd = [LLAMA_CLI, "-m", gguf, "-p", prompt, "-n", str(max_tokens),
           "-c", "512", "-t", "2", "--no-display-prompt",
           "--temp", "0.2", "--log-disable", "--single-turn",
           "--reasoning", "off"]
    start = time.monotonic()
    proc = subprocess.run(cmd, capture_output=True, text=True,
                          timeout=600, stdin=subprocess.DEVNULL)
    wall = time.monotonic() - start
    if proc.returncode != 0:
        raise RuntimeError(f"llama-cli exit {proc.returncode}: "
                           f"{proc.stderr[-300:]}")
    text = extract(proc.stdout)
    if not text:
        raise RuntimeError("empty completion")
    return text, wall


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--gguf", required=True)
    ap.add_argument("--turns", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--policy", default=None)
    ap.add_argument("--max-tokens", type=int, default=40)
    args = ap.parse_args()

    policy = ""
    if args.policy:
        with open(args.policy) as fh:
            policy = fh.read().strip() + "\n\n"
    with open(args.turns) as fh:
        turns = json.load(fh)["turns"]

    results = []
    for t in turns:
        prompt = f"{policy}Chat turn: {t['text']}\nResponse:" if policy else (
            f"Chat turn: {t['text']}\nResponse:")
        text, wall = run_turn(args.gguf, prompt, args.max_tokens)
        results.append({"turn_id": t["id"], "turn_class": t["class"],
                        "turn_text": t["text"], "response": text,
                        "wall_s": round(wall, 2)})
        print(f"{t['id']}: {wall:.1f}s :: {text[:70]}", flush=True)
    with open(args.out, "w") as fh:
        json.dump(results, fh, indent=1)
    mean = sum(r["wall_s"] for r in results) / len(results)
    print(f"wrote {args.out}: {len(results)} turns, mean {mean:.2f}s")


if __name__ == "__main__":
    main()
