"""C-9 activation requests: the Primary→Curiosity handoff record.

Design: architecture/phase0-c9-activation-design_2026-09-29.md §1
(Activation request record shape).

An ActivationRequest is a REQUEST FOR WORK, not a result: it never enters
finding/evidence inlets (that would breach the C-7 fence — design §3.1).
Field-level ownership is split per the design table; no field has two
writers. The Primary Executive writes issuance fields; the Curiosity side
(takeup.py) writes decision fields. This module enforces the split by
construction: validate() checks only Primary-written fields, and the
take-up writes only its owned fields.

The ActivationRequestStore (Primary-side, Primary-track-owned) is UNBUILT
as of CUR-P4A; this module defines the curiosity-side intake contract (the
§1 record shape + fail-closed validation). The Primary track binds
issuance/storage against this contract. The decision record written here
is the defined return channel the Primary side reads back.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

# Lifecycle states (design §4).
ST_REQUESTED = "REQUESTED"
ST_ACCEPTED = "ACCEPTED"
ST_REFUSED = "REFUSED"
ST_WITHDRAWN = "WITHDRAWN"

# Refusal / terminal names (design §4 refusal-state table R1–R7).
R_REQUEST_MALFORMED = "REQUEST_MALFORMED"            # R1
R_DUPLICATE_ACTIVE_REQUEST = "DUPLICATE_ACTIVE_REQUEST"  # R2
R_CURIOSITY_DISABLED = "CURIOSITY_DISABLED"          # R3
R_REFUSED_GRANT = "REFUSED_GRANT"                    # R4
R_REFUSED_FIT = "REFUSED_FIT"                        # R5
R_WITHDRAWN = "WITHDRAWN"                            # R6
R_KILLED = "KILLED"                                  # R7 (termination, P4B)

_ISSUER = "primary_executive"


class RequestMalformed(Exception):
    """Fail-closed validation refusal (R1). The record never enters take-up."""


class DuplicateActiveRequest(Exception):
    """R2: the store is keyed by request_id; re-issue requires a new id."""


@dataclass
class ActivationRequest:
    """One Primary-issued activation request (design §1).

    Primary-written (set at construction, never mutated by take-up):
    request_id, issued_at, issued_by, operational_objective_ref,
    knowledge_gap, bounded_requirement, epoch_context.
    Curiosity-written (takeup.py only): state, decision, decision_basis,
    inquiry_id, withdrawn_at, withdrawn_reason.
    """

    # -- Primary-written ---------------------------------------------------
    request_id: str
    issued_at: float
    issued_by: str
    operational_objective_ref: Dict[str, Any]
    knowledge_gap: str
    bounded_requirement: Dict[str, Any]
    epoch_context: Dict[str, Any]

    # -- Curiosity-written -------------------------------------------------
    state: str = ST_REQUESTED
    decision: Optional[Dict[str, Any]] = None
    decision_basis: Dict[str, Any] = field(default_factory=dict)
    inquiry_id: Optional[str] = None
    withdrawn_at: Optional[float] = None
    withdrawn_reason: Optional[str] = None

    def validate(self) -> "ActivationRequest":
        """Fail-closed: a malformed record is not an activation request."""
        problems = []
        if not (self.request_id or "").strip():
            problems.append("request_id is required")
        if self.issued_by != _ISSUER:
            problems.append(
                f"issued_by must be {_ISSUER!r}, got {self.issued_by!r}")
        ref = self.operational_objective_ref or {}
        if not isinstance(ref, dict) or not (ref.get("objective_id") or "").strip():
            problems.append("operational_objective_ref.objective_id is required")
        if not isinstance(ref, dict) or not (ref.get("statement") or "").strip():
            problems.append("operational_objective_ref.statement is required")
        if not (self.knowledge_gap or "").strip():
            problems.append("knowledge_gap is required (C-9.1)")
        br = self.bounded_requirement
        if not isinstance(br, dict) or not br:
            problems.append("bounded_requirement is required (C-9.1)")
        if problems:
            raise RequestMalformed(
                f"{R_REQUEST_MALFORMED}: " + "; ".join(problems))
        return self

    @classmethod
    def from_primary_dict(cls, d: Dict[str, Any]) -> "ActivationRequest":
        """Build from the Primary-issued dict shape (design §1)."""
        return cls(
            request_id=d.get("request_id") or uuid.uuid4().hex,
            issued_at=d.get("issued_at") or time.time(),
            issued_by=d.get("issued_by") or "",
            operational_objective_ref=d.get("operational_objective_ref") or {},
            knowledge_gap=d.get("knowledge_gap") or "",
            bounded_requirement=d.get("bounded_requirement") or {},
            epoch_context=d.get("epoch_context") or {},
        ).validate()

    def view(self) -> Dict[str, Any]:
        """The record as the Primary side reads it back (return channel)."""
        return {
            "request_id": self.request_id,
            "issued_at": self.issued_at,
            "issued_by": self.issued_by,
            "operational_objective_ref": dict(self.operational_objective_ref),
            "knowledge_gap": self.knowledge_gap,
            "bounded_requirement": dict(self.bounded_requirement),
            "epoch_context": dict(self.epoch_context),
            "state": self.state,
            "decision": dict(self.decision) if self.decision else None,
            "decision_basis": dict(self.decision_basis),
            "inquiry_id": self.inquiry_id,
            "withdrawn_at": self.withdrawn_at,
            "withdrawn_reason": self.withdrawn_reason,
        }
