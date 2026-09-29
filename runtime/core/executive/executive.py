"""The Executive Controller: level 1 of the three-level hierarchy.

"What boundary exists?" -> "Which loop owns it?" -> "Enter that loop."

The executive owns the six loop registrations. It selects the owning loop
for a VALIDATED boundary presentation and enters that loop through the
loop's real entry point (the inlet). It never sees the microcontroller
population: the only type it consumes above the loop level is LoopView
(the frozen microcontroller-interface/v1 type) -- enforced by
construction, because the executive holds no reference to any
Microcontroller object and its inlets surface only LoopView aggregates.

Fail-closed selection:
  - boundary fails validation -> UNOWNED (BoundaryRefused at validate()).
  - owning loop registered ABSENT -> the absence is named; nothing is
    entered, nothing is faked.
  - owning loop registered REAL -> the inlet enters the real machinery.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from ..microcontroller import (
    LOOPS,
    MicrocontrollerSubstrate,
)
from .boundary import (
    BOUNDARY_ACQUISITION_GAP,
    BOUNDARY_COMPLETION_CANDIDATE,
    BOUNDARY_EXECUTION_FAILURE,
    BOUNDARY_KINDS,
    BOUNDARY_NOVEL_TASK,
    BOUNDARY_RUN_WAKE,
    BOUNDARY_TECHNIQUE_DELTA,
    BoundaryPresentation,
    BoundaryRefused,
)
from .loops import (
    LOOP_STATE_ABSENT,
    LOOP_STATE_REAL,
    INLETS,
    LOOP_ACCEPTANCE,
    LOOP_ACQUISITION,
    LOOP_DISTILLATION,
    LOOP_EXECUTION,
    LOOP_GENERALIZATION,
    LOOP_RUN,
    Q1_COGNITION_DRIVER,
    Q1_DRIVE_CONTRACT,
    AcceptanceLoopInlet,
    AcquisitionLoopInlet,
    DistillationLoopInlet,
    ExecutionRepairLoopInlet,
    GeneralizationLoopInlet,
    LoopInlet,
    LoopOutcome,
    LoopRegistration,
    RunLoopInlet,
)
from .relevance import (
    Finding,
    OperationalObjective,
    RelevanceDecision,
    RelevanceGate,
    RelevanceRefused,
)

#: The declared ownership map: boundary class -> owning loop. This is a
#: declared architectural fact (James's six-loop hierarchy: each loop
#: manages convergence for its boundary class), not a learned guess.
#: The *recognition* of what boundary exists comes from the observing
#: machinery's validated structured record; this map only answers
#: "which loop owns this boundary class".
BOUNDARY_OWNERSHIP: Dict[str, str] = {
    BOUNDARY_RUN_WAKE: LOOP_RUN,
    BOUNDARY_ACQUISITION_GAP: LOOP_ACQUISITION,
    BOUNDARY_EXECUTION_FAILURE: LOOP_EXECUTION,
    BOUNDARY_COMPLETION_CANDIDATE: LOOP_ACCEPTANCE,
    BOUNDARY_TECHNIQUE_DELTA: LOOP_DISTILLATION,
    BOUNDARY_NOVEL_TASK: LOOP_GENERALIZATION,
}

#: Per-loop admission budgets on the executive's substrate. Modest: the
#: substrate hosts loop-internal work; the RunController keeps run-level
#: admission (its V10-P2 ownership is unchanged).
_LOOP_BUDGET_S = 600.0
_LOOP_MAX_CONCURRENT = 16

DECISION_ROUTED = "routed"
DECISION_ABSENT = "absent"
DECISION_UNOWNED = "unowned"


@dataclass
class RoutingDecision:
    """The executive's selection for one boundary."""
    boundary_id: str
    kind: str
    status: str  # routed | absent | unowned
    selected_loop: Optional[str]
    reason: str


class ExecutiveController:
    """Level 1: selects the owning loop for a validated boundary and
    enters it. Constructed with the live engine and the existing
    machinery; the engine is INJECTED (never constructed here -- the
    single-owner lock forbids a second live engine per process).

    absent_loops: loop names to register ABSENT (for degraded states or
        future loops whose machinery does not exist yet). The executive
        names the absence instead of routing to a fake.
    """

    def __init__(self, *, engine: Any, run_controller: Any,
                 gap_registry: Any, acceptance_loop: Any,
                 epistemic: Any = None,
                 substrate: Optional[MicrocontrollerSubstrate] = None,
                 absent_loops: tuple = (),
                 relevance_gate: Optional[RelevanceGate] = None) -> None:
        self._engine = engine
        self._run_controller = run_controller
        self._gap_registry = gap_registry
        self._acceptance_loop = acceptance_loop
        self._epistemic = (epistemic if epistemic is not None
                         else engine.intellect.epistemic)
        self._relevance_gate = relevance_gate
        self._substrate = substrate or MicrocontrollerSubstrate()
        for loop in LOOPS:
            self._substrate.register_loop(
                loop, budget_s=_LOOP_BUDGET_S,
                max_concurrent=_LOOP_MAX_CONCURRENT)
        self._registrations: Dict[str, LoopRegistration] = {}
        for loop in LOOPS:
            if loop in absent_loops:
                self._registrations[loop] = LoopRegistration(
                    loop=loop, state=LOOP_STATE_ABSENT,
                    absent_reason=(
                        f"{loop} registered ABSENT by construction: no "
                        "real loop-controller machinery is wired; the "
                        "executive names the absence rather than "
                        "entering a stub"))
            else:
                self._registrations[loop] = LoopRegistration(
                    loop=loop, state=LOOP_STATE_REAL,
                    inlet=self._build_inlet(loop))
        # Q1 settlement, recorded on the Acquisition registration:
        # explicitly subordinated -- see loops.Q1_DRIVE_CONTRACT.
        acq = self._registrations.get(LOOP_ACQUISITION)
        if acq is not None and acq.state == LOOP_STATE_REAL:
            acq.cognition_driver = Q1_COGNITION_DRIVER
            acq.drive_contract = Q1_DRIVE_CONTRACT

    # -- construction ---------------------------------------------------
    def _build_inlet(self, loop: str) -> LoopInlet:
        if loop == LOOP_RUN:
            return RunLoopInlet(self._run_controller, self._substrate)
        if loop == LOOP_ACQUISITION:
            return AcquisitionLoopInlet(self._gap_registry, self._substrate)
        if loop == LOOP_EXECUTION:
            return ExecutionRepairLoopInlet(self._engine, self._substrate)
        if loop == LOOP_ACCEPTANCE:
            return AcceptanceLoopInlet(self._acceptance_loop, self._substrate)
        if loop == LOOP_DISTILLATION:
            return DistillationLoopInlet(
                self._engine, self._epistemic, self._substrate)
        if loop == LOOP_GENERALIZATION:
            return GeneralizationLoopInlet(
                self._engine, self._epistemic, self._substrate)
        raise ValueError(f"no inlet defined for loop {loop!r}")

    # -- selection ------------------------------------------------------
    def route(self, boundary: BoundaryPresentation) -> RoutingDecision:
        """Select the owning loop for a boundary. Validates first;
        validation failure -> UNOWNED (fail closed)."""
        try:
            boundary.validate()
        except BoundaryRefused as exc:
            return RoutingDecision(
                boundary_id=boundary.boundary_id, kind=boundary.kind,
                status=DECISION_UNOWNED, selected_loop=None,
                reason=f"boundary refused: {exc}")
        owner = BOUNDARY_OWNERSHIP.get(boundary.kind)
        if owner is None:
            return RoutingDecision(
                boundary_id=boundary.boundary_id, kind=boundary.kind,
                status=DECISION_UNOWNED, selected_loop=None,
                reason=(f"kind {boundary.kind!r} is owned by no loop: "
                        "the executive does not guess"))
        reg = self._registrations.get(owner)
        if reg is None or reg.state == LOOP_STATE_ABSENT:
            reason = (reg.absent_reason if reg is not None
                      else f"loop {owner!r} is not registered")
            return RoutingDecision(
                boundary_id=boundary.boundary_id, kind=boundary.kind,
                status=DECISION_ABSENT, selected_loop=owner,
                reason=reason)
        return RoutingDecision(
            boundary_id=boundary.boundary_id, kind=boundary.kind,
            status=DECISION_ROUTED, selected_loop=owner,
            reason=f"{boundary.kind} is owned by the {owner} loop")

    # -- entry ----------------------------------------------------------
    def enter(self, boundary: BoundaryPresentation) -> LoopOutcome:
        """Route the boundary and enter the owning loop's real entry
        point. Absent/unowned boundaries are named, never entered."""
        decision = self.route(boundary)
        if decision.status != DECISION_ROUTED:
            return LoopOutcome(
                loop=decision.selected_loop or "unowned",
                entered=False, result=None,
                detail=(f"not entered: {decision.status}: "
                        f"{decision.reason}"),
                view=None)
        reg = self._registrations[decision.selected_loop]
        assert reg.inlet is not None  # routed => real => inlet built
        return reg.inlet.enter(boundary)

    # -- findings inlet: relevance ownership (charter C-3, PLOOP-7) --------
    def set_operational_objective(
            self, objective: OperationalObjective) -> None:
        """Set the objective relevance is judged against. Fail closed:
        without a relevance gate there is no relevance owner."""
        if self._relevance_gate is None:
            raise RelevanceRefused(
                "no relevance gate installed: the executive cannot own "
                "relevance decisions without one")
        self._relevance_gate.set_objective(objective)

    def submit_finding(self, finding: Finding) -> RelevanceDecision:
        """Submit a finding at the Primary inlet. The relevance gate --
        the Primary side's relevance owner -- decides: admitted, retained,
        or rejected. Every decision is persisted and re-checkable."""
        if self._relevance_gate is None:
            raise RelevanceRefused(
                "no relevance gate installed: findings cannot be accepted "
                "without a relevance decision")
        return self._relevance_gate.decide(finding)

    # -- executive-facing state: LoopView only, O(6) --------------------
    def loop_view(self, loop: str):
        """Aggregate loop state. The executive sees 'Acquisition is
        active' -- never the microcontroller population."""
        return self._substrate.loop_view(loop)

    def all_loop_views(self) -> Dict[str, Any]:
        return {loop: self._substrate.loop_view(loop) for loop in LOOPS}

    def registrations(self) -> Dict[str, LoopRegistration]:
        return dict(self._registrations)

    def ownership_map(self) -> Dict[str, str]:
        return dict(BOUNDARY_OWNERSHIP)
