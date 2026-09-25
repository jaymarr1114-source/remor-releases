"""
swarm_engine/intellect/reasoner.py

Two things, kept structurally separate on purpose:

ExternalReasoner — the injection point for a real reasoning model, should
one ever be connected. Optional, model-agnostic, no Claude/OpenAI/etc
hardcoded anywhere. Every method has a corresponding internal fallback in
IntellectualEngine, used when no reasoner is attached. This is the honest
boundary: where SWarm's own mechanisms genuinely cannot generate a good
question, hypothesis, or experiment design, that limitation is visible here
as an unimplemented capability, not hidden behind a hardcoded rule dressed
up as reasoning.

EvidenceArbiter — decides hypothesis status from evidence ALONE, never from
the hypothesis's own stated confidence or the reasoner's own claim. Mirrors
verification/independent.py's Arbiter.decide(evidence) — the method
signature doesn't accept anything but evidence, which is the whole reason
that shape resists self-certification.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from swarm_engine.intellect.epistemic import Evidence, HypothesisState


class ExternalReasoner(ABC):
    @abstractmethod
    def propose_questions(self, context: Dict[str, Any]) -> List[str]:
        ...

    @abstractmethod
    def propose_hypotheses(self, question_text: str, context: Dict[str, Any]
                           ) -> List[str]:
        ...

    @abstractmethod
    def design_experiment(self, question_text: str, hypothesis_statements: List[str],
                          context: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        ...


class NoExternalReasoner(ExternalReasoner):
    """The default. Returns nothing, explicitly — not a stub that silently
    produces empty-but-plausible-looking output. IntellectualEngine checks
    for empty results and falls through to its own internal generation."""

    def propose_questions(self, context: Dict[str, Any]) -> List[str]:
        return []

    def propose_hypotheses(self, question_text: str, context: Dict[str, Any]
                           ) -> List[str]:
        return []

    def design_experiment(self, question_text: str, hypothesis_statements: List[str],
                          context: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        return None


@dataclass
class ArbiterVerdict:
    hypothesis_id: str
    new_state: HypothesisState
    new_confidence: float
    reasoning: str

    def as_dict(self) -> Dict[str, Any]:
        return {"hypothesis_id": self.hypothesis_id,
                "new_state": self.new_state.value,
                "new_confidence": self.new_confidence, "reasoning": self.reasoning}


class EvidenceArbiter:
    SUPPORT_THRESHOLD = 0.75
    REFUTE_THRESHOLD = 0.25

    def decide(self, hypothesis_id: str, evidence: List[Evidence]) -> ArbiterVerdict:
        if not evidence:
            return ArbiterVerdict(hypothesis_id, HypothesisState.PROPOSED, 0.5,
                                  "no evidence yet; neutral, not certified")

        supporting = sum(1 for e in evidence if e.supports)
        total = len(evidence)
        confidence = supporting / total

        if confidence >= self.SUPPORT_THRESHOLD and total >= 2:
            state = HypothesisState.SUPPORTED
            reasoning = (f"{supporting}/{total} evidence items support this "
                        f"hypothesis, at or above the "
                        f"{self.SUPPORT_THRESHOLD:.0%} threshold")
        elif confidence <= self.REFUTE_THRESHOLD and total >= 2:
            state = HypothesisState.REFUTED
            reasoning = (f"only {supporting}/{total} evidence items support "
                        f"this hypothesis, at or below the "
                        f"{self.REFUTE_THRESHOLD:.0%} threshold — refuted, "
                        f"not merely low-confidence")
        else:
            state = HypothesisState.UNDER_TEST
            reasoning = (f"{supporting}/{total} evidence items support this "
                        f"hypothesis — inconclusive, more evidence needed "
                        f"before a verdict")

        return ArbiterVerdict(hypothesis_id, state, confidence, reasoning)

    def compare(self, verdicts: List[ArbiterVerdict]) -> Optional[str]:
        decided = [v for v in verdicts if v.new_state in
                  (HypothesisState.SUPPORTED, HypothesisState.UNDER_TEST)]
        if not decided:
            return None
        return max(decided, key=lambda v: v.new_confidence).hypothesis_id
