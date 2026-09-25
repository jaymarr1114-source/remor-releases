"""
swarm_engine/agents/blackboard.py

Shared state Agent 0 and the five roles read and write to.

Without this, "agents" are just functions called in sequence with return
values passed by the caller — nothing is actually shared, and one role
cannot see what another discovered except through whatever Agent 0 happens
to forward. A blackboard makes discoveries genuinely visible: the Researcher
posts a gap analysis, the Builder can read it directly rather than trusting
Agent 0's summary of it, and everything is persisted so a later session can
see what an earlier one found.

Entries are namespaced by project so multiple concurrent objectives don't
collide, and every write is attributed to the role that made it — this is
what makes reconciliation possible: when two roles' entries for the same key
disagree, that disagreement is visible as data, not lost in whichever value
happened to be written last.
"""
from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class BlackboardEntry:
    project: str
    key: str
    role: str
    value: Any
    at: float = field(default_factory=time.time)

    def as_dict(self) -> Dict[str, Any]:
        return {"project": self.project, "key": self.key, "role": self.role,
                "value": self.value, "at": self.at}


class Blackboard:
    """Persisted, attributed, append-only shared memory."""

    def __init__(self, db_path: str = "swarm_engine.db"):
        self.db_path = db_path
        with self._conn() as conn:
            conn.execute("""CREATE TABLE IF NOT EXISTS blackboard (
                id INTEGER PRIMARY KEY AUTOINCREMENT, project TEXT, key TEXT,
                role TEXT, value TEXT, at REAL)""")

    def _conn(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def post(self, project: str, key: str, role: str, value: Any) -> BlackboardEntry:
        entry = BlackboardEntry(project=project, key=key, role=role, value=value)
        with self._conn() as conn:
            conn.execute("""INSERT INTO blackboard (project, key, role, value, at)
                VALUES (?,?,?,?,?)""",
                (project, key, role, json.dumps(value, default=str), entry.at))
        return entry

    def read(self, project: str, key: str) -> List[BlackboardEntry]:
        """Every entry ever posted under this key — all of them, not just the
        latest, so disagreement between roles is visible rather than
        overwritten."""
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM blackboard WHERE project=? AND key=? ORDER BY id",
                (project, key)).fetchall()
        return [self._row(r) for r in rows]

    def latest(self, project: str, key: str) -> Optional[BlackboardEntry]:
        entries = self.read(project, key)
        return entries[-1] if entries else None

    def by_role(self, project: str, role: str) -> List[BlackboardEntry]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM blackboard WHERE project=? AND role=? ORDER BY id",
                (project, role)).fetchall()
        return [self._row(r) for r in rows]

    def project_state(self, project: str) -> Dict[str, List[Dict[str, Any]]]:
        """Everything known about a project, grouped by key — the full shared
        state, for a role or Agent 0 to inspect before acting."""
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM blackboard WHERE project=? ORDER BY id",
                (project,)).fetchall()
        out: Dict[str, List[Dict[str, Any]]] = {}
        for row in rows:
            out.setdefault(row["key"], []).append(self._row(row).as_dict())
        return out

    def _row(self, row) -> BlackboardEntry:
        return BlackboardEntry(project=row["project"], key=row["key"],
                               role=row["role"], value=json.loads(row["value"]),
                               at=row["at"])
