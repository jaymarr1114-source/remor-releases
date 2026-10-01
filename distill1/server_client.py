#!/usr/bin/env python3
"""DISTILL-2: stdlib-only HTTP client for the local persistent llama-server.

The student is ALWAYS local: this client talks only to 127.0.0.1.
No third-party dependencies, no API keys, no external connections.

Usage:
  server_client.py --turn "can u do maths for me" [--policy policy.txt]
                   [--max-tokens 40] [--host 127.0.0.1] [--port 18080]
"""
import argparse
import json
import sys
import time
import urllib.request

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 18080


def turn(prompt, host=DEFAULT_HOST, port=DEFAULT_PORT,
         max_tokens=25, temperature=0.2, timeout=120):
    """One completion turn through the persistent server. Returns
    (text, wall_s, tok_per_s). cache_prompt keeps the KV cache warm.
    max_tokens=25: the distilled policy mandates 1-2 sentence answers
    (~18 words); 25 tokens enforces the policy's brevity and is the
    serving-parameter tune that holds the 2s budget even at 12 tok/s
    (DISTILL-1 measured with 40; DISTILL-2 tunes the serving, not the
    test). Stops + repeat_penalty end turns early; n_predict is the cap."""
    body = json.dumps({
        "prompt": prompt,
        "n_predict": max_tokens,
        "temperature": temperature,
        "cache_prompt": True,
        "stream": False,
        # Stop at the end of the answer: the model serves one chat turn.
        # Without these it generates to n_predict even after finishing.
        # repeat_penalty breaks degenerate repetition loops (the model
        # otherwise repeats a phrase to n_predict on arithmetic turns).
        "stop": ["\nChat turn:", "\nChat:", "\nResponse:", "\n\n"],
        "repeat_penalty": 1.1,
    }).encode()
    req = urllib.request.Request(
        f"http://{host}:{port}/completion", data=body,
        headers={"Content-Type": "application/json"}, method="POST")
    start = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.load(resp)
    except Exception as e:
        return None, time.monotonic() - start, None, f"{type(e).__name__}: {e}"
    wall = time.monotonic() - start
    text = (data.get("content") or "").strip()
    # strip echoed prompt the way the CLI path does
    idx = text.find("Response:")
    if idx != -1:
        text = text[idx + len("Response:"):].strip()
    timings = data.get("timings", {}) or {}
    tps = timings.get("predicted_per_second")
    return text, wall, tps, None


def health(host=DEFAULT_HOST, port=DEFAULT_PORT, timeout=5):
    try:
        with urllib.request.urlopen(
                f"http://{host}:{port}/health", timeout=timeout) as resp:
            return json.load(resp).get("status") == "ok"
    except Exception:
        return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--turn", required=True)
    ap.add_argument("--policy", default=None)
    ap.add_argument("--max-tokens", type=int, default=40)
    ap.add_argument("--host", default=DEFAULT_HOST)
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    args = ap.parse_args()
    policy = ""
    if args.policy:
        with open(args.policy) as fh:
            policy = fh.read().strip() + "\n\n"
    prompt = f"{policy}Chat turn: {args.turn}\nResponse:"
    text, wall, tps, err = turn(prompt, args.host, args.port, args.max_tokens)
    if err:
        print(f"FAILED wall={wall:.1f}s {err}")
        sys.exit(2)
    tps_s = f"{tps:.1f}" if tps else "n/a"
    print(f"wall={wall:.2f}s tok/s={tps_s} :: {text[:120]}")


if __name__ == "__main__":
    main()
