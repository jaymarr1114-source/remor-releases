"""The Questioning loop: converges imprecise questions into precise,
checkable form through a structural refinement graph.

The loop owns a refinement graph (nodes below) operated through the real
GraphController, with each refinement pass running inside microcontrollers
on the curiosity substrate (one pass MC per pass, nested under the
inquiry's root MC; the corpus search runs in a child MC of the pass MC --
real nesting, real per-unit budgets).

Reasoning model note (D-8): there is no separate curiosity mind. The
loop's reasoning operations -- extract the unknown, compose the precise
question, score it on the checklist -- travel the substrate's real
cognition inlet: the active pass microcontroller calls
MicrocontrollerSubstrate.cognize(), and the substrate routes the request
to its registered CognitionProvider (PrecisionCognitionProvider, a
deterministic mechanical precision reasoner; see
swarm_engine/curiosity/cognition.py). No external reasoning model is
consulted, and no answers are hard-coded: the provider processes
arbitrary question text. Corpus probing stays the loop's own retrieval
machinery (not reasoning).

Convergence rules (checkable):
  - precision score 3/3 -> QUESTION_RESOLVED (precise question +
    investigation path named).
  - max passes reached, score < 3:
      - no observable phrase in the question AND no corpus passage in any
        pass -> BOUNDARY_ESTABLISHED: names the missing means (what would
        have to become observable).
      - otherwise -> INSUFFICIENT_EVIDENCE (the question pointed at
        evidence; the available corpus does not carry it).
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from swarm_engine.core.graph_controller.controller import GraphController
from swarm_engine.core.microcontroller.substrate import R_UNKNOWN_MC
from swarm_engine.curiosity.cognition import (
    OP_COMPOSE,
    OP_EXTRACT,
    OP_SCORE,
    _content_terms,
    compose_precise_question,  # re-exported: legacy import path
    extract_scope,
    extract_unknown,           # re-exported: legacy import path
    mentions_observable,       # re-exported: legacy import path
    precision_score,           # re-exported: legacy import path
)
from swarm_engine.curiosity.substrate import LOOP_QUESTIONING


class SubstrateRefused(Exception):
    """The substrate refused a spawn or exhausted a microcontroller
    mid-step. The run controller converts this into the governed
    RESOURCE_BOUNDARY path (checkpoint + suspend + BLOCKED finding) --
    it is never swallowed here."""

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(f"{reason}: {message}")
        self.reason = reason
        self.message = message

# ---------------------------------------------------------------------------
# refinement graph shape
# ---------------------------------------------------------------------------

NODE_EXTRACT = "extract_unknown"
NODE_PROBE = "probe_observable"
NODE_SCOPE = "probe_scope"
NODE_COMPOSE = "compose"
NODE_SCORE = "score"

REFINEMENT_NODES = (
    NODE_EXTRACT, NODE_PROBE, NODE_SCOPE, NODE_COMPOSE, NODE_SCORE)

NODE_DEPENDENCIES = {
    NODE_EXTRACT: (),
    NODE_PROBE: (NODE_EXTRACT,),
    NODE_SCOPE: (NODE_EXTRACT,),
    NODE_COMPOSE: (NODE_EXTRACT, NODE_PROBE, NODE_SCOPE),
    NODE_SCORE: (NODE_COMPOSE,),
}

#: Node goals, worded so the GraphController's relevance selection admits
#: the refinement chain for a refinement objective (dependency closure
#: pulls the whole chain once the head matches; every goal independently
#: clears the controller's 0.34 relevance floor).
NODE_GOALS = {
    NODE_EXTRACT: ("extract the unknown phrase from the imprecise "
                   "question to refine it"),
    NODE_PROBE: ("probe the corpus for observable passages to refine "
                 "the imprecise question"),
    NODE_SCOPE: ("extract scope hints from the imprecise question to "
                 "refine it"),
    NODE_COMPOSE: ("compose a precise checkable question from the slots "
                   "to refine the imprecise question"),
    NODE_SCORE: ("score the precise checkable question on the precision "
                 "checklist"),
}

REFINEMENT_OBJECTIVE = ("refine the imprecise question into a precise "
                        "checkable question")


class RefinementGraph:
    """The structural refinement graph, adapted to the GraphController's
    StructuralGraph protocol. The controller never touches graph
    internals; the loop never bypasses the controller."""

    graph_id = "curiosity/questioning/refinement/v1"

    def node_ids(self) -> List[str]:
        return list(REFINEMENT_NODES)

    def node_goal(self, node_id: str) -> str:
        return NODE_GOALS[node_id]

    def dependencies(self, node_id: str) -> List[str]:
        return list(NODE_DEPENDENCIES[node_id])

TERMINAL_RESOLVED = "QUESTION_RESOLVED"
TERMINAL_BOUNDARY = "BOUNDARY_ESTABLISHED"
TERMINAL_INSUFFICIENT = "INSUFFICIENT_EVIDENCE"

MAX_PASSES = 3

# ---------------------------------------------------------------------------
# corpus probing: real retrieval over a bounded document set
# ---------------------------------------------------------------------------
#
# NOTE: the mechanical precision machinery (extract_unknown,
# compose_precise_question, precision_score, ...) lives in
# swarm_engine.curiosity.cognition, behind the CognitionProvider
# protocol. The reasoning nodes below reach it ONLY through the
# substrate's cognition inlet (substrate.cognize on the active pass
# microcontroller) -- never by direct call.

class CorpusIndex:
    """Inverted index over bounded document lines. Deterministic;
    passage ranking is term-overlap order (documented, not learned)."""

    def __init__(self, docs: List[str]) -> None:
        self._lines: List[str] = []
        self._postings: Dict[str, List[int]] = {}
        for doc in docs:
            for line in doc.splitlines():
                line = line.strip()
                if len(line) < 24:
                    continue
                idx = len(self._lines)
                self._lines.append(line)
                for tok in set(_content_terms(line)):
                    self._postings.setdefault(tok, []).append(idx)

    def search(self, terms: List[str], top_k: int = 3
               ) -> List[Tuple[str, int]]:
        """Return (line, overlap) for the top_k lines by term overlap."""
        scores: Dict[int, int] = {}
        for tok in terms:
            for idx in self._postings.get(tok, ()):  # exact-term postings
                scores[idx] = scores.get(idx, 0) + 1
        ranked = sorted(scores.items(), key=lambda kv: (-kv[1], kv[0]))
        return [(self._lines[i], s) for i, s in ranked[:top_k] if s > 0]

    @property
    def line_count(self) -> int:
        return len(self._lines)


# ---------------------------------------------------------------------------
# loop state + inlet
# ---------------------------------------------------------------------------

@dataclass
class StepResult:
    done: bool
    terminal: Optional[Dict[str, Any]] = None
    detail: str = ""


@dataclass
class LoopContext:
    """What one loop step may touch. The loop never sees the executive;
    the executive never sees through this."""
    substrate: Any
    inquiry_id: str
    budget_slice_s: float
    corpus: CorpusIndex


class QuestioningLoopInlet:
    """The questioning loop's entry point. enter() validates the trigger
    shape and returns the loop's initial private state; the run
    controller drives it from there."""

    def __init__(self, loop: "QuestioningLoop") -> None:
        self._loop = loop

    def enter(self, trigger: Any, ctx: LoopContext) -> Dict[str, Any]:
        return self._loop.new_inquiry(trigger, ctx)


class QuestioningLoop:
    """The logical questioning-loop controller (level 2). Owns the
    refinement graph and the per-pass microcontrollers; exposes only
    step()/abort()/restore() and loop-level aggregates."""

    loop_name = LOOP_QUESTIONING

    def __init__(self, *, max_passes: int = MAX_PASSES) -> None:
        self._max_passes = max_passes

    # -- lifecycle --------------------------------------------------------

    def new_inquiry(self, trigger: Any, ctx: LoopContext) -> Dict[str, Any]:
        return {
            "inquiry_id": ctx.inquiry_id,
            "trigger": trigger.as_dict(),
            "pass": 0,
            "max_passes": self._max_passes,
            "slots": {"unknown": None, "observable": None,
                      "scopes": [], "context_terms": [],
                      "observable_mentioned": False},
            "passes": [],
            "current": None,          # in-flight pass record
            "region_id": None,
            "root_mc": None,
            "pass_mc": None,
            "status": "running",
        }

    def restore(self, saved: Dict[str, Any]) -> Dict[str, Any]:
        """Rebuild loop state from a verified checkpoint. The graph
        region is per-pass and rebuilt; completed passes are kept as
        history and the next pass continues from the last slots."""
        state = dict(saved)
        state["current"] = None
        state["region_id"] = None
        state["root_mc"] = None
        state["pass_mc"] = None
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
            # close_region returns a Refusal value for unknown regions --
            # never raises.
            graph.close_region(state["region_id"])
            state["region_id"] = None
        for mc_id in (state.get("pass_mc"), state.get("root_mc")):
            if mc_id:
                sub.retire(mc_id)
        state["root_mc"] = state["pass_mc"] = None
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
            # Reservation budget: the inquiry tree's peak LIVE reservation
            # is root 1/2 + pass 1/4 + probe 1/16 = 13/16 of the slice
            # (children are retired after use, releasing their
            # reservations). 13/16 < 1, so max_concurrent inquiries never
            # over-reserve the grant-sized loop pool. The root itself is
            # ~never charged (node elapsed goes to the pass MC); its
            # reservation is the anchor, not the spend authority -- exact
            # spend enforcement stays with the run controller's slice
            # accounting in _step_inquiry.
            spawned = sub.spawn(
                LOOP_QUESTIONING, purpose=f"inquiry:{ctx.inquiry_id}",
                budget_s=ctx.budget_slice_s / 2.0)
            if not spawned.ok:
                raise SubstrateRefused(spawned.refusal.reason,
                                      spawned.refusal.message)
            state["root_mc"] = spawned.mc.mc_id

        if state["region_id"] is None:
            self._open_pass(state, ctx, graph)

        region_id = state["region_id"]
        nxt = graph.next_operable(region_id)
        if not nxt.ok:
            # No operable node: the pass cannot proceed -- finish it.
            return self._finish_pass(state, ctx, graph, blocked=nxt.status)

        node_id = nxt.node_id
        t0 = time.monotonic()
        value = self._operate_node(node_id, state, ctx, sub)
        elapsed = time.monotonic() - t0
        active_mc = state["pass_mc"] or state["root_mc"]
        exhausted, charge_state = sub.charge(active_mc, elapsed)
        if charge_state == R_UNKNOWN_MC:
            pass  # retired mid-step; the run controller still accounts time
        elif exhausted:
            raise SubstrateRefused(
                "microcontroller_exhausted",
                f"pass microcontroller exhausted charging {elapsed:.4f}s "
                f"(state={charge_state})")
        graph.record_result(region_id, node_id, success=True,
                            value={"node": node_id,
                                   "summary": value.get("summary", "")})

        if node_id == NODE_SCORE:
            return self._finish_pass(state, ctx, graph, blocked=None)
        return StepResult(done=False, detail=f"operated {node_id}")

    # -- pass mechanics ----------------------------------------------------

    def _open_pass(self, state: Dict[str, Any], ctx: LoopContext,
                   graph: GraphController) -> None:
        sub = ctx.substrate
        pass_no = state["pass"]
        spawned = sub.spawn(
            LOOP_QUESTIONING, purpose=f"refine-pass:{pass_no}",
            parent_id=state["root_mc"],
            budget_s=ctx.budget_slice_s / 4.0)
        if not spawned.ok:
            raise SubstrateRefused(spawned.refusal.reason,
                                  spawned.refusal.message)
        state["pass_mc"] = spawned.mc.mc_id
        # Expanded terms on later passes: the composed question feeds back
        # into retrieval -- genuine iterative refinement. De-duplicated:
        # a term occurring twice must not count double in overlap
        # scoring (that let a single shared word pose as a substantive
        # passage match).
        seed_terms = list(state["slots"]["context_terms"])
        if state["passes"]:
            seed_terms += _content_terms(
                state["passes"][-1].get("precise_question", ""))
        deduped: List[str] = []
        for term in seed_terms:
            if term not in deduped:
                deduped.append(term)
        seed_terms = deduped
        graph.attach_graph(RefinementGraph())
        opened = graph.open_region(
            state["pass_mc"], RefinementGraph.graph_id,
            f"{REFINEMENT_OBJECTIVE} (pass {pass_no})",
            max_operations=16)
        if not opened.ok or not opened.region_id:
            raise SubstrateRefused(
                "region_open_refused",
                (opened.refusal.message if opened.refusal
                 else "no region id"))
        state["region_id"] = opened.region_id
        state["current"] = {"pass": pass_no, "seed_terms": seed_terms[:12],
                            "probes": [], "node_summaries": {}}

    def _cognize(self, state: Dict[str, Any], ctx: LoopContext, sub: Any,
                 operation: str, prompt: str,
                 context: Dict[str, Any]) -> Tuple[Dict[str, Any], str]:
        """Run one reasoning operation through the substrate's cognition
        inlet: the ACTIVE pass microcontroller asks, the substrate routes
        to its registered CognitionProvider. A failed or malformed
        cognition result fails closed (SubstrateRefused) -- the loop never
        falls back to a direct call, which would bypass the inlet."""
        mc_id = state["pass_mc"] or state["root_mc"]
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
        slots = state["slots"]
        trigger = state["trigger"]
        cur = state["current"]
        if node_id == NODE_EXTRACT:
            data, mc_id = self._cognize(
                state, ctx, sub, OP_EXTRACT,
                "extract the unknown from the imprecise question",
                {"question_text": trigger["question_text"]})
            slots["unknown"] = data["unknown"]
            slots["context_terms"] = data["terms"]
            slots["observable_mentioned"] = data["observable_mentioned"]
            summary = (f"unknown={data['unknown']!r} "
                       f"terms={data['terms'][:8]} [cognize:{mc_id}]")
        elif node_id == NODE_PROBE:
            terms = list(cur.get("seed_terms") or slots["context_terms"])
            spawned = sub.spawn(
                LOOP_QUESTIONING, purpose="corpus-search",
                parent_id=state["pass_mc"],
                budget_s=ctx.budget_slice_s / 16.0)
            if not spawned.ok:
                raise SubstrateRefused(spawned.refusal.reason,
                                      spawned.refusal.message)
            probe_mc = spawned.mc.mc_id
            try:
                passages = ctx.corpus.search(terms, top_k=3)
            finally:
                sub.retire(probe_mc)
            cur["probes"].append(
                {"terms": terms[:12],
                 "hits": [{"line": ln, "overlap": ov}
                          for ln, ov in passages]})
            # Only a substantive match counts as a genuine observable path:
            # overlap >= 2 AND at least one of the unknown's own terms in
            # the passage. The second clause blocks template-vocabulary
            # pollution: later passes expand seed terms with the composed
            # question's words ("observable evidence"), which must not let
            # a passage about the evidence machinery qualify as an
            # observable path for an unrelated unknown.
            unknown_terms = set(_content_terms(slots["unknown"] or ""))
            qualifying = [
                ln for ln, ov in passages
                if ov >= 2 and (unknown_terms & set(_content_terms(ln)))]
            cur["qualifying_hits"] = len(qualifying)
            if qualifying:
                slots["observable"] = qualifying[0]
            summary = (f"{len(passages)} passage(s), "
                       f"{len(qualifying)} qualifying, for {terms[:6]}")
        elif node_id == NODE_SCOPE:
            slots["scopes"] = extract_scope(trigger["question_text"])
            summary = f"scopes={slots['scopes']}"
        elif node_id == NODE_COMPOSE:
            data, mc_id = self._cognize(
                state, ctx, sub, OP_COMPOSE,
                "compose the precise checkable question from the slots",
                {"unknown": slots["unknown"],
                 "observable": slots["observable"],
                 "scopes": slots["scopes"]})
            cur["precise_question"] = data["precise_question"]
            summary = cur["precise_question"][:80] + f" [cognize:{mc_id}]"
        elif node_id == NODE_SCORE:
            data, mc_id = self._cognize(
                state, ctx, sub, OP_SCORE,
                "score the precise question on the precision checklist",
                {"precise_question": cur.get("precise_question", ""),
                 "unknown": slots["unknown"] or "",
                 "observable": slots["observable"]})
            cur["score"] = data["score"]
            cur["checks"] = data["checks"]
            summary = f"precision {data['score']}/3 [cognize:{mc_id}]"
        else:  # pragma: no cover - graph cannot yield unknown nodes
            raise AssertionError(f"unknown refinement node {node_id!r}")
        cur["node_summaries"][node_id] = summary
        return {"summary": summary}

    def _finish_pass(self, state: Dict[str, Any], ctx: LoopContext,
                     graph: GraphController,
                     blocked: Optional[str]) -> StepResult:
        cur = state["current"] or {}
        slots = state["slots"]
        region_id = state["region_id"]
        region_summary = None
        if region_id:
            from swarm_engine.core.graph_controller.controller import (
                RegionSummary)
            closed = graph.close_region(region_id)
            if isinstance(closed, RegionSummary):
                region_summary = closed.as_dict()
            state["region_id"] = None
        # Retire the pass MC (children cascade automatically, releasing
        # their reservations); the root MC persists across passes.
        if state.get("pass_mc"):
            ctx.substrate.retire(state["pass_mc"])
            state["pass_mc"] = None
        score = cur.get("score", 0)
        state["passes"].append({
            "pass": state["pass"],
            "score": score,
            "checks": cur.get("checks", []),
            "precise_question": cur.get("precise_question", ""),
            "probes": cur.get("probes", []),
            "qualifying_hits": cur.get("qualifying_hits", 0),
            "node_summaries": cur.get("node_summaries", {}),
            "blocked": blocked,
            "region_summary": region_summary,
        })
        state["current"] = None

        if score >= 3:
            terminal = self._terminal(
                TERMINAL_RESOLVED, state,
                detail=(f"precision 3/3 on pass {state['pass']}: "
                        f"{cur.get('precise_question', '')}"))
            state["terminal"] = terminal
            state["status"] = "converged"
            return StepResult(done=True, terminal=terminal,
                              detail="QUESTION_RESOLVED")

        if state["pass"] + 1 >= state["max_passes"]:
            terminal = self._terminal_unresolved(state)
            state["terminal"] = terminal
            state["status"] = "converged"
            return StepResult(done=True, terminal=terminal,
                              detail=terminal["terminal_state"])

        state["pass"] += 1
        return StepResult(
            done=False,
            detail=(f"pass {state['pass'] - 1} scored {score}/3; "
                    f"refining (pass {state['pass']})"))

    def _terminal(self, terminal_state: str, state: Dict[str, Any],
                  detail: str) -> Dict[str, Any]:
        slots = state["slots"]
        last = state["passes"][-1] if state["passes"] else {}
        return {
            "terminal_state": terminal_state,
            "inquiry_id": state["inquiry_id"],
            "trigger_id": state["trigger"]["trigger_id"],
            "bounded_objective": state["trigger"]["bounded_objective"],
            "origin": state["trigger"]["origin"],
            "precise_question": last.get("precise_question", ""),
            "score": last.get("score", 0),
            "passes": state["passes"],
            "slots": dict(slots),
            "detail": detail,
        }

    def _terminal_unresolved(self, state: Dict[str, Any]) -> Dict[str, Any]:
        slots = state["slots"]
        any_qualifying = any(p.get("qualifying_hits")
                             for p in state["passes"])
        if not any_qualifying and not slots["observable_mentioned"]:
            unknown = slots["unknown"] or "the question's subject"
            detail = (
                f"no observable path for {unknown!r} in the available "
                f"corpus across {state['max_passes']} passes, and the "
                f"question names none: resolving it would require "
                f"{unknown!r} to be made observable via an operational "
                f"definition tied to observable events")
            return self._terminal(TERMINAL_BOUNDARY, state, detail)
        detail = (
            f"precision {state['passes'][-1].get('score', 0)}/3 after "
            f"{state['max_passes']} passes: the question points at "
            f"evidence but the available corpus carries no substantive "
            f"passage for it")
        return self._terminal(TERMINAL_INSUFFICIENT, state, detail)

    # -- aggregates (loop-level only) --------------------------------------

    def loop_aggregate(self) -> Dict[str, Any]:
        return {"loop": self.loop_name, "nodes": list(REFINEMENT_NODES),
                "max_passes": self._max_passes}
