"""Loop inlets: the executive's entries into the six loops.

Each inlet is a THIN adapter around the real machinery that converges its
loop's boundary class. An inlet is not the loop controller -- the full
controllers (with their microcontroller populations and convergence
management) are the other tracks' missions (ACQ-CTRL-1, EXE-CTRL-1,
ACC-CTRL-1, DIS-CTRL-1, GEN-CTRL-1), built against the frozen
microcontroller interface. The inlet is the executive's honest entry point:
"enter that loop" means invoking the loop's real entry below.

A loop with no real machinery registers ABSENT (LoopRegistration with
state="absent"): the executive names the absence, never a stub.

The executive above the loop level consumes only LoopView (the frozen
type). Inlets that host work on the substrate do so with loop names only;
no Microcontroller object, id, or purpose ever crosses to the executive.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from .boundary import (
    BOUNDARY_ACQUISITION_GAP,
    BOUNDARY_COMPLETION_CANDIDATE,
    BOUNDARY_EXECUTION_FAILURE,
    BOUNDARY_NOVEL_TASK,
    BOUNDARY_RUN_WAKE,
    BOUNDARY_TECHNIQUE_DELTA,
    BoundaryPresentation,
)

LOOP_STATE_REAL = "real"
LOOP_STATE_ABSENT = "absent"

# The six loop names, shared with the frozen substrate's LOOPS.
LOOP_RUN = "run"
LOOP_ACQUISITION = "acquisition"
LOOP_EXECUTION = "execution"
LOOP_ACCEPTANCE = "acceptance"
LOOP_DISTILLATION = "distillation"
LOOP_GENERALIZATION = "generalization"

#: Q1 settlement (RUN-EXEC-1 decision: EXPLICITLY SUBORDINATE).
#: Q1's CognitionLoop is the Acquisition loop's cognition driver. It is
#: retained (Q1 owns the file; no rewrite), and it is driven ONLY through
#: AcquisitionLoopInlet.drive_cognition -- never directly, and never by the
#: RunController (which keeps its V10-P2 no-double-drive discipline: the V10
#: generation is driven by V10-P4's sweep, which the RunController clocks).
#: Until the full Acquisition loop controller exists (ACQ-CTRL-1), nothing
#: drives it on a schedule; the executive records that state honestly.
Q1_COGNITION_DRIVER = "swarm_engine.acquisition.loop_driver.CognitionLoop"
Q1_DRIVE_CONTRACT = (
    "driven only through AcquisitionLoopInlet.drive_cognition, hosted in a "
    "microcontroller with loop='acquisition' against the inlet's cycle "
    "budget; the RunController never drives CognitionLoop.cycle() (V10-P2 "
    "no-double-drive discipline); scheduled driving awaits ACQ-CTRL-1."
)


@dataclass
class LoopOutcome:
    """What entering a loop produced. `view` is a LoopView (aggregate
    only) or None when the loop was absent/unowned and nothing ran."""
    loop: str
    entered: bool
    result: Any = None
    detail: str = ""
    view: Any = None  # LoopView -- the only above-loop type permitted


@dataclass
class LoopRegistration:
    """The executive's record of one loop: real machinery or named absence."""
    loop: str
    state: str  # LOOP_STATE_REAL | LOOP_STATE_ABSENT
    inlet: Optional["LoopInlet"] = None
    absent_reason: str = ""
    # Named cognition driver + its drive contract, where the loop has one.
    cognition_driver: Optional[str] = None
    drive_contract: str = ""


class LoopInlet:
    """Base inlet: enter() invokes the loop's real entry point."""

    loop = "undefined"

    def enter(self, boundary: BoundaryPresentation) -> LoopOutcome:
        raise NotImplementedError


class RunLoopInlet(LoopInlet):
    """Enter the Run loop: one authorized tick of the V10-P2 RunController.

    The executive does not own cadence -- the RunController does (James's
    2026-09-27 decision, standing). Entering the Run loop means the
    controller takes one tick: gap queue (user-gaps first), distillation
    sweep, quarantine sweep + Q7 triggers, observe.
    """

    loop = LOOP_RUN

    def __init__(self, run_controller: Any, substrate: Any) -> None:
        self._controller = run_controller
        self._substrate = substrate

    def enter(self, boundary: BoundaryPresentation) -> LoopOutcome:
        summary = self._controller.tick()
        return LoopOutcome(
            loop=self.loop, entered=True, result=summary,
            detail=(f"RunController.tick: {len(summary.get('gaps', []))} "
                    f"gaps, errors={len(summary.get('errors', []))}"),
            view=self._substrate.loop_view(self.loop))


class AcquisitionLoopInlet(LoopInlet):
    """Enter the Acquisition loop: M7's GapRegistry.dispatch on the real
    gap record. A failed acquisition leaves the gap OPEN with the reason
    visible -- that honest outcome is the loop's real behavior, not a
    refusal of entry."""

    loop = LOOP_ACQUISITION

    def __init__(self, gap_registry: Any, substrate: Any,
                 cognition_cycle_budget_s: float = 600.0) -> None:
        self._registry = gap_registry
        self._substrate = substrate
        self._cognition_cycle_budget_s = float(cognition_cycle_budget_s)

    def enter(self, boundary: BoundaryPresentation) -> LoopOutcome:
        record = boundary.evidence["gap_record"]
        dispatch_result = self._registry.dispatch(record.gap_id)
        return LoopOutcome(
            loop=self.loop, entered=True, result=dispatch_result,
            detail=(f"GapRegistry.dispatch({record.gap_id}): "
                    f"routed={dispatch_result.routed} "
                    f"route={dispatch_result.route_name} "
                    f"outcome={dispatch_result.outcome}"),
            view=self._substrate.loop_view(self.loop))

    # -- Q1 subordination: the only authorized drive path ---------------
    def drive_cognition(self, cognition_loop: Any,
                        budget_s: Optional[float] = None) -> Dict[str, Any]:
        """Drive one Q1 CognitionLoop.cycle() under Acquisition ownership.

        This method is the ONLY authorized caller of CognitionLoop.cycle()
        in the runtime: the cycle is hosted in a microcontroller with
        loop='acquisition' (the seam RUN-MICRO-1 declared), bounded by the
        inlet's cycle budget. The RunController never calls this path.
        """
        budget = (self._cognition_cycle_budget_s if budget_s is None
                  else float(budget_s))
        spawn = self._substrate.spawn(
            loop=self.loop, purpose="cognition_cycle", budget_s=budget)
        if not spawn.ok:
            return {"driven": False,
                    "refusal": spawn.refusal.as_dict()
                    if spawn.refusal else {"reason": "unknown"}}
        mc_id = spawn.mc.mc_id
        try:
            report = cognition_loop.cycle(time_budget_s=budget)
            outcome = "resolved"
        except Exception as exc:  # the driver never raises, but never trust
            report = {"error": f"{type(exc).__name__}: {exc}"}
            outcome = "exhausted"
        finally:
            self._substrate.retire(mc_id, outcome=outcome, loop=self.loop)
        return {"driven": True, "cycle_report": report,
                "hosted": {"loop": self.loop, "budget_s": budget,
                           "outcome": outcome},
                "view": self._substrate.loop_view(self.loop).as_dict()}


class ExecutionRepairLoopInlet(LoopInlet):
    """Enter the Execution+Repair loop: M5's diagnose_quarantine on the
    quarantined capability. Diagnosis is the loop's entry; repair follows
    the diagnosis inside the loop (not the executive's business)."""

    loop = LOOP_EXECUTION

    def __init__(self, engine: Any, substrate: Any) -> None:
        self._engine = engine
        self._substrate = substrate

    def enter(self, boundary: BoundaryPresentation) -> LoopOutcome:
        from swarm_engine.synthesis.integrity import diagnose_quarantine
        capability_id = boundary.evidence["capability_id"]
        diagnosis = diagnose_quarantine(self._engine, capability_id)
        return LoopOutcome(
            loop=self.loop, entered=True, result=diagnosis,
            detail=(f"diagnose_quarantine({capability_id}): "
                    f"quarantined={diagnosis.quarantined} "
                    f"verdict={diagnosis.verdict} "
                    f"reason={str(diagnosis.reason)[:120]}"),
            view=self._substrate.loop_view(self.loop))


class AcceptanceLoopInlet(LoopInlet):
    """Enter the Acceptance loop: Q8's present() -- a completed attempt
    that passed authentication becomes a CANDIDATE awaiting verdict.
    Completed is a candidate state, never terminal."""

    loop = LOOP_ACCEPTANCE

    def __init__(self, acceptance_loop: Any, substrate: Any) -> None:
        self._acceptance = acceptance_loop
        self._substrate = substrate

    def enter(self, boundary: BoundaryPresentation) -> LoopOutcome:
        ev = boundary.evidence
        record = self._acceptance.present(
            ev["run_id"], ev["goal"], ev["attempt"], ev["auth"])
        return LoopOutcome(
            loop=self.loop, entered=True, result=record,
            detail=(f"present({ev['run_id']}): state={record.state}"),
            view=self._substrate.loop_view(self.loop))


class DistillationLoopInlet(LoopInlet):
    """Enter the Distillation loop: DistillationLoop.distill on the real
    M2 delta record. Success or named failure -- both are the loop's real
    behavior; the executive routes, the loop judges."""

    loop = LOOP_DISTILLATION

    def __init__(self, engine: Any, epistemic: Any, substrate: Any) -> None:
        self._engine = engine
        self._epistemic = epistemic
        self._substrate = substrate

    def enter(self, boundary: BoundaryPresentation) -> LoopOutcome:
        from swarm_engine.acquisition.distill import DistillationLoop
        delta = boundary.evidence["delta"]
        result = DistillationLoop(
            self._engine, self._epistemic).distill(delta)
        return LoopOutcome(
            loop=self.loop, entered=True, result=result,
            detail=(f"distill({result.delta_id}): success={result.success} "
                    f"reason={result.reason[:160]}"),
            view=self._substrate.loop_view(self.loop))


class GeneralizationLoopInlet(LoopInlet):
    """Enter the Generalization loop: generalize_for_task -- the distilled
    technique re-parameterized against novel evidence, through the exact
    trust path (ReviewBoard + verdict-bound promotion)."""

    loop = LOOP_GENERALIZATION

    def __init__(self, engine: Any, epistemic: Any, substrate: Any) -> None:
        self._engine = engine
        self._epistemic = epistemic
        self._substrate = substrate

    def enter(self, boundary: BoundaryPresentation) -> LoopOutcome:
        from swarm_engine.acquisition.generalize_driver import (
            generalize_for_task)
        ev = boundary.evidence
        result = generalize_for_task(
            self._engine, self._epistemic, ev["distilled_ref"],
            ev["novel_goal"], ev["novel_examples"])
        return LoopOutcome(
            loop=self.loop, entered=True, result=result,
            detail=(f"generalize({ev['distilled_ref'].get('promoted_name')} "
                    f"-> {ev['novel_goal'][:60]}): "
                    f"success={result.success}"),
            view=self._substrate.loop_view(self.loop))


#: Inlet class per loop, in executive construction order.
INLETS = {
    LOOP_RUN: RunLoopInlet,
    LOOP_ACQUISITION: AcquisitionLoopInlet,
    LOOP_EXECUTION: ExecutionRepairLoopInlet,
    LOOP_ACCEPTANCE: AcceptanceLoopInlet,
    LOOP_DISTILLATION: DistillationLoopInlet,
    LOOP_GENERALIZATION: GeneralizationLoopInlet,
}
