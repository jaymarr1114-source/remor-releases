"""Creativity executive slices D-4 + D-5 (runtime/creativity/).

Peer-level executive domain — NOT under runtime/curiosity/ (which is
the FRM/second-executive domain). The creativity executive is the
third executive on paper; this package holds the paper-SPECIFIED
slices James's decisions settled, built easiest-first:

- D-5 (paper §9 T2, §2): the six ordered creative stages
  (stages.py).
- D-4 (paper §9 T1): the intent register with intent-state ownership
  (intent.py).
- §3 mechanism (paper §3, DEFERRED pending a substrate; EVIDENCE-WIRE-1
  landed the substrate): the verified-substrate ledger, read-side only
  (ledger.py). Compositions draw exclusively from the verified side;
  unverified demand becomes a named gap, never substrate.

NOT built here: the novelty check (§4 proposed), the critique
machinery (§6 DEFERRED implementation), the composition search, peer
arbitration (§5), the executive itself, the run controller, budgets,
or anything depending on undecided parameters. The dual-executive
build hold was lifted 2026-10-01 (only dispatch is not-to-build);
this package still builds decided slices only, in causal order —
see ~/workspace/tracks/creativity-executive/TRACK_PLAN.md.
"""

from .intent import (
    CreativeIntent,
    IntentRefused,
    admissible_stages,
    register_intent,
    select_state,
)
from .ledger import (
    DEVICE_CONFIRMED,
    DEVICE_NOT_APPLICABLE,
    DEVICE_UNCONFIRMED,
    CompositionVerdict,
    Ledger,
    LedgerEntry,
    LedgerRefused,
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
    "CompositionVerdict",
    "DEVICE_CONFIRMED",
    "DEVICE_NOT_APPLICABLE",
    "DEVICE_UNCONFIRMED",
    "IntentRefused",
    "Ledger",
    "LedgerEntry",
    "LedgerRefused",
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
