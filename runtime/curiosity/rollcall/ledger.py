"""Append-only attestation ledger for the GAM roll-call channel.

Phase-0 GAM mechanism design, Section 3. Properties:

* Owned solely by GAM; GAM writes, enforcement reads. There is no
  update path and no delete path for records younger than the
  retention floor -- "rewrite a MISSED as MET" is not an operation
  this writer possesses, so fabricated compliance cannot be produced
  through it.
* Every row is hash-chained (HMAC-SHA256 over the canonical fields
  plus the previous row's digest, key held by the writer in a separate
  key file). audit() recomputes the whole chain: a record rewritten
  out-of-band (direct sqlite UPDATE) breaks the chain and is reported
  with the exact attestation_id -- never silently accepted.
* Retention: records persist at least six months beyond the
  attestation (D-3 L3: the ban period plus re-entry evaluation).
  purge() refuses any cutoff younger than the floor by construction;
  only records older than the floor may be removed.
* Separate store and separate writer from the evidence, acceptance,
  and kill ledgers: its own sqlite file, its own writer class, no
  shared write path.

This module reuses no existing ledger class on purpose: the design
requires a separate store AND a separate writer, and sharing a writer
would collapse the very separation the proof must demonstrate.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import time
from dataclasses import replace
from typing import Any, Callable, Dict, List, Optional, Tuple

from .schemas import AttestationRecord

#: Six months, in seconds. Records younger than this floor are never
#: purged; purge() refuses by construction.
RETENTION_MIN_S = 6 * 30 * 24 * 3600

GENESIS_DIGEST = "GENESIS"

_LEDGER_SCHEMA = """
CREATE TABLE IF NOT EXISTS attestations (
    seq             INTEGER PRIMARY KEY AUTOINCREMENT,
    attestation_id  TEXT NOT NULL UNIQUE,
    challenge_id    TEXT NOT NULL,
    domain_id       TEXT NOT NULL,
    issued_at       REAL NOT NULL,
    responded_at    REAL,
    classification  TEXT NOT NULL,
    validation_detail TEXT NOT NULL,
    recorded_at     REAL NOT NULL,
    prev_digest     TEXT NOT NULL,
    row_digest      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_att_domain
    ON attestations (domain_id, seq);
"""


class RetentionRefused(Exception):
    """purge() was asked to remove records younger than the retention
    floor. Refused by construction."""


class ChainBroken(Exception):
    """audit() found a tampered or reordered row. Carries the
    attestation_id of the first broken link."""


def _canonical(fields: Dict[str, Any]) -> str:
    return json.dumps(fields, sort_keys=True, separators=(",", ":"),
                      default=str)


class AttestationLedger:
    """GAM's sole writer for attestation records.

    db_path: the ledger's own sqlite file (separate store).
    key_path: the writer's HMAC key file (separate from the db file;
        defaults to db_path + ".key"). The key never lives in the db.
    clock: time source, default time.time (injectable for tests that
        need genuinely old records).
    """

    def __init__(self, db_path: str, key_path: Optional[str] = None,
                 clock: Callable[[], float] = time.time) -> None:
        self._db_path = os.path.abspath(db_path)
        self._key_path = os.path.abspath(
            key_path if key_path is not None else db_path + ".key")
        self._clock = clock
        self._key = self._load_or_create_key()
        with sqlite3.connect(self._db_path) as conn:
            conn.executescript(_LEDGER_SCHEMA)

    # -- key handling ---------------------------------------------------

    def _load_or_create_key(self) -> bytes:
        if os.path.exists(self._key_path):
            with open(self._key_path, "rb") as fh:
                    return fh.read()
        key = secrets.token_bytes(32)
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        fd = os.open(self._key_path, flags, 0o600)
        try:
            os.write(fd, key)
        finally:
            os.close(fd)
        return key

    # -- hashing ----------------------------------------------------------

    def _digest(self, fields: Dict[str, Any], prev_digest: str) -> str:
        msg = (_canonical(fields) + "|" + prev_digest).encode("utf-8")
        return hmac.new(self._key, msg, hashlib.sha256).hexdigest()

    @staticmethod
    def _fields(rec: AttestationRecord) -> Dict[str, Any]:
        return {
            "attestation_id": rec.attestation_id,
            "challenge_id": rec.challenge_id,
            "domain_id": rec.domain_id,
            "issued_at": rec.issued_at,
            "responded_at": rec.responded_at,
            "classification": rec.classification,
            "validation_detail": rec.validation_detail,
            "recorded_at": rec.recorded_at,
        }

    def _conn(self) -> sqlite3.Connection:
        return sqlite3.connect(self._db_path)

    def _last_digest(self, conn: sqlite3.Connection) -> str:
        row = conn.execute(
            "SELECT row_digest FROM attestations ORDER BY seq DESC LIMIT 1"
        ).fetchone()
        return row[0] if row else GENESIS_DIGEST

    # -- the ONLY write path: append --------------------------------------

    def record(self, rec: AttestationRecord) -> Dict[str, Any]:
        """Append one attestation. There is deliberately no update(),
        rewrite(), or delete() method on this writer.

        recorded_at is stamped by the writer's own clock when the
        record carries the default 0.0 -- the writer timestamps, per
        the D-5 authority (GAM may timestamp)."""
        if rec.recorded_at == 0.0:
            rec = replace(rec, recorded_at=self._clock())
        fields = self._fields(rec)
        conn = self._conn()
        try:
            prev = self._last_digest(conn)
            digest = self._digest(fields, prev)
            conn.execute(
                "INSERT INTO attestations (attestation_id, challenge_id,"
                " domain_id, issued_at, responded_at, classification,"
                " validation_detail, recorded_at, prev_digest, row_digest)"
                " VALUES (?,?,?,?,?,?,?,?,?,?)",
                (rec.attestation_id, rec.challenge_id, rec.domain_id,
                 rec.issued_at, rec.responded_at, rec.classification,
                 rec.validation_detail, rec.recorded_at, prev, digest))
            conn.commit()
        finally:
            conn.close()
        return dict(rec.to_wire())

    # -- reads (copies; mutating a returned dict cannot touch the store) --

    def attestations(self, domain_id: Optional[str] = None,
                     classification: Optional[str] = None
                     ) -> List[Dict[str, Any]]:
        q = ("SELECT attestation_id, challenge_id, domain_id, issued_at,"
             " responded_at, classification, validation_detail, recorded_at"
             " FROM attestations")
        clauses, params = [], []
        if domain_id is not None:
            clauses.append("domain_id = ?")
            params.append(domain_id)
        if classification is not None:
            clauses.append("classification = ?")
            params.append(classification)
        if clauses:
            q += " WHERE " + " AND ".join(clauses)
        q += " ORDER BY seq"
        conn = self._conn()
        try:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(q, params).fetchall()
        finally:
            conn.close()
        return [dict(r) for r in rows]

    def latest(self, domain_id: str) -> Optional[Dict[str, Any]]:
        rows = self.attestations(domain_id=domain_id)
        return rows[-1] if rows else None

    def count(self) -> int:
        conn = self._conn()
        try:
            row = conn.execute("SELECT COUNT(*) FROM attestations").fetchone()
        finally:
            conn.close()
        return int(row[0])

    # -- tamper audit -------------------------------------------------------

    def audit(self) -> Tuple[bool, Optional[str]]:
        """Recompute the hash chain over every row in seq order.

        Returns (True, None) when the chain is intact, else (False,
        attestation_id-of-first-broken-link). A rewritten record (or a
        reordered/deleted one) breaks its link: detection, not trust."""
        conn = self._conn()
        try:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT attestation_id, challenge_id, domain_id, issued_at,"
                " responded_at, classification, validation_detail,"
                " recorded_at, prev_digest, row_digest FROM attestations"
                " ORDER BY seq").fetchall()
        finally:
            conn.close()
        prev = GENESIS_DIGEST
        for r in rows:
            fields = {
                "attestation_id": r["attestation_id"],
                "challenge_id": r["challenge_id"],
                "domain_id": r["domain_id"],
                "issued_at": r["issued_at"],
                "responded_at": r["responded_at"],
                "classification": r["classification"],
                "validation_detail": r["validation_detail"],
                "recorded_at": r["recorded_at"],
            }
            if r["prev_digest"] != prev:
                return (False, r["attestation_id"])
            if r["row_digest"] != self._digest(fields, prev):
                return (False, r["attestation_id"])
            prev = r["row_digest"]
        return (True, None)

    # -- retention ------------------------------------------------------------

    @property
    def retention_floor_s(self) -> float:
        """Youngest age a record may be purged at: now - 6 months."""
        return self._clock() - RETENTION_MIN_S

    def purge(self, older_than: float) -> int:
        """Remove records with recorded_at < older_than.

        Refuses (RetentionRefused) when older_than is younger than the
        six-month floor: purging young records is not an operation this
        ledger performs. Returns the number of rows removed (0 when no
        record is old enough -- the common case)."""
        if older_than > self.retention_floor_s:
            raise RetentionRefused(
                f"purge refused: cutoff {older_than} is younger than the "
                f"six-month retention floor {self.retention_floor_s}")
        conn = self._conn()
        try:
            cur = conn.execute(
                "DELETE FROM attestations WHERE recorded_at < ?",
                (older_than,))
            conn.commit()
            return cur.rowcount
        finally:
            conn.close()

    # -- identity (for the separate-store proof) ------------------------------

    @property
    def db_path(self) -> str:
        return self._db_path

    @property
    def writer_id(self) -> str:
        return f"AttestationLedger@{id(self):x}"
