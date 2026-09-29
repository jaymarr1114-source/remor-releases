"""The fenced Curiosity Evidence Store (C-7).

Storage-only. This module holds NO writer: writes arrive exclusively
through runtime/curiosity/evidence/writer.py (the curiosity-side writer,
domain-fenced). Reads are open — Primary Acceptance will inspect these
records read-only in Phase 4+ (A23); the store therefore exposes `get`,
`get_by_evidence_id`, and `all` read methods with no domain restriction,
but the DATABASE FILE itself is distinct from every Primary store.

Pattern reuse (no-duplication mandate): the sqlite layout follows the
AcceptanceStore pattern (runtime/services/acceptance.py: one table keyed
by the record id, JSON data column, updated_at timestamp) — separate
database file, separate table, never shared.

By construction:
  * the only table is `curiosity_evidence`; no Primary table exists here
    and no curiosity table exists in any Primary database,
  * this module exports no `submit`/`save`/`insert` path — the write path
    is the writer's alone, and the writer's domain fence (writer.py)
    refuses Primary-side callers at runtime, proved by the adversarial
    battery.
"""
from __future__ import annotations

import json
import sqlite3
import time
from typing import List, Optional

from .records import CuriosityFinding

DEFAULT_DB_PATH = "curiosity_evidence.db"
TABLE = "curiosity_evidence"


class CuriosityEvidenceStore:
    """Storage for curiosity evidence records.

    `db_path` must point at the fenced database file
    (runtime/curiosity/evidence/curiosity_evidence.db per the frozen
    interface). Reads are unrestricted; writes are NOT offered here —
    use CuriosityWriter (writer.py).
    """

    def __init__(self, db_path: str):
        self.db_path = db_path
        with self._conn() as conn:
            conn.execute(f"""CREATE TABLE IF NOT EXISTS {TABLE} (
                evidence_id TEXT PRIMARY KEY, data TEXT NOT NULL,
                terminal_state TEXT NOT NULL, origin TEXT NOT NULL,
                created_at REAL NOT NULL)""")

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=30.0)
        conn.row_factory = sqlite3.Row
        return conn

    # -- storage internals (used by the writer only) ------------------------

    def _insert(self, finding: CuriosityFinding) -> CuriosityFinding:
        """Persist a validated record. Called only by CuriosityWriter after
        validation AND the domain-fence check. Duplicate evidence_id is a
        loud error (the PLOOP-12 duplicate-present lesson)."""
        with self._conn() as conn:
            cur = conn.execute(
                f"SELECT 1 FROM {TABLE} WHERE evidence_id=?",
                (finding.evidence_id,))
            if cur.fetchone() is not None:
                raise ValueError(
                    f"duplicate evidence_id {finding.evidence_id!r}: "
                    "refused loudly, never silently re-recorded")
            conn.execute(
                f"INSERT INTO {TABLE} (evidence_id, data, terminal_state, "
                f"origin, created_at) VALUES (?,?,?,?,?)",
                (finding.evidence_id, json.dumps(finding.as_dict()),
                 finding.terminal_state, finding.origin,
                 finding.created_at))
        return finding

    # -- read API (open: Primary Acceptance inspects read-only, Phase 4+) ----

    def get(self, evidence_id: str) -> Optional[CuriosityFinding]:
        with self._conn() as conn:
            row = conn.execute(
                f"SELECT data FROM {TABLE} WHERE evidence_id=?",
                (evidence_id,)).fetchone()
        return CuriosityFinding.from_dict(json.loads(row["data"])) if row else None

    def all(self) -> List[CuriosityFinding]:
        with self._conn() as conn:
            rows = conn.execute(
                f"SELECT data FROM {TABLE} ORDER BY created_at").fetchall()
        return [CuriosityFinding.from_dict(json.loads(r["data"])) for r in rows]

    def tables(self) -> List[str]:
        """Inventory of tables in this database file — the fence check:
        exactly one curiosity table, no Primary tables, nothing else."""
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "ORDER BY name").fetchall()
        return [r["name"] for r in rows]
