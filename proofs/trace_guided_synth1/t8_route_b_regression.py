"""t8: Route B regression — fresh synthesis still works after the refactor.

compute_sum (BORROW-NATIVE-1 probe-7 pattern): a delta WITHOUT traces must
take route fresh-synthesis and succeed, exactly as before.
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _tg_common as C

SEC = "t8"
os.environ["REMOR_TEST_MODE"] = "1"


def main():
    from runtime.core.engine import SwarmEngine
    from swarm_engine.acquisition.delta import DeltaRecord
    from swarm_engine.acquisition.distill import DistillationLoop

    db = os.path.join(C.PROOF_DIR, "_t8_routeb.db")
    for f in (db, db + ".oracle.db"):
        if os.path.exists(f):
            os.remove(f)
    eng = SwarmEngine(db_path=db, _test_allow_shared=True)

    pairs = [(2, 3), (5, 7), (10, 20), (1, 1), (15, 25), (3, 8),
             (100, 200), (33, 44), (7, 9), (12, 30), (8, 8), (21, 34)]
    evidence = [{"input": {"a": a, "b": b}, "output": a + b} for a, b in pairs]
    delta = DeltaRecord(
        objective="compute the sum of two numbers",
        external_actions="demonstrated addition",
        prior_capability="none",
        capability_gap="cannot add",
        technique="addition",
        evidence=evidence,
        source="trace-guided-synth-1/t8",
    ).validate()

    result = DistillationLoop(eng, epistemic=None).distill(delta)
    C.check(SEC, "route_is_fresh", result.route == "fresh-synthesis",
            f"{result.route}: {result.reason}")
    C.check(SEC, "success", result.success is True, result.reason)
    C.check(SEC, "heldout",
            result.heldout_passed == result.heldout_examples > 0,
            f"{result.heldout_passed}/{result.heldout_examples}")
    got = eng.primitives.invoke_sync(result.promoted_name, a=41, b=59)
    C.check(SEC, "primitive_adds", got == 100, got)
    print("T8_DONE")


if __name__ == "__main__":
    main()
