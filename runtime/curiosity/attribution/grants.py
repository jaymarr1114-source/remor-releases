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

U-7 grant-shape migration (GRANT-MIGRATE-1, 2026-10-01 — standing
decision U-7, James 2026-09-30: "FrmGrant wins"):
    The ledger records ``runtime.curiosity.frm.grant.FrmGrant`` directly.
    The old mutable attribution ``Grant`` (epoch_id: str, falsely
    documented as "the frozen FRM grant shape") was removed — it was a
    competing canonical grant contract, an architectural contradiction.
    Exactly one grant contract exists in the tree: FrmGrant (frozen,
    epoch_id: int). ``swarm_engine.primitives.core.Grant``
    (``PrimitiveGrant``) is a DIFFERENT concept — an effect permission
    for the primitive governor (Effect + target pattern), not an FRM
    resource grant — and is intentionally untouched.
"""
from __future__ import annotations

import sqlite3
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

# NOTE: absolute swarm_engine import is load-bearing. runtime/curiosity and
# pylib/swarm_engine/curiosity are the same files (hardlinked); a relative
# "..frm.grant" import would bind a DIFFERENT FrmGrant class object
# depending on which namespace imported this module first, breaking the
# isinstance gate below. Every FrmGrant consumer in the tree imports via
# the swarm_engine namespace — this module must too.
from swarm_engine.curiosity.frm.grant import FrmGrant, LendingRecord


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
# grant validation (the single canonical contract is FrmGrant)
# ---------------------------------------------------------------------------

def validate_grant(grant: FrmGrant) -> FrmGrant:
    """Enforce the allocation-time invariants on a canonical grant.

    ``FrmGrant.as_dict()`` asserts the frozen six-key shape; the checks
    below are the allocation leg's own invariants (carried over from the
    retired attribution ``Grant.validate``). Anything that is not an
    ``FrmGrant`` is refused — never silently coerced.
    """
    if not isinstance(grant, FrmGrant):
        raise AllocationRefused(
            f"grant must be FrmGrant, got {type(grant).__name__}: fail closed")
    shape = grant.as_dict()  # frozen-shape assertion lives here
    if not grant.grant_id:
        raise AllocationRefused("grant requires grant_id")
    if not isinstance(grant.epoch_id, int):
        raise AllocationRefused(
            f"grant {grant.grant_id!r}: epoch_id must be int, "
            f"got {type(grant.epoch_id).__name__}")
    if grant.epoch_s <= 0:
        raise AllocationRefused(
            f"grant {grant.grant_id!r}: epoch_s must be positive")
    for key in ("budget_s", "max_concurrent"):
        if shape["dimensions"][key] < 0:
            raise AllocationRefused(
                f"grant {grant.grant_id!r}: dimensions[{key}] negative")
    return grant


def grant_expired(grant: FrmGrant, now: Optional[float] = None) -> bool:
    """True when the grant's epoch is over (lending is epoch-bounded)."""
    return (now if now is not None else time.time()) - grant.issued_at > grant.epoch_s


# ---------------------------------------------------------------------------
# records
# ---------------------------------------------------------------------------

@dataclass
class Allocation:
    """Allocation-site metering record: a grant bound to a specific
    controller (the caller whose budget pays, A32)."""
    allocation_id: str
    grant_id: str
    epoch_id: int
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
    epoch_id INTEGER NOT NULL,
    epoch_s REAL NOT NULL,
    dimensions TEXT NOT NULL,
    primary_minimum TEXT NOT NULL,
    lent INTEGER NOT NULL,
    issued_at REAL NOT NULL,
    domain TEXT NOT NULL DEFAULT '',
    lending_json TEXT
);
CREATE TABLE IF NOT EXISTS allocations (
    allocation_id TEXT PRIMARY KEY,
    grant_id TEXT NOT NULL,
    epoch_id INTEGER NOT NULL,
    controller_id TEXT NOT NULL,
    originating_executive TEXT NOT NULL,
    budget_s REAL NOT NULL,
    max_concurrent INTEGER NOT NULL,
    lent INTEGER NOT NULL,
    allocated_at REAL NOT NULL,
    FOREIGN KEY (grant_id) REFERENCES grants(grant_id)
);
"""


def _migrate_schema(conn: sqlite3.Connection) -> None:
    """Bring pre-migration ledger DBs (mutable-Grant era) up to the
    FrmGrant schema. New tables are created by _SCHEMA directly."""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(grants)").fetchall()}
    if "domain" not in cols:
        conn.execute("ALTER TABLE grants ADD COLUMN domain TEXT NOT NULL DEFAULT ''")
    if "lending_json" not in cols:
        conn.execute("ALTER TABLE grants ADD COLUMN lending_json TEXT")
    conn.commit()


class AllocationLedger:
    """Append-only allocation ledger. Grants are recorded once;
    allocations bind a grant to a controller. The ledger is the
    allocation link of the attribution chain."""

    def __init__(self, db_path: str):
        self.db_path = db_path
        self._conn = sqlite3.connect(db_path)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_SCHEMA)
        _migrate_schema(self._conn)
        self._conn.commit()

    # -- grants -----------------------------------------------------------
    def record_grant(self, grant: FrmGrant) -> FrmGrant:
        validate_grant(grant)
        shape = grant.as_dict()
        self._conn.execute(
            "INSERT INTO grants VALUES (?,?,?,?,?,?,?,?,?)",
            (grant.grant_id, grant.epoch_id, grant.epoch_s,
             _json(shape["dimensions"]), _json(shape["primary_minimum"]),
             int(grant.lent), grant.issued_at, grant.domain,
             _json(grant.lending.as_dict())),
        )
        self._conn.commit()
        return grant

    def get_grant(self, grant_id: str) -> Optional[FrmGrant]:
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
        if grant_expired(grant):
            raise AllocationRefused(
                f"grant {grant_id!r}: epoch {grant.epoch_id} over "
                "(lending is epoch-bounded)")
        alloc = Allocation(
            allocation_id=str(uuid.uuid4()),
            grant_id=grant.grant_id,
            epoch_id=grant.epoch_id,
            controller_id=controller_id,
            originating_executive=originating_executive,
            budget_s=float(grant.budget_s),
            max_concurrent=int(grant.max_concurrent),
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


def _grant_from_row(row: sqlite3.Row) -> FrmGrant:
    # enforcement_state_at_issue / note are round-record data, not ledger
    # data (see FrmGrant.as_dict docstring); they reconstruct as defaults.
    dims = _json_mod.loads(row["dimensions"])
    pm = _json_mod.loads(row["primary_minimum"])
    try:
        epoch_id = int(row["epoch_id"])
    except (TypeError, ValueError):
        raise AllocationRefused(
            f"grant {row['grant_id']!r}: legacy epoch_id {row['epoch_id']!r} "
            "is not an integer — U-7 unified epoch_id to int; a row "
            "predating the migration cannot be honestly converted")
    lending_raw = row["lending_json"]
    if lending_raw:
        lj = _json_mod.loads(lending_raw)
        lending = LendingRecord(
            lent_budget_s=float(lj["lent_budget_s"]),
            lent_concurrent=int(lj["lent_concurrent"]),
            source=lj.get("source", "primary_unused_minimum"),
            recalled=bool(lj.get("recalled", False)),
        )
    else:
        lending = LendingRecord(0.0, 0)
    return FrmGrant(
        grant_id=row["grant_id"], epoch_id=epoch_id,
        epoch_s=float(row["epoch_s"]), domain=row["domain"] or "",
        budget_s=float(dims["budget_s"]),
        max_concurrent=int(dims["max_concurrent"]),
        primary_minimum_budget_s=float(pm["budget_s"]),
        primary_minimum_concurrent=int(pm["max_concurrent"]),
        lent=bool(row["lent"]), lending=lending,
        issued_at=float(row["issued_at"]))


def _alloc_from_row(row: sqlite3.Row) -> Allocation:
    return Allocation(
        allocation_id=row["allocation_id"], grant_id=row["grant_id"],
        epoch_id=int(row["epoch_id"]), controller_id=row["controller_id"],
        originating_executive=row["originating_executive"],
        budget_s=row["budget_s"], max_concurrent=row["max_concurrent"],
        lent=bool(row["lent"]), allocated_at=row["allocated_at"])
