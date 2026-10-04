"""Append-only triage event ledger (charter C-3.3).

First-pass triage is assigned once per finding; any re-triage is a NEW
event in this ledger, never a rewrite. Re-triage REQUIRES new evidence
(a new evidence ref the finding's history does not already cite) --
attempting to upgrade a triage without new evidence is refused with
TriageRefused, named and loud.

`get_triage(finding_id)` returns the latest event (latest-wins for
readers); the ledger itself only ever appends. The finding record in the
fenced Evidence Store keeps its first-pass triage -- history lives here.
"""

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

from .rules import TRIAGE_FLAGS, TriageRefused

TABLE = "triage_events"
SCHEMA = f"""CREATE TABLE IF NOT EXISTS {TABLE} (
    finding_id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    triage TEXT NOT NULL,
    rule_id TEXT NOT NULL,
    cause TEXT NOT NULL,
    evidence_ref TEXT NOT NULL,
    ts REAL NOT NULL,
    PRIMARY KEY (finding_id, seq))"""


@dataclass(frozen=True)
class TriageEvent:
    finding_id: str
    seq: int
    triage: str
    rule_id: str
    cause: str
    evidence_ref: str
    ts: float


class TriageLedger:
    """Append-only ledger of triage assignments. One sqlite file per
    deployment; the table is created on first use."""

    def __init__(self, db_path: str):
        self.db_path = db_path
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as conn:
            conn.execute(SCHEMA)

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=30.0)
        conn.row_factory = sqlite3.Row
        return conn

    def _events(self, finding_id: str) -> List[TriageEvent]:
        with self._conn() as conn:
            rows = conn.execute(
                f"SELECT * FROM {TABLE} WHERE finding_id=? ORDER BY seq",
                (finding_id,)).fetchall()
        return [TriageEvent(finding_id=r["finding_id"], seq=r["seq"],
                            triage=r["triage"], rule_id=r["rule_id"],
                            cause=r["cause"], evidence_ref=r["evidence_ref"],
                            ts=r["ts"]) for r in rows]

    def record_triage(self, *, finding_id: str, triage: str, rule_id: str,
                      cause: str, evidence_ref: str) -> TriageEvent:
        """Append a triage event. First record for a finding must carry
        cause 'first-pass'. Re-triage must cite NEW evidence (an
        evidence_ref not already in this finding's history) and a cause
        -- otherwise TriageRefused. There is no update API; history only
        grows."""
        if triage not in TRIAGE_FLAGS:
            raise TriageRefused(
                f"refusing to record triage {triage!r}: not in the "
                f"enumerated flag set {TRIAGE_FLAGS}")
        if not evidence_ref:
            raise TriageRefused(
                "refusing to record triage with empty evidence_ref: "
                "every triage event must cite evidence")
        history = self._events(finding_id)
        if history:
            cited = {e.evidence_ref for e in history}
            if evidence_ref in cited:
                raise TriageRefused(
                    f"refusing re-triage of {finding_id!r}: evidence_ref "
                    f"{evidence_ref!r} is already cited -- a triage "
                    f"upgrade without NEW evidence is not permitted")
            seq = history[-1].seq + 1
        else:
            if cause != "first-pass":
                raise TriageRefused(
                    f"refusing first triage of {finding_id!r} with cause "
                    f"{cause!r}: the first event must carry cause "
                    f"'first-pass'")
            seq = 0
        event = TriageEvent(finding_id=finding_id, seq=seq, triage=triage,
                            rule_id=rule_id, cause=cause,
                            evidence_ref=evidence_ref, ts=time.time())
        with self._conn() as conn:
            conn.execute(
                f"INSERT INTO {TABLE} (finding_id, seq, triage, rule_id, "
                f"cause, evidence_ref, ts) VALUES (?,?,?,?,?,?,?)",
                (event.finding_id, event.seq, event.triage, event.rule_id,
                 event.cause, event.evidence_ref, event.ts))
        return event

    def get_triage(self, finding_id: str) -> Optional[TriageEvent]:
        """Latest triage event for a finding (latest-wins for readers),
        or None if never triaged."""
        history = self._events(finding_id)
        return history[-1] if history else None

    def history(self, finding_id: str) -> List[TriageEvent]:
        """Full append-only history, oldest first."""
        return self._events(finding_id)
