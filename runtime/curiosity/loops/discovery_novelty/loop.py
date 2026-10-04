"""Discovery/Novelty loop controller (Phase 3, CUR-P3C).

The fourth curiosity loop: converges discovery/novelty-shaped
boundaries -- a detected pattern in adjacent space whose
classification is not yet established -- through the discovery
pipeline (explore -> detect -> investigate -> verify -> classify) to
a terminal state from the charter's enumerated set, with provenance
stamped on every finding.

Theory grounding
(``architecture/theoretical-curiosity-executive-controller_2026-09-28.md``
§7, Discovery / Novelty Controller):
    KNOWN ENVELOPE -> EXPLORE ADJACENT SPACE -> OBSERVE -> DETECT
    NOVELTY -> QUESTION -> INVESTIGATE -> VERIFY -> CLASSIFY ->
    INCORPORATE / RETAIN / BOUND
"Discovery therefore feeds Questioning and Scientific Inquiry when
novelty cannot immediately be understood." This loop implements that
feed: a classified-but-not-understood novelty produces a well-formed
input for the Questioning loop's inlet.

Boundary-presentation design (mandate 2, validated against the
theory and the as-built vocabulary):
  The track owner's working hypothesis was a ``novel_pattern``
  presentation -- a detected pattern in adjacent space whose meaning
  is not yet established. The theory confirms it: the loop exists to
  detect "unknown relationships, unexplored capability spaces,
  anomalies, unexpected patterns, new dependencies, new opportunities,
  previously unseen task structures, and potentially useful
  phenomena" -- all of which are *patterns* (structured observations),
  not bare observations and not tasks. It is distinct from:
  - ``novel_observation`` (scientific_inquiry): an observation whose
    *reproducibility* is in question -- the observation is
    established, the hypothesis is whether it replicates;
  - ``novel_task`` (generalization): an unseen *task structure* to
    route, not a pattern to classify.
  ``novel_pattern`` = a detected pattern in the adjacent space of the
  known envelope whose classification is not established. The
  presentation carries the pattern description, where in adjacent
  space it was detected, and how it differs from the known envelope.
  This constant is PROPOSED here -- it is not declared in the frozen
  ``boundary.py`` (that declaration is James's U-1-class decision
  (a); see the honest-status note below).

Structural template: mirrors
``runtime/curiosity/loops/creative_exploration/loop.py`` (the CUR-P3B
stage pattern): loop + inlet, per-stage microcontrollers nested under
a root MC, reasoning via ``substrate.cognize()`` through the governed
inlet, ``substrate.charge()`` per step, ``SubstrateRefused`` on
refusal. The mechanical stage functions below are pure and
independently provable; the loop orchestrates them through the
substrate exactly as the other loops do.

Charter grounding:
- C-6.1: the loop owns exactly one boundary class; inlet and loop
  refuse every foreign boundary with LoopRefused.
- C-2.2: every finding carries complete provenance or the fenced
  store refuses the record.
- C-6.2: microcontrollers retire inside the curiosity forest.
- The loop makes no capability claims: it classifies novelty, it does
  not assert understanding. A novelty it cannot understand is fed to
  Questioning, never silently dropped and never declared understood.

Honest status (2026-10-03): the controller is complete and its stage
machinery is proven (proofs/cur_p3c/). It cannot yet execute against
the curiosity substrate -- the frozen Phase-2 chain admits exactly
three loop vocabulary entries ("questioning", "scientific_inquiry",
"creative_exploration" -- the last landed 2026-10-03):
``CuriositySubstrate.register_loop`` raises ``ValueError`` for
"discovery_novelty", the executive's vocabulary has no discovery
boundary class at all (``BOUNDARY_NOVEL_PATTERN`` is proposed in this
module, not declared in frozen ``boundary.py``), and the run
controller's registry holds only the three admitted loops. Extending
that vocabulary is a U-1-class governance decision (James) plus
authorized frozen-file integration edits (track owner). See
proofs/cur_p3c/BOUNDARY.md.
"""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from swarm_engine.core.graph_controller.controller import GraphController
from swarm_engine.core.microcontroller.substrate import R_UNKNOWN_MC
from swarm_engine.curiosity.evidence.records import (
    TERMINAL_STATES,
    CuriosityFinding,
    EvidenceProvenance,
)
from swarm_engine.curiosity.evidence.writer import CuriosityWriter


#: The loop's vocabulary name. Not yet admitted by the frozen Phase-2
#: chain (see module docstring); the fenced
#: register_loop("discovery_novelty") attempt raises ValueError until
#: the vocabulary is extended by a U-1-class decision.
LOOP_DISCOVERY_NOVELTY = "discovery_novelty"

#: The loop's proposed boundary presentation. NOT declared in the
#: frozen boundary.py -- declaring it is James's U-1-class decision
#: (a). Until then the executive cannot route it (it is not even a
#: known presentation string to the frozen chain).
BOUNDARY_NOVEL_PATTERN = "novel_pattern"

#: Boundary classes this loop owns (C-6.1). The inlet and
#: new_discovery refuse any other boundary class with LoopRefused --
#: the loop never claims another loop's boundary, even if the
#: executive misroutes.
DISCOVERY_BOUNDARIES = (BOUNDARY_NOVEL_PATTERN,)

#: Model/provider stamp for provenance (mechanical composition; no
#: external cognition provider -- same honesty note as the other
#: loops).
MODEL_ID = ("curiosity-discovery-novelty/v1 "
            "(mechanical composition; no external cognition provider)")

#: Evidence terminal states (frozen charter vocabulary) this loop
#: emits. NOVELTY_CLASSIFIED is the loop's convergence: the pattern is
#: classified (understood, or understood-well-enough to route).
TERMINAL_CLASSIFIED = "NOVELTY_CLASSIFIED"
TERMINAL_INSUFFICIENT = "INSUFFICIENT_EVIDENCE"
TERMINAL_INCONCLUSIVE = "INCONCLUSIVE"
TERMINAL_GAP = "BOUNDARY_ESTABLISHED"

assert TERMINAL_CLASSIFIED in TERMINAL_STATES
assert TERMINAL_INSUFFICIENT in TERMINAL_STATES
assert TERMINAL_INCONCLUSIVE in TERMINAL_STATES
assert TERMINAL_GAP in TERMINAL_STATES

#: Executive outcome codes this loop's convergence maps to. The
#: lifecycle terminals (run-controller/enforcement-driven) are NOT
#: loop-converged: RESOURCE_BOUNDARY arrives via SubstrateRefused,
#: LOOP_SUSPENDED/KILLED via the run controller's abort path.
OUTCOME_CONVERGED = "NOVELTY_CONVERGED"
OUTCOME_FEED = "QUESTIONING_FED"
OUTCOME_GAP = "GAP_ACQUISITION"
OUTCOME_UNREACHABLE = "BOUNDARY_UNREACHABLE"

#: Triage advisories. "questioning_fed" means the novelty was
#: classified but not understood and a well-formed input was produced
#: for the Questioning loop's inlet -- the feed, not a conclusion.
TRIAGE_CLASSIFIED = "retain"
TRIAGE_FEED = "questioning_fed"
TRIAGE_GAP = "boundary"


class LoopRefused(Exception):
    """This loop refuses a boundary it does not own (C-6.1)."""


class SubstrateRefused(Exception):
    """The curiosity substrate refused an operation (budget/MC)."""

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(f"{reason}: {message}")
        self.reason = reason
        self.message = message


# ---------------------------------------------------------------------------
# discovery graph
# ---------------------------------------------------------------------------

NODE_EXPLORE = "explore"
NODE_DETECT = "detect"
NODE_INVESTIGATE = "investigate"
NODE_VERIFY = "verify"
NODE_CLASSIFY = "classify"

DISCOVERY_NODES = (
    NODE_EXPLORE,
    NODE_DETECT,
    NODE_INVESTIGATE,
    NODE_VERIFY,
    NODE_CLASSIFY,
)

NODE_DEPENDENCIES = {
    NODE_EXPLORE: (),
    NODE_DETECT: (NODE_EXPLORE,),
    NODE_INVESTIGATE: (NODE_DETECT,),
    NODE_VERIFY: (NODE_INVESTIGATE,),
    NODE_CLASSIFY: (NODE_VERIFY,),
}

#: Node goals, worded so the GraphController's relevance selection
#: admits the discovery chain for a discovery objective (dependency
#: closure pulls the whole chain once the head matches). Each goal
#: shares the objective's literal vocabulary ("exploration", not
#: "explore"; "detection", not "detect" -- the controller's stemmer
#: does not conflate them) at or above the controller's relevance
#: floor (verified empirically in the battery's T13: all five >= 0.60
#: vs 0.34 -- the permanent CUR-P3A-INT lesson check).
NODE_GOALS = {
    NODE_EXPLORE: ("exploration of adjacent space from the known "
                   "envelope for novel pattern discovery"),
    NODE_DETECT: ("detection of novelty among explored patterns "
                  "through adjacent space discovery"),
    NODE_INVESTIGATE: ("investigation of detected novel patterns "
                       "through discovery exploration"),
    NODE_VERIFY: ("verification of discovery exploration through "
                  "novel pattern detection investigation "
                  "classification"),
    NODE_CLASSIFY: ("classification of verified novel patterns "
                    "through discovery exploration"),
}

DISCOVERY_OBJECTIVE = ("exploration of adjacent space for novel "
                       "pattern discovery detection investigation "
                       "verification and classification")


class DiscoveryGraph:
    """The structural discovery graph, adapted to the GraphController's
    StructuralGraph protocol. The controller never touches graph
    internals; the loop never bypasses the controller."""

    graph_id = "curiosity/discovery_novelty/pipeline/v1"

    def node_ids(self) -> List[str]:
        return list(DISCOVERY_NODES)

    def node_goal(self, node_id: str) -> str:
        return NODE_GOALS[node_id]

    def dependencies(self, node_id: str) -> List[str]:
        return list(NODE_DEPENDENCIES[node_id])


# ---------------------------------------------------------------------------
# pure stage machinery: explore -> detect -> investigate -> verify ->
# classify. Every function is pure and independently provable; the loop
# orchestrates them through the substrate exactly as the other loops
# do. All dataclasses below use JSON-native fields only (the
# CUR-P3B-INT checkpoint lesson): loop state holds asdict() forms and
# restore() rebuilds the dataclasses from them.
# ---------------------------------------------------------------------------

#: Novelty floor: a sighting must carry at least this many markers not
#: shared with its nearest envelope neighbor to count as novel.
#: Below the floor it is a trivial variation -- routine, named.
NOVELTY_MARKER_FLOOR = 2


@dataclass
class PatternSighting:
    """One pattern observed in adjacent space, with lineage."""
    sighting_id: str
    markers: List[str]            # the pattern's term markers
    adjacent_dimension: str       # where in adjacent space it was seen
    variation_note: str           # how it differs from the envelope


@dataclass
class NoveltyAssessment:
    """The novelty verdict for one sighting, with a named reason."""
    sighting_id: str
    novel: bool
    reason: str                   # named: exact_repeat / trivial_variation /
                                  # already_classified / no_stable_structure /
                                  # novel_pattern
    distinguishing_markers: List[str] = field(default_factory=list)
    nearest_envelope: str = ""


@dataclass
class Investigation:
    """What the investigation established about a novel sighting."""
    sighting_id: str
    characterization: str         # what the pattern appears to be
    relates_to: List[str]         # envelope patterns it relates to
    open_questions: List[str]     # what is not yet understood


@dataclass
class Verification:
    """The verified-substrate check on an investigated pattern."""
    sighting_id: str
    verified: bool
    check_note: str               # what was checked, or the named gap
    gap_named: str = ""           # non-empty when unverified
    resolved_questions: List[str] = field(default_factory=list)
    # open questions the verified entry answers (mechanical: >= 2
    # content-term overlap with the entry's markers)


def explore_adjacent_space(*, known_envelope: List[Dict[str, Any]],
                           adjacent_observations: List[Dict[str, Any]],
                           ) -> List[PatternSighting]:
    """Structure presented adjacent-space observations into pattern
    sightings with lineage. The exploration is over presented
    adjacent-space data (the loop cannot survey a real environment at
    stage level); the honest work here is lineage: every sighting
    records where it was seen and what it varies from."""
    sightings: List[PatternSighting] = []
    for i, obs in enumerate(adjacent_observations):
        sightings.append(PatternSighting(
            sighting_id=f"sgt_{i:03d}",
            markers=list(obs.get("markers", [])),
            adjacent_dimension=str(obs.get("dimension", "unspecified")),
            variation_note=str(obs.get("variation_note", "")),
        ))
    return sightings


def assess_novelty(sighting: PatternSighting, *,
                   known_envelope: List[Dict[str, Any]],
                   prior_art: Tuple[str, ...],
                   ) -> NoveltyAssessment:
    """The core adversarial gate: decide novel vs routine, with a
    named reason for every verdict. Routine patterns are NEVER
    claimed as novel -- each refusal names its reason."""
    markers = set(sighting.markers)
    # No stable structure: fewer than two markers is noise, not a
    # pattern.
    if len(markers) < 2:
        return NoveltyAssessment(
            sighting_id=sighting.sighting_id, novel=False,
            reason="no_stable_structure",
            distinguishing_markers=[],
            nearest_envelope="",
        )
    # Already classified: the pattern's marker set matches prior art.
    for prior in prior_art:
        if set(prior.split()) == markers:
            return NoveltyAssessment(
                sighting_id=sighting.sighting_id, novel=False,
                reason="already_classified",
                distinguishing_markers=[],
                nearest_envelope=prior,
            )
    # Compare against the known envelope.
    best_overlap = 0
    best_name = ""
    best_new = 0
    for entry in known_envelope:
        env_markers = set(entry.get("markers", []))
        overlap = len(markers & env_markers)
        new = len(markers - env_markers)
        if overlap > best_overlap or (overlap == best_overlap
                                      and new < best_new):
            best_overlap = overlap
            best_name = str(entry.get("name", ""))
            best_new = new
    if best_overlap == len(markers) and best_new == 0:
        return NoveltyAssessment(
            sighting_id=sighting.sighting_id, novel=False,
            reason="exact_repeat",
            distinguishing_markers=[],
            nearest_envelope=best_name,
        )
    if best_new < NOVELTY_MARKER_FLOOR:
        return NoveltyAssessment(
            sighting_id=sighting.sighting_id, novel=False,
            reason="trivial_variation",
            distinguishing_markers=sorted(markers),
            nearest_envelope=best_name,
        )
    return NoveltyAssessment(
        sighting_id=sighting.sighting_id, novel=True,
        reason="novel_pattern",
        distinguishing_markers=sorted(markers - set(
            next((e.get("markers", []) for e in known_envelope
                  if str(e.get("name", "")) == best_name), []))),
        nearest_envelope=best_name,
    )


def investigate_pattern(sighting: PatternSighting,
                        assessment: NoveltyAssessment) -> Investigation:
    """Investigate a novel sighting: characterize it, relate it to the
    envelope, and name what is not understood. Only called for
    novel=True assessments -- routine sightings are never
    investigated (there is nothing to investigate)."""
    if not assessment.novel:
        raise ValueError("investigate_pattern called on a routine sighting")
    return Investigation(
        sighting_id=sighting.sighting_id,
        characterization=(
            f"pattern in {sighting.adjacent_dimension} distinguished by "
            f"{', '.join(assessment.distinguishing_markers)}; "
            f"nearest envelope neighbor: {assessment.nearest_envelope or 'none'}"
        ),
        relates_to=([assessment.nearest_envelope]
                    if assessment.nearest_envelope else []),
        open_questions=[
            f"what does the distinguishing marker set imply for "
            f"{sighting.adjacent_dimension}",
        ],
    )


#: Content stopwords for the question-resolution rule (same set as
#: the GraphController's, restated locally so this module does not
#: depend on the controller's privates).
_RESOLVE_STOP = {"a", "an", "the", "of", "to", "for", "from", "and",
                 "or", "then", "with", "on", "in", "is", "it", "its",
                 "what", "does", "do", "how", "why", "which"}


def _content_terms(text: str) -> set:
    return {w for w in "".join(
        c.lower() if c.isalnum() or c == " " else " "
        for c in text).split() if w not in _RESOLVE_STOP}


def verify_pattern(investigation: Investigation, *,
                   verified_ledger: List[Dict[str, Any]]) -> Verification:
    """Check the investigated pattern against the verified substrate.
    Only concretely verified material counts (the verified-factual
    substrate rule); unverified material names its gap, never treated
    as fact. A verified entry additionally resolves the investigation's
    open questions whose content terms it covers (>= 2 shared terms):
    the substrate answered them, so they are not fed to Questioning."""
    for entry in verified_ledger:
        entry_markers = set(entry.get("markers", []))
        if entry_markers & _content_terms(investigation.characterization):
            resolved = [
                q for q in investigation.open_questions
                if len(_content_terms(q) & entry_markers) >= 2
            ]
            return Verification(
                sighting_id=investigation.sighting_id, verified=True,
                check_note=(f"characterization markers check against "
                            f"verified entry {entry.get('primitive_id', '?')}"),
                resolved_questions=resolved,
            )
    return Verification(
        sighting_id=investigation.sighting_id, verified=False,
        check_note="no verified-ledger entry covers the characterization",
        gap_named="unverified_pattern_characterization",
    )


def classify_novelty(*, sighting: PatternSighting,
                     assessment: NoveltyAssessment,
                     investigation: Optional[Investigation],
                     verification: Optional[Verification],
                     ) -> Tuple[str, str, str, Dict[str, Any]]:
    """Converge one novel sighting to a terminal state. Returns
    (terminal, outcome, triage, detail). A verified novelty whose open
    questions the substrate resolved classifies; a novelty with
    genuinely unanswered questions feeds Questioning; thin evidence is
    insufficient; a named capability gap is a boundary."""
    if verification is not None and not verification.verified:
        return (TERMINAL_GAP, OUTCOME_GAP, TRIAGE_GAP, {
            "gap": verification.gap_named,
            "note": "the pattern names a capability gap: its "
                    "characterization cannot be verified against the "
                    "verified substrate",
        })
    open_qs = (investigation.open_questions if investigation else [])
    resolved = (verification.resolved_questions if verification else [])
    unresolved = [q for q in open_qs if q not in resolved]
    if unresolved:
        # Not understood: feed Questioning (the theory §7 feed).
        return (TERMINAL_INSUFFICIENT, OUTCOME_FEED, TRIAGE_FEED, {
            "note": "novelty classified as genuine but not understood; "
                    "feeding Questioning",
            "open_questions": unresolved,
        })
    return (TERMINAL_CLASSIFIED, OUTCOME_CONVERGED, TRIAGE_CLASSIFIED, {
        "distinguishing_markers": assessment.distinguishing_markers,
        "nearest_envelope": assessment.nearest_envelope,
    })


def build_questioning_trigger(*, discovery_id: str,
                              classification: Dict[str, Any],
                              bounded_objective: str,
                              ) -> Dict[str, Any]:
    """Build the well-formed input for the Questioning loop's inlet
    (the theory §7 feed): an imprecise_question-shaped trigger dict
    carrying the novelty the discovery loop could not understand.
    Shaped exactly like CuriosityTrigger.as_dict()."""
    questions = classification.get("open_questions", [])
    question_text = ("the discovery loop classified a novel pattern it "
                     "cannot understand: " + "; ".join(questions)
                     if questions else
                     "the discovery loop classified a novel pattern it "
                     "cannot understand")
    return {
        "trigger_id": f"trg_feed_{discovery_id}",
        "boundary_class": "imprecise_question",
        "question_text": question_text,
        "bounded_objective": bounded_objective,
        "origin": "CURIOUSITY_INITIATED",
    }


# ---------------------------------------------------------------------------
# provenance + findings
# ---------------------------------------------------------------------------

def stamp_provenance(*, bounded_objective: str,
                     triage: str) -> EvidenceProvenance:
    """Provenance for every discovery finding (C-2.2: complete or the
    fenced store refuses the record)."""
    return EvidenceProvenance(
        loop=LOOP_DISCOVERY_NOVELTY,
        bounded_objective=bounded_objective,
        model=MODEL_ID,
        triage=triage,
    )


def build_finding(*, discovery_id: str, trigger: Dict[str, Any],
                  terminal_state: str, outcome: str, triage: str,
                  assessment: NoveltyAssessment, detail: str,
                  payload_ref: str) -> CuriosityFinding:
    """Build the fenced-store finding for a converged discovery. Always
    carries complete provenance; validate() passes or the caller has a
    bug (the store refuses incomplete records -- C-2.2)."""
    return CuriosityFinding(
        evidence_id="ev_" + discovery_id.replace("dis_", ""),
        loop=LOOP_DISCOVERY_NOVELTY,
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
    the executive never sees through this. ``known_envelope`` carries
    the presented known-envelope patterns the novelty check runs
    against; ``prior_art`` the already-classified patterns;
    ``verified_ledger`` the presented verified set."""
    substrate: Any
    discovery_id: str
    budget_slice_s: float
    known_envelope: List[Dict[str, Any]] = field(default_factory=list)
    prior_art: Tuple[str, ...] = ()
    verified_ledger: List[Dict[str, Any]] = field(default_factory=list)


class DiscoveryNoveltyLoopInlet:
    """The discovery loop's entry point. enter() validates the trigger
    shape (C-6.1: foreign boundary classes are refused HERE, never
    claimed; the presentation must be a novel_pattern) and returns the
    loop's initial private state; the run controller drives it from
    there."""

    def __init__(self, loop: "DiscoveryNoveltyLoop") -> None:
        self._loop = loop

    def enter(self, trigger: Any, ctx: LoopContext) -> Dict[str, Any]:
        return self._loop.new_discovery(trigger, ctx)


class DiscoveryNoveltyLoop:
    """The logical discovery/novelty loop controller (level 2). Owns
    the discovery graph and the per-stage microcontrollers; exposes
    only step()/abort()/restore() and loop-level aggregates."""

    loop_name = LOOP_DISCOVERY_NOVELTY

    # -- lifecycle --------------------------------------------------------

    def new_discovery(self, trigger: Any, ctx: LoopContext) -> Dict[str, Any]:
        # C-6.1 defense in depth: the loop itself refuses a foreign
        # boundary even if the inlet check is ever bypassed.
        boundary = trigger.boundary_class
        if boundary not in DISCOVERY_BOUNDARIES:
            raise LoopRefused(
                f"discovery_novelty does not own boundary class "
                f"{boundary!r}; owned: {list(DISCOVERY_BOUNDARIES)}")
        # The presentation must carry a pattern: a novel_pattern
        # trigger without pattern content is refused, not explored.
        pattern_text = (trigger.question_text or "").strip()
        if not pattern_text:
            raise LoopRefused(
                "discovery_novelty refuses the trigger: a novel_pattern "
                "presentation must carry the detected pattern's "
                "description")
        # State holds JSON-native forms only (the CUR-P3B-INT
        # checkpoint lesson): dataclasses are rebuilt in restore().
        return {
            "discovery_id": ctx.discovery_id,
            "trigger": trigger.as_dict(),
            "boundary_class": boundary,
            "stage": NODE_EXPLORE,
            "sightings": [],
            "assessments": [],
            "investigations": [],
            "verifications": [],
            "classifications": [],
            "questioning_feeds": [],
            "node_summaries": {},
            "region_id": None,
            "root_mc": None,
            "stage_mc": None,
            "status": "running",
        }

    def restore(self, saved: Dict[str, Any]) -> Dict[str, Any]:
        """Rebuild loop state from a verified checkpoint. The graph
        region is per-discovery and rebuilt; completed stage summaries
        are kept as history and the pipeline resumes at the saved
        stage. All persisted forms are JSON-native (asdict), so the
        dataclasses rebuild faithfully -- no str()-degraded objects
        (the CUR-P3B-INT lesson)."""
        state = dict(saved)
        # Rebuild dataclass lists from their asdict forms.
        state["sightings"] = [PatternSighting(**s)
                              for s in saved.get("sightings", [])]
        state["assessments"] = [NoveltyAssessment(**a)
                                for a in saved.get("assessments", [])]
        state["investigations"] = [Investigation(**i)
                                   for i in saved.get("investigations", [])]
        state["verifications"] = [Verification(**v)
                                  for v in saved.get("verifications", [])]
        state["region_id"] = None
        state["root_mc"] = None
        state["stage_mc"] = None
        state["status"] = "running"
        return state

    def abort(self, state: Dict[str, Any], ctx: LoopContext,
              graph: GraphController) -> None:
        """Kill path: close the open region, retire the discovery's
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
        terminal dict when the discovery converged. Requires a substrate
        that admits this loop's vocabulary -- at stage level the frozen
        chain refuses, so this path is written to the template and
        labeled NOT VERIFIED THIS SHIFT (mandate 5)."""
        if state["status"] != "running":
            return StepResult(done=True, terminal=state.get("terminal"),
                              detail="loop not running")

        sub = ctx.substrate
        if state["root_mc"] is None:
            # Reservation budget mirrors the other loops': root 1/2 +
            # stage 1/4 of the slice; stage MCs retire after use.
            spawned = sub.spawn(
                LOOP_DISCOVERY_NOVELTY,
                purpose=f"discovery:{ctx.discovery_id}",
                budget_s=ctx.budget_slice_s / 2.0)
            if not spawned.ok:
                raise SubstrateRefused(spawned.refusal.reason,
                                      spawned.refusal.message)
            state["root_mc"] = spawned.mc.mc_id

        if state["region_id"] is None:
            self._open_discovery(state, ctx, graph)

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

        if node_id == NODE_CLASSIFY:
            return self._finish(state, ctx, graph, blocked=None)
        return StepResult(done=False, detail=f"operated {node_id}")

    # -- discovery mechanics ----------------------------------------------

    def _open_discovery(self, state: Dict[str, Any], ctx: LoopContext,
                        graph: GraphController) -> None:
        region = graph.open_region(
            DiscoveryGraph(),
            objective=(f"discovery of novel patterns in adjacent space: "
                       f"{state['trigger'].get('question_text', '')}"),
        )
        if not region.ok:
            raise SubstrateRefused(
                region.status,
                "graph controller refused the discovery region")
        state["region_id"] = region.region_id

    def _operate_node(self, node_id: str, state: Dict[str, Any],
                      ctx: LoopContext, sub: Any) -> Dict[str, Any]:
        """Run one pipeline node through a stage microcontroller. Pure
        stage functions do the work; the MC only bounds and accounts
        it."""
        spawned = sub.spawn(
            LOOP_DISCOVERY_NOVELTY,
            purpose=f"discovery:{ctx.discovery_id}:{node_id}",
            budget_s=ctx.budget_slice_s / 4.0,
            parent=state["root_mc"])
        if not spawned.ok:
            raise SubstrateRefused(spawned.refusal.reason,
                                  spawned.refusal.message)
        stage_mc = spawned.mc.mc_id
        state["stage_mc"] = stage_mc
        try:
            if node_id == NODE_EXPLORE:
                value = self._node_explore(state, ctx)
            elif node_id == NODE_DETECT:
                value = self._node_detect(state, ctx)
            elif node_id == NODE_INVESTIGATE:
                value = self._node_investigate(state, ctx)
            elif node_id == NODE_VERIFY:
                value = self._node_verify(state, ctx)
            elif node_id == NODE_CLASSIFY:
                value = self._node_classify(state, ctx)
            else:  # pragma: no cover -- graph only yields known nodes
                raise ValueError(f"unknown discovery node {node_id!r}")
        finally:
            sub.retire(stage_mc)
            state["stage_mc"] = None
        return value

    def _node_explore(self, state: Dict[str, Any],
                      ctx: LoopContext) -> Dict[str, Any]:
        trigger = state["trigger"]
        adjacent_observations = trigger.get("adjacent_observations", [])
        sightings = explore_adjacent_space(
            known_envelope=ctx.known_envelope,
            adjacent_observations=adjacent_observations)
        # Persist asdict forms (JSON-native; the checkpoint lesson).
        state["sightings"] = [asdict(s) for s in sightings]
        return {"summary": f"explored {len(sightings)} sightings",
                "sightings": [asdict(s) for s in sightings]}

    def _node_detect(self, state: Dict[str, Any],
                     ctx: LoopContext) -> Dict[str, Any]:
        sightings = [PatternSighting(**s) for s in state["sightings"]]
        assessments = [assess_novelty(s, known_envelope=ctx.known_envelope,
                                     prior_art=ctx.prior_art)
                       for s in sightings]
        state["assessments"] = [asdict(a) for a in assessments]
        novel = sum(1 for a in assessments if a.novel)
        return {"summary": f"detected {novel}/{len(assessments)} novel",
                "assessments": [asdict(a) for a in assessments]}

    def _node_investigate(self, state: Dict[str, Any],
                          ctx: LoopContext) -> Dict[str, Any]:
        sightings = {s["sighting_id"]: PatternSighting(**s)
                     for s in state["sightings"]}
        investigations = []
        for a in state["assessments"]:
            if not a["novel"]:
                continue  # routine sightings are never investigated
            inv = investigate_pattern(
                sightings[a["sighting_id"]],
                NoveltyAssessment(**a))
            investigations.append(asdict(inv))
        state["investigations"] = investigations
        return {"summary": f"investigated {len(investigations)} novel",
                "investigations": investigations}

    def _node_verify(self, state: Dict[str, Any],
                     ctx: LoopContext) -> Dict[str, Any]:
        verifications = [
            asdict(verify_pattern(
                Investigation(**i), verified_ledger=ctx.verified_ledger))
            for i in state["investigations"]
        ]
        state["verifications"] = verifications
        return {"summary": f"verified {len(verifications)}",
                "verifications": verifications}

    def _node_classify(self, state: Dict[str, Any],
                       ctx: LoopContext) -> Dict[str, Any]:
        sightings = {s["sighting_id"]: PatternSighting(**s)
                     for s in state["sightings"]}
        investigations = {i["sighting_id"]: Investigation(**i)
                          for i in state["investigations"]}
        verifications = {v["sighting_id"]: Verification(**v)
                         for v in state["verifications"]}
        classifications = []
        feeds = []
        for a in state["assessments"]:
            if not a["novel"]:
                continue
            sid = a["sighting_id"]
            terminal, outcome, triage, detail = classify_novelty(
                sighting=sightings[sid],
                assessment=NoveltyAssessment(**a),
                investigation=investigations.get(sid),
                verification=verifications.get(sid))
            classifications.append({
                "sighting_id": sid, "terminal": terminal,
                "outcome": outcome, "triage": triage, "detail": detail,
            })
            if triage == TRIAGE_FEED:
                feeds.append(build_questioning_trigger(
                    discovery_id=state["discovery_id"],
                    classification={"open_questions":
                                    detail.get("open_questions", [])},
                    bounded_objective=state["trigger"]["bounded_objective"]))
        state["classifications"] = classifications
        state["questioning_feeds"] = feeds
        return {"summary": f"classified {len(classifications)} "
                           f"({len(feeds)} fed to questioning)",
                "classifications": classifications}

    def _finish(self, state: Dict[str, Any], ctx: LoopContext,
                graph: GraphController,
                blocked: Optional[str]) -> StepResult:
        """Converge the discovery to its terminal state."""
        if state.get("region_id"):
            graph.close_region(state["region_id"])
            state["region_id"] = None
        sub = ctx.substrate
        for mc_id in (state.get("stage_mc"), state.get("root_mc")):
            if mc_id:
                sub.retire(mc_id)
        state["root_mc"] = state["stage_mc"] = None

        classifications = state.get("classifications", [])
        if blocked is not None:
            terminal = {"terminal_state": "BLOCKED",
                        "outcome": OUTCOME_UNREACHABLE,
                        "triage": TRIAGE_GAP,
                        "detail": f"graph blocked: {blocked}"}
        elif not classifications:
            terminal = {"terminal_state": TERMINAL_INSUFFICIENT,
                        "outcome": OUTCOME_UNREACHABLE,
                        "triage": TRIAGE_GAP,
                        "detail": "no novel sighting survived detection"}
        else:
            # The discovery converges on its classifications; the
            # primary terminal is the first classification's (the
            # battery drives single-novelty discoveries; multi-novelty
            # discoveries persist each classification's finding).
            first = classifications[0]
            terminal = {"terminal_state": first["terminal"],
                        "outcome": first["outcome"],
                        "triage": first["triage"],
                        "detail": first["detail"],
                        "classifications": classifications,
                        "questioning_feeds": state.get(
                            "questioning_feeds", [])}
        state["status"] = "done"
        state["terminal"] = terminal
        return StepResult(done=True, terminal=terminal,
                          detail="discovery converged")
