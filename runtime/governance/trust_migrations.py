"""Audited trust-schema migrations for the external trust anchor.

Background
----------
The engine's root decision-class set (ROOT_DECISION_CLASSES in
governance.oracle_binding) grows over time (e.g. the agent:* classes after
v8; agent:remote_dispatch in REMOTE-DISPATCH-1). On an EXISTING database the
OracleRegistry bootstrap backfills the missing root authorizations as
VISIBLE chained GRANT events in ob_producer_authz_events, restoring the
documented invariant that the engine holds every root class. That backfill
moves the oracle:ob_producer_authz_events chain head, so any anchor journal
whose tip predates the backfill fails verification -- RemorOrganization.boot
refuses with AnchorMismatch. The anchor is CORRECT to refuse: from its
perspective the head change is indistinguishable from tampering.

This module provides the designed crossing for that exact case: when -- and
only when -- the SOLE head difference is the documented engine backfill
(grant events for the engine producer, for root decision classes, recorded
with the backfill reason), the engine records an AUDITED, SIGNED journal
transition (reason="migration", authority=the authenticated engine id) and
boot proceeds. The journal visibly shows genesis -> migration, so an honest
schema migration stays distinguishable from malicious rewriting.

Anything else -- a second differing scope, a revoked class, a grant to any
other producer, a grant of a non-root class, a mutated row, a truncated
chain -- returns False and the caller keeps refusing (fail-closed). This
module never weakens the anchor: it only recognizes the delta the engine is
already documented to hold.

Security note: the permitted delta grants the engine producer ONLY classes
from ROOT_DECISION_CLASSES, which the engine holds by design on every fresh
database. An attacker who can write the database gains nothing by forging
rows that match this predicate -- they cannot escalate the engine (it is
already root) nor authorize any other producer.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Dict, List, Optional

_MIGRATION_SCOPE = "oracle:ob_producer_authz_events"
_MIGRATION_TABLE = "ob_producer_authz_events"
_BACKFILL_REASON_PREFIX = "bootstrap backfill:"
_GENESIS_PREFIX = "GENESIS:"


def _fold_rows(rows: List[Dict[str, Any]]) -> str:
    """Replicate OracleRegistry.head_digest's fold over the given rows.

    Each row is a dict of ALL its columns (as sqlite3.Row -> dict). The
    replication is checked against the registry's own head_digest before
    any migration decision is made: if the two ever drift, the migration
    refuses rather than misreads (fail-closed on implementation drift).
    """
    h = hashlib.sha256()
    for r in rows:
        h.update(json.dumps(r, sort_keys=True, separators=(",", ":")).encode("utf-8"))
        h.update(b"\x00")
    return h.hexdigest()


def _backfill_delta_rows(oregistry: Any, anchored_digest: str) -> Optional[List[Dict[str, Any]]]:
    """Find the exact row delta since the anchored head.

    Returns the delta rows (a strict suffix of the live table, in seq
    order) when the anchored digest matches a strict prefix of the live
    rows; None otherwise (no match -> not a pure append -> no migration).
    """
    cur = oregistry._conn.cursor()
    cur.execute(f"SELECT * FROM {_MIGRATION_TABLE} ORDER BY seq ASC")
    rows = [dict(r) for r in cur.fetchall()]

    # Self-check the fold replication against the registry's own digest.
    live_digest = oregistry.head_digest(_MIGRATION_TABLE)
    if rows:
        if _fold_rows(rows) != live_digest:
            return None  # implementation drift: refuse, never misread
    elif live_digest != f"{_GENESIS_PREFIX}{_MIGRATION_TABLE}":
        return None

    if anchored_digest == f"{_GENESIS_PREFIX}{_MIGRATION_TABLE}":
        split = 0
    else:
        split = None
        # The anchored state must be a STRICT prefix of the live rows.
        for k in range(len(rows)):
            if _fold_rows(rows[:k]) == anchored_digest:
                split = k
                break
        if split is None:
            return None
    delta = rows[split:]
    if not delta:
        return None  # heads differ but no new rows: not a backfill; refuse
    return delta


def _is_backfill_row(row: Dict[str, Any]) -> bool:
    """The documented engine backfill, and nothing else."""
    from swarm_engine.governance.oracle_binding import (
        ENGINE_PRODUCER_ID,
        ROOT_DECISION_CLASSES,
    )

    return (
        row.get("producer_id") == ENGINE_PRODUCER_ID
        and row.get("event") == "grant"
        and row.get("actor") == ENGINE_PRODUCER_ID
        and row.get("decision_class") in ROOT_DECISION_CLASSES
        and isinstance(row.get("reason"), str)
        and row["reason"].startswith(_BACKFILL_REASON_PREFIX)
        and isinstance(row.get("event_id"), str)
        and row["event_id"].startswith("azev_grant_")
    )


def migrate_engine_backfill(store: Any, oregistry: Any, anchor: Any) -> bool:
    """Record an audited migration transition for the engine backfill.

    Returns True when a migration transition was recorded (the caller
    should re-verify and proceed). Returns False when the head difference
    is NOT exactly the documented backfill -- the caller must keep
    refusing. Never raises for a non-matching delta; raises only for
    genuine machinery failures (which the caller treats as refusal).
    """
    from swarm_engine.governance.anchor import collect_anchor_heads

    anchored = anchor.latest_heads()
    live = collect_anchor_heads(store, oregistry)
    differing = sorted(
        s for s in set(anchored) | set(live) if anchored.get(s) != live.get(s)
    )
    if differing != [_MIGRATION_SCOPE]:
        return False
    delta = _backfill_delta_rows(oregistry, anchored[_MIGRATION_SCOPE])
    if not delta:
        return False
    if not all(_is_backfill_row(r) for r in delta):
        return False

    # The delta is exactly the documented backfill. Record the audited,
    # signed transition through the real caller-authorization path: the
    # anchor is bound to the registry and the engine caller (which holds
    # agent:anchor_write as a root class) is authenticated. The recorded
    # authority is the authenticated engine id, and the journal visibly
    # shows the migration.
    from swarm_engine.governance.oracle_binding import ENGINE_PRODUCER_ID

    anchor.bind_registry(oregistry)
    caller = oregistry.engine_handle()
    anchor.transition(
        live, reason="migration", authority=ENGINE_PRODUCER_ID, caller=caller
    )
    return True
