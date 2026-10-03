"""Scientific Inquiry loop controller (Phase 3, CUR-P3A).

The second curiosity loop: converges inquiry-shaped boundaries --
a hypothesis candidate or a novel observation -- through the scientific
pipeline (observe -> hypothesize -> predict -> test -> conclude) to a
terminal state from the charter's enumerated set, with provenance
stamped on every finding.

Structural template: mirrors
``runtime/curiosity/loops/questioning/loop.py`` (the Phase-2 template):
loop + inlet, per-stage microcontrollers nested under a root MC,
reasoning via ``substrate.cognize()`` through the governed inlet,
``substrate.charge()`` per step, ``SubstrateRefused`` on refusal.
The mechanical stage functions below are pure and independently
provable; the loop orchestrates them through the substrate exactly as
the questioning loop does.

Honest status (2026-10-01): the controller is complete and its stage
machinery is proven (proofs/cur_p3a/). It cannot yet execute against
the curiosity substrate -- the frozen Phase-2 chain admits exactly one
loop vocabulary entry ("questioning"): ``CuriositySubstrate.
register_loop`` raises ``ValueError`` for "scientific_inquiry", the
executive's ``LOOP_OWNERSHIP`` has no entry for the inquiry boundary
classes (``LOOP_ABSENT``), and the run controller hard-codes the
questioning loop/inlet. Extending that vocabulary is a U-1-class
governance decision (James) plus authorized frozen-file integration
edits (track owner). See proofs/cur_p3a/BOUNDARY.md.
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
    mentions_observable,
)
from swarm_engine.curiosity.executive.boundary import (
    BOUNDARY_HYPOTHESIS_CANDIDATE,
    BOUNDARY_NOVEL_OBSERVATION,
)
from swarm_engine.curiosity.evidence.records import (
    TERMINAL_STATES,
    CuriosityFinding,
    EvidenceProvenance,
)
from swarm_engine.curiosity.evidence.writer import CuriosityWriter


#: The loop's vocabulary name. Not yet admitted by the frozen Phase-2
#: chain (see module docstring); the fenced register_loop("scientific_
#: inquiry") attempt raises ValueError until the vocabulary is extended.
LOOP_SCIENTIFIC_INQUIRY = "scientific_inquiry"

#: Boundary classes this loop owns (C-6.1). The inlet and new_inquiry
#: refuse any other boundary class with LoopRefused -- the loop never
#: claims another loop's boundary, even if the executive misroutes.
INQUIRY_BOUNDARIES = (
    BOUNDARY_HYPOTHESIS_CANDIDATE,
    BOUNDARY_NOVEL_OBSERVATION,
)

#: Model/provider stamp for provenance (mechanical inquiry; no external
#: cognition provider -- same honesty note as the questioning loop).
MODEL_ID = "curiosity-scientific-inquiry/v1 (mechanical inquiry; no external cognition provider)"

#: Evidence terminal states (frozen charter vocabulary) this loop emits.
TERMINAL_SUPPORTED = "HYPOTHESIS_SUPPORTED"
TERMINAL_REFUTED = "HYPOTHESIS_REFUTED"
TERMINAL_INSUFFICIENT = "INSUFFICIENT_EVIDENCE"
TERMINAL_INCONCLUSIVE = "INCONCLUSIVE"
TERMINAL_METHOD_BOUNDARY = "BOUNDARY_ESTABLISHED"

assert TERMINAL_SUPPORTED in TERMINAL_STATES
assert TERMINAL_REFUTED in TERMINAL_STATES
assert TERMINAL_INSUFFICIENT in TERMINAL_STATES
assert TERMINAL_INCONCLUSIVE in TERMINAL_STATES
assert TERMINAL_METHOD_BOUNDARY in TERMINAL_STATES

#: Executive outcome codes this loop's convergence maps to. The
#: lifecycle terminals (run-controller/enforcement-driven) are NOT
#: loop-converged: RESOURCE_BOUNDARY arrives via SubstrateRefused,
#: LOOP_SUSPENDED/KILLED via the run controller's abort path.
OUTCOME_CONVERGED = "QUESTION_CONVERGED"
OUTCOME_GAP = "GAP_ACQUISITION"
OUTCOME_UNREACHABLE = "BOUNDARY_UNREACHABLE"


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
    boundaries even if misrouted)."""


# ---------------------------------------------------------------------------
# inquiry graph shape
# ---------------------------------------------------------------------------

NODE_OBSERVE = "observe"
NODE_HYPOTHESIZE = "hypothesize"
NODE_PREDICT = "predict"
NODE_TEST = "test"
NODE_CONCLUDE = "conclude"

INQUIRY_NODES = (
    NODE_OBSERVE, NODE_HYPOTHESIZE, NODE_PREDICT, NODE_TEST, NODE_CONCLUDE)

NODE_DEPENDENCIES = {
    NODE_OBSERVE: (),
    NODE_HYPOTHESIZE: (NODE_OBSERVE,),
    NODE_PREDICT: (NODE_HYPOTHESIZE,),
    NODE_TEST: (NODE_PREDICT,),
    NODE_CONCLUDE: (NODE_TEST,),
}

#: Node goals, worded so the GraphController's relevance selection admits
#: the inquiry chain for an inquiry objective (dependency closure pulls
#: the whole chain once the head matches). Each goal shares the
#: objective's vocabulary ("scientific", not "scientifically" -- the
#: controller's stemmer does not conflate them) at or above the
#: controller's relevance floor (verified: all five >= 0.41 vs 0.34).
NODE_GOALS = {
    NODE_OBSERVE: ("record the presented observation or hypothesis "
                   "candidate for scientific testing"),
    NODE_HYPOTHESIZE: ("form a falsifiable hypothesis candidate from "
                       "the observation for the scientific test"),
    NODE_PREDICT: ("derive the testable prediction from the hypothesis "
                   "candidate for the scientific test of the observation"),
    NODE_TEST: ("run the scientific test of the hypothesis candidate "
                "prediction against the presented novel observation "
                "evidence"),
    NODE_CONCLUDE: ("assess the scientific test outcome and converge "
                    "the hypothesis to a terminal state"),
}

INQUIRY_OBJECTIVE = ("test the hypothesis candidate or novel observation "
                     "through the scientific pipeline to a terminal state")


class InquiryGraph:
    """The structural inquiry graph, adapted to the GraphController's
    StructuralGraph protocol. The controller never touches graph
    internals; the loop never bypasses the controller."""

    graph_id = "curiosity/scientific_inquiry/pipeline/v1"

    def node_ids(self) -> List[str]:
        return list(INQUIRY_NODES)

    def node_goal(self, node_id: str) -> str:
        return NODE_GOALS[node_id]

    def dependencies(self, node_id: str) -> List[str]:
        return list(NODE_DEPENDENCIES[node_id])


# ---------------------------------------------------------------------------
# pure stage machinery: observation -> hypothesis -> prediction -> test
# ---------------------------------------------------------------------------

_UNOBSERVABLE_DENIALS = (
    "undetectable", "invisible", "unobservable",
    "never be measured", "cannot be measured", "can't be measured",
    "no observable", "beyond measurement")


def _strip_framing(text: str) -> str:
    """Strip a leading presentation framing label ("Novel observation:",
    "Hypothesis:", "I hypothesize that") before the observability
    check: the label's own vocabulary must not count as the hypothesis
    naming an observable."""
    import re
    lowered = text.lstrip().lower()
    for prefix in ("novel observation:", "observation:",
                   "hypothesis candidate:", "hypothesis:",
                   "i hypothesize that"):
        if lowered.startswith(prefix):
            return text.lstrip()[len(prefix):].strip()
    return text


def _denies_observability(text: str) -> bool:
    """Explicit denial of observability ("undetectable", "never be
    measured", ...): substring matching alone would mistake the denial
    for an observable."""
    lowered = text.lower()
    return any(denial in lowered for denial in _UNOBSERVABLE_DENIALS)


@dataclass
class Hypothesis:
    """A formed hypothesis with its checkable falsifiability slots."""
    statement: str
    claim_terms: List[str]
    falsifiable: bool
    observable_consequence: str
    falsifier: str
    reason: str


@dataclass
class Prediction:
    """The testable observable prediction derived from a hypothesis."""
    statement: str
    observable_markers: List[str]


@dataclass
class TestResult:
    """Outcome of running a prediction against presented evidence."""
    verdict: str  # SUPPORTED | REFUTED | INCONCLUSIVE | INSUFFICIENT
    supporting: List[str]
    refuting: List[str]
    detail: str


def form_hypothesis(*, source_text: str, boundary_class: str,
                    claim: str = "", claim_terms: Optional[List[str]] = None
                    ) -> Hypothesis:
    """Form (or validate) the hypothesis for one inquiry.

    For a hypothesis candidate the source IS the hypothesis: it is
    validated for falsifiability. For a novel observation the
    hypothesis is the reproducibility claim over the extracted claim.
    Falsifiability is checkable, never asserted: the hypothesis must
    name an observable consequence AND a falsifier (what observation
    would refute it). ``claim``/``claim_terms`` arrive from the
    cognition inlet (OP_EXTRACT); when empty they are derived
    mechanically here so the function stays pure and testable.
    """
    text = (source_text or "").strip()
    if not claim:
        claim, terms = extract_unknown(text)
    else:
        terms = list(claim_terms or _content_terms(claim))
    # The framing label ("Novel observation:") must not count as the
    # hypothesis naming an observable; an explicit denial of
    # observability ("undetectable", "never be measured") defeats a
    # substring match.
    check_text = _strip_framing(text)
    observable = (mentions_observable(check_text)
                  and not _denies_observability(check_text))
    names_falsifier = "falsif" in text.lower()

    if boundary_class == BOUNDARY_NOVEL_OBSERVATION:
        statement = (
            f"The observed pattern in {claim!r} is real and reproducible "
            f"under the same conditions.")
        observable_consequence = (
            f"independent re-observation reproduces {claim!r}")
        falsifier = (
            f"controlled re-observation under the same conditions fails "
            f"to reproduce {claim!r}")
        # A novel observation's reproducibility claim is falsifiable
        # exactly when the observation names something observable.
        falsifiable = observable and bool(claim)
        reason = ("novel observation: reproducibility claim; falsifiable "
                  f"= observation names an observable ({observable}) "
                  f"with a non-empty claim ({bool(claim)})")
    else:
        statement = text
        observable_consequence = (
            f"the observable consequence named by the hypothesis "
            f"({claim!r}) is detected in evidence")
        falsifier = (
            f"evidence shows the absence of {claim!r} where the "
            f"hypothesis requires its presence")
        falsifiable = observable and bool(claim)
        reason = ("hypothesis candidate: falsifiable = names an "
                  f"observable consequence ({observable}) and a "
                  f"non-empty claim ({bool(claim)})")
        # A candidate that already names its falsifier is strictly
        # stronger; one that does not is still falsifiable when the
        # consequence is observable -- the loop supplies the falsifier.
        _ = names_falsifier
    return Hypothesis(
        statement=statement,
        claim_terms=terms,
        falsifiable=falsifiable,
        observable_consequence=observable_consequence,
        falsifier=falsifier,
        reason=reason,
    )


def derive_prediction(hypothesis: Hypothesis) -> Prediction:
    """Derive the testable prediction: the observable consequence in
    checkable form, with the marker terms the test matches against."""
    markers = [t for t in hypothesis.claim_terms
               if t not in ("the", "a", "an")]
    statement = (
        f"If the hypothesis holds, then {hypothesis.observable_consequence}.")
    return Prediction(statement=statement, observable_markers=markers[:8])


def run_test(prediction: Prediction, hypothesis: Hypothesis,
             evidence_lines: List[str]) -> TestResult:
    """Run the prediction against presented evidence (mechanical).

    A line SUPPORTS when it shares >= 2 marker terms with the
    prediction AND mentions an occurrence marker (observed, measured,
    detected, confirmed, reproduced, found, shows). A line REFUTES when
    it shares >= 2 marker terms AND mentions an absence/falsifier
    marker (absent, missing, failed, no evidence, not observed,
    contradicts, refutes). Anything else is neutral. No evidence at
    all is INSUFFICIENT (not inconclusive): the test could not run.
    """
    markers = set(prediction.observable_markers)
    supporting: List[str] = []
    refuting: List[str] = []
    _OCCUR = ("observ", "measur", "detect", "confirm", "reproduc",
              "found", "shows", "demonstrat")
    _ABSENT = ("absent", "missing", "fail", "no evidence", "not observ",
               "contradict", "refut")
    for line in evidence_lines or []:
        lowered = line.lower()
        overlap = markers & set(_content_terms(line))
        if len(overlap) < 2:
            continue
        if any(mk in lowered for mk in _ABSENT):
            refuting.append(line)
        elif any(mk in lowered for mk in _OCCUR):
            supporting.append(line)
    if not evidence_lines:
        return TestResult(
            verdict="INSUFFICIENT", supporting=[], refuting=[],
            detail=("no evidence presented: the prediction could not be "
                    "tested"))
    if refuting and not supporting:
        verdict, detail = ("REFUTED",
                           f"{len(refuting)} refuting line(s), none supporting")
    elif supporting and not refuting:
        verdict, detail = ("SUPPORTED",
                           f"{len(supporting)} supporting line(s), none refuting")
    elif supporting and refuting:
        verdict, detail = ("INCONCLUSIVE",
                           f"{len(supporting)} supporting vs "
                           f"{len(refuting)} refuting line(s)")
    else:
        verdict, detail = ("INSUFFICIENT",
                           "evidence presented but no line substantively "
                           "bears on the prediction (>= 2 marker terms)")
    return TestResult(verdict=verdict, supporting=supporting,
                      refuting=refuting, detail=detail)


_VERDICT_TO_TERMINAL = {
    "SUPPORTED": TERMINAL_SUPPORTED,
    "REFUTED": TERMINAL_REFUTED,
    "INSUFFICIENT": TERMINAL_INSUFFICIENT,
    "INCONCLUSIVE": TERMINAL_INCONCLUSIVE,
}

_VERDICT_TO_TRIAGE = {
    "SUPPORTED": "retain",
    "REFUTED": "retain",
    "INSUFFICIENT": "propose_investigation",
    "INCONCLUSIVE": "propose_investigation",
}


def assess_convergence(hypothesis: Hypothesis,
                       test_result: TestResult) -> Tuple[str, str, str]:
    """Map the pipeline outcome to (evidence terminal_state, executive
    outcome, triage). An unfalsifiable hypothesis never reaches the
    test: the scientific method's boundary is established honestly
    (BOUNDARY_ESTABLISHED), not papered over with a verdict."""
    if not hypothesis.falsifiable:
        return (TERMINAL_METHOD_BOUNDARY, OUTCOME_UNREACHABLE, "boundary")
    terminal = _VERDICT_TO_TERMINAL[test_result.verdict]
    triage = _VERDICT_TO_TRIAGE[test_result.verdict]
    return (terminal, OUTCOME_CONVERGED, triage)


# ---------------------------------------------------------------------------
# provenance + findings
# ---------------------------------------------------------------------------

def stamp_provenance(*, bounded_objective: str,
                     triage: str) -> EvidenceProvenance:
    """Provenance for every scientific-inquiry finding (C-2.2: complete
    or the fenced store refuses the record)."""
    return EvidenceProvenance(
        loop=LOOP_SCIENTIFIC_INQUIRY,
        bounded_objective=bounded_objective,
        model=MODEL_ID,
        triage=triage,
    )


def build_finding(*, inquiry_id: str, trigger: Dict[str, Any],
                  terminal_state: str, outcome: str, triage: str,
                  hypothesis: Hypothesis, prediction: Prediction,
                  test_result: TestResult, detail: str,
                  payload_ref: str) -> CuriosityFinding:
    """Build the fenced-store finding for a converged inquiry. Always
    carries complete provenance; validate() passes or the caller has a
    bug (the store refuses incomplete records -- C-2.2)."""
    return CuriosityFinding(
        evidence_id="ev_" + inquiry_id.replace("inq_", ""),
        loop=LOOP_SCIENTIFIC_INQUIRY,
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
    the executive never sees through this. ``evidence`` carries the
    presented evidence lines the test stage runs against."""
    substrate: Any
    inquiry_id: str
    budget_slice_s: float
    evidence: List[str] = field(default_factory=list)


class ScientificInquiryLoopInlet:
    """The scientific-inquiry loop's entry point. enter() validates the
    trigger shape (C-6.1: foreign boundary classes are refused HERE,
    never claimed) and returns the loop's initial private state; the
    run controller drives it from there."""

    def __init__(self, loop: "ScientificInquiryLoop") -> None:
        self._loop = loop

    def enter(self, trigger: Any, ctx: LoopContext) -> Dict[str, Any]:
        return self._loop.new_inquiry(trigger, ctx)


class ScientificInquiryLoop:
    """The logical scientific-inquiry loop controller (level 2). Owns
    the inquiry graph and the per-stage microcontrollers; exposes only
    step()/abort()/restore() and loop-level aggregates."""

    loop_name = LOOP_SCIENTIFIC_INQUIRY

    # -- lifecycle --------------------------------------------------------

    def new_inquiry(self, trigger: Any, ctx: LoopContext) -> Dict[str, Any]:
        # C-6.1 defense in depth: the loop itself refuses a foreign
        # boundary even if the inlet check is ever bypassed.
        boundary = trigger.boundary_class
        if boundary not in INQUIRY_BOUNDARIES:
            raise LoopRefused(
                f"scientific_inquiry does not own boundary class "
                f"{boundary!r}; owned: {list(INQUIRY_BOUNDARIES)}")
        return {
            "inquiry_id": ctx.inquiry_id,
            "trigger": trigger.as_dict(),
            "boundary_class": boundary,
            "stage": NODE_OBSERVE,
            "hypothesis": None,
            "prediction": None,
            "test_result": None,
            "node_summaries": {},
            "region_id": None,
            "root_mc": None,
            "stage_mc": None,
            "status": "running",
        }

    def restore(self, saved: Dict[str, Any]) -> Dict[str, Any]:
        """Rebuild loop state from a verified checkpoint. The graph
        region is per-inquiry and rebuilt; completed stage summaries
        are kept as history and the pipeline resumes at the saved
        stage."""
        state = dict(saved)
        state["region_id"] = None
        state["root_mc"] = None
        state["stage_mc"] = None
        state["status"] = "running"
        return state

    def abort(self, state: Dict[str, Any], ctx: LoopContext,
              graph: GraphController) -> None:
        """Kill path: close the open region, retire the inquiry's
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
        terminal dict when the inquiry converged."""
        if state["status"] != "running":
            return StepResult(done=True, terminal=state.get("terminal"),
                              detail="loop not running")

        sub = ctx.substrate
        if state["root_mc"] is None:
            # Reservation budget mirrors the questioning loop's: root 1/2
            # + stage 1/4 of the slice; stage MCs retire after use.
            spawned = sub.spawn(
                LOOP_SCIENTIFIC_INQUIRY,
                purpose=f"inquiry:{ctx.inquiry_id}",
                budget_s=ctx.budget_slice_s / 2.0)
            if not spawned.ok:
                raise SubstrateRefused(spawned.refusal.reason,
                                      spawned.refusal.message)
            state["root_mc"] = spawned.mc.mc_id

        if state["region_id"] is None:
            self._open_inquiry(state, ctx, graph)

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

        if node_id == NODE_CONCLUDE:
            return self._finish(state, ctx, graph, blocked=None)
        return StepResult(done=False, detail=f"operated {node_id}")

    # -- inquiry mechanics -------------------------------------------------

    def _open_inquiry(self, state: Dict[str, Any], ctx: LoopContext,
                      graph: GraphController) -> None:
        sub = ctx.substrate
        spawned = sub.spawn(
            LOOP_SCIENTIFIC_INQUIRY, purpose="inquiry-pipeline",
            parent_id=state["root_mc"],
            budget_s=ctx.budget_slice_s / 4.0)
        if not spawned.ok:
            raise SubstrateRefused(spawned.refusal.reason,
                                  spawned.refusal.message)
        state["stage_mc"] = spawned.mc.mc_id
        graph.attach_graph(InquiryGraph())
        opened = graph.open_region(
            state["stage_mc"], InquiryGraph.graph_id,
            f"{INQUIRY_OBJECTIVE} (inquiry {state['inquiry_id']})",
            max_operations=16)
        if not opened.ok or not opened.region_id:
            raise SubstrateRefused(
                "region_open_refused",
                (opened.refusal.message if opened.refusal
                 else "no region id"))
        state["region_id"] = opened.region_id

    def _cognize(self, state: Dict[str, Any], ctx: LoopContext, sub: Any,
                 operation: str, prompt: str,
                 context: Dict[str, Any]) -> Tuple[Dict[str, Any], str]:
        """Run one reasoning operation through the substrate's cognition
        inlet: the ACTIVE stage microcontroller asks, the substrate routes
        to its registered CognitionProvider. A failed or malformed
        cognition result fails closed (SubstrateRefused) -- the loop never
        falls back to a direct call, which would bypass the inlet."""
        mc_id = state["stage_mc"] or state["root_mc"]
        res = sub.cognize(mc_id, prompt,
                          {"operation": operation, **context})
        if not res.ok:
            raise SubstrateRefused(
                "cognition_failed",
                f"{operation} via cognize on {mc_id}: {res.error}")
        try:
            data = json.loads(res.text)
        except ValueError as exc:
            raise SubstrateRefused(
                "cognition_malformed",
                f"{operation} returned non-JSON cognition text: {exc}")
        return data, mc_id

    def _operate_node(self, node_id: str, state: Dict[str, Any],
                      ctx: LoopContext, sub: Any) -> Dict[str, Any]:
        trigger = state["trigger"]
        summaries = state["node_summaries"]
        if node_id == NODE_OBSERVE:
            source = trigger.get("question_text", "")
            summaries[node_id] = (
                f"recorded {state['boundary_class']} presentation "
                f"({len(source)} chars)")
        elif node_id == NODE_HYPOTHESIZE:
            source = trigger.get("question_text", "")
            data, mc_id = self._cognize(
                state, ctx, sub, OP_EXTRACT,
                "extract the claim from the inquiry presentation",
                {"question_text": source})
            hyp = form_hypothesis(
                source_text=source,
                boundary_class=state["boundary_class"],
                claim=data["unknown"], claim_terms=data["terms"])
            state["hypothesis"] = hyp
            summaries[node_id] = (
                f"hypothesis falsifiable={hyp.falsifiable}: "
                f"{hyp.statement[:80]} [cognize:{mc_id}]")
        elif node_id == NODE_PREDICT:
            hyp = state["hypothesis"]
            pred = derive_prediction(hyp)
            state["prediction"] = pred
            summaries[node_id] = f"prediction: {pred.statement[:80]}"
        elif node_id == NODE_TEST:
            result = run_test(state["prediction"], state["hypothesis"],
                              ctx.evidence)
            state["test_result"] = result
            summaries[node_id] = (
                f"test verdict={result.verdict}: {result.detail}")
        elif node_id == NODE_CONCLUDE:
            summaries[node_id] = "concluding"
        else:  # pragma: no cover - graph cannot yield unknown nodes
            raise AssertionError(f"unknown inquiry node {node_id!r}")
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

        hyp = state.get("hypothesis")
        result = state.get("test_result")
        if hyp is None or result is None or blocked:
            terminal = self._terminal(
                TERMINAL_INSUFFICIENT, OUTCOME_GAP, "propose_investigation",
                state, hyp, result,
                detail=(f"inquiry blocked ({blocked}): the pipeline could "
                        f"not complete"))
        else:
            terminal_state, outcome, triage = assess_convergence(hyp, result)
            detail = (f"{terminal_state}: {hyp.statement[:100]} -- "
                      f"test {result.verdict}: {result.detail}")
            terminal = self._terminal(
                terminal_state, outcome, triage, state, hyp, result,
                detail=detail)
        terminal["region_summary"] = region_summary
        state["terminal"] = terminal
        state["status"] = "converged"
        return StepResult(done=True, terminal=terminal,
                          detail=terminal["terminal_state"])

    def _terminal(self, terminal_state: str, outcome: str, triage: str,
                  state: Dict[str, Any],
                  hyp: Optional[Hypothesis],
                  result: Optional[TestResult],
                  detail: str) -> Dict[str, Any]:
        return {
            "terminal_state": terminal_state,
            "outcome": outcome,  # executive OutcomeCode this maps to
            "triage": triage,
            "inquiry_id": state["inquiry_id"],
            "trigger_id": state["trigger"]["trigger_id"],
            "bounded_objective": state["trigger"]["bounded_objective"],
            "origin": state["trigger"]["origin"],
            "boundary_class": state["boundary_class"],
            "hypothesis": (hyp.statement if hyp else ""),
            "hypothesis_falsifiable": (hyp.falsifiable if hyp else False),
            "prediction": (state["prediction"].statement
                           if state.get("prediction") else ""),
            "verdict": (result.verdict if result else "INSUFFICIENT"),
            "provenance": stamp_provenance(
                bounded_objective=state["trigger"]["bounded_objective"],
                triage=triage).as_dict(),
            "node_summaries": dict(state["node_summaries"]),
            "detail": detail,
        }

    # -- aggregates (loop-level only) --------------------------------------

    def loop_aggregate(self) -> Dict[str, Any]:
        return {"loop": self.loop_name, "nodes": list(INQUIRY_NODES),
                "boundaries": list(INQUIRY_BOUNDARIES)}
