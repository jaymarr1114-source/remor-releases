"""Curiosity-side microcontroller substrate.

FLAGGED DEVIATION from the Primary-as-template rule (recorded here and in
the mission report): the frozen MicrocontrollerSubstrate
(runtime/core/microcontroller/substrate.py, microcontroller-interface/v1)
registers only the Primary's six loops, and widening that vocabulary inside
the frozen module would be a breaking change requiring James's explicit
decision. This subclass reuses ALL of the base machinery -- spawn/retire
semantics, depth and concurrency caps, per-loop admission pools, budget
charging, cascade unwind, fabricated-retire detection, LoopView
encapsulation, the export/import checkpoint seam -- and overrides exactly
ONE method, register_loop, to accept the curiosity executive's own
(provisional, C-6.1) loop names. This is an adapter, not a second
implementation: the lifecycle bounds are the base class's, unchanged.

C-1.4: the Curiosity Run Controller owns a SEPARATE INSTANCE of this class
from any Primary-side substrate instance. No path exists for either side to
spawn, retire, or inspect the other's microcontrollers -- enforced by
separate instances, verified adversarially (cross-instance spawn/retire
attempts fail with unknown_parent / unknown_microcontroller).
"""

from __future__ import annotations

from typing import Tuple

from swarm_engine.core.microcontroller.substrate import (
    LoopAdmission,
    MicrocontrollerSubstrate,
)
from swarm_engine.core.microcontroller.granted_cognition import (
    GrantedCognitionProvider,
)
from swarm_engine.curiosity.cognition import PrecisionCognitionProvider

#: The curiosity executive's loop vocabulary (provisional, C-6.1). Phase 2
#: builds exactly one: questioning. Later phases add loop controllers here
#: as their convergence boundaries are demonstrated -- never speculatively.
#: "scientific_inquiry" admitted by James's U-1-class decision 2026-10-01
#: (CUR-P3A-INT) after its stage-level convergence boundary was
#: demonstrated (CUR-P3A, 61/61); the fence stays closed otherwise.
#: "creative_exploration" admitted by James 2026-10-03 (CUR-P3B-INT;
#: loop-classification vocabulary only, never a claim of independent
#: creative reasoning; stage boundary demonstrated CUR-P3B, 66/66).
#: "discovery_novelty" admitted by James 2026-10-03 (loop-classification
#: vocabulary only, never a claim of independent scientific reasoning).
CURIOSITY_LOOPS: Tuple[str, ...] = (
    "questioning", "scientific_inquiry",
    "creative_exploration", "discovery_novelty")
CURIOSITY_LOOP_SET = frozenset(CURIOSITY_LOOPS)

LOOP_QUESTIONING = "questioning"
LOOP_SCIENTIFIC_INQUIRY = "scientific_inquiry"
LOOP_CREATIVE_EXPLORATION = "creative_exploration"
LOOP_DISCOVERY_NOVELTY = "discovery_novelty"


class CuriositySubstrate(MicrocontrollerSubstrate):
    """MicrocontrollerSubstrate with the curiosity loop vocabulary.

    Everything except register_loop is inherited verbatim. The curiosity
    substrate hosts ONLY curiosity loops: Primary loop names are refused
    here (they belong to the Primary side's instance).

    The curiosity substrate's cognition inlet is the governed provider
    (BRAIN-SCAFFOLD-1): GrantedCognitionProvider with the deterministic
    mechanical precision reasoner as its native tier (D-8: the same
    cognition utility shape the Primary uses; no separate mind).
    Existing OP_* cognition calls take the native path -- no grant
    needed, no FRM charge -- so behavior is unchanged; the borrow path
    activates only on a named native refusal with an FrmGrant.
    A different provider can still be installed via set_cognition_provider.
    """

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.set_cognition_provider(GrantedCognitionProvider(
            substrate=self, native=PrecisionCognitionProvider()))

    def register_loop(self, loop: str, *, budget_s: float,
                      max_concurrent: int = 64) -> None:
        if loop not in CURIOSITY_LOOP_SET:
            raise ValueError(
                f"unknown curiosity loop {loop!r}; expected one of "
                f"{CURIOSITY_LOOPS} (Primary loop names belong to the "
                "Primary side's substrate instance)")
        if budget_s <= 0:
            raise ValueError("loop budget_s must be > 0")
        # Same registration body as the base class; the loop admission
        # pool, concurrency cap, and idle state are the base machinery.
        self._loops[loop] = LoopAdmission(
            loop=loop, budget_s=float(budget_s),
            max_concurrent=int(max_concurrent))
        self._loop_state.setdefault(loop, "idle")
