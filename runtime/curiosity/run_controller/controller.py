"""The Curiosity Run Controller: level 2 execution control (C-1).

Owns cadence (tick), dispatch, priority funding, checkpoints/recovery,
pause/resume/stop, budget and concurrency admission, and termination.
It holds the curiosity substrate instance (separate from any Primary-side
instance, C-1.4) and the per-inquiry GraphControllers; the Executive
holds neither (C-6.3).

Funding rule (Phase 2, documented): the FRM epoch grant's budget is
divided equally among the grant's max_concurrent slots; each inquiry's
slice is enforced exactly (spent_s > slice -> RESOURCE_BOUNDARY).
Concurrency admission: active inquiries < grant.max_concurrent; the rest
wait PENDING and are admitted in priority order (PRIMARY_REQUESTED=0
first). The substrate's loop admission pool is the backstop for
concurrent microcontroller reservations; exact consumption is accounted
per-inquiry here against the FRM grant slice.

Pause is in-memory (not durable): PAUSED inquiries keep their state and
resume on resume_inquiry. Kill and resource suspension are durable:
checkpointed through the TransitionCheckpointStore inquiry adapter and
resumable in a fresh process with the same inquiry lineage.

Terminal routing: every stopped inquiry is recorded in the reusable
TerminalLedger with its evidence refs; curiosity uses a route adapter
(loop="questioning", consumer="curiosity_evidence_store"), NOT a
Primary-specific TerminalRouter (which is hard-coded to Primary
loop/terminal pairs).
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from swarm_engine.core.executive.checkpoint import (
    TransitionCheckpointStore, verify_checkpoint_integrity)
from swarm_engine.core.executive.terminal_routing import (
    TerminalLedger, TerminalRoute)
from swarm_engine.core.graph_controller.controller import GraphController
from swarm_engine.curiosity.attribution.chain import (
    AdmissionRecord, ChainLedger, Result, WorkUnit)
from swarm_engine.curiosity.evidence.records import (
    CuriosityFinding, EvidenceProvenance)
from swarm_engine.curiosity.evidence.store import CuriosityEvidenceStore
from swarm_engine.curiosity.evidence.writer import CuriosityWriter
from swarm_engine.curiosity.loops.questioning.loop import (
    TERMINAL_BOUNDARY, TERMINAL_INSUFFICIENT, TERMINAL_RESOLVED,
    LoopContext, QuestioningLoop, QuestioningLoopInlet, SubstrateRefused)
from swarm_engine.curiosity.loops.scientific_inquiry.loop import (
    MODEL_ID as INQUIRY_MODEL_ID,
    TERMINAL_INCONCLUSIVE as INQUIRY_TERMINAL_INCONCLUSIVE,
    TERMINAL_INSUFFICIENT as INQUIRY_TERMINAL_INSUFFICIENT,
    TERMINAL_METHOD_BOUNDARY as INQUIRY_TERMINAL_METHOD_BOUNDARY,
    TERMINAL_REFUTED as INQUIRY_TERMINAL_REFUTED,
    TERMINAL_SUPPORTED as INQUIRY_TERMINAL_SUPPORTED,
    LoopContext as InquiryLoopContext,
    ScientificInquiryLoop, ScientificInquiryLoopInlet,
    SubstrateRefused as InquirySubstrateRefused)
from swarm_engine.curiosity.substrate import (
    LOOP_QUESTIONING, LOOP_SCIENTIFIC_INQUIRY)

# Inquiry states.
ST_PENDING = "PENDING"
ST_ACTIVE = "ACTIVE"
ST_PAUSED = "PAUSED"
ST_KILLED = "KILLED"
ST_SUSPENDED = "SUSPENDED"
ST_TERMINATED = "TERMINATED"

# Curiosity-local checkpoint labels (the store accepts any non-empty
# terminal_state; these name inquiry suspensions, not Primary terminals).
CKPT_KILL = "SUSPENDED_KILL"
CKPT_RESOURCE = "SUSPENDED_RESOURCE"

#: Provenance model stamps, per loop (mechanical; no external provider).
#: The questioning string is unchanged from Phase 2; the inquiry string
#: is the loop's own MODEL_ID.
LOOP_MODELS = {
    LOOP_QUESTIONING: ("curiosity-questioning/v1 (mechanical refinement; "
                       "no external cognition provider)"),
    LOOP_SCIENTIFIC_INQUIRY: INQUIRY_MODEL_ID,
}

#: Terminal states that count as a decisive convergence for attribution.
#: Mirrors the questioning division (resolved -> success, insufficient ->
#: partial): the inquiry loop's decisive verdicts are SUPPORTED/REFUTED;
#: INSUFFICIENT/INCONCLUSIVE converge but stay partial. BOUNDARY_ESTABLISHED
#: is the same string for both loops (TERMINAL_BOUNDARY).
SUCCESS_TERMINALS = frozenset({
    TERMINAL_RESOLVED, TERMINAL_BOUNDARY,
    INQUIRY_TERMINAL_SUPPORTED, INQUIRY_TERMINAL_REFUTED})

D4_NOTE = ("relevance threshold 0.25 is provisional per D-4, never a "
           "universal constant; this score is ADVISORY ONLY. Primary "
           "Acceptance is final (C-3.4); curiosity never accepts its own "
           "findings and true-but-imprecise results stay fenced.")


class AdmissionRefused(Exception):
    """Dispatch-time refusal (concurrency admission cannot be satisfied)."""


@dataclass
class InquiryHandoff:
    """Curiosity-local handoff record: inquiry suspend state mapped onto
    the shape TransitionCheckpointStore.save() reads (C-7.3 / P-11
    adapter). The store is duck-typed; this is the documented mapping,
    not a second handoff class competing with the Primary's."""
    handoff_id: str
    evidence_refs: Dict[str, str]
    resource_delta: Dict[str, float]
    from_loop: str
    to_loop: str
    terminal_state: str
    boundary_kind: str
    triggering_boundary_id: str
    chain_depth: int = 0


@dataclass
class InquiryRecord:
    """One curiosity inquiry under run-control."""
    inquiry_id: str
    trigger: Any                      # CuriosityTrigger
    loop: str
    priority: int
    grant: Any                        # FrmGrant (frozen)
    epoch_id: int
    budget_slice_s: float
    seq: int = 0                      # dispatch order; FIFO tiebreak
    state: str = ST_PENDING
    spent_s: float = 0.0
    loop_state: Dict[str, Any] = field(default_factory=dict)
    lineage: List[Dict[str, Any]] = field(default_factory=list)
    kill_requested: bool = False
    kill_reason: str = ""
    checkpoint_seq: int = 0
    evidence_id: Optional[str] = None
    terminal_state: Optional[str] = None
    result: Optional[Dict[str, Any]] = None
    # Runtime-only (never checkpointed): per-inquiry graph + loop ctx.
    graph: Any = None
    ctx: Any = None

    def note(self, event: str, detail: str = "") -> None:
        self.lineage.append({"t": time.time(), "event": event,
                             "detail": detail})

    def view(self) -> Dict[str, Any]:
        """Loop-level aggregate for the executive. No microcontroller
        ids, purposes, or graph detail -- by construction (this dict is
        all the executive ever sees of an inquiry)."""
        return {
            "inquiry_id": self.inquiry_id,
            "trigger_id": self.trigger.trigger_id,
            "loop": self.loop,
            "state": self.state,
            "priority": self.priority,
            "origin": self.trigger.origin,
            "boundary_class": self.trigger.boundary_class,
            "spent_s": round(self.spent_s, 6),
            "budget_slice_s": self.budget_slice_s,
            "epoch_id": self.epoch_id,
            "terminal_state": self.terminal_state,
            "evidence_id": self.evidence_id,
            "lineage_events": len(self.lineage),
        }


class CuriosityRunController:
    """Level 2: cadence, dispatch, funding, checkpoints, kill, budget."""

    def __init__(self, *, substrate: Any, checkpoint_db: str,
                 evidence_db: str, ledger_db: str, attribution_db: str,
                 payload_dir: str, corpus_docs: List[str],
                 clock: Callable[[], float] = time.monotonic) -> None:
        self._substrate = substrate  # owned here; never exposed upward
        self._checkpoints = TransitionCheckpointStore(checkpoint_db)
        self._evidence = CuriosityEvidenceStore(evidence_db)
        self._writer = CuriosityWriter(self._evidence)
        self._ledger = TerminalLedger(ledger_db)
        self._attribution = ChainLedger(attribution_db)
        self._payload_dir = Path(payload_dir)
        self._payload_dir.mkdir(parents=True, exist_ok=True)
        self._corpus_docs = list(corpus_docs)
        self._clock = clock
        # Loop registry dispatched by inq.loop (CUR-P3A-INT). Questioning
        # keeps its exact construction; scientific_inquiry is admitted by
        # James's U-1-class decision 2026-10-01. Nothing else may be
        # registered here without a James decision -- the vocabulary
        # stays fenced (substrate.CURIOSITY_LOOPS is the authority).
        self._loops = {
            LOOP_QUESTIONING: QuestioningLoop(),
            LOOP_SCIENTIFIC_INQUIRY: ScientificInquiryLoop(),
        }
        self._inlets = {
            LOOP_QUESTIONING:
                QuestioningLoopInlet(self._loops[LOOP_QUESTIONING]),
            LOOP_SCIENTIFIC_INQUIRY:
                ScientificInquiryLoopInlet(
                    self._loops[LOOP_SCIENTIFIC_INQUIRY]),
        }
        self._inquiries: Dict[str, InquiryRecord] = {}
        self._dispatch_seq = 0  # monotonic dispatch order for FIFO tiebreak
        # Durable inquiry -> latest-checkpoint index (the checkpoint store
        # has no query API; this is the run controller's own lookup, not
        # a second checkpoint mechanism).
        self._index_path = Path(str(checkpoint_db) + ".index.json")
        # Corpus index is built lazily by the loop's first probe.

    # -- dispatch + admission -------------------------------------------

    def dispatch(self, decision: Any) -> str:
        """Admit an approved activation decision as an inquiry. Priority
        funding: PENDING inquiries are admitted in (priority, dispatch
        order) when a concurrency slot frees."""
        if not getattr(decision, "approved", False):
            raise AdmissionRefused("cannot dispatch a refused decision")
        grant = decision.grant
        if grant.max_concurrent < 1:
            # Backstop: a grant with no concurrency slot can never be
            # admitted (admission would deadlock). The executive refuses
            # these first (NO_SLOT); this guards direct dispatch callers.
            raise AdmissionRefused(
                f"grant {grant.grant_id} funds "
                f"{grant.max_concurrent} concurrency slots: no inquiry "
                "can run under it")
        inquiry_id = "inq_" + uuid.uuid4().hex[:12]
        slice_s = grant.budget_s / max(1, grant.max_concurrent)
        # Fail-closed priority: a decision that carries no priority is
        # treated as curiosity-initiated (1), never as Primary's (0).
        prio = (decision.priority
                if getattr(decision, "priority", None) is not None else 1)
        inq = InquiryRecord(
            inquiry_id=inquiry_id, trigger=decision.trigger,
            loop=decision.loop or LOOP_QUESTIONING,
            priority=prio,
            grant=grant,
            epoch_id=decision.epoch_id or 0, budget_slice_s=slice_s,
            seq=self._dispatch_seq)
        self._dispatch_seq += 1
        inq.note("dispatched",
                 f"priority={inq.priority} slice={slice_s:.4f}s "
                 f"epoch={inq.epoch_id}")
        if inq.loop not in self._loops:
            raise AdmissionRefused(
                f"no registered loop {inq.loop!r}: the curiosity loop "
                "registry is fenced (vocabulary admission is James's "
                "U-1-class decision)")
        self._inquiries[inquiry_id] = inq
        self._admit_pending()
        return inquiry_id

    def _admit_pending(self) -> None:
        pending = [i for i in self._inquiries.values()
                   if i.state == ST_PENDING]
        pending.sort(key=lambda i: (i.priority, i.seq))
        for inq in pending:
            slots = inq.grant.max_concurrent
            # Concurrency is per-grant: slots funded by the same FRM grant.
            active = sum(1 for i in self._inquiries.values()
                         if i.state == ST_ACTIVE and i.grant is inq.grant)
            if active >= slots:
                break
            self._activate(inq)

    def _activate(self, inq: InquiryRecord) -> None:
        # The loop admission pool is the substrate backstop; register
        # once per process (idempotent). Pool sizing invariant: the pool
        # is grant-sized (max_concurrent * slice) and one inquiry tree's
        # peak live reservation is 13/16 of its slice (see the root spawn
        # in QuestioningLoop.step), so max_concurrent admitted inquiries
        # can never over-reserve the pool -- reservation deadlock is
        # structurally impossible, and exact spend is enforced per slice.
        try:
            self._substrate.loop_view(inq.loop)
        except ValueError:
            self._substrate.register_loop(
                inq.loop, budget_s=inq.grant.budget_s, max_concurrent=64)
        inq.ctx = self._build_ctx(inq)
        inq.graph = GraphController()
        inq.loop_state = self._inlets[inq.loop].enter(inq.trigger, inq.ctx)
        inq.state = ST_ACTIVE
        inq.note("admitted", f"loop={inq.loop}")

    def _build_ctx(self, inq: InquiryRecord) -> Any:
        """Per-loop LoopContext construction (CUR-P3A-INT). Questioning
        probes the corpus index; the inquiry loop tests its predictions
        against the presented evidence lines -- the corpus documents,
        mechanically (its run_test treats empty evidence as
        INSUFFICIENT, never as a crash)."""
        if inq.loop == LOOP_SCIENTIFIC_INQUIRY:
            return InquiryLoopContext(
                substrate=self._substrate, inquiry_id=inq.inquiry_id,
                budget_slice_s=inq.budget_slice_s,
                evidence=list(self._corpus_docs))
        from swarm_engine.curiosity.loops.questioning.loop import CorpusIndex
        return LoopContext(
            substrate=self._substrate, inquiry_id=inq.inquiry_id,
            budget_slice_s=inq.budget_slice_s,
            corpus=CorpusIndex(self._corpus_docs))

    # -- cadence ----------------------------------------------------------

    def tick(self) -> List[Dict[str, Any]]:
        """One cadence quantum: advance every ACTIVE inquiry one step,
        then admit newly-freed slots."""
        results: List[Dict[str, Any]] = []
        for inq in [i for i in self._inquiries.values()
                    if i.state == ST_ACTIVE]:
            results.append(self._step_inquiry(inq))
        self._admit_pending()
        return results

    def run_inquiry(self, decision: Any,
                    max_ticks: int = 100000) -> Dict[str, Any]:
        """Dispatch one inquiry and drive it to a stopped state."""
        inquiry_id = self.dispatch(decision)
        for _ in range(max_ticks):
            inq = self._inquiries[inquiry_id]
            if inq.state in (ST_TERMINATED, ST_SUSPENDED, ST_KILLED):
                assert inq.result is not None
                return inq.result
            self.tick()
        raise RuntimeError(
            f"inquiry {inquiry_id} did not stop within {max_ticks} ticks")

    def _step_inquiry(self, inq: InquiryRecord) -> Dict[str, Any]:
        if inq.kill_requested:
            return self._execute_kill(inq, CKPT_KILL)
        if inq.state == ST_PAUSED:
            return {"inquiry_id": inq.inquiry_id, "tick": "skipped(paused)"}
        if inq.spent_s >= inq.budget_slice_s:
            return self._handle_resource_boundary(
                inq, cause="inquiry slice exhausted before tick")
        t0 = self._clock()
        try:
            sres = self._loops[inq.loop].step(inq.loop_state, inq.ctx,
                                              inq.graph)
        except (SubstrateRefused, InquirySubstrateRefused) as exc:
            inq.spent_s += self._clock() - t0
            return self._handle_resource_boundary(
                inq, cause=f"substrate refused: {exc.reason}: {exc.message}")
        inq.spent_s += self._clock() - t0
        if inq.spent_s > inq.budget_slice_s:
            return self._handle_resource_boundary(
                inq, cause="inquiry slice exceeded after tick")
        if sres.done:
            assert sres.terminal is not None
            return self._persist_terminal(inq, sres.terminal)
        return {"inquiry_id": inq.inquiry_id, "tick": "advanced",
                "detail": sres.detail}

    # -- termination ------------------------------------------------------

    def _persist_terminal(self, inq: InquiryRecord,
                          terminal: Dict[str, Any]) -> Dict[str, Any]:
        """Converged: build the provenance-stamped finding, persist the
        payload + finding, attribute, route the terminal, release the
        loop's microcontrollers. The payload envelope is loop-agnostic;
        loop-specific extras come from the terminal dict with honest
        defaults -- a missing key is an absence, never fabricated."""
        evidence_id = "ev_" + uuid.uuid4().hex[:16]
        score = terminal.get("score", 0)
        # The inquiry loop carries its own triage verdict; questioning
        # terminals predate that key, so the score rule stands for them
        # (their persisted payloads are byte-identical to Phase 2).
        triage = (terminal.get("triage")
                  or ("propose_investigation" if score >= 3 else "retain"))
        # The inquiry loop emits verdicts, not precision scores; no
        # relevance score is fabricated for it (D-4: advisory only).
        if inq.loop == LOOP_QUESTIONING:
            relevance = {"score": round(score / 3.0, 4), "d4_note": D4_NOTE}
        else:
            relevance = {"score": None, "d4_note": D4_NOTE}
        payload = {
            "evidence_id": evidence_id,
            "inquiry_id": inq.inquiry_id,
            "trigger_id": inq.trigger.trigger_id,
            "loop": inq.loop,
            "bounded_objective": inq.trigger.bounded_objective,
            "origin": inq.trigger.origin,
            "terminal_state": terminal["terminal_state"],
            "precise_question": terminal.get("precise_question", ""),
            "precision_score": score,
            "passes": terminal.get("passes", []),
            "relevance": relevance,
            "triage": triage,
            "triage_note": (
                "proposed to Primary Acceptance; not accepted "
                "(curiosity never accepts its own findings)"
                if triage == "propose_investigation" else
                "fenced; true-but-imprecise per C-3.4; never promoted "
                "by curiosity"),
            "resource": {"spent_s": round(inq.spent_s, 6),
                         "slice_s": inq.budget_slice_s,
                         "grant_epoch": inq.epoch_id},
            "produced_at": time.time(),
        }
        payload_path = self._payload_dir / f"{evidence_id}.json"
        payload_path.write_text(json.dumps(payload, indent=2))
        finding = CuriosityFinding(
            evidence_id=evidence_id, loop=inq.loop,
            bounded_objective=inq.trigger.bounded_objective,
            origin=inq.trigger.origin,
            terminal_state=terminal["terminal_state"],
            provenance=EvidenceProvenance(
                loop=inq.loop,
                bounded_objective=inq.trigger.bounded_objective,
                model=LOOP_MODELS[inq.loop], triage=triage),
            payload_ref=str(payload_path))
        self._writer.submit(finding)  # validates + persists (fenced)
        self._attribute(inq, evidence_id=evidence_id,
                        outcome=("success" if terminal["terminal_state"]
                                 in SUCCESS_TERMINALS else "partial"),
                        detail=terminal["terminal_state"])
        self._ledger.record(TerminalRoute(
            route_id="route_" + uuid.uuid4().hex[:12],
            loop=inq.loop, terminal_state=terminal["terminal_state"],
            consumer="curiosity_evidence_store",
            reason=(f"inquiry {inq.inquiry_id} converged: "
                    f"{terminal.get('detail', '')}"),
            evidence_refs={"evidence_id": evidence_id}))
        self._release_inquiry_loop(inq)
        inq.evidence_id = evidence_id
        inq.terminal_state = terminal["terminal_state"]
        inq.state = ST_TERMINATED
        inq.note("terminated",
                 f"{inq.terminal_state} -> {evidence_id} "
                 f"spent={inq.spent_s:.4f}s")
        inq.result = {
            "inquiry_id": inq.inquiry_id,
            "terminal_state": inq.terminal_state,
            "evidence_id": evidence_id,
            "precise_question": terminal.get("precise_question", ""),
            "score": score,
            "triage": triage,
            "spent_s": round(inq.spent_s, 6),
        }
        return inq.result

    def _release_inquiry_loop(self, inq: InquiryRecord) -> None:
        """Retire the inquiry's microcontrollers (cascade). resolve_loop
        only when no other inquiry still uses the loop."""
        root = (inq.loop_state or {}).get("root_mc")
        if root:
            self._substrate.retire(root)
        others = [i for i in self._inquiries.values()
                  if i is not inq and i.loop == inq.loop
                  and i.state in (ST_ACTIVE, ST_PAUSED)]
        if not others:
            try:
                self._substrate.resolve_loop(inq.loop)
            except ValueError:
                pass

    # -- resource boundary --------------------------------------------------

    def _handle_resource_boundary(self, inq: InquiryRecord,
                                  cause: str) -> Dict[str, Any]:
        """The inquiry exceeded its FRM-funded slice: exact consumption
        is recorded, state is checkpointed and preserved, the inquiry is
        suspended, RESOURCE_BOUNDARY is returned upward, and the
        unresolved boundary is externalized as a BLOCKED finding."""
        boundary = {
            "spent_s": round(inq.spent_s, 6),
            "budget_s": inq.budget_slice_s,
            "cause": cause,
            "unresolved_boundary": inq.trigger.boundary_class,
        }
        # Stop the loop's machinery first (abort is idempotent).
        try:
            self._loops[inq.loop].abort(inq.loop_state, inq.ctx, inq.graph)
        except Exception:
            pass
        # Terminal event BEFORE the checkpoint: the saved lineage must
        # carry the boundary, or the resumed record loses it.
        inq.note("resource_boundary",
                 f"{cause}; spent={inq.spent_s:.4f}s "
                 f"slice={inq.budget_slice_s:.4f}s")
        checkpoint_id = self._checkpoint_inquiry(inq, CKPT_RESOURCE)
        evidence_id = "ev_" + uuid.uuid4().hex[:16]
        payload = {
            "evidence_id": evidence_id,
            "inquiry_id": inq.inquiry_id,
            "trigger_id": inq.trigger.trigger_id,
            "loop": inq.loop,
            "bounded_objective": inq.trigger.bounded_objective,
            "origin": inq.trigger.origin,
            "terminal_state": "BLOCKED",
            "resource_boundary": boundary,
            "partial_refinement": (
                (inq.loop_state or {}).get("passes", [])
                or list((inq.loop_state or {}).get("node_summaries",
                                                  {}).values())),
            "checkpoint_id": checkpoint_id,
            "detail": ("inquiry suspended at the resource boundary: the "
                       f"{inq.trigger.boundary_class} boundary is "
                       "unresolved and externalized here, not dropped"),
            "produced_at": time.time(),
        }
        payload_path = self._payload_dir / f"{evidence_id}.json"
        payload_path.write_text(json.dumps(payload, indent=2))
        finding = CuriosityFinding(
            evidence_id=evidence_id, loop=inq.loop,
            bounded_objective=inq.trigger.bounded_objective,
            origin=inq.trigger.origin, terminal_state="BLOCKED",
            provenance=EvidenceProvenance(
                loop=inq.loop,
                bounded_objective=inq.trigger.bounded_objective,
                model=LOOP_MODELS[inq.loop], triage="boundary"),
            payload_ref=str(payload_path))
        self._writer.submit(finding)
        self._attribute(inq, evidence_id=evidence_id, outcome="failed",
                        detail="BLOCKED: resource boundary")
        self._ledger.record(TerminalRoute(
            route_id="route_" + uuid.uuid4().hex[:12],
            loop=inq.loop, terminal_state="BLOCKED",
            consumer="curiosity_evidence_store",
            reason=(f"RESOURCE_BOUNDARY on inquiry {inq.inquiry_id}: "
                    f"{cause}; spent={inq.spent_s:.4f}s "
                    f"slice={inq.budget_slice_s:.4f}s"),
            evidence_refs={"evidence_id": evidence_id}))
        inq.evidence_id = evidence_id
        inq.terminal_state = "BLOCKED"
        inq.state = ST_SUSPENDED
        inq.result = {
            "inquiry_id": inq.inquiry_id,
            "terminal_state": "BLOCKED",
            "evidence_id": evidence_id,
            "resource_boundary": boundary,
            "checkpoint_id": checkpoint_id,
            "spent_s": round(inq.spent_s, 6),
        }
        return inq.result

    # -- kill / pause / resume --------------------------------------------------

    def kill_inquiry(self, inquiry_id: str, reason: str) -> Dict[str, Any]:
        """The kill switch (executive -> run controller -> loop ->
        microcontroller). Propagated and executed here; the executive
        never touches the machinery."""
        inq = self._require(inquiry_id)
        if inq.state in (ST_TERMINATED, ST_SUSPENDED, ST_KILLED):
            return {"inquiry_id": inquiry_id, "state": inq.state,
                    "detail": "already stopped; kill is a no-op"}
        inq.kill_requested = True
        inq.kill_reason = reason
        inq.note("kill_requested", reason)
        if inq.state == ST_ACTIVE:
            return self._execute_kill(inq, CKPT_KILL)
        # PENDING/PAUSED: mark killed without a checkpoint (no live
        # machinery to preserve); lineage records the kill.
        inq.state = ST_KILLED
        inq.note("killed", f"{reason} (no live machinery)")
        inq.result = {"inquiry_id": inquiry_id, "state": ST_KILLED,
                      "kill_reason": reason,
                      "checkpoint_id": None,
                      "spent_s": round(inq.spent_s, 6)}
        return inq.result

    def _execute_kill(self, inq: InquiryRecord,
                      ckpt_label: str) -> Dict[str, Any]:
        self._loops[inq.loop].abort(inq.loop_state, inq.ctx, inq.graph)
        # Terminal event BEFORE the checkpoint: the saved lineage must
        # carry the kill, or the resumed record loses the attempt's
        # terminal event across the process boundary.
        inq.note("killed", inq.kill_reason or ckpt_label)
        checkpoint_id = self._checkpoint_inquiry(inq, ckpt_label)
        self._attribute(inq, evidence_id=None, outcome="failed",
                        detail=f"killed: {inq.kill_reason}")
        inq.state = ST_KILLED
        inq.result = {
            "inquiry_id": inq.inquiry_id, "state": ST_KILLED,
            "kill_reason": inq.kill_reason,
            "checkpoint_id": checkpoint_id,
            "spent_s": round(inq.spent_s, 6),
        }
        return inq.result

    def pause_inquiry(self, inquiry_id: str) -> Dict[str, Any]:
        inq = self._require(inquiry_id)
        if inq.state != ST_ACTIVE:
            raise AdmissionRefused(
                f"cannot pause inquiry in state {inq.state}")
        inq.state = ST_PAUSED
        inq.note("paused", "in-memory; kill/suspend are the durable stops")
        return {"inquiry_id": inquiry_id, "state": ST_PAUSED}

    def resume_inquiry(self, inquiry_id: str) -> Dict[str, Any]:
        """Resume a PAUSED inquiry in-process, or a KILLED/SUSPENDED
        inquiry from its checkpoint -- including in a fresh process, where
        the inquiry is rebuilt from the checkpoint store under the same
        inquiry lineage."""
        inq = self._inquiries.get(inquiry_id)
        if inq is not None and inq.state == ST_PAUSED:
            inq.state = ST_ACTIVE
            inq.note("resumed", "from in-memory pause")
            return {"inquiry_id": inquiry_id, "state": ST_ACTIVE,
                    "resumed_from": "pause"}
        # Durable resume: locate the latest checkpoint for this inquiry.
        handoff_id = self._latest_checkpoint_handoff(inquiry_id)
        if handoff_id is None:
            raise AdmissionRefused(
                f"no checkpoint for inquiry {inquiry_id}: nothing to resume")
        loaded = self._checkpoints.load(handoff_id)
        # Integrity BEFORE trust: load() does not verify the
        # tamper-evidence hash. A corrupted checkpoint refuses here and
        # is never marked verified, never resumed.
        verify_checkpoint_integrity(loaded)
        self._checkpoints.mark_verified(loaded["checkpoint_id"])
        from_state = loaded["from_state"]
        saved_inq = from_state["inquiry"]
        # Rebuild the record under the SAME inquiry lineage.
        new = InquiryRecord(
            inquiry_id=saved_inq["inquiry_id"],
            trigger=_trigger_from_dict(saved_inq["trigger"]),
            loop=saved_inq["loop"], priority=saved_inq["priority"],
            grant=_grant_from_dict(saved_inq["grant"]),
            epoch_id=saved_inq["epoch_id"],
            budget_slice_s=saved_inq["budget_slice_s"],
            state=ST_ACTIVE, spent_s=saved_inq["spent_s"],
            lineage=saved_inq["lineage"],
            kill_reason=saved_inq.get("kill_reason", ""),
            checkpoint_seq=saved_inq.get("checkpoint_seq", 0),
            evidence_id=saved_inq.get("evidence_id"),
            terminal_state=saved_inq.get("terminal_state"))
        try:
            self._substrate.loop_view(new.loop)
        except ValueError:
            self._substrate.register_loop(
                new.loop, budget_s=new.grant.budget_s, max_concurrent=64)
        self._substrate.import_state(from_state["substrate"])
        new.ctx = self._build_ctx(new)
        new.graph = GraphController()
        new.loop_state = self._loops[new.loop].restore(from_state["loop_state"])
        new.note("resumed_from_checkpoint",
                 f"{handoff_id} (verified); lineage continues")
        self._checkpoints.mark_consumed(loaded["checkpoint_id"])
        self._inquiries[inquiry_id] = new
        return {"inquiry_id": inquiry_id, "state": ST_ACTIVE,
                "resumed_from": handoff_id,
                "lineage_events": len(new.lineage)}

    def _latest_checkpoint_handoff(self, inquiry_id: str
                                   ) -> Optional[str]:
        try:
            index = json.loads(self._index_path.read_text())
        except (FileNotFoundError, ValueError):
            return None
        entry = index.get(inquiry_id)
        return entry["handoff_id"] if entry else None

    def _write_index_entry(self, inquiry_id: str, handoff_id: str,
                           checkpoint_id: str, label: str) -> None:
        try:
            index = json.loads(self._index_path.read_text())
        except (FileNotFoundError, ValueError):
            index = {}
        index[inquiry_id] = {"handoff_id": handoff_id,
                             "checkpoint_id": checkpoint_id,
                             "label": label, "written_at": time.time()}
        self._index_path.write_text(json.dumps(index, indent=2))

    # -- checkpoints ----------------------------------------------------------

    def _checkpoint_inquiry(self, inq: InquiryRecord,
                            ckpt_label: str) -> str:
        """Inquiry -> TransitionCheckpointStore adapter (C-7.3/P-11):
        inquiry suspend state mapped onto the handoff/from_state shape.
        The GraphController is per-pass and rebuilt on resume; the
        substrate export carries the microcontroller state."""
        inq.checkpoint_seq += 1
        handoff = InquiryHandoff(
            handoff_id=f"inq-{inq.inquiry_id}-ckpt-{inq.checkpoint_seq}",            evidence_refs={"inquiry_id": inq.inquiry_id,
                           **({"evidence_id": inq.evidence_id}
                              if inq.evidence_id else {})},
            resource_delta={"spent_s": round(inq.spent_s, 6),
                            "slice_s": inq.budget_slice_s},
            from_loop=inq.loop, to_loop=inq.loop,
            terminal_state=ckpt_label,
            boundary_kind=inq.trigger.boundary_class,
            triggering_boundary_id=inq.trigger.trigger_id,
            chain_depth=0)
        serializable_loop_state = _jsonable(inq.loop_state)
        from_state = {
            "inquiry": {
                "inquiry_id": inq.inquiry_id,
                "trigger": inq.trigger.as_dict(),
                "loop": inq.loop, "priority": inq.priority,
                # Full dataclass fields (NOT the frozen six-key display
                # shape): the resume path rebuilds the real FrmGrant.
                "grant": _grant_as_dict(inq.grant),
                "epoch_id": inq.epoch_id,
                "budget_slice_s": inq.budget_slice_s,
                "state": inq.state, "spent_s": inq.spent_s,
                "lineage": inq.lineage,
                "kill_reason": inq.kill_reason,
                "checkpoint_seq": inq.checkpoint_seq,
                "evidence_id": inq.evidence_id,
                "terminal_state": inq.terminal_state,
            },
            "lineage": inq.lineage,
            "loop_state": serializable_loop_state,
            "substrate": self._substrate.export_state(),
        }
        # The "checkpointed" event is noted BEFORE the save. from_state
        # holds the live inq.lineage list by reference, so the stored
        # lineage carries the checkpoint event: a resumed inquiry's
        # lineage shows the checkpoint, not just the events before it.
        # (Callers note the attempt's terminal event -- killed /
        # resource_boundary -- before calling this, for the same reason.)
        inq.note("checkpointed", f"{handoff.handoff_id} (saving)")
        checkpoint_id = self._checkpoints.save(handoff, from_state)
        self._write_index_entry(inq.inquiry_id, handoff.handoff_id,
                                checkpoint_id, ckpt_label)
        return checkpoint_id

    # -- attribution ----------------------------------------------------------

    def _attribute(self, inq: InquiryRecord, *,
                   evidence_id: Optional[str], outcome: str,
                   detail: str) -> None:
        # Attempt-scoped refs: a killed-then-resumed inquiry re-enters
        # _attribute under the SAME inquiry_id, and work_ref/result_ref
        # are primary keys. attempt_no (counting resumed_from_checkpoint
        # lineage events) keeps every attempt's records while the
        # parent_work_ref chain preserves the shared inquiry lineage.
        attempt = (sum(1 for e in inq.lineage
                       if e.get("event") == "resumed_from_checkpoint")
                   + 1)
        work_ref = (f"work_{inq.inquiry_id}" if attempt == 1
                    else f"work_{inq.inquiry_id}#a{attempt}")
        parent_ref = (None if attempt == 1
                      else f"work_{inq.inquiry_id}"
                      if attempt == 2
                      else f"work_{inq.inquiry_id}#a{attempt - 1}")
        self._attribution.record_work(WorkUnit(
            work_ref=work_ref, inquiry_id=inq.inquiry_id,
            grant_id=inq.grant.grant_id,
            controller_id="curiosity_run_controller",
            attempt_no=attempt, parent_work_ref=parent_ref))
        result_ref = (f"res_{inq.inquiry_id}" if attempt == 1
                      else f"res_{inq.inquiry_id}#a{attempt}")
        self._attribution.record_result(Result(
            result_ref=result_ref, work_ref=work_ref, outcome=outcome,
            evidence_ref=evidence_id, detail=detail).validate())
        self._attribution.record_admission(AdmissionRecord(
            admission_id="adm_" + uuid.uuid4().hex[:12],
            result_ref=result_ref, verdict="retained", yield_value=0.0,
            decided_by="curiosity_run_controller (retention, not acceptance)",
        ).validate())

    # -- observable state ------------------------------------------------------

    def inquiry_views(self) -> List[Dict[str, Any]]:
        """Loop-level aggregates only -- what the executive may see."""
        return [i.view() for i in self._inquiries.values()]

    def loop_view(self, loop: str) -> Dict[str, Any]:
        """The substrate's own aggregate LoopView (no microcontroller
        ids, purposes, or internals -- enforced by construction in the
        base class)."""
        try:
            return self._substrate.loop_view(loop).as_dict()
        except ValueError:
            return {"loop": loop, "registered": False}

    # -- internals --------------------------------------------------------------

    def _require(self, inquiry_id: str) -> InquiryRecord:
        inq = self._inquiries.get(inquiry_id)
        if inq is None:
            raise AdmissionRefused(f"unknown inquiry {inquiry_id!r}")
        return inq


def _jsonable(obj: Any) -> Any:
    """Round-trip through JSON to guarantee checkpoint serializability."""
    return json.loads(json.dumps(obj, default=str))


def _trigger_from_dict(d: Dict[str, Any]) -> Any:
    from swarm_engine.curiosity.executive.boundary import CuriosityTrigger
    return CuriosityTrigger(
        trigger_id=d["trigger_id"], boundary_class=d["boundary_class"],
        question_text=d["question_text"],
        bounded_objective=d["bounded_objective"], origin=d["origin"],
        presented_at=d.get("presented_at", 0.0))


def _grant_as_dict(grant: Any) -> Dict[str, Any]:
    """Full constructor fields of the frozen FrmGrant (dataclasses.asdict
    recurses into LendingRecord). The frozen six-key as_dict() is the
    display shape and cannot rebuild the grant."""
    import dataclasses
    return dataclasses.asdict(grant)


def _grant_from_dict(d: Dict[str, Any]) -> Any:
    from swarm_engine.curiosity.frm.grant import FrmGrant, LendingRecord
    d = dict(d)
    lending = d.get("lending")
    if isinstance(lending, dict):
        d["lending"] = LendingRecord(**lending)
    fields = FrmGrant.__dataclass_fields__
    return FrmGrant(**{k: v for k, v in d.items() if k in fields})
