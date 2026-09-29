"""runtime/curiosity/frm/ledger.py

Append-only epoch ledger for the FRM evaluation layer.

Every contention round and every epoch close is appended as a JSON
record. The ledger exposes INSERT and SELECT only: there is no update
or delete API, so a recorded round cannot be rewritten after the fact
(same discipline as the GAM attestation ledger and the kill ledger in
the sibling missions). Retention is by explicit owner action only --
nothing prunes itself.

Storage is SQLite (one file, one table family), separate from the
curiosity evidence DB, the GAM attestation ledger, and the kill ledger:
separate store, separate writer, per the frozen interfaces.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from typing import Any, Dict, List, Optional


_SCHEMA = """
CREATE TABLE IF NOT EXISTS frm_epochs (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,            -- 'round' | 'epoch_close'
    epoch_id INTEGER NOT NULL,
    recorded_at REAL NOT NULL,
    payload_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_frm_epochs_epoch
    ON frm_epochs (epoch_id, seq);
"""


class EpochLedger:
    """Append-only record of FRM contention rounds and epoch closes."""

    def __init__(self, path: Optional[str] = None) -> None:
        # path=None => in-memory SQLite. Same append-only semantics;
        # a deployment passes an explicit file path.
        self._path = path or ":memory:"
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(self._path, check_same_thread=False)
        with self._lock:
            self._conn.executescript(_SCHEMA)
            self._conn.commit()

    # -- append -----------------------------------------------------------

    def append_round(self, epoch_id: int, payload: Dict[str, Any],
                     recorded_at: Optional[float] = None) -> int:
        return self._append("round", epoch_id, payload, recorded_at)

    def append_epoch_close(self, epoch_id: int, payload: Dict[str, Any],
                           recorded_at: Optional[float] = None) -> int:
        return self._append("epoch_close", epoch_id, payload, recorded_at)

    def _append(self, kind: str, epoch_id: int, payload: Dict[str, Any],
                recorded_at: Optional[float]) -> int:
        if not isinstance(payload, dict):
            raise ValueError("ledger payload must be a dict")
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO frm_epochs (kind, epoch_id, recorded_at, "
                "payload_json) VALUES (?, ?, ?, ?)",
                (kind, int(epoch_id),
                 recorded_at if recorded_at is not None else time.time(),
                 json.dumps(payload, sort_keys=True)))
            self._conn.commit()
            return int(cur.lastrowid)

    # -- read --------------------------------------------------------------

    def rounds_for_epoch(self, epoch_id: int) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT payload_json FROM frm_epochs "
                "WHERE kind = 'round' AND epoch_id = ? ORDER BY seq",
                (int(epoch_id),)).fetchall()
        return [json.loads(r[0]) for r in rows]

    def closes_for_epoch(self, epoch_id: int) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT payload_json FROM frm_epochs "
                "WHERE kind = 'epoch_close' AND epoch_id = ? ORDER BY seq",
                (int(epoch_id),)).fetchall()
        return [json.loads(r[0]) for r in rows]

    def all_records(self) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT kind, epoch_id, recorded_at, payload_json "
                "FROM frm_epochs ORDER BY seq").fetchall()
        return [{"kind": k, "epoch_id": e, "recorded_at": t,
                 "payload": json.loads(p)}
                for k, e, t, p in rows]

    def count(self) -> int:
        with self._lock:
            return int(self._conn.execute(
                "SELECT COUNT(*) FROM frm_epochs").fetchone()[0])

    # -- lifecycle ----------------------------------------------------------

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    @property
    def path(self) -> str:
        return self._path
