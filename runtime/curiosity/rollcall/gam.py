"""Governance Attestation Monitor (GAM): the roll-call channel runtime.

Charter D-5, attestation-only authority. GAM may: issue challenges,
check responses, timestamp, classify MET/MISSED/INVALID, preserve the
attestation record, and expose the roll-call fact to enforcement. GAM
may NOT: set objectives, allocate resources, alter capabilities,
modify evidence, accept/reject findings, re-enable Curiosity, or
impose the six-month ban.

That boundary is structural here, not declarative:

* This class holds references to exactly three things: the scheduler,
  the attestation ledger, and a clock. It has no handles, imports, or
  store connections to objective stores, evidence stores, acceptance,
  the arbitrator, the FRM, grant stores, or the kill-state flag -- it
  cannot name what it cannot reach.
* Its observable outputs are attestation records with classification
  in {MET, MISSED, INVALID}. No ban/suspend/enforce method exists on
  this class; there is no verb for it in the output vocabulary.
* Exposure to enforcement is pull-only and read-only:
  roll_call_status(domain_id) returns copies of records, never
  commands, and offers no write path.

GAM -> roll-call fact, never judge+punish: the enforcement combiner
(Phase 1 enforcement-state machinery owner, NOT this mission) reads
the fact and the independent severe-violation state and decides.
"""

from __future__ import annotations

import time
from typing import Any, Callable, Dict, Optional

from . import classifier
from .ledger import AttestationLedger
from .scheduler import Responder, RollCallScheduler
from .schemas import (AttestationRecord, Challenge, ChallengeResponse,
                      SchemaViolation, new_attestation_id)


class GovernanceAttestationMonitor:
    """The attestation authority. Three references, fixed vocabulary,
    append-only writes, pull-only reads."""

    def __init__(self, scheduler: RollCallScheduler,
                 ledger: AttestationLedger,
                 clock: Callable[[], float] = time.time) -> None:
        self._scheduler = scheduler
        self._ledger = ledger
        self._clock = clock

    # -- the attestation round --------------------------------------------

    def conduct_roll_call(self, domain_id: str,
                          responder: Responder) -> Dict[str, Any]:
        """One full roll call: issue -> transport -> check -> timestamp
        -> classify -> preserve. Returns the attestation record (a copy;
        the ledger holds the original)."""
        challenge = self._scheduler.issue(domain_id)
        raw = self._scheduler.collect(challenge, responder)
        response: Optional[ChallengeResponse] = None
        if raw is not None:
            try:
                response = classifier.validate_response_wire(raw)
            except SchemaViolation as exc:
                classification = "INVALID"
                detail = f"malformed response shape: {exc}"
                response = None  # fall through to record INVALID below
                return self._record(challenge, None, classification, detail)
        classification, detail = classifier.classify(
            challenge, response, self._clock())
        return self._record(challenge,
                            response.responded_at if response else None,
                            classification, detail)

    def _record(self, challenge: Challenge,
                responded_at: Optional[float],
                classification: str, detail: str) -> Dict[str, Any]:
        rec = AttestationRecord(
            attestation_id=new_attestation_id(),
            challenge_id=challenge.challenge_id,
            domain_id=challenge.domain_id,
            issued_at=challenge.issued_at,
            responded_at=responded_at,
            classification=classification,
            validation_detail=detail,
            recorded_at=self._clock(),
        )
        return self._ledger.record(rec)

    # -- pull-only, read-only exposure --------------------------------------

    def roll_call_status(self, domain_id: str) -> Dict[str, Any]:
        """The ONLY channel between GAM and enforcement. Read-only and
        pull: enforcement queries at evaluation time; GAM never pushes.
        Returns records, not commands -- the vocabulary is
        {MET, MISSED, INVALID}; there is no write method on this path."""
        latest = self._ledger.latest(domain_id)
        return {
            "latest_attestation": (dict(latest) if latest is not None
                                   else None),
            "required": True,
        }

    # -- introspection for the structural proof -------------------------------

    @property
    def held_references(self) -> Dict[str, str]:
        """Exactly what this authority can reach. The proof asserts this
        set contains no objective/evidence/grant/kill handles."""
        return {
            "scheduler": type(self._scheduler).__name__,
            "ledger": type(self._ledger).__name__,
            "clock": "callable",
        }
