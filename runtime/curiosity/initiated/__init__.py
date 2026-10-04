"""Curiosity-initiated work: self-generated objectives (C-1.3).

This package mints inquiries that curiosity originates with NO Primary
permission in their causal history — no Primary request, no LivePath
involvement, no Primary grant — and proves the negative by inspection
of the real lineage. Per C-1.3, initiated work "gains no authority over
Primary loops by beginning"; the no-authority adversarial is exercised
in proofs/cur_p5a (each refusal executed against the real machinery).

Structural guarantee: this package never imports runtime.core (the
Primary side). The battery verifies that.

Origin vocabulary uses the frozen spelling "CURIOUSITY_INITIATED"
(an observed misspelling in the frozen boundary vocabulary, deliberately
left unrepaired — renaming breaks landed callers).
"""

from .initiate import mint_initiated_trigger
from .ledger import InitiationLedger, InitiationRecord, OriginForged
from .lineage import verify_initiated_lineage

__all__ = [
    "mint_initiated_trigger",
    "InitiationLedger",
    "InitiationRecord",
    "OriginForged",
    "verify_initiated_lineage",
]
