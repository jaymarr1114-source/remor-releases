"""Generalization Controller -- level 2 of the three-level hierarchy.

Owns the loop convergence process: probe a novel task -> verify ->
expand the envelope or mark the bound.

Subordinates (wrap and own, never rewrite): generalize_driver (V10-P5),
the V10-COMPOSE composer set, DistillationLoop.generalize (M2/Q6),
admit_as_engine and the Q8 auth inlet (V10-P6). Microcontroller
spawn/retire goes through RUN-MICRO-1's frozen substrate
(microcontroller-interface/v1); the executive sees only LoopView.
"""

from .controller import (
    GeneralizationController,
    CycleResult,
    ENVELOPE_KIND,
    ENVELOPE_BASELINE_KIND,
    BASELINE_HOLDS,
    BASELINE_BREAKS,
)

__all__ = [
    "GeneralizationController",
    "CycleResult",
    "ENVELOPE_KIND",
    "ENVELOPE_BASELINE_KIND",
    "BASELINE_HOLDS",
    "BASELINE_BREAKS",
]
