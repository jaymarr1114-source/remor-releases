"""CUR-P1C proof: FRM evaluation layer under adversarial demand.

Proves, against the REAL FinancialResourceManager, the REAL
ResourceArbitrator instance it owns (the same class the Primary path
uses -- second instance, never a second class), and the REAL
append-only epoch ledger (fake clock injected -- a supported API, not
a mock):

  B1  curiosity demand spike cannot breach primary's guaranteed minimum
  B2  HARD_SHUTDOWN_RESOURCE -> zero curiosity allocation, demand or not
  B3  SUSPENDED_SAFETY       -> zero curiosity allocation, demand or not
  B4  BANNED_6M              -> zero curiosity allocation, demand or not
  B5  WARNING_1 restricts curiosity to James's standing policy fraction
  B6  lending is epoch-bounded: lent capacity recalled at the boundary
  B7  mid-epoch demand cannot shrink a running grant (non-preemption;
      grants are frozen dataclasses; refused demand is recorded)
  B8  primary minimum above capacity is a loud configuration error
  B9  expensive-model request outside standing policy is refused;
      an authorized model passes
  B10 cost inputs keep provenance; UNPRICED handled without a crash
  B11 every grant matches the frozen grant shape exactly
  B12 end-to-end: demand -> FRM -> arbitrator -> grant -> real spending,
      with refusal past the grant (the grant is a real ceiling)
  B13 expected-yield estimates are provisional and non-binding
  B14 unknown enforcement state is rejected loudly
  B15 the FRM owns a second arbitrator INSTANCE of the shared class

Exits nonzero on the first failed check. Prints N/N at the end.
"""
import os
import sys

sys.path.insert(0, os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "pylib")))

from swarm_engine.curiosity.frm.proof_battery import run_all


def main() -> None:
    results = run_all()
    passed = 0
    for name, ok, detail in results:
        status = "PASS" if ok else "FAIL"
        print(f"[{status}] {name}" + ("" if ok else f" -- {detail}"))
        if not ok:
            raise SystemExit(f"PROOF FAILED at: {name} -- {detail}")
        passed += 1
    print(f"\nCUR-P1C proof: {passed}/{len(results)} checks green")


if __name__ == "__main__":
    main()
