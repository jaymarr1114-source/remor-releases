"""Curiosity boundary presentations: the CuriosityExecutive's input vocabulary.

"What boundary exists?" -> "Which loop owns it?" -> "Enter that loop."

Phase 2 owns exactly one boundary class: an imprecise question
(uncertainty about how to ask). Later phases add loop controllers for
their own boundary classes as the provisional loop set (C-6.1) grows;
their names are declared here as ABSENT so the executive names the
absence instead of routing to a fake.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict

#: The one boundary class Phase 2 converges.
BOUNDARY_IMPRECISE_QUESTION = "imprecise_question"

#: Declared future boundary classes (Phase 3+). Named here so the
#: executive can report LOOP_ABSENT with the owning loop named, never
#: route them to questioning as a silent downgrade.
BOUNDARY_HYPOTHESIS_CANDIDATE = "hypothesis_candidate"      # -> scientific_inquiry
BOUNDARY_NOVEL_OBSERVATION = "novel_observation"            # -> scientific_inquiry
BOUNDARY_GENERATIVE_PROMPT = "generative_prompt"            # -> creative_exploration
BOUNDARY_NOVEL_TASK = "novel_task"                          # -> generalization

ORIGINS = ("PRIMARY_REQUESTED", "CURIOUSITY_INITIATED")


class TriggerRefused(Exception):
    """A curiosity trigger failed validation: malformed, never routed."""


@dataclass(frozen=True)
class CuriosityTrigger:
    """A validated boundary presentation offered to the CuriosityExecutive.

    boundary_class: which convergence boundary this presents.
    question_text: the imprecise question (for BOUNDARY_IMPRECISE_QUESTION).
    bounded_objective: the bounded inquiry objective this serves.
    origin: PRIMARY_REQUESTED (Primary asked) | CURIOUSITY_INITIATED.
    """
    trigger_id: str
    boundary_class: str
    question_text: str
    bounded_objective: str
    origin: str
    presented_at: float = field(default_factory=time.time)

    def validate(self) -> "CuriosityTrigger":
        if not (self.trigger_id or "").strip():
            raise TriggerRefused("trigger_id is required")
        if not (self.boundary_class or "").strip():
            raise TriggerRefused("boundary_class is required")
        if self.origin not in ORIGINS:
            raise TriggerRefused(
                f"origin {self.origin!r} not in {ORIGINS}")
        if not (self.bounded_objective or "").strip():
            raise TriggerRefused("bounded_objective is required")
        # question_text may be empty for non-question boundary classes;
        # the fit check (executive) decides per-class requirements.
        return self

    def as_dict(self) -> Dict[str, Any]:
        return {
            "trigger_id": self.trigger_id,
            "boundary_class": self.boundary_class,
            "question_text": self.question_text,
            "bounded_objective": self.bounded_objective,
            "origin": self.origin,
            "presented_at": self.presented_at,
        }


def new_trigger(*, boundary_class: str, question_text: str,
                bounded_objective: str, origin: str) -> CuriosityTrigger:
    return CuriosityTrigger(
        trigger_id="trg_" + uuid.uuid4().hex[:12],
        boundary_class=boundary_class,
        question_text=question_text,
        bounded_objective=bounded_objective,
        origin=origin).validate()
