"""
swarm_engine/cognition/reasoning.py

The four reasoning services and their selection order.

"Grammar-based synthesis" (the fourth service G names) is CompositionalSynthesizer
itself, invoked as the broadest, most expensive fallback. Deduction is the same
search mechanism narrowed by a known Concept (a verified general pattern applied
to a new instance). Analogy is case retrieval plus a named, bounded form of
adaptation — never general relational structure-mapping.

Selection order, cheapest/narrowest first: ANALOGY -> DEDUCTION -> SYNTHESIS.
This mirrors the existing Activator's "cheapest sufficient check first"
discipline rather than introducing a new principle.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

from swarm_engine.cognition.representations import (
    CaseMemory, Concept, ConceptGraph, Expr, Hypothesis, SearchBias,
)
from swarm_engine.cognition.synthesis import (
    CompositionalSynthesizer, ExpressivenessAnalyzer, GeneralSynthesizer,
)


@dataclass
class ReasoningResult:
    hypothesis: Optional[Hypothesis]
    stages_tried: List[str] = field(default_factory=list)
    candidates_tried: int = 0
    case_id: Optional[int] = None
    unsatisfiable_args: List[Dict[str, Any]] = field(default_factory=list)
    # Identifiability verdicts from synthesis, surfaced distinctly: an
    # ambiguous conditional is a different outcome from "search ran out
    # of budget", and contradictory training data is a property of the
    # evidence, not a search failure.
    ambiguous: List[Dict[str, Any]] = field(default_factory=list)
    contradiction: Optional[str] = None

    def as_dict(self) -> Dict[str, Any]:
        return {"found": self.hypothesis is not None,
                "origin": self.hypothesis.origin if self.hypothesis else None,
                "op_sequence": list(self.hypothesis.op_sequence) if self.hypothesis else None,
                "stages_tried": self.stages_tried,
                "candidates_tried": self.candidates_tried,
                "unsatisfiable_args": list(self.unsatisfiable_args),
                "ambiguous": list(self.ambiguous),
                "contradiction": self.contradiction}


def _lexical_overlap(a: str, b: str) -> float:
    wa, wb = set(a.lower().split()), set(b.lower().split())
    if not wa or not wb:
        return 0.0
    return len(wa & wb) / len(wa | wb)


class ReasoningEngine:
    """Wires the four services together with an explicit, cheapest-first
    selection order.

    Synthesis now routes through GeneralSynthesizer for any number of
    parameters — the earlier `len(param_names) in {1, 2}` branching (one
    hand-written search function per arity) is gone. Analogy and deduction
    now test candidates by executing the actual stored Composer plan against
    the new examples, rather than reconstructing execution from a flattened
    op-sequence — this is what lets both work correctly for tree-shaped
    hypotheses (nested, multi-argument) and not just the old linear chains.
    """

    def __init__(self, registry, bias: SearchBias, concepts: ConceptGraph,
                 cases: CaseMemory, max_size: int = 3, max_candidates: int = 20000):
        self.reg = registry
        self.bias = bias
        self.concepts = concepts
        self.cases = cases
        self.synthesizer = GeneralSynthesizer(
            registry, bias, max_size=max_size, max_candidates=max_candidates)
        self.expressiveness = ExpressivenessAnalyzer(registry)
        from swarm_engine.synthesis.composer import Composer
        self._composer = Composer(registry)

    def solve(self, goal: str, examples: Sequence[Tuple[Dict[str, Any], Any]],
             param_name: str) -> ReasoningResult:
        return self.solve_multi(goal, examples, (param_name,))

    def solve_multi(self, goal: str, examples: Sequence[Tuple[Dict[str, Any], Any]],
                    param_names: Sequence[str]) -> ReasoningResult:
        """Any number of parameters — analogy and deduction first (cheapest),
        the general arity-agnostic search last (broadest)."""
        result = ReasoningResult(hypothesis=None)

        result.stages_tried.append("analogical")
        analogy, case_id = self._try_analogy(examples, param_names, goal)
        if analogy is not None:
            result.hypothesis = analogy
            result.case_id = case_id
            return result

        result.stages_tried.append("deductive")
        deduction = self._try_deduction(examples, param_names)
        if deduction is not None:
            result.hypothesis = deduction
            return result

        result.stages_tried.append("synthesis")
        extra_literals = self._derive_extra_literals(examples, param_names)
        oracle = getattr(self, "_synthesis_oracle", None)
        hyp, trace = self.synthesizer.search(
            examples, param_names,
            extra_literals=tuple(extra_literals),
            oracle=oracle)
        result.hypothesis = hyp
        result.candidates_tried = trace.candidates_tried
        result.unsatisfiable_args = list(getattr(trace, "unsatisfiable_args", None) or [])
        result.ambiguous = list(getattr(trace, "ambiguous", None) or [])
        result.contradiction = getattr(trace, "contradiction", None)
        return result

    @staticmethod
    def _derive_extra_literals(examples: Sequence[Tuple[Dict[str, Any], Any]],
                                param_names: Sequence[str]) -> set:
        """Evidence-derived candidate constants, delegated to the shared
        miner (cognition/constants.py) so there is exactly one derivation
        implementation. The search itself also mines internally; the union
        is deduplicated by value, so overlap here is harmless."""
        from swarm_engine.cognition.constants import mine_constants
        return {m.value for m in mine_constants(examples, param_names)}

    def _plan_matches(self, plan: Dict[str, Any],
                      examples: Sequence[Tuple[Dict[str, Any], Any]]) -> bool:
        """Test a stored plan against new examples via the real Composer —
        the same executor everything else in the engine uses, so this works
        identically for a linear chain, a nested tree, or any arity."""
        for args, expected in examples:
            try:
                out = self._composer.execute_sync(plan, dict(args))
            except Exception:
                return False
            if not out.get("success") or out.get("value") != expected:
                return False
        return True

    def _try_analogy(self, examples, param_names, goal
                     ) -> Tuple[Optional[Hypothesis], Optional[int]]:
        cases = self.cases.all()
        if not cases:
            return None, None
        ranked = sorted(cases, key=lambda ic: -_lexical_overlap(goal, ic[1].goal))
        best_id, best_case = ranked[0]
        if _lexical_overlap(goal, best_case.goal) <= 0.0:
            return None, None

        if self._plan_matches(best_case.plan, examples):
            return Hypothesis(plan=best_case.plan, op_sequence=best_case.op_sequence,
                              origin="analogical", expr=best_case.expr,
                              derivation=f"reused case {best_id} verbatim "
                                        f"(goal similarity to {best_case.goal!r})"), best_id

        if self.cases.adaptation_already_failed(best_id, goal):
            return None, None

        if len(param_names) == 1 and len(best_case.op_sequence) >= 1:
            adapted = self._slot_substitute(best_case, examples, param_names[0])
            if adapted is not None:
                return adapted, best_id

        self.cases.mark_adaptation_failed(best_id, goal)
        return None, None

    def _slot_substitute(self, case, examples, param_name
                         ) -> Optional[Hypothesis]:
        """Bounded, explicitly narrow: single-position substitution over a
        LINEAR chain only, using the old unary-chain synthesizer as a plan
        builder for this one purpose. Tree-shaped or multi-parameter cases
        fall through to full synthesis instead — a real, stated limitation,
        not silently attempted and failed."""
        sequence = case.op_sequence
        linear_helper = CompositionalSynthesizer(self.reg, self.bias)
        for i, op in enumerate(sequence):
            for alt in self._same_role_alternatives(op):
                candidate = list(sequence)
                candidate[i] = alt
                plan = linear_helper._build_plan(candidate, param_name)
                if self._plan_matches(plan, examples):
                    return Hypothesis(
                        plan=plan, op_sequence=tuple(candidate), origin="analogical",
                        derivation=f"adapted case by substituting {op!r} -> {alt!r} "
                                  f"at position {i} (same input/output kind)")
        return None

    def _same_role_alternatives(self, op: str) -> List[str]:
        target = self.reg.get(op)
        if target is None:
            return []
        target_required = [k for k, v in target.inputs.items() if not v.optional]
        if len(target_required) != 1:
            return []
        target_in_kind = target.inputs[target_required[0]].kind
        out = []
        for name in self.synthesizer._ops:
            if name == op:
                continue
            candidate = self.reg.get(name)
            required = [k for k, v in candidate.inputs.items() if not v.optional]
            if len(required) != 1:
                continue
            in_kind = candidate.inputs[required[0]].kind
            if in_kind == target_in_kind and candidate.output.kind == target.output.kind:
                out.append(name)
        return out

    def _try_deduction(self, examples, param_names) -> Optional[Hypothesis]:
        for concept in sorted(self.concepts.all(), key=lambda c: -c.support):
            for plan in self._compile_concept_permutations(concept, param_names):
                if self._plan_matches(plan, examples):
                    return Hypothesis(
                        plan=plan, op_sequence=concept.op_sequence, origin="deductive",
                        derivation=f"applied concept {concept.concept_id} "
                                  f"(support={concept.support}) directly to this "
                                  f"instance", expr=concept.expr)
        return None

    def _compile_concept_permutations(self, concept: Concept, param_names: Sequence[str]
                                      ):
        """A concept's own leaf parameter names carry no reliable
        correspondence to a new problem's parameter order — the order a
        tree traversal happens to discover leaves in is an artifact of tree
        structure, not a semantic alignment (`add(a=leaf_b, b=abs(leaf_a))`
        discovers 'b' before 'a', which is not the same thing as "the first
        problem parameter maps to the first concept parameter"). Rebinding
        by that discovery order alone produced a wrong mapping and a
        deduction that failed even though the concept was exactly the right
        shape. Trying every permutation and keeping whichever one the
        examples actually confirm is small (concept arity is always a few
        parameters at most in this system) and correct, where guessing one
        fixed order was neither.
        """
        import itertools
        concept_params = self._leaf_params(concept.expr)
        if len(concept_params) > len(param_names):
            return
        for ordering in itertools.permutations(param_names, len(concept_params)):
            rebinding = dict(zip(concept_params, ordering))
            rebound = self._rebind(concept.expr, rebinding)
            yield self.synthesizer._build_general_plan(rebound, param_names)

    def _leaf_params(self, expr: Expr) -> List[str]:
        if expr.is_leaf():
            return [] if expr.is_literal else [expr.param]
        out: List[str] = []
        for _, child in expr.children:
            for p in self._leaf_params(child):
                if p not in out:
                    out.append(p)
        return out

    def _rebind(self, expr: Expr, mapping: Dict[str, str]) -> Expr:
        if expr.is_leaf():
            if expr.is_literal:
                return expr  # literal leaves carry no parameter to rebind
            return Expr(param=mapping.get(expr.param, expr.param))
        return Expr(op=expr.op, children=tuple(
            (name, self._rebind(child, mapping)) for name, child in expr.children))
