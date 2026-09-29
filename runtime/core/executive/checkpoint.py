"""Transition checkpoints: durable from-loop state across handoffs (PLOOP-8).

When a loop handoff is produced through
:mod:`swarm_engine.core.executive.handoff`, the producing loop's
resumable state is checkpointed to disk at produce time. When the
handoff is accepted, the checkpoint is re-read on a fresh connection,
verified against the live state (ids match, the underlying real
records have not drifted, the row has not been tampered with or
replayed), and only then does the transition proceed. After the
receiving loop is entered, the checkpoint is marked consumed --
a checkpoint is single-use; replay is refused loudly.

What this module does NOT do:
  - It does not change what a handoff means (handoff.py owns the
    contract). The checkpoint is durability + continuity verification
    around the contract, never a second contract.
  - It does not invent state. Every field of ``from_state`` is read
    from the producing machinery's real records (the run controller's
    persisted ``rc_cycles`` row, the dispatch result's real fields).
    Where the machinery is not in context, the extraction is marked
    "shallow" honestly instead of guessing.
  - It never silently cold-starts: a missing, tampered, drifted, or
    already-consumed checkpoint at accept time is a loud refusal,
    never a quiet fallback to starting cold.

Fail-loud rules (load-bearing):
  - ``save`` on a duplicate handoff_id raises: one checkpoint per
    handoff, never silently overwritten.
  - ``mark_consumed`` on an already-consumed checkpoint raises:
    checkpoints are single-use; replay is refused.
  - Verification failures raise :class:`CheckpointError` naming the
    exact mismatch. handoff.py translates these to HandoffRefused at
    the contract boundary (this module never imports handoff.py --
    the dependency runs one way only).
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
import uuid
from typing import Any, Dict, Mapping, Optional

#: Contract version stamp, recorded on every checkpoint row.
CHECKPOINT_CONTRACT_VERSION = "transition-checkpoint/v1"

#: Row lifecycle: saved -> verified -> consumed. A consumed row is
#: terminal: it is never verified or consumed again.
STATUS_SAVED = "saved"
STATUS_VERIFIED = "verified"
STATUS_CONSUMED = "consumed"

_CHECKPOINT_SCHEMA = """
CREATE TABLE IF NOT EXISTS transition_checkpoints (
    checkpoint_id TEXT PRIMARY KEY,
    handoff_id TEXT UNIQUE NOT NULL,
    from_loop TEXT NOT NULL,
    to_loop TEXT NOT NULL,
    terminal_state TEXT NOT NULL,
    boundary_kind TEXT NOT NULL,
    triggering_boundary_id TEXT NOT NULL,
    chain_depth INTEGER NOT NULL,
    evidence_refs_json TEXT NOT NULL,
    resource_delta_json TEXT NOT NULL,
    from_state_json TEXT NOT NULL,
    integrity_sha256 TEXT NOT NULL,
    produced_at REAL NOT NULL,
    produced_by TEXT NOT NULL,
    status TEXT NOT NULL
);
"""


class CheckpointError(Exception):
    """Checkpoint mechanics failed: storage, integrity, replay, or
    drift. The message names the exact violation."""


def _integrity_sha256(fields: Mapping[str, Any]) -> str:
    """Tamper-evidence over the checkpoint's content fields."""
    canonical = json.dumps(fields, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _json(obj: Any, what: str) -> str:
    try:
        return json.dumps(obj, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError) as exc:
        raise CheckpointError(
            f"checkpoint {what} is not JSON-serializable "
            f"({type(exc).__name__}: {exc}): the checkpoint stores "
            "plain data, never live objects")


class TransitionCheckpointStore:
    """Durable per-handoff checkpoint store (sqlite).

    Every operation opens a fresh connection: a row written by one
    process is readable by another with no shared in-memory state.
    """

    def __init__(self, db_path: str = "transition_checkpoints.db") -> None:
        self.db_path = db_path
        con = sqlite3.connect(self.db_path)
        try:
            con.executescript(_CHECKPOINT_SCHEMA)
            con.commit()
        finally:
            con.close()

    def _conn(self) -> sqlite3.Connection:
        return sqlite3.connect(self.db_path)

    def save(self, handoff: Any, from_state: Mapping[str, Any]) -> str:
        """Persist the checkpoint for a validated handoff. Returns the
        checkpoint_id. Raises CheckpointError on a duplicate handoff_id
        (one checkpoint per handoff, never silently overwritten)."""
        checkpoint_id = f"ckpt_{uuid.uuid4().hex[:12]}"
        evidence_refs = dict(handoff.evidence_refs or {})
        resource_delta = dict(handoff.resource_delta or {})
        content = {
            "handoff_id": handoff.handoff_id,
            "from_loop": handoff.from_loop,
            "to_loop": handoff.to_loop,
            "terminal_state": handoff.terminal_state,
            "boundary_kind": handoff.boundary_kind,
            "triggering_boundary_id": handoff.triggering_boundary_id,
            "chain_depth": handoff.chain_depth,
            "evidence_refs": evidence_refs,
            "resource_delta": resource_delta,
            "from_state": dict(from_state),
        }
        row = {
            "checkpoint_id": checkpoint_id,
            "integrity_sha256": _integrity_sha256(content),
            "evidence_refs_json": _json(evidence_refs, "evidence_refs"),
            "resource_delta_json": _json(resource_delta, "resource_delta"),
            "from_state_json": _json(dict(from_state), "from_state"),
            "produced_at": time.time(),
            "produced_by": CHECKPOINT_CONTRACT_VERSION,
            "status": STATUS_SAVED,
        }
        con = self._conn()
        try:
            con.execute(
                "INSERT INTO transition_checkpoints ("
                "checkpoint_id, handoff_id, from_loop, to_loop, "
                "terminal_state, boundary_kind, triggering_boundary_id, "
                "chain_depth, evidence_refs_json, resource_delta_json, "
                "from_state_json, integrity_sha256, produced_at, "
                "produced_by, status) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (row["checkpoint_id"], content["handoff_id"],
                 content["from_loop"], content["to_loop"],
                 content["terminal_state"], content["boundary_kind"],
                 content["triggering_boundary_id"], content["chain_depth"],
                 row["evidence_refs_json"], row["resource_delta_json"],
                 row["from_state_json"], row["integrity_sha256"],
                 row["produced_at"], row["produced_by"], row["status"]))
            con.commit()
        except sqlite3.IntegrityError as exc:
            raise CheckpointError(
                f"checkpoint save refused for handoff "
                f"{content['handoff_id']}: {exc}: one checkpoint per "
                "handoff, never silently overwritten")
        finally:
            con.close()
        return checkpoint_id

    def load(self, handoff_id: str) -> Optional[Dict[str, Any]]:
        """Read a checkpoint by handoff_id on a fresh connection.
        Returns None when no checkpoint was saved for the handoff."""
        con = self._conn()
        try:
            cur = con.execute(
                "SELECT checkpoint_id, handoff_id, from_loop, to_loop, "
                "terminal_state, boundary_kind, triggering_boundary_id, "
                "chain_depth, evidence_refs_json, resource_delta_json, "
                "from_state_json, integrity_sha256, produced_at, "
                "produced_by, status FROM transition_checkpoints "
                "WHERE handoff_id=?", (handoff_id,))
            rec = cur.fetchone()
        finally:
            con.close()
        return self._row_to_dict(rec)

    def load_by_checkpoint_id(
            self, checkpoint_id: str) -> Optional[Dict[str, Any]]:
        con = self._conn()
        try:
            cur = con.execute(
                "SELECT checkpoint_id, handoff_id, from_loop, to_loop, "
                "terminal_state, boundary_kind, triggering_boundary_id, "
                "chain_depth, evidence_refs_json, resource_delta_json, "
                "from_state_json, integrity_sha256, produced_at, "
                "produced_by, status FROM transition_checkpoints "
                "WHERE checkpoint_id=?", (checkpoint_id,))
            rec = cur.fetchone()
        finally:
            con.close()
        return self._row_to_dict(rec)

    @staticmethod
    def _row_to_dict(rec: Any) -> Optional[Dict[str, Any]]:
        if rec is None:
            return None
        (checkpoint_id, handoff_id, from_loop, to_loop, terminal_state,
         boundary_kind, triggering_boundary_id, chain_depth,
         evidence_refs_json, resource_delta_json, from_state_json,
         integrity_sha256, produced_at, produced_by,
         status) = rec
        return {
            "checkpoint_id": checkpoint_id,
            "handoff_id": handoff_id,
            "from_loop": from_loop,
            "to_loop": to_loop,
            "terminal_state": terminal_state,
            "boundary_kind": boundary_kind,
            "triggering_boundary_id": triggering_boundary_id,
            "chain_depth": chain_depth,
            "evidence_refs": json.loads(evidence_refs_json),
            "resource_delta": json.loads(resource_delta_json),
            "from_state": json.loads(from_state_json),
            "integrity_sha256": integrity_sha256,
            "produced_at": produced_at,
            "produced_by": produced_by,
            "status": status,
        }

    def _set_status(self, checkpoint_id: str, new_status: str,
                    allowed_from: tuple) -> Dict[str, Any]:
        row = self.load_by_checkpoint_id(checkpoint_id)
        if row is None:
            raise CheckpointError(
                f"checkpoint {checkpoint_id}: no such checkpoint: "
                "status transitions on thin air are refused")
        if row["status"] not in allowed_from:
            raise CheckpointError(
                f"checkpoint {checkpoint_id}: status is "
                f"{row['status']!r}: cannot move to {new_status!r}: "
                "checkpoints move saved -> verified -> consumed, once")
        if row["status"] == new_status:
            return row  # idempotent: verified -> verified is fine
        con = self._conn()
        try:
            con.execute(
                "UPDATE transition_checkpoints SET status=? "
                "WHERE checkpoint_id=?", (new_status, checkpoint_id))
            con.commit()
        finally:
            con.close()
        row["status"] = new_status
        return row

    def mark_verified(self, checkpoint_id: str) -> Dict[str, Any]:
        """saved|verified -> verified. Consumed rows refuse: no replay."""
        return self._set_status(checkpoint_id, STATUS_VERIFIED,
                               (STATUS_SAVED, STATUS_VERIFIED))

    def mark_consumed(self, checkpoint_id: str) -> Dict[str, Any]:
        """saved|verified -> consumed. An already-consumed checkpoint
        raises: checkpoints are single-use, replay is refused loudly."""
        return self._set_status(checkpoint_id, STATUS_CONSUMED,
                               (STATUS_SAVED, STATUS_VERIFIED))


# ---------------------------------------------------------------------------
# From-loop state extraction: real records, never invented
# ---------------------------------------------------------------------------

def _sha256_canonical(obj: Any) -> str:
    return hashlib.sha256(
        json.dumps(obj, sort_keys=True, separators=(",", ":"),
                   default=str).encode("utf-8")).hexdigest()


def _extract_run(outcome: Any, context: Mapping[str, Any]) -> Dict[str, Any]:
    """The run loop's resumable cursor: the accepted per-cycle record.

    Deep extraction reads the run controller's persisted rc_cycles row
    on a fresh connection (the PLOOP-3 acceptance record for the
    cycle). Without the controller in context the extraction is marked
    "shallow" honestly -- the tick summary's digest only, never
    guessed controller state.
    """
    summary = outcome.result if isinstance(outcome.result, dict) else {}
    controller = context.get("run_controller")
    if controller is None:
        return {
            "extraction": "shallow",
            "summary_sha256": _sha256_canonical(summary),
            "gap_ids": [g.get("gap_id") for g in (summary.get("gaps") or [])
                        if isinstance(g, dict) and g.get("gap_id")],
            "errors": len(summary.get("errors") or []),
        }
    checkpoint_path = getattr(controller, "checkpoint_path", "")
    if not checkpoint_path:
        raise CheckpointError(
            "run from-state extraction: context run_controller has no "
            "checkpoint_path: the checkpoint names real persisted rows, "
            "never assumed paths")
    con = sqlite3.connect(checkpoint_path)
    try:
        row = con.execute(
            "SELECT n, at, summary_json FROM rc_cycles "
            "ORDER BY n DESC LIMIT 1").fetchone()
        cycles = con.execute(
            "SELECT COUNT(*) FROM rc_cycles").fetchone()
        try:
            processed = con.execute(
                "SELECT COUNT(*) FROM rc_processed_gaps").fetchone()
        except sqlite3.Error:
            processed = (0,)
    finally:
        con.close()
    if row is None:
        raise CheckpointError(
            "run from-state extraction: rc_cycles has no rows: the tick "
            "that produced this handoff left no persisted cycle record")
    n, at, summary_json = row
    return {
        "extraction": "deep",
        "run_id": getattr(controller, "_run_id", ""),
        "checkpoint_db": checkpoint_path,
        "cycle_n": int(n),
        "cycle_at": float(at),
        "summary_sha256": hashlib.sha256(
            summary_json.encode("utf-8")).hexdigest(),
        "cycles_completed": int(cycles[0]) if cycles else 0,
        "processed_gaps": int(processed[0]) if processed else 0,
    }


def _extract_acquisition(outcome: Any,
                         _context: Mapping[str, Any]) -> Dict[str, Any]:
    result = outcome.result
    return {
        "extraction": "deep",
        "gap_id": str(getattr(result, "gap_id", "") or ""),
        "outcome": str(getattr(result, "outcome", "") or ""),
        "routed": bool(getattr(result, "routed", False)),
        "route_name": str(getattr(result, "route_name", "") or ""),
    }


def _extract_distillation(outcome: Any,
                          _context: Mapping[str, Any]) -> Dict[str, Any]:
    result = outcome.result
    return {
        "extraction": "deep",
        "promoted_name": str(getattr(result, "promoted_name", "") or ""),
        "capability_id": str(getattr(result, "capability_id", "") or ""),
        "success": bool(getattr(result, "success", False)),
    }


def _extract_execution(outcome: Any,
                       _context: Mapping[str, Any]) -> Dict[str, Any]:
    result = outcome.result
    return {
        "extraction": "deep",
        "capability_id": str(getattr(result, "capability_id", "") or ""),
        "verdict": str(getattr(result, "verdict", "") or ""),
        "quarantined": bool(getattr(result, "quarantined", False)),
    }


def _extract_acceptance(outcome: Any,
                        _context: Mapping[str, Any]) -> Dict[str, Any]:
    result = outcome.result
    state = getattr(result, "state", "")
    return {
        "extraction": "deep",
        "state": str(getattr(state, "name", state) or ""),
    }


_EXTRACTORS = {
    "run": _extract_run,
    "acquisition": _extract_acquisition,
    "distillation": _extract_distillation,
    "generalization": _extract_distillation,
    "execution": _extract_execution,
    "acceptance": _extract_acceptance,
}


def extract_from_state(from_loop: str, outcome: Any,
                       context: Mapping[str, Any]) -> Dict[str, Any]:
    """Build the from-loop's resumable-state snapshot from its real
    records. Raises CheckpointError for an unknown loop (never
    guessed) or when the real state cannot be read."""
    extractor = _EXTRACTORS.get(from_loop)
    if extractor is None:
        raise CheckpointError(
            f"from-state extraction: unknown loop {from_loop!r}: the "
            "checkpoint does not guess loop state")
    state = extractor(outcome, context or {})
    state["from_loop"] = from_loop
    state["extracted_at"] = time.time()
    state["produced_by"] = CHECKPOINT_CONTRACT_VERSION
    # Fail loud on non-serializable content: the store holds plain
    # data, never live objects.
    _json(state, "from_state")
    return state


# ---------------------------------------------------------------------------
# Verification: the checkpoint against the live world
# ---------------------------------------------------------------------------

def verify_checkpoint_integrity(row: Mapping[str, Any]) -> None:
    """Recompute the tamper-evidence hash. Raises CheckpointError
    naming the corruption on mismatch."""
    content = {
        "handoff_id": row["handoff_id"],
        "from_loop": row["from_loop"],
        "to_loop": row["to_loop"],
        "terminal_state": row["terminal_state"],
        "boundary_kind": row["boundary_kind"],
        "triggering_boundary_id": row["triggering_boundary_id"],
        "chain_depth": row["chain_depth"],
        "evidence_refs": row["evidence_refs"],
        "resource_delta": row["resource_delta"],
        "from_state": row["from_state"],
    }
    if _integrity_sha256(content) != row.get("integrity_sha256"):
        raise CheckpointError(
            f"checkpoint {row.get('checkpoint_id')}: integrity hash "
            "mismatch: the row was tampered with after save: refusing "
            "to resume from a corrupted checkpoint")


def verify_against_live(row: Mapping[str, Any],
                        context: Mapping[str, Any]) -> None:
    """Verify the checkpoint still describes the live world.

    Raises CheckpointError naming the exact drift:
      - the row is already consumed (replay);
      - the gap the checkpoint names is gone or no longer open;
      - the run cycle row the checkpoint captured was rewritten.
    A drifted checkpoint is never resumed silently.
    """
    if row["status"] == STATUS_CONSUMED:
        raise CheckpointError(
            f"checkpoint {row['checkpoint_id']} for handoff "
            f"{row['handoff_id']} is already consumed: checkpoints are "
            "single-use; replay is refused")
    from_state = row.get("from_state") or {}
    refs = row.get("evidence_refs") or {}

    # The acquisition_gap route: the gap must still be the same open
    # gap. Re-fetched through the real registry, never trusted from
    # the row alone.
    gap_id = refs.get("gap_record")
    if gap_id and gap_id != "?":
        fetcher = (context or {}).get("gap_fetcher")
        if fetcher is None:
            raise CheckpointError(
                f"checkpoint {row['checkpoint_id']}: names gap "
                f"{gap_id} but context has no gap_fetcher: the live "
                "gap cannot be re-verified, so the checkpoint cannot "
                "be resumed")
        record = fetcher(gap_id)
        if record is None:
            raise CheckpointError(
                f"checkpoint {row['checkpoint_id']}: gap {gap_id} no "
                "longer exists in the registry: the world moved on; "
                "refusing to resume a stale checkpoint")
        if getattr(record, "status", None) not in ("open", "acquiring"):
            raise CheckpointError(
                f"checkpoint {row['checkpoint_id']}: gap {gap_id} now "
                f"has status {getattr(record, 'status', None)!r}: it is "
                "no longer an open boundary; refusing to resume a "
                "stale checkpoint")

    # Deep run extraction: the persisted cycle row must be byte-identical
    # to what was checkpointed. History rewritten -> refuse.
    if from_state.get("extraction") == "deep" and \
            from_state.get("from_loop") == "run":
        controller = (context or {}).get("run_controller")
        if controller is not None:
            checkpoint_path = getattr(controller, "checkpoint_path", "")
            if checkpoint_path != from_state.get("checkpoint_db"):
                raise CheckpointError(
                    f"checkpoint {row['checkpoint_id']}: context "
                    "run_controller points at a different checkpoint "
                    "database than the checkpoint captured: refusing")
            con = sqlite3.connect(checkpoint_path)
            try:
                live = con.execute(
                    "SELECT summary_json FROM rc_cycles WHERE n=?",
                    (from_state["cycle_n"],)).fetchone()
            finally:
                con.close()
            if live is None:
                raise CheckpointError(
                    f"checkpoint {row['checkpoint_id']}: rc_cycles row "
                    f"n={from_state['cycle_n']} is gone: the cycle "
                    "history was rewritten; refusing")
            live_sha = hashlib.sha256(
                live[0].encode("utf-8")).hexdigest()
            if live_sha != from_state["summary_sha256"]:
                raise CheckpointError(
                    f"checkpoint {row['checkpoint_id']}: rc_cycles row "
                    f"n={from_state['cycle_n']} no longer matches the "
                    "checkpointed summary: history rewritten; refusing")
