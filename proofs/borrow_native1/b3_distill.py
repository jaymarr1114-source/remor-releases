"""b3: distill — the verified demonstrations become a native primitive.

Builds the charter delta record from b2's verified demonstrations, saves it
(delta_record.json — the deliverable), and runs the LIVE DistillationLoop
against a real SwarmEngine: synthesis -> held-out verification in fresh
processes -> negative controls -> ReviewBoard -> promotion through the
frozen promotion API. No bypass, no manual badge-flipping.

Checkpoint: distill_result.json with success=true; on rerun the engine is
rebuilt on the same DB and the promoted primitive's registration is
re-verified before skipping.
"""
import sys
import os
import json
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common as C

SEC = "b3"
FORCE = "--force" in sys.argv


def build_delta(demos):
    from swarm_engine.acquisition.delta import DeltaRecord
    evidence = [{"input": dict(d["input"]), "output": d["output"],
                 "teacher_text": d.get("teacher_text", "")}
                for d in demos]
    delta = DeltaRecord(
        objective="compute the sum of two integers",
        external_actions=(
            "Qwen3-8B (borrowed teacher) demonstrated integer addition: "
            "for each presented pair (a, b) it computed a+b, showing its "
            "work. Demonstrated on "
            f"{len(evidence)} verified additions; every demonstration's "
            "result was checked against ground truth before recording."),
        prior_capability=(
            "REMOR's native tier had no addition machinery: purpose "
            "'compute_sum' raised NativeRefusal(no_sum_computation); the "
            "microcontroller could not answer 'what is a+b' natively."),
        capability_gap=(
            "The teacher could compute sums; REMOR natively could not — "
            "integer addition was unowned as a task-class capability."),
        technique="addition: given (a, b), compute a+b",
        evidence=evidence,
        dependencies=[],
        verification={"teacher_revision": C.QWEN3_REV,
                      "ground_truth_checked": True,
                      "n_demonstrations": len(evidence)},
        source="borrow-native-1/b2",
    )
    return delta.validate()


def build_engine():
    os.environ["REMOR_TEST_MODE"] = "1"
    from runtime.core.engine import SwarmEngine
    return SwarmEngine(db_path=C.ENGINE_DB, _test_allow_shared=True)


def main():
    with open(C.DEMOS_PATH) as fh:
        demos = json.load(fh)["demonstrations"]
    assert len(demos) >= 10, f"need >=10 demos, have {len(demos)}"

    delta = build_delta(demos)
    with open(C.DELTA_PATH, "w") as fh:
        json.dump({"delta_id": delta.delta_id,
                   "objective": delta.objective,
                   "external_actions": delta.external_actions,
                   "prior_capability": delta.prior_capability,
                   "capability_gap": delta.capability_gap,
                   "technique": delta.technique,
                   "evidence": delta.evidence,
                   "dependencies": delta.dependencies,
                   "verification": delta.verification,
                   "source": delta.source}, fh, indent=1)
    C.check(SEC, "delta_valid", True, "")
    C.check(SEC, "delta_saved", os.path.isfile(C.DELTA_PATH), "")

    # checkpoint: already distilled and still registered?
    if os.path.isfile(C.DISTILL_PATH) and not FORCE:
        with open(C.DISTILL_PATH) as fh:
            prev = json.load(fh)
        if prev.get("success"):
            eng = build_engine()
            pname = prev.get("promoted_name", "")
            p = eng.primitives.get(pname) if pname else None
            if p is not None:
                print(f"b3: checkpoint valid ({pname} registered), "
                      f"skipping distillation")
                C.check(SEC, "distill_success", True, "")
                C.check(SEC, "primitive_registered", True, "")
                ok = C.summarize(SEC)
                print(("BATTERY b3 " + "PASS") if ok else "BATTERY b3 FAIL")
                return 0 if ok else 1
            print("b3: checkpoint stale (primitive not registered), "
                  "re-distilling")

    eng = build_engine()
    print("b3: engine built; running DistillationLoop.distill ...", flush=True)
    from swarm_engine.acquisition.distill import DistillationLoop
    loop = DistillationLoop(eng, epistemic=None)
    t0 = time.time()
    result = loop.distill(delta)
    dt = time.time() - t0
    print(f"b3: distill finished in {dt:.1f}s: success={result.success} "
          f"route={result.route} promoted={result.promoted_name!r} "
          f"reason={result.reason!r}", flush=True)

    C.check(SEC, "distill_success", result.success is True,
            result.reason)
    C.check(SEC, "heldout_passed",
            result.heldout_passed == result.heldout_examples
            and result.heldout_examples >= 2,
            f"{result.heldout_passed}/{result.heldout_examples}")
    C.check(SEC, "verdict_admitted", result.verdict_admitted is True, "")
    C.check(SEC, "promoted_named", bool(result.promoted_name),
            result.promoted_name)
    if result.promoted_name:
        p = eng.primitives.get(result.promoted_name)
        C.check(SEC, "primitive_registered", p is not None,
                result.promoted_name)

    with open(C.DISTILL_PATH, "w") as fh:
        json.dump({"success": bool(result.success),
                   "route": result.route,
                   "promoted_name": result.promoted_name,
                   "capability_id": result.capability_id,
                   "build_examples": result.build_examples,
                   "heldout_examples": result.heldout_examples,
                   "heldout_passed": result.heldout_passed,
                   "negative_controls_passed":
                       result.negative_controls_passed,
                   "verdict_admitted": result.verdict_admitted,
                   "reason": result.reason,
                   "delta_id": result.delta_id}, fh, indent=1)

    ok = C.summarize(SEC)
    print(("BATTERY b3 " + "PASS") if ok else "BATTERY b3 FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
