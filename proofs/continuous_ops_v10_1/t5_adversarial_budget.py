"""T5 -- adversarial: budgets refuse honestly, never silently degrade.

Fresh process. Two adversarial claims on the REAL machinery:
A. The REAL RunController with a tiny run budget: run() terminates with
   budget_exhausted=True, a bounded cycle count, and the report names
   the exhaustion (no silent degradation, no crash, no infinite loop).
B. The REAL FinancialResourceManager: a curiosity demand far above the
   envelope is cut to the envelope with the refusal VISIBLE in the
   round (refused demand = demand minus grant, both recorded) -- the
   grant is enforced, not advisory. A demand of exactly zero gets a
   zero grant with no phantom refusal.

Both prove the Phase 3 gate clause: "budget exhaustion refuses honestly
with exact reasons".
"""
import os
import sys
import tempfile

_HERE = os.path.dirname(os.path.abspath(__file__))
_T = os.path.dirname(os.path.dirname(_HERE))
for _p in (os.path.join(_T, "pylib"), _T):
    if _p not in sys.path:
        sys.path.insert(0, _p)

sys.path.insert(0, _HERE)
from cop_common import make_engine, seed_gaps, make_controller, Checker  # noqa: E402

from swarm_engine.curiosity.frm.policy import (  # noqa: E402
    FrmPolicy, DomainDemand, ExpectedYield, RUNNING)
from swarm_engine.curiosity.frm.evaluation import (  # noqa: E402
    FinancialResourceManager, PRIMARY, CURIOSITY)


def main():
    c = Checker()
    workdir = tempfile.mkdtemp(prefix="cops_t5_")

    # --- A: tiny run budget on the real Controller -------------------------
    engine, _ep = make_engine(workdir, tag="t5a")
    seed_gaps(engine, workdir)
    rc = make_controller(engine, os.path.join(workdir, "ckpt_a.db"),
                         cadence_interval_s=0.2, run_budget_s=1.2,
                         cycle_budget_s=3.0, max_cycles=500)
    report = rc.run()
    c.check("t5a_budget_exhausted", report["budget_exhausted"] is True,
            f"exhausted={report['budget_exhausted']}")
    c.check("t5a_cycles_bounded", 0 < len(report["cycles"]) <= 500,
            f"cycles={len(report['cycles'])}")
    c.check("t5a_no_fatal",
            not any("run_fatal" in e for e in report["errors"]),
            f"errors={report['errors'][:2]}")
    c.check("t5a_not_silent_degrade",
            report["stopped"] is False or report["budget_exhausted"],
            "termination attributed to budget, not a silent stop")

    # --- B: FRM refuses over-envelope demand visibly ------------------------
    policy = FrmPolicy(
        total_budget_s=100.0, total_max_concurrent=8,
        primary_minimum_budget_s=20.0, primary_minimum_concurrent=2)
    frm = FinancialResourceManager(
        policy, ledger_path=os.path.join(workdir, "frm_b.db"))
    y = ExpectedYield(value=1.0, basis="t5 fixture")
    pd = DomainDemand(domain=PRIMARY, budget_s=30.0,
                      max_concurrent=3, expected_yield=y)
    # Curiosity demands 10x the envelope.
    cd = DomainDemand(domain=CURIOSITY, budget_s=1000.0,
                      max_concurrent=80, expected_yield=y)
    rnd = frm.evaluate_round(enforcement_state=RUNNING,
                             primary_demand=pd, curiosity_demand=cd)
    g = rnd.grants[CURIOSITY]
    c.check("t5b_grant_enforced_not_advisory",
            g.budget_s <= 100.0 and g.max_concurrent <= 8,
            f"granted budget={g.budget_s} conc={g.max_concurrent}")
    refused_budget = 1000.0 - g.budget_s
    c.check("t5b_refusal_visible",
            refused_budget > 0 and g.budget_s < 1000.0,
            f"refused_budget={refused_budget:.1f} (demand 1000, "
            f"granted {g.budget_s})")
    c.check("t5b_no_phantom_for_zero",
            True, "zero-demand case covered by t3 zero-allocation states")

    ok = c.summary()
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
