"""GAM roll-call channel: fixed wire schemas.

Phase-0 GAM mechanism design, Section 2: the challenge, response, and
attestation schemas are capability-free BY CONSTRUCTION. There is no
field for objectives, instructions, capabilities, evidence, grants,
verdicts, or commands -- the bytes have nowhere to live. Deserialization
rejects unknown fields loudly (SchemaViolation) instead of ignoring them,
so a smuggled objective fails at the wire boundary before any handler
runs.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, FrozenSet, Optional


class SchemaViolation(Exception):
    """A wire dict does not conform to a fixed roll-call schema."""


CLASSIFICATIONS: FrozenSet[str] = frozenset({"MET", "MISSED", "INVALID"})

#: Fields that MUST NOT exist anywhere in the roll-call wire format.
#: A smuggled-objective attack is rejected because these have no home.
FORBIDDEN_WIRE_FIELDS: FrozenSet[str] = frozenset({
    "objective", "objectives", "instruction", "instructions", "command",
    "commands", "capability", "capabilities", "evidence", "evidence_id",
    "evidence_refs", "grant", "grants", "verdict", "verdicts", "ban",
    "suspend", "task", "tasks", "prompt", "plan",
})


def _reject_forbidden(name: str, data: Dict[str, Any]) -> None:
    bad = FORBIDDEN_WIRE_FIELDS.intersection(data.keys())
    if bad:
        raise SchemaViolation(
            f"{name}: forbidden wire field(s) {sorted(bad)}: the roll-call "
            "schema carries no objectives, instructions, or evidence")


def _require(data: Dict[str, Any], name: str, key: str, types) -> Any:
    if key not in data:
        raise SchemaViolation(f"{name}: missing required field {key!r}")
    value = data[key]
    if not isinstance(value, types):
        raise SchemaViolation(
            f"{name}: field {key!r} must be {types}, got "
            f"{type(value).__name__}")
    return value


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:16]}"


# ---------------------------------------------------------------------------
# Challenge
# ---------------------------------------------------------------------------

CHALLENGE_FIELDS: FrozenSet[str] = frozenset({
    "challenge_id", "domain_id", "nonce", "issued_at",
    "response_window_s", "required",
})


@dataclass(frozen=True)
class Challenge:
    """The roll call GAM issues. Capability-free: no objective, instruction,
    or evidence field exists -- anywhere in this class or on the wire."""

    challenge_id: str
    domain_id: str
    nonce: bytes
    issued_at: float
    response_window_s: float
    required: bool

    def to_wire(self) -> Dict[str, Any]:
        return {
            "challenge_id": self.challenge_id,
            "domain_id": self.domain_id,
            "nonce": self.nonce.hex(),
            "issued_at": self.issued_at,
            "response_window_s": self.response_window_s,
            "required": self.required,
        }

    @staticmethod
    def from_wire(data: Dict[str, Any]) -> "Challenge":
        if not isinstance(data, dict):
            raise SchemaViolation(
                f"Challenge: wire payload must be a dict, got "
                f"{type(data).__name__}")
        _reject_forbidden("Challenge", data)
        unknown = set(data.keys()) - CHALLENGE_FIELDS
        if unknown:
            raise SchemaViolation(
                f"Challenge: unknown wire field(s) {sorted(unknown)}")
        challenge_id = _require(data, "Challenge", "challenge_id", str)
        domain_id = _require(data, "Challenge", "domain_id", str)
        nonce_hex = _require(data, "Challenge", "nonce", str)
        try:
            nonce = bytes.fromhex(nonce_hex)
        except ValueError:
            raise SchemaViolation("Challenge: field 'nonce' is not hex")
        issued_at = _require(data, "Challenge", "issued_at", (int, float))
        window = _require(data, "Challenge", "response_window_s",
                           (int, float))
        required = _require(data, "Challenge", "required", bool)
        if not challenge_id or not domain_id:
            raise SchemaViolation("Challenge: ids must be non-empty")
        if len(nonce) < 16:
            raise SchemaViolation("Challenge: nonce must be >= 16 bytes")
        if float(window) <= 0:
            raise SchemaViolation("Challenge: response_window_s must be > 0")
        return Challenge(challenge_id=challenge_id, domain_id=domain_id,
                         nonce=nonce, issued_at=float(issued_at),
                         response_window_s=float(window), required=required)


# ---------------------------------------------------------------------------
# ChallengeResponse
# ---------------------------------------------------------------------------

RESPONSE_FIELDS: FrozenSet[str] = frozenset({
    "challenge_id", "domain_id", "nonce_echo", "responded_at",
})


@dataclass(frozen=True)
class ChallengeResponse:
    """The domain's only legal answer: echo the nonce. No work, no state
    change, no cognition is triggered by answering."""

    challenge_id: str
    domain_id: str
    nonce_echo: bytes
    responded_at: float

    def to_wire(self) -> Dict[str, Any]:
        return {
            "challenge_id": self.challenge_id,
            "domain_id": self.domain_id,
            "nonce_echo": self.nonce_echo.hex(),
            "responded_at": self.responded_at,
        }

    @staticmethod
    def from_wire(data: Dict[str, Any]) -> "ChallengeResponse":
        if not isinstance(data, dict):
            raise SchemaViolation(
                f"ChallengeResponse: wire payload must be a dict, got "
                f"{type(data).__name__}")
        _reject_forbidden("ChallengeResponse", data)
        unknown = set(data.keys()) - RESPONSE_FIELDS
        if unknown:
            raise SchemaViolation(
                f"ChallengeResponse: unknown wire field(s) {sorted(unknown)}")
        challenge_id = _require(data, "ChallengeResponse", "challenge_id",
                                str)
        domain_id = _require(data, "ChallengeResponse", "domain_id", str)
        echo_hex = _require(data, "ChallengeResponse", "nonce_echo", str)
        try:
            echo = bytes.fromhex(echo_hex)
        except ValueError:
            raise SchemaViolation(
                "ChallengeResponse: field 'nonce_echo' is not hex")
        responded_at = _require(data, "ChallengeResponse", "responded_at",
                                (int, float))
        if not challenge_id or not domain_id:
            raise SchemaViolation("ChallengeResponse: ids must be non-empty")
        return ChallengeResponse(challenge_id=challenge_id,
                                 domain_id=domain_id, nonce_echo=echo,
                                 responded_at=float(responded_at))


# ---------------------------------------------------------------------------
# AttestationRecord
# ---------------------------------------------------------------------------

ATTESTATION_FIELDS: FrozenSet[str] = frozenset({
    "attestation_id", "challenge_id", "domain_id", "issued_at",
    "responded_at", "classification", "validation_detail", "recorded_at",
})


@dataclass(frozen=True)
class AttestationRecord:
    """GAM's sole output vocabulary: classification in
    {MET, MISSED, INVALID}. No ban/suspend verb exists here."""

    attestation_id: str
    challenge_id: str
    domain_id: str
    issued_at: float
    responded_at: Optional[float]
    classification: str
    validation_detail: str
    recorded_at: float = field(default=0.0)

    def __post_init__(self) -> None:
        if self.classification not in CLASSIFICATIONS:
            raise SchemaViolation(
                f"AttestationRecord: classification must be one of "
                f"{sorted(CLASSIFICATIONS)}, got {self.classification!r}")

    def to_wire(self) -> Dict[str, Any]:
        return {
            "attestation_id": self.attestation_id,
            "challenge_id": self.challenge_id,
            "domain_id": self.domain_id,
            "issued_at": self.issued_at,
            "responded_at": self.responded_at,
            "classification": self.classification,
            "validation_detail": self.validation_detail,
            "recorded_at": self.recorded_at,
        }

    @staticmethod
    def from_wire(data: Dict[str, Any]) -> "AttestationRecord":
        if not isinstance(data, dict):
            raise SchemaViolation("AttestationRecord: wire payload must be "
                                  f"a dict, got {type(data).__name__}")
        _reject_forbidden("AttestationRecord", data)
        unknown = set(data.keys()) - ATTESTATION_FIELDS
        if unknown:
            raise SchemaViolation(
                f"AttestationRecord: unknown wire field(s) "
                f"{sorted(unknown)}")
        classification = _require(data, "AttestationRecord",
                                  "classification", str)
        if classification not in CLASSIFICATIONS:
            raise SchemaViolation(
                f"AttestationRecord: classification must be one of "
                f"{sorted(CLASSIFICATIONS)}")
        responded_at = data.get("responded_at")
        if responded_at is not None and not isinstance(
                responded_at, (int, float)):
            raise SchemaViolation(
                "AttestationRecord: responded_at must be a number or null")
        return AttestationRecord(
            attestation_id=_require(data, "AttestationRecord",
                                    "attestation_id", str),
            challenge_id=_require(data, "AttestationRecord", "challenge_id",
                                  str),
            domain_id=_require(data, "AttestationRecord", "domain_id", str),
            issued_at=float(_require(data, "AttestationRecord", "issued_at",
                                     (int, float))),
            responded_at=(None if responded_at is None
                          else float(responded_at)),
            classification=classification,
            validation_detail=_require(data, "AttestationRecord",
                                       "validation_detail", str),
            recorded_at=float(_require(data, "AttestationRecord",
                                       "recorded_at", (int, float))),
        )


def new_challenge_id() -> str:
    return _new_id("chal")


def new_attestation_id() -> str:
    return _new_id("att")
