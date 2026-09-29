"""Roll-call scheduler: issues challenges on the governance policy schedule.

Phase-0 GAM mechanism design, Section 2 and Section 6 case D. The
schedule is James's governance POLICY -- GAM (and this scheduler)
executes it; it does not set it. Structural rate bounds:

* Config validation at construction: the response window must be far
  larger than the allocation epoch churn (here: >= 2x the 300s FRM
  epoch), the interval must cover the window, and the interval is
  capped so a starved schedule (no challenges for months, blinding
  enforcement) cannot be configured.
* Issuance rate bound: at most one challenge per `min_issue_spacing_s`
  (default: the configured interval). A flood of issue requests is
  refused with RateLimited -- the responder can never be DoS'd through
  this path, and the handler's own cost is a constant nonce echo.
* Duplicate challenge_id: re-issuing an already-issued id returns the
  original challenge and never re-sends -- duplicates are ignored.
"""

from __future__ import annotations

import secrets
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Optional

from .schemas import Challenge, new_challenge_id

#: FRM allocation epoch, seconds (FRM amendment Section 15). The
#: response window must be >> this so routine epoch churn never
#: produces flapping MISSED classifications.
ALLOCATION_EPOCH_S = 300.0


class ScheduleRefused(Exception):
    """The configured schedule violates the governance policy bounds."""


class RateLimited(Exception):
    """An issue request arrived sooner than the rate bound allows."""


@dataclass(frozen=True)
class RollCallPolicy:
    """James's governance policy values (the OPEN values from design
    Section 7.6, with the Section 2 constraints enforced here)."""

    interval_s: float = 3600.0
    response_window_s: float = 900.0
    min_issue_spacing_s: Optional[float] = None  # default: interval_s

    # Policy bounds (constraints, not values):
    min_window_s: float = 2 * ALLOCATION_EPOCH_S  # window >> epoch churn
    max_interval_s: float = 30 * 24 * 3600.0     # interval << ban timescales

    def validate(self) -> "RollCallPolicy":
        if self.response_window_s < self.min_window_s:
            raise ScheduleRefused(
                f"response_window_s={self.response_window_s} < "
                f"min_window_s={self.min_window_s}: the window must be >> "
                f"the {ALLOCATION_EPOCH_S}s allocation epoch")
        if self.interval_s < self.response_window_s:
            raise ScheduleRefused(
                f"interval_s={self.interval_s} < response_window_s="
                f"{self.response_window_s}: windows must not overlap")
        if self.interval_s > self.max_interval_s:
            raise ScheduleRefused(
                f"interval_s={self.interval_s} > max_interval_s="
                f"{self.max_interval_s}: a starved schedule would blind "
                "enforcement")
        spacing = (self.min_issue_spacing_s
                   if self.min_issue_spacing_s is not None
                   else self.interval_s)
        if spacing <= 0:
            raise ScheduleRefused("min_issue_spacing_s must be > 0")
        return self

    @property
    def spacing_s(self) -> float:
        return (self.min_issue_spacing_s
                if self.min_issue_spacing_s is not None
                else self.interval_s)


#: A challenge responder: the domain-side handler. Takes the challenge
#: wire dict, returns the response wire dict, or None for silence.
#: (Phase 2 owns the real Curiosity handler; Phase 1 tests use doubles.)
Responder = Callable[[Dict[str, Any]], Optional[Dict[str, Any]]]


class RollCallScheduler:
    """Issues challenges per the validated policy. The transport here is
    the in-process call path GAM -> domain handler (the governance
    plane invoking the domain's dedicated challenge path); the wire
    format on that path is the fixed schema, validated at both ends."""

    def __init__(self, policy: RollCallPolicy,
                 clock: Callable[[], float] = time.time) -> None:
        self._policy = policy.validate()
        self._clock = clock
        self._issued: Dict[str, Challenge] = {}
        self._last_issue_at: Optional[float] = None
        self._rate_limited_count = 0

    @property
    def policy(self) -> RollCallPolicy:
        return self._policy

    @property
    def rate_limited_count(self) -> int:
        return self._rate_limited_count

    def issue(self, domain_id: str,
              challenge_id: Optional[str] = None) -> Challenge:
        """Build (and register) the next challenge for a domain.

        Rate bound: raises RateLimited when called sooner than
        min_issue_spacing_s after the previous issue. Duplicate
        challenge_id: returns the already-issued challenge, never a
        second send."""
        if not domain_id:
            raise ScheduleRefused("issue: domain_id must be non-empty")
        now = self._clock()
        if challenge_id is not None and challenge_id in self._issued:
            return self._issued[challenge_id]
        if (self._last_issue_at is not None
                and now - self._last_issue_at < self._policy.spacing_s):
            self._rate_limited_count += 1
            raise RateLimited(
                f"issue refused: {now - self._last_issue_at:.3f}s since "
                f"last issue, bound is {self._policy.spacing_s:.3f}s")
        challenge = Challenge(
            challenge_id=challenge_id or new_challenge_id(),
            domain_id=domain_id,
            nonce=secrets.token_bytes(32),
            issued_at=now,
            response_window_s=self._policy.response_window_s,
            required=True,
        )
        self._issued[challenge.challenge_id] = challenge
        self._last_issue_at = now
        return challenge

    def collect(self, challenge: Challenge,
                responder: Responder) -> Optional[Dict[str, Any]]:
        """Send the challenge down the transport and take the raw wire
        response (or None for silence). Schema validation of the
        response happens in the classifier, not here."""
        if challenge.challenge_id not in self._issued:
            raise ScheduleRefused(
                "collect: unknown challenge_id -- the scheduler only "
                "transports challenges it issued")
        return responder(challenge.to_wire())

    def issued_count(self) -> int:
        return len(self._issued)
