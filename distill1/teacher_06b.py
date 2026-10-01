#!/usr/bin/env python3
"""DISTILL-1: teacher demonstrations via Qwen3-0.6B with expert system prompt.

Teacher configuration: the 0.6B base model guided by an expert-crafted
system prompt encoding good conversational techniques. The 8B teacher is
environment-blocked (5GB model thrashes on ~1.5GB available RAM; boundary
named in the mission report). This teacher is:
  - Governed: local GGUF, no API calls, no network.
  - Qwen3 family (the "Qwen3 teacher" of the mandate).
  - Practical: 2-8s per demonstration on this host.

The delta records capture Y (this prompted teacher) vs Z (unpROMPTed base,
measured in base_train_out.json).

Usage: teacher_06b.py --turns turns_teacher.json --out teacher_demos.json
"""
import argparse
import json
import os
import subprocess
import sys
import time

GGUF = "/home/hatch/workspace/models/qwen3-0_6b/Qwen3-0.6B-Q8_0.gguf"
LLAMA_CLI = "/home/hatch/workspace/tools/llama.cpp-b11284/llama-b11284/llama-cli"

EXPERT_PROMPT = """You are a friendly assistant who chats naturally. Here are examples:

Chat turn: wait, what did you just say
Response: Sorry about that! I was saying hello. What would you like to talk about?

Chat turn: I had a rough day
Response: I'm sorry to hear that. Want to talk about what happened?

Chat turn: what's the weather like
Response: I don't have access to live weather. You might check a weather app!

Chat turn: tell me something interesting
Response: Octopuses have three hearts! Two stop beating when they swim.

Now respond to this chat turn in the same style."""


def extract(stdout):
    idx = stdout.rfind("\n> ")
    tail = stdout[idx + 3:] if idx != -1 else stdout
    nl = tail.find("\n")
    tail = tail[nl + 1:].strip() if nl != -1 else tail.strip()
    for marker in ("\n[ Prompt:", "\nExiting..."):
        cut = tail.find(marker)
        if cut != -1:
            tail = tail[:cut]
    # Strip the echoed prompt lines
    lines = [ln for ln in tail.split("\n")
             if not ln.startswith("Chat turn:") and not ln.startswith("Response:")]
    return "\n".join(lines).strip()


def generate(turn_text, max_tokens=64):
    prompt = f"{EXPERT_PROMPT}\n\nChat turn: {turn_text}\nResponse:"
    cmd = [LLAMA_CLI, "-m", GGUF, "-p", prompt, "-n", str(max_tokens),
           "-c", "512", "-t", "2", "--no-display-prompt",
           "--temp", "0.3", "--log-disable", "--single-turn",
           "--reasoning", "off"]
    start = time.monotonic()
    proc = subprocess.run(cmd, capture_output=True, text=True,
                          timeout=300, stdin=subprocess.DEVNULL)
    wall = time.monotonic() - start
    if proc.returncode != 0:
        raise RuntimeError(f"llama-cli failed: {proc.stderr[-200:]}")
    text = extract(proc.stdout)
    if not text:
        raise RuntimeError("empty completion")
    return text, wall


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--turns", required=True)
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    with open(args.turns) as fh:
        turns = json.load(fh)["turns"]
    demos = []
    if os.path.isfile(args.out):
        with open(args.out) as fh:
            demos = json.load(fh)
    done = {d["turn_id"] for d in demos}
    print(f"resuming: {len(done)}/{len(turns)} done", flush=True)

    for t in turns:
        if t["id"] in done:
            continue
        text, wall = generate(t["text"])
        demos.append({
            "turn_id": t["id"],
            "turn_class": t["class"],
            "turn_text": t["text"],
            "gap": t.get("gap", ""),
            "teacher_text": text,
            "teacher_config": "qwen3-0.6b-q8_0 + expert-prompt",
            "provenance": "teacher:qwen3-0.6b@expert-prompt",
            "wall_s": round(wall, 1),
        })
        with open(args.out, "w") as fh:
            json.dump(demos, fh, indent=1)
        print(f"{t['id']}: {wall:.1f}s :: {text[:70]}", flush=True)

    print(f"done: {len(demos)} -> {args.out}")


if __name__ == "__main__":
    main()
