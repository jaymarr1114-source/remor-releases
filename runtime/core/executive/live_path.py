"""PLOOP-11: the Primary loop's live-path integration driver.

The production seam that turns the nine crossed component mechanisms into
ONE integrated path: wake -> select loop -> converge -> accept -> distill ->
retain, with the handoff contracts, the checkpoint store, the terminal
router, and admitted-finding delivery all engaged on the real call path.

Ownership: this module is PLOOP-11's seam and the only file it owns. It
CONSUMES (never edits): RunController.tick (V10-P2), ExecutiveController,
the handoff contracts (PLOOP-2), TransitionCheckpointStore (PLOOP-8),
TerminalRouter (PLOOP-9), RelevanceGate (PLOOP-7), GapRegistry (M7),
AcceptanceLoop (Q8). Additive only: no existing behavior is changed.

Degradation contract (PLOOP-10 pattern): the checkpoint store and the
terminal router are bindable. Unbound, the driver degrades LOUDLY --
status() names the fallback, the drive report names every unrouted
outcome. Nothing is ever silently dropped.
"""

from __future__ import annotations

import os
import time
import traceback
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional, Tuple

from .boundary import (
    BOUNDARY_RUN_WAKE,
    BoundaryPresentation,
    BoundaryRefused,
)
from .checkpoint import (
    CheckpointError,
    TransitionCheckpointStore,
    verify_checkpoint_integrity,
)
from .handoff import (
    HANDOFF_CONTRACT_VERSION,
    MAX_HANDOFF_DEPTH,
    HandoffRefused,
    LoopHandoff,
    accept_handoff,
    classify_terminal,
    produce_handoff,
    rehydrate_handoff,
    route_owner,
)
from ..microcontroller import LOOPS
from .loops import (
    LOOP_ACCEPTANCE,
    LOOP_ACQUISITION,
    LOOP_DISTILLATION,
    LOOP_EXECUTION,
    LOOP_GENERALIZATION,
    LOOP_RUN,
    LoopOutcome,
)
from .relevance import (
    ADMITTED,
    Finding,
    FindingRefused,
    OperationalObjective,
    RelevanceGate,
)
from .terminal_routing import TerminalRouter

#: Version stamp for the live-path seam itself.
LIVE_PATH_VERSION = "live-path/v1"

#: Wake reasons the driver may present (subset of the boundary contract).
_WAKE_REASON = "cadence_tick"


# ---------------------------------------------------------------------------
# Construction
# ---------------------------------------------------------------------------

@dataclass
class LivePathConfig:
    """Db-backed workdir + run knobs. All state lives under workdir."""
    workdir: str
    objective_statement: str = (
        "Keep REMOR's Primary loop turning: converge real boundaries, "
        "admit only relevant findings, never lose work across a crash.")
    cadence_interval_s: float = 60.0
    cycle_budget_s: float = 120.0
    max_gaps_per_cycle: int = 5
    bind_store: bool = True
    bind_router: bool = True


class LivePath:
    """The integrated production driver. Build via build_live_path()."""

    def __init__(self, *, config: LivePathConfig, engine: Any,
                 run_controller: Any, registry: Any, acceptance_loop: Any,
                 executive: Any, relevance_gate: RelevanceGate,
                 checkpoint_store: Optional[TransitionCheckpointStore],
                 terminal_router: Optional[TerminalRouter]) -> None:
        self.config = config
        self.engine = engine
        self.rc = run_controller
        self.registry = registry
        self.acceptance_loop = acceptance_loop
        self.executive = executive
        self.gate = relevance_gate
        self.store = checkpoint_store
        self.router = terminal_router
        # Admitted findings awaiting delivery: finding_id ->
        # (Finding, target_loop, boundary_kind, evidence).
        self._delivery_outbox: Dict[str, Tuple[Finding, str, str, Dict[str, Any]]] = {}
        self._drives = 0

    # -- status (PLOOP-10 pattern: fallback named, never silent) ------------
    def status(self) -> Dict[str, Any]:
        arb = {}
        try:
            arb = self.rc.arbitration_status()
        except Exception as exc:  # status never raises
            arb = {"mode": "status_error", "error": str(exc)}
        return {
            "live_path": LIVE_PATH_VERSION,
            "drives_completed": self._drives,
            "checkpoint_store": ("bound" if self.store is not None
                                 else "fallback_unbound"),
            "terminal_router": ("bound" if self.router is not None
                                else "fallback_unbound"),
            "relevance_gate": "bound",
            "arbitration": arb,
        }

    # -- wake ----------------------------------------------------------------
    def wake(self) -> BoundaryPresentation:
        """The production wake boundary: cadence tick, this run."""
        return BoundaryPresentation(
            kind=BOUNDARY_RUN_WAKE,
            evidence={"wake_reason": _WAKE_REASON,
                      "run_id": self.rc._run_id},
            observed_by="live_path",
        ).validate()

    # -- one tick through the real path --------------------------------------
    def drive_tick(self) -> Tuple[BoundaryPresentation, LoopOutcome, Any]:
        """Enter the run loop through the executive: the real tick.

        Returns (wake_boundary, outcome, view_before). Persists the
        cycle through the controller's real record_cycle -- the terminal
        router's run handler verifies the persisted record, so the
        persist must precede any surfacing.
        """
        wake = self.wake()
        view_before = self.executive.loop_view(LOOP_RUN)
        outcome = self.executive.enter(wake)
        if outcome.entered and isinstance(outcome.result, dict):
            self.rc._checkpoint.record_cycle(
                int(outcome.result.get("cycle", 0)), outcome.result)
        return wake, outcome, view_before

    # -- surfacing: one contractual hop --------------------------------------
    def _surface_context(self, chain_depth: int) -> Dict[str, Any]:
        ctx: Dict[str, Any] = {
            "gap_fetcher": self.registry.get,
            "run_controller": self.rc,
            "chain_depth": chain_depth,
        }
        if self.store is not None:
            ctx["checkpoint_store"] = self.store
        return ctx

    def surface(self, outcome: LoopOutcome,
                triggering_boundary: BoundaryPresentation, *,
                chain_depth: int = 0, view_before: Any = None,
                novel_spec: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Surface one loop outcome through the contracts.

        produce -> accept -> enter, with the checkpoint consume tail
        replicated exactly (this split -- not transition() -- is what
        lets a process die between produce and accept). Declared
        terminals go to the terminal router; contract violations go to
        route_refusal. Unbound router/store degrade LOUDLY.
        """
        report: Dict[str, Any] = {
            "from_loop": outcome.loop,
            "terminal_state": None,
            "path": None,
        }
        ctx = self._surface_context(chain_depth)
        if novel_spec:
            ctx.update(novel_spec)
        try:
            terminal = classify_terminal(outcome.loop, outcome)
        except HandoffRefused as exc:
            return self._refusal_report(report, outcome.loop, exc)
        report["terminal_state"] = terminal
        if view_before is None:
            view_before = self.executive.loop_view(outcome.loop)
        view_after = outcome.view

        # produce ---------------------------------------------------------
        try:
            handoff = produce_handoff(
                outcome=outcome, triggering_boundary=triggering_boundary,
                context=ctx, view_before=view_before, view_after=view_after)
        except HandoffRefused as exc:
            return self._refusal_report(report, outcome.loop, exc)
        if handoff is None:
            return self._terminal_report(report, outcome, ctx)

        # accept + enter --------------------------------------------------
        report["handoff_id"] = handoff.handoff_id
        report["to_loop"] = handoff.to_loop
        report["boundary_kind"] = handoff.boundary_kind
        report["checkpoint_id"] = handoff.checkpoint_id
        try:
            boundary = accept_handoff(self.executive, handoff, ctx)
        except HandoffRefused as exc:
            return self._refusal_report(report, outcome.loop, exc)
        # Capture the receiving loop's pre-entry view for the next hop's
        # measured delta before entering.
        next_view_before = self.executive.loop_view(handoff.to_loop)
        try:
            received = self.executive.enter(boundary)
        except Exception as exc:  # a crashing inlet is a named defect,
            # never a silent one
            report["path"] = "inlet_error"
            report["error"] = (f"{type(exc).__name__}: {exc}\n"
                               f"{traceback.format_exc(limit=3)}")
            return report
        report["entered"] = received.entered
        report["received_detail"] = received.detail[:200]
        # The accepted boundary is the honest triggering boundary for
        # the NEXT hop: the receiving loop was entered with it, so the
        # follow-on it produces is caused by it. Lineage is preserved
        # per accepted boundary, never collapsed to the root wake.
        # accepted_boundary_id is the JSON-safe lineage link (the
        # object itself stays internal).
        report["accepted_boundary"] = boundary
        report["accepted_boundary_id"] = boundary.boundary_id
        # transition()'s consume tail, replicated exactly: single-use.
        if handoff.checkpoint_id is not None and received.entered:
            store = ctx.get("checkpoint_store")
            if store is None:  # cannot happen: produce engaged it
                raise HandoffRefused(
                    f"surface: handoff {handoff.handoff_id} carries a "
                    "checkpoint but the store vanished from context")
            try:
                store.mark_consumed(handoff.checkpoint_id)
                report["checkpoint_consumed"] = True
            except CheckpointError as exc:
                return self._refusal_report(report, outcome.loop,
                                            HandoffRefused(
                                                f"surface: checkpoint "
                                                f"consume failed: {exc}"))
        report["path"] = "transitioned"
        report["next_view_before"] = next_view_before
        report["received_outcome"] = received
        return report

    def _terminal_report(self, report: Dict[str, Any], outcome: LoopOutcome,
                         ctx: Dict[str, Any]) -> Dict[str, Any]:
        """Declared terminal: route to the real consumer, or LOUD fallback."""
        report["path"] = "terminal"
        if self.router is None:
            report["terminal_routing"] = {
                "mode": "fallback_unrouted",
                "reason": ("no TerminalRouter bound: declared-terminal "
                           "outcome recorded here, never silently dropped"),
                "loop": outcome.loop,
                "terminal_state": report["terminal_state"],
                "detail": str(outcome.detail)[:200],
            }
            return report
        try:
            route = self.router.route_terminal(outcome.loop, outcome, ctx)
        except HandoffRefused as exc:
            return self._refusal_report(report, outcome.loop, exc)
        report["terminal_routing"] = {
            "mode": "routed",
            "route_id": route.route_id,
            "consumer": route.consumer,
            "reason": route.reason,
            "evidence_refs": dict(route.evidence_refs),
        }
        return report

    def _refusal_report(self, report: Dict[str, Any], loop: str,
                        exc: HandoffRefused) -> Dict[str, Any]:
        """Contract violation: finding to the executive, or LOUD fallback."""
        report["path"] = "refused"
        report["violation"] = str(exc)
        if self.router is None:
            report["refusal_routing"] = {
                "mode": "fallback_unrouted",
                "reason": ("no TerminalRouter bound: contract violation "
                           "recorded here, never silently dropped"),
            }
            return report
        try:
            route = self.router.route_refusal(exc, loop=loop, context={})
        except HandoffRefused as exc2:
            report["refusal_routing"] = {
                "mode": "refusal_failed",
                "reason": f"route_refusal itself refused: {exc2}",
            }
            return report
        decision = self.gate.get_decision(
            route.evidence_refs.get("finding_id", ""))
        report["refusal_routing"] = {
            "mode": "finding_submitted",
            "route_id": route.route_id,
            "consumer": route.consumer,
            "finding_id": route.evidence_refs.get("finding_id"),
            "gate_verdict": (decision.verdict if decision is not None
                             else "unknown"),
        }
        return report

    # -- full cycle: wake -> ... -> terminal, then admitted delivery ---------
    def drive_cycle(self, *,
                    novel_spec: Optional[Dict[str, Any]] = None
                    ) -> Dict[str, Any]:
        """One integrated production cycle.

        wake -> select loop -> converge -> accept -> distill -> retain,
        following every contractual hop to its terminal, then delivering
        admitted findings into loop inlets.
        """
        started = time.time()
        wake, outcome, view_before = self.drive_tick()
        hops: List[Dict[str, Any]] = []
        triggering = wake
        depth = 0
        while True:
            hop = self.surface(outcome, triggering, chain_depth=depth,
                               view_before=view_before,
                               novel_spec=novel_spec)
            # novel_spec is single-use: only the distillation hop may
            # consume it (novelty is never auto-generated downstream).
            novel_spec = None
            hops.append({k: v for k, v in hop.items()
                         if k not in ("received_outcome",
                                      "accepted_boundary")})
            if hop["path"] != "transitioned":
                break
            received = hop["received_outcome"]
            depth = depth + 1
            if depth >= MAX_HANDOFF_DEPTH:
                hops.append({"path": "depth_cap",
                             "reason": "chain depth cap reached; stopping"})
                break
            outcome, triggering = received, hop.get("accepted_boundary")
            if triggering is None:  # pragma: no cover - defensive
                triggering = wake
            # Lineage: each hop's handoff carries the accepted boundary
            # the producing loop was entered with, chaining back to the
            # root wake (the handoff's triggering_boundary_id chain).
            view_before = hop["next_view_before"]
        deliveries = self.deliver_admitted()
        self._drives += 1
        return {
            "live_path": LIVE_PATH_VERSION,
            "drive_n": self._drives,
            "elapsed_s": round(time.time() - started, 2),
            "tick_entered": True,
            "hops": hops,
            "deliveries": deliveries,
            "status": self.status(),
        }

    # -- kill-resume: the production split (proc A / proc B) -----------------
    def produce_for_resume(
            self) -> Tuple[Dict[str, Any], LoopOutcome, LoopHandoff]:
        """Proc-A path: tick + produce, NEVER accept.

        Returns the durable handoff identity (persist it wherever the
        deployment passes control across the process boundary), the
        tick outcome, and the handoff. The caller exits without
        accepting: the checkpoint is what survives.
        """
        if self.store is None:
            raise HandoffRefused(
                "produce_for_resume needs a bound checkpoint store: "
                "resume without durability is not resume")
        wake, outcome, view_before = self.drive_tick()
        ctx = self._surface_context(chain_depth=0)
        handoff = produce_handoff(
            outcome=outcome, triggering_boundary=wake, context=ctx,
            view_before=view_before, view_after=outcome.view)
        if handoff is None:
            raise HandoffRefused(
                "produce_for_resume: the tick was declared terminal: "
                "nothing to resume across processes")
        if not handoff.checkpoint_id:
            raise HandoffRefused(
                "produce_for_resume: produce did not checkpoint: the "
                "store is bound but the handoff carries no checkpoint")
        identity = {
            "handoff_id": handoff.handoff_id,
            "checkpoint_id": handoff.checkpoint_id,
            "from_loop": handoff.from_loop,
            "to_loop": handoff.to_loop,
            "terminal_state": handoff.terminal_state,
            "boundary_kind": handoff.boundary_kind,
            "triggering_boundary_id": handoff.triggering_boundary_id,
            "chain_depth": handoff.chain_depth,
            "evidence_refs": dict(handoff.evidence_refs),
            "resource_delta": dict(handoff.resource_delta),
            "produced_by": handoff.produced_by,
        }
        return identity, outcome, handoff

    def resume(self, handoff_id: str) -> Dict[str, Any]:
        """Proc-B path: fresh process resumes a produced handoff.

        Loads the checkpoint on a fresh connection (durability),
        rehydrates the handoff from the row plus honestly re-fetched
        records, accepts (verification against the live world), enters
        the receiving loop, consumes the checkpoint. Single-use:
        a second resume of the same handoff is refused.
        """
        verdict: Dict[str, Any] = {"handoff_id": handoff_id}
        if self.store is None:
            raise HandoffRefused(
                "resume needs a bound checkpoint store")
        row = self.store.load(handoff_id)
        if row is None:
            raise HandoffRefused(
                f"resume: no checkpoint row for handoff {handoff_id}: "
                "not durable, not resumable")
        verify_checkpoint_integrity(row)  # tamper -> CheckpointError, loud
        if row.get("status") != "saved":
            raise HandoffRefused(
                f"resume: checkpoint {row.get('checkpoint_id')} has status "
                f"{row.get('status')!r}: replay of a consumed checkpoint "
                "is refused")
        evidence = self._refetch_evidence(row)
        handoff = rehydrate_handoff(row, evidence)
        ctx = self._surface_context(chain_depth=int(row.get("chain_depth", 1)))
        boundary = accept_handoff(self.executive, handoff, ctx)
        verdict["verified_checkpoint"] = handoff.verified_checkpoint is not None
        verdict["boundary_kind"] = boundary.kind
        verdict["to_loop"] = handoff.to_loop
        received = self.executive.enter(boundary)
        verdict["entered"] = received.entered
        verdict["received_detail"] = received.detail[:200]
        if handoff.checkpoint_id is not None and received.entered:
            self.store.mark_consumed(handoff.checkpoint_id)
            verdict["checkpoint_consumed"] = True
        return verdict

    def _refetch_evidence(self, row: Mapping[str, Any]) -> Dict[str, Any]:
        """Honestly re-fetch the records the checkpoint row names.

        The row's evidence_refs are an id map, never the records: the
        REAL records come from the live stores, or resume refuses.
        """
        refs = dict(row.get("evidence_refs") or {})
        kind = row.get("boundary_kind")
        if kind == "acquisition_gap":
            gap_id = refs.get("gap_record")
            rec = self.registry.get(gap_id) if gap_id else None
            if rec is None:
                raise HandoffRefused(
                    f"resume: checkpoint's gap {gap_id!r} is not in the "
                    "registry: the boundary is gone")
            if getattr(rec, "status", None) not in ("open", "acquiring"):
                raise HandoffRefused(
                    f"resume: gap {gap_id!r} has status "
                    f"{getattr(rec, 'status', None)!r}: not a live open "
                    "boundary")
            return {"gap_record": rec}
        if kind == "execution_failure":
            cid = refs.get("capability_id")
            if not cid:
                raise HandoffRefused(
                    "resume: checkpoint names no capability_id")
            # The evidence IS the id: the inlet re-reads the live
            # quarantine record through M5's frozen API.
            return {"capability_id": str(cid)}
        raise HandoffRefused(
            f"resume: boundary kind {kind!r} has no honest re-fetch rule: "
            "resume refuses rather than fabricates evidence")

    # -- PLOOP-7 residual: admitted-finding delivery --------------------------
    def submit_finding(self, finding: Finding, *, target_loop: str,
                       boundary_kind: str,
                       evidence: Dict[str, Any]):
        """Submit a finding with its producer-declared delivery destination.

        The producer names the loop and boundary kind the finding is
        about; the relevance gate judges admission; delivery validates
        the declaration against the contract before any loop is entered.
        The gate's record -- never the outbox -- is the authority on
        admission.
        """
        finding.validate()
        if target_loop not in LOOPS:
            raise FindingRefused(
                f"delivery declaration names unknown loop "
                f"{target_loop!r}")
        reg = self.executive.registrations().get(target_loop)
        if reg is None or reg.state != "real":
            raise FindingRefused(
                f"delivery declaration targets loop {target_loop!r} "
                "which is not registered REAL: findings are not "
                "delivered into absences")
        try:
            owner = route_owner(boundary_kind)
        except HandoffRefused as exc:
            raise FindingRefused(
                f"delivery declaration: {exc}")
        if owner != target_loop:
            raise FindingRefused(
                f"delivery declaration: boundary kind {boundary_kind!r} "
                f"is owned by {owner!r}, not {target_loop!r}: the "
                "declaration contradicts the ownership contract")
        if not isinstance(evidence, dict) or not evidence:
            raise FindingRefused(
                "delivery declaration needs a non-empty evidence dict")
        decision = self.executive.submit_finding(finding)
        if decision.verdict == ADMITTED:
            self._delivery_outbox[finding.finding_id] = (
                finding, target_loop, boundary_kind, dict(evidence))
        return decision

    def deliver_admitted(self) -> List[Dict[str, Any]]:
        """Deliver outbox findings whose admission the gate still confirms.

        Each delivery builds the declared boundary and passes the
        boundary's own anti-fabrication validate() before the inlet is
        entered. Anything off is refused loudly and routed as a finding
        when the router is bound.
        """
        delivered: List[Dict[str, Any]] = []
        for finding_id, (finding, target_loop, kind, evidence) in list(
                self._delivery_outbox.items()):
            rec: Dict[str, Any] = {
                "finding_id": finding_id,
                "target_loop": target_loop,
                "boundary_kind": kind,
            }
            decision = self.gate.get_decision(finding_id)
            if decision is None or decision.verdict != ADMITTED:
                rec["delivered"] = False
                rec["reason"] = ("gate no longer confirms ADMITTED "
                                 f"({decision.verdict if decision else 'no decision'})")
                delivered.append(rec)
                self._delivery_outbox.pop(finding_id, None)
                continue
            try:
                boundary = BoundaryPresentation(
                    kind=kind, evidence=dict(evidence),
                    observed_by=f"finding:{finding_id}").validate()
            except BoundaryRefused as exc:
                rec["delivered"] = False
                rec["reason"] = f"boundary refused: {exc}"
                if self.router is not None:
                    try:
                        route = self.router.route_refusal(
                            HandoffRefused(
                                f"finding delivery refused: {exc}"),
                            loop=target_loop, context={})
                        rec["refusal_route_id"] = route.route_id
                    except HandoffRefused:
                        pass
                delivered.append(rec)
                self._delivery_outbox.pop(finding_id, None)
                continue
            try:
                outcome = self.executive.enter(boundary)
            except Exception as exc:
                rec["delivered"] = False
                rec["reason"] = f"inlet error: {type(exc).__name__}: {exc}"
                delivered.append(rec)
                self._delivery_outbox.pop(finding_id, None)
                continue
            rec["delivered"] = bool(outcome.entered)
            rec["detail"] = outcome.detail[:200]
            delivered.append(rec)
            self._delivery_outbox.pop(finding_id, None)
        return delivered


# ---------------------------------------------------------------------------
# Construction site (SEAM_EXECUTIVE.md, now wired)
# ---------------------------------------------------------------------------

def build_live_path(config: LivePathConfig,
                    engine: Any = None) -> LivePath:
    """Construct the full integrated stack. The engine is injected when
    the caller already holds the single live one; otherwise built here
    on the workdir's db files."""
    from swarm_engine.core.engine import SwarmEngine
    from swarm_engine.core.run_controller import (
        RunController, RunConfig)
    from swarm_engine.acquisition.gaps import GapRegistry
    from swarm_engine.services.acceptance import (
        AcceptanceLoop, AcceptanceStore)
    from swarm_engine.core.executive import ExecutiveController

    os.makedirs(config.workdir, exist_ok=True)
    eng = engine or SwarmEngine(
        db_path=os.path.join(config.workdir, "engine.db"))
    registry = GapRegistry(
        eng, db_path=os.path.join(config.workdir, "gaps.db"))
    rc = RunController(
        eng,
        config=RunConfig(
            cadence_interval_s=config.cadence_interval_s,
            cycle_budget_s=config.cycle_budget_s,
            max_gaps_per_cycle=config.max_gaps_per_cycle),
        checkpoint_path=os.path.join(config.workdir, "rc.db"),
        registry=registry)
    acceptance_loop = AcceptanceLoop(
        AcceptanceStore(db_path=os.path.join(config.workdir, "acc.db")),
        eng.intellect.epistemic, engine=eng)
    gate = RelevanceGate(
        objective=OperationalObjective(
            objective_id="primary-loop",
            statement=config.objective_statement),
        store_path=os.path.join(config.workdir, "relevance.db"))
    executive = ExecutiveController(
        engine=eng, run_controller=rc, gap_registry=registry,
        acceptance_loop=acceptance_loop, relevance_gate=gate)
    store = (TransitionCheckpointStore(
        os.path.join(config.workdir, "transition_checkpoints.db"))
        if config.bind_store else None)
    router = None
    if config.bind_router:
        from swarm_engine.synthesis.integrity import diagnose_quarantine
        router = TerminalRouter(
            executive=executive, run_controller=rc,
            acceptance_loop=acceptance_loop,
            gap_fetcher=registry.get,
            diagnosis_fetcher=(lambda cid: diagnose_quarantine(eng, cid)),
            ledger_path=os.path.join(config.workdir, "terminal_ledger.db"))
    return LivePath(
        config=config, engine=eng, run_controller=rc, registry=registry,
        acceptance_loop=acceptance_loop, executive=executive,
        relevance_gate=gate, checkpoint_store=store, terminal_router=router)
