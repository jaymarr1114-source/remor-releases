"""Honest unavailability: the refusal type every unreachable capability
front raises instead of simulating a result.

A CapabilityUnavailable is never a silent None or a fabricated payload;
it names the capability, the classification (UNAVAILABLE per the mission
matrix), and the exact missing dependencies found by probing the
environment at call time.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List


@dataclass
class CapabilityUnavailable(Exception):
    capability: str
    reason: str
    missing: List[str] = field(default_factory=list)
    classification: str = "UNAVAILABLE"

    def __str__(self) -> str:
        base = (f"{self.capability} is {self.classification}: {self.reason}")
        if self.missing:
            base += f" (missing: {', '.join(self.missing)})"
        return base

    def as_dict(self) -> Dict[str, Any]:
        return {
            "capability": self.capability,
            "classification": self.classification,
            "reason": self.reason,
            "missing": self.missing,
        }
