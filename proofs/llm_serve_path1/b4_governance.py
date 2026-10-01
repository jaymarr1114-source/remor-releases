#!/usr/bin/env python3
"""LLM-SERVE-PATH-1 b4: governance (charging, provenance, telemetry).

After a real completion, verify:
  * the grant was charged (consumed > 0)
  * provenance is borrowed:qwen3@<rev>
  * telemetry recorded the borrow event (queryable by purpose)
"""
import sys
import time

WT = "/home/hatch/workspace/worktrees/llm-serve-path-1"
sys.path.insert(0, WT)
sys.path.insert(0, WT + "/pylib")

from runtime.services.agent_api import build_llm_wiring

def main() -> int:
    wiring = build_llm_wiring()
    provider = wiring.provider
    passed = 0
    total = 4

    grant = wiring.grant_issuer(400.0)
    gid = grant.grant_id
    res = provider.request_cognition(
        mc_id=wiring.mc_id, prompt="What is 3+3? Reply with just the number.",
        context={"frm_grant": grant, "purpose": "b4-governance",
                 "target_profile": "bench"})
    if not res.ok:
        print(f"[b4] FAIL: completion failed: {res.error}")
        return 1

    # 1. provenance
    ok = res.provenance.startswith("borrowed:qwen3@")
    print(f"[b4] provenance={res.provenance[:30]} -> {'PASS' if ok else 'FAIL'}")
    passed += ok

    # 2. telemetry has the event
    events = [e for e in provider._telemetry._events
              if e.purpose == "b4-governance"]
    ok = len(events) >= 1 and events[-1].outcome == "borrowed"
    print(f"[b4] telemetry events={len(events)} outcome={events[-1].outcome if events else '?'} "
          f"-> {'PASS' if ok else 'FAIL'}")
    passed += ok

    # 3. charged seconds recorded
    ok = bool(events and events[-1].charged_s > 0)
    print(f"[b4] charged_s={events[-1].charged_s if events else 0:.1f} "
          f"-> {'PASS' if ok else 'FAIL'}")
    passed += ok

    # 4. result_id links telemetry to result
    ok = bool(events and events[-1].result_id == res.result_id and res.result_id != "")
    print(f"[b4] result_id linked -> {'PASS' if ok else 'FAIL'}")
    passed += ok

    print(f"[b4] PASS={passed} FAIL={total-passed}")
    return 0 if passed == total else 1

if __name__ == "__main__":
    sys.exit(main())
