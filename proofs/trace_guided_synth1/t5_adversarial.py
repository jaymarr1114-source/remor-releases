"""t5: adversarial fail-closed — the route refuses by name, never silently.

t5a: garbage traces -> named trace-guided refusal (no work lines).
t5b: fewer traces than build examples -> named refusal.
t5c: NO traces on step_verify evidence -> falls through to Route B, which
     fails honestly (the wall); success stays False.
t5d: inconsistent work-line counts across traces -> named refusal.
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _tg_common as C

SEC = "t5"
os.environ["REMOR_TEST_MODE"] = "1"


def fresh_engine(name):
    from runtime.core.engine import SwarmEngine
    db = os.path.join(C.PROOF_DIR, f"_t5_{name}.db")
    for f in (db, db + ".oracle.db"):
        if os.path.exists(f):
            os.remove(f)
    return SwarmEngine(db_path=db, _test_allow_shared=True)


def distill_with(eng, evidence, traces, tag):
    from swarm_engine.acquisition.delta import DeltaRecord
    from swarm_engine.acquisition.distill import DistillationLoop
    delta = DeltaRecord(
        objective="verify whether the claimed sum is correct",
        external_actions="demonstrated recompute-and-compare",
        prior_capability="none",
        capability_gap="cannot verify sums",
        technique="recompute-and-compare",
        evidence=evidence,
        demonstration_traces=traces,
        source=f"trace-guided-synth-1/t5-{tag}",
    ).validate()
    return DistillationLoop(eng, epistemic=None).distill(delta)


def ex(a, b, claimed):
    return {"input": {"a": a, "b": b, "claimed": claimed},
            "output": (a + b) == claimed}


def main():
    evidence6 = [ex(*t) for t in
                 [(2, 4, 6), (2, 4, 7), (3, 4, 7), (3, 4, 8), (10, 15, 25), (10, 15, 24)]]

    # t5a: garbage traces
    r = distill_with(fresh_engine("a"), evidence6,
                     ["not a trace at all"] * 6, "a")
    C.check(SEC, "a_refused", r.success is False, r.success)
    C.check(SEC, "a_named", r.route == "trace-guided"
            and "no work lines" in (r.reason or ""), f"{r.route}: {r.reason}")

    # t5b: fewer traces than build examples (build = 4 of 6)
    good_trace = "recompute: 2 + 4 = 6\ncompare: 6 == 6\nresult: true"
    r = distill_with(fresh_engine("b"), evidence6, [good_trace] * 3, "b")
    C.check(SEC, "b_refused", r.success is False, r.success)
    C.check(SEC, "b_named", r.route == "trace-guided"
            and "traces <" in (r.reason or ""), f"{r.route}: {r.reason}")

    # t5d: inconsistent work-line counts
    traces_d = [
        "recompute: 2 + 4 = 6\ncompare: 6 == 6\nresult: true",
        "recompute: 2 + 4 = 7\ncompare: 7 == 7\nresult: false",
        "step1: 3 + 4 = 7\nstep2: check 7\nstep3: 7 == 8\nresult: false",
        "recompute: 10 + 15 = 25\ncompare: 25 == 24\nresult: false",
        "recompute: 3 + 4 = 7\ncompare: 7 == 7\nresult: true",
        "recompute: 10 + 15 = 25\ncompare: 25 == 25\nresult: true",
    ]
    r = distill_with(fresh_engine("d"), evidence6, traces_d, "d")
    C.check(SEC, "d_refused", r.success is False, r.success)
    C.check(SEC, "d_named", r.route == "trace-guided"
            and "work lines" in (r.reason or ""), f"{r.route}: {r.reason}")

    # t5c: no traces -> falls through to Route B -> honest failure (the wall)
    evidence12 = [ex(*t) for t in C.STEP_TASKS[:12]]
    r = distill_with(fresh_engine("c"), evidence12, [], "c")
    C.check(SEC, "c_not_trace_guided", r.route != "trace-guided", r.route)
    # Route B names its route only on success; on honest failure the route
    # field stays unset — the fall-through is proven by "not trace-guided".
    C.check(SEC, "c_fell_through", r.route in ("", "fresh-synthesis"), r.route)
    C.check(SEC, "c_honest_failure", r.success is False,
            f"route={r.route} reason={r.reason}")
    print(f"[t5] c reason: {r.reason}", flush=True)

    print("T5_DONE")


if __name__ == "__main__":
    main()
