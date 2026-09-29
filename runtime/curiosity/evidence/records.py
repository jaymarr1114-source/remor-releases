"""Curiosity evidence records (C-7): the record shapes of the fenced
Curiosity Evidence Store.

Validation follows the *pattern* of Primary's Finding validation
(runtime/core/executive/relevance.py: Finding.validate raises FindingRefused
on missing provenance or a terminal state outside the enumerated set) — but
this is a DISTINCT record type, in a DISTINCT database, with DISTINCT
writers. Reusing the pattern, never sharing tables, writers, or record
classes (no-duplication mandate, James 2026-09-27).

Frozen interface (Phase-1 shared block):
  {evidence_id: uuid, loop: str, bounded_objective: str,
   origin: PRIMARY_REQUESTED|CURIOUSITY_INITIATED,
   terminal_state: <enumerated>, provenance: {loop, bounded_objective,
   model/provider}, payload_ref: str, created_at: ts}
"""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, Optional, Tuple

# ---------------------------------------------------------------------------
# Frozen vocabulary (mirrors the Phase-1 shared interface; the enumerated
# terminal states are the charter C-2.2 set).
# ---------------------------------------------------------------------------

TERMINAL_STATES: Tuple[str, ...] = (
    "QUESTION_RESOLVED",
    "HYPOTHESIS_SUPPORTED",
    "HYPOTHESIS_REFUTED",
    "DISCOVERY_VERIFIED",
    "CANDIDATE_GENERATED",
    "MODEL_REVISED",
    "NOVELTY_CLASSIFIED",
    "BOUNDARY_ESTABLISHED",
    "INSUFFICIENT_EVIDENCE",
    "INCONCLUSIVE",
    "BLOCKED",
)

ORIGINS = ("PRIMARY_REQUESTED", "CURIOUSITY_INITIATED")


def terminal_state_parity(primary_states: Tuple[str, ...]) -> bool:
    """Check the frozen terminal-state vocabulary matches the Primary
    enumerated set it is derived from. Used by the proof battery as an
    executable parity assertion (the curiosity set must neither drift nor
    silently subset the shared vocabulary)."""
    return tuple(primary_states) == TERMINAL_STATES


# ---------------------------------------------------------------------------
# Refusals
# ---------------------------------------------------------------------------

class EvidenceRefused(Exception):
    """An evidence record is malformed: inadmissible into the fenced store.

    Fail-closed: a refused record is NEVER persisted, never partially
    written. The message is the explicit reason.
    """


class DomainFenceError(Exception):
    """A write was attempted from outside the curiosity domain. The store
    admits writes only from curiosity-side callers (see writer.py); this
    exception is the loud refusal, never a silent downgrade."""


# ---------------------------------------------------------------------------
# Records
# ---------------------------------------------------------------------------

@dataclass
class EvidenceProvenance:
    """Where the curiosity finding came from. All three fields are required:
    a finding with no producing loop, no bounded objective, or no
    model/provider is ungrounded and refused.

    `triage` is the curiosity side's advisory first pass (charter C-3.3):
    "retain" | "propose_capability" | "propose_investigation" | "boundary".
    Advisory only — recorded, never decisive; nothing in this package acts
    on it. It is carried here so Primary Acceptance's later inspection
    (Phase 4+) can read what curiosity suggested, per the C-9 design §7.
    """
    loop: str
    bounded_objective: str
    model: str  # the model/provider that produced the finding
    triage: Optional[str] = None

    def as_dict(self) -> Dict[str, Any]:
        return {
            "loop": self.loop,
            "bounded_objective": self.bounded_objective,
            "model": self.model,
            "triage": self.triage,
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "EvidenceProvenance":
        return cls(loop=d["loop"], bounded_objective=d["bounded_objective"],
                   model=d["model"], triage=d.get("triage"))


@dataclass
class CuriosityFinding:
    """One curiosity evidence record (C-7). The first link of the
    evidence-return chain (Evidence Store → inspection → relevance →
    acceptance → commit/retain/reject, chained by evidence_id; C-7.4)."""
    evidence_id: str
    loop: str
    bounded_objective: str
    origin: str
    terminal_state: str
    provenance: EvidenceProvenance
    payload_ref: str
    created_at: float = field(default_factory=time.time)

    def validate(self) -> "CuriosityFinding":
        """Charter C-7/C-2.2 admissibility: provenance present and complete,
        terminal state from the enumerated set, origin from the enumerated
        set. Raises EvidenceRefused otherwise — the record is inadmissible."""
        if self.provenance is None:
            raise EvidenceRefused(
                f"finding {self.evidence_id!r}: provenance is required "
                "(a finding with no producing loop is ungrounded)")
        for attr, label in (("loop", "loop"),
                            ("bounded_objective", "bounded_objective"),
                            ("model", "model/provider")):
            if not (getattr(self.provenance, attr) or "").strip():
                raise EvidenceRefused(
                    f"finding {self.evidence_id!r}: provenance.{attr} is "
                    f"required ({label})")
        if not (self.loop or "").strip():
            raise EvidenceRefused(
                f"finding {self.evidence_id!r}: loop is required")
        if not (self.bounded_objective or "").strip():
            raise EvidenceRefused(
                f"finding {self.evidence_id!r}: bounded_objective is required")
        if self.origin not in ORIGINS:
            raise EvidenceRefused(
                f"finding {self.evidence_id!r}: origin {self.origin!r} is not "
                f"in the enumerated set {ORIGINS}")
        if self.terminal_state not in TERMINAL_STATES:
            raise EvidenceRefused(
                f"finding {self.evidence_id!r}: terminal_state "
                f"{self.terminal_state!r} is not in the enumerated set")
        if not (self.payload_ref or "").strip():
            raise EvidenceRefused(
                f"finding {self.evidence_id!r}: payload_ref is required")
        return self

    def as_dict(self) -> Dict[str, Any]:
        return {
            "evidence_id": self.evidence_id,
            "loop": self.loop,
            "bounded_objective": self.bounded_objective,
            "origin": self.origin,
            "terminal_state": self.terminal_state,
            "provenance": self.provenance.as_dict(),
            "payload_ref": self.payload_ref,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "CuriosityFinding":
        return cls(
            evidence_id=d["evidence_id"], loop=d["loop"],
            bounded_objective=d["bounded_objective"], origin=d["origin"],
            terminal_state=d["terminal_state"],
            provenance=EvidenceProvenance.from_dict(d["provenance"]),
            payload_ref=d["payload_ref"], created_at=d["created_at"])

    @classmethod
    def new(cls, loop: str, bounded_objective: str, origin: str,
            terminal_state: str, provenance: EvidenceProvenance,
            payload_ref: str) -> "CuriosityFinding":
        return cls(evidence_id=uuid.uuid4().hex, loop=loop,
                   bounded_objective=bounded_objective, origin=origin,
                   terminal_state=terminal_state, provenance=provenance,
                   payload_ref=payload_ref)
