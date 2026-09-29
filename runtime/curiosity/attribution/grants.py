"""CUR-P1D — allocation-site metering (grant → controller).

Frozen interface (CUR-P1A block):
    FRM grant: {grant_id, epoch_id, epoch_s (default 300),
                dimensions: {budget_s, max_concurrent},
                primary_minimum: {budget_s, max_concurrent},
                lent: bool}
    Zero allocation while any enforcement state is active.
    Lending epoch-bounded; grants non-preemptive.

Charter: A11 (allocation mechanism grants), A5 (Run Controller
apportions — Phase 2), A6 (loop controller enforces — Phase 2),
A32 (the calling controller's budget pays). The allocation ledger
here records grant → controller bindings; it is the first link of
the attribution chain (resource allocation).
"""
from __future__ import annotations

import sqlite3
import time
import uuid
from dataclasses import dataclass, field, asdict
from typing import Any, Dict, List, Optional


# ---------------------------------------------------------------------------
# frozen vocabulary
# ---------------------------------------------------------------------------

ENFORCEMENT_STATES = (
    "RUNNING",
    "HARD_SHUTDOWN_RESOURCE",
    "WARNING_1",
    "SUSPENDED_SAFETY",
    "BANNED_6M",
)

#: Zero allocation while any of these states is active. RUNNING is the
#: only state under which allocation may proceed.
ALLOC_BLOCKING_STATES = frozenset(s for s in ENFORCEMENT_STATES if s != "RUNNING")


class EnforcementStatusProvider:
    """Inlet for the enforcement-state fact.

    CUR-P1B builds the enforcement-state machine; this package must not
    invent it. The inlet exposes the frozen state vocabulary only.
    A provider answers ``active_state(domain)`` with one of
    ENFORCEMENT_STATES.
    """

    def active_state(self, domain: str) -> str:  # pragma: no cover - interface
        raise NotImplementedError


class AllocationRefused(Exception):
    """An allocation was refused: enforcement active, grant malformed, or
    epoch over. Refusals are values the caller must handle, never silent
    downgrades."""


# ---------------------------------------------------------------------------
# records
# ---------------------------------------------------------------------------

@dataclass
class Grant:
    """The frozen FRM grant shape. Exactly the fields of the frozen
    interface — nothing added, nothing renamed."""
    grant_id: str
    epoch_id: str
    epoch_s: int = 300
    dimensions: Dict[str, float] = field(default_factory=dict)   # {budget_s, max_concurrent}
    primary_minimum: Dict[str, float] = field(default_factory=dict)
    lent: bool = False
    issued_at: float = field(default_factory=time.time)

    def validate(self) -> "Grant":
        if not self.grant_id or not self.epoch_id:
            raise AllocationRefused("grant requires grant_id and epoch_id")
        if self.epoch_s <= 0:
            raise AllocationRefused(f"grant {self.grant_id!r}: epoch_s must be positive")
        for key in ("budget_s", "max_concurrent"):
            if key not in self.dimensions:
                raise AllocationRefused(
                    f"grant {self.grant_id!r}: dimensions missing {key!r}")
            if self.dimensions[key] < 0:
                raise AllocationRefused(
                    f"grant {self.grant_id!r}: dimensions[{key}] negative")
        return self

    def expired(self, now: Optional[float] = None) -> bool:
        return (now if now is not None else time.time()) - self.issued_at > self.epoch_s


@dataclass
class Allocation:
    """Allocation-site metering record: a grant bound to a specific
    controller (the caller whose budget pays, A32)."""
    allocation_id: str
    grant_id: str
    epoch_id: str
    controller_id: str            # originating Run Controller (FRM §7)
    originating_executive: str    # originating executive (FRM §7)
    budget_s: float
    max_concurrent: int
    lent: bool
    allocated_at: float = field(default_factory=time.time)


# ---------------------------------------------------------------------------
# ledger
# ---------------------------------------------------------------------------

_SCHEMA = """
CREATE TABLE IF NOT EXISTS grants (
    grant_id TEXT PRIMARY KEY,
    epoch_id TEXT NOT NULL,
    epoch_s INTEGER NOT NULL,
    dimensions TEXT NOT NULL,
    primary_minimum TEXT NOT NULL,
    lent INTEGER NOT NULL,
    issued_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS allocations (
    allocation_id TEXT PRIMARY KEY,
    grant_id TEXT NOT NULL,
    epoch_id TEXT NOT NULL,
    controller_id TEXT NOT NULL,
    originating_executive TEXT NOT NULL,
    budget_s REAL NOT NULL,
    max_concurrent INTEGER NOT NULL,
    lent INTEGER NOT NULL,
    allocated_at REAL NOT NULL,
    FOREIGN KEY (grant_id) REFERENCES grants(grant_id)
);
"""


class AllocationLedger:
    """Append-only allocation ledger. Grants are recorded once;
    allocations bind a grant to a controller. The ledger is the
    allocation link of the attribution chain."""

    def __init__(self, db_path: str):
        self.db_path = db_path
        self._conn = sqlite3.connect(db_path)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    # -- grants -----------------------------------------------------------
    def record_grant(self, grant: Grant) -> Grant:
        grant.validate()
        self._conn.execute(
            "INSERT INTO grants VALUES (?,?,?,?,?,?,?)",
            (grant.grant_id, grant.epoch_id, grant.epoch_s,
             _json(grant.dimensions), _json(grant.primary_minimum),
             int(grant.lent), grant.issued_at),
        )
        self._conn.commit()
        return grant

    def get_grant(self, grant_id: str) -> Optional[Grant]:
        row = self._conn.execute(
            "SELECT * FROM grants WHERE grant_id=?", (grant_id,)).fetchone()
        return _grant_from_row(row) if row else None

    # -- allocations ------------------------------------------------------
    def allocate(self, grant_id: str, controller_id: str,
                 originating_executive: str,
                 enforcement: EnforcementStatusProvider,
                 domain: str = "curiosity") -> Allocation:
        """Bind a grant to a controller. Refuses (never silently
        downgrades) when any enforcement state is active, when the
        grant is unknown, or when the epoch is over."""
        state = enforcement.active_state(domain)
        if state not in ENFORCEMENT_STATES:
            raise AllocationRefused(
                f"unknown enforcement state {state!r}: fail closed")
        if state in ALLOC_BLOCKING_STATES:
            raise AllocationRefused(
                f"zero allocation while enforcement state {state} is active "
                f"(domain {domain})")
        grant = self.get_grant(grant_id)
        if grant is None:
            raise AllocationRefused(f"unknown grant {grant_id!r}")
        if grant.expired():
            raise AllocationRefused(
                f"grant {grant_id!r}: epoch {grant.epoch_id} over "
                "(lending is epoch-bounded)")
        alloc = Allocation(
            allocation_id=str(uuid.uuid4()),
            grant_id=grant.grant_id,
            epoch_id=grant.epoch_id,
            controller_id=controller_id,
            originating_executive=originating_executive,
            budget_s=float(grant.dimensions["budget_s"]),
            max_concurrent=int(grant.dimensions["max_concurrent"]),
            lent=grant.lent,
        )
        self._conn.execute(
            "INSERT INTO allocations VALUES (?,?,?,?,?,?,?,?,?)",
            (alloc.allocation_id, alloc.grant_id, alloc.epoch_id,
             alloc.controller_id, alloc.originating_executive,
             alloc.budget_s, alloc.max_concurrent, int(alloc.lent),
             alloc.allocated_at),
        )
        self._conn.commit()
        return alloc

    def allocations_for_grant(self, grant_id: str) -> List[Allocation]:
        rows = self._conn.execute(
            "SELECT * FROM allocations WHERE grant_id=?", (grant_id,)).fetchall()
        return [_alloc_from_row(r) for r in rows]

    def close(self) -> None:
        self._conn.commit()
        self._conn.close()


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

import json as _json_mod  # noqa: E402  (kept local to avoid name clash)


def _json(obj: Any) -> str:
    return _json_mod.dumps(obj, sort_keys=True)


def _grant_from_row(row: sqlite3.Row) -> Grant:
    return Grant(
        grant_id=row["grant_id"], epoch_id=row["epoch_id"],
        epoch_s=row["epoch_s"],
        dimensions=_json_mod.loads(row["dimensions"]),
        primary_minimum=_json_mod.loads(row["primary_minimum"]),
        lent=bool(row["lent"]), issued_at=row["issued_at"])


def _alloc_from_row(row: sqlite3.Row) -> Allocation:
    return Allocation(
        allocation_id=row["allocation_id"], grant_id=row["grant_id"],
        epoch_id=row["epoch_id"], controller_id=row["controller_id"],
        originating_executive=row["originating_executive"],
        budget_s=row["budget_s"], max_concurrent=row["max_concurrent"],
        lent=bool(row["lent"]), allocated_at=row["allocated_at"])
