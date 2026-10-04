#!/usr/bin/env python3
"""INTEGRATE-1 E1: a commissioned creative task through the REAL call path.

run controller activation (predicates met, real FRM budget grant) ->
executive run() -> generation composes from the ledger's verified side ->
critique judges -> admission admits -> AdmissionRecord complete.

Every fixture is a real record in scratch stores via public APIs
(fixtures.py). Exit 0 iff every check passes.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import fixtures as F  # noqa: E402

PASSED = 0
FAILED = 0


def check(name, cond, detail=""):
    global PASSED, FAILED
    if cond:
        PASSED += 1
        print(f"[PASS] {name}")
    else:
        FAILED += 1
        print(f"[FAIL] {name} :: {detail}")


def main():
    w = F.make_world(("add", "mul", "sub"), compute_bound=60.0,
                     work_id="e1_work")
    intent = F.make_intent()
    ctl, ex, budget, tmp = w["ctl"], w["ex"], w["budget"], w["tmp"]

    rec = ctl.run(intent, estimated_compute_s=2.0)

    # -- activation: the real predicate set, the real grant ---------------
    names = [p.name for p in rec.predicates]
    check("e1 four activation predicates evaluated",
          names == ["no_kill_active", "intent_present",
                    "executive_runnable", "budget_granted"], str(names))
    check("e1 all predicates met", all(p.met for p in rec.predicates),
          str([(p.name, p.met, p.reason) for p in rec.predicates]))
    check("e1 real budget grant issued at open", rec.grant_issued is True)
    check("e1 grant recorded in the spend ledger",
          budget.spend_summary("e1_work")["granted_compute_s"] >= 2.0,
          str(budget.spend_summary("e1_work")))

    # -- the run went through the executive's real stages ------------------
    check("e1 run completed", rec.status == "completed", rec.status)
    check("e1 outcome is the executive's admission",
          rec.outcome_status == "completed"
          and "admitted" in (rec.outcome_detail or ""),
          f"{rec.outcome_status} {rec.outcome_detail}")
    check("e1 measured actuals spent (not the estimate)",
          rec.spend_s is not None and rec.spend_s > 0, str(rec.spend_s))

    # -- the admission record, from the REAL registry ---------------------
    reg = F.admission_registry_for(tmp)
    ids = reg.list_admissions()
    check("e1 exactly one admission registered", len(ids) == 1, str(ids))
    adm = reg.get_admission(ids[0]) if ids else None
    check("e1 admission record retrieved", adm is not None)
    if adm is None:
        return 1
    check("e1 admission carries the intent",
          adm.intent_outcome == intent.outcome
          and adm.commissioning_principal == intent.principal,
          f"{adm.intent_outcome!r} {adm.commissioning_principal!r}")
    check("e1 admission composed from the verified side",
          len(adm.primitive_ids) > 0
          and set(adm.primitive_ids) <= {"add", "mul", "sub"},
          str(adm.primitive_ids))
    check("e1 admission carries verification events",
          len(adm.verification_events) > 0, str(adm.verification_events))
    check("e1 verification events cite the gate",
          all("gate:INTEGRATE1" in e.get("gate_reference", "")
              for e in adm.verification_events),
          str(adm.verification_events))
    check("e1 admission carries critique evidence",
          isinstance(adm.novelty, dict) and "verdict" in adm.novelty
          and isinstance(adm.value, dict) and len(adm.value) > 0,
          f"novelty={adm.novelty} value_keys={list(adm.value)}")
    check("e1 admission carries panel verdicts",
          len(adm.panels) > 0
          and all("panel" in p and "passed" in p for p in adm.panels),
          str(adm.panels)[:200])
    check("e1 admission criteria all passed",
          len(adm.criteria) > 0 and all(c.passed for c in adm.criteria),
          str([(c.criterion, c.passed) for c in adm.criteria]))
    check("e1 admission stamped with criteria version",
          adm.criteria_version == "v1" and adm.admitted_at > 0,
          f"{adm.criteria_version} {adm.admitted_at}")

    # -- round-trip: the registry's contract with its consumers ------------
    d = adm.to_dict()
    adm2 = type(adm).from_dict(d)
    check("e1 admission record round-trips through the registry",
          adm2.admission_id == adm.admission_id
          and adm2.primitive_ids == adm.primitive_ids
          and len(adm2.verification_events) == len(adm.verification_events),
          adm2.admission_id)

    print(f"\n==== e1_full_path: {PASSED}/{PASSED + FAILED} checks passed ====")
    return 0 if FAILED == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
