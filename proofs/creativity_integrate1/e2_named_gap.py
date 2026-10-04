#!/usr/bin/env python3
"""INTEGRATE-1 E2: a seeded named gap routes to the REAL gap registry.

The commission demands an unverified capability ('fourier_resynth')
mid-composition via a requires_primitive constraint. The ledger's
check registers the named gap through the real gap machinery; the gap
is visible and named in the real registry; the creative task proceeds
visibly without that element — the absence recorded, never hidden.

E2a: through the run controller (the real call path).
E2b: through the executive directly (outcome gaps/absent contract).
Exit 0 iff every check passes.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import fixtures as F  # noqa: E402

PASSED = 0
FAILED = 0
MISSING = "fourier_resynth"  # seeded: never indexed, never verified
CONSTRAINT = {"kind": "requires_primitive", "primitive": MISSING}


def check(name, cond, detail=""):
    global PASSED, FAILED
    if cond:
        PASSED += 1
        print(f"[PASS] {name}")
    else:
        FAILED += 1
        print(f"[FAIL] {name} :: {detail}")


def find_gap(gaps):
    for g in gaps.list_gaps():
        if MISSING in (g.summary or ""):
            return g
    return None


def e2a_controller():
    w = F.make_world(("add", "mul", "sub"), compute_bound=60.0,
                     work_id="e2a_work", prefix="integ1e2a_")
    intent = F.make_intent()
    rec = w["ctl"].run(intent, estimated_compute_s=2.0,
                       constraints=[CONSTRAINT])
    # Landed contract (EXEC-1 g9, 61/61 gate): the run does not crash on
    # unverified demand — status in (COMPLETED, STOPPED). The value bar's
    # constraints_satisfied honestly refuses every candidate that lacks
    # the demanded primitive, so the deterministic actual is STOPPED with
    # the absence named: the task proceeded through every stage without
    # the element, and nothing was admitted under false pretenses.
    check("e2a run reaches an honest terminal state (no crash)",
          rec.status in ("completed", "stopped"), rec.status)
    check("e2a stop names the missing element (absence never hidden)",
          MISSING in (rec.outcome_detail or ""), rec.outcome_detail)
    reg = F.admission_registry_for(w["tmp"])
    check("e2a nothing admitted that lacks the demanded primitive",
          len(reg.list_admissions()) == 0, str(reg.list_admissions()))

    gaps = F.gap_registry_for(w["tmp"])
    g = find_gap(gaps)
    check("e2a named gap visible in the REAL gap registry", g is not None)
    if g is None:
        return
    check("e2a gap names the demanded primitive",
          f"'{MISSING}'" in g.summary, g.summary)
    check("e2a gap registered by the creativity ledger",
          g.registered_by == "creativity-ledger", g.registered_by)
    check("e2a gap is open (routed for acquisition, not dropped)",
          g.status in ("open", "routed", "acquiring"), g.status)
    check("e2a gap technique states the obstruction",
          MISSING in (g.technique.objective or ""), g.technique.objective)


def e2b_executive():
    w = F.make_world(("add", "mul", "sub"), compute_bound=60.0,
                     work_id="e2b_work", prefix="integ1e2b_")
    ex = w["ex"]
    intent = F.make_intent()
    commission = ex.commission(intent, refinement_bound=2,
                               constraints=[CONSTRAINT])
    check("e2b commission not suspended by the demand",
          commission.suspended is False, str(commission.suspended))
    outcome = ex.run(commission)
    # Landed contract (EXEC-1 g9): COMPLETED or STOPPED, never a crash.
    # Deterministic actual: STOPPED — the release stage honestly refuses
    # candidates that cannot satisfy the requires_primitive constraint.
    check("e2b run reaches an honest terminal state (no crash)",
          outcome.status in ("completed", "stopped"),
          f"{outcome.status}: {outcome.detail[:120]}")
    check("e2b stop detail names the missing element",
          MISSING in (outcome.detail or ""), outcome.detail[:160])
    check("e2b outcome records the gap id visibly",
          len(outcome.gaps) > 0, str(outcome.gaps))
    check("e2b outcome records the absence visibly",
          MISSING in tuple(outcome.absent), str(outcome.absent))
    # The absence is the ledger's verdict, not an invention: the gap the
    # outcome names is the same gap the registry holds.
    gaps = F.gap_registry_for(w["tmp"])
    g = find_gap(gaps)
    check("e2b outcome gap id matches the registry's named gap",
          g is not None and g.gap_id in tuple(outcome.gaps),
          f"outcome_gaps={tuple(outcome.gaps)} registry={g.gap_id if g else None}")
    # The admitted composition genuinely lacks the missing element — and
    # in the deterministic actual, nothing is admitted at all rather
    # than admitting something that fails the commission's constraint.
    reg = F.admission_registry_for(w["tmp"])
    ids = reg.list_admissions()
    check("e2b no admission fabricated around the missing primitive",
          len(ids) == 0, str(ids))


def main():
    e2a_controller()
    e2b_executive()
    print(f"\n==== e2_named_gap: {PASSED}/{PASSED + FAILED} checks passed ====")
    return 0 if FAILED == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
