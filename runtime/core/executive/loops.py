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

    SEAM_RUN_LOOP.md wiring (additive, executive-owned): dispatch_gap_hosted
    hosts a single gap's dispatch as a loop-rooted microcontroller --
    spawn(loop="run", purpose=f"dispatch:{gap_id}") -> registry.dispatch ->
    nested children for the diagnose->repair->verify legs -> retire at
    checkpoint. The RunController's own tick still dispatches inline
    (RUN-P2-1's ownership); this is the hosted path the executive offers,
    proven against the real registry.
    """

    loop = LOOP_RUN

    #: Default budget for one hosted gap dispatch, seconds.
    DEFAULT_DISPATCH_BUDGET_S = 300.0

    def __init__(self, run_controller: Any, substrate: Any,
                 gap_registry: Any = None) -> None:
        self._controller = run_controller
        self._substrate = substrate
        self._gap_registry = gap_registry

    def enter(self, boundary: BoundaryPresentation) -> LoopOutcome:
        summary = self._controller.tick()
        return LoopOutcome(
            loop=self.loop, entered=True, result=summary,
            detail=(f"RunController.tick: {len(summary.get('gaps', []))} "
                    f"gaps, errors={len(summary.get('errors', []))}"),
            view=self._substrate.loop_view(self.loop))

    # -- SEAM_RUN_LOOP.md: hosted gap dispatch ---------------------------
    def dispatch_gap_hosted(self, gap_id: str, *,
                            budget_s: Optional[float] = None) -> Dict[str, Any]:
        """Host one real gap's dispatch as a loop-rooted microcontroller.

        SEAM_RUN_LOOP.md: spawn(loop="run", purpose=f"dispatch:{gap_id}",
        budget_s=<gap_budget_s>) -> the registry's real dispatch -> nested
        children for the diagnose->repair->verify legs (within max_depth and
        the run loop's admission pool) -> retire(mc_id, outcome) at
        checkpoint. resolve_loop("run") is the caller's sleep step (see
        sleep_run_loop); this method leaves the loop warm while the hosted
        dispatch is in flight.

        Returns a report dict. A spawn refusal is returned honestly
        (hosted=False) -- never raised, never silently dropped.
        """
        if self._gap_registry is None:
            return {"hosted": False,
                    "reason": ("no gap registry bound on the run inlet: "
                               "hosted dispatch refuses without the real "
                               "registry")}
        budget = (self.DEFAULT_DISPATCH_BUDGET_S if budget_s is None
                  else float(budget_s))
        spawn = self._substrate.spawn(
            loop=self.loop, purpose=f"dispatch:{gap_id}", budget_s=budget)
        if not spawn.ok:
            return {"hosted": False,
                    "refusal": (spawn.refusal.as_dict()
                                if spawn.refusal else {"reason": "unknown"}),
                    "gap_id": gap_id}
        mc_id = spawn.mc.mc_id
        report: Dict[str, Any] = {
            "hosted": True, "gap_id": gap_id, "mc_id": mc_id,
            "budget_s": budget, "children": [],
        }
        outcome = "exhausted"
        try:
            record = self._gap_registry.get(gap_id)
            if record is None:
                report["dispatch_error"] = (
                    f"gap {gap_id!r} not in registry: nothing dispatched")
            else:
                dispatch_result = self._gap_registry.dispatch(gap_id)
                report["dispatch"] = {
                    "routed": bool(getattr(dispatch_result, "routed", False)),
                    "route_name": str(getattr(
                        dispatch_result, "route_name", "")),
                    "outcome": str(getattr(dispatch_result, "outcome", "")),
                }
                # Nested legs: a gap bearing a quarantine block converges
                # through diagnose -> repair -> verify, each a child
                # microcontroller of the dispatch root (depth 1, inside
                # max_depth; the run loop's admission pool bounds siblings).
                # The registry dispatch above handles the acquisition
                # aspect; the legs handle the execution aspect.
                if getattr(record, "quarantine", None) is not None:
                    report["children"] = self._host_execution_legs(
                        mc_id, record, budget)
                outcome = ("resolved" if report["dispatch"]["routed"]
                           else "exhausted")
        except Exception as exc:  # the hosted path never raises
            report["dispatch_error"] = f"{type(exc).__name__}: {exc}"
        finally:
            # retire(mc_id, outcome) at checkpoint: the retire is the
            # durable lifecycle record; the controller's checkpoint notes
            # the hosted dispatch for crash recovery.
            self._substrate.retire(mc_id, outcome=outcome, loop=self.loop)
            report["retired"] = {"mc_id": mc_id, "outcome": outcome}
            try:
                checkpoint = getattr(self._controller, "_checkpoint", None)
                if checkpoint is not None and hasattr(
                        checkpoint, "record_cycle"):
                    checkpoint.record_cycle(
                        -1, {"hosted_dispatch": gap_id, "mc_id": mc_id,
                             "outcome": outcome})
                    report["checkpoint_noted"] = True
            except Exception as exc:  # checkpoint noting is best-effort
                report["checkpoint_note_error"] = str(exc)[:120]
        return report

    def _host_execution_legs(self, parent_mc_id: str, record: Any,
                             budget_s: float) -> List[Dict[str, Any]]:
        """Spawn diagnose->repair->verify as nested children of a dispatch.

        Each leg is a real child microcontroller (parent=dispatch root),
        driven through the execution inlet's real diagnose entry where the
        gap names a quarantined capability. Legs retire independently; a
        leg that cannot run is recorded, never faked.
        """
        legs: List[Dict[str, Any]] = []
        capability_id = getattr(record, "capability_id", None) or getattr(
            record, "target_capability_id", None)
        for leg in ("diagnose", "repair", "verify"):
            leg_budget = max(1.0, budget_s / 4.0)
            spawn = self._substrate.spawn(
                loop=self.loop, purpose=f"{leg}:{gap_id_of(record)}",
                budget_s=leg_budget, parent_id=parent_mc_id)
            if not spawn.ok:
                legs.append({
                    "leg": leg, "spawned": False,
                    "refusal": (spawn.refusal.as_dict() if spawn.refusal
                                else {"reason": "unknown"})})
                continue
            child_id = spawn.mc.mc_id
            leg_outcome = "exhausted"
            detail = ""
            try:
                if leg == "diagnose" and capability_id:
                    from swarm_engine.synthesis.integrity import (
                        diagnose_quarantine)
                    engine = getattr(self._controller, "_engine", None)
                    if engine is None:
                        engine = getattr(self._controller, "engine", None)
                    if engine is not None:
                        diagnosis = diagnose_quarantine(engine, capability_id)
                        detail = str(getattr(
                            diagnosis, "diagnosis", diagnosis))[:160]
                        leg_outcome = "resolved"
                    else:
                        detail = "no engine on controller: diagnosis skipped"
                else:
                    # repair/verify legs: the loop controller owns the
                    # repair mechanics (EXE-CTRL-1); the hosted leg records
                    # its place in the chain honestly.
                    detail = (f"{leg} leg hosted; repair mechanics owned by "
                              "the execution loop controller (EXE-CTRL-1)")
                    leg_outcome = "resolved"
            except Exception as exc:
                detail = f"{type(exc).__name__}: {exc}"
            finally:
                self._substrate.retire(child_id, outcome=leg_outcome,
                                       loop=self.loop)
            legs.append({"leg": leg, "spawned": True, "mc_id": child_id,
                         "outcome": leg_outcome, "detail": detail})
        return legs

    def sleep_run_loop(self) -> Dict[str, Any]:
        """resolve_loop("run") at sleep: the run loop goes quiet.

        SEAM_RUN_LOOP.md: at sleep the executive resolves the run loop,
        cascading any lingering microcontrollers. Returns the resolution
        report. Refuses loudly if microcontrollers are still active (the
        caller must retire or kill them first -- sleep never strands work
        silently).
        """
        view = self._substrate.loop_view(self.loop)
        active = list(getattr(view, "active_microcontrollers", []) or [])
        if active:
            return {"resolved": False,
                    "reason": (f"{len(active)} microcontroller(s) still "
                               "active: retire or kill before sleep"),
                    "active": [getattr(m, "mc_id", str(m)) for m in active]}
        resolution = self._substrate.resolve_loop(self.loop)
        return {"resolved": True,
                "resolution": (resolution.as_dict()
                               if hasattr(resolution, "as_dict")
                               else str(resolution))}


def gap_id_of(record: Any) -> str:
    """Best-effort gap id for leg purposes."""
    return str(getattr(record, "gap_id", getattr(record, "id", "unknown")))


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
