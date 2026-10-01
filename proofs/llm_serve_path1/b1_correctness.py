#!/usr/bin/env python3
"""LLM-SERVE-PATH-1 b1: correctness of the existing LLM wiring.

Three real completions on fixed prompts via build_llm_wiring().
Each must: return ok=True, contain the expected answer, carry
borrowed:qwen3 provenance, and not be stub text.
"""
import sys
import time

WT = "/home/hatch/workspace/worktrees/llm-serve-path-1"
sys.path.insert(0, WT)
sys.path.insert(0, WT + "/pylib")

from runtime.services.agent_api import build_llm_wiring

CASES = [
    ("What is 2+2? Reply with just the number.", "4"),
    ("What is the capital of France? Reply with just the city name.", "Paris"),
    ("Spell the word 'cat' backwards. Reply with just the result.", "tac"),
]

def main() -> int:
    wiring = build_llm_wiring()
    passed = 0
    for prompt, expected in CASES:
        grant = wiring.grant_issuer(400.0)
        t0 = time.monotonic()
        res = wiring.provider.request_cognition(
            mc_id=wiring.mc_id, prompt=prompt,
            context={"frm_grant": grant, "purpose": "b1-correctness",
                     "target_profile": "bench"})
        dt = time.monotonic() - t0
        ok = (res.ok and expected.lower() in res.text.lower()
              and res.provenance.startswith("borrowed:qwen3@")
              and "STUB-TEACHER" not in res.text)
        print(f"[b1] prompt={prompt[:30]!r} ok={res.ok} "
              f"latency={dt:.1f}s provenance={res.provenance[:20]} "
              f"text={res.text[:40]!r} -> {'PASS' if ok else 'FAIL'}")
        if ok:
            passed += 1
    print(f"[b1] PASS={passed} FAIL={len(CASES)-passed}")
    return 0 if passed == len(CASES) else 1

if __name__ == "__main__":
    sys.exit(main())
