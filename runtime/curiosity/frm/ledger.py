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

Record-level integrity (CUR-HARDEN-1, James 2026-10-04):
  * every row carries `integrity_sha256` — SHA-256 over the canonical JSON
    of the row's stored fields, computed at append time. TAMPER-EVIDENCE
    against corruption (the threat P6F proved: single-byte flips went
    silent here), not authenticity against a filesystem adversary (out of
    scope per the P5C bound). Same pattern as the checkpoint store and the
    hardened evidence store.
  * `rounds_for_epoch()` / `closes_for_epoch()` / `all_records()` verify
    the hash on every read, in the read path: a mismatch raises
    LedgerIntegrityError — the corrupted entry is REFUSED and can never
    become trusted FRM state. `count()` returns no records and verifies
    nothing (documented).
  * legacy rows (appended before hardening): accepted while the ledger is
    unsealed (explicit legacy trust); the first append seals the ledger
    (backfills hashes, sets integrity_version=1), after which a hash-less
    row is REJECTED as tampered — the downgrade is closed.
  * crash recovery (SQLite journal replay) is structural; per-record
    validation happens on the first read after open — the read path IS the
    recovery validation. A corrupted entry is never resurrected as
    trusted: reads raise before the payload reaches the evaluator.
  * `quarantine_seq()` moves a failed entry to a separate quarantine file;
    it refuses callers from inside the curiosity domain.
"""

from __future__ import annotations

import hashlib
import inspect
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
    payload_json TEXT NOT NULL,
    integrity_sha256 TEXT
);
CREATE INDEX IF NOT EXISTS idx_frm_epochs_epoch
    ON frm_epochs (epoch_id, seq);
"""

INTEGRITY_VERSION = 1
# Seal marker: SQLite PRAGMA user_version (file-header integer, not a
# table — no table-inventory side effects).
_USER_VERSION_PRAGMA = "PRAGMA user_version"


class LedgerIntegrityError(Exception):
    """A ledger entry failed its integrity check: the stored bytes do not
    match the append-time hash, or a sealed ledger holds a hash-less entry
    (downgrade). The entry is REFUSED — never returned as trusted state."""


def _record_sha256(kind: str, epoch_id: int, recorded_at: float,
                   payload_json: str) -> str:
    """Tamper-evidence over an entry's stored fields (byte-exact: the hash
    covers the stored payload JSON string, not a re-serialization)."""
    canonical = json.dumps(
        {"kind": kind, "epoch_id": int(epoch_id),
         "recorded_at": recorded_at, "payload_json": payload_json},
        sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _external_caller() -> str:
    for frame_info in inspect.stack():
        mod = inspect.getmodule(frame_info.frame)
        name = mod.__name__ if mod is not None else "<unknown>"
        if name != __name__:
            return name
    return "<unknown>"


class EpochLedger:
    """Append-only record of FRM contention rounds and epoch closes."""

    def __init__(self, path: Optional[str] = None) -> None:
        # path=None => in-memory SQLite. Same append-only semantics;
        # a deployment passes an explicit file path.
        self._path = path or ":memory:"
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(self._path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.executescript(_SCHEMA)
            cols = [r[1] for r in self._conn.execute(
                "PRAGMA table_info(frm_epochs)").fetchall()]
            if "integrity_sha256" not in cols:
                # Pre-hardening database: existing rows stay hash-less
                # (legacy) until the first append seals the ledger.
                self._conn.execute(
                    "ALTER TABLE frm_epochs "
                    "ADD COLUMN integrity_sha256 TEXT")
            self._conn.commit()

    # -- integrity internals ------------------------------------------------

    def _is_sealed(self) -> bool:
        row = self._conn.execute(_USER_VERSION_PRAGMA).fetchone()
        return row is not None and int(row[0]) >= INTEGRITY_VERSION

    def _seal_if_unsealed(self) -> None:
        """Seal on first append: backfill hashes, set version. Idempotent."""
        if self._is_sealed():
            return
        for r in self._conn.execute(
                "SELECT seq, kind, epoch_id, recorded_at, payload_json "
                "FROM frm_epochs WHERE integrity_sha256 IS NULL").fetchall():
            h = _record_sha256(r["kind"], r["epoch_id"],
                               r["recorded_at"], r["payload_json"])
            self._conn.execute(
                "UPDATE frm_epochs SET integrity_sha256=? WHERE seq=?",
                (h, r["seq"]))
        self._conn.execute(f"{_USER_VERSION_PRAGMA} = {INTEGRITY_VERSION}")

    def seal_legacy(self) -> int:
        """Explicit migration: hash every pre-integrity entry, seal."""
        with self._lock:
            before = self._conn.execute(
                "SELECT COUNT(*) FROM frm_epochs "
                "WHERE integrity_sha256 IS NULL").fetchone()[0]
            self._seal_if_unsealed()
            self._conn.commit()
            return int(before)

    def _verify_row(self, row: sqlite3.Row) -> Dict[str, Any]:
        stored = row["integrity_sha256"]
        if stored is None:
            if self._is_sealed():
                raise LedgerIntegrityError(
                    f"frm_epochs seq={row['seq']}: sealed ledger holds a "
                    f"hash-less entry: treated as tampered (downgrade "
                    f"refused): the entry is NOT returned")
            # Legacy trust (explicit): pre-integrity entry in an unsealed
            # ledger. Accepted, but NOT integrity-verified.
            return json.loads(row["payload_json"])
        expected = _record_sha256(row["kind"], row["epoch_id"],
                                 row["recorded_at"], row["payload_json"])
        if stored != expected:
            raise LedgerIntegrityError(
                f"frm_epochs seq={row['seq']}: integrity mismatch: the "
                f"stored bytes do not match the append-time hash: REFUSED, "
                f"never returned as trusted state")
        return json.loads(row["payload_json"])

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
            self._seal_if_unsealed()
            ts = recorded_at if recorded_at is not None else time.time()
            payload_json = json.dumps(payload, sort_keys=True)
            cur = self._conn.execute(
                "INSERT INTO frm_epochs (kind, epoch_id, recorded_at, "
                "payload_json, integrity_sha256) VALUES (?, ?, ?, ?, ?)",
                (kind, int(epoch_id), ts, payload_json,
                 _record_sha256(kind, int(epoch_id), ts, payload_json)))
            self._conn.commit()
            return int(cur.lastrowid)

    # -- read (every returned entry is integrity-verified in the read path)

    def rounds_for_epoch(self, epoch_id: int) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT seq, kind, epoch_id, recorded_at, payload_json, "
                "integrity_sha256 FROM frm_epochs "
                "WHERE kind = 'round' AND epoch_id = ? ORDER BY seq",
                (int(epoch_id),)).fetchall()
            return [self._verify_row(r) for r in rows]

    def closes_for_epoch(self, epoch_id: int) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT seq, kind, epoch_id, recorded_at, payload_json, "
                "integrity_sha256 FROM frm_epochs "
                "WHERE kind = 'epoch_close' AND epoch_id = ? ORDER BY seq",
                (int(epoch_id),)).fetchall()
            return [self._verify_row(r) for r in rows]

    def all_records(self) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT seq, kind, epoch_id, recorded_at, payload_json, "
                "integrity_sha256 FROM frm_epochs ORDER BY seq").fetchall()
            return [{"kind": r["kind"], "epoch_id": r["epoch_id"],
                     "recorded_at": r["recorded_at"],
                     "payload": self._verify_row(r)} for r in rows]

    def count(self) -> int:
        # Returns no records: nothing to verify. A corrupt entry is caught
        # when it is READ, never when it is counted.
        with self._lock:
            return int(self._conn.execute(
                "SELECT COUNT(*) FROM frm_epochs").fetchone()[0])

    # -- quarantine (governance-plane disposition) --------------------------

    def quarantine_seq(self, seq: int, reason: str) -> str:
        """Move a failed entry to a SEPARATE quarantine file
        (`<path>.quarantine`, table `frm_epochs_quarantine`) and delete it
        from the ledger. Refuses callers from inside the curiosity domain.
        Returns the quarantine file path."""
        caller = _external_caller()
        if (caller == "runtime.curiosity"
                or caller.startswith("runtime.curiosity.")
                or caller == "swarm_engine.curiosity"
                or caller.startswith("swarm_engine.curiosity.")):
            raise LedgerIntegrityError(
                f"quarantine refused: caller {caller!r} is inside the "
                f"curiosity domain: quarantine is a governance-plane action")
        with self._lock:
            row = self._conn.execute(
                "SELECT seq, kind, epoch_id, recorded_at, payload_json, "
                "integrity_sha256 FROM frm_epochs WHERE seq=?",
                (int(seq),)).fetchone()
            if row is None:
                raise ValueError(f"quarantine refused: no such seq {seq!r}")
            qpath = self._path + ".quarantine"
            qconn = sqlite3.connect(qpath)
            try:
                qconn.execute("""CREATE TABLE IF NOT EXISTS
                    frm_epochs_quarantine (
                    seq INTEGER PRIMARY KEY, kind TEXT NOT NULL,
                    epoch_id INTEGER NOT NULL, recorded_at REAL NOT NULL,
                    payload_json TEXT NOT NULL, integrity_sha256 TEXT,
                    quarantined_at REAL NOT NULL, reason TEXT NOT NULL)""")
                qconn.execute(
                    """INSERT OR REPLACE INTO frm_epochs_quarantine
                    (seq, kind, epoch_id, recorded_at, payload_json,
                     integrity_sha256, quarantined_at, reason)
                    VALUES (?,?,?,?,?,?,?,?)""",
                    (row["seq"], row["kind"], row["epoch_id"],
                     row["recorded_at"], row["payload_json"],
                     row["integrity_sha256"], time.time(), reason))
                qconn.commit()
            finally:
                qconn.close()
            self._conn.execute("DELETE FROM frm_epochs WHERE seq=?",
                               (int(seq),))
            self._conn.commit()
            return qpath

    # -- lifecycle ----------------------------------------------------------

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    @property
    def path(self) -> str:
        return self._path
