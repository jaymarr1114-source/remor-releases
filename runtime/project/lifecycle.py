"""
swarm_engine/project/lifecycle.py

A project's lifecycle as an explicit, persisted state machine — the same
shape as CapabilityLifecycle, deliberately: named states, a legal-transition
table, an append-only log. A project needs the same guarantees a capability
does — a state a restart can recover, a history that can be inspected, no
silent jump from INGESTED straight to COMPLETE that would mean a real stage
got skipped.

Progress within a project is tracked separately (ProjectProgress) so a
resumed run can pick up exactly where it left off rather than re-deriving
progress from scratch.
"""
from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional


class ProjectState(Enum):
    INGESTED = "ingested"
    ANALYZING = "analyzing"
    PLANNING = "planning"
    EXECUTING = "executing"
    VERIFYING = "verifying"
    BLOCKED = "blocked"
    COMPLETE = "complete"
    FAILED = "failed"
    # RETIRED is the locked Phase 2 internal archive: a project the user
    # shelved. It is neither active nor destroyed -- the store model,
    # lifecycle history, and progress are all preserved untouched, and
    # retirement is one more append-only log row (never a deletion).
    # Retirement is administrative: it is reachable from every work state.
    # Resume re-enters at ANALYZING, the normal re-entry point for a
    # project that must re-derive its plan from preserved state.
    RETIRED = "retired"


_TRANSITIONS: Dict[ProjectState, List[ProjectState]] = {
    ProjectState.INGESTED: [ProjectState.ANALYZING, ProjectState.RETIRED],
    ProjectState.ANALYZING: [ProjectState.PLANNING, ProjectState.BLOCKED,
                             ProjectState.RETIRED],
    ProjectState.PLANNING: [ProjectState.EXECUTING, ProjectState.BLOCKED,
                            ProjectState.RETIRED],
    ProjectState.EXECUTING: [ProjectState.VERIFYING, ProjectState.BLOCKED,
                             ProjectState.ANALYZING, ProjectState.RETIRED],
    ProjectState.VERIFYING: [ProjectState.COMPLETE, ProjectState.ANALYZING,
                             ProjectState.FAILED, ProjectState.RETIRED],
    ProjectState.BLOCKED: [ProjectState.ANALYZING, ProjectState.FAILED,
                           ProjectState.RETIRED],
    ProjectState.COMPLETE: [ProjectState.RETIRED],
    ProjectState.FAILED: [ProjectState.ANALYZING, ProjectState.RETIRED],
    ProjectState.RETIRED: [ProjectState.ANALYZING],
}


class IllegalProjectTransition(Exception):
    pass


@dataclass
class ProjectProgress:
    project_id: str
    satisfied_requirements: List[str] = field(default_factory=list)
    outstanding_requirements: List[str] = field(default_factory=list)
    attempted_capabilities: List[str] = field(default_factory=list)
    cycles: int = 0
    updated_at: float = field(default_factory=time.time)

    def as_dict(self) -> Dict[str, Any]:
        total = len(self.satisfied_requirements) + len(self.outstanding_requirements)
        return {"project_id": self.project_id, "cycles": self.cycles,
                "satisfied": len(self.satisfied_requirements),
                "outstanding": len(self.outstanding_requirements),
                "fraction_complete": (len(self.satisfied_requirements) / total
                                      if total else 1.0),
                "satisfied_requirements": self.satisfied_requirements,
                "outstanding_requirements": self.outstanding_requirements,
                "attempted_capabilities": self.attempted_capabilities}


class ProjectLifecycle:
    """Persisted project state machine plus progress tracking."""

    def __init__(self, db_path: str = "swarm_engine.db"):
        self.db_path = db_path
        with self._conn() as conn:
            conn.execute("""CREATE TABLE IF NOT EXISTS project_state (
                project_id TEXT PRIMARY KEY, state TEXT NOT NULL, updated_at REAL)""")
            conn.execute("""CREATE TABLE IF NOT EXISTS project_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT, project_id TEXT,
                from_state TEXT, to_state TEXT, reason TEXT, at REAL)""")
            conn.execute("""CREATE TABLE IF NOT EXISTS project_progress (
                project_id TEXT PRIMARY KEY, satisfied TEXT, outstanding TEXT,
                attempted TEXT, cycles INTEGER, updated_at REAL)""")

    def _conn(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def state_of(self, project_id: str) -> ProjectState:
        with self._conn() as conn:
            row = conn.execute("SELECT state FROM project_state WHERE project_id=?",
                              (project_id,)).fetchone()
        return ProjectState(row["state"]) if row else ProjectState.INGESTED

    def transition(self, project_id: str, to_state: ProjectState,
                   reason: str = "") -> None:
        current = self.state_of(project_id)
        if to_state not in _TRANSITIONS.get(current, []):
            raise IllegalProjectTransition(
                f"{project_id}: {current.value} -> {to_state.value} is not legal "
                f"(allowed: {[s.value for s in _TRANSITIONS.get(current, [])]})")
        at = time.time()
        with self._conn() as conn:
            conn.execute("""INSERT OR REPLACE INTO project_state
                (project_id, state, updated_at) VALUES (?,?,?)""",
                (project_id, to_state.value, at))
            conn.execute("""INSERT INTO project_log
                (project_id, from_state, to_state, reason, at) VALUES (?,?,?,?,?)""",
                (project_id, current.value, to_state.value, reason, at))

    def history(self, project_id: str) -> List[Dict[str, Any]]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM project_log WHERE project_id=? ORDER BY id",
                (project_id,)).fetchall()
        return [{"from": r["from_state"], "to": r["to_state"],
                "reason": r["reason"], "at": r["at"]} for r in rows]

    def save_progress(self, progress: ProjectProgress) -> None:
        progress.updated_at = time.time()
        with self._conn() as conn:
            conn.execute("""INSERT OR REPLACE INTO project_progress
                (project_id, satisfied, outstanding, attempted, cycles, updated_at)
                VALUES (?,?,?,?,?,?)""",
                (progress.project_id, json.dumps(progress.satisfied_requirements),
                 json.dumps(progress.outstanding_requirements),
                 json.dumps(progress.attempted_capabilities), progress.cycles,
                 progress.updated_at))

    def load_progress(self, project_id: str) -> Optional[ProjectProgress]:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM project_progress WHERE project_id=?",
                (project_id,)).fetchone()
        if row is None:
            return None
        return ProjectProgress(
            project_id=project_id,
            satisfied_requirements=json.loads(row["satisfied"] or "[]"),
            outstanding_requirements=json.loads(row["outstanding"] or "[]"),
            attempted_capabilities=json.loads(row["attempted"] or "[]"),
            cycles=row["cycles"] or 0, updated_at=row["updated_at"] or 0.0)
