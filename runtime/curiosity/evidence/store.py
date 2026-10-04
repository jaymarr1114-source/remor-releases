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

Record-level integrity (CUR-HARDEN-1, James 2026-10-04):
  * every row carries `integrity_sha256` — SHA-256 over the canonical JSON
    of the row's stored content fields, computed at write time. This is
    TAMPER-EVIDENCE against corruption (bit rot, torn writes, single-byte
    flips), the threat P6F proved real — not authenticity against a
    filesystem adversary (a same-process/filesystem attacker is outside
    the threat model per the P5C bound: it could recompute any hash).
    The checkpoint store (runtime/core/executive/checkpoint.py) uses the
    same SHA-256 tamper-evidence pattern.
  * `get()` / `all()` verify the hash on every read, in the read path:
    a mismatch raises EvidenceIntegrityError — the corrupted record is
    REFUSED and never returned as trusted state.
  * legacy rows (written before hardening, no hash): accepted while the
    store is unsealed, with the legacy trust explicitly labeled; the first
    write seals the store (backfills hashes, sets integrity_version=1),
    after which a hash-less row is REJECTED as tampered — the downgrade
    (strip the hash to bypass verification) is closed.
  * `quarantine_corrupt()` moves a failed record to a SEPARATE quarantine
    file (the fenced table inventory is untouched); it refuses callers
    from inside the curiosity domain — curiosity cannot hide its own
    evidence.
"""
from __future__ import annotations

import hashlib
import inspect
import json
import sqlite3
import time
from typing import List, Optional

from .records import CuriosityFinding, DomainFenceError

DEFAULT_DB_PATH = "curiosity_evidence.db"
TABLE = "curiosity_evidence"
INTEGRITY_VERSION = 1
# Seal marker: SQLite PRAGMA user_version (file-header integer, not a
# table — the fenced table inventory checked by tables()/P1A is untouched).
_USER_VERSION_PRAGMA = "PRAGMA user_version"


class EvidenceIntegrityError(Exception):
    """A stored evidence record failed its integrity check: the bytes on
    disk do not match the hash recorded at write time, or a sealed store
    holds a hash-less row (downgrade). The record is REFUSED — it is never
    returned as trusted state."""


def _record_sha256(evidence_id: str, data_json: str, terminal_state: str,
                   origin: str, created_at: float) -> str:
    """Tamper-evidence over a record's stored content fields (byte-exact:
    the hash covers the stored JSON string, not a re-serialization)."""
    canonical = json.dumps(
        {"evidence_id": evidence_id, "data": data_json,
         "terminal_state": terminal_state, "origin": origin,
         "created_at": created_at},
        sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _external_caller() -> str:
    """First caller frame outside this module (for the quarantine fence)."""
    for frame_info in inspect.stack():
        mod = inspect.getmodule(frame_info.frame)
        name = mod.__name__ if mod is not None else "<unknown>"
        if name != __name__:
            return name
    return "<unknown>"


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
            self._ensure_schema(conn)

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=30.0)
        conn.row_factory = sqlite3.Row
        return conn

    # -- integrity schema ---------------------------------------------------

    def _ensure_schema(self, conn: sqlite3.Connection) -> None:
        conn.execute(f"""CREATE TABLE IF NOT EXISTS {TABLE} (
            evidence_id TEXT PRIMARY KEY, data TEXT NOT NULL,
            terminal_state TEXT NOT NULL, origin TEXT NOT NULL,
            created_at REAL NOT NULL, integrity_sha256 TEXT)""")
        cols = [r[1] for r in
                conn.execute(f"PRAGMA table_info({TABLE})").fetchall()]
        if "integrity_sha256" not in cols:
            # Pre-hardening database: add the hash column; existing rows
            # stay hash-less (legacy) until the first write seals the store.
            conn.execute(
                f"ALTER TABLE {TABLE} ADD COLUMN integrity_sha256 TEXT")

    def _is_sealed(self, conn: sqlite3.Connection) -> bool:
        row = conn.execute(_USER_VERSION_PRAGMA).fetchone()
        return row is not None and int(row[0]) >= INTEGRITY_VERSION

    def seal_legacy(self) -> int:
        """Explicit migration: hash every pre-integrity row and mark the
        store sealed. Returns the number of rows backfilled. After sealing,
        a hash-less row is rejected as tampered (downgrade closed)."""
        with self._conn() as conn:
            self._ensure_schema(conn)
            n = self._backfill_hashes(conn)
            conn.execute(f"{_USER_VERSION_PRAGMA} = {INTEGRITY_VERSION}")
            return n

    def _backfill_hashes(self, conn: sqlite3.Connection) -> int:
        n = 0
        for r in conn.execute(
                f"SELECT evidence_id, data, terminal_state, origin, "
                f"created_at FROM {TABLE} "
                f"WHERE integrity_sha256 IS NULL").fetchall():
            h = _record_sha256(r[0], r[1], r[2], r[3], r[4])
            conn.execute(
                f"UPDATE {TABLE} SET integrity_sha256=? "
                f"WHERE evidence_id=?", (h, r[0]))
            n += 1
        return n

    def _verify_row(self, conn: sqlite3.Connection,
                    row: sqlite3.Row) -> CuriosityFinding:
        stored = row["integrity_sha256"]
        if stored is None:
            if self._is_sealed(conn):
                raise EvidenceIntegrityError(
                    f"evidence {row['evidence_id']!r}: sealed store holds a "
                    f"hash-less row: treated as tampered (downgrade "
                    f"refused): the record is NOT returned")
            # Legacy trust (explicit): pre-integrity row in an unsealed
            # store. Accepted, but NOT integrity-verified — the store seals
            # on the first write, after which this path is closed.
            return CuriosityFinding.from_dict(json.loads(row["data"]))
        expected = _record_sha256(row["evidence_id"], row["data"],
                                 row["terminal_state"], row["origin"],
                                 row["created_at"])
        if stored != expected:
            raise EvidenceIntegrityError(
                f"evidence {row['evidence_id']!r}: integrity mismatch: the "
                f"stored bytes do not match the write-time hash: REFUSED, "
                f"never returned as trusted state")
        return CuriosityFinding.from_dict(json.loads(row["data"]))

    # -- storage internals (used by the writer only) ------------------------

    def _insert(self, finding: CuriosityFinding) -> CuriosityFinding:
        """Persist a validated record. Called only by CuriosityWriter after
        validation AND the domain-fence check. Duplicate evidence_id is a
        loud error (the PLOOP-12 duplicate-present lesson). The first write
        seals the store (legacy migration); every row is hashed."""
        data_json = json.dumps(finding.as_dict())
        with self._conn() as conn:
            self._ensure_schema(conn)
            self.seal_legacy_if_unsealed(conn)
            cur = conn.execute(
                f"SELECT 1 FROM {TABLE} WHERE evidence_id=?",
                (finding.evidence_id,))
            if cur.fetchone() is not None:
                raise ValueError(
                    f"duplicate evidence_id {finding.evidence_id!r}: "
                    "refused loudly, never silently re-recorded")
            conn.execute(
                f"INSERT INTO {TABLE} (evidence_id, data, terminal_state, "
                f"origin, created_at, integrity_sha256) "
                f"VALUES (?,?,?,?,?,?)",
                (finding.evidence_id, data_json,
                 finding.terminal_state, finding.origin,
                 finding.created_at,
                 _record_sha256(finding.evidence_id, data_json,
                               finding.terminal_state, finding.origin,
                               finding.created_at)))
        return finding

    def seal_legacy_if_unsealed(self, conn: sqlite3.Connection) -> int:
        """Seal on first write: backfill + version marker. Idempotent."""
        if self._is_sealed(conn):
            return 0
        n = self._backfill_hashes(conn)
        conn.execute(f"{_USER_VERSION_PRAGMA} = {INTEGRITY_VERSION}")
        return n

    # -- read API (open: Primary Acceptance inspects read-only, Phase 4+;
    #    every record is integrity-verified IN THE READ PATH) ---------------

    def get(self, evidence_id: str) -> Optional[CuriosityFinding]:
        with self._conn() as conn:
            row = conn.execute(
                f"SELECT evidence_id, data, terminal_state, origin, "
                f"created_at, integrity_sha256 FROM {TABLE} "
                f"WHERE evidence_id=?",
                (evidence_id,)).fetchone()
            if row is None:
                return None
            return self._verify_row(conn, row)

    def all(self) -> List[CuriosityFinding]:
        with self._conn() as conn:
            rows = conn.execute(
                f"SELECT evidence_id, data, terminal_state, origin, "
                f"created_at, integrity_sha256 FROM {TABLE} "
                f"ORDER BY created_at").fetchall()
            return [self._verify_row(conn, r) for r in rows]

    def quarantine_corrupt(self, evidence_id: str, reason: str) -> str:
        """Governance-plane disposition for a record that failed integrity:
        move the row to a SEPARATE quarantine file
        (`<db_path>.quarantine`, table `curiosity_evidence_quarantine`) and
        delete it from the fenced store — the corrupt bytes leave
        authoritative state without being silently dropped (the quarantine
        file preserves them for forensics). Refuses callers from inside the
        curiosity domain: curiosity cannot hide its own evidence. Returns
        the quarantine file path."""
        caller = _external_caller()
        if (caller == "runtime.curiosity"
                or caller.startswith("runtime.curiosity.")
                or caller == "swarm_engine.curiosity"
                or caller.startswith("swarm_engine.curiosity.")):
            raise DomainFenceError(
                f"quarantine refused: caller {caller!r} is inside the "
                f"curiosity domain: quarantine is a governance-plane action")
        qpath = self.db_path + ".quarantine"
        with self._conn() as conn:
            row = conn.execute(
                f"SELECT evidence_id, data, terminal_state, origin, "
                f"created_at, integrity_sha256 FROM {TABLE} "
                f"WHERE evidence_id=?", (evidence_id,)).fetchone()
            if row is None:
                raise ValueError(
                    f"quarantine refused: no such evidence {evidence_id!r}")
            qconn = sqlite3.connect(qpath, timeout=30.0)
            try:
                qconn.execute("""CREATE TABLE IF NOT EXISTS
                    curiosity_evidence_quarantine (
                    evidence_id TEXT PRIMARY KEY, data TEXT NOT NULL,
                    terminal_state TEXT NOT NULL, origin TEXT NOT NULL,
                    created_at REAL NOT NULL, integrity_sha256 TEXT,
                    quarantined_at REAL NOT NULL, reason TEXT NOT NULL)""")
                qconn.execute(
                    """INSERT OR REPLACE INTO curiosity_evidence_quarantine
                    (evidence_id, data, terminal_state, origin, created_at,
                     integrity_sha256, quarantined_at, reason)
                    VALUES (?,?,?,?,?,?,?,?)""",
                    (row["evidence_id"], row["data"], row["terminal_state"],
                     row["origin"], row["created_at"],
                     row["integrity_sha256"], time.time(), reason))
                qconn.commit()
            finally:
                qconn.close()
            conn.execute(f"DELETE FROM {TABLE} WHERE evidence_id=?",
                         (evidence_id,))
        return qpath

    def tables(self) -> List[str]:
        """Inventory of tables in this database file — the fence check:
        exactly one curiosity table, no Primary tables, nothing else."""
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "ORDER BY name").fetchall()
        return [r["name"] for r in rows]
