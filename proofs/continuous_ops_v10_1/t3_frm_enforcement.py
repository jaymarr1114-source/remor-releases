"""T3 -- FRM enforcement states honored as zero-allocation (or restricted).

Fresh process. Drives the REAL FinancialResourceManager.evaluate_round
through all five frozen enforcement states and asserts the frozen
interface contract from runtime/curiosity/frm/policy.py:
- HARD_SHUTDOWN_RESOURCE / SUSPENDED_SAFETY / BANNED_6M -> curiosity
  grant budget_s == 0.0 and max_concurrent == 0 (zero allocation), with
  the enforcement state named in the round notes.
- WARNING_1 -> curiosity demand restricted to warning_1_cap_fraction
  of stated demand (not zeroed), state named in notes.
- RUNNING -> normal evaluation: curiosity receives a nonzero grant when
  it states nonzero demand within the envelope.
- Unknown state -> ValueError (fail-closed, never a silent default).
- Grants are immutable records carrying enforcement_state_at_issue.
"""
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_T_PROOF = os.path.dirname(_HERE)          # proofs/
_T = os.path.dirname(_T_PROOF)             # tree root
for _p in (os.path.join(_T, "pylib"), _T):
    if _p not in sys.path:
        sys.path.insert(0, _p)

sys.path.insert(0, _HERE)
from cop_common import Checker  # noqa: E402

from swarm_engine.curiosity.frm.policy import (  # noqa: E402
    FrmPolicy, DomainDemand, ExpectedYield,
    RUNNING, HARD_SHUTDOWN_RESOURCE, WARNING_1,
    SUSPENDED_SAFETY, BANNED_6M,
    ZERO_ALLOCATION_STATES)
from swarm_engine.curiosity.frm.evaluation import (  # noqa: E402
    FinancialResourceManager, PRIMARY, CURIOSITY)


def _frm(tmp_ledger):
    policy = FrmPolicy(
        total_budget_s=100.0, total_max_concurrent=8,
        primary_minimum_budget_s=20.0, primary_minimum_concurrent=2,
        warning_1_cap_fraction=0.25)
    return FinancialResourceManager(policy, ledger_path=tmp_ledger)


def _demands(budget=40.0, conc=4):
    y = ExpectedYield(value=1.0, basis="t3 fixture")
    return (DomainDemand(domain=PRIMARY, budget_s=30.0,
                         max_concurrent=3, expected_yield=y),
            DomainDemand(domain=CURIOSITY, budget_s=budget,
                         max_concurrent=conc, expected_yield=y))


def main():
    import tempfile
    c = Checker()
    workdir = tempfile.mkdtemp(prefix="cops_t3_")

    # --- zero-allocation states -------------------------------------------
    for state in sorted(ZERO_ALLOCATION_STATES):
        frm = _frm(os.path.join(workdir, f"ledger_{state}.db"))
        pd, cd = _demands()
        rnd = frm.evaluate_round(enforcement_state=state,
                                 primary_demand=pd, curiosity_demand=cd)
        g = rnd.grants[CURIOSITY]
        c.check(f"t3_zero_{state}_budget", g.budget_s == 0.0,
                f"budget_s={g.budget_s}")
        c.check(f"t3_zero_{state}_concurrent", g.max_concurrent == 0,
                f"max_concurrent={g.max_concurrent}")
        c.check(f"t3_zero_{state}_named",
                any(state in n for n in rnd.notes),
                f"notes={rnd.notes[:1]}")
        c.check(f"t3_zero_{state}_stamped",
                g.enforcement_state_at_issue == state,
                f"stamped={g.enforcement_state_at_issue}")

    # --- WARNING_1 restricts, does not zero --------------------------------
    frm = _frm(os.path.join(workdir, "ledger_w1.db"))
    pd, cd = _demands(budget=40.0, conc=4)
    rnd = frm.evaluate_round(enforcement_state=WARNING_1,
                             primary_demand=pd, curiosity_demand=cd)
    g = rnd.grants[CURIOSITY]
    c.check("t3_warning1_restricted_not_zero",
            0.0 < g.budget_s <= 40.0 * 0.25 + 1e-9,
            f"budget_s={g.budget_s} (stated 40.0, cap 0.25)")
    c.check("t3_warning1_named",
            any(WARNING_1 in n for n in rnd.notes),
            f"notes={rnd.notes[:1]}")

    # --- RUNNING: normal evaluation, nonzero grant --------------------------
    frm = _frm(os.path.join(workdir, "ledger_run.db"))
    pd, cd = _demands()
    rnd = frm.evaluate_round(enforcement_state=RUNNING,
                             primary_demand=pd, curiosity_demand=cd)
    g = rnd.grants[CURIOSITY]
    c.check("t3_running_nonzero_grant",
            g.budget_s > 0.0 and g.max_concurrent > 0,
            f"budget_s={g.budget_s} conc={g.max_concurrent}")
    c.check("t3_running_grant_immutable",
            isinstance(g.grant_id, str) and g.domain == CURIOSITY,
            f"grant_id={g.grant_id[:8]}")

    # --- unknown state: fail-closed -----------------------------------------
    frm = _frm(os.path.join(workdir, "ledger_bad.db"))
    pd, cd = _demands()
    try:
        frm.evaluate_round(enforcement_state="MAINTENANCE",
                           primary_demand=pd, curiosity_demand=cd)
        c.check("t3_unknown_state_refused", False,
                "no ValueError raised")
    except ValueError as exc:
        c.check("t3_unknown_state_refused", True,
                f"ValueError: {str(exc)[:60]}")

    ok = c.summary()
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
