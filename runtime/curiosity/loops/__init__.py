"""Curiosity loop controllers (level 2 of the curiosity hierarchy)."""

from swarm_engine.curiosity.loops.questioning.loop import (
    TERMINAL_BOUNDARY,
    TERMINAL_INSUFFICIENT,
    TERMINAL_RESOLVED,
    CorpusIndex,
    LoopContext,
    QuestioningLoop,
    QuestioningLoopInlet,
    StepResult,
    SubstrateRefused,
)
from swarm_engine.curiosity.cognition import (
    compose_precise_question,
    extract_scope,
    extract_unknown,
    mentions_observable,
    precision_score,
)

__all__ = [
    "TERMINAL_BOUNDARY",
    "TERMINAL_INSUFFICIENT",
    "TERMINAL_RESOLVED",
    "CorpusIndex",
    "LoopContext",
    "QuestioningLoop",
    "QuestioningLoopInlet",
    "StepResult",
    "SubstrateRefused",
    "compose_precise_question",
    "extract_scope",
    "extract_unknown",
    "mentions_observable",
    "precision_score",
]
