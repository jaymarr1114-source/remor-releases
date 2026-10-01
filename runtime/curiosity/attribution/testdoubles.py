"""CUR-P1D — test doubles that really spend.

The mandate: "Do NOT build the Curiosity Executive, Run Controller,
or loops — drive the chain with test doubles that really spend (real
substrate calls, real budgets)."

These doubles are honest stand-ins, labeled as such:
    * CallerBudget — the calling controller's real paying budget
      (A32: the caller's budget pays; A31: the substrate only
      reports). It really deducts measured spend and really refuses
      overspend. It is not the Run Controller; it is the budget
      object the controller would hold.
    * TestAdmissionBoard — stands in for Primary Acceptance, which
      arrives in Phase 4+. It writes admission records in the
      measurement shape the FRM consumes. It is labeled
      decided_by="test_admission_board" on every record and does not
      pretend to be the real authority.
    * EnforcementStub — an EnforcementStatusProvider test double
      speaking the frozen state vocabulary. Proves the
      zero-allocation-under-enforcement rule.
    * InquiryDriver — a test-double controller that runs the full
      chain end-to-end: allocate → spend via the metered substrate →
      record work → record result → submit for admission →
      recompute the attribution link.

Real spend: every substrate call executes real_substrate_work
(iterated hashing — actual CPU, actually measured with
perf_counter), and the SpendReport carries the measured
cpu_seconds, counted work_units, and a declared-rate monetary
cost (cost_kind="declared", FRM §8 — honestly a policy input,
never presented as observed billing).
"""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from .chain import (AdmissionRecord, ChainLedger, Result, WorkUnit,
                    aggregate_attribution, recompute_attribution)
from .grants import (Allocation, AllocationLedger, AllocationRefused,
                     EnforcementStatusProvider)
# Absolute swarm_engine import: see the load-bearing note in grants.py —
# the FrmGrant class object must be identical across both namespaces.
from swarm_engine.curiosity.frm.grant import FrmGrant
from .spend import (Expenditure, ExpenditureLedger, MeteredSubstrate,
                    SpendReport, real_substrate_work)


# ---------------------------------------------------------------------------
# real paying budget (A32)
# ---------------------------------------------------------------------------

class BudgetExceeded(Exception):
    """The caller's budget refused the spend. Real enforcement by the
    budget holder — the substrate never sees this decision (A31)."""


@dataclass
class BudgetState:
    budget_s: float
    spent_s: float = 0.0

    @property
    def remaining_s(self) -> float:
        return self.budget_s - self.spent_s


class CallerBudget:
    """The calling controller's budget. pay() deducts the substrate's
    REPORTED spend (A31/A32) and refuses when the grant's budget_s
    would be exceeded. Non-preemptive: in-flight spend already
    reported is never clawed back."""

    def __init__(self, allocation: Allocation):
        self.allocation = allocation
        self.state = BudgetState(budget_s=allocation.budget_s)
        self.payments: List[Dict[str, Any]] = []

    def pay(self, report: SpendReport) -> Dict[str, Any]:
        if report.controller_id != self.allocation.controller_id:
            raise BudgetExceeded(
                f"spend reported for {report.controller_id!r} cannot be "
                f"paid by {self.allocation.controller_id!r}'s budget")
        if report.cpu_seconds > self.state.remaining_s:
            raise BudgetExceeded(
                f"controller {self.allocation.controller_id}: spend "
                f"{report.cpu_seconds:.4f}s exceeds remaining "
                f"{self.state.remaining_s:.4f}s of grant "
                f"{self.allocation.grant_id}")
        self.state.spent_s += report.cpu_seconds
        payment = {"call_id": report.call_id,
                   "cpu_seconds": report.cpu_seconds,
                   "monetary_cost": report.monetary_cost,
                   "remaining_s": self.state.remaining_s,
                   "paid_at": time.time()}
        self.payments.append(payment)
        return payment


# ---------------------------------------------------------------------------
# enforcement stub (frozen vocabulary only)
# ---------------------------------------------------------------------------

class EnforcementStub(EnforcementStatusProvider):
    """Test double for the enforcement-state inlet. Speaks only the
    frozen state vocabulary; the real machine is CUR-P1B's."""

    def __init__(self, state: str = "RUNNING"):
        self._state = state

    def set_state(self, state: str) -> None:
        self._state = state

    def active_state(self, domain: str) -> str:
        return self._state


# ---------------------------------------------------------------------------
# admission board stub (stands in for Primary Acceptance, Phase 4+)
# ---------------------------------------------------------------------------

class TestAdmissionBoard:
    """Test double for the acceptance/admission authority. Writes
    admission records in the measurement shape the FRM consumes.
    Every record is labeled decided_by="test_admission_board" — it
    never claims to be Primary Acceptance."""

    def __init__(self, chain: ChainLedger):
        self._chain = chain

    def decide(self, result_ref: str, verdict: str, yield_value: float = 0.0,
               accepted_portion: float = 1.0) -> AdmissionRecord:
        record = AdmissionRecord(
            admission_id=str(uuid.uuid4()),
            result_ref=result_ref,
            verdict=verdict,
            yield_value=yield_value,
            accepted_portion=accepted_portion,
            decided_by="test_admission_board",
        ).validate()
        return self._chain.record_admission(record)


# ---------------------------------------------------------------------------
# inquiry driver — the test-double controller
# ---------------------------------------------------------------------------

class InquiryDriver:
    """Drives one inquiry end-to-end through the real chain:
    allocate → metered substrate calls (real spend) → budget pays →
    expenditure recorded → work/result recorded → admission →
    attribution link recomputed."""

    def __init__(self, controller_id: str, originating_executive: str,
                 alloc_ledger: AllocationLedger, exp_ledger: ExpenditureLedger,
                 chain: ChainLedger, board: TestAdmissionBoard,
                 substrate: MeteredSubstrate):
        self.controller_id = controller_id
        self.originating_executive = originating_executive
        self._alloc_ledger = alloc_ledger
        self._exp_ledger = exp_ledger
        self._chain = chain
        self._board = board
        self._substrate = substrate
        self._allocation: Optional[Allocation] = None
        self._budget: Optional[CallerBudget] = None
        self._grant_id = ""

    def allocate(self, grant: FrmGrant, enforcement: EnforcementStatusProvider,
                 domain: str = "curiosity") -> Allocation:
        self._alloc_ledger.record_grant(grant)
        self._allocation = self._alloc_ledger.allocate(
            grant.grant_id, self.controller_id, self.originating_executive,
            enforcement, domain)
        self._budget = CallerBudget(self._allocation)
        self._grant_id = grant.grant_id
        return self._allocation

    def spend(self, inquiry_id: str, work_ref: str,
              payload: bytes, iterations: int = 2000) -> Expenditure:
        """One real substrate call: real work, measured, paid, recorded."""
        assert self._budget is not None, "allocate first"
        call = self._substrate.call(self.controller_id, inquiry_id,
                                    work_ref, payload, iterations)
        self._budget.pay(call.report)          # A32: caller's budget pays
        return self._exp_ledger.record(self._grant_id, call.report)

    def work(self, inquiry_id: str, attempt_no: int = 1,
             parent_work_ref: Optional[str] = None,
             kind: str = "execution",
             acquired_capability_id: Optional[str] = None) -> WorkUnit:
        w = WorkUnit(work_ref=f"work-{uuid.uuid4().hex[:12]}",
                     inquiry_id=inquiry_id, grant_id=self._grant_id,
                     controller_id=self.controller_id, attempt_no=attempt_no,
                     parent_work_ref=parent_work_ref, kind=kind,
                     acquired_capability_id=acquired_capability_id)
        return self._chain.record_work(w)

    def result(self, work_ref: str, outcome: str,
               detail: str = "") -> Result:
        r = Result(result_ref=f"res-{uuid.uuid4().hex[:12]}",
                   work_ref=work_ref, outcome=outcome, detail=detail)
        return self._chain.record_result(r)

    def admit(self, result_ref: str, verdict: str,
              yield_value: float = 0.0,
              accepted_portion: float = 1.0) -> AdmissionRecord:
        return self._board.decide(result_ref, verdict, yield_value,
                                  accepted_portion)

    def link(self, work_ref: str):
        return recompute_attribution(work_ref, self._chain,
                                     self._exp_ledger,
                                     self.originating_executive)

    def aggregate(self, work_ref: str):
        return aggregate_attribution(work_ref, self._chain,
                                     self._exp_ledger,
                                     self.originating_executive)

    @property
    def budget(self) -> CallerBudget:
        assert self._budget is not None
        return self._budget


def make_substrate(model_provider: str,
                   declared_rate_per_cpu_s: float = 0.0) -> MeteredSubstrate:
    """A metered substrate whose calls really spend."""
    return MeteredSubstrate(real_substrate_work, model_provider,
                            declared_rate_per_cpu_s)
