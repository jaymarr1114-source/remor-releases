"""Loop-to-loop handoff contracts: what a loop hands off on transition.

A loop's `enter()` produces a `LoopOutcome` that previously went nowhere
(zero consumers outside this package). This module makes transitions
contractual, never ad-hoc:

  1. CLASSIFY -- `classify_terminal(loop, outcome)` maps the loop's REAL
     result type to a terminal-state taxonomy (never invented: each
     classifier reads the producing machinery's own fields).
  2. PRODUCE -- `produce_handoff(...)` looks up the declared
     HANDOFF_ROUTES table for (from_loop, terminal_state), builds the
     follow-on boundary's evidence through the route's builder (which
     fetches REAL records, never fabricates), measures resource
     accounting from the substrate's LoopViews, and returns a validated
     LoopHandoff -- or None when the table declares the outcome
     contractually terminal (a declared answer, not a silent drop).
  3. ACCEPT -- `accept_handoff(executive, handoff, context)` validates the
     handoff, verifies the (from_loop, terminal_state, to_loop) triple is
     a declared route, enforces the chain-depth guard, and constructs the
     BoundaryPresentation -- which then passes the existing
     anti-fabrication `validate()` gate. Any mismatch raises
     HandoffRefused, loudly, never silently downgraded.
  4. TRANSITION -- `transition(...)` runs produce -> accept ->
     executive.enter, the full contractual path.

Fail-loud rules (load-bearing):
  - A route that applies but cannot honestly build its evidence raises
    HandoffRefused (e.g. distillation converged but no novel-task spec in
    context: novelty is never auto-generated).
  - A handoff naming a to_loop that does not own the route's boundary
    kind is refused at accept time.
  - Chain depth beyond MAX_HANDOFF_DEPTH is refused at produce AND
    accept time. Handoffs never ping-pong forever.
  - Missing context a builder needs (fetchers, specs) is refused, not
    defaulted.

What this module does NOT do:
  - It does not change run_controller.py, the scheduler, checkpoint
    code, or any loop machinery (all consumed, never modified).
  - It does not wire production transitions (the SEAM_EXECUTIVE.md
    future seam: run-level machinery presenting boundaries instead of
    handling inline). The contract is the interface; wiring is a later
    mission's explicit boundary.
  - It does not invent loop results: every classifier reads the real
    result types (DispatchResult, DistillationResult, QuarantineDiagnosis,
    tick summary dict, acceptance record).
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Mapping, Optional, Tuple

from .boundary import (
    BOUNDARY_ACQUISITION_GAP,
    BOUNDARY_COMPLETION_CANDIDATE,
    BOUNDARY_EXECUTION_FAILURE,
    BOUNDARY_NOVEL_TASK,
    BOUNDARY_TECHNIQUE_DELTA,
    BoundaryPresentation,
)
from .checkpoint import (
    CheckpointError,
    TransitionCheckpointStore,
    extract_from_state,
    verify_against_live,
    verify_checkpoint_integrity,
)
from .executive import BOUNDARY_OWNERSHIP
from .loops import (
    LOOP_ACCEPTANCE,
    LOOP_ACQUISITION,
    LOOP_DISTILLATION,
    LOOP_EXECUTION,
    LOOP_GENERALIZATION,
    LOOP_RUN,
    LoopOutcome,
)

# ---------------------------------------------------------------------------
# Terminal-state taxonomy
# ---------------------------------------------------------------------------
# Classified FROM the real result types, never invented. Meanings:
#   converged -- the loop's boundary class converged on real machinery.
#   exhausted -- budget/time exhausted without convergence.
#   refused   -- entry or work refused (admission, routing, auth).
#   failed    -- the loop raised a real error (enter() itself raised; the
#                contract never sees it -- recorded here for completeness).
#   open      -- honest non-convergence with a named reason (the loop's
#                real behavior, e.g. an acquisition gap left OPEN naming
#                its missing piece).
#   absent    -- the loop was not entered (unowned/absent boundary).
#   candidate -- acceptance: a completed attempt is a CANDIDATE awaiting
#                verdict, never terminal (Q8's rule, enforced here too).

TERMINAL_CONVERGED = "converged"
TERMINAL_EXHAUSTED = "exhausted"
TERMINAL_REFUSED = "refused"
TERMINAL_FAILED = "failed"
TERMINAL_OPEN = "open"
TERMINAL_ABSENT = "absent"
TERMINAL_CANDIDATE = "candidate"

TERMINAL_STATES = frozenset({
    TERMINAL_CONVERGED, TERMINAL_EXHAUSTED, TERMINAL_REFUSED,
    TERMINAL_FAILED, TERMINAL_OPEN, TERMINAL_ABSENT, TERMINAL_CANDIDATE,
})

#: Chain-depth guard: a handoff chain longer than this is refused, loudly.
#: Matches the microcontroller substrate's default max_depth.
MAX_HANDOFF_DEPTH = 8

#: Contract version stamp, recorded on every handoff.
HANDOFF_CONTRACT_VERSION = "handoff-contract/v1"


class HandoffRefused(Exception):
    """The handoff contract was violated: fail loud, never silently
    downgraded. The message names the exact violation."""


# ---------------------------------------------------------------------------
# Classification: real result types -> terminal states
# ---------------------------------------------------------------------------

def classify_terminal(loop: str, outcome: LoopOutcome) -> str:
    """Classify what entering `loop` produced, reading the loop's real
    result type. Raises HandoffRefused on an unknown loop or an
    unrecognizable result shape (never guessed)."""
    if not outcome.entered:
        return TERMINAL_ABSENT
    result = outcome.result
    if loop == LOOP_RUN:
        return _classify_run(result)
    if loop == LOOP_ACQUISITION:
        return _classify_acquisition(result)
    if loop == LOOP_EXECUTION:
        return _classify_execution(result)
    if loop == LOOP_ACCEPTANCE:
        return _classify_acceptance(result)
    if loop == LOOP_DISTILLATION:
        return _classify_distillation(result)
    if loop == LOOP_GENERALIZATION:
        return _classify_generalization(result)
    raise HandoffRefused(
        f"classify_terminal: unknown loop {loop!r}: the contract does not "
        "guess terminal states")


def _classify_run(result: Any) -> str:
    # RunController.tick() returns a summary dict; per-step errors are
    # caught into summary["errors"] and the tick continues (V10-P2).
    if not isinstance(result, dict):
        raise HandoffRefused(
            f"run outcome must be the tick summary dict, got "
            f"{type(result).__name__}: not classified")
    if result.get("budget_exceeded"):
        return TERMINAL_EXHAUSTED
    if result.get("errors"):
        return TERMINAL_OPEN  # partial tick, named errors -- honest open
    return TERMINAL_CONVERGED


def _classify_acquisition(result: Any) -> str:
    # GapRegistry.dispatch -> DispatchResult(outcome: "closed" | "open").
    outcome = getattr(result, "outcome", None)
    if outcome == "closed":
        return TERMINAL_CONVERGED
    if outcome == "open":
        return TERMINAL_OPEN
    if getattr(result, "routed", True) is False:
        return TERMINAL_REFUSED
    raise HandoffRefused(
        f"acquisition outcome unrecognized (outcome={outcome!r}, "
        f"type={type(result).__name__}): not classified")


def _classify_execution(result: Any) -> str:
    # diagnose_quarantine -> QuarantineDiagnosis(verdict, quarantined).
    verdict = getattr(result, "verdict", None)
    if verdict in ("reason_cleared", "not_quarantined"):
        return TERMINAL_CONVERGED
    if getattr(result, "quarantined", False):
        return TERMINAL_OPEN  # still quarantined, reason named
    if verdict in ("reason_holds", "indeterminate", None):
        return TERMINAL_OPEN
    raise HandoffRefused(
        f"execution outcome unrecognized (verdict={verdict!r}, "
        f"type={type(result).__name__}): not classified")


def _classify_acceptance(result: Any) -> str:
    # AcceptanceLoop.present -> record with .state; COMPLETED is a
    # candidate state, never terminal (Q8).
    state = getattr(result, "state", None)
    state_name = getattr(state, "name", state)
    if state_name == "CANDIDATE" or state == "CANDIDATE":
        return TERMINAL_CANDIDATE
    raise HandoffRefused(
        f"acceptance outcome unrecognized (state={state!r}): the loop's "
        "only honest terminal is CANDIDATE; anything else is not "
        "classified")


def _classify_distillation(result: Any) -> str:
    # DistillationLoop.distill / generalize -> DistillationResult with
    # named success or named failure (never silent).
    if getattr(result, "success", False) is True:
        return TERMINAL_CONVERGED
    if getattr(result, "success", None) is False:
        return TERMINAL_OPEN  # named failure -- the loop's real behavior
    raise HandoffRefused(
        f"distillation outcome unrecognized (success="
        f"{getattr(result, 'success', 'MISSING')!r}): not classified")


def _classify_generalization(result: Any) -> str:
    # generalize_for_task returns DistillationResult (success or named
    # refusal -- refusals are recorded, never raised).
    return _classify_distillation(result)


# ---------------------------------------------------------------------------
# The handoff record: what a loop hands off on transition
# ---------------------------------------------------------------------------

@dataclass
class LoopHandoff:
    """The contractual package a loop hands off on transition.

    from_loop / to_loop: loop names; to_loop must own the follow-on
        boundary kind per BOUNDARY_OWNERSHIP (checked at accept).
    terminal_state: the classified terminal state of the producing turn.
    boundary_kind: the follow-on boundary kind this handoff carries
        evidence for (recorded at produce; accept verifies it names a
        declared route and that to_loop owns it).
    triggering_boundary_id: the boundary whose entry produced the
        outcome (lineage; handoffs never appear from nowhere).
    outcome_detail: bounded human summary of the outcome (<=500 chars).
    evidence: the REAL evidence dict for the follow-on boundary, built
        by the route's builder from real records (in-process; the
        receiving boundary's validate() gate checks it again).
    evidence_refs: inspectable id map of the real records carried
        (e.g. {"gap_record": gap_id}) -- the loggable face of evidence.
    resource_delta: measured substrate cost of the producing turn:
        {"spawned": n, "retired": n, "refused": n} from LoopView
        before/after. The contract requires measurement, not estimates.
    chain_depth: 1 for the first handoff; +1 per hop. Guarded by
        MAX_HANDOFF_DEPTH.
    """
    handoff_id: str
    from_loop: str
    to_loop: str
    terminal_state: str
    boundary_kind: str
    triggering_boundary_id: str
    outcome_detail: str = ""
    evidence: Dict[str, Any] = field(default_factory=dict)
    evidence_refs: Dict[str, str] = field(default_factory=dict)
    resource_delta: Dict[str, int] = field(default_factory=dict)
    chain_depth: int = 1
    produced_at: float = field(default_factory=time.time)
    produced_by: str = HANDOFF_CONTRACT_VERSION
    #: The durable transition checkpoint for this handoff, set by
    #: produce_handoff when a checkpoint store is in context (PLOOP-8).
    #: None when checkpointing is not engaged: the contract works
    #: without it, exactly as before.
    checkpoint_id: Optional[str] = None
    #: The verified checkpoint row, attached by accept_handoff after
    #: the checkpoint is re-read and verified against the live world.
    #: In-memory only: durability lives in the store, never here.
    verified_checkpoint: Optional[Dict[str, Any]] = field(
        default=None, repr=False)

    def validate(self) -> "LoopHandoff":
        """Validate every field. Raises HandoffRefused naming the exact
        violation -- a handoff that fails validation is never accepted."""
        if not (self.handoff_id or "").strip():
            raise HandoffRefused("handoff needs handoff_id: anonymous "
                                 "handoffs are not routable")
        if self.from_loop == self.to_loop:
            raise HandoffRefused(
                f"handoff {self.handoff_id}: from_loop == to_loop "
                f"({self.from_loop!r}): a loop does not hand off to itself")
        if self.terminal_state not in TERMINAL_STATES:
            raise HandoffRefused(
                f"handoff {self.handoff_id}: unknown terminal_state "
                f"{self.terminal_state!r} (known: {sorted(TERMINAL_STATES)})")
        if not (self.boundary_kind or "").strip():
            raise HandoffRefused(
                f"handoff {self.handoff_id}: boundary_kind is required: "
                "the handoff must name the boundary kind its evidence "
                "was built for")
        if not (self.triggering_boundary_id or "").strip():
            raise HandoffRefused(
                f"handoff {self.handoff_id}: triggering_boundary_id is "
                "required: handoffs never appear from nowhere")
        if len(self.outcome_detail) > 500:
            raise HandoffRefused(
                f"handoff {self.handoff_id}: outcome_detail is "
                f"{len(self.outcome_detail)} chars: bounded at 500, the "
                "ledger is not a log dump")
        if not isinstance(self.evidence, dict) or not self.evidence:
            raise HandoffRefused(
                f"handoff {self.handoff_id}: evidence must be a non-empty "
                "dict of real records: a handoff with nothing to hand "
                "off is not a handoff")
        if not isinstance(self.evidence_refs, dict):
            raise HandoffRefused(
                f"handoff {self.handoff_id}: evidence_refs must be a dict")
        for key in ("spawned", "retired", "refused"):
            if key not in self.resource_delta:
                raise HandoffRefused(
                    f"handoff {self.handoff_id}: resource_delta is missing "
                    f"{key!r}: the contract requires measured resource "
                    "accounting, not estimates")
            if not isinstance(self.resource_delta[key], int):
                raise HandoffRefused(
                    f"handoff {self.handoff_id}: resource_delta[{key!r}] "
                    "must be an int")
        if not isinstance(self.chain_depth, int) or self.chain_depth < 1:
            raise HandoffRefused(
                f"handoff {self.handoff_id}: chain_depth must be a "
                f"positive int, got {self.chain_depth!r}")
        if self.chain_depth > MAX_HANDOFF_DEPTH:
            raise HandoffRefused(
                f"handoff {self.handoff_id}: chain_depth "
                f"{self.chain_depth} exceeds MAX_HANDOFF_DEPTH "
                f"{MAX_HANDOFF_DEPTH}: handoffs never ping-pong forever")
        if self.produced_by != HANDOFF_CONTRACT_VERSION:
            raise HandoffRefused(
                f"handoff {self.handoff_id}: produced_by "
                f"{self.produced_by!r} is not this contract "
                f"({HANDOFF_CONTRACT_VERSION}): version-skewed handoffs "
                "are not accepted")
        if self.checkpoint_id is not None and \
                not str(self.checkpoint_id).strip():
            raise HandoffRefused(
                f"handoff {self.handoff_id}: checkpoint_id is set but "
                "empty: a checkpoint reference must name a real row")
        return self


# ---------------------------------------------------------------------------
# Follow-on routes: the declared (from_loop, terminal_state) table
# ---------------------------------------------------------------------------
# Every pair has a declared answer: a (possibly empty) ordered list of
# FollowOn. Empty list = contractually terminal: the outcome ends here BY
# CONTRACT, not by neglect. Each FollowOn names the follow-on boundary
# kind and carries a builder: (outcome, context) -> evidence dict | None.
#   - builder returns None: this route does not apply to this outcome
#     (try the next FollowOn in order).
#   - builder raises HandoffRefused: the route applies but the evidence
#     cannot honestly be built (missing context, wrong types) -- LOUD,
#     never silently skipped or downgraded.
# Context keys a builder may need (absence -> HandoffRefused, not
# defaults): "gap_fetcher" (gap_id -> GapRecord|None), "novel_goal"
# (str), "novel_examples" (list), "chain_depth" (int, default 0).

EvidenceBuilder = Callable[[LoopOutcome, Mapping[str, Any]],
                           Optional[Dict[str, Any]]]


@dataclass
class FollowOn:
    """One declared follow-on: the boundary kind produced and how to
    build its evidence honestly."""
    boundary_kind: str
    observed_by: str  # provenance recorded on the produced boundary
    build: EvidenceBuilder
    # Human reason this follow-on exists (the declared policy).
    reason: str = ""


def _tick_summary(outcome: LoopOutcome) -> Dict[str, Any]:
    summary = outcome.result
    if not isinstance(summary, dict):
        raise HandoffRefused(
            "run follow-on needs the tick summary dict: the producing "
            "turn did not leave one")
    return summary


def _build_run_execution(outcome: LoopOutcome,
                         context: Mapping[str, Any]) -> Optional[Dict[str, Any]]:
    """run -> execution_failure: the tick's quarantine sweep surfaced a
    capability STILL quarantined (the Q7 attempt did not readmit it).
    Readmitted capabilities converged; the tick's own report covers
    them. The attempt dict's real keys are read (capability_id,
    attempt_outcome) -- never assumed."""
    summary = _tick_summary(outcome)
    for attempt in summary.get("q7_attempts") or []:
        if not isinstance(attempt, dict):
            continue
        cid = attempt.get("capability_id")
        if not cid:
            continue
        # Only still-quarantined capabilities are the execution loop's
        # boundary. "readmitted" converged; an exception-shaped attempt
        # (no attempt_outcome) means the trigger failed and the
        # capability stays quarantined -- still the execution loop's
        # boundary, with the trigger error named in the tick's record.
        if attempt.get("attempt_outcome") == "readmitted":
            continue
        return {"capability_id": str(cid)}
    return None


def _build_run_acquisition(outcome: LoopOutcome,
                           context: Mapping[str, Any]) -> Optional[Dict[str, Any]]:
    """run -> acquisition_gap: the tick's gap queue surfaced an open gap.
    The REAL GapRecord is fetched through the context's gap_fetcher --
    never reconstructed, never shaped like one."""
    summary = _tick_summary(outcome)
    gap_entries = [g for g in (summary.get("gaps") or [])
                   if isinstance(g, dict) and g.get("gap_id")]
    if not gap_entries:
        return None
    fetcher = context.get("gap_fetcher")
    if fetcher is None:
        raise HandoffRefused(
            "run->acquisition needs context['gap_fetcher']: the follow-on "
            "boundary requires the REAL GapRecord; the contract will not "
            "proceed without the fetcher")
    # Prefer a gap the tick left open (honest boundary); fall back to
    # the first surfaced gap.
    ordered = sorted(
        gap_entries,
        key=lambda g: 0 if g.get("outcome") == "open" else 1)
    for entry in ordered:
        record = fetcher(entry["gap_id"])
        if record is None:
            continue
        if getattr(record, "status", None) not in ("open", "acquiring"):
            continue
        return {"gap_record": record}
    return None


def _build_distillation_generalization(
        outcome: LoopOutcome,
        context: Mapping[str, Any]) -> Optional[Dict[str, Any]]:
    """distillation -> novel_task: the distilled technique is a candidate
    for generalization -- but novelty is NEVER auto-generated, and the
    generalize inlet needs the retained plan's capability_id (read from
    the REAL result, never invented). The route applies (a real promoted
    technique exists); without an explicit novel spec in context, or
    without the capability_id the inlet needs, the builder refuses
    LOUDLY."""
    result = outcome.result
    promoted = getattr(result, "promoted_name", "") or ""
    if not promoted:
        return None  # route does not apply: nothing promoted
    capability_id = getattr(result, "capability_id", "") or ""
    if not capability_id:
        raise HandoffRefused(
            "distillation->generalization: the route applies (technique "
            f"{promoted!r} promoted) but the result carries no "
            "capability_id: the generalize inlet needs it to recover "
            "the retained plan; the follow-on boundary cannot honestly "
            "be built")
    novel_goal = context.get("novel_goal")
    novel_examples = context.get("novel_examples")
    if not (isinstance(novel_goal, str) and novel_goal.strip()):
        raise HandoffRefused(
            "distillation->generalization: the route applies (technique "
            f"{promoted!r} promoted) but context has no novel_goal: "
            "novelty is never auto-generated; the follow-on boundary "
            "cannot honestly be built")
    if not (isinstance(novel_examples, list) and novel_examples):
        raise HandoffRefused(
            "distillation->generalization: the route applies but context "
            "has no non-empty novel_examples: generalization without "
            "novel evidence is vacuous; the boundary cannot honestly "
            "be built")
    return {"distilled_ref": {"promoted_name": promoted,
                              "capability_id": capability_id,
                              "delta_observation_id":
                                  getattr(result, "delta_id", "") or ""},
            "novel_goal": novel_goal,
            "novel_examples": novel_examples}


def _terminal(reason: str) -> List[FollowOn]:
    """Declared contractual terminal: the outcome ends here BY CONTRACT."""
    return []


HANDOFF_ROUTES: Dict[Tuple[str, str], List[FollowOn]] = {
    # -- run: the tick surfaces boundaries for other loops, in declared
    #    urgency order (failure first, then work queue, then improvement).
    #    Production note: the tick currently ALSO dispatches gaps inline
    #    (V10-P2 behavior, unchanged); the future seam has the tick
    #    surface instead of inline-dispatch. Until then a surfaced gap
    #    may be re-dispatched by the acquisition inlet -- redundant but
    #    real, never faked; recorded as the production-wiring residual.
    (LOOP_RUN, TERMINAL_CONVERGED): [
        FollowOn(BOUNDARY_EXECUTION_FAILURE,
                 observed_by="handoff:run-tick-quarantine",
                 build=_build_run_execution,
                 reason="a still-quarantined capability is the most "
                        "urgent surfaced boundary"),
        FollowOn(BOUNDARY_ACQUISITION_GAP,
                 observed_by="handoff:run-tick-gap-queue",
                 build=_build_run_acquisition,
                 reason="the gap queue's open gaps are the run loop's "
                        "standing work"),
        # No run -> technique_delta: the tick's distillation sweep IS
        # distillation work on the run cadence (V10-P4) -- it distills
        # every pending delta and marks them consumed. Surfacing a
        # technique_delta boundary for a sweep-distilled delta would
        # re-distill consumed work. The distillation inlet is entered
        # directly with technique_delta boundaries (distillation
        # sessions), not via run follow-ons.
    ],
    (LOOP_RUN, TERMINAL_EXHAUSTED): _terminal(
        "budget-exhausted ticks are retried by the run controller's own "
        "backoff; the executive does not second-guess cadence"),
    (LOOP_RUN, TERMINAL_OPEN): _terminal(
        "a tick with named errors is observed by the run controller; no "
        "other loop owns 'the tick had errors'"),
    # -- acquisition: a closed gap's closing evidence stands as the
    #    record. Technique gaps already distill INLINE via the technique
    #    route as part of closing (gaps._acquire_technique); no real
    #    route carries a live DeltaRecord in closing_evidence (they are
    #    plain dicts), so there is no honest acquisition -> distillation
    #    follow-on today -- declaring one would be speculative. An OPEN
    #    gap names its missing piece; retry is the run controller's
    #    backoff, not an executive follow-on -- no auto-retry, no spin,
    #    never silently dropped.
    (LOOP_ACQUISITION, TERMINAL_CONVERGED): _terminal(
        "closed gaps stand as their own record; the technique route "
        "distills inline while closing"),
    (LOOP_ACQUISITION, TERMINAL_OPEN): _terminal(
        "an OPEN gap names its missing piece; retry is the run "
        "controller's backoff, not an executive follow-on"),
    (LOOP_ACQUISITION, TERMINAL_REFUSED): _terminal(
        "a refused dispatch is recorded; the refusal reason is the "
        "boundary, and no loop owns refusals"),
    # -- execution: the diagnosis is the loop's product. A repaired
    #    capability does NOT auto-become a completion candidate: the
    #    acceptance inlet requires Attempt + passing AuthReport, which
    #    the execution loop does not produce -- manufacturing them
    #    would be fabrication.
    (LOOP_EXECUTION, TERMINAL_CONVERGED): _terminal(
        "repaired capabilities re-enter through the normal completion "
        "path with real attempt evidence, not via executive follow-on"),
    (LOOP_EXECUTION, TERMINAL_OPEN): _terminal(
        "a still-quarantined capability with a named reason stays the "
        "execution loop's business"),
    # -- acceptance: COMPLETED is a candidate state, never terminal.
    #    The verdict is James's; nothing auto-advances a candidate.
    (LOOP_ACCEPTANCE, TERMINAL_CANDIDATE): _terminal(
        "a CANDIDATE awaits verdict; the executive never auto-advances "
        "acceptance"),
    # -- distillation: converged techniques are generalization
    #    CANDIDATES, but novelty requires an explicit spec (never
    #    auto-generated) -- the builder enforces this loudly.
    (LOOP_DISTILLATION, TERMINAL_CONVERGED): [
        FollowOn(BOUNDARY_NOVEL_TASK,
                 observed_by="handoff:distillation-promoted-technique",
                 build=_build_distillation_generalization,
                 reason="a promoted technique is a generalization "
                        "candidate once a novel task is specified"),
    ],
    (LOOP_DISTILLATION, TERMINAL_OPEN): _terminal(
        "a named distillation failure is the distillation loop's "
        "record; no other loop owns it"),
    # -- generalization: a generalized technique enters the trust path
    #    (ReviewBoard + verdict-bound promotion), which lives outside
    #    the six loops.
    (LOOP_GENERALIZATION, TERMINAL_CONVERGED): _terminal(
        "generalized techniques go to the trust path, outside the six "
        "loops"),
    (LOOP_GENERALIZATION, TERMINAL_OPEN): _terminal(
        "an honest out-of-envelope refusal is the generalization "
        "loop's record"),
}

# Every terminal state has a declared answer for every loop: anything
# missing here is a contract hole, caught by the coverage proof.
for _loop in (LOOP_RUN, LOOP_ACQUISITION, LOOP_EXECUTION, LOOP_ACCEPTANCE,
              LOOP_DISTILLATION, LOOP_GENERALIZATION):
    for _state in TERMINAL_STATES:
        HANDOFF_ROUTES.setdefault((_loop, _state), [])


def route_owner(boundary_kind: str) -> str:
    """The loop owning a boundary kind, per the executive's declared
    ownership map. Raises HandoffRefused for unowned kinds (the
    executive never guesses; neither does the contract)."""
    owner = BOUNDARY_OWNERSHIP.get(boundary_kind)
    if owner is None:
        raise HandoffRefused(
            f"no loop owns boundary kind {boundary_kind!r}: the handoff "
            "contract does not guess ownership")
    return owner


# ---------------------------------------------------------------------------
# Transition checkpoints (PLOOP-8): durable from-loop state
# ---------------------------------------------------------------------------
# Checkpointing is engaged by context: pass a TransitionCheckpointStore
# as context["checkpoint_store"], or a path as
# context["checkpoint_db_path"]. Without either, produce/accept behave
# exactly as before (the contract never required durability). With a
# store, produce_handoff checkpoints the from-loop's resumable state at
# produce time; accept_handoff re-reads the checkpoint on a fresh
# connection and verifies it against the live world before the
# transition proceeds; transition() marks it consumed once the
# receiving loop is entered. A missing, tampered, drifted, or
# already-consumed checkpoint is a loud HandoffRefused -- never a
# silent cold start.

def _resolve_checkpoint_store(
        context: Mapping[str, Any]) -> Optional[TransitionCheckpointStore]:
    """The checkpoint store for this transition, or None when
    checkpointing is not engaged."""
    store = (context or {}).get("checkpoint_store")
    if store is not None:
        if not isinstance(store, TransitionCheckpointStore):
            raise HandoffRefused(
                "context['checkpoint_store'] must be a "
                "TransitionCheckpointStore, got "
                f"{type(store).__name__}: the checkpoint store is a "
                "real durable store, never a stand-in")
        return store
    path = (context or {}).get("checkpoint_db_path")
    if path:
        return TransitionCheckpointStore(str(path))
    return None


def _checkpointed_produce(handoff: LoopHandoff,
                          outcome: LoopOutcome,
                          context: Mapping[str, Any]) -> LoopHandoff:
    """Checkpoint the from-loop's resumable state for a validated
    handoff. Returns the handoff with checkpoint_id attached."""
    store = _resolve_checkpoint_store(context)
    if store is None:
        return handoff
    try:
        from_state = extract_from_state(handoff.from_loop, outcome,
                                        context)
        checkpoint_id = store.save(handoff, from_state)
    except CheckpointError as exc:
        raise HandoffRefused(
            f"produce: checkpointing failed for handoff "
            f"{handoff.handoff_id}: {exc}")
    handoff.checkpoint_id = checkpoint_id
    return handoff.validate()


def _checkpointed_accept(handoff: LoopHandoff,
                         context: Mapping[str, Any]) -> LoopHandoff:
    """Re-read the handoff's checkpoint on a fresh connection and
    verify it against the live world. Attaches the verified row as
    handoff.verified_checkpoint. Handoffs without a checkpoint pass
    through untouched."""
    if handoff.checkpoint_id is None:
        return handoff
    store = _resolve_checkpoint_store(context)
    if store is None:
        raise HandoffRefused(
            f"accept: handoff {handoff.handoff_id} carries checkpoint "
            f"{handoff.checkpoint_id} but context has no checkpoint "
            "store: durability claims that cannot be re-verified are "
            "not accepted")
    try:
        row = store.load(handoff.handoff_id)
        if row is None:
            raise CheckpointError(
                f"checkpoint {handoff.checkpoint_id}: no row for "
                f"handoff {handoff.handoff_id}: the checkpoint is "
                "missing; refusing to resume from nothing")
        if row["checkpoint_id"] != handoff.checkpoint_id:
            raise CheckpointError(
                f"checkpoint mismatch: handoff names "
                f"{handoff.checkpoint_id} but the store holds "
                f"{row['checkpoint_id']} for handoff "
                f"{handoff.handoff_id}: refusing")
        for field in ("from_loop", "to_loop", "terminal_state",
                      "boundary_kind", "triggering_boundary_id",
                      "chain_depth"):
            if row[field] != getattr(handoff, field):
                raise CheckpointError(
                    f"checkpoint {row['checkpoint_id']}: field "
                    f"{field!r} is {row[field]!r} but the handoff "
                    f"says {getattr(handoff, field)!r}: the checkpoint "
                    "does not describe this handoff; refusing")
        if row["evidence_refs"] != dict(handoff.evidence_refs or {}):
            raise CheckpointError(
                f"checkpoint {row['checkpoint_id']}: evidence_refs "
                "drifted from the handoff's: refusing")
        verify_checkpoint_integrity(row)
        verify_against_live(row, context)
        row = store.mark_verified(row["checkpoint_id"])
    except CheckpointError as exc:
        raise HandoffRefused(
            f"accept: checkpoint verification failed for handoff "
            f"{handoff.handoff_id}: {exc}")
    handoff.verified_checkpoint = row
    return handoff


def rehydrate_handoff(checkpoint_row: Mapping[str, Any],
                      evidence: Dict[str, Any]) -> LoopHandoff:
    """Rebuild a LoopHandoff from a durable checkpoint row plus
    honestly re-fetched evidence.

    Cross-process resume: the checkpoint row is the durable source of
    truth; the caller re-fetches the REAL records the row's
    evidence_refs name (e.g. the GapRecord by gap_id) and passes them
    as evidence. The rebuilt handoff is validated like any other --
    a fabricated evidence dict dies in validate(), loudly.
    """
    row = dict(checkpoint_row)
    handoff = LoopHandoff(
        handoff_id=str(row["handoff_id"]),
        from_loop=str(row["from_loop"]),
        to_loop=str(row["to_loop"]),
        terminal_state=str(row["terminal_state"]),
        boundary_kind=str(row["boundary_kind"]),
        triggering_boundary_id=str(row["triggering_boundary_id"]),
        outcome_detail="",
        evidence=dict(evidence),
        evidence_refs={str(k): str(v)
                       for k, v in (row.get("evidence_refs") or {}).items()},
        resource_delta={str(k): int(v)
                        for k, v in (row.get("resource_delta") or {}).items()},
        chain_depth=int(row.get("chain_depth", 1)),
        produced_at=float(row.get("produced_at", 0.0) or 0.0),
        produced_by=HANDOFF_CONTRACT_VERSION,
        checkpoint_id=str(row["checkpoint_id"]),
    )
    return handoff.validate()


# ---------------------------------------------------------------------------
# Produce: outcome -> validated LoopHandoff (or None: declared terminal)
# ---------------------------------------------------------------------------

def _measure_delta(view_before: Any, view_after: Any) -> Dict[str, int]:
    """Measured resource accounting from the substrate's LoopViews
    (frozen interface: aggregates only, no internals)."""
    delta: Dict[str, int] = {}
    for key, attr in (("spawned", "total_spawned"),
                      ("retired", "total_retired"),
                      ("refused", "total_refused")):
        before = getattr(view_before, attr, 0) or 0
        after = getattr(view_after, attr, 0) or 0
        delta[key] = int(after) - int(before)
    return delta


def produce_handoff(*, outcome: LoopOutcome,
                    triggering_boundary: BoundaryPresentation,
                    context: Optional[Mapping[str, Any]] = None,
                    view_before: Any = None,
                    view_after: Any = None) -> Optional[LoopHandoff]:
    """Produce the contractual handoff for a loop's outcome.

    Returns None when the HANDOFF_ROUTES table declares the outcome
    contractually terminal (a declared answer, never a silent drop).
    Raises HandoffRefused, loudly, when the contract is violated:
    unclassifiable outcome, chain depth exceeded, or a route that
    applies but cannot honestly build its evidence.

    view_before/view_after are the substrate LoopViews for the
    producing loop, captured around its entry -- the contract REQUIRES
    measured resource accounting (both must be provided).
    """
    context = context or {}
    from_loop = outcome.loop
    terminal_state = classify_terminal(from_loop, outcome)

    chain_depth = int(context.get("chain_depth", 0) or 0) + 1
    if chain_depth > MAX_HANDOFF_DEPTH:
        raise HandoffRefused(
            f"produce: chain_depth {chain_depth} exceeds "
            f"MAX_HANDOFF_DEPTH {MAX_HANDOFF_DEPTH}: handoffs never "
            "ping-pong forever")

    routes = HANDOFF_ROUTES.get((from_loop, terminal_state), [])
    for follow in routes:
        evidence = follow.build(outcome, context)
        if evidence is None:
            continue  # route does not apply to this outcome: try next
        to_loop = route_owner(follow.boundary_kind)
        if view_before is None or view_after is None:
            raise HandoffRefused(
                "produce: view_before and view_after are required: the "
                "contract requires MEASURED resource accounting, not "
                "estimates")
        resource_delta = _measure_delta(view_before, view_after)
        evidence_refs = {
            key: str(value) for key, value in _evidence_ref_ids(
                follow.boundary_kind, evidence).items()
        }
        handoff = LoopHandoff(
            handoff_id=f"handoff_{uuid.uuid4().hex[:12]}",
            from_loop=from_loop,
            to_loop=to_loop,
            terminal_state=terminal_state,
            boundary_kind=follow.boundary_kind,
            triggering_boundary_id=triggering_boundary.boundary_id,
            outcome_detail=str(outcome.detail or "")[:500],
            evidence=evidence,
            evidence_refs=evidence_refs,
            resource_delta=resource_delta,
            chain_depth=chain_depth,
        )
        handoff.validate()
        # PLOOP-8: checkpoint the from-loop's resumable state at produce
        # time (engaged by context; without a store this is a no-op and
        # the contract behaves exactly as before).
        return _checkpointed_produce(handoff, outcome, context)
    # No route applied: the table's declared answer is terminal.
    return None


def _evidence_ref_ids(boundary_kind: str,
                      evidence: Dict[str, Any]) -> Dict[str, str]:
    """Inspectable id map for the real records carried as evidence."""
    refs: Dict[str, str] = {}
    if boundary_kind == BOUNDARY_ACQUISITION_GAP:
        rec = evidence.get("gap_record")
        refs["gap_record"] = str(getattr(rec, "gap_id", "?"))
    elif boundary_kind == BOUNDARY_TECHNIQUE_DELTA:
        rec = evidence.get("delta")
        refs["delta"] = str(getattr(rec, "delta_id", "?"))
    elif boundary_kind == BOUNDARY_EXECUTION_FAILURE:
        refs["capability_id"] = str(evidence.get("capability_id", "?"))
    elif boundary_kind == BOUNDARY_NOVEL_TASK:
        ref = evidence.get("distilled_ref") or {}
        refs["distilled_ref"] = str(ref.get("promoted_name", "?"))
        refs["novel_goal"] = str(evidence.get("novel_goal", "?"))[:80]
    elif boundary_kind == BOUNDARY_COMPLETION_CANDIDATE:
        refs["run_id"] = str(evidence.get("run_id", "?"))
    return refs


# ---------------------------------------------------------------------------
# Accept: handoff -> validated BoundaryPresentation
# ---------------------------------------------------------------------------

def accept_handoff(executive: Any, handoff: LoopHandoff,
                   context: Optional[Mapping[str, Any]] = None) -> BoundaryPresentation:
    """Accept a handoff and construct the follow-on boundary.

    Validates the handoff, verifies the (from_loop, terminal_state,
    to_loop) triple names a DECLARED route whose boundary kind is owned
    by to_loop, enforces the chain-depth guard, and constructs the
    BoundaryPresentation -- which passes the existing anti-fabrication
    validate() gate. Anything off raises HandoffRefused, loudly.
    The executive itself is only used for nothing here beyond its
    declared ownership map: acceptance is contract-level, and the
    caller then drives executive.enter(boundary).
    """
    handoff.validate()
    routes = HANDOFF_ROUTES.get((handoff.from_loop, handoff.terminal_state),
                                [])
    matched: Optional[FollowOn] = None
    for follow in routes:
        if follow.boundary_kind == handoff.boundary_kind:
            matched = follow
            break
    if matched is None:
        raise HandoffRefused(
            f"accept: ({handoff.from_loop}, {handoff.terminal_state}) -> "
            f"{handoff.boundary_kind!r} is not a declared route: the "
            "handoff names a follow-on boundary the contract never "
            "declared for this transition")
    owner = route_owner(matched.boundary_kind)
    if owner != handoff.to_loop:
        raise HandoffRefused(
            f"accept: handoff names to_loop={handoff.to_loop!r} but "
            f"{matched.boundary_kind!r} is owned by {owner!r}: the "
            "receiving loop must own the follow-on boundary")
    boundary = BoundaryPresentation(
        kind=matched.boundary_kind,
        evidence=dict(handoff.evidence),
        observed_by=f"handoff:{handoff.handoff_id}",
    )
    # The receiving gate: the existing anti-fabrication validation.
    # A handoff carrying fabricated evidence dies here, loudly.
    boundary.validate()
    # PLOOP-8: when the handoff carries a checkpoint, re-read it on a
    # fresh connection and verify it against the live world before the
    # transition proceeds. A missing, tampered, drifted, or consumed
    # checkpoint refuses loudly -- never a silent cold start.
    _checkpointed_accept(handoff, context or {})
    return boundary


# ---------------------------------------------------------------------------
# Transition: the full contractual path
# ---------------------------------------------------------------------------

def transition(*, executive: Any, outcome: LoopOutcome,
               triggering_boundary: BoundaryPresentation,
               context: Optional[Mapping[str, Any]] = None,
               view_before: Any = None,
               view_after: Any = None) -> Optional[LoopOutcome]:
    """Run the full contractual transition: produce -> accept -> enter.

    Returns the receiving loop's LoopOutcome, or None when the
    producing outcome is contractually terminal. The context for the
    next hop carries chain_depth = handoff.chain_depth (the driver
    threads it through if it continues the chain).
    """
    context = dict(context or {})
    handoff = produce_handoff(
        outcome=outcome, triggering_boundary=triggering_boundary,
        context=context, view_before=view_before, view_after=view_after)
    if handoff is None:
        return None
    boundary = accept_handoff(executive, handoff, context)
    received = executive.enter(boundary)
    # PLOOP-8: the checkpoint is single-use. Once the receiving loop
    # has been entered, the checkpoint is consumed; a second
    # transition on the same handoff is replay and is refused.
    if handoff.checkpoint_id is not None and received is not None \
            and received.entered:
        store = _resolve_checkpoint_store(context)
        if store is None:  # pragma: no cover - cannot happen: produce
            # engaged the store from this same context
            raise HandoffRefused(
                f"transition: handoff {handoff.handoff_id} carries "
                "checkpoint but the store vanished from context")
        try:
            store.mark_consumed(handoff.checkpoint_id)
        except CheckpointError as exc:
            raise HandoffRefused(
                f"transition: checkpoint consume failed for handoff "
                f"{handoff.handoff_id}: {exc}")
    return received
