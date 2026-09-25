"""Confidence-gated approximate agreement for behavioral identity checks.

Exact residual algebra remains exact. This module only answers:
  "does observed vector Y strongly near-agree with target T?"

Design constraints (frontier policy):
  - No task-specific similarity, no name bonuses, no threshold tuned to a
    decisive harness outcome.
  - Strong near-equivalent: float-stable / machine-relative agreement on
    every example → high confidence → may admit as approximate identity.
  - Weak/superficial (off-by-one, large relative drift) → low confidence →
    refuse.

Confidence is 1 - max_i relative_error_i for numeric pairs, or 0/1 for
non-numeric exactness. Admission floor is machine-epsilon class
(1 - 1e-9), a representational stability bound, not a soft fuzzy knob.
"""
from __future__ import annotations

import math
from typing import Any, Sequence, Tuple

# Representational stability floor: ~1e-9 relative. Not tuned to any task.
APPROX_ADMIT_FLOOR = 1.0 - 1e-9


def _rel_err(a: float, b: float) -> float:
    if a == b:
        return 0.0
    if math.isnan(a) or math.isnan(b) or math.isinf(a) or math.isinf(b):
        return 1.0 if a != b else 0.0
    scale = max(1.0, abs(a), abs(b))
    return abs(a - b) / scale


def example_agreement(got: Any, want: Any) -> float:
    """Per-example agreement in [0, 1]. Exact non-numerics; relative for nums."""
    if got == want:
        return 1.0
    if isinstance(got, bool) or isinstance(want, bool):
        return 1.0 if got == want else 0.0
    if isinstance(got, (int, float)) and isinstance(want, (int, float)):
        return max(0.0, 1.0 - _rel_err(float(got), float(want)))
    return 0.0


def vector_confidence(got: Sequence[Any], want: Sequence[Any]) -> float:
    if len(got) != len(want) or not got:
        return 0.0
    return min(example_agreement(g, w) for g, w in zip(got, want))


def approx_identity_admit(
        got: Sequence[Any], want: Sequence[Any],
        floor: float = APPROX_ADMIT_FLOOR) -> Tuple[bool, float]:
    """Return (admit?, confidence). Exact short-circuits to (True, 1.0)."""
    if tuple(got) == tuple(want):
        return True, 1.0
    conf = vector_confidence(got, want)
    return (conf >= floor), conf
