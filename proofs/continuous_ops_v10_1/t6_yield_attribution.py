"""T6 -- yield attribution: accepted work yields, unaccepted work costs.

Fresh process. Drives the REAL attribution chain (ChainLedger +
ExpenditureLedger + recompute_attribution) and asserts the FRM
amendment section 6 contract:
- R1: a work unit with real expenditure and an ACCEPTED admission
  record gets attributed_yield = yield_value * accepted_portion > 0,
  with the acceptance_record_id set.
- R1 (negative): a work unit with real expenditure but NO admission
  record gets attributed_yield == 0.0 (cost accrues, yield zero).
- R3: partial acceptance scales yield proportionally.
- Rejected verdict -> zero yield.
"""
import os
import sys
import tempfile
import uuid

_HERE = os.path.dirname(os.path.abspath(__file__))
_T = os.path.dirname(os.path.dirname(_HERE))
for _p in (os.path.join(_T, "pylib"), _T):
    if _p not in sys.path:
        sys.path.insert(0, _p)

sys.path.insert(0, _HERE)
from cop_common import Checker  # noqa: E402

from runtime.curiosity.attribution.chain import (  # noqa: E402
    ChainLedger, WorkUnit, Result, AdmissionRecord,
    recompute_attribution)
from runtime.curiosity.attribution.spend import (  # noqa: E402
    ExpenditureLedger, SpendReport)


def _spend(exp_ledger, work_ref, grant="grant-t6"):
    exp_ledger.record(grant, SpendReport(
        call_id=str(uuid.uuid4()), controller_id="run-controller",
        inquiry_id="inq-t6", work_ref=work_ref,
        model_provider="test-provider",
        cpu_seconds=1.5, work_units=10, bytes_processed=512,
        monetary_cost=0.01, cost_kind="measured").validate())


def main():
    c = Checker()
    workdir = tempfile.mkdtemp(prefix="cops_t6_")
    chain = ChainLedger(db_path=os.path.join(workdir, "chain.db"))
    exp = ExpenditureLedger(db_path=os.path.join(workdir, "exp.db"))

    # --- accepted work -> measurable yield ---------------------------------
    w1 = f"w-acc-{uuid.uuid4().hex[:8]}"
    chain.record_work(WorkUnit(work_ref=w1, inquiry_id="inq-t6",
                               grant_id="grant-t6",
                               controller_id="run-controller"))
    _spend(exp, w1)
    r1 = f"r-{w1}"
    chain.record_result(Result(result_ref=r1, work_ref=w1,
                               outcome="success").validate())
    chain.record_admission(AdmissionRecord(
        admission_id=f"a-{w1}", result_ref=r1, verdict="accepted",
        yield_value=4.0, accepted_portion=1.0,
        decided_by="t6-proof").validate())
    link = recompute_attribution(w1, chain, exp,
                                 originating_executive="curiosity")
    c.check("t6_accepted_yield_positive", link.attributed_yield == 4.0,
            f"yield={link.attributed_yield}")
    c.check("t6_accepted_record_set",
            link.acceptance_record_id is not None,
            f"record={link.acceptance_record_id}")
    c.check("t6_accepted_cost_accrued", link.total_cpu_seconds == 1.5,
            f"cpu={link.total_cpu_seconds}")

    # --- partial acceptance -> proportional yield ---------------------------
    w2 = f"w-part-{uuid.uuid4().hex[:8]}"
    chain.record_work(WorkUnit(work_ref=w2, inquiry_id="inq-t6",
                               grant_id="grant-t6",
                               controller_id="run-controller"))
    _spend(exp, w2)
    r2 = f"r-{w2}"
    chain.record_result(Result(result_ref=r2, work_ref=w2,
                               outcome="partial").validate())
    chain.record_admission(AdmissionRecord(
        admission_id=f"a-{w2}", result_ref=r2, verdict="accepted",
        yield_value=4.0, accepted_portion=0.5,
        decided_by="t6-proof").validate())
    link2 = recompute_attribution(w2, chain, exp,
                                  originating_executive="curiosity")
    c.check("t6_partial_yield_proportional",
            link2.attributed_yield == 2.0,
            f"yield={link2.attributed_yield}")

    # --- no admission -> zero yield, cost still accrues ----------------------
    w3 = f"w-noadm-{uuid.uuid4().hex[:8]}"
    chain.record_work(WorkUnit(work_ref=w3, inquiry_id="inq-t6",
                               grant_id="grant-t6",
                               controller_id="run-controller"))
    _spend(exp, w3)
    r3 = f"r-{w3}"
    chain.record_result(Result(result_ref=r3, work_ref=w3,
                               outcome="success").validate())
    link3 = recompute_attribution(w3, chain, exp,
                                  originating_executive="curiosity")
    c.check("t6_unaccepted_zero_yield", link3.attributed_yield == 0.0,
            f"yield={link3.attributed_yield}")
    c.check("t6_unaccepted_no_record",
            link3.acceptance_record_id is None, "record is None")
    c.check("t6_unaccepted_cost_accrues",
            link3.total_cpu_seconds == 1.5,
            f"cpu={link3.total_cpu_seconds} (cost, zero yield)")

    # --- rejected verdict -> zero yield --------------------------------------
    w4 = f"w-rej-{uuid.uuid4().hex[:8]}"
    chain.record_work(WorkUnit(work_ref=w4, inquiry_id="inq-t6",
                               grant_id="grant-t6",
                               controller_id="run-controller"))
    _spend(exp, w4)
    r4 = f"r-{w4}"
    chain.record_result(Result(result_ref=r4, work_ref=w4,
                               outcome="failed").validate())
    chain.record_admission(AdmissionRecord(
        admission_id=f"a-{w4}", result_ref=r4, verdict="rejected",
        yield_value=0.0, accepted_portion=1.0,
        decided_by="t6-proof").validate())
    link4 = recompute_attribution(w4, chain, exp,
                                  originating_executive="curiosity")
    c.check("t6_rejected_zero_yield", link4.attributed_yield == 0.0,
            f"yield={link4.attributed_yield}")

    ok = c.summary()
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
