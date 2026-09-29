"""REMOR Executive Controller -- level 1 of the three-level hierarchy.

James's standing architecture (2026-09-28): the Executive Controller selects
which of the six nested loops is active:

    "What boundary exists?" -> "Which loop owns it?" -> "Enter that loop."

The brain sits BEHIND the executive (understands, remembers, reasons,
learns); the executive selects the loop; loop controllers manage
convergence; microcontrollers operate graphs inside one loop.

What this module builds (only the missing selection/ownership layer):
  - BoundaryPresentation: a boundary carried as VALIDATED structured
    evidence. The executive never routes an unvalidated claim.
  - ExecutiveController: owns the six loop registrations, routes a
    validated boundary to its owning loop, and enters that loop through
    the loop's real entry point. Its view above the loop level is O(6)
    LoopView aggregates only -- never the microcontroller population
    (enforced by construction: the only type it consumes is LoopView).
  - Loop inlets (loops.py): one per loop, each invoking the REAL
    machinery that converges that loop's boundary class. A loop with no
    real machinery registers ABSENT and is named, never stubbed.
  - Loop handoffs (handoff.py): the contractual loop-to-loop
    transition. A loop's terminal outcome is classified from its real
    result type, looked up in the declared HANDOFF_ROUTES table, and --
    where the table declares a follow-on -- packaged as a validated
    LoopHandoff (terminal state, real evidence, measured resource
    accounting, chain-depth guard) whose acceptance rebuilds a validated
    BoundaryPresentation. Transitions are contractual, never ad-hoc;
    violations raise HandoffRefused, loudly.

What it reuses (called, never edited, never rebuilt):
  - MicrocontrollerSubstrate / LoopView (RUN-MICRO-1, frozen
    microcontroller-interface/v1): loop-scoped hosting and the
    executive-facing aggregate view. The substrate module is consumed
    additively; it is never modified.
  - RunController.tick (V10-P2): the Run loop's entry.
  - GapRegistry.dispatch (M7): the Acquisition loop's entry.
  - diagnose_quarantine (M5): the Execution+Repair loop's entry.
  - AcceptanceLoop.present (Q8): the Acceptance loop's entry.
  - DistillationLoop.distill (M2/V10-P4): the Distillation loop's entry.
  - generalize_for_task (V10-P5): the Generalization loop's entry.
  - CognitionLoop.cycle (Q1): the Acquisition loop's cognition driver,
    explicitly subordinated -- see OWNERSHIP.md.

Selection honesty (the mandate's classification, stated plainly):
  The executive performs EVIDENCE-DRIVEN ROUTING, not open-ended
  comprehension. The *recognition* of what boundary exists is performed
  by the observing machinery that emitted the validated structured
  record (M5's diagnosis, M7's gap record, the run controller's wake,
  the distillation session's delta). The executive maps a validated
  boundary class to its owning loop per the declared six-loop ownership
  map -- that mapping is a declared architectural fact (James's
  hierarchy), not a guess. A boundary whose evidence fails validation,
  or whose kind no loop owns, is refused fail-closed as UNOWNED -- the
  executive never guesses. Open-ended comprehension of novel boundary
  kinds the machinery has never emitted a record for is UNPROVEN; that
  is the brain's future work, not the executive's.
"""

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
from .executive import ExecutiveController, RoutingDecision
from .handoff import (
    HANDOFF_CONTRACT_VERSION,
    HANDOFF_ROUTES,
    MAX_HANDOFF_DEPTH,
    TERMINAL_STATES,
    TERMINAL_ABSENT,
    TERMINAL_CANDIDATE,
    TERMINAL_CONVERGED,
    TERMINAL_EXHAUSTED,
    TERMINAL_FAILED,
    TERMINAL_OPEN,
    TERMINAL_REFUSED,
    FollowOn,
    HandoffRefused,
    LoopHandoff,
    accept_handoff,
    classify_terminal,
    produce_handoff,
    route_owner,
    transition,
)
from .loops import (
    LOOP_STATE_ABSENT,
    LOOP_STATE_REAL,
    AcquisitionLoopInlet,
    AcceptanceLoopInlet,
    DistillationLoopInlet,
    ExecutionRepairLoopInlet,
    GeneralizationLoopInlet,
    LoopInlet,
    LoopOutcome,
    LoopRegistration,
    RunLoopInlet,
)
from .terminal_routing import (
    FINDING_STATE_REFUSAL,
    TERMINAL_ROUTING_VERSION,
    TerminalLedger,
    TerminalRoute,
    TerminalRouter,
)

__all__ = [
    "BOUNDARY_ACQUISITION_GAP",
    "BOUNDARY_COMPLETION_CANDIDATE",
    "BOUNDARY_EXECUTION_FAILURE",
    "BOUNDARY_KINDS",
    "BOUNDARY_NOVEL_TASK",
    "BOUNDARY_RUN_WAKE",
    "BOUNDARY_TECHNIQUE_DELTA",
    "BoundaryPresentation",
    "BoundaryRefused",
    "ExecutiveController",
    "RoutingDecision",
    "HANDOFF_CONTRACT_VERSION",
    "HANDOFF_ROUTES",
    "MAX_HANDOFF_DEPTH",
    "TERMINAL_STATES",
    "TERMINAL_ABSENT",
    "TERMINAL_CANDIDATE",
    "TERMINAL_CONVERGED",
    "TERMINAL_EXHAUSTED",
    "TERMINAL_FAILED",
    "TERMINAL_OPEN",
    "TERMINAL_REFUSED",
    "FollowOn",
    "HandoffRefused",
    "LoopHandoff",
    "accept_handoff",
    "classify_terminal",
    "produce_handoff",
    "route_owner",
    "transition",
    "LOOP_STATE_ABSENT",
    "LOOP_STATE_REAL",
    "AcquisitionLoopInlet",
    "AcceptanceLoopInlet",
    "DistillationLoopInlet",
    "ExecutionRepairLoopInlet",
    "GeneralizationLoopInlet",
    "LoopInlet",
    "LoopOutcome",
    "LoopRegistration",
    "RunLoopInlet",
    "TERMINAL_ROUTING_VERSION",
    "TerminalLedger",
    "TerminalRoute",
    "TerminalRouter",
    "FINDING_STATE_REFUSAL",
]
