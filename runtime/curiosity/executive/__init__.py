"""The Curiosity Executive Controller (level 1): selects the owning loop,
sets priority, and accepts/refuses activation requests. Never operates
runtime machinery."""

from swarm_engine.curiosity.executive.boundary import (
    BOUNDARY_GENERATIVE_PROMPT,
    BOUNDARY_HYPOTHESIS_CANDIDATE,
    BOUNDARY_IMPRECISE_QUESTION,
    BOUNDARY_NOVEL_OBSERVATION,
    BOUNDARY_NOVEL_TASK,
    CuriosityTrigger,
    TriggerRefused,
    new_trigger,
)
from swarm_engine.curiosity.executive.executive import (
    ABSENT_OWNERSHIP,
    LOOP_OWNERSHIP,
    ActivationDecision,
    ActivationRefused,
    CuriosityExecutive,
    CuriosityLoopOutcome,
)

__all__ = [
    "BOUNDARY_GENERATIVE_PROMPT",
    "BOUNDARY_HYPOTHESIS_CANDIDATE",
    "BOUNDARY_IMPRECISE_QUESTION",
    "BOUNDARY_NOVEL_OBSERVATION",
    "BOUNDARY_NOVEL_TASK",
    "CuriosityTrigger",
    "TriggerRefused",
    "new_trigger",
    "ABSENT_OWNERSHIP",
    "LOOP_OWNERSHIP",
    "ActivationDecision",
    "ActivationRefused",
    "CuriosityExecutive",
    "CuriosityLoopOutcome",
]
