"""t3: trace-guided distillation through the live DistillationLoop.

Builds a DeltaRecord from the borrowed demos (evidence + demonstration
traces) and distills. Asserts: route == trace-guided, success, promoted
primitive registered, held-out 4/4, negative controls pass, verdict admitted.
Writes the distill record for t4/t6.
"""
import sys
import os
import json
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _tg_common as C

SEC = "t3"
os.environ["REMOR_TEST_MODE"] = "1"


def main():
    technique = sys.argv[1]
    assert technique in C.TECHNIQUES, technique
    tech = C.TECHNIQUES[technique]
    sec = f"{SEC}/{technique}"

    with open(C.selected_path(technique)) as fh:
        sel = json.load(fh)
    demos = sel["demonstrations"]
    C.check(sec, "twelve_demos", len(demos) == 12, len(demos))

    from runtime.core.engine import SwarmEngine
    from swarm_engine.acquisition.delta import DeltaRecord
    from swarm_engine.acquisition.distill import DistillationLoop

    db = C.engine_db(technique)
    for f in (db, db + ".oracle.db"):
        if os.path.exists(f):
            os.remove(f)
    eng = SwarmEngine(db_path=db, _test_allow_shared=True)
    print(f"{sec}: engine built", flush=True)

    evidence = [{"input": d["input"], "output": d["expected"]} for d in demos]
    traces = [d["teacher_text"] for d in demos]
    delta = DeltaRecord(
        objective=tech["objective"],
        external_actions=f"demonstrated {tech['technique']}",
        prior_capability="none",
        capability_gap=tech["gap"],
        technique=tech["technique"],
        evidence=evidence,
        demonstration_traces=traces,
        source=f"trace-guided-synth-1/{technique}",
    ).validate()

    loop = DistillationLoop(eng, epistemic=None)
    result = loop.distill(delta)
    print(f"{sec}: success={result.success} route={result.route} "
          f"reason={result.reason}", flush=True)
    C.check(sec, "route_is_trace_guided", result.route == "trace-guided",
            result.route)
    C.check(sec, "distill_success", result.success is True, result.reason)
    C.check(sec, "heldout_4_of_4",
            result.heldout_passed == 4 and result.heldout_examples == 4,
            f"{result.heldout_passed}/{result.heldout_examples}")
    C.check(sec, "negative_controls",
            result.negative_controls_passed >= 2,
            result.negative_controls_passed)
    C.check(sec, "verdict_admitted", result.verdict_admitted is True,
            result.verdict_admitted)
    pname = result.promoted_name
    C.check(sec, "promoted_registered",
            eng.primitives.get(pname) is not None, pname)

    # Show the composed plan shape for the record.
    with open(C.distill_result_path(technique), "w") as fh:
        json.dump({"technique": technique, "success": result.success,
                   "route": result.route, "promoted_name": pname,
                   "heldout": [result.heldout_passed, result.heldout_examples],
                   "negative_controls": result.negative_controls_passed,
                   "verdict_admitted": result.verdict_admitted,
                   "reason": result.reason}, fh, indent=1)
    print(f"{sec}: promoted {pname}")
    print("T3_DONE")


if __name__ == "__main__":
    main()
