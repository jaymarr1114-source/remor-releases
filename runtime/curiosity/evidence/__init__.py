"""Fenced Curiosity Evidence Store (C-7): the first link of the
evidence-return chain.

Public surface: records (CuriosityFinding, EvidenceProvenance,
EvidenceRefused, DomainFenceError, TERMINAL_STATES), store
(CuriosityEvidenceStore), writer (CuriosityWriter, the sole write path),
caller_stub (LoopCallerStub, the Phase-1 loop-side stand-in).

Deliberately NOT exported: no Primary-facing write API exists anywhere in
this package — that absence is the fence, enforced at runtime in writer.py.
"""
from .records import (
    TERMINAL_STATES,
    ORIGINS,
    CuriosityFinding,
    EvidenceProvenance,
    EvidenceRefused,
    DomainFenceError,
    terminal_state_parity,
)
from .store import CuriosityEvidenceStore
from .writer import CuriosityWriter
from .caller_stub import LoopCallerStub

__all__ = [
    "TERMINAL_STATES", "ORIGINS", "CuriosityFinding", "EvidenceProvenance",
    "EvidenceRefused", "DomainFenceError", "terminal_state_parity",
    "CuriosityEvidenceStore", "CuriosityWriter", "LoopCallerStub",
]
