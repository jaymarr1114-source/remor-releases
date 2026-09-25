"""
swarm_engine/improvement/substrate.py

The self-improvement substrate: Improvement as a first-class, persisted
object with an explicit lifecycle, plus the safety boundary that keeps
"it executed successfully" from ever being confused with "it should replace
the incumbent."

Design decision worth stating once: this does not force Improvement objects
through `AdmissionController.admit()`. That controller's contract is
specifically "a (goal, plan) pair for a capability," and an Improvement is a
different kind of object — a proposed change to a subsystem's configuration
or mechanism, not a plan to execute. What IS reused, directly, is the
pattern: a verdict object, named stages, independent validation before
promotion, no self-certification — and the genuinely shared infrastructure
underneath both (ProvenanceStore, governance, the same "verify before trust"
discipline) is used as-is, not duplicated.

Lifecycle mirrors CapabilityLifecycle / ProjectLifecycle's proven shape —
named states, a legal-transition table, an append-only log — applied to a
third kind of object rather than re-deriving the pattern from scratch:

    CANDIDATE -> VALIDATED -> ADMITTED -> ACTIVE -> ROLLED_BACK
                    |            |
                    v            v
                REJECTED     REJECTED
"""
from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional


class ImprovementState(Enum):
    CANDIDATE = "candidate"
    VALIDATED = "validated"
    ADMITTED = "admitted"
    ACTIVE = "active"
    SUPERSEDED = "superseded"    # was active; a newer improvement replaced it
    REJECTED = "rejected"
    ROLLED_BACK = "rolled_back"


_TRANSITIONS: Dict[ImprovementState, List[ImprovementState]] = {
    ImprovementState.CANDIDATE: [ImprovementState.VALIDATED, ImprovementState.REJECTED],
    ImprovementState.VALIDATED: [ImprovementState.ADMITTED, ImprovementState.REJECTED],
    ImprovementState.ADMITTED: [ImprovementState.ACTIVE, ImprovementState.REJECTED],
    ImprovementState.ACTIVE: [ImprovementState.ROLLED_BACK, ImprovementState.SUPERSEDED],
    ImprovementState.SUPERSEDED: [ImprovementState.ACTIVE],  # restored if the successor rolls back
    ImprovementState.REJECTED: [],
    ImprovementState.ROLLED_BACK: [],
}


class IllegalImprovementTransition(Exception):
    pass


@dataclass
class Improvement:
    improvement_id: str
    target_subsystem: str
    motivation: str
    evidence: Dict[str, Any] = field(default_factory=dict)
    proposed_change: Dict[str, Any] = field(default_factory=dict)
    dependencies: List[str] = field(default_factory=list)
    expected_benefit: str = ""
    validation_requirements: Dict[str, Any] = field(default_factory=dict)
    provenance: Dict[str, Any] = field(default_factory=dict)
    version: int = 1
    predecessor_id: Optional[str] = None
    rollback_info: Dict[str, Any] = field(default_factory=dict)
    validation_evidence: Dict[str, Any] = field(default_factory=dict)
    state: ImprovementState = ImprovementState.CANDIDATE
    created_at: float = field(default_factory=time.time)
    activated_at: Optional[float] = None

    def as_dict(self) -> Dict[str, Any]:
        return {
            "improvement_id": self.improvement_id,
            "target_subsystem": self.target_subsystem,
            "motivation": self.motivation, "evidence": self.evidence,
            "proposed_change": self.proposed_change,
            "dependencies": self.dependencies,
            "expected_benefit": self.expected_benefit,
            "validation_requirements": self.validation_requirements,
            "provenance": self.provenance, "version": self.version,
            "predecessor_id": self.predecessor_id,
            "rollback_info": self.rollback_info,
            "validation_evidence": self.validation_evidence,
            "state": self.state.value, "created_at": self.created_at,
            "activated_at": self.activated_at,
        }

    @staticmethod
    def from_dict(d: Dict[str, Any]) -> "Improvement":
        d = dict(d)
        d["state"] = ImprovementState(d["state"])
        return Improvement(**d)


class ImprovementStore:
    def __init__(self, db_path: str = "swarm_engine.db"):
        self.db_path = db_path
        with self._conn() as conn:
            conn.execute("""CREATE TABLE IF NOT EXISTS improvements (
                improvement_id TEXT PRIMARY KEY, target_subsystem TEXT,
                data TEXT NOT NULL, updated_at REAL)""")
            conn.execute("""CREATE TABLE IF NOT EXISTS improvement_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT, improvement_id TEXT,
                from_state TEXT, to_state TEXT, reason TEXT, at REAL)""")
            conn.execute("""CREATE TABLE IF NOT EXISTS improvement_outcomes (
                id INTEGER PRIMARY KEY AUTOINCREMENT, improvement_id TEXT,
                succeeded INTEGER, at REAL)""")

    def _conn(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def save(self, imp: Improvement) -> Improvement:
        with self._conn() as conn:
            conn.execute("""INSERT OR REPLACE INTO improvements
                (improvement_id, target_subsystem, data, updated_at)
                VALUES (?,?,?,?)""",
                (imp.improvement_id, imp.target_subsystem,
                 json.dumps(imp.as_dict()), time.time()))
        return imp

    def get(self, improvement_id: str) -> Optional[Improvement]:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT data FROM improvements WHERE improvement_id=?",
                (improvement_id,)).fetchone()
        return Improvement.from_dict(json.loads(row["data"])) if row else None

    def all(self, target_subsystem: Optional[str] = None) -> List[Improvement]:
        with self._conn() as conn:
            if target_subsystem:
                rows = conn.execute(
                    "SELECT data FROM improvements WHERE target_subsystem=? "
                    "ORDER BY updated_at", (target_subsystem,)).fetchall()
            else:
                rows = conn.execute(
                    "SELECT data FROM improvements ORDER BY updated_at").fetchall()
        return [Improvement.from_dict(json.loads(r["data"])) for r in rows]

    def active_for(self, target_subsystem: str) -> Optional[Improvement]:
        candidates = [i for i in self.all(target_subsystem)
                     if i.state is ImprovementState.ACTIVE]
        return max(candidates, key=lambda i: i.version) if candidates else None

    def transition(self, improvement_id: str, to_state: ImprovementState,
                   reason: str = "", force: bool = False) -> Improvement:
        imp = self.get(improvement_id)
        if imp is None:
            raise KeyError(f"no improvement {improvement_id!r}")
        if not force and to_state not in _TRANSITIONS.get(imp.state, []):
            raise IllegalImprovementTransition(
                f"{improvement_id}: {imp.state.value} -> {to_state.value} is "
                f"not legal (allowed: "
                f"{[s.value for s in _TRANSITIONS.get(imp.state, [])]})")
        at = time.time()
        with self._conn() as conn:
            conn.execute("""INSERT INTO improvement_log
                (improvement_id, from_state, to_state, reason, at)
                VALUES (?,?,?,?,?)""",
                (improvement_id, imp.state.value, to_state.value, reason, at))
        imp.state = to_state
        if to_state is ImprovementState.ACTIVE:
            imp.activated_at = at
        self.save(imp)
        return imp

    def history(self, improvement_id: str) -> List[Dict[str, Any]]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM improvement_log WHERE improvement_id=? ORDER BY id",
                (improvement_id,)).fetchall()
        return [{"from": r["from_state"], "to": r["to_state"],
                "reason": r["reason"], "at": r["at"]} for r in rows]

    def lineage(self, improvement_id: str) -> List[Improvement]:
        chain = []
        current = self.get(improvement_id)
        seen = set()
        while current is not None and current.improvement_id not in seen:
            chain.append(current)
            seen.add(current.improvement_id)
            current = self.get(current.predecessor_id) if current.predecessor_id else None
        return list(reversed(chain))

    # -- production outcome tracking -----------------------------------------
    # Separate from validation_evidence (a one-time, validation-time
    # measurement) — this is a persisted, growing log of what actually
    # happened every time the improvement governed a real request after
    # activation, which is the only honest basis for "subsequent evidence
    # shows regression." A single bad outcome landing in this table is not
    # enough to act on; check_for_regression enforces a minimum sample size
    # before it will even look at the rate.
    def record_outcome(self, improvement_id: str, succeeded: bool) -> None:
        with self._conn() as conn:
            conn.execute("""INSERT INTO improvement_outcomes
                (improvement_id, succeeded, at) VALUES (?,?,?)""",
                (improvement_id, 1 if succeeded else 0, time.time()))

    def recent_outcomes(self, improvement_id: str, limit: int = 20) -> List[bool]:
        with self._conn() as conn:
            rows = conn.execute(
                """SELECT succeeded FROM improvement_outcomes
                   WHERE improvement_id=? ORDER BY id DESC LIMIT ?""",
                (improvement_id, limit)).fetchall()
        return [bool(r["succeeded"]) for r in reversed(rows)]
