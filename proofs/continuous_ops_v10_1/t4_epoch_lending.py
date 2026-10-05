"""T4 -- epoch-bounded lending and non-preemption.

Fresh process. Drives the REAL FinancialResourceManager through two
epochs and asserts the FRM amendment section 14 contract:
- mid-epoch demand changes are REFUSED (mid_epoch_refusal=True): an
  active epoch's grants are never altered by new demand; the demand is
  recorded for the next round.
- advance_epoch() recalls lent capacity: the EpochClose names the
  recalled lent budget/concurrent, and the NEXT round re-evaluates
  from zero lending (the recalled loan does not carry over).
- running work admitted against the closing epoch's grants is NOT
  preempted (the close notes say so explicitly).
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
from cop_common import Checker  # noqa: E402

from swarm_engine.curiosity.frm.policy import (  # noqa: E402
    FrmPolicy, DomainDemand, ExpectedYield, RUNNING)
from swarm_engine.curiosity.frm.evaluation import (  # noqa: E402
    FinancialResourceManager, PRIMARY, CURIOSITY)


def _frm(workdir, tag):
    policy = FrmPolicy(
        total_budget_s=100.0, total_max_concurrent=8,
        primary_minimum_budget_s=20.0, primary_minimum_concurrent=2)
    return FinancialResourceManager(
        policy, ledger_path=os.path.join(workdir, f"{tag}.db"))


def _demands(c_budget=40.0, c_conc=4):
    y = ExpectedYield(value=1.0, basis="t4 fixture")
    return (DomainDemand(domain=PRIMARY, budget_s=30.0,
                         max_concurrent=3, expected_yield=y),
            DomainDemand(domain=CURIOSITY, budget_s=c_budget,
                         max_concurrent=c_conc, expected_yield=y))


def main():
    c = Checker()
    workdir = tempfile.mkdtemp(prefix="cops_t4_")
    frm = _frm(workdir, "ep1")

    pd, cd = _demands()
    r1 = frm.evaluate_round(enforcement_state=RUNNING,
                            primary_demand=pd, curiosity_demand=cd)
    g1 = r1.grants[CURIOSITY]
    epoch1 = r1.epoch_id
    c.check("t4_epoch1_granted", g1.budget_s > 0,
            f"epoch={epoch1} budget={g1.budget_s}")

    # Mid-epoch demand change: refused, grants stand.
    pd2, cd2 = _demands(c_budget=80.0, c_conc=8)
    r_mid = frm.evaluate_round(enforcement_state=RUNNING,
                               primary_demand=pd2, curiosity_demand=cd2)
    c.check("t4_mid_epoch_refused", r_mid.mid_epoch_refusal is True,
            f"mid_epoch_refusal={r_mid.mid_epoch_refusal}")
    c.check("t4_mid_epoch_grants_stand",
            r_mid.grants[CURIOSITY].budget_s == g1.budget_s,
            f"stood={r_mid.grants[CURIOSITY].budget_s}")
    c.check("t4_mid_epoch_recorded_for_next",
            len(frm.pending_demands) >= 1,
            f"pending={len(frm.pending_demands)}")

    # Close the epoch: lent capacity recalled, running work not preempted.
    close = frm.advance_epoch()
    c.check("t4_epoch_closed", close.epoch_id == epoch1,
            f"closed={close.epoch_id}")
    c.check("t4_recall_named",
            any("recalled" in n.lower() or "recall" in n.lower()
                for n in close.notes),
            f"notes={close.notes[:1]}")
    c.check("t4_no_preemption_noted",
            any("NOT preempted" in n for n in close.notes),
            f"notes={close.notes}")

    # Next round re-evaluates from zero lending.
    pd3, cd3 = _demands()
    r2 = frm.evaluate_round(enforcement_state=RUNNING,
                            primary_demand=pd3, curiosity_demand=cd3)
    c.check("t4_new_epoch", r2.epoch_id != epoch1,
            f"e1={epoch1} e2={r2.epoch_id}")
    c.check("t4_lending_reset",
            r2.grants[CURIOSITY].lending.lent_budget_s == 0.0,
            f"lent={r2.grants[CURIOSITY].lending.lent_budget_s}")

    ok = c.summary()
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
