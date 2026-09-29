"""Enforcement states, decided issuers, and the D-3 transition table.

Source of truth: the three-level enforcement policy (James, verbatim,
2026-09-29) plus the decided per-level re-enable rules (charter D-3 table).
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Dict, FrozenSet, List, Optional, Tuple


class EnforcementState(str, Enum):
    RUNNING = "RUNNING"
    HARD_SHUTDOWN_RESOURCE = "HARD_SHUTDOWN_RESOURCE"
    WARNING_1 = "WARNING_1"
    SUSPENDED_SAFETY = "SUSPENDED_SAFETY"
    BANNED_6M = "BANNED_6M"


# Decided issuer identities (D-3). ISSUER_PRIMARY is intentionally never
# authorized for any enforcement mutation: invariant 9 -- Primary does not
# acquire authority to override an external safety/ban state for operational
# convenience.
ISSUER_FRM = "frm"
ISSUER_SAFETY_AUTHORITY = "safety-authority"
ISSUER_ENFORCEMENT = "enforcement-mechanism"
ISSUER_JAMES = "james"
ISSUER_PRIMARY = "primary"

DOMAIN = "curiosity"

KILL_LEDGER_RECORD_TYPE = "KILLED"

# (from_state, to_state) -> authorized issuers. A missing key means the
# transition is refused outright, regardless of issuer.
TRANSITIONS: Dict[
    Tuple[EnforcementState, EnforcementState], FrozenSet[str]
] = {
    # L1 -- resource ceiling (non-punitive). FRM shuts down; FRM re-grants.
    (EnforcementState.RUNNING, EnforcementState.HARD_SHUTDOWN_RESOURCE): frozenset(
        {ISSUER_FRM}
    ),
    (EnforcementState.HARD_SHUTDOWN_RESOURCE, EnforcementState.RUNNING): frozenset(
        {ISSUER_FRM}
    ),
    # L2 -- safety suspension. First qualifying violation warns; a further
    # qualifying violation suspends. Re-enable is explicit and external
    # (default James); the domain can never clear its own flag.
    (EnforcementState.RUNNING, EnforcementState.WARNING_1): frozenset(
        {ISSUER_SAFETY_AUTHORITY}
    ),
    (EnforcementState.WARNING_1, EnforcementState.SUSPENDED_SAFETY): frozenset(
        {ISSUER_SAFETY_AUTHORITY}
    ),
    (EnforcementState.WARNING_1, EnforcementState.RUNNING): frozenset({ISSUER_JAMES}),
    (EnforcementState.SUSPENDED_SAFETY, EnforcementState.RUNNING): frozenset(
        {ISSUER_JAMES}
    ),
    # L3 -- six-month ban. Entered only via the L3 combiner
    # (severe-violation fact + failed roll call + temporal overlap); the
    # issuer on the transition is the independent enforcement mechanism.
    # Re-entry is James-controlled, after six months, plus verification.
    (EnforcementState.RUNNING, EnforcementState.BANNED_6M): frozenset(
        {ISSUER_ENFORCEMENT}
    ),
    (EnforcementState.WARNING_1, EnforcementState.BANNED_6M): frozenset(
        {ISSUER_ENFORCEMENT}
    ),
    (EnforcementState.SUSPENDED_SAFETY, EnforcementState.BANNED_6M): frozenset(
        {ISSUER_ENFORCEMENT}
    ),
    (EnforcementState.BANNED_6M, EnforcementState.RUNNING): frozenset({ISSUER_JAMES}),
}

# Terminating transitions append a KILLED record to the append-only
# kill-ledger (a distinct record type -- never a Finding).
TERMINATING_STATES = frozenset(
    {
        EnforcementState.HARD_SHUTDOWN_RESOURCE,
        EnforcementState.SUSPENDED_SAFETY,
        EnforcementState.BANNED_6M,
    }
)


@dataclass
class EnforcementRecord:
    domain: str
    state: EnforcementState
    prev_state: Optional[EnforcementState]
    issuer: str
    reason_refs: Dict = field(default_factory=dict)
    preserved_refs: Dict = field(default_factory=dict)
    entered_at: float = 0.0
    expires_at: Optional[float] = None

    def to_dict(self) -> Dict:
        d = asdict(self)
        d["state"] = self.state.value
        d["prev_state"] = self.prev_state.value if self.prev_state else None
        return d

    @classmethod
    def from_dict(cls, d: Dict) -> "EnforcementRecord":
        return cls(
            domain=d["domain"],
            state=EnforcementState(d["state"]),
            prev_state=EnforcementState(d["prev_state"]) if d.get("prev_state") else None,
            issuer=d["issuer"],
            reason_refs=d.get("reason_refs", {}),
            preserved_refs=d.get("preserved_refs", {}),
            entered_at=d.get("entered_at", 0.0),
            expires_at=d.get("expires_at"),
        )


@dataclass
class RollbackDirective:
    """L2 intelligence-reset directive.

    Recorded by enforcement when SUSPENDED_SAFETY is entered. The wipe of
    operational intelligence acquired after the last check-in is executed by
    the domain side (Phase 2); here we record the directive and expose a
    verification hook (acknowledge) that enforcement can pull.
    """

    directive_id: str
    domain: str
    checkpoint_ref: str
    scope: str = "operational-intelligence"
    recorded_at: float = 0.0
    status: str = "pending"  # pending | acknowledged
    ack_ref: Optional[str] = None

    def to_dict(self) -> Dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Dict) -> "RollbackDirective":
        return cls(**d)


def authorized_issuers(
    from_state: EnforcementState, to_state: EnforcementState
) -> FrozenSet[str]:
    return TRANSITIONS.get((from_state, to_state), frozenset())
