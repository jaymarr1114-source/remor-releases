"""Creativity executive slices D-4 + D-5 (runtime/creativity/).

Peer-level executive domain — NOT under runtime/curiosity/ (which is
the FRM/second-executive domain). The creativity executive is the
third executive on paper; this package holds ONLY the two
paper-SPECIFIED slices James's decisions settled:

- D-5 (paper §9 T2, §2): the six ordered creative stages
  (stages.py).
- D-4 (paper §9 T1): the intent register with intent-state ownership
  (intent.py).

NOT built here: the ledger mechanism (§3 DEFERRED), the novelty
check (§4 proposed), the critique machinery (§6 DEFERRED
implementation), the composition search, peer arbitration (§5), the
executive itself, the run controller, budgets, or anything depending
on the §11 governance decisions. The dual-executive build hold
remains in force.
"""

from .intent import (
    CreativeIntent,
    IntentRefused,
    admissible_stages,
    register_intent,
    select_state,
)
from .stages import (
    STAGE_ORDER,
    CreativeStage,
    StageDescriptor,
    StageRefused,
    admissible_next,
    describe,
    is_terminal,
    refinement_loop_admissible,
)

__all__ = [
    "STAGE_ORDER",
    "CreativeIntent",
    "CreativeStage",
    "IntentRefused",
    "StageDescriptor",
    "StageRefused",
    "admissible_next",
    "admissible_stages",
    "describe",
    "is_terminal",
    "refinement_loop_admissible",
    "register_intent",
    "select_state",
]
