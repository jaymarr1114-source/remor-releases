"""Transition engine with per-level issuer verification.

Import-guarded: importing this module from inside the curiosity domain raises
:class:`_guard.DomainSeparationError` at import time. The transition table in
:mod:`states` is the decided D-3 rule set; the engine refuses anything outside
it, and refuses any issuer not authorized for the specific transition
(invariant 9: Primary can never override an external safety/ban state for
operational convenience).
"""

from __future__ import annotations

import calendar
import datetime
import time
import uuid
from typing import Callable, Dict, List, Optional

from . import _guard as _domain_guard

_domain_guard.ensure_external_caller()

from . import _guard  # noqa: E402
from ._persistence import KillLedger, StateStore  # noqa: E402
from .states import (  # noqa: E402
    DOMAIN,
    ISSUER_ENFORCEMENT,
    ISSUER_FRM,
    ISSUER_JAMES,
    ISSUER_SAFETY_AUTHORITY,
    TERMINATING_STATES,
    EnforcementRecord,
    EnforcementState,
    RollbackDirective,
    authorized_issuers,
)


class EnforcementError(Exception):
    """Base class for enforcement refusals."""


class TransitionRefused(EnforcementError):
    """The (from_state, to_state) pair is not in the decided D-3 table."""


class IssuerRefused(EnforcementError):
    """The issuer is not authorized for this transition."""


class ReenableRefused(EnforcementError):
    """Re-enable preconditions (epoch grant / expiry / verification) unmet."""


def _add_months(ts: float, months: int) -> float:
    dt = datetime.datetime.fromtimestamp(ts, tz=datetime.timezone.utc)
    month = dt.month - 1 + months
    year = dt.year + month // 12
    month = month % 12 + 1
    day = min(dt.day, calendar.monthrange(year, month)[1])
    return dt.replace(year=year, month=month, day=day).timestamp()


class EnforcementEngine:
    """Holds enforcement state outside the curiosity domain and transitions it
    per the decided D-3 rules with issuer checks."""

    def __init__(
        self,
        state_dir: str,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._store = StateStore(state_dir)
        self._ledger = KillLedger(state_dir)
        self._clock = clock

    # -- reads ------------------------------------------------------------

    def current(self, domain: str = DOMAIN) -> EnforcementRecord:
        """Current record; a domain with no record is implicitly RUNNING."""
        rec = self._store.read_record(domain)
        if rec is None:
            return EnforcementRecord(
                domain=domain,
                state=EnforcementState.RUNNING,
                prev_state=None,
                issuer="bootstrap",
                reason_refs={},
                entered_at=self._clock(),
            )
        return rec

    def rollback_status(self, domain: str = DOMAIN) -> List[RollbackDirective]:
        return self._store.read_directives(domain)

    # -- transitions --------------------------------------------------------

    def transition(
        self,
        domain: str,
        to_state: EnforcementState,
        issuer: str,
        reason_refs: Optional[Dict] = None,
        preserved_refs: Optional[Dict] = None,
    ) -> EnforcementRecord:
        now = self._clock()
        current = self.current(domain)
        from_state = current.state
        reason_refs = dict(reason_refs or {})
        preserved_refs = dict(preserved_refs or {})

        allowed = authorized_issuers(from_state, to_state)
        if not allowed:
            raise TransitionRefused(
                f"{from_state.value} -> {to_state.value} is not a decided transition"
            )
        if issuer not in allowed:
            raise IssuerRefused(
                f"issuer {issuer!r} is not authorized for "
                f"{from_state.value} -> {to_state.value}"
            )

        expires_at: Optional[float] = None
        if to_state is EnforcementState.BANNED_6M:
            # Six-month ban, decided L3 re-entry rule.
            expires_at = _add_months(now, 6)
        if from_state is EnforcementState.BANNED_6M and to_state is EnforcementState.RUNNING:
            if now < (current.expires_at or float("inf")):
                raise ReenableRefused("ban has not expired")
            if not reason_refs.get("verification_ref"):
                raise ReenableRefused(
                    "post-ban re-entry requires a verification record ref"
                )
        if (
            from_state is EnforcementState.HARD_SHUTDOWN_RESOURCE
            and to_state is EnforcementState.RUNNING
        ):
            # L1 re-enable: automatic eligibility at the next valid grant
            # epoch -- the FRM must actually grant resources again.
            if not reason_refs.get("grant_ref"):
                raise ReenableRefused(
                    "L1 re-enable requires a new FRM grant ref (next valid epoch)"
                )

        record = EnforcementRecord(
            domain=domain,
            state=to_state,
            prev_state=from_state,
            issuer=issuer,
            reason_refs=reason_refs,
            preserved_refs=preserved_refs,
            entered_at=now,
            expires_at=expires_at,
        )
        self._store.write_record(record)

        if to_state in TERMINATING_STATES:
            self._ledger.append_killed(
                domain=domain,
                entered_state=to_state.value,
                issuer=issuer,
                reason_refs=reason_refs,
                entered_at=now,
            )
        if to_state is EnforcementState.SUSPENDED_SAFETY:
            self._record_rollback_directive(domain, reason_refs, now)
        return record

    def re_enable(
        self,
        domain: str,
        issuer: str,
        reason_refs: Optional[Dict] = None,
    ) -> EnforcementRecord:
        """Re-enable path: WARNING_1/SUSPENDED_SAFETY -> RUNNING (James),
        HARD_SHUTDOWN_RESOURCE -> RUNNING (FRM, new grant),
        BANNED_6M -> RUNNING (James, after expiry + verification)."""
        return self.transition(domain, EnforcementState.RUNNING, issuer, reason_refs)

    # -- L2 rollback directive + verification hook ---------------------------

    def _record_rollback_directive(
        self, domain: str, reason_refs: Dict, now: float
    ) -> RollbackDirective:
        _guard.ensure_external_caller()
        directives = self._store.read_directives(domain)
        directive = RollbackDirective(
            directive_id=str(uuid.uuid4()),
            domain=domain,
            checkpoint_ref=reason_refs.get("last_checkin_ref", "unknown"),
            recorded_at=now,
        )
        directives.append(directive)
        self._store.write_directives(domain, directives)
        return directive

    def acknowledge_rollback(
        self, domain: str, directive_id: str, ack_ref: str
    ) -> RollbackDirective:
        """Verification hook: the domain-side wipe (Phase 2) reports back with
        an ack ref; enforcement marks the directive acknowledged."""
        _guard.ensure_external_caller()
        directives = self._store.read_directives(domain)
        for directive in directives:
            if directive.directive_id == directive_id:
                directive.status = "acknowledged"
                directive.ack_ref = ack_ref
                self._store.write_directives(domain, directives)
                return directive
        raise EnforcementError(f"unknown rollback directive {directive_id!r}")

    # -- kill ledger access (reads) ------------------------------------------

    def kill_ledger(self, domain: str = DOMAIN) -> List[dict]:
        return self._ledger.records(domain)

    def verify_kill_ledger(self):
        return self._ledger.verify_chain()
