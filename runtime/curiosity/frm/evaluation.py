"""runtime/curiosity/frm/evaluation.py

The FRM evaluation layer (FRM amendment sections 2, 10, 15, 16).

Per contention round the FinancialResourceManager:

  1. Takes James's standing policy (FrmPolicy), the enforcement state
     (validated label; the real machine is P1B's), each domain's demand,
     cost inputs, and provisional expected-yield estimates.
  2. Applies enforcement gating: zero allocation to Curiosity under any
     active enforcement state; WARNING_1 restricts curiosity demand to
     James's standing warning_1_cap_fraction.
  3. Refuses expensive-model requests outside James's standing
     authorization (amendment section 9) -- fail-closed.
  4. Hands the effective demands to the EXISTING ResourceArbitrator --
     a second INSTANCE of the class runtime/core/resource_arbitrator.py
     defines (no-duplication rule: the class is shared with the Primary
     path, the instance is FRM-owned).
  5. Verifies Primary's guaranteed minimum on the resulting grants
     (fail-closed post-check on top of the arbitrator's by-construction
     minimum guarantee).
  6. Computes epoch-bounded lending (amendment section 14): Primary's
     unused guaranteed minimum lent to Curiosity for THIS epoch only,
     recorded on the grant (lent=True), recalled at the epoch boundary.
  7. Appends the full round to the append-only epoch ledger.

Non-preemption is structural: issued FrmGrants are frozen dataclasses;
evaluate_round refuses to alter an active epoch's grants when new demand
arrives mid-epoch (the demand is recorded for the next round). There is
no API that shrinks a running grant.

Expected-yield estimates are recorded PROVISIONAL and non-binding
(policy.py) until empirical yield attribution (CUR-P1D) is wired.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence

from swarm_engine.core.resource_arbitrator import ResourceArbitrator
from swarm_engine.curiosity.frm.policy import (
    BANNED_6M,
    ENFORCEMENT_STATES,
    HARD_SHUTDOWN_RESOURCE,
    RUNNING,
    SUSPENDED_SAFETY,
    WARNING_1,
    ZERO_ALLOCATION_STATES,
    CostInput,
    DomainDemand,
    ExpectedYield,
    FrmPolicy,
)
from swarm_engine.curiosity.frm.grant import FrmGrant, LendingRecord
from swarm_engine.curiosity.frm.ledger import EpochLedger

PRIMARY = "primary"
CURIOSITY = "curiosity"


@dataclass
class FrmRound:
    """One evaluated contention round, fully inspectable."""
    epoch_id: int
    decided_at: float
    enforcement_state: str
    primary_demand_stated: DomainDemand
    curiosity_demand_stated: DomainDemand
    curiosity_demand_effective: DomainDemand
    grants: Dict[str, FrmGrant]
    expected_yields: Dict[str, ExpectedYield]
    cost_inputs: List[CostInput]
    notes: List[str] = field(default_factory=list)
    mid_epoch_refusal: bool = False

    def as_dict(self) -> Dict[str, Any]:
        return {
            "epoch_id": self.epoch_id,
            "decided_at": self.decided_at,
            "enforcement_state": self.enforcement_state,
            "primary_demand_stated": {
                "budget_s": self.primary_demand_stated.budget_s,
                "max_concurrent": self.primary_demand_stated.max_concurrent,
            },
            "curiosity_demand_stated": {
                "budget_s": self.curiosity_demand_stated.budget_s,
                "max_concurrent": self.curiosity_demand_stated.max_concurrent,
            },
            "curiosity_demand_effective": {
                "budget_s": self.curiosity_demand_effective.budget_s,
                "max_concurrent": self.curiosity_demand_effective.max_concurrent,
            },
            "grants": {d: g.as_dict() for d, g in self.grants.items()},
            "grant_detail": {
                d: {
                    "domain": g.domain,
                    "lending": g.lending.as_dict(),
                    "enforcement_state_at_issue":
                        g.enforcement_state_at_issue,
                    "note": g.note,
                } for d, g in self.grants.items()},
            "expected_yields": {
                d: {"value": y.value, "basis": y.basis,
                    "provisional": y.provisional}
                for d, y in self.expected_yields.items()},
            "cost_inputs": [
                {"kind": c.kind, "value": c.value,
                 "provenance": c.provenance}
                for c in self.cost_inputs],
            "notes": list(self.notes),
            "mid_epoch_refusal": self.mid_epoch_refusal,
        }


@dataclass
class EpochClose:
    """Recall of epoch-bounded lending at the epoch boundary."""
    epoch_id: int
    closed_at: float
    recalled_lent_budget_s: float
    recalled_lent_concurrent: int
    notes: List[str] = field(default_factory=list)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "epoch_id": self.epoch_id,
            "closed_at": self.closed_at,
            "recalled_lent_budget_s": self.recalled_lent_budget_s,
            "recalled_lent_concurrent": self.recalled_lent_concurrent,
            "notes": list(self.notes),
        }


class FinancialResourceManager:
    """Evaluates James's standing allocation policy each contention round.

    Owns one ResourceArbitrator INSTANCE (the same class the Primary
    path uses) configured for the two executive domains. Primary is
    registered with its guaranteed minimum as the arbitrator-level
    anti-starvation minimum: the guarantee is mechanical, not cooperative.
    """

    def __init__(self, policy: FrmPolicy,
                 clock: Callable[[], float] = time.monotonic,
                 ledger_path: Optional[str] = None) -> None:
        if not isinstance(policy, FrmPolicy):
            raise ValueError("policy must be an FrmPolicy")
        self._policy = policy
        self._clock = clock
        # Second INSTANCE of the existing class -- never a second class.
        self._arbitrator = ResourceArbitrator(
            total_budget_s=policy.total_budget_s,
            total_max_concurrent=policy.total_max_concurrent,
            epoch_length_s=policy.epoch_s,
            clock=clock)
        self._arbitrator.register_loop(
            PRIMARY, weight=policy.primary_weight,
            minimum_budget_s=policy.primary_minimum_budget_s,
            minimum_concurrent=policy.primary_minimum_concurrent)
        self._arbitrator.register_loop(
            CURIOSITY, weight=policy.curiosity_weight,
            minimum_budget_s=0.0, minimum_concurrent=0)
        self._ledger = EpochLedger(ledger_path)
        self._active_round: Optional[FrmRound] = None
        self._pending_demands: List[Dict[str, Any]] = []

    # -- one contention round ----------------------------------------------

    def evaluate_round(self, *, enforcement_state: str,
                       primary_demand: DomainDemand,
                       curiosity_demand: DomainDemand,
                       cost_inputs: Sequence[CostInput] = (),
                       expected_yields: Optional[Dict[str, ExpectedYield]] = None
                       ) -> FrmRound:
        """Evaluate one contention round and issue epoch grants."""
        if enforcement_state not in ENFORCEMENT_STATES:
            raise ValueError(
                f"unknown enforcement state {enforcement_state!r}")
        if primary_demand.domain != PRIMARY or curiosity_demand.domain != CURIOSITY:
            raise ValueError("demands must be labeled primary|curiosity")
        for ci in cost_inputs:
            if not isinstance(ci, CostInput):
                raise ValueError("cost_inputs must be CostInput records")
        yields = expected_yields or {}
        notes: List[str] = []

        # Non-preemption: an active epoch's grants are never altered by
        # mid-epoch demand. The new demand is recorded for the next round.
        if self._active_round is not None:
            self._pending_demands.append({
                "enforcement_state": enforcement_state,
                "primary_demand": _demand_summary(primary_demand),
                "curiosity_demand": _demand_summary(curiosity_demand),
            })
            refusal = FrmRound(
                epoch_id=self._active_round.epoch_id,
                decided_at=self._clock(),
                enforcement_state=enforcement_state,
                primary_demand_stated=primary_demand,
                curiosity_demand_stated=curiosity_demand,
                curiosity_demand_effective=self._active_round
                .curiosity_demand_effective,
                grants=dict(self._active_round.grants),
                expected_yields=dict(self._active_round.expected_yields),
                cost_inputs=list(cost_inputs),
                notes=[(
                    "mid-epoch demand change REFUSED: epoch "
                    f"{self._active_round.epoch_id} grants are active and "
                    "non-preemptive; demand recorded for the next round")],
                mid_epoch_refusal=True,
            )
            self._ledger.append_round(
                self._active_round.epoch_id, refusal.as_dict(),
                recorded_at=self._clock())
            return refusal

        notes.append(
            "expected-yield estimates are PROVISIONAL (no empirical yield "
            "attribution wired -- CUR-P1D boundary): recorded for audit, "
            "non-binding on this round's grants")

        # -- enforcement gating -------------------------------------------
        effective_curiosity = curiosity_demand
        if enforcement_state in ZERO_ALLOCATION_STATES:
            effective_curiosity = DomainDemand(
                domain=CURIOSITY, budget_s=0.0, max_concurrent=0,
                expected_yield=curiosity_demand.expected_yield)
            notes.append(
                f"enforcement state {enforcement_state}: curiosity demand "
                "forced to zero -- zero allocation even with stated demand")
        elif enforcement_state == WARNING_1:
            frac = self._policy.warning_1_cap_fraction
            effective_curiosity = DomainDemand(
                domain=CURIOSITY,
                budget_s=curiosity_demand.budget_s * frac,
                max_concurrent=int(curiosity_demand.max_concurrent * frac),
                expected_yield=curiosity_demand.expected_yield,
                requests_expensive_model=(
                    curiosity_demand.requests_expensive_model),
                expensive_model_name=curiosity_demand.expensive_model_name)
            notes.append(
                f"WARNING_1: curiosity demand restricted to "
                f"{frac:g} of stated demand per James's standing policy")

        # -- expensive-model authorization (amendment section 9) ------------
        effective_primary = primary_demand
        for label, demand in (("primary", primary_demand),
                              ("curiosity", effective_curiosity)):
            if demand.requests_expensive_model and not self._policy.expensive_models.authorizes(
                    demand.expensive_model_name or ""):
                notes.append(
                    f"{label} requested expensive model "
                    f"{demand.expensive_model_name!r}: NOT authorized by "
                    "James's standing policy -- demand refused fail-closed")
                if label == "primary":
                    effective_primary = DomainDemand(
                        domain=PRIMARY, budget_s=0.0, max_concurrent=0,
                        expected_yield=demand.expected_yield)
                else:
                    effective_curiosity = DomainDemand(
                        domain=CURIOSITY, budget_s=0.0, max_concurrent=0,
                        expected_yield=demand.expected_yield)

        # -- mechanical mapping via the shared arbitrator class -------------
        self._arbitrator.set_demand(
            PRIMARY, budget_s=effective_primary.budget_s,
            concurrent=effective_primary.max_concurrent)
        self._arbitrator.set_demand(
            CURIOSITY, budget_s=effective_curiosity.budget_s,
            concurrent=effective_curiosity.max_concurrent)
        decision = self._arbitrator.arbitrate()
        epoch_id = decision.epoch_id
        now = self._clock()

        # -- fail-closed post-check: Primary's guaranteed minimum -----------
        p_grant = decision.grants.get(PRIMARY)
        if p_grant is not None and (
                effective_primary.budget_s
                >= self._policy.primary_minimum_budget_s):
            if p_grant.budget_s < self._policy.primary_minimum_budget_s:
                raise RuntimeError(
                    "FRM INVARIANT VIOLATED: primary grant "
                    f"{p_grant.budget_s:.1f}s below guaranteed minimum "
                    f"{self._policy.primary_minimum_budget_s:.1f}s")
            if p_grant.max_concurrent < min(
                    effective_primary.max_concurrent,
                    self._policy.primary_minimum_concurrent):
                raise RuntimeError(
                    "FRM INVARIANT VIOLATED: primary concurrency grant "
                    f"{p_grant.max_concurrent} below guaranteed minimum")
            notes.append(
                "post-check: primary grant "
                f"{p_grant.budget_s:.1f}s/{p_grant.max_concurrent} slots >= "
                "guaranteed minimum "
                f"{self._policy.primary_minimum_budget_s:.1f}s/"
                f"{self._policy.primary_minimum_concurrent} slots -- holds")

        # -- James's curiosity ceilings ------------------------------------
        c_grant = decision.grants.get(CURIOSITY)
        c_budget = c_grant.budget_s if c_grant else 0.0
        c_conc = c_grant.max_concurrent if c_grant else 0
        if self._policy.curiosity_ceiling_budget_s is not None:
            if c_budget > self._policy.curiosity_ceiling_budget_s:
                notes.append(
                    f"curiosity ceiling: grant clamped "
                    f"{c_budget:.1f}s -> "
                    f"{self._policy.curiosity_ceiling_budget_s:.1f}s")
                c_budget = self._policy.curiosity_ceiling_budget_s
        if self._policy.curiosity_ceiling_concurrent is not None:
            if c_conc > self._policy.curiosity_ceiling_concurrent:
                notes.append(
                    f"curiosity ceiling: grant clamped {c_conc} -> "
                    f"{self._policy.curiosity_ceiling_concurrent} slots")
                c_conc = self._policy.curiosity_ceiling_concurrent

        # -- epoch-bounded lending (amendment section 14) -------------------
        # Primary's unused guaranteed minimum is lendable THIS epoch only.
        p_budget = p_grant.budget_s if p_grant else 0.0
        p_conc = p_grant.max_concurrent if p_grant else 0
        unused_min_b = max(
            0.0, self._policy.primary_minimum_budget_s - p_budget)
        unused_min_c = max(
            0, self._policy.primary_minimum_concurrent - p_conc)
        lent_b = min(unused_min_b, c_budget) if c_budget > 0 else 0.0
        lent_c = min(unused_min_c, c_conc) if c_conc > 0 else 0
        if lent_b > 0 or lent_c > 0:
            notes.append(
                f"epoch-bounded lending: {lent_b:.1f}s/{lent_c} slots of "
                "primary's unused guaranteed minimum lent to curiosity "
                f"for epoch {epoch_id} only; recalled at the boundary")

        grants = {
            PRIMARY: FrmGrant.issue(
                domain=PRIMARY, epoch_id=epoch_id,
                epoch_s=self._policy.epoch_s,
                budget_s=p_budget, max_concurrent=p_conc,
                primary_minimum_budget_s=self._policy.primary_minimum_budget_s,
                primary_minimum_concurrent=self._policy.primary_minimum_concurrent,
                lent=False,
                lending=LendingRecord(0.0, 0),
                enforcement_state_at_issue=enforcement_state,
                issued_at=now),
            CURIOSITY: FrmGrant.issue(
                domain=CURIOSITY, epoch_id=epoch_id,
                epoch_s=self._policy.epoch_s,
                budget_s=c_budget, max_concurrent=c_conc,
                primary_minimum_budget_s=self._policy.primary_minimum_budget_s,
                primary_minimum_concurrent=self._policy.primary_minimum_concurrent,
                lent=(lent_b > 0 or lent_c > 0),
                lending=LendingRecord(lent_b, lent_c),
                enforcement_state_at_issue=enforcement_state,
                issued_at=now,
                note=("includes epoch-bounded lent capacity" if lent_b > 0
                      or lent_c > 0 else "")),
        }

        round_ = FrmRound(
            epoch_id=epoch_id, decided_at=now,
            enforcement_state=enforcement_state,
            primary_demand_stated=primary_demand,
            curiosity_demand_stated=curiosity_demand,
            curiosity_demand_effective=effective_curiosity,
            grants=grants,
            expected_yields={
                PRIMARY: yields.get(PRIMARY, primary_demand.expected_yield),
                CURIOSITY: yields.get(CURIOSITY,
                                      curiosity_demand.expected_yield),
            },
            cost_inputs=list(cost_inputs),
            notes=notes,
        )
        self._active_round = round_
        self._ledger.append_round(epoch_id, round_.as_dict(),
                                  recorded_at=now)
        return round_

    # -- epoch boundary ------------------------------------------------------

    def advance_epoch(self) -> EpochClose:
        """Close the active epoch: recall lent capacity, free the round.

        Recall affects the NEXT allocation decision and admission only --
        running work admitted against the closing epoch's grants is never
        preempted (amendment section 14: no instantaneous recall without
        preemption machinery).
        """
        now = self._clock()
        if self._active_round is None:
            close = EpochClose(epoch_id=self._arbitrator.epoch_id,
                               closed_at=now,
                               recalled_lent_budget_s=0.0,
                               recalled_lent_concurrent=0,
                               notes=["no active round: nothing to recall"])
        else:
            c_grant = self._active_round.grants[CURIOSITY]
            close = EpochClose(
                epoch_id=self._active_round.epoch_id, closed_at=now,
                recalled_lent_budget_s=c_grant.lending.lent_budget_s,
                recalled_lent_concurrent=c_grant.lending.lent_concurrent,
                notes=[
                    "lent capacity recalled at epoch boundary: the next "
                    "round re-evaluates from zero lending",
                    "running work admitted against the closing grants is "
                    "NOT preempted (non-preemptive grants stand)",
                ])
            self._active_round = None
        self._arbitrator.clear_demands()
        self._ledger.append_epoch_close(close.epoch_id, close.as_dict(),
                                        recorded_at=now)
        return close

    # -- inspection ----------------------------------------------------------

    @property
    def policy(self) -> FrmPolicy:
        return self._policy

    @property
    def ledger(self) -> EpochLedger:
        return self._ledger

    @property
    def active_round(self) -> Optional[FrmRound]:
        return self._active_round

    @property
    def pending_demands(self) -> List[Dict[str, Any]]:
        return list(self._pending_demands)

    @property
    def arbitrator(self) -> ResourceArbitrator:
        """The FRM-owned second arbitrator INSTANCE (inspection only)."""
        return self._arbitrator


def _demand_summary(d: DomainDemand) -> Dict[str, Any]:
    return {"budget_s": d.budget_s, "max_concurrent": d.max_concurrent,
            "requests_expensive_model": d.requests_expensive_model,
            "expensive_model_name": d.expensive_model_name}
