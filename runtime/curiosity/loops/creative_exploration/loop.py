"""Creative Exploration loop controller (Phase 3, CUR-P3B).

The third curiosity loop: converges creative-exploration-shaped
boundaries -- a generative prompt carrying an intent-state -- through
the creative pipeline (intent -> generate -> compose -> evaluate ->
release) to a terminal state from the charter's enumerated set, with
provenance stamped on every finding.

Structural template: mirrors
``runtime/curiosity/loops/scientific_inquiry/loop.py`` (the CUR-P3A
stage pattern): loop + inlet, per-stage microcontrollers nested under
a root MC, reasoning via ``substrate.cognize()`` through the governed
inlet, ``substrate.charge()`` per step, ``SubstrateRefused`` on
refusal. The mechanical stage functions below are pure and
independently provable; the loop orchestrates them through the
substrate exactly as the questioning and inquiry loops do.

Charter grounding (all James-decided, 2026-09-30):
- The selection grammar begins with intent-state ownership: the loop
  admits a trigger only when it carries a creative commission as an
  intent-state (what is wanted, not how to make it). A missing or
  convergence-shaped intent-state is refused.
- The six elements are ordered stages of one creative process; this
  loop implements intent -> generation (with explicit variation) ->
  composition (with bounded refinement) -> evaluation (critique +
  verified-substrate check) -> release.
- Creativity uses the Acceptance panel + kill ladder (GAM does not
  attach); acceptance/kill-control/attestation stay distinct.
- C-6.4: the loop never produces an immediate belief. Every output is
  a candidate hypothesis for the Acceptance panel; the payload marks
  ``epistemic_status: "hypothesis"`` and there is no code path that
  submits to a capability-admission interface.
- The verified-factual substrate rule: compositions draw exclusively
  from the presented verified ledger (primitives with cited,
  checkable verification events). Unverified material is quarantined
  and the gap is named -- never silently assumed, never treated as
  fact. When a commission needs an unverified capability, the loop
  converges to BOUNDARY_ESTABLISHED with the gap named in the record.

Honest status (2026-10-03): the controller is complete and its stage
machinery is proven (proofs/cur_p3b/). It cannot yet execute against
the curiosity substrate -- the frozen Phase-2 chain admits exactly two
loop vocabulary entries ("questioning", "scientific_inquiry"):
``CuriositySubstrate.register_loop`` raises ``ValueError`` for
"creative_exploration", the executive's ``LOOP_OWNERSHIP`` has no
entry for the generative-prompt boundary class (``LOOP_ABSENT``), and
the run controller's registry holds only the two admitted loops.
Extending that vocabulary is a U-1-class governance decision (James)
plus authorized frozen-file integration edits (track owner). See
proofs/cur_p3b/BOUNDARY.md.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from swarm_engine.core.graph_controller.controller import GraphController
from swarm_engine.core.microcontroller.substrate import R_UNKNOWN_MC
from swarm_engine.curiosity.cognition import (
    OP_EXTRACT,
    _content_terms,
    extract_unknown,
)
from swarm_engine.curiosity.executive.boundary import (
    BOUNDARY_GENERATIVE_PROMPT,
)
from swarm_engine.curiosity.evidence.records import (
    TERMINAL_STATES,
    CuriosityFinding,
    EvidenceProvenance,
)
from swarm_engine.curiosity.evidence.writer import CuriosityWriter


#: The loop's vocabulary name. Not yet admitted by the frozen Phase-2
#: chain (see module docstring); the fenced
#: register_loop("creative_exploration") attempt raises ValueError
#: until the vocabulary is extended by a U-1-class decision.
LOOP_CREATIVE_EXPLORATION = "creative_exploration"

#: Boundary classes this loop owns (C-6.1). The inlet and
#: new_exploration refuse any other boundary class with LoopRefused --
#: the loop never claims another loop's boundary, even if the
#: executive misroutes.
CREATIVE_BOUNDARIES = (BOUNDARY_GENERATIVE_PROMPT,)

#: Model/provider stamp for provenance (mechanical composition; no
#: external cognition provider -- same honesty note as the questioning
#: and inquiry loops).
MODEL_ID = ("curiosity-creative-exploration/v1 "
            "(mechanical composition; no external cognition provider)")

#: Evidence terminal states (frozen charter vocabulary) this loop
#: emits. CANDIDATE_GENERATED is the loop's convergence: candidates
#: released to the Acceptance panel as hypotheses -- never beliefs.
TERMINAL_RELEASED = "CANDIDATE_GENERATED"
TERMINAL_INSUFFICIENT = "INSUFFICIENT_EVIDENCE"
TERMINAL_INCONCLUSIVE = "INCONCLUSIVE"
TERMINAL_GAP = "BOUNDARY_ESTABLISHED"

assert TERMINAL_RELEASED in TERMINAL_STATES
assert TERMINAL_INSUFFICIENT in TERMINAL_STATES
assert TERMINAL_INCONCLUSIVE in TERMINAL_STATES
assert TERMINAL_GAP in TERMINAL_STATES

#: Executive outcome codes this loop's convergence maps to. The
#: lifecycle terminals (run-controller/enforcement-driven) are NOT
#: loop-converged: RESOURCE_BOUNDARY arrives via SubstrateRefused,
#: LOOP_SUSPENDED/KILLED via the run controller's abort path.
OUTCOME_CONVERGED = "CANDIDATE_RELEASED"
OUTCOME_GAP = "GAP_ACQUISITION"
OUTCOME_UNREACHABLE = "BOUNDARY_UNREACHABLE"

#: Triage advisories. "candidate_for_acceptance" routes the finding to
#: the Acceptance panel as a hypothesis -- it is advisory, never an
#: admission verdict (C-6.4).
TRIAGE_CANDIDATE = "candidate_for_acceptance"
TRIAGE_INVESTIGATE = "propose_investigation"
TRIAGE_BOUNDARY = "boundary"


class SubstrateRefused(Exception):
    """The substrate refused a spawn or exhausted a microcontroller
    mid-step. The run controller converts this into the governed
    RESOURCE_BOUNDARY path -- it is never swallowed here."""

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(f"{reason}: {message}")
        self.reason = reason
        self.message = message


class LoopRefused(Exception):
    """The loop refused a trigger: the boundary class is not one this
    loop owns (C-6.1 -- no fixed pipeline, loops refuse foreign
    boundaries even if misrouted), or the intent-state is missing or
    not a creative commission."""


# ---------------------------------------------------------------------------
# creative graph shape
# ---------------------------------------------------------------------------

NODE_INTENT = "intent"
NODE_GENERATE = "generate"
NODE_COMPOSE = "compose"
NODE_EVALUATE = "evaluate"
NODE_RELEASE = "release"

CREATIVE_NODES = (
    NODE_INTENT, NODE_GENERATE, NODE_COMPOSE, NODE_EVALUATE, NODE_RELEASE)

NODE_DEPENDENCIES = {
    NODE_INTENT: (),
    NODE_GENERATE: (NODE_INTENT,),
    NODE_COMPOSE: (NODE_GENERATE,),
    NODE_EVALUATE: (NODE_COMPOSE,),
    NODE_RELEASE: (NODE_EVALUATE,),
}

#: Node goals, worded so the GraphController's relevance selection
#: admits the creative chain for a creative objective (dependency
#: closure pulls the whole chain once the head matches). Each goal
#: shares the objective's literal vocabulary ("generation", not
#: "generate"; "composition", not "composed" -- the controller's
#: stemmer does not conflate them) at or above the controller's
#: relevance floor (verified: all five >= 0.70 vs 0.34).
NODE_GOALS = {
    NODE_INTENT: ("explore the creative commission through intent-owned "
                  "assessment for candidate release"),
    NODE_GENERATE: ("generation of creative commission candidates "
                    "through intent-owned explore"),
    NODE_COMPOSE: ("composition of generated candidates through "
                   "creative commission intent-owned release"),
    NODE_EVALUATE: ("evaluation of composed candidates through "
                    "creative commission intent-owned candidate release"),
    NODE_RELEASE: ("release the evaluated candidates through creative "
                   "commission intent-owned candidate exploration"),
}

CREATIVE_OBJECTIVE = ("explore the creative commission through "
                      "intent-owned generation composition and "
                      "evaluation to candidate release")


class CreativeGraph:
    """The structural creative graph, adapted to the GraphController's
    StructuralGraph protocol. The controller never touches graph
    internals; the loop never bypasses the controller."""

    graph_id = "curiosity/creative_exploration/pipeline/v1"

    def node_ids(self) -> List[str]:
        return list(CREATIVE_NODES)

    def node_goal(self, node_id: str) -> str:
        return NODE_GOALS[node_id]

    def dependencies(self, node_id: str) -> List[str]:
        return list(NODE_DEPENDENCIES[node_id])

# ---------------------------------------------------------------------------
# pure stage machinery: intent -> generate -> compose -> evaluate ->
# release. Every function is pure and independently provable; the loop
# orchestrates them through the substrate exactly as the questioning
# and inquiry loops do.
# ---------------------------------------------------------------------------

#: Commission markers: the intent-state owns this loop's pipeline when
#: the commission names something to MAKE (a creative commission as an
#: outcome). Mirrors the James<->Felix intent register: what is wanted,
#: not how to make it.
_COMMISSION_MARKERS = (
    "make", "design", "compose", "create", "write", "generate",
    "invent", "draft", "build", "produce", "craft", "draw",
    "paint", "song", "story", "logo", "image", "video", "music",
    "melody", "poem", "poster", "mural", "sculpture", "game",
    "app", "website", "brand", "mural",
)

#: Convergence markers: an intent-state shaped like a boundary to
#: CONVERGE (resolve/prove/verify) is a foreign intent -- it belongs
#: to the questioning or inquiry loops, never to creative exploration.
_CONVERGENCE_MARKERS = (
    "resolve whether", "prove that", "verify that",
    "determine whether", "investigate whether", "test whether",
    "find out whether", "establish whether", "check whether",
    "is it true that", "does the",
)

#: Explicit variation dimensions (paper §2): variation is searched,
#: not hoped for. Each dimension carries a small mechanical value set;
#: generation walks the cross product (bounded).
VARIATION_DIMENSIONS: Dict[str, Tuple[str, ...]] = {
    "style": ("minimalist", "vintage", "playful", "bold"),
    "structure": ("emblem", "wordmark", "badge", "scene"),
    "medium": ("vector", "print", "audio", "text"),
}

#: Bounded refinement (James decision §8.6): bounded turns with an
#: adaptive stop inside the bound -- never indefinite refinement.
MAX_REFINEMENT_TURNS = 3


@dataclass
class IntentState:
    """The assessed intent-state: the commission as a wanted outcome."""
    commission: str
    intent_terms: List[str]
    owned: bool
    reason: str


def assess_intent(*, commission: str,
                  bounded_objective: str) -> IntentState:
    """Assess the intent-state: the selection grammar begins here.

    Owned (True) exactly when the commission names something to make
    (a creative commission as an outcome). Refused (False) when the
    commission is missing/empty (no intent-state to own) or when it is
    convergence-shaped (a foreign intent belonging to the questioning
    or inquiry loops). The check is mechanical, never asserted.
    """
    text = (commission or "").strip()
    lowered = text.lower()
    terms = _content_terms(text) if text else []
    if not text:
        return IntentState(
            commission="", intent_terms=[], owned=False,
            reason=("no intent-state: the commission is empty -- the "
                    "loop cannot own an exploration with nothing wanted"))
    if any(marker in lowered for marker in _CONVERGENCE_MARKERS):
        return IntentState(
            commission=text, intent_terms=terms, owned=False,
            reason=("foreign intent-state: the commission is "
                    "convergence-shaped (resolve/prove/verify) -- it "
                    "belongs to the questioning or inquiry loops, not "
                    "to creative exploration"))
    if not any(marker in lowered for marker in _COMMISSION_MARKERS):
        return IntentState(
            commission=text, intent_terms=terms, owned=False,
            reason=("no creative commission: the text names nothing to "
                    "make -- the loop owns generative commissions only"))
    return IntentState(
        commission=text, intent_terms=terms, owned=True,
        reason=("creative commission owned: the intent-state names a "
                "wanted outcome"))


@dataclass
class Candidate:
    """One generated candidate: a mechanical variation setting plus
    the sketch it produces. ``lineage`` records exactly which
    dimension values produced it -- variation is searched, and the
    search is inspectable."""
    candidate_id: str
    dimension_settings: Dict[str, str]
    sketch: str
    lineage: str


def generate_candidates(*, intent_terms: List[str],
                        max_candidates: int = 6) -> List[Candidate]:
    """Generate candidates along the explicit variation dimensions.

    Mechanical: walks the bounded cross product of dimension values,
    pairing each setting with the intent's terms into a sketch. No
    external model, no hidden sampling -- the same inputs always
    produce the same candidates.
    """
    dims = list(VARIATION_DIMENSIONS.items())
    combos: List[Dict[str, str]] = [{}]
    for name, values in dims:
        combos = [{**c, name: v} for c in combos for v in values]
    terms = [t for t in intent_terms if t not in ("the", "a", "an")]
    core = " ".join(terms[:6]) or "untitled commission"
    out: List[Candidate] = []
    for i, settings in enumerate(combos[:max_candidates]):
        desc = ", ".join(f"{k}={v}" for k, v in settings.items())
        sketch = (f"[{desc}] {core}")
        out.append(Candidate(
            candidate_id=f"cand_{i:02d}",
            dimension_settings=dict(settings),
            sketch=sketch,
            lineage=f"variation search over {desc}"))
    return out


@dataclass
class VerifiedPrimitive:
    """One entry of the verified ledger: a primitive the loop may
    compose, carrying its verification event and gate reference. The
    battery checks every cited commit exists -- the ledger is
    checkable, never decorative."""
    primitive_id: str
    verification_event: str
    gate_reference: str


@dataclass
class ComposedCandidate:
    """A candidate after composition: refined through bounded turns
    using only verified primitives. ``applied`` names the primitives
    used; ``gaps`` names capabilities the commission needed that no
    ledger entry provides -- the named gap, never a silent fill."""
    candidate: Candidate
    applied: List[str]
    gaps: List[str]
    turns: int
    refined_sketch: str


def compose_candidates(*, candidates: List[Candidate],
                       verified_ledger: List[VerifiedPrimitive],
                       intent_terms: List[str]) -> List[ComposedCandidate]:
    """Compose candidates through bounded refinement (James §8.6).

    Each turn applies verified primitives whose ids share a term with
    the candidate's dimensions (mechanical relevance); the turn stops
    adapting when no new primitive applies (adaptive stop inside the
    bound). Primitives never come from outside the ledger -- a
    commission needing an unlisted capability records the gap instead
    of filling it.
    """
    ledger_ids = [p.primitive_id for p in verified_ledger]
    out: List[ComposedCandidate] = []
    for cand in candidates:
        applied: List[str] = []
        sketch = cand.sketch
        dim_terms = set()
        for v in cand.dimension_settings.values():
            dim_terms.update(_content_terms(v))
        dim_terms.update(t.lower() for t in intent_terms)
        for turn in range(1, MAX_REFINEMENT_TURNS + 1):
            newly: List[str] = []
            for pid in ledger_ids:
                if pid in applied:
                    continue
                pid_terms = set(_content_terms(
                    pid.replace("-", " ").replace("_", " ")))
                if pid_terms & dim_terms:
                    newly.append(pid)
            if not newly:
                break  # adaptive stop: nothing left to apply
            applied.extend(newly)
            sketch = f"{sketch} +[{', '.join(newly)}]"
        # Gaps: intent terms no ledger primitive covers.
        covered = set()
        for pid in ledger_ids:
            covered.update(_content_terms(
                pid.replace("-", " ").replace("_", " ")))
        gaps = [t for t in intent_terms
                if t.lower() not in covered and len(t) > 3]
        out.append(ComposedCandidate(
            candidate=cand, applied=applied, gaps=gaps,
            turns=min(turn + 1, MAX_REFINEMENT_TURNS)
            if applied else 0,
            refined_sketch=sketch))
    return out


@dataclass
class CandidateVerdict:
    """The evaluation verdict for one composed candidate."""
    candidate_id: str
    verdict: str  # ADMIT | QUARANTINE | GAP
    score: float
    reasons: List[str]
    quarantined_primitives: List[str]


@dataclass
class Evaluation:
    """The mandatory investigation/verification path's result: every
    candidate passed through evaluation before any release."""
    verdicts: List[CandidateVerdict]
    evaluated: int


def evaluate_candidates(*, composed: List[ComposedCandidate],
                        verified_ledger: List[VerifiedPrimitive],
                        intent_terms: List[str],
                        prior_art: Tuple[str, ...] = ()) -> Evaluation:
    """The mandatory investigation/verification path (plan 3b).

    Every candidate is investigated before release:
    - verified-substrate check: every applied primitive must be in the
      ledger; an applied primitive with no ledger entry is quarantined
      (it cannot happen through compose_candidates -- the check is
      defense in depth, and the battery attacks it directly).
    - novelty: the sketch must not duplicate presented prior art
      (mechanical term-overlap against the prior-art list).
    - value: the sketch must cover the intent's terms (mechanical
      coverage fraction).
    - honesty: gaps are named, never filled; a candidate whose gaps
      are load-bearing for the commission converges to GAP.
    A candidate is ADMITted only when all four pass.
    """
    ledger_ids = {p.primitive_id for p in verified_ledger}
    intent_set = {t.lower() for t in intent_terms}
    prior_sets = [set(_content_terms(p.lower())) for p in prior_art]
    verdicts: List[CandidateVerdict] = []
    for cc in composed:
        reasons: List[str] = []
        quarantined: List[str] = []
        # 1. verified-substrate check (defense in depth).
        for pid in cc.applied:
            if pid not in ledger_ids:
                quarantined.append(pid)
                reasons.append(
                    f"unverified primitive {pid!r} applied -- quarantined")
        # 2. novelty vs prior art.
        sketch_terms = set(_content_terms(cc.refined_sketch.lower()))
        for i, prior in enumerate(prior_sets):
            if prior and len(sketch_terms & prior) / len(prior) >= 0.8:
                reasons.append(
                    f"duplicates prior art entry {i} -- not novel")
        # 3. value: intent coverage.
        coverage = (len(sketch_terms & intent_set) / len(intent_set)
                    if intent_set else 0.0)
        if coverage < 0.5:
            reasons.append(
                f"intent coverage {coverage:.2f} < 0.50 -- does not "
                f"serve the commission")
        # 4. honesty: load-bearing gaps.
        load_bearing = [g for g in cc.gaps
                        if g.lower() in intent_set]
        if quarantined:
            verdict = "QUARANTINE"
        elif load_bearing:
            verdict = "GAP"
            reasons.append(
                "needs unverified capability "
                f"{load_bearing[0]!r} -- named gap, routed to Acquisition")
        elif reasons:
            verdict = "QUARANTINE"
        else:
            verdict = "ADMIT"
            reasons.append(
                f"all checks pass (coverage {coverage:.2f})")
        score = coverage if verdict == "ADMIT" else 0.0
        verdicts.append(CandidateVerdict(
            candidate_id=cc.candidate.candidate_id, verdict=verdict,
            score=score, reasons=reasons,
            quarantined_primitives=quarantined))
    return Evaluation(verdicts=verdicts, evaluated=len(composed))


def release_decision(*, evaluation: Evaluation,
                     evaluated: bool) -> Tuple[str, str, str, str]:
    """Converge the pipeline to (terminal_state, outcome, triage,
    detail). Release is structurally gated on evaluation: an
    unevaluated pipeline cannot release -- the mandatory
    investigation/verification path cannot be bypassed.
    """
    if not evaluated:
        raise LoopRefused(
            "release without evaluation: the mandatory "
            "investigation/verification path cannot be bypassed")
    admitted = [v for v in evaluation.verdicts if v.verdict == "ADMIT"]
    gaps = [v for v in evaluation.verdicts if v.verdict == "GAP"]
    if admitted:
        return (TERMINAL_RELEASED, OUTCOME_CONVERGED, TRIAGE_CANDIDATE,
                f"{len(admitted)} candidate(s) released to the Acceptance "
                f"panel as hypotheses")
    if gaps:
        named = gaps[0].reasons[-1] if gaps[0].reasons else "unnamed gap"
        return (TERMINAL_GAP, OUTCOME_UNREACHABLE, TRIAGE_BOUNDARY,
                f"commission needs an unverified capability: {named}")
    if not evaluation.verdicts:
        return (TERMINAL_INSUFFICIENT, OUTCOME_GAP, TRIAGE_INVESTIGATE,
                "no candidates: the intent-state was too vague to "
                "generate from")
    return (TERMINAL_INCONCLUSIVE, OUTCOME_GAP, TRIAGE_INVESTIGATE,
            "evaluation deadlocked: no candidate admitted, no single "
            "named gap -- propose investigation")

# ---------------------------------------------------------------------------
# provenance + findings
# ---------------------------------------------------------------------------

def stamp_provenance(*, bounded_objective: str,
                     triage: str) -> EvidenceProvenance:
    """Provenance for every creative-exploration finding (C-2.2:
    complete or the fenced store refuses the record)."""
    return EvidenceProvenance(
        loop=LOOP_CREATIVE_EXPLORATION,
        bounded_objective=bounded_objective,
        model=MODEL_ID,
        triage=triage,
    )


def build_finding(*, exploration_id: str, trigger: Dict[str, Any],
                  terminal_state: str, outcome: str, triage: str,
                  candidates: List[CandidateVerdict], detail: str,
                  payload_ref: str) -> CuriosityFinding:
    """Build the fenced-store finding for a converged exploration.

    C-6.4 is structural here: the finding carries candidates as
    hypotheses for the Acceptance panel -- ``epistemic_status`` in the
    payload is always "hypothesis", and there is no code path in this
    module that submits to a capability-admission interface. Always
    carries complete provenance; validate() passes or the caller has a
    bug (the store refuses incomplete records -- C-2.2).
    """
    return CuriosityFinding(
        evidence_id="ev_" + exploration_id.replace("exp_", ""),
        loop=LOOP_CREATIVE_EXPLORATION,
        bounded_objective=trigger["bounded_objective"],
        origin=trigger["origin"],
        terminal_state=terminal_state,
        provenance=stamp_provenance(
            bounded_objective=trigger["bounded_objective"], triage=triage),
        payload_ref=payload_ref,
    )


def persist_finding(store: Any, finding: CuriosityFinding) -> CuriosityFinding:
    """The loop's write path into the fenced Evidence Store. Defined in
    this curiosity-domain module so the writer's domain fence admits
    it; the proof battery calls this (never the writer directly)."""
    writer = CuriosityWriter(store)
    return writer.submit(finding)


def finding_payload(*, exploration_id: str, terminal_state: str,
                    candidates: List[CandidateVerdict],
                    gaps: List[str], detail: str) -> Dict[str, Any]:
    """The finding's payload body (persisted by the battery, referenced
    by payload_ref). ``epistemic_status`` is ALWAYS "hypothesis": the
    loop's outputs are candidates for the Acceptance panel, never
    beliefs (C-6.4)."""
    return {
        "exploration_id": exploration_id,
        "loop": LOOP_CREATIVE_EXPLORATION,
        "epistemic_status": "hypothesis",
        "terminal_state": terminal_state,
        "candidates": [
            {"candidate_id": c.candidate_id, "verdict": c.verdict,
             "score": c.score, "reasons": c.reasons,
             "quarantined_primitives": c.quarantined_primitives}
            for c in candidates
        ],
        "named_gaps": gaps,
        "detail": detail,
        "admission_path": None,  # structural: no admission interface exists
    }


# ---------------------------------------------------------------------------
# loop state + inlet + controller
# ---------------------------------------------------------------------------

@dataclass
class StepResult:
    done: bool
    terminal: Optional[Dict[str, Any]] = None
    detail: str = ""


@dataclass
class LoopContext:
    """What one loop step may touch. The loop never sees the executive;
    the executive never sees through this. ``prior_art`` carries the
    presented prior-art entries the novelty check runs against;
    ``verified_ledger`` is the presented verified set the loop may
    compose from."""
    substrate: Any
    exploration_id: str
    budget_slice_s: float
    verified_ledger: List[VerifiedPrimitive] = field(default_factory=list)
    prior_art: Tuple[str, ...] = ()


class CreativeExplorationLoopInlet:
    """The creative-exploration loop's entry point. enter() validates
    the trigger shape (C-6.1: foreign boundary classes are refused
    HERE, never claimed; the intent-state must be a creative
    commission) and returns the loop's initial private state; the run
    controller drives it from there."""

    def __init__(self, loop: "CreativeExplorationLoop") -> None:
        self._loop = loop

    def enter(self, trigger: Any, ctx: LoopContext) -> Dict[str, Any]:
        return self._loop.new_exploration(trigger, ctx)


class CreativeExplorationLoop:
    """The logical creative-exploration loop controller (level 2).
    Owns the creative graph and the per-stage microcontrollers;
    exposes only step()/abort()/restore() and loop-level aggregates."""

    loop_name = LOOP_CREATIVE_EXPLORATION

    # -- lifecycle --------------------------------------------------------

    def new_exploration(self, trigger: Any, ctx: LoopContext) -> Dict[str, Any]:
        # C-6.1 defense in depth: the loop itself refuses a foreign
        # boundary even if the inlet check is ever bypassed.
        boundary = trigger.boundary_class
        if boundary not in CREATIVE_BOUNDARIES:
            raise LoopRefused(
                f"creative_exploration does not own boundary class "
                f"{boundary!r}; owned: {list(CREATIVE_BOUNDARIES)}")
        # Intent-state ownership: the selection grammar begins here.
        intent = assess_intent(
            commission=trigger.question_text,
            bounded_objective=trigger.bounded_objective)
        if not intent.owned:
            raise LoopRefused(
                f"creative_exploration refuses the trigger: {intent.reason}")
        return {
            "exploration_id": ctx.exploration_id,
            "trigger": trigger.as_dict(),
            "boundary_class": boundary,
            "stage": NODE_INTENT,
            "intent": intent,
            "candidates": None,
            "composed": None,
            "evaluation": None,
            "evaluated": False,
            "node_summaries": {},
            "region_id": None,
            "root_mc": None,
            "stage_mc": None,
            "status": "running",
        }

    def restore(self, saved: Dict[str, Any]) -> Dict[str, Any]:
        """Rebuild loop state from a verified checkpoint. The graph
        region is per-exploration and rebuilt; completed stage
        summaries are kept as history and the pipeline resumes at the
        saved stage.

        The checkpoint store round-trips loop_state through JSON with
        default=str, so the IntentState dataclass does not survive as
        an object (it degrades to its str() form). restore() rebuilds
        it: assess_intent is a pure function of the trigger's
        commission and bounded objective -- both preserved verbatim in
        the checkpoint -- so re-derivation is faithful, not
        approximate. (Integration-exposed defect, CUR-P3B-INT: the
        stage battery never checkpointed, so only the kill/resume
        path exposed it.)
        """
        state = dict(saved)
        state["region_id"] = None
        state["root_mc"] = None
        state["stage_mc"] = None
        state["status"] = "running"
        if not isinstance(state.get("intent"), IntentState):
            trig = state.get("trigger") or {}
            state["intent"] = assess_intent(
                commission=trig.get("question_text", ""),
                bounded_objective=trig.get("bounded_objective", ""))
        return state

    def abort(self, state: Dict[str, Any], ctx: LoopContext,
              graph: GraphController) -> None:
        """Kill path: close the open region, retire the exploration's
        microcontrollers (children cascade automatically). No graph work
        happens after this. retire() returns a Refusal value for unknown
        ids -- never raises -- so this is idempotent."""
        sub = ctx.substrate
        if state.get("region_id"):
            graph.close_region(state["region_id"])
            state["region_id"] = None
        for mc_id in (state.get("stage_mc"), state.get("root_mc")):
            if mc_id:
                sub.retire(mc_id)
        state["root_mc"] = state["stage_mc"] = None
        state["status"] = "aborted"

    # -- the step ---------------------------------------------------------

    def step(self, state: Dict[str, Any], ctx: LoopContext,
             graph: GraphController) -> StepResult:
        """Advance one graph-node operation. Returns done=True with a
        terminal dict when the exploration converged. Requires a
        substrate that admits this loop's vocabulary -- at stage level
        the frozen chain refuses, so this path is written to the
        template and labeled NOT VERIFIED THIS SHIFT (mandate 5)."""
        if state["status"] != "running":
            return StepResult(done=True, terminal=state.get("terminal"),
                              detail="loop not running")

        sub = ctx.substrate
        if state["root_mc"] is None:
            # Reservation budget mirrors the questioning/inquiry loops':
            # root 1/2 + stage 1/4 of the slice; stage MCs retire after
            # use.
            spawned = sub.spawn(
                LOOP_CREATIVE_EXPLORATION,
                purpose=f"exploration:{ctx.exploration_id}",
                budget_s=ctx.budget_slice_s / 2.0)
            if not spawned.ok:
                raise SubstrateRefused(spawned.refusal.reason,
                                      spawned.refusal.message)
            state["root_mc"] = spawned.mc.mc_id

        if state["region_id"] is None:
            self._open_exploration(state, ctx, graph)

        region_id = state["region_id"]
        nxt = graph.next_operable(region_id)
        if not nxt.ok:
            return self._finish(state, ctx, graph,
                                blocked=nxt.status)

        node_id = nxt.node_id
        t0 = time.monotonic()
        value = self._operate_node(node_id, state, ctx, sub)
        elapsed = time.monotonic() - t0
        active_mc = state["stage_mc"] or state["root_mc"]
        exhausted, charge_state = sub.charge(active_mc, elapsed)
        if charge_state == R_UNKNOWN_MC:
            pass  # retired mid-step; the run controller still accounts time
        elif exhausted:
            raise SubstrateRefused(
                "microcontroller_exhausted",
                f"stage microcontroller exhausted charging {elapsed:.4f}s "
                f"(state={charge_state})")
        graph.record_result(region_id, node_id, success=True,
                            value={"node": node_id,
                                   "summary": value.get("summary", "")})

        if node_id == NODE_RELEASE:
            return self._finish(state, ctx, graph, blocked=None)
        return StepResult(done=False, detail=f"operated {node_id}")

    # -- exploration mechanics --------------------------------------------

    def _open_exploration(self, state: Dict[str, Any], ctx: LoopContext,
                          graph: GraphController) -> None:
        sub = ctx.substrate
        spawned = sub.spawn(
            LOOP_CREATIVE_EXPLORATION, purpose="exploration-pipeline",
            parent_id=state["root_mc"],
            budget_s=ctx.budget_slice_s / 4.0)
        if not spawned.ok:
            raise SubstrateRefused(spawned.refusal.reason,
                                  spawned.refusal.message)
        state["stage_mc"] = spawned.mc.mc_id
        graph.attach_graph(CreativeGraph())
        opened = graph.open_region(
            state["stage_mc"], CreativeGraph.graph_id,
            f"{CREATIVE_OBJECTIVE} (exploration {state['exploration_id']})",
            max_operations=16)
        if not opened.ok or not opened.region_id:
            raise SubstrateRefused(
                "region_open_refused",
                (opened.refusal.message if opened.refusal
                 else "no region id"))
        state["region_id"] = opened.region_id

    def _operate_node(self, node_id: str, state: Dict[str, Any],
                      ctx: LoopContext, sub: Any) -> Dict[str, Any]:
        trigger = state["trigger"]
        summaries = state["node_summaries"]
        intent: IntentState = state["intent"]
        if node_id == NODE_INTENT:
            summaries[node_id] = (
                f"intent owned: {intent.reason} "
                f"({len(intent.intent_terms)} terms)")
        elif node_id == NODE_GENERATE:
            candidates = generate_candidates(
                intent_terms=intent.intent_terms)
            state["candidates"] = candidates
            summaries[node_id] = (
                f"generated {len(candidates)} candidates along "
                f"{len(VARIATION_DIMENSIONS)} dimensions")
        elif node_id == NODE_COMPOSE:
            composed = compose_candidates(
                candidates=state["candidates"],
                verified_ledger=ctx.verified_ledger,
                intent_terms=intent.intent_terms)
            state["composed"] = composed
            turns = max((c.turns for c in composed), default=0)
            summaries[node_id] = (
                f"composed {len(composed)} candidates "
                f"(max {turns} refinement turns, bound "
                f"{MAX_REFINEMENT_TURNS})")
        elif node_id == NODE_EVALUATE:
            evaluation = evaluate_candidates(
                composed=state["composed"],
                verified_ledger=ctx.verified_ledger,
                intent_terms=intent.intent_terms,
                prior_art=ctx.prior_art)
            state["evaluation"] = evaluation
            state["evaluated"] = True
            admitted = sum(1 for v in evaluation.verdicts
                           if v.verdict == "ADMIT")
            summaries[node_id] = (
                f"evaluated {evaluation.evaluated}: {admitted} admitted")
        elif node_id == NODE_RELEASE:
            summaries[node_id] = "releasing"
        else:  # pragma: no cover - graph cannot yield unknown nodes
            raise AssertionError(f"unknown creative node {node_id!r}")
        return {"summary": summaries[node_id]}

    def _finish(self, state: Dict[str, Any], ctx: LoopContext,
                graph: GraphController,
                blocked: Optional[str]) -> StepResult:
        region_id = state["region_id"]
        region_summary = None
        if region_id:
            from swarm_engine.core.graph_controller.controller import (
                RegionSummary)
            closed = graph.close_region(region_id)
            if isinstance(closed, RegionSummary):
                region_summary = closed.as_dict()
            state["region_id"] = None
        if state.get("stage_mc"):
            ctx.substrate.retire(state["stage_mc"])
            state["stage_mc"] = None

        evaluation = state.get("evaluation")
        if evaluation is None or blocked:
            terminal_state, outcome, triage = (
                TERMINAL_INSUFFICIENT, OUTCOME_GAP, TRIAGE_INVESTIGATE)
            detail = (f"exploration blocked ({blocked}): the pipeline "
                      f"could not complete")
            verdicts: List[CandidateVerdict] = []
        else:
            terminal_state, outcome, triage, detail = release_decision(
                evaluation=evaluation, evaluated=state["evaluated"])
            verdicts = evaluation.verdicts
        terminal = self._terminal(
            terminal_state, outcome, triage, state, verdicts, detail=detail)
        terminal["region_summary"] = region_summary
        state["terminal"] = terminal
        state["status"] = "converged"
        return StepResult(done=True, terminal=terminal,
                          detail=terminal["terminal_state"])

    def _terminal(self, terminal_state: str, outcome: str, triage: str,
                  state: Dict[str, Any],
                  verdicts: List[CandidateVerdict],
                  detail: str) -> Dict[str, Any]:
        return {
            "terminal_state": terminal_state,
            "outcome": outcome,  # executive OutcomeCode this maps to
            "triage": triage,
            "exploration_id": state["exploration_id"],
            "trigger_id": state["trigger"]["trigger_id"],
            "bounded_objective": state["trigger"]["bounded_objective"],
            "origin": state["trigger"]["origin"],
            "boundary_class": state["boundary_class"],
            "epistemic_status": "hypothesis",  # C-6.4: never a belief
            "candidates": [
                {"candidate_id": v.candidate_id, "verdict": v.verdict,
                 "score": v.score}
                for v in verdicts
            ],
            "provenance": stamp_provenance(
                bounded_objective=state["trigger"]["bounded_objective"],
                triage=triage).as_dict(),
            "node_summaries": dict(state["node_summaries"]),
            "detail": detail,
        }

    # -- aggregates (loop-level only) --------------------------------------

    def loop_aggregate(self) -> Dict[str, Any]:
        return {"loop": self.loop_name, "nodes": list(CREATIVE_NODES),
                "boundaries": list(CREATIVE_BOUNDARIES)}
