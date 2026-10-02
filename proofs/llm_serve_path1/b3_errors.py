#!/usr/bin/env python3
"""LLM-SERVE-PATH-1 b3: error behavior (no inference needed).

The wiring must fail honestly BEFORE any model call:
  * no grant -> refused with cognition_refused:no_grant
  * wrong grant type -> refused with cognition_refused:grant_not_frmgrant
  * insufficient grant -> deferred with cognition_deferred:insufficient_grant
None of these may touch the teacher (no inference, no charge).
"""
import sys

WT = "/home/hatch/workspace/worktrees/llm-serve-path-1"
sys.path.insert(0, WT)
sys.path.insert(0, WT + "/pylib")

from runtime.services.agent_api import build_llm_wiring
from swarm_engine.curiosity.frm.grant import FrmGrant, LendingRecord
import time

def main() -> int:
    wiring = build_llm_wiring()
    passed = 0
    total = 0

    # 1. no grant
    total += 1
    r = wiring.provider.request_cognition(
        mc_id=wiring.mc_id, prompt="hi",
        context={"purpose": "b3", "target_profile": "bench"})
    ok = (not r.ok and r.error.startswith("cognition_refused:no_grant"))
    print(f"[b3] no-grant -> refused: {'PASS' if ok else 'FAIL'} ({r.error[:60]})")
    passed += ok

    # 2. wrong grant type
    total += 1
    r = wiring.provider.request_cognition(
        mc_id=wiring.mc_id, prompt="hi",
        context={"frm_grant": "not-a-grant", "purpose": "b3",
                 "target_profile": "bench"})
    ok = (not r.ok and r.error.startswith("cognition_refused:grant_not_frmgrant"))
    print(f"[b3] bad-grant -> refused: {'PASS' if ok else 'FAIL'} ({r.error[:60]})")
    passed += ok

    # 3. insufficient grant
    total += 1
    tiny = FrmGrant.issue(
        domain="b3", epoch_id=int(time.time()), epoch_s=3600.0,
        budget_s=1.0, max_concurrent=1,
        primary_minimum_budget_s=0.0, primary_minimum_concurrent=0,
        lent=False, lending=LendingRecord(0.0, 0),
        enforcement_state_at_issue="RUNNING", issued_at=time.time(),
        note="b3 insufficient")
    r = wiring.provider.request_cognition(
        mc_id=wiring.mc_id, prompt="hi",
        context={"frm_grant": tiny, "purpose": "b3",
                 "target_profile": "bench"})
    ok = (not r.ok and r.error.startswith("cognition_deferred:insufficient_grant"))
    print(f"[b3] insufficient -> deferred: {'PASS' if ok else 'FAIL'} ({r.error[:60]})")
    passed += ok

    print(f"[b3] PASS={passed} FAIL={total-passed}")
    return 0 if passed == total else 1

if __name__ == "__main__":
    sys.exit(main())
