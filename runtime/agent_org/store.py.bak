"""Hash-chained sqlite sidecar for the agent organization.

Every table created here is append-only and hash-chained:
    row_digest = sha256(canonical_json(fields) + prev_digest)
with prev_digest = "GENESIS" for the first row. audit() recomputes every
link; a single tampered field breaks the chain and is reported, never
silently accepted.

Current-state tables (agents, assignments, work products) are append-only
too: each state change inserts a new row; readers take the latest seq.
History is therefore never rewritten.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import sqlite3
import time
from typing import Any, Dict, List, Optional, Tuple

GENESIS = "GENESIS"


def canonical(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)


def digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime())


# table -> ordered field names (everything except seq/prev_digest/row_digest)
SCHEMAS: Dict[str, List[str]] = {
    "ao_agents": ["agent_id", "template_id", "template_version", "substrate_id",
                  "substrate_kind", "producer_id", "state", "workspace_path",
                  "created_at"],
    "ao_agent_events": ["agent_id", "from_state", "to_state", "actor", "reason",
                        "substrate_old", "substrate_new", "created_at"],
    "ao_templates": ["template_id", "version", "family", "substrate_kind",
                     "contracts_json", "created_at"],
    "ao_template_events": ["template_id", "version", "actor", "action",
                           "created_at"],
    "ao_assignments": ["assignment_id", "agent_id", "objective_json",
                       "constraints_json", "authority_scope_json",
                       "expected_outputs_json", "validation_requirements_json",
                       "originating_decision", "state", "created_at"],
    "ao_assignment_events": ["assignment_id", "from_state", "to_state",
                             "actor", "reason", "created_at"],
    "ao_assignment_grants": ["assignment_id", "grant_id", "effect", "pattern",
                              "created_at"],
    "ao_work_products": ["wp_id", "assignment_id", "agent_id", "template_id",
                         "template_version", "substrate_id", "entrypoint",
                         "input_digest", "outputs_json", "artifacts_json",
                         "evidence_refs_json", "environment_json",
                         "self_reported_success_json", "state", "created_at"],
    "ao_work_product_events": ["wp_id", "from_state", "to_state", "actor",
                               "reason", "created_at"],
    "ao_manager_decisions": ["decision_id", "wp_id", "verdict", "producer",
                             "kind", "reasons_json", "created_at"],
    "ao_review_verdicts": ["wp_id", "admitted", "reasons_json", "bindings",
                           # --- verdict-binding fields (mission 2026-09-25) ---
                           # code_digest binds the verdict to the exact artifact
                           # version that was verified: a verdict never
                           # authorizes a different byte string.
                           "code_digest", "artifact_kind", "artifact_ref",
                           # verifier identifies the verification procedure
                           # that produced this row (never a caller claim).
                           "verifier",
                           # execution_id uniquely identifies this verification
                           # run; spec_digest binds the verification standard.
                           "execution_id", "spec_digest",
                           "created_at"],
    "ao_experience_candidates": ["candidate_id", "wp_id", "agent_id",
                                 "problem_class", "technique_name",
                                 "description", "code", "entrypoint",
                                 "io_contract_json", "tags_json",
                                 "params_json", "evidence_refs_json",
                                 "created_at"],
    "ao_experiences": ["exp_id", "level", "technique_name", "code",
                       "entrypoint", "problem_class", "tags_json",
                       "io_contract_json", "params_json",
                       "derived_from_json", "validation_evidence_json",
                       "code_digest", "created_at",
                       # --- provenance fields (mission 2026-09-25) ---
                       # discovered_by: agent_id that discovered the technique,
                       # or "remor:engine" for REMOR-synthesized artifacts.
                       # origin: "agent_discovery" | "remor_synthesis".
                       "discovered_by", "origin",
                       # verdict_execution_id: the persisted authoritative
                       # verification verdict this experience's trust rests
                       # on (ao_review_verdicts.execution_id). Trust is
                       # derived from that stored row, never from caller
                       # claims.
                       "verdict_execution_id"],
    "ao_performance": ["agent_id", "template_id", "problem_class", "outcome",
                       "verified", "resource_usage_json", "created_at"],
    # --- dispatch evidence (Track 3, 2026-09-25) ---
    # Canonical record of one real tool dispatch, attributable to the exact
    # agent + assignment that performed it. The evidence_json column carries
    # the canonical evidence document whose digest the independent
    # dispatch-evidence verdict binds to. Like every table here this one is
    # hash-chained and covered by the external anchor (collect_anchor_heads
    # enumerates SCHEMAS). Raw intent_dispatches rows are NOT trusted and
    # never authorize knowledge: only a stored admitted
    # artifact_kind="dispatch_evidence" verdict does.
    "ao_dispatch_evidence": ["evidence_id", "dispatch_id", "agent_id",
                            "assignment_id", "request_text", "route_via",
                            "route_score", "capability_id",
                            "capability_version", "plan_fingerprint",
                            "args_json", "input_digest", "result_json",
                            "result_digest", "ok", "error", "evidence_json",
                            "created_at"],
}


class OrgStore:
    """Hash-chained sidecar database."""

    def __init__(self, db_path: str):
        self.db_path = db_path
        self._conn = sqlite3.connect(db_path)
        self._conn.row_factory = sqlite3.Row
        self._init_tables()

    # -- schema ---------------------------------------------------------
    def _init_tables(self) -> None:
        cur = self._conn.cursor()
        for table, fields in SCHEMAS.items():
            cols = ", ".join(f"{f} TEXT" for f in fields)
            cur.execute(
                f"CREATE TABLE IF NOT EXISTS {table} ("
                f"seq INTEGER PRIMARY KEY AUTOINCREMENT, "
                f"prev_digest TEXT NOT NULL, row_digest TEXT NOT NULL, "
                f"{cols})")
        self._conn.commit()

    # -- writes ---------------------------------------------------------
    def _head_digest(self, table: str) -> str:
        cur = self._conn.execute(
            f"SELECT row_digest FROM {table} ORDER BY seq DESC LIMIT 1")
        row = cur.fetchone()
        return row["row_digest"] if row else GENESIS

    def head_digest(self, table: str) -> str:
        """Full-table state digest for the external trust anchor.

        Unlike _head_digest (the chain tip used when appending rows), this
        folds EVERY stored row -- seq, prev_digest, row_digest and all
        field values, in seq order -- into one digest. Any mutation of any
        row, with or without chain recomputation, changes the anchored
        head; a tip-only digest would miss non-tip mutations. Empty table
        -> "GENESIS". Internal chaining semantics are untouched.
        """
        if table not in SCHEMAS:
            raise KeyError(f"unknown table {table!r}")
        names = SCHEMAS[table]
        cols = ", ".join(["seq", "prev_digest", "row_digest"] + names)
        cur = self._conn.execute(
            f"SELECT {cols} FROM {table} ORDER BY seq ASC")
        n = 0
        h = hashlib.sha256()
        for row in cur.fetchall():
            n += 1
            h.update(canonical({
                "seq": row[0],
                "prev_digest": row[1],
                "row_digest": row[2],
                **{k: row[3 + i] for i, k in enumerate(names)},
            }).encode("utf-8"))
            h.update(b"\x00")
        return h.hexdigest() if n else GENESIS

    def insert(self, table: str, fields: Dict[str, Any]) -> int:
        """Append one chained row. Returns the seq."""
        if table not in SCHEMAS:
            raise KeyError(f"unknown table {table!r}")
        names = SCHEMAS[table]
        unknown = set(fields) - set(names)
        if unknown:
            raise ValueError(f"unknown fields for {table}: {sorted(unknown)}")
        ordered = {k: fields.get(k) for k in names}
        prev = self._head_digest(table)
        row_d = digest(canonical(ordered) + prev)
        cols = ", ".join(names)
        placeholders = ", ".join("?" for _ in names)
        cur = self._conn.execute(
            f"INSERT INTO {table} (prev_digest, row_digest, {cols}) "
            f"VALUES (?, ?, {placeholders})",
            [prev, row_d] + [ordered[k] for k in names])
        self._conn.commit()
        return cur.lastrowid

    # -- reads ----------------------------------------------------------
    def latest(self, table: str, col: str, val: Any) -> Optional[Dict[str, Any]]:
        cur = self._conn.execute(
            f"SELECT * FROM {table} WHERE {col}=? ORDER BY seq DESC LIMIT 1",
            (val,))
        row = cur.fetchone()
        return dict(row) if row else None

    def rows(self, table: str, col: Optional[str] = None,
             val: Any = None) -> List[Dict[str, Any]]:
        if col is None:
            cur = self._conn.execute(f"SELECT * FROM {table} ORDER BY seq ASC")
        else:
            cur = self._conn.execute(
                f"SELECT * FROM {table} WHERE {col}=? ORDER BY seq ASC",
                (val,))
        return [dict(r) for r in cur.fetchall()]

    def history(self, table: str, col: str,
                val: Any) -> List[Dict[str, Any]]:
        return self.rows(table, col, val)

    # -- audit ----------------------------------------------------------
    def audit(self, table: str) -> Tuple[bool, str]:
        if table not in SCHEMAS:
            raise KeyError(f"unknown table {table!r}")
        names = SCHEMAS[table]
        cols = ", ".join(["seq", "prev_digest", "row_digest"] + names)
        cur = self._conn.execute(
            f"SELECT {cols} FROM {table} ORDER BY seq ASC")
        rows = cur.fetchall()
        prev = GENESIS
        for row in rows:
            seq = row[0]
            if not hmac.compare_digest(str(row[1]), prev):
                return False, (f"{table}: chain break at seq {seq} "
                               f"(prev_digest mismatch)")
            ordered = {k: row[3 + i] for i, k in enumerate(names)}
            expect = digest(canonical(ordered) + prev)
            if not hmac.compare_digest(expect, str(row[2])):
                return False, (f"{table}: tamper at seq {seq} "
                               f"(row_digest mismatch)")
            prev = str(row[2])
        return True, f"{table}: {len(rows)} rows, chain intact"

    def audit_all(self) -> Dict[str, Tuple[bool, str]]:
        return {t: self.audit(t) for t in SCHEMAS}

    def close(self) -> None:
        self._conn.close()
