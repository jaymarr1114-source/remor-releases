"""P2: real inference through the substrate.

One REAL Qwen3 completion through LLMSubstrate.run():
  * the result satisfies the substrate result contract
    (implementation/entrypoint/technique/params/claimed_capabilities/
    measurements/notes, implementation is str),
  * provenance is exactly borrowed:qwen3@<pinned-rev>,
  * the grant was really consumed (consumed_s > 0),
  * latency is measured and reported honestly.

This is the slow probe (~2-4 min on this host, contention with
DISTILL-1's parallel inference noted in the report).
"""
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from common import (  # noqa: E402
    PASS, FAIL, check, report, real_wiring,
    QWEN3_REV, EXPECTED_PROVENANCE_PREFIX,
)

REQUIRED = ("implementation", "entrypoint", "technique", "params",
            "measurements", "notes")


def main():
    wiring = real_wiring()
    sub = wiring.build_substrate()
    task = {
        "objective": "write a python function add(a, b) returning a + b",
        "params": {"language": "python"},
        "entrypoint": "add",
    }
    t0 = time.monotonic()
    result = sub.run(task)
    wall = time.monotonic() - t0
    print(f"    [real inference wall: {wall:.1f}s]")

    check("P2", "result_is_dict", isinstance(result, dict))
    missing = [k for k in REQUIRED if k not in result]
    check("P2", "result_contract_keys", not missing, f"missing {missing}")
    check("P2", "implementation_is_str",
          isinstance(result.get("implementation"), str))
    check("P2", "implementation_nonempty",
          len(result.get("implementation") or "") > 0)
    prov = result.get("provenance", "")
    check("P2", "provenance_borrowed_qwen3",
          prov.startswith(EXPECTED_PROVENANCE_PREFIX), prov)
    check("P2", "provenance_pinned_revision",
          prov == f"borrowed:qwen3@{QWEN3_REV}", prov)
    meas = result.get("measurements", {})
    check("P2", "grant_consumed_positive",
          meas.get("grant_consumed_s", 0) > 0, str(meas))
    check("P2", "latency_measured_positive",
          meas.get("cognition_latency_s", 0) > 0, str(meas))
    check("P2", "latency_consistent_with_wall",
          abs(meas.get("cognition_latency_s", 0) - wall) < 30.0,
          f"measured {meas.get('cognition_latency_s')} vs wall {wall:.1f}")
    check("P2", "native_refusal_named",
          meas.get("native_refusal") == "agent_org:no_native_reasoning_tier",
          str(meas.get("native_refusal")))
    check("P2", "claimed_capabilities_empty_list",
          result.get("claimed_capabilities") == [],
          str(result.get("claimed_capabilities")))
    # The model's text is genuinely the model's: not a stub marker.
    check("P2", "not_stub_text",
          "STUB-TEACHER" not in (result.get("implementation") or ""),
          (result.get("implementation") or "")[:60])

    ok = report("P2")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
