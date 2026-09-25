"""
swarm_engine/memory/failure_memory.py

Failure as a first-class, persisted input rather than something that
disappears after the task returns a False.

Every failure is classified (reusing FailureDiagnoser so the taxonomy is one
thing, not two), recorded with what was still valid at the point of failure,
and made queryable by goal or by kind. The orchestrator and gap reasoner can
then ask "has this been tried before, and how did it go?" before spending a
budget on an attempt whose outcome is already known.
"""
from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class FailureRecord:
    goal: str
    kind: str
    detail: str
    recoverable: bool
    valid_prior_state: Dict[str, Any] = field(default_factory=dict)
    at: float = field(default_factory=time.time)
    # 2026-09-14: the search-policy context the failure was recorded under
    # (e.g. "search-policy-v2"). A failure is evidence about the machinery
    # that produced it; after a search-policy repair, prior failures are
    # stale and must not keep blocking a strategy retry. Records without a
    # context (legacy rows, non-strategy failures) never match a current
    # context, so they are ignored by context-scoped skip rules — fail-open.
    context: Optional[str] = None

    def as_dict(self) -> Dict[str, Any]:
        return {"goal": self.goal, "kind": self.kind, "detail": self.detail[:300],
                "recoverable": self.recoverable,
                "valid_prior_state": self.valid_prior_state, "at": self.at,
                "context": self.context}


class FailureMemory:
    """Persisted, classified failure history."""

    def __init__(self, diagnoser, db_path: str = "swarm_engine.db"):
        self.diagnoser = diagnoser
        self.db_path = db_path
        with self._conn() as conn:
            conn.execute("""CREATE TABLE IF NOT EXISTS failure_memory (
                id INTEGER PRIMARY KEY AUTOINCREMENT, goal TEXT, kind TEXT,
                detail TEXT, recoverable INTEGER, valid_prior_state TEXT, at REAL)""")
            existing_cols = {row[1] for row in
                             conn.execute("PRAGMA table_info(failure_memory)")}
            if "context" not in existing_cols:
                conn.execute("ALTER TABLE failure_memory ADD COLUMN context TEXT")

    def _conn(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def record(self, goal: str, error: Any,
              valid_prior_state: Optional[Dict[str, Any]] = None,
              context: Optional[str] = None) -> FailureRecord:
        diagnosis = self.diagnoser.diagnose(error)
        record = FailureRecord(goal=goal, kind=diagnosis.kind.value,
                               detail=str(error), recoverable=diagnosis.retryable,
                               valid_prior_state=valid_prior_state or {},
                               context=context)
        with self._conn() as conn:
            conn.execute("""INSERT INTO failure_memory
                (goal, kind, detail, recoverable, valid_prior_state, at, context)
                VALUES (?,?,?,?,?,?,?)""",
                (record.goal, record.kind, record.detail, int(record.recoverable),
                 json.dumps(record.valid_prior_state, default=str), record.at,
                 record.context))
        return record

    def history_for(self, goal: str, limit: int = 20) -> List[FailureRecord]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM failure_memory WHERE goal=? ORDER BY id DESC LIMIT ?",
                (goal, limit)).fetchall()
        return [self._row(r) for r in rows]

    def by_kind(self, kind: str, limit: int = 50) -> List[FailureRecord]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM failure_memory WHERE kind=? ORDER BY id DESC LIMIT ?",
                (kind, limit)).fetchall()
        return [self._row(r) for r in rows]

    def repeated_failure_count(self, goal: str) -> int:
        return len(self.history_for(goal, limit=1000))

    def dominant_kind(self, min_samples: int = 3) -> Optional[Dict[str, Any]]:
        """The most common failure kind across everything recorded, if there
        is enough history to say anything about it."""
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT kind, COUNT(*) c FROM failure_memory GROUP BY kind "
                "ORDER BY c DESC LIMIT 1").fetchall()
        if not rows or rows[0]["c"] < min_samples:
            return None
        return {"kind": rows[0]["kind"], "count": rows[0]["c"]}

    def _row(self, row) -> FailureRecord:
        cols = set(row.keys()) if hasattr(row, "keys") else set()
        return FailureRecord(goal=row["goal"], kind=row["kind"], detail=row["detail"],
                             recoverable=bool(row["recoverable"]),
                             valid_prior_state=json.loads(row["valid_prior_state"] or "{}"),
                             at=row["at"],
                             context=row["context"] if "context" in cols else None)
