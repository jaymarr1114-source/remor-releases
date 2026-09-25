"""Persistent diagnostic records for insufficient semantic evidence.

This module deliberately represents an evidence *gap*, not a semantic answer.
It records pre-existing acquisition-target evidence that a goal's operation
semantics are not grounded by examples, a registry identity, or an admitted
semantic capability.  It does not formulate a source query, choose evidence,
create a hypothesis, or evaluate a candidate implementation.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional


GAP_KIND = "semantic_evidence_insufficient"
TARGET_CLASS = "semantic_operation_interpretation"


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def _fingerprint(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class SemanticEvidenceGap:
    """A durable statement that independent semantic evidence is absent.

    The record is diagnostic only.  `evidence_options` mirrors the generic
    options already carried by the acquisition target; it must never be
    interpreted as a selected source, expected answer, or semantic contract.
    """

    gap_id: str
    goal: str
    gap_kind: str
    target_class: str
    required_for: str
    evidence_options: List[str]
    available_evidence: Dict[str, Any]
    reason: str
    target_fingerprint: str
    status: str
    created_at: float
    updated_at: float

    def as_dict(self) -> Dict[str, Any]:
        return {
            "gap_id": self.gap_id,
            "goal": self.goal,
            "gap_kind": self.gap_kind,
            "target_class": self.target_class,
            "required_for": self.required_for,
            "evidence_options": list(self.evidence_options),
            "available_evidence": dict(self.available_evidence),
            "reason": self.reason,
            "target_fingerprint": self.target_fingerprint,
            "status": self.status,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @staticmethod
    def from_dict(data: Dict[str, Any]) -> "SemanticEvidenceGap":
        return SemanticEvidenceGap(
            gap_id=str(data["gap_id"]),
            goal=str(data.get("goal") or ""),
            gap_kind=str(data.get("gap_kind") or GAP_KIND),
            target_class=str(data.get("target_class") or ""),
            required_for=str(data.get("required_for") or ""),
            evidence_options=list(data.get("evidence_options") or []),
            available_evidence=dict(data.get("available_evidence") or {}),
            reason=str(data.get("reason") or ""),
            target_fingerprint=str(data.get("target_fingerprint") or ""),
            status=str(data.get("status") or "open"),
            created_at=float(data.get("created_at") or 0.0),
            updated_at=float(data.get("updated_at") or 0.0),
        )


class SemanticEvidenceGapStore:
    """SQLite ledger for generic, unresolved semantic-evidence diagnostics."""

    def __init__(self, db_path: str):
        self.db_path = db_path
        self._init()

    def _conn(self):
        return sqlite3.connect(self.db_path)

    def _init(self) -> None:
        with self._conn() as conn:
            conn.execute(
                """CREATE TABLE IF NOT EXISTS semantic_evidence_gaps (
                    gap_id TEXT PRIMARY KEY,
                    target_fingerprint TEXT NOT NULL,
                    status TEXT NOT NULL,
                    body_json TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                )"""
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_semantic_evidence_gaps_target "
                "ON semantic_evidence_gaps(target_fingerprint, status)"
            )

    @staticmethod
    def _target_context(goal: str, target: Dict[str, Any]) -> Dict[str, Any]:
        # Keep only generic target fields already produced by the target
        # interpreter.  Nothing is inferred from objective wording here.
        return {
            "goal": str(goal),
            "target_class": str(target.get("class") or ""),
            "required_for": str(target.get("required_for") or ""),
            "required_kind": str(target.get("required_kind") or ""),
            "evidence_required": list(target.get("evidence_required") or []),
            "constructible_by_current_search": bool(
                target.get("constructible_by_current_search", False)
            ),
            "note": str(target.get("note") or ""),
        }

    def record_open(self, goal: str, target: Dict[str, Any]) -> SemanticEvidenceGap:
        """Upsert the only supported ungrounded-semantics diagnostic.

        Rejecting other target classes prevents this ledger from silently
        recategorizing ordinary capability or structural gaps as semantic
        evidence gaps.
        """
        if str(target.get("class") or "") != TARGET_CLASS:
            raise ValueError("semantic evidence gap requires semantic operation target")
        context = self._target_context(goal, target)
        target_fingerprint = _fingerprint(context)
        gap_id = "seg_" + _fingerprint({"kind": GAP_KIND, **context})[:20]
        now = time.time()
        with self._conn() as conn:
            row = conn.execute(
                "SELECT body_json, created_at FROM semantic_evidence_gaps "
                "WHERE gap_id=?", (gap_id,)
            ).fetchone()
            created_at = float(row[1]) if row else now
            record = SemanticEvidenceGap(
                gap_id=gap_id,
                goal=str(goal),
                gap_kind=GAP_KIND,
                target_class=TARGET_CLASS,
                required_for=str(target.get("required_for") or ""),
                evidence_options=list(target.get("evidence_required") or []),
                available_evidence={
                    "behavioral_examples": 0,
                    "registry_grounded_operation_identity": False,
                    "validated_semantic_capability": False,
                },
                reason=str(target.get("note") or ""),
                target_fingerprint=target_fingerprint,
                status="open",
                created_at=created_at,
                updated_at=now,
            )
            conn.execute(
                """INSERT OR REPLACE INTO semantic_evidence_gaps
                   (gap_id, target_fingerprint, status, body_json, created_at, updated_at)
                   VALUES (?,?,?,?,?,?)""",
                (gap_id, target_fingerprint, "open", _canonical(record.as_dict()),
                 created_at, now),
            )
        return record

    def get(self, gap_id: str) -> Optional[SemanticEvidenceGap]:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT body_json FROM semantic_evidence_gaps WHERE gap_id=?", (gap_id,)
            ).fetchone()
        return SemanticEvidenceGap.from_dict(json.loads(row[0])) if row else None

    def find_open(self, goal: str, target: Dict[str, Any]) -> Optional[SemanticEvidenceGap]:
        if str(target.get("class") or "") != TARGET_CLASS:
            return None
        target_fingerprint = _fingerprint(self._target_context(goal, target))
        with self._conn() as conn:
            row = conn.execute(
                "SELECT body_json FROM semantic_evidence_gaps "
                "WHERE target_fingerprint=? AND status='open' "
                "ORDER BY updated_at DESC LIMIT 1", (target_fingerprint,),
            ).fetchone()
        return SemanticEvidenceGap.from_dict(json.loads(row[0])) if row else None

    def list(self, status: Optional[str] = None) -> List[SemanticEvidenceGap]:
        query = "SELECT body_json FROM semantic_evidence_gaps"
        params: List[Any] = []
        if status is not None:
            query += " WHERE status=?"
            params.append(status)
        query += " ORDER BY created_at, gap_id"
        with self._conn() as conn:
            rows = conn.execute(query, params).fetchall()
        return [SemanticEvidenceGap.from_dict(json.loads(row[0])) for row in rows]
