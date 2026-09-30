"""The Questioning loop: converges imprecise questions into precise,
checkable form."""

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
]
