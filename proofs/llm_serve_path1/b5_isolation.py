#!/usr/bin/env python3
"""LLM-SERVE-PATH-1 b5: sequential isolation.

Two sequential completions through the same wiring must not leak state:
different result_ids, independent grants, second call unaffected by first.
"""
import sys
import time

WT = "/home/hatch/workspace/worktrees/llm-serve-path-1"
sys.path.insert(0, WT)
sys.path.insert(0, WT + "/pylib")

from runtime.services.agent_api import build_llm_wiring

def main() -> int:
    wiring = build_llm_wiring()
    passed = 0
    total = 3

    g1 = wiring.grant_issuer(400.0)
    r1 = wiring.provider.request_cognition(
        mc_id=wiring.mc_id, prompt="What is 5+5? Reply with just the number.",
        context={"frm_grant": g1, "purpose": "b5-isolation",
                 "target_profile": "bench"})
    g2 = wiring.grant_issuer(400.0)
    r2 = wiring.provider.request_cognition(
        mc_id=wiring.mc_id, prompt="What is 6+6? Reply with just the number.",
        context={"frm_grant": g2, "purpose": "b5-isolation",
                 "target_profile": "bench"})

    ok = bool(r1.ok and r2.ok)
    print(f"[b5] both ok: {'PASS' if ok else 'FAIL'}")
    passed += ok
    if not ok:
        print(f"[b5] r1={r1.error[:50]} r2={r2.error[:50]}")
        return 1

    ok = bool(r1.result_id != r2.result_id and r1.result_id and r2.result_id)
    print(f"[b5] distinct result_ids: {'PASS' if ok else 'FAIL'}")
    passed += ok

    ok = bool("10" in r1.text and "12" in r2.text
              and g1.grant_id != g2.grant_id)
    print(f"[b5] correct+independent (r1={r1.text[:10]!r} r2={r2.text[:10]!r}): "
          f"{'PASS' if ok else 'FAIL'}")
    passed += ok

    print(f"[b5] PASS={passed} FAIL={total-passed}")
    return 0 if passed == total else 1

if __name__ == "__main__":
    sys.exit(main())
