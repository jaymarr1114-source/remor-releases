"""
swarm_engine/cognition/learning.py

Learning (H): mutating the same SearchBias, ConceptGraph, and CaseMemory that
synthesis and reasoning read from, so a future search is genuinely different
because of past experience — not a record appended for a human to read later.

Concept induction is deliberately narrow: a Concept only forms when the exact
same expression-tree STRUCTURE independently verifies against two or more
DISTINCT goals — literal structural recurrence (now including argument
position, since `add(abs(a), b)` and `add(a, abs(b))` are different trees
even though they share the same two ops), not an inferred general principle.
"""
from __future__ import annotations

from typing import Any, Dict, Optional

from swarm_engine.cognition.representations import (
    CaseMemory, Concept, ConceptGraph, Expr, Hypothesis, SearchBias,
)


class Learner:
    def __init__(self, bias: SearchBias, concepts: ConceptGraph, cases: CaseMemory):
        self.bias = bias
        self.concepts = concepts
        self.cases = cases
        # Attached by the engine when representation expansion is wired in:
        # a ReificationObserver that reifies recurring sub-computations into
        # promoted primitives. None keeps this class usable standalone.
        self.reifier = None

    def record_outcome(self, goal: str, hypothesis: Optional[Hypothesis],
                       succeeded: bool,
                       examples: Optional[Any] = None,
                       param_names: Optional[Any] = None) -> Dict[str, Any]:
        report: Dict[str, Any] = {"bias_updated": False, "concept_formed": None,
                                  "case_remembered": False}
        if hypothesis is None:
            return report

        if hypothesis.origin == "synthesis":
            self.bias.record(hypothesis.op_sequence, succeeded)
            report["bias_updated"] = True

        if not succeeded:
            return report

        self.cases.remember(goal, hypothesis.op_sequence, hypothesis.plan,
                            expr=hypothesis.expr, examples=examples,
                            param_names=param_names)
        report["case_remembered"] = True

        if hypothesis.expr is not None:
            concept = self._try_induce(hypothesis.expr, goal)
            if concept is not None:
                report["concept_formed"] = concept.as_dict()
            # Autonomous representation expansion: recurring
            # sub-computations across distinct verified goals earn
            # reification into registered primitives. Fail-closed and
            # never allowed to break the success path that got here.
            reifier = getattr(self, "reifier", None)
            if reifier is not None:
                try:
                    reified = reifier.observe(hypothesis.expr, goal,
                                              examples, param_names)
                    if reified:
                        report["reified"] = reified
                except Exception as exc:
                    report["reify_error"] = f"{type(exc).__name__}: {exc}"
        return report

    def _try_induce(self, expr: Expr, goal: str) -> Optional[Concept]:
        """Vocabulary expansion via abstraction (items 3/6): once the SAME
        tree independently solves two distinct goals, it is promoted into a
        Concept — which `ReasoningEngine._try_deduction` can then apply
        directly to a THIRD, structurally unrelated-by-wording problem
        without re-deriving it by search. That promoted concept becoming a
        usable building block for future problems is the actual mechanism
        of "capability growth" here — bounded to exact structural recurrence,
        not a claim of inventing a new primitive from nothing.
        """
        existing = self.concepts.find(expr)
        already_known = set(existing.example_goals) if existing else set()
        canonical = expr.canonical()
        prior_goals = set()
        for _, entry in self.cases.all():
            if (entry.expr is not None and entry.expr.canonical() == canonical
                    and entry.goal != goal):
                prior_goals.add(entry.goal)
        if not prior_goals:
            return None

        concept = existing
        for prior in sorted(prior_goals - already_known):
            concept = self.concepts.reinforce(expr, prior)
        if goal not in (concept.example_goals if concept else []):
            concept = self.concepts.reinforce(expr, goal)
        return concept
