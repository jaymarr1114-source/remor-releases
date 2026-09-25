"""
swarm_engine/core/activation.py

On-demand activation.

Callers currently run a fixed sequence (gap check, then acquisition, then
verification...) regardless of whether the request needs all of it. A goal
already backed by an admitted, trusted capability needs none of that — just
execution. This module makes that decision explicit and cheap, from signals
already on hand, so the engine does not run its full cognitive stack on
every request by default.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List


class Subsystem(Enum):
    EXECUTE = "execute"
    RESEARCH = "research"
    BUILD = "build"
    VERIFY = "verify"
    DEBUG = "debug"
    OPTIMIZE = "optimize"


@dataclass
class ActivationPlan:
    needed: List[Subsystem] = field(default_factory=list)
    reason: str = ""

    def as_dict(self) -> Dict:
        return {"needed": [s.value for s in self.needed], "reason": self.reason}


_DEBUG_WORDS = ("fail", "error", "broken", "debug", "diagnose")
_OPTIMIZE_WORDS = ("improve", "optimize", "compare", "faster", "better")
_VERIFY_WORDS = ("verify", "check", "validate", "confirm")
_BUILD_WORDS = ("build", "acquire", "create", "synthesize", "compose")


class Activator:
    """Decides which subsystems a request needs, so the caller runs only
    those rather than a fixed full pipeline."""

    def __init__(self, engine):
        self.engine = engine

    def plan(self, task: str, error: str = "") -> ActivationPlan:
        if error:
            return ActivationPlan([Subsystem.DEBUG], "an error is present")

        lowered = task.lower()
        if any(w in lowered for w in _DEBUG_WORDS):
            return ActivationPlan([Subsystem.DEBUG], "task reads as diagnostic")
        if any(w in lowered for w in _OPTIMIZE_WORDS):
            return ActivationPlan([Subsystem.OPTIMIZE], "task reads as comparative")
        if any(w in lowered for w in _VERIFY_WORDS):
            return ActivationPlan([Subsystem.VERIFY], "task reads as verification")
        if any(w in lowered for w in _BUILD_WORDS):
            return ActivationPlan([Subsystem.BUILD], "task states build intent directly")

        record = self.engine.capabilities.resolve_goal(task)
        if record is not None and record.status == "active":
            return ActivationPlan([Subsystem.EXECUTE],
                                  f"admitted capability {record.capability_id} "
                                  f"already covers this")

        gap = self.engine.gaps.detect(task)
        if not gap.has_gap:
            return ActivationPlan([Subsystem.EXECUTE],
                                  "composable from existing primitives")
        return ActivationPlan([Subsystem.RESEARCH, Subsystem.BUILD], gap.reason)
