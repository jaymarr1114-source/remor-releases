#!/usr/bin/env python3
"""LLM-SERVE-PATH-1 b2: latency envelope.

Measures real completion latencies and checks them against the declared
production envelope. The envelope is honest about this host: the wiring
is sufficient for deferred/background serving, not interactive chat.

ENVELOPE (reference host, 2-core, Qwen3-8B Q4_K_M via llama-cli):
  * single completion p95 < 600s
  * every completion < 900s (hard ceiling; above this the wiring is
    not production-usable even deferred)

Reads b1's timings from its output log when present, else runs one
fresh completion to measure.
"""
import re
import sys
import time

WT = "/home/hatch/workspace/worktrees/llm-serve-path-1"
sys.path.insert(0, WT)
sys.path.insert(0, WT + "/pylib")

ENVELOPE_P95_S = 600.0
ENVELOPE_HARD_S = 900.0

def main() -> int:
    latencies = []
    # try to reuse b1's measured latencies from its log
    try:
        with open(WT + "/proofs/llm_serve_path1/b1_run.out") as f:
            for line in f:
                m = re.search(r"latency=([\d.]+)s", line)
                if m:
                    latencies.append(float(m.group(1)))
    except FileNotFoundError:
        pass
    if not latencies:
        from runtime.services.agent_api import build_llm_wiring
        wiring = build_llm_wiring()
        grant = wiring.grant_issuer(400.0)
        t0 = time.monotonic()
        res = wiring.provider.request_cognition(
            mc_id=wiring.mc_id, prompt="Say hello.",
            context={"frm_grant": grant, "purpose": "b2-latency",
                     "target_profile": "bench"})
        dt = time.monotonic() - t0
        if not res.ok:
            print(f"[b2] FAIL: completion failed: {res.error}")
            return 1
        latencies.append(dt)
    latencies.sort()
    p95 = latencies[int(0.95 * (len(latencies) - 1))]
    mx = latencies[-1]
    ok = p95 < ENVELOPE_P95_S and mx < ENVELOPE_HARD_S
    print(f"[b2] n={len(latencies)} latencies={[f'{x:.1f}s' for x in latencies]}")
    print(f"[b2] p95={p95:.1f}s (envelope <{ENVELOPE_P95_S:.0f}s) "
          f"max={mx:.1f}s (hard <{ENVELOPE_HARD_S:.0f}s) "
          f"-> {'PASS' if ok else 'FAIL'}")
    return 0 if ok else 1

if __name__ == "__main__":
    sys.exit(main())
