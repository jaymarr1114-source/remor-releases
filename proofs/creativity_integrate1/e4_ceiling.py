#!/usr/bin/env python3
"""INTEGRATE-1 E4: budget ceiling mid-run — CeilingStop, frozen totals.

Envelope compute bound 0.05s; the open estimate (0.01s) fits, so the
real grant issues and the run starts. The measured actuals (~0.24s)
exceed the ceiling: record_spend returns CeilingStop, the work halts,
and no further spend accrues — a second budget request raises
BudgetHalted and a second run goes dormant with totals frozen.

Exit 0 iff every check passes.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import fixtures as F  # noqa: E402
from runtime.creativity.budget import BudgetHalted  # noqa: E402

PASSED = 0
FAILED = 0
BOUND = 0.05


def check(name, cond, detail=""):
    global PASSED, FAILED
    if cond:
        PASSED += 1
        print(f"[PASS] {name}")
    else:
        FAILED += 1
        print(f"[FAIL] {name} :: {detail}")


def main():
    w = F.make_world(("add", "mul", "sub"), compute_bound=BOUND,
                     work_id="e4_work", prefix="integ1e4_")
    ctl, budget = w["ctl"], w["budget"]
    intent = F.make_intent()

    rec = ctl.run(intent, estimated_compute_s=0.01)

    # The grant issued (estimate fit); the ceiling fired on actuals.
    check("e4 grant issued at open (estimate fit the envelope)",
          rec.grant_issued is True)
    check("e4 ceiling stop recorded", rec.ceiling_stop is not None,
          str(rec.ceiling_stop))
    if rec.ceiling_stop is None:
        return 1
    check("e4 ceiling reason names the bound",
          f"{BOUND:.2f}" in rec.ceiling_stop.get("reason", ""),
          rec.ceiling_stop.get("reason"))
    check("e4 preservation keys carried on the stop",
          "preservation_keys" in rec.ceiling_stop,
          str(sorted(rec.ceiling_stop)))
    check("e4 actuals exceeded the ceiling",
          rec.spend_s is not None and rec.spend_s >= BOUND,
          str(rec.spend_s))
    check("e4 work halted at the ceiling",
          budget.ledger.is_halted("e4_work") is True)

    frozen = budget.ledger.spent_compute("e4_work")

    # -- zero further spend: the halt is non-cooperative ------------------
    try:
        budget.request_budget(work_id="e4_work", estimated_compute_s=0.01)
        check("e4 further budget refused after halt", False,
              "request_budget did not raise")
    except BudgetHalted:
        check("e4 further budget refused after halt", True)
    check("e4 spend total frozen (nothing accrued after the halt)",
          budget.ledger.spent_compute("e4_work") == frozen,
          f"{budget.ledger.spent_compute('e4_work')} vs {frozen}")

    rec2 = ctl.run(intent, estimated_compute_s=0.01)
    check("e4 second run goes dormant at the halted ceiling",
          rec2.status == "dormant", rec2.status)
    check("e4 dormant run issued no grant",
          rec2.grant_issued is False)
    check("e4 dormant run accrued no spend",
          budget.ledger.spent_compute("e4_work") == frozen,
          str(budget.ledger.spent_compute("e4_work")))

    print(f"\n==== e4_ceiling: {PASSED}/{PASSED + FAILED} checks passed ====")
    return 0 if FAILED == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
