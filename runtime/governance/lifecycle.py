"""
swarm_engine/governance/lifecycle.py

The capability lifecycle as an explicit state machine, and the graph of
successor/predecessor relationships between capabilities.

ProvenanceStore already tracks trust and lineage; this sits above it and adds
what was implicit: a named state per capability (DISCOVERED through
DEPRECATED), a transition log, and illegal-transition rejection — a
capability cannot jump from CANDIDATE straight to DEPLOYED, because that would
mean something was skipped, and the whole point of the state machine is that
nothing can be skipped silently.
"""
from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional


class LifecycleState(Enum):
    DISCOVERED = "discovered"
    CANDIDATE = "candidate"
    CONSTRUCTED = "constructed"
    VALIDATING = "validating"
    VERIFIED = "verified"
    ADMITTED = "admitted"
    REGISTERED = "registered"
    DEPLOYED = "deployed"
    MONITORED = "monitored"
    IMPROVED = "improved"
    VERSIONED = "versioned"
    DEPRECATED = "deprecated"
    QUARANTINED = "quarantined"
    ROLLED_BACK = "rolled_back"


# The legal graph. Anything not listed as a value here cannot be transitioned
# to from that state — an attempt raises rather than silently succeeding,
# because a lifecycle that accepts any transition is not a lifecycle.
_TRANSITIONS: Dict[LifecycleState, List[LifecycleState]] = {
    LifecycleState.DISCOVERED: [LifecycleState.CANDIDATE, LifecycleState.QUARANTINED],
    LifecycleState.CANDIDATE: [LifecycleState.CONSTRUCTED, LifecycleState.QUARANTINED],
    LifecycleState.CONSTRUCTED: [LifecycleState.VALIDATING, LifecycleState.QUARANTINED],
    LifecycleState.VALIDATING: [LifecycleState.VERIFIED, LifecycleState.QUARANTINED],
    LifecycleState.VERIFIED: [LifecycleState.ADMITTED, LifecycleState.QUARANTINED],
    LifecycleState.ADMITTED: [LifecycleState.REGISTERED, LifecycleState.QUARANTINED],
    LifecycleState.REGISTERED: [LifecycleState.DEPLOYED, LifecycleState.QUARANTINED],
    LifecycleState.DEPLOYED: [LifecycleState.MONITORED, LifecycleState.QUARANTINED,
                              LifecycleState.ROLLED_BACK],
    LifecycleState.MONITORED: [LifecycleState.IMPROVED, LifecycleState.DEPLOYED,
                               LifecycleState.QUARANTINED, LifecycleState.DEPRECATED],
    LifecycleState.IMPROVED: [LifecycleState.VERSIONED, LifecycleState.QUARANTINED],
    LifecycleState.VERSIONED: [LifecycleState.DEPLOYED, LifecycleState.DEPRECATED],
    LifecycleState.DEPRECATED: [],
    LifecycleState.QUARANTINED: [LifecycleState.ROLLED_BACK, LifecycleState.DEPRECATED,
                                 LifecycleState.CANDIDATE],  # re-attempt after repair
    LifecycleState.ROLLED_BACK: [LifecycleState.DEPLOYED],
}


class IllegalTransition(Exception):
    pass


@dataclass
class Transition:
    capability_id: str
    from_state: LifecycleState
    to_state: LifecycleState
    reason: str
    at: float = field(default_factory=time.time)

    def as_dict(self) -> Dict[str, Any]:
        return {"capability_id": self.capability_id, "from": self.from_state.value,
                "to": self.to_state.value, "reason": self.reason, "at": self.at}


class CapabilityLifecycle:
    """Tracks lifecycle state and successor/predecessor relationships."""

    def __init__(self, db_path: str = "swarm_engine.db"):
        self.db_path = db_path
        with self._conn() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS lifecycle_state (
                    capability_id TEXT PRIMARY KEY, state TEXT NOT NULL,
                    updated_at REAL)""")
            conn.execute("""
                CREATE TABLE IF NOT EXISTS lifecycle_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, capability_id TEXT,
                    from_state TEXT, to_state TEXT, reason TEXT, at REAL)""")
            conn.execute("""
                CREATE TABLE IF NOT EXISTS lifecycle_succession (
                    predecessor TEXT, successor TEXT, reason TEXT, at REAL,
                    PRIMARY KEY (predecessor, successor))""")

    def _conn(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def state_of(self, capability_id: str) -> LifecycleState:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT state FROM lifecycle_state WHERE capability_id=?",
                (capability_id,)).fetchone()
        return LifecycleState(row["state"]) if row else LifecycleState.DISCOVERED

    def transition(self, capability_id: str, to_state: LifecycleState,
                   reason: str = "", force: bool = False) -> Transition:
        current = self.state_of(capability_id)
        if not force and to_state not in _TRANSITIONS.get(current, []):
            raise IllegalTransition(
                f"{capability_id}: {current.value} -> {to_state.value} is not a "
                f"legal transition (allowed: "
                f"{[s.value for s in _TRANSITIONS.get(current, [])]})")

        record = Transition(capability_id, current, to_state, reason)
        with self._conn() as conn:
            conn.execute("""INSERT OR REPLACE INTO lifecycle_state
                (capability_id, state, updated_at) VALUES (?,?,?)""",
                (capability_id, to_state.value, record.at))
            conn.execute("""INSERT INTO lifecycle_log
                (capability_id, from_state, to_state, reason, at) VALUES (?,?,?,?,?)""",
                (capability_id, current.value, to_state.value, reason, record.at))
        return record

    def history(self, capability_id: str) -> List[Transition]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM lifecycle_log WHERE capability_id=? ORDER BY id",
                (capability_id,)).fetchall()
        return [Transition(r["capability_id"], LifecycleState(r["from_state"]),
                           LifecycleState(r["to_state"]), r["reason"], r["at"])
                for r in rows]

    def record_succession(self, predecessor: str, successor: str, reason: str) -> None:
        """A new version supersedes an old one. Both remain queryable."""
        with self._conn() as conn:
            conn.execute("""INSERT OR REPLACE INTO lifecycle_succession
                (predecessor, successor, reason, at) VALUES (?,?,?,?)""",
                (predecessor, successor, reason, time.time()))

    def successor_of(self, capability_id: str) -> Optional[str]:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT successor FROM lifecycle_succession WHERE predecessor=?",
                (capability_id,)).fetchone()
        return row["successor"] if row else None

    def predecessor_of(self, capability_id: str) -> Optional[str]:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT predecessor FROM lifecycle_succession WHERE successor=?",
                (capability_id,)).fetchone()
        return row["predecessor"] if row else None

    def by_state(self, state: LifecycleState) -> List[str]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT capability_id FROM lifecycle_state WHERE state=?",
                (state.value,)).fetchall()
        return [r["capability_id"] for r in rows]

    def summary(self) -> Dict[str, int]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT state, COUNT(*) c FROM lifecycle_state GROUP BY state").fetchall()
        return {r["state"]: r["c"] for r in rows}
