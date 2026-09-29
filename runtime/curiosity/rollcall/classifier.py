"""Roll-call classifier: MET / MISSED / INVALID.

Phase-0 GAM mechanism design, Section 2. Classification is GAM's sole
judgment -- attestation, not enforcement. It is mechanical:

  MET     valid response (matching challenge_id, domain_id, nonce_echo)
          with responded_at - issued_at <= response_window_s.
  MISSED  no response at all within the window (silence).
  INVALID a response arrived but fails validation: wrong nonce, wrong
          domain_id, malformed shape, or outside the response window.

INVALID is forensically distinct from MISSED: a domain that answers
wrongly (compromised/impersonated) is distinguished from a silent one.
Both count as "failed roll call" for Level 3 evaluation, but the
distinction is preserved in the attestation record.
"""

from __future__ import annotations

from typing import Optional, Tuple

from .schemas import Challenge, ChallengeResponse, SchemaViolation


def classify(challenge: Challenge,
             response: Optional[ChallengeResponse],
             now: float) -> Tuple[str, str]:
    """Classify one roll call. Returns (classification, validation_detail).

    `response` is None when the domain stayed silent. `now` is the time
    the window is evaluated at (normally >= issued_at + window for a
    silence verdict; for an arrived response, classification uses the
    response's own responded_at).
    """
    if response is None:
        return ("MISSED",
                "no response received within the response window")
    # A response arrived: every check below that fails makes it INVALID,
    # never MET, never silently accepted.
    if response.challenge_id != challenge.challenge_id:
        return ("INVALID",
                f"challenge_id mismatch: response answers "
                f"{response.challenge_id!r}, expected "
                f"{challenge.challenge_id!r}")
    if response.domain_id != challenge.domain_id:
        return ("INVALID",
                f"domain_id mismatch: response from {response.domain_id!r}, "
                f"challenge issued to {challenge.domain_id!r} "
                "(another controller answering for the domain)")
    if response.nonce_echo != challenge.nonce:
        return ("INVALID", "nonce_echo does not match the issued nonce")
    if response.responded_at < challenge.issued_at:
        return ("INVALID",
                "responded_at predates the challenge issuance")
    elapsed = response.responded_at - challenge.issued_at
    if elapsed > challenge.response_window_s:
        return ("INVALID",
                f"response arrived {elapsed:.3f}s after issuance, outside "
                f"the {challenge.response_window_s:.3f}s window")
    return ("MET",
            f"valid nonce echo {elapsed:.3f}s after issuance, inside the "
            f"{challenge.response_window_s:.3f}s window")


def validate_response_wire(data: object) -> ChallengeResponse:
    """Parse a raw wire response strictly. Malformed shape raises
    SchemaViolation, which the caller maps to INVALID (a response
    arrived but is not a well-formed answer)."""
    if not isinstance(data, dict):
        raise SchemaViolation(
            "ChallengeResponse: wire payload must be a dict, got "
            f"{type(data).__name__}")
    return ChallengeResponse.from_wire(data)
