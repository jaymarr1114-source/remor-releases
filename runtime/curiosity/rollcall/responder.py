"""Domain-side challenge responders: test doubles for Phase 1.

The real Curiosity Run Controller challenge handler is Phase 2's
(design Section 7.5): read challenge -> echo nonce -> return, on a
dedicated path, never the objective-intake path. These doubles stand
in for it and REALLY answer challenges (or really stay silent, or
really answer wrongly) -- they are not stubs that return canned
verdicts. Each double validates the challenge wire schema strictly,
exactly as the Phase 2 handler must, so schema rejections are
exercised on both ends of the transport.
"""

from __future__ import annotations

import time
from typing import Any, Callable, Dict, Optional, Set

from .schemas import Challenge, ChallengeResponse, SchemaViolation


class ChallengeResponder:
    """Base double: strict challenge validation + per-challenge_id
    duplicate suppression (a second send of the same challenge_id is
    ignored, per design Section 6 case D)."""

    def __init__(self, clock: Callable[[], float] = time.time) -> None:
        self._clock = clock
        self._answered: Set[str] = set()
        self.invocations = 0

    def __call__(self, challenge_wire: Dict[str, Any]
                 ) -> Optional[Dict[str, Any]]:
        self.invocations += 1
        challenge = Challenge.from_wire(challenge_wire)  # strict: may raise
        if challenge.challenge_id in self._answered:
            return None  # duplicate challenge_id: ignored
        self._answered.add(challenge.challenge_id)
        return self._answer(challenge)

    def _answer(self, challenge: Challenge) -> Optional[Dict[str, Any]]:
        raise NotImplementedError


class HonestTestDouble(ChallengeResponder):
    """The conforming domain handler: read -> echo nonce -> return.
    Performs no other work on receipt (answering never triggers
    cognition, dispatch, or state change)."""

    def _answer(self, challenge: Challenge) -> Optional[Dict[str, Any]]:
        return ChallengeResponse(
            challenge_id=challenge.challenge_id,
            domain_id=challenge.domain_id,
            nonce_echo=challenge.nonce,
            responded_at=self._clock(),
        ).to_wire()


class SilentTestDouble(ChallengeResponder):
    """The unresponsive domain: never answers. The roll call must
    classify this MISSED (forensically distinct from INVALID)."""

    def _answer(self, challenge: Challenge) -> Optional[Dict[str, Any]]:
        return None


class WrongNonceTestDouble(ChallengeResponder):
    """A compromised/impersonated domain answering wrongly: the echo
    does not match the issued nonce. Must classify INVALID."""

    def _answer(self, challenge: Challenge) -> Optional[Dict[str, Any]]:
        bad = bytearray(challenge.nonce)
        bad[0] ^= 0xFF
        return ChallengeResponse(
            challenge_id=challenge.challenge_id,
            domain_id=challenge.domain_id,
            nonce_echo=bytes(bad),
            responded_at=self._clock(),
        ).to_wire()


class WrongDomainTestDouble(ChallengeResponder):
    """Another controller answering for the domain (the D-3 L3
    anti-bypass case). Must classify INVALID, not MET."""

    def _answer(self, challenge: Challenge) -> Optional[Dict[str, Any]]:
        return ChallengeResponse(
            challenge_id=challenge.challenge_id,
            domain_id="impostor-controller",
            nonce_echo=challenge.nonce,
            responded_at=self._clock(),
        ).to_wire()


class LateTestDouble(ChallengeResponder):
    """Answers correctly but after the response window closed. Must
    classify INVALID (a response arrived but fails window validation),
    never MET."""

    def __init__(self, overshoot_s: float = 1.0,
                 clock: Callable[[], float] = time.time) -> None:
        super().__init__(clock=clock)
        self._overshoot_s = overshoot_s

    def _answer(self, challenge: Challenge) -> Optional[Dict[str, Any]]:
        time.sleep(challenge.response_window_s + self._overshoot_s)
        return ChallengeResponse(
            challenge_id=challenge.challenge_id,
            domain_id=challenge.domain_id,
            nonce_echo=challenge.nonce,
            responded_at=self._clock(),
        ).to_wire()
