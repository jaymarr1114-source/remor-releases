"""The Creativity Executive Controller — paper §9, T0–T9 rendering.

Paper: ~/workspace/architecture/exec-creativity-controller_2026-09-29.md
Mission: CREATIVITY-EXEC-1 (Phase 2, mission 1).

T0 — identity and authority
---------------------------
Third executive on paper. Peer of the Primary Executive Controller —
not a subordinate, not a Primary mode, not a seventh loop (§1's
category error). Does NOT merge with or rename the EXEC2/FRM second
executive. Authority derives from James's seven §8 decisions (paper
§11, 2026-09-30): peer-with-gap-registry-handoff (Q1), boundary
precedence for ambiguous commissions (Q2), FRM-governed compute+cost
budget (Q3), host-admits-and-James-can-remove (Q4), independent
sequencing (Q5), bounded refinement with adaptive stop (Q6), earned
discretion from constrained criteria (Q7).

Decided deviations from the Primary baseline (all DECIDED, cited):
  D-4 — selection grammar begins with intent-state ownership
        ("What is wanted? (intent)" → "What creative process state is
        admissible?" → "Enter/continue that state").
  D-5 — the six elements are ORDERED STAGES of one creative process,
        not six independent boundary classes; selection is preserved
        via "which stage owns the current state".
  D-6 — a dedicated conditionally-activated Creativity Run Controller
        (RUNCTRL-1's build, not this mission's); this executive exposes
        step()/run() drivable directly and never assumes the run
        controller exists.
  D-7 — acceptance, kill/execution-control, and attestation stay three
        distinct mechanisms; the Acceptance panel judges artifacts;
        the kill ladder attaches; GAM does NOT attach (attestation is
        scoped to curiosity governance).
  D-8 — no separate brain: the same cognition slot (CognitionProvider
        protocol), cognition as utility, never decider.

T9 — the verified-factual substrate rule (standing, §3): the executive
constructs and holds the REAL ledger bound to the REAL stores and
wires ledger → critique → release directly. No verdict object is
accepted from outside (the pipeline always runs inside); no unbound
ledger is substitutable (the binding is the executive's, verified live
at each run). This closes the two trust boundaries Phase 1 left open:
CRITIQUE-1's verdict trust boundary and RELEASE-1's ledger-instance
trust boundary.
"""

from __future__ import annotations

import inspect
import itertools
import json
import os
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from runtime.core.executive.detector import ScanReport
from runtime.core.microcontroller.substrate import (
    CognitionProvider,
    NullCognitionProvider,
)
from runtime.acquisition.gaps import GapRegistry
from runtime.governance.provenance import ProvenanceStore, TrustLevel
# NOTE on import namespaces: pylib/swarm_engine is a symlink to runtime,
# so `runtime.X` and `swarm_engine.X` are distinct module objects with
# distinct classes (notably the Kind enum — `is` comparisons break across
# the boundary). runtime/synthesis/composer.py imports its primitives via
# the swarm_engine prefix, so this module does the same: PrimitiveRegistry
# and Composer MUST come from swarm_engine.primitives.core /
# swarm_engine.synthesis.composer, or isinstance checks and the static
# type checker silently disagree. Batteries must define primitives via
# the same swarm_engine prefix (the proof_critique.py convention).
from swarm_engine.primitives.core import PrimitiveRegistry
from swarm_engine.synthesis.composer import Composer
from runtime.services.evidence import EvidenceStore

from runtime.creativity.stages import (
    CreativeStage,
    StageDescriptor,
    StageRefused,
    admissible_next,
    describe,
    is_terminal,
    refinement_loop_admissible,
)
from runtime.creativity.intent import (
    CreativeIntent,
    IntentRefused,
    admissible_stages,
    register_intent,
)
from runtime.creativity.ledger import (
    CompositionVerdict,
    Ledger,
    LedgerEntry,
    LedgerRefused,
)
from runtime.creativity.critique import (
    NOVEL,
    RETRIEVAL,
    VALUE_CRITERIA_V1,
    CandidateComposition,
    CritiqueRefused,
    CritiqueReport,
    NoveltyVerdict,
    ValueScore,
    canonical_signature,
    check_novelty,
    critique_candidate,
    critique_value,
)
from runtime.creativity.release import (
    AdmissionRecord,
    AdmissionRefused,
    AdmissionRegistry,
    ReleaseResult,
    run_release_flow,
)


# ---------------------------------------------------------------------------
# T0 — identity block
# ---------------------------------------------------------------------------

EXECUTIVE_IDENTITY: Dict[str, Any] = {
    "name": "CreativityExecutiveController",
    "paper": ("~/workspace/architecture/exec-creativity-controller_2026-09-29.md"),
    "executive_number": "third executive on paper",
    "relation_to_primary": "peer — not subordinate, not a mode, not a loop",
    "relation_to_exec2_frm": "does not merge with or rename the second (FRM) executive",
    "authority": (
        "James's seven §8 decisions, paper §11 (2026-09-30): "
        "Q1 peer with gap-registry handoff; Q2 boundary precedence; "
        "Q3 FRM-governed compute+cost budget; Q4 host-admits-and-James-can-remove; "
        "Q5 independent sequencing; Q6 bounded refinement with adaptive stop; "
        "Q7 earned discretion from constrained criteria"
    ),
    "decided_deviations": ("D-4", "D-5", "D-6", "D-7", "D-8"),
}


class ExecutiveRefused(Exception):
    """The executive refused an operation: exact reason, never silent."""


# ---------------------------------------------------------------------------
# Q2 — ambiguity classification (reviewable v1 proxy)
# ---------------------------------------------------------------------------
#
# A commission is AMBIGUOUS when its stated outcome names no concrete
# artifact kind from the reviewable table below. The table is a v1 proxy,
# not a semantic judgment (Q7: that latitude is earned, not assumed).
# Misclassifications fail toward AMBIGUOUS — the safe direction, since
# an ambiguous commission gets the Primary's boundary question first
# (convergence before creation, §11 Q2).

ARTIFACT_KINDS_V1 = frozenset({
    "song", "music", "melody", "tune",
    "image", "picture", "drawing", "logo", "icon",
    "story", "poem", "poetry", "text", "article", "essay", "document",
    "video", "animation",
    "zip", "archive",
    "plan", "design", "blueprint",
    "expression", "arithmetic", "calculation",
})

AMBIGUOUS = "ambiguous"
CLEAR = "clear"


def classify_ambiguity(intent: CreativeIntent) -> str:
    """Classify a commissioned intent as AMBIGUOUS or CLEAR.

    Mechanical v1 rule: the lowercased outcome names a concrete artifact
    kind from ARTIFACT_KINDS_V1 → CLEAR; otherwise AMBIGUOUS. The table is
    the reviewable artifact — James expands it, the executive never
    invents semantic confidence it does not have.
    """
    if not isinstance(intent, CreativeIntent):
        raise ExecutiveRefused(
            f"ambiguity classification needs a CreativeIntent, got "
            f"{type(intent).__name__}")
    outcome = (intent.outcome or "").lower()
    for kind in ARTIFACT_KINDS_V1:
        if kind in outcome:
            return CLEAR
    return AMBIGUOUS


# ---------------------------------------------------------------------------
# T2 — the six loop controllers
# ---------------------------------------------------------------------------

@dataclass
class LoopStep:
    """One loop controller's step outcome."""
    stage: CreativeStage
    advanced: bool
    next_stage: Optional[CreativeStage]
    detail: str


class CreativeLoopController:
    """One ordered stage's controller (paper §9 T2, D-5).

    Owns its stage's decision class, inputs, outputs, and termination
    condition (drawn from stages.describe(), i.e. paper §2 verbatim).
    step() advances the run context only when the stage's termination
    condition is met; the returned next_stage is always drawn from
    admissible_next(stage) — inadmissible transitions are structurally
    unreachable, not refused by convention.
    """

    def __init__(self, stage: CreativeStage) -> None:
        self._stage = stage
        self._descriptor: StageDescriptor = describe(stage)

    @property
    def stage(self) -> CreativeStage:
        return self._stage

    @property
    def descriptor(self) -> StageDescriptor:
        return self._descriptor

    def admissible(self) -> Tuple[CreativeStage, ...]:
        return admissible_next(self._stage)

    def step(self, ctx: "_RunContext") -> LoopStep:
        handler = {
            CreativeStage.INTENT: self._step_intent,
            CreativeStage.GENERATION: self._step_generation,
            CreativeStage.VARIATION: self._step_variation,
            CreativeStage.CRITIQUE: self._step_critique,
            CreativeStage.REFINEMENT: self._step_refinement,
            CreativeStage.RELEASE: self._step_release,
        }[self._stage]
        return handler(ctx)

    # -- per-stage termination -------------------------------------------
    def _step_intent(self, ctx: "_RunContext") -> LoopStep:
        # Termination: "the commission is registered as a stated outcome".
        admissible_stages(ctx.intent)  # raises the exact refusal if invalid
        nxt = admissible_next(CreativeStage.INTENT)[0]
        return LoopStep(CreativeStage.INTENT, True, nxt,
                        f"intent registered: {ctx.intent.outcome!r} "
                        f"(principal {ctx.intent.principal!r})")

    def _step_generation(self, ctx: "_RunContext") -> LoopStep:
        # Termination: "candidate artifacts composed from verified primitives".
        ctx.candidates = ctx.executive._generate(ctx)
        if not ctx.candidates:
            nxt = admissible_next(CreativeStage.GENERATION)[0]
            return LoopStep(CreativeStage.GENERATION, True, nxt,
                            "no candidates survived legality — proceeding "
                            "with the empty set visibly (gaps recorded)")
        nxt = admissible_next(CreativeStage.GENERATION)[0]
        return LoopStep(CreativeStage.GENERATION, True, nxt,
                        f"{len(ctx.candidates)} candidates composed from "
                        f"verified primitives")

    def _step_variation(self, ctx: "_RunContext") -> LoopStep:
        # Termination: candidates varied along explicit dimensions.
        before = len(ctx.candidates)
        ctx.candidates = ctx.executive._vary(ctx)
        nxt = admissible_next(CreativeStage.VARIATION)[0]
        return LoopStep(CreativeStage.VARIATION, True, nxt,
                        f"variation: {before} → {len(ctx.candidates)} "
                        f"candidates along explicit dimensions")

    def _step_critique(self, ctx: "_RunContext") -> LoopStep:
        # Termination: every candidate carries its full judgment record.
        ctx.reports = [ctx.executive._critique_one(c, ctx)
                       for c in ctx.candidates]
        best = ctx.executive._best_value_count(ctx.reports)
        ctx.value_history.append(best)
        nxt = admissible_next(CreativeStage.CRITIQUE)[0]
        return LoopStep(CreativeStage.CRITIQUE, True, nxt,
                        f"{len(ctx.reports)} candidates judged "
                        f"(best value {best}/{len(VALUE_CRITERIA_V1)} criteria)")

    def _step_refinement(self, ctx: "_RunContext") -> LoopStep:
        # Termination: the loop-back/admit decision is made — never loops
        # forever (§11 Q6). The bound is an explicit parameter, never a
        # chosen constant; the adaptive stop fires inside the bound.
        if ctx.executive._refinement_continue(ctx):
            ctx.iterations_used += 1
            nxt = CreativeStage.VARIATION  # head of the iterate loop (§2)
            assert nxt in admissible_next(CreativeStage.REFINEMENT)
            return LoopStep(CreativeStage.REFINEMENT, True, nxt,
                            f"refinement iteration {ctx.iterations_used}/"
                            f"{ctx.refinement_bound} admissible — "
                            f"looping back to VARIATION")
        nxt = CreativeStage.RELEASE
        assert nxt in admissible_next(CreativeStage.REFINEMENT)
        return LoopStep(CreativeStage.REFINEMENT, True, nxt,
                        "refinement ends: bound exhausted or adaptive stop — "
                        "release with state/evidence, never loop forever")

    def _step_release(self, ctx: "_RunContext") -> LoopStep:
        # Terminal: is_terminal(RELEASE) — nothing is admissible after it.
        assert is_terminal(CreativeStage.RELEASE)
        return LoopStep(CreativeStage.RELEASE, True, None,
                        "release is terminal (D-5)")


# ---------------------------------------------------------------------------
# T3 — microcontrollers inside the creative loops
# ---------------------------------------------------------------------------
#
# The Primary's microcontroller substrate (runtime/core/microcontroller/,
# frozen interface v1) is consumed read-only as the RULES reference, but
# the creativity executive does NOT register its loops there: the frozen
# v1 loop set is {run, acquisition, execution, acceptance, distillation,
# generalization}, and registering creativity stages would be cross-domain
# contamination of another executive's substrate. Instead this registry
# implements the same lifecycle rules (spawn with purpose/parent, retire,
# cascade-retire children, max depth, unwind on resolution) scoped to the
# creativity stages. Same nesting rule, same live/retire-inside-the-loop
# lifecycle (paper §9 T3) — own registry, shared rules.

_MC_ACTIVE = "active"
_MC_RESOLVED = "resolved"
_MC_MAX_DEPTH = 8


@dataclass
class _MCRecord:
    mc_id: str
    loop: str  # a CreativeStage name — creativity-scoped, never Primary loops
    purpose: str
    parent_id: Optional[str]
    depth: int
    state: str = _MC_ACTIVE
    children: List[str] = field(default_factory=list)
    spawned_at: float = field(default_factory=time.time)
    resolved_at: Optional[float] = None


class MicrocontrollerRegistry:
    """Per-creative-loop microcontroller lifecycle (paper §9 T3)."""

    _CREATIVITY_LOOPS = frozenset(s.name for s in CreativeStage)

    def __init__(self) -> None:
        self._records: Dict[str, _MCRecord] = {}
        self._seq = 0

    def spawn(self, loop: str, purpose: str,
              *, parent_id: Optional[str] = None) -> _MCRecord:
        if loop not in self._CREATIVITY_LOOPS:
            raise ExecutiveRefused(
                f"microcontroller spawn refused: loop {loop!r} is not a "
                f"creativity stage — cross-loop spawn refused "
                f"(creativity loops: {sorted(self._CREATIVITY_LOOPS)})")
        if not purpose or not purpose.strip():
            raise ExecutiveRefused(
                "microcontroller spawn refused: purpose must be non-empty")
        depth = 0
        if parent_id is not None:
            parent = self._records.get(parent_id)
            if parent is None or parent.state != _MC_ACTIVE:
                raise ExecutiveRefused(
                    f"microcontroller spawn refused: parent {parent_id!r} "
                    f"is not an active record")
            if parent.loop != loop:
                raise ExecutiveRefused(
                    "microcontroller spawn refused: cross-loop nesting "
                    "refused — a child lives in its parent's loop")
            depth = parent.depth + 1
            if depth > _MC_MAX_DEPTH:
                raise ExecutiveRefused(
                    f"microcontroller spawn refused: max depth "
                    f"{_MC_MAX_DEPTH} exceeded")
        self._seq += 1
        rec = _MCRecord(
            mc_id=f"mc-{uuid.uuid4().hex[:12]}",
            loop=loop, purpose=purpose.strip(),
            parent_id=parent_id, depth=depth)
        self._records[rec.mc_id] = rec
        if parent_id is not None:
            self._records[parent_id].children.append(rec.mc_id)
        return rec

    def retire(self, mc_id: str, outcome: str = "resolved") -> _MCRecord:
        rec = self._records.get(mc_id)
        if rec is None:
            raise ExecutiveRefused(
                f"microcontroller retire refused: unknown {mc_id!r}")
        if rec.state != _MC_ACTIVE:
            raise ExecutiveRefused(
                f"microcontroller retire refused: {mc_id!r} already "
                f"{rec.state}")
        # Unwind bottom-up: cascade-retire active children first.
        for child_id in list(rec.children):
            child = self._records.get(child_id)
            if child is not None and child.state == _MC_ACTIVE:
                self.retire(child_id, outcome=f"retired_cascade:{outcome}")
        rec.state = _MC_RESOLVED
        rec.resolved_at = time.time()
        return rec

    def active_in(self, loop: str) -> List[_MCRecord]:
        return [r for r in self._records.values()
                if r.loop == loop and r.state == _MC_ACTIVE]

    def get(self, mc_id: str) -> Optional[_MCRecord]:
        return self._records.get(mc_id)


# ---------------------------------------------------------------------------
# Generation — explicit bounded composition search
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class SearchBounds:
    """Explicit bounds on the composition search (reviewable parameters).

    The search enumerates ordered arrangements of verified primitives up
    to max_composition_size, capped at max_candidates total. The bounds
    are engineering parameters with their derivation documented — they
    are not James's open numbers, but they are reviewable.
    """
    max_composition_size: int = 3
    max_candidates: int = 25


@dataclass
class _RunContext:
    executive: "CreativityExecutiveController"
    intent: CreativeIntent
    ambiguity: str
    current_stage: CreativeStage
    refinement_bound: int
    constraints: Tuple[Dict[str, Any], ...] = ()
    iterations_used: int = 0
    candidates: List[Tuple[CandidateComposition, NoveltyVerdict]] = field(
        default_factory=list)
    reports: List[CritiqueReport] = field(default_factory=list)
    value_history: List[int] = field(default_factory=list)
    gaps: List[str] = field(default_factory=list)
    absent: List[str] = field(default_factory=list)
    mc: MicrocontrollerRegistry = field(default_factory=MicrocontrollerRegistry)
    killed: bool = False


@dataclass
class ExecutiveOutcome:
    """The terminal outcome of a run."""
    status: str  # completed | suspended | refused | killed | stopped
    stage: Optional[CreativeStage]
    detail: str
    admission: Optional[AdmissionRecord] = None
    boundary: Optional[Any] = None  # the BoundaryPresentation, when suspended
    gaps: Tuple[str, ...] = ()
    absent: Tuple[str, ...] = ()
    candidates_considered: int = 0


COMPLETED = "completed"
SUSPENDED = "suspended"
REFUSED = "refused"
KILLED = "killed"
STOPPED = "stopped"


# ---------------------------------------------------------------------------
# T9 — the Creativity Executive Controller
# ---------------------------------------------------------------------------

class CreativityExecutiveController:
    """The executive itself (paper §9, T0–T9).

    Construction rule (T9 trust-boundary closure): the executive builds
    its OWN stores from ``store_dir`` — EvidenceStore, ProvenanceStore,
    GapRegistry, Ledger — and never accepts store/ledger/verdict objects
    as parameters. The pipeline (ledger → critique → release) always runs
    inside, on the executive's own bound ledger. There is structurally no
    parameter through which an outside verdict or an unbound ledger could
    be substituted (proven by signature inspection in the battery).

    ``primitives`` is capability inventory (the admitted-capability set
    the distillation machinery maintains); verification is the ledger's
    job, and EVERY composition is legality-checked against the live
    ledger — an unverified primitive in the registry cannot slip through.

    ``boundary_scan`` is the read-only Q2 hook: a zero-arg callable
    returning the Primary's ScanReport. It runs FIRST on every
    commission; the creative task suspends (convergence precedence)
    iff the commission is AMBIGUOUS and the scan presents a genuine
    boundary.

    ``stop_event`` is a threading.Event (the same type the V10
    RunController uses): the kill ladder polls it between steps; a set
    event stops the run cold with state preserved.

    ``cognition`` is utility, never decider (D-8, T4): it may suggest
    variation dimensions; every suggestion passes through the same
    legality/novelty predicates as everything else.
    """

    def __init__(self, *,
                 store_dir: str,
                 primitives: PrimitiveRegistry,
                 boundary_scan: Callable[[], ScanReport],
                 stop_event: threading.Event,
                 cognition: Optional[CognitionProvider] = None,
                 search_bounds: Optional[SearchBounds] = None,
                 repo_root: str,
                 admission_db_path: str) -> None:
        if not store_dir or not os.path.isdir(store_dir):
            raise ExecutiveRefused(
                f"executive needs an existing store_dir, got {store_dir!r}")
        if not isinstance(primitives, PrimitiveRegistry):
            raise ExecutiveRefused(
                "executive needs a PrimitiveRegistry as capability "
                f"inventory, got {type(primitives).__name__}")
        if not callable(boundary_scan):
            raise ExecutiveRefused("executive needs a boundary_scan callable")
        if not isinstance(stop_event, threading.Event):
            raise ExecutiveRefused(
                "executive needs a threading.Event as the kill signal, got "
                f"{type(stop_event).__name__}")
        if cognition is not None and not hasattr(
                cognition, "request_cognition"):
            raise ExecutiveRefused(
                "cognition must implement the CognitionProvider protocol "
                "(request_cognition)")

        # T9: build the real stores HERE — never accepted from outside.
        self._evidence = EvidenceStore(
            db_path=os.path.join(store_dir, "ev.db"))
        self._provenance = ProvenanceStore(
            db_path=os.path.join(store_dir, "prov.db"))
        self._gaps = GapRegistry(
            None, db_path=os.path.join(store_dir, "gaps.db"))
        self._ledger = Ledger(
            self._evidence, self._provenance, gap_registry=self._gaps)

        self._primitives = primitives
        self._composer = Composer(primitives)
        self._boundary_scan = boundary_scan
        self._stop_event = stop_event
        self._cognition: CognitionProvider = (
            cognition if cognition is not None else NullCognitionProvider())
        self._bounds = search_bounds or SearchBounds()
        self._repo_root = repo_root
        self._registry = AdmissionRegistry(admission_db_path)
        self._loops: Dict[CreativeStage, CreativeLoopController] = {
            stage: CreativeLoopController(stage) for stage in CreativeStage
        }

    # -- T9 accessors (the bound machinery; still no outside substitution) --
    @property
    def ledger(self) -> Ledger:
        """The executive's own ledger, bound to its own real stores."""
        return self._ledger

    @property
    def identity(self) -> Dict[str, Any]:
        return dict(EXECUTIVE_IDENTITY)

    # -- T1: commission ----------------------------------------------------
    def commission(self, intent: CreativeIntent,
                   *, refinement_bound: int,
                   constraints: Sequence[Dict[str, Any]] = ()) -> "_Commission":
        """T1 — "What is wanted? (intent)" → admissibility → the run.

        The boundary scan runs FIRST on every commission (ordering is
        structural: nothing creative happens before it). Suspension
        (convergence precedence) happens iff the commission is AMBIGUOUS
        and the scan presents a genuine boundary.

        constraints: structured constraint dicts from the commission
        (same mechanical shape as CandidateComposition.constraints).
        requires_primitive entries naming unverified primitives become
        named gaps via the real gap machinery (T7a).
        """
        if not isinstance(refinement_bound, int) or refinement_bound < 0:
            raise ExecutiveRefused(
                "refinement_bound is an explicit required parameter "
                "(§11 Q6): non-negative int, no default invented")
        admissible_stages(intent)  # q1+q2; raises the exact refusal
        clean_constraints: Tuple[Dict[str, Any], ...] = tuple(
            c for c in constraints)
        ambiguity = classify_ambiguity(intent)
        scan = self._boundary_scan()  # FIRST — before any creative work
        if not isinstance(scan, ScanReport):
            raise ExecutiveRefused(
                "boundary_scan must return the Primary's ScanReport, got "
                f"{type(scan).__name__}")
        if ambiguity == AMBIGUOUS and scan.presentations:
            presentation = scan.presentations[0]
            gap_id = self._suspend_gap(intent, presentation)
            return _Commission.make_suspended(
                intent, ambiguity, presentation,
                detail=(f"convergence precedence (§11 Q2): commission is "
                        f"AMBIGUOUS and the Primary presents a genuine "
                        f"boundary ({presentation.kind}, "
                        f"{presentation.boundary_id}) — the creative task "
                        f"suspends with the boundary named; suspension "
                        f"recorded as gap {gap_id}"))
        return _Commission(
            intent=intent, ambiguity=ambiguity,
            refinement_bound=refinement_bound,
            constraints=clean_constraints)

    def _suspend_gap(self, intent: CreativeIntent, presentation: Any) -> str:
        """Record the convergence-precedence suspension as a named gap
        through the real gap machinery — the suspension is visible in
        the gap queue, never silent."""
        from runtime.acquisition.gaps import GapRecord, TechniqueBlock
        record = GapRecord(
            summary=(f"[creativity-exec] commission suspended: convergence "
                     f"precedence — Primary boundary {presentation.kind} "
                     f"({presentation.boundary_id}) on AMBIGUOUS commission "
                     f"{intent.outcome!r}"),
            registered_by="creativity-executive",
            technique=TechniqueBlock(
                objective=("route the Primary boundary to its owning loop "
                           "and converge it; then the creative commission "
                           "may be re-commissioned"),
                note=(f"Obstruction: genuine {presentation.kind} boundary "
                      f"presented by {presentation.observed_by}. The "
                      f"creativity executive does not run loop machinery "
                      f"(T7); the boundary belongs to the Primary.")),
            evidence=[{
                "kind": "observation",
                "observed": (f"boundary scan presented "
                             f"{presentation.kind} before any generation"),
                "detail": (f"boundary_id={presentation.boundary_id}, "
                           f"observed_by={presentation.observed_by}"),
            }],
        )
        return self._gaps.register(record).gap_id

    # -- the run ------------------------------------------------------------
    def run(self, commission: "_Commission") -> ExecutiveOutcome:
        """Drive the six ordered stages to a terminal outcome."""
        if commission.suspended:
            return ExecutiveOutcome(
                status=SUSPENDED, stage=None,
                detail=commission.suspend_detail,
                boundary=commission.boundary)
        ctx = _RunContext(
            executive=self, intent=commission.intent,
            ambiguity=commission.ambiguity,
            current_stage=CreativeStage.INTENT,
            refinement_bound=commission.refinement_bound,
            constraints=commission.constraints)
        # T7a: named gaps for demanded-but-unverified capabilities, first.
        self._demand_gaps(ctx)
        while True:
            if self._stop_event.is_set():
                return self._killed(ctx, "kill signal set — run stops cold")
            controller = self._loops[ctx.current_stage]
            mc = ctx.mc.spawn(ctx.current_stage.name,
                              f"{ctx.current_stage.name.lower()}-pass")
            try:
                step = controller.step(ctx)
            finally:
                # Unwind: the stage's microcontrollers retire with the stage.
                for active in ctx.mc.active_in(ctx.current_stage.name):
                    ctx.mc.retire(active.mc_id,
                                  outcome="stage-complete-unwind")
            if not step.advanced:
                return ExecutiveOutcome(
                    status=REFUSED, stage=ctx.current_stage,
                    detail=f"stage {ctx.current_stage.name} refused to "
                           f"advance: {step.detail}",
                    gaps=tuple(ctx.gaps), absent=tuple(ctx.absent),
                    candidates_considered=len(ctx.candidates))
            if self._stop_event.is_set():
                return self._killed(ctx, "kill signal set mid-stage")
            if step.stage == CreativeStage.RELEASE:
                return self._release_stage(ctx)
            nxt = step.next_stage
            if nxt is None or nxt not in controller.admissible():
                raise ExecutiveRefused(
                    f"internal error: inadmissible transition "
                    f"{ctx.current_stage.name} → {nxt}")
            ctx.current_stage = nxt

    def step(self, ctx: "_RunContext") -> LoopStep:
        """Advance exactly one stage — the directly-drivable interface
        RUNCTRL-1 will use. Does not assume the run controller exists."""
        if self._stop_event.is_set():
            raise ExecutiveRefused("kill signal set — step refused")
        controller = self._loops[ctx.current_stage]
        return controller.step(ctx)

    # -- stage machinery ----------------------------------------------------
    def _killed(self, ctx: _RunContext, detail: str) -> ExecutiveOutcome:
        # Preservation: the ledger is read-side (nothing to corrupt);
        # the run context records where the run died.
        return ExecutiveOutcome(
            status=KILLED, stage=ctx.current_stage, detail=detail,
            gaps=tuple(ctx.gaps), absent=tuple(ctx.absent),
            candidates_considered=len(ctx.candidates))

    def _demand_gaps(self, ctx: _RunContext) -> None:
        """T7a — demanded-but-unverified capabilities become named gaps.

        Structured requires_primitive constraints on the commission name
        primitives the commission wants; any without a live ledger entry
        is unverified demand: the ledger's check registers the named gap
        through the real gap machinery and the absence is recorded
        visibly in the run context. The creative task proceeds without
        that element — visibly, never silently.
        """
        for constraint in ctx.constraints:
            if not isinstance(constraint, dict):
                continue
            if constraint.get("kind") != "requires_primitive":
                continue
            pid = constraint.get("primitive")
            if not isinstance(pid, str) or not pid:
                continue
            verdict = self._ledger.check_composition([pid])
            if not verdict.legal:
                ctx.gaps.extend(verdict.gaps)
                ctx.absent.extend(verdict.absent)

    def _generate(self, ctx: _RunContext) -> List[Tuple[CandidateComposition,
                                                        NoveltyVerdict]]:
        """The honest core: enumerate → legality-check → novelty-filter →
        value-rank. Each step is a mechanism with an explicit predicate."""
        verified = [pid for pid in self._ledger.indexed()
                    if self._ledger.check_composition([pid]).legal]
        if self._stop_event.is_set():
            return []
        arrangements: List[Tuple[str, ...]] = []
        for size in range(1, self._bounds.max_composition_size + 1):
            for arrangement in itertools.permutations(verified, size):
                arrangements.append(arrangement)
                if len(arrangements) >= self._bounds.max_candidates:
                    break
            if len(arrangements) >= self._bounds.max_candidates:
                break
        scored: List[Tuple[CandidateComposition, NoveltyVerdict]] = []
        for arrangement in arrangements:
            if self._stop_event.is_set():
                break
            verdict = self._ledger.check_composition(list(arrangement))
            if not verdict.legal:
                # The ledger registered the named gap; the candidate is
                # impossible — recorded by the ledger, not hidden.
                continue
            candidate = self._build_candidate(arrangement, ctx, variant_of=None)
            if candidate is None:
                continue
            novelty = check_novelty(candidate, self._provenance)
            scored.append((candidate, novelty))
        # Rank: NOVEL above RETRIEVAL (explicit predicate); the release
        # stage enforces the bar — generation proposes, release disposes.
        scored.sort(key=lambda pair: 0 if pair[1].verdict == NOVEL else 1)
        return scored

    def _build_candidate(self, arrangement: Sequence[str], ctx: _RunContext,
                         *, variant_of: Optional[str],
                         arg_overrides: Optional[Dict[str, Any]] = None,
                         ) -> Optional[CandidateComposition]:
        """Build a real composer plan chaining the arrangement's ops.

        The claimed outcome is COMPUTED by executing the plan through the
        real composer — never asserted. A plan that fails to execute
        yields no candidate (surfaced, never faked).
        """
        steps: List[Dict[str, Any]] = []
        params: Dict[str, str] = {}
        args: Dict[str, Any] = {}
        param_names = ["x", "y", "z", "w"]
        for i, pid in enumerate(arrangement):
            sid = f"s{i + 1}"
            if i == 0:
                pa, pb = param_names[0], param_names[1]
                params[pa] = "num"
                params[pb] = "num"
                step_args = {"a": {"$param": pa}, "b": {"$param": pb}}
                args[pa] = 2
                args[pb] = 3
            else:
                pb = param_names[min(i + 1, len(param_names) - 1)]
                params[pb] = "num"
                step_args = {"a": {"$step": steps[-1]["id"]},
                             "b": {"$param": pb}}
                args[pb] = i + 3
            steps.append({"id": sid, "op": pid, "args": step_args})
        if arg_overrides:
            for key, value in arg_overrides.items():
                if key in args and isinstance(value, (int, float)):
                    args[key] = value
        plan = {"steps": steps, "output": {"$step": steps[-1]["id"]},
                "params": params}
        try:
            result = self._composer.execute_sync(plan, args)
        except Exception:
            return None
        if not isinstance(result, dict) or not result.get("success"):
            return None
        # Held-out cases: real executions on unseen args — the
        # CorrectnessPanel refuses candidates with nothing to execute.
        held_out: List[Dict[str, Any]] = []
        for spec in ({"x": 1, "y": 1, "z": 2, "w": 5},
                     {"x": 0, "y": 5, "z": 3, "w": 2}):
            hargs = {k: spec[k] for k in params if k in spec}
            try:
                hres = self._composer.execute_sync(plan, hargs)
            except Exception:
                return None
            if not isinstance(hres, dict) or not hres.get("success"):
                return None
            held_out.append({"args": hargs, "expected": hres.get("value")})
        candidate_id = f"cand-{uuid.uuid4().hex[:8]}"
        return CandidateComposition(
            candidate_id=candidate_id,
            primitive_ids=tuple(arrangement),
            plan=plan,
            args=dict(args),
            claimed_outcome=result.get("value"),
            held_out=tuple(held_out),
            file_artifacts=(),
            addresses=ctx.intent.outcome,
            constraints=tuple(ctx.constraints),
            run_id=f"exec-{uuid.uuid4().hex[:8]}",
        )

    def _vary(self, ctx: _RunContext) -> List[Tuple[CandidateComposition,
                                                   NoveltyVerdict]]:
        """Variation along explicit dimensions (paper §2, D-5).

        Dimensions are explicit named arg-override maps. The cognition
        provider may SUGGEST dimensions (utility); every suggestion is
        validated as pure data and every variant passes through the same
        legality/novelty predicates — a malicious provider cannot inject
        an unverified primitive or bypass a check.
        """
        dimensions: List[Tuple[str, Dict[str, Any]]] = [
            ("rescale-2x", {"x": 4, "y": 6}),
            ("rescale-half", {"x": 1, "y": 1}),
        ]
        suggested = self._cognition_dimensions(ctx)
        dimensions.extend(suggested)
        varied: List[Tuple[CandidateComposition, NoveltyVerdict]] = []
        for candidate, _novelty in ctx.candidates:
            varied.append((candidate, _novelty))  # the base survives
            for dim_name, overrides in dimensions:
                if self._stop_event.is_set():
                    break
                # Validate the suggestion as pure data, fail closed.
                if (not isinstance(overrides, dict) or
                        not all(isinstance(k, str) and
                                isinstance(v, (int, float))
                                for k, v in overrides.items())):
                    continue
                variant = self._build_candidate(
                    candidate.primitive_ids, ctx, variant_of=candidate.candidate_id,
                    arg_overrides=overrides)
                if variant is None:
                    continue
                verdict = self._ledger.check_composition(
                    list(variant.primitive_ids))
                if not verdict.legal:
                    continue
                novelty = check_novelty(variant, self._provenance)
                varied.append((variant, novelty))
        varied.sort(key=lambda pair: 0 if pair[1].verdict == NOVEL else 1)
        return varied[:self._bounds.max_candidates]

    def _cognition_dimensions(
            self, ctx: _RunContext) -> List[Tuple[str, Dict[str, Any]]]:
        """Ask the cognition provider for variation dimensions (T4: utility).

        The provider's answer is UNTRUSTED DATA: its text is parsed as JSON
        looking for {"dimensions": [{"name": str, "overrides": {param: num}}]}.
        Anything unparseable or malformed is dropped fail-closed. Every
        surviving suggestion still passes through the legality/novelty
        predicates in _vary() — the provider proposes, the predicates
        dispose. Cognition is utility, never decider.
        """
        try:
            result = self._cognition.request_cognition(
                mc_id="executive-variation",
                prompt=("suggest variation dimensions as JSON: "
                        '{"dimensions": [{"name": str, "overrides": '
                        '{param: number}}]}'),
                context={"intent_outcome": ctx.intent.outcome})
        except Exception:
            return []
        suggestions: List[Tuple[str, Dict[str, Any]]] = []
        try:
            if not bool(getattr(result, "ok", False)):
                return []
            payload = json.loads(getattr(result, "text", "") or "{}")
            dims = payload.get("dimensions", [])
        except Exception:
            return []
        for dim in dims if isinstance(dims, list) else []:
            if (isinstance(dim, dict) and isinstance(dim.get("name"), str)
                    and isinstance(dim.get("overrides"), dict)
                    and all(isinstance(k, str)
                            and isinstance(v, (int, float))
                            and not isinstance(v, bool)
                            for k, v in dim["overrides"].items())):
                suggestions.append((f"cognition-{dim['name']}",
                                    dict(dim["overrides"])))
        return suggestions

    def _critique_one(self, pair: Tuple[CandidateComposition, NoveltyVerdict],
                      ctx: _RunContext) -> CritiqueReport:
        """Run the real critique pipeline on one candidate.

        Trust-boundary closure: the verdict is computed HERE by the
        executive's own bound ledger — critique_candidate's verdict
        parameter is always filled by this call, never by a caller.
        """
        candidate, _novelty = pair
        verdict = self._ledger.check_composition(list(candidate.primitive_ids))
        return critique_candidate(
            verdict=verdict,
            intent=ctx.intent,
            candidate=candidate,
            provenance_store=self._provenance,
            composer=self._composer,
            primitives=self._primitives,
            repo_root=self._repo_root)

    @staticmethod
    def _best_value_count(reports: Sequence[CritiqueReport]) -> int:
        best = 0
        for report in reports:
            try:
                count = sum(1 for c in report.value.criteria if c.passed)
            except Exception:
                count = 0
            best = max(best, count)
        return best

    def _refinement_continue(self, ctx: _RunContext) -> bool:
        """Q6: bounded turns with an adaptive stop inside the bound.

        The bound is explicit (required run() parameter). The v1 adaptive
        stop: no improvement in the best value count over the last two
        critique rounds → stop refining, release with state/evidence.
        Never loops forever.
        """
        if not refinement_loop_admissible(ctx.iterations_used,
                                          ctx.refinement_bound):
            return False
        history = ctx.value_history
        if len(history) >= 3 and history[-1] <= max(history[-3:-1]):
            return False  # adaptive stop: plateaued
        return True

    def _release_stage(self, ctx: _RunContext) -> ExecutiveOutcome:
        """RELEASE: admit the best-ranked candidate through the real
        admission contract, wired to the executive's own ledger."""
        # Rank by value: most criteria passed first.
        ranked = sorted(
            zip(ctx.candidates, ctx.reports),
            key=lambda pr: self._best_value_count([pr[1]]),
            reverse=True)
        refusals: List[str] = []
        for (candidate, _novelty), _report in ranked:
            if self._stop_event.is_set():
                return self._killed(ctx, "kill signal set at release")
            try:
                result: ReleaseResult = run_release_flow(
                    ledger=self._ledger,  # the executive's own bound ledger
                    primitive_ids=list(candidate.primitive_ids),
                    intent=ctx.intent,
                    candidate=candidate,
                    provenance_store=self._provenance,
                    composer=self._composer,
                    primitives=self._primitives,
                    repo_root=self._repo_root,
                    registry=self._registry)
            except Exception as exc:  # surfaced, never swallowed
                refusals.append(f"{candidate.candidate_id}: "
                                f"{type(exc).__name__}: {exc}")
                continue
            if result.admitted and result.admission is not None:
                return ExecutiveOutcome(
                    status=COMPLETED, stage=CreativeStage.RELEASE,
                    detail=(f"admitted {result.admission.admission_id} "
                            f"(candidate {candidate.candidate_id})"),
                    admission=result.admission,
                    gaps=tuple(ctx.gaps), absent=tuple(ctx.absent),
                    candidates_considered=len(ctx.candidates))
            refusals.append(f"{candidate.candidate_id}: "
                            f"{result.refusal_reason}")
        # Exhausted without admission: release with state/evidence or stop.
        return ExecutiveOutcome(
            status=STOPPED, stage=CreativeStage.RELEASE,
            detail=("no candidate admitted; refusals recorded: "
                    + " | ".join(refusals)),
            gaps=tuple(ctx.gaps), absent=tuple(ctx.absent),
            candidates_considered=len(ctx.candidates))


@dataclass
class _Commission:
    """The T1 commission record."""
    intent: CreativeIntent
    ambiguity: str
    refinement_bound: int = 0
    constraints: Tuple[Dict[str, Any], ...] = ()
    suspended: bool = False
    boundary: Optional[Any] = None
    suspend_detail: str = ""

    @classmethod
    def make_suspended(cls, intent: CreativeIntent, ambiguity: str,
                       boundary: Any, detail: str) -> "_Commission":
        return cls(intent=intent, ambiguity=ambiguity, suspended=True,
                   boundary=boundary, suspend_detail=detail)


# ---------------------------------------------------------------------------
# Structural self-checks (used by the battery)
# ---------------------------------------------------------------------------

def public_surface_has_no(param_names: Sequence[str]) -> List[str]:
    """Names in `param_names` that appear as parameters on the executive's
    public entry points — must be empty (trust-boundary closure)."""
    found: List[str] = []
    for fn in (CreativityExecutiveController.commission,
               CreativityExecutiveController.run,
               CreativityExecutiveController.step):
        try:
            params = inspect.signature(fn).parameters
        except (TypeError, ValueError):
            continue
        for name in param_names:
            if name in params:
                found.append(f"{fn.__name__}.{name}")
    return found
