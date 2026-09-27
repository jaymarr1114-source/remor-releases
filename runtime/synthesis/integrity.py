"""Integrity for capabilities and epistemic artifacts: corruption
detection, dependency integrity, rehydration validation, lifecycle
reconciliation, audited rollback, and tamper-evident lineage.

The package already had the *pieces* but not the *bindings*:

- ``plan_fingerprint`` existed as a capability id but was never
  recomputed on load (a tampered ``plan_json`` loaded silently);
- capabilities pinned primitives by *name* only, so a silently
  mutated primitive (same name, new signature/effects) passed
  ``rehydrate()`` as healthy;
- three parallel status systems (store ``status``, provenance
  ``trust``, lifecycle ``state``) could disagree about whether a
  capability may run;
- ``rollback()`` touched three tables but left provenance trust,
  lifecycle state, and prerequisite bindings pointing at the wrong
  revision;
- epistemic objects (hypotheses, experiments, evidence) had no
  integrity binding at all.

This module binds them, without changing any existing table schema:

- primitive *fingerprints* (contract hash: name/family/signature/
  effects) pinned at seal time; re-verified on every check;
- an ``IntegrityStore`` (new tables in the same DB) holding seals and
  lineage links;
- ``verify_capability``: one call that recomputes the plan
  fingerprint, checks the record seal, re-validates the plan against
  the current registry, compares pinned vs live primitive
  fingerprints, and reports the three status systems plus the single
  effective status;
- ``effective_status``: the one answer to "may this run?" -- the most
  restrictive of the three systems;
- ``quarantine_everywhere`` / ``rollback_with_lineage``: state changes
  applied consistently across all three systems, with reasons logged;
- lineage: goal -> question -> hypotheses -> experiments -> evidence
  -> capability, sealed per object, so any tampering breaks the chain.
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from swarm_engine.governance.lifecycle import CapabilityLifecycle, LifecycleState
from swarm_engine.governance.provenance import (
    Origin, ProvenanceRecord, ProvenanceStore, TrustLevel)
from swarm_engine.synthesis.capability_store import (
    CapabilityRecord, CapabilityStore, plan_fingerprint)


def _ensure_provenance(engine, capability_id: str) -> None:
    """A capability without a ProvenanceRecord cannot have its trust
    set (set_trust is UPDATE-only). Create the minimal record so the
    trust system always has something to say."""
    try:
        if engine.provenance.get(capability_id) is None:
            rec = engine.capabilities.get(capability_id)
            engine.provenance.record(ProvenanceRecord(
                capability_id=capability_id, origin=Origin.SYNTHESIZED,
                trust=TrustLevel.UNKNOWN, source="integrity",
                primitives_used=list(rec.ops) if rec else [],
                effects=list(rec.effects) if rec else []))
    except Exception:
        pass


# --------------------------------------------------------------------------
# primitive fingerprints: the dependency contract, hashed
# --------------------------------------------------------------------------

def fingerprint_primitive(prim) -> str:
    """Contract hash of a primitive: name, family, input/output type
    specs, effects, and flags. Two registrations with the same contract
    hash are interchangeable for every stored plan; any signature or
    effect change alters the hash, which is exactly what dependency
    integrity must detect. (Deliberately not a code hash: behavior
    inside the fn body is the primitive author's responsibility and is
    covered by PrimitiveValidator, not by pinning.)"""
    contract = {
        "name": prim.name,
        "family": prim.family,
        "inputs": {k: str(v) for k, v in prim.inputs.items()},
        "output": str(prim.output),
        "effects": sorted(str(e) for e in prim.effects),
        "variadic": bool(prim.variadic),
        "pure": bool(prim.pure),
        "needs_ctx": bool(prim.needs_ctx),
    }
    blob = json.dumps(contract, sort_keys=True, separators=(",", ":"))
    return "prim_" + hashlib.sha256(blob.encode()).hexdigest()[:20]


def _walk_ops(plan: Dict[str, Any]) -> List[str]:
    """Every op name referenced anywhere in a plan, including nested
    control-flow step lists."""
    ops: List[str] = []

    def walk(steps) -> None:
        for s in steps or []:
            if not isinstance(s, dict):
                continue
            if s.get("op"):
                ops.append(s["op"])
            for key in ("then", "else", "body", "catch"):
                sub = s.get(key)
                if isinstance(sub, list):
                    walk(sub)
            branches = s.get("branches")
            if isinstance(branches, dict):
                for bsteps in branches.values():
                    if isinstance(bsteps, list):
                        walk(bsteps)

    if isinstance(plan, dict):
        walk(plan.get("steps"))
    return ops


def snapshot_plan_deps(plan: Dict[str, Any], registry) -> Dict[str, str]:
    """Pin the fingerprint of every primitive a plan depends on.
    Missing primitives pin as ``"MISSING"`` -- loudly, not silently."""
    pins: Dict[str, str] = {}
    for op in _walk_ops(plan):
        prim = registry.get(op)
        pins[op] = fingerprint_primitive(prim) if prim else "MISSING"
    return pins


def seal_dict(d: Dict[str, Any]) -> str:
    """Tamper-evident seal over a canonical JSON encoding."""
    blob = json.dumps(d, sort_keys=True, separators=(",", ":"),
                      default=str)
    return "seal_" + hashlib.sha256(blob.encode()).hexdigest()[:24]


# --------------------------------------------------------------------------
# IntegrityStore: seals + lineage, same DB, new tables
# --------------------------------------------------------------------------

class IntegrityStore:
    def __init__(self, db_path: str = "swarm_engine.db"):
        self.db_path = db_path
        self._init_schema()

    def _conn(self):
        c = sqlite3.connect(self.db_path)
        c.execute("PRAGMA journal_mode=WAL")
        return c

    def _init_schema(self) -> None:
        with self._conn() as c:
            c.execute("""
                CREATE TABLE IF NOT EXISTS integrity_seals (
                    object_kind  TEXT NOT NULL,
                    object_id    TEXT NOT NULL,
                    seal         TEXT NOT NULL,
                    dep_pins_json TEXT NOT NULL DEFAULT '{}',
                    created_at   REAL NOT NULL,
                    PRIMARY KEY (object_kind, object_id)
                )
            """)
            c.execute("""
                CREATE TABLE IF NOT EXISTS integrity_lineage (
                    id           INTEGER PRIMARY KEY AUTOINCREMENT,
                    parent_kind  TEXT NOT NULL,
                    parent_id    TEXT NOT NULL,
                    child_kind   TEXT NOT NULL,
                    child_id     TEXT NOT NULL,
                    kind         TEXT NOT NULL DEFAULT 'derivation',
                    at           REAL NOT NULL
                )
            """)
            c.execute("CREATE INDEX IF NOT EXISTS idx_lin_child "
                      "ON integrity_lineage(child_kind, child_id)")
            c.execute("CREATE INDEX IF NOT EXISTS idx_lin_parent "
                      "ON integrity_lineage(parent_kind, parent_id)")

    # -- seals -----------------------------------------------------------
    def seal_object(self, kind: str, object_id: str,
                    data: Dict[str, Any],
                    dep_pins: Optional[Dict[str, str]] = None) -> str:
        seal = seal_dict(data)
        with self._conn() as c:
            c.execute("INSERT OR REPLACE INTO integrity_seals "
                      "(object_kind, object_id, seal, dep_pins_json, created_at)"
                      " VALUES (?,?,?,?,?)",
                      (kind, object_id, seal,
                       json.dumps(dep_pins or {}), time.time()))
        return seal

    def get_seal(self, kind: str,
                 object_id: str) -> Optional[Dict[str, Any]]:
        with self._conn() as c:
            row = c.execute("SELECT seal, dep_pins_json, created_at"
                            " FROM integrity_seals WHERE object_kind=? "
                            "AND object_id=?", (kind, object_id)).fetchone()
        if not row:
            return None
        return {"seal": row[0], "dep_pins": json.loads(row[1]),
                "created_at": row[2]}

    def verify_seal(self, kind: str, object_id: str,
                    data: Dict[str, Any]) -> Tuple[bool, str]:
        rec = self.get_seal(kind, object_id)
        if rec is None:
            return False, "not_sealed"
        ok = seal_dict(data) == rec["seal"]
        return ok, "match" if ok else "MISMATCH"

    # -- lineage ---------------------------------------------------------
    def link(self, parent_kind: str, parent_id: str,
             child_kind: str, child_id: str,
             kind: str = "derivation") -> None:
        with self._conn() as c:
            c.execute("INSERT INTO integrity_lineage "
                      "(parent_kind, parent_id, child_kind, child_id, kind, at)"
                      " VALUES (?,?,?,?,?,?)",
                      (parent_kind, parent_id, child_kind, child_id,
                       kind, time.time()))

    def lineage_of(self, kind: str,
                   object_id: str) -> Dict[str, List[Dict[str, str]]]:
        """Ancestors (walk up) and descendants (walk down) of an object."""
        ancestors: List[Dict[str, str]] = []
        seen = {(kind, object_id)}
        frontier = [(kind, object_id)]
        with self._conn() as c:
            while frontier:
                ck, cid = frontier.pop()
                rows = c.execute(
                    "SELECT parent_kind, parent_id, kind FROM integrity_lineage"
                    " WHERE child_kind=? AND child_id=?", (ck, cid)).fetchall()
                for pk, pid, k in rows:
                    if (pk, pid) not in seen:
                        seen.add((pk, pid))
                        ancestors.append({"kind": pk, "id": pid,
                                          "link": k})
                        frontier.append((pk, pid))
            descendants: List[Dict[str, str]] = []
            seen_d = {(kind, object_id)}
            frontier = [(kind, object_id)]
            while frontier:
                pk, pid = frontier.pop()
                rows = c.execute(
                    "SELECT child_kind, child_id, kind FROM integrity_lineage"
                    " WHERE parent_kind=? AND parent_id=?",
                    (pk, pid)).fetchall()
                for ck, cid, k in rows:
                    if (ck, cid) not in seen_d:
                        seen_d.add((ck, cid))
                        descendants.append({"kind": ck, "id": cid,
                                            "link": k})
                        frontier.append((ck, cid))
        return {"ancestors": ancestors, "descendants": descendants}


# --------------------------------------------------------------------------
# lifecycle reconciliation: one effective status
# --------------------------------------------------------------------------

def effective_status(engine, capability_id: str) -> Dict[str, Any]:
    """The single truth for 'may this run?': the most restrictive of
    the three status systems. Any quarantine anywhere quarantines
    everywhere; a superseded record is never runnable even if its
    trust looks fine."""
    store_status = None
    rec = engine.capabilities.get(capability_id)
    if rec is not None:
        store_status = rec.status
    trust = None
    try:
        prec = engine.provenance.get(capability_id)
        trust = prec.trust.value if prec else None
    except Exception:
        trust = None
    lifecycle = None
    try:
        lifecycle = engine.lifecycle.state_of(capability_id).value
    except Exception:
        lifecycle = None

    quarantined = (
        store_status == "quarantined"
        or trust == TrustLevel.QUARANTINED.value
        or lifecycle == LifecycleState.QUARANTINED.value
    )
    if quarantined:
        effective = "quarantined"
    elif store_status == "superseded":
        effective = "superseded"
    elif lifecycle in (LifecycleState.DEPRECATED.value,
                       LifecycleState.ROLLED_BACK.value):
        effective = lifecycle
    elif store_status == "active":
        effective = "active"
    else:
        effective = store_status or "unknown"
    return {"effective": effective,
            "store_status": store_status,
            "trust": trust,
            "lifecycle": lifecycle,
            "consistent": (
                (store_status == "quarantined")
                == (trust == TrustLevel.QUARANTINED.value)
            )}


def quarantine_everywhere(engine, capability_id: str,
                          reason: str, *,
                          caller: Any = None) -> Dict[str, Any]:
    """Quarantine across all three status systems at once, with the
    reason logged in each. This is the only supported way to
    quarantine: it cannot leave the systems disagreeing.

    caller: REQUIRED -- the caller must hold 'agent:quarantine'.
    Unauthenticated or unauthorized callers are refused (default-deny).

    2026-09-19: also unregisters the capability's acquired primitive(s)
    -- previously the statuses flipped but the primitive stayed callable,
    so a "quarantined" capability remained executable -- and propagates
    the quarantine through the persisted plan-dependency graph
    (dependents become `dependency_quarantined`, a derived state eligible
    for automatic recovery, not an epistemic withdrawal).
    """
    from swarm_engine.governance.caller_authorization import (
        AgentDirectory, AuthorizationError, require_authorized)
    from swarm_engine.governance.oracle_binding import DECISION_QUARANTINE
    oreg = getattr(engine, "oracle_registry", None)
    if oreg is None:
        raise AuthorizationError(
            "quarantine_everywhere refused: engine has no oracle registry "
            "-- caller authorization cannot be verified")
    authed = require_authorized(
        oreg, AgentDirectory(oreg), caller, DECISION_QUARANTINE,
        "quarantine_everywhere", target=capability_id)
    actions: Dict[str, Any] = {"reason": reason, "caller": authed}
    engine.capabilities.set_status(capability_id, "quarantined")
    actions["store"] = "quarantined"
    # Drop exact-goal bindings to this id so recover can bind a live
    # ACTIVE capability. Leaving the stale winner makes resolve_goal()
    # return None (it requires status=active) even after re-admission.
    try:
        unbound = []
        for g, cid in list(engine.capabilities.goal_bindings().items()):
            if cid == capability_id:
                if engine.capabilities.unbind_goal_if(g, capability_id):
                    unbound.append(g)
        actions["unbound_goals"] = unbound
    except Exception as e:
        actions["unbind_error"] = str(e)
    # 2026-09-19 (R7): acquired-code capabilities live in their own table;
    # set_status on plan_capabilities is a no-op for them. Quarantine the
    # entry in its own store so boot's restore loop does not resurrect it.
    try:
        ac = getattr(engine, "acquired_code", None)
        if ac is not None and ac.set_status_by_id(capability_id, "quarantined"):
            actions["acquired_code"] = "quarantined"
    except Exception as e:
        actions["acquired_code_error"] = str(e)
    _ensure_provenance(engine, capability_id)
    try:
        engine.provenance.set_trust(capability_id, TrustLevel.QUARANTINED,
                                    reason)
        actions["trust"] = TrustLevel.QUARANTINED.value
    except Exception as e:
        actions["trust_error"] = str(e)
    try:
        engine.lifecycle.transition(capability_id,
                                    LifecycleState.QUARANTINED,
                                    reason, force=True)
        actions["lifecycle"] = LifecycleState.QUARANTINED.value
    except Exception as e:
        actions["lifecycle_error"] = str(e)
    engine.capabilities.log(capability_id, "quarantined_everywhere", reason)
    # Propagate through the real dependency graph (no second system).
    # 2026-09-19 (R8): this MUST run before the unregister block below.
    # plan_acquired_refs resolves bare acquired-code aliases (e.g. a
    # code-acquired capability registered under its node name) through the
    # registry's tag index; unregistering the seed's primitives pops those
    # tags, so propagating afterwards is blind to exactly the dependents
    # it exists to find (canonical acquired.<id> refs survive tag removal,
    # which is why the canonical-id lifecycle propagated but the
    # bare-alias lifecycle did not).
    try:
        from swarm_engine.cognition.revocation import (
            propagate_plan_quarantine)
        prop = propagate_plan_quarantine(
            engine.capabilities, engine.primitives, {capability_id},
            cause="deliberate")
        actions["propagated"] = prop["propagated"]
    except Exception as e:
        actions["propagation_error"] = str(e)
    # Unregister every primitive name bound to this capability id so the
    # Composer cannot keep executing it (statuses alone do not gate
    # execution; the registry does). Propagated dependents are handled by
    # propagate_plan_quarantine -> _quarantine, which unregisters all
    # names bound to each dependent's id (2026-09-19 R11).
    try:
        reg = engine.primitives
        names = [f"acquired.{capability_id}"]
        tag_index = getattr(reg, "_acquired_capability_ids", None)
        if isinstance(tag_index, dict):
            names.extend(k for k, v in tag_index.items()
                         if v == capability_id and k not in names)
        unregistered = []
        for name in names:
            try:
                if reg.unregister(name):
                    unregistered.append(name)
            except Exception:
                pass
            if isinstance(tag_index, dict):
                tag_index.pop(name, None)
        actions["unregistered"] = unregistered
    except Exception as e:
        actions["unregister_error"] = str(e)
    return actions


class RestoreRefused(Exception):
    """Raised when a governed restore cannot proceed. The capability is
    left quarantined (fail closed)."""


# Canonical forward order for the restore walk. QUARANTINED may legally go
# to CANDIDATE ("re-attempt after repair", lifecycle.py); from there the
# normal forward chain runs to DEPLOYED.
_RESTORE_WALK = [
    LifecycleState.CANDIDATE, LifecycleState.CONSTRUCTED,
    LifecycleState.VALIDATING, LifecycleState.VERIFIED,
    LifecycleState.ADMITTED, LifecycleState.REGISTERED,
    LifecycleState.DEPLOYED,
]


def _unregister_capability_primitives(engine, capability_id: str) -> list:
    """Remove every primitive name bound to this capability id (mirror of
    the unregister block in quarantine_everywhere)."""
    unregistered = []
    try:
        reg = engine.primitives
        names = [f"acquired.{capability_id}"]
        tag_index = getattr(reg, "_acquired_capability_ids", None)
        if isinstance(tag_index, dict):
            names.extend(k for k, v in tag_index.items()
                         if v == capability_id and k not in names)
        for name in names:
            try:
                if reg.unregister(name):
                    unregistered.append(name)
            except Exception:
                pass
            if isinstance(tag_index, dict):
                tag_index.pop(name, None)
    except Exception:
        pass
    return unregistered


def restore_everywhere(engine, capability_id: str, *,
                       caller: Any = None, reason: str = "",
                       smoke=None) -> Dict[str, Any]:
    """Governed inverse of quarantine_everywhere: the 'restore path that
    inverts the revocation' named (but never implemented) by
    capability_store.py's deliberate-quarantine comment.

    caller: REQUIRED -- the caller must hold BOTH 'agent:restore' (the
    restore itself) and 'trust:transition' (the trust hop it entails).
    Authentication is by token, never by a bare producer_id attribute:
    the old ``authority`` handle was forgeable -- any object with a
    producer_id attribute passed the check without proving it holds the
    credential. The HTTP adapter previously passed the ENGINE's own
    handle for every HTTP caller; it now forwards the HTTP caller's own
    credentials.

    This is NOT a status flip. The persisted plan is re-verified through the
    real admission path (type check, effect ceiling, permission check, and an
    optional smoke test, all against the CURRENT registry and policy); only
    then are the three status systems moved through their governed
    transitions:

      store     -> active   via AdmissionController.admit (INSERT OR REPLACE,
                               goal re-bound, primitive re-registered)
      lifecycle -> DEPLOYED via CapabilityLifecycle.transition along the
                               legal QUARANTINED -> CANDIDATE -> ... ->
                               DEPLOYED chain (every hop logged)
      trust     -> TRUSTED  via ProvenanceStore.set_trust, which routes
                               through the tamper-evident trust-transition
                               log and requires 'trust:transition' authority

    Preconditions (each a refusal, fail closed):
      * the capability id is known;
      * its effective status is quarantined (this is not a generic activator);
      * the quarantine was deliberate (derived quarantines belong to
        audit_all's fixpoint recovery);
      * it was not epistemically revoked (vindication path only -- the
        epistemic guard in admit() is never bypassed);
      * the caller holds 'agent:restore' AND 'trust:transition'
        (token-authenticated);
      * the persisted plan still fingerprints to its id (else corruption);
      * its goal binding is not held by another ACTIVE capability.

    Any failure after admission re-verified re-quarantines the store row and
    unregisters the primitive, so the three systems stay consistent and the
    capability cannot run half-restored (fail closed).
    """
    from swarm_engine.governance.caller_authorization import (
        AgentDirectory, AuthorizationError, require_all,
        _engine_caller_context)
    from swarm_engine.governance.oracle_binding import (
        DECISION_RESTORE, DECISION_TRUST_TRANSITION)
    from swarm_engine.synthesis.admission import Verdict

    actions: Dict[str, Any] = {"capability_id": capability_id,
                               "reason": reason}
    if not reason:
        raise RestoreRefused("restore requires a non-empty reason (audited)")

    oreg = getattr(engine, "oracle_registry", None)
    if oreg is None:
        raise AuthorizationError(
            "restore_everywhere refused: engine has no oracle registry "
            "-- caller authorization cannot be verified")
    # The caller must hold BOTH 'agent:restore' and 'trust:transition'.
    # Token-authenticated: a forged handle with a bare producer_id no
    # longer passes.
    authed = require_all(
        oreg, AgentDirectory(oreg), caller,
        (DECISION_RESTORE, DECISION_TRUST_TRANSITION),
        "restore_everywhere", target=capability_id)
    actions["authority"] = authed
    actions["caller"] = authed

    rec = engine.capabilities.get(capability_id)
    if rec is None:
        raise RestoreRefused(f"unknown capability {capability_id!r}")
    actions["record"] = "found"

    eff = effective_status(engine, capability_id)
    if eff["effective"] != "quarantined":
        raise RestoreRefused(
            f"effective status is {eff['effective']!r}, not 'quarantined'; "
            "restore_everywhere only inverts a quarantine")
    actions["effective_before"] = eff

    if not engine.capabilities._quarantine_was_deliberate(capability_id):
        raise RestoreRefused(
            "quarantine is derived (dependency/integrity), not deliberate; "
            "use audit_all recovery, not restore_everywhere")
    actions["deliberate"] = True

    if engine.admission._epistemically_revoked(capability_id):
        raise RestoreRefused(
            "epistemically revoked: restore refused; vindication path only")
    actions["epistemic_clear"] = True

    # Integrity: the persisted plan must fingerprint to its own id, exactly
    # as admission's reuse stage demands. A corrupted stored plan must NOT
    # be trusted as the previously-admitted one.
    try:
        fp = plan_fingerprint(rec.plan)
    except Exception as exc:
        raise RestoreRefused(f"plan unreadable, cannot verify integrity: {exc!r}")
    if fp != capability_id:
        engine.capabilities.log(capability_id, "corruption_detected",
                                "restore refused: stored plan fingerprint "
                                f"mismatch ({fp[:16]}... != {capability_id[:16]}...)")
        raise RestoreRefused(
            "stored plan corrupted (fingerprint mismatch); restore refused")
    actions["integrity"] = "fingerprint_ok"

    # Goal steal-check: quarantine_everywhere dropped this capability's goal
    # bindings; re-binding must not steal a goal another ACTIVE capability
    # now holds.
    goal = (rec.goal or "").strip()
    if goal:
        holder = engine.capabilities.goal_bindings().get(goal.strip().lower())
        if holder and holder != capability_id:
            other = engine.capabilities.get(holder)
            if other is not None and other.status == "active":
                raise RestoreRefused(
                    f"goal {goal!r} is now bound to active capability "
                    f"{holder[:16]}...; restore refused to avoid stealing it")
    actions["goal"] = goal or None

    # Independent re-verification through the real admission path, against
    # the CURRENT registry and policy. This re-stores the row as active,
    # re-binds the goal, and re-registers acquired.<id> via the same
    # registration path fresh admission uses. _restore_reverification=True
    # skips ONLY admission's 0c deliberate-quarantine guard -- this restore
    # has already been authorized (agent:restore + trust:transition checked
    # above); the epistemic guard and all other stages still apply.
    # Engine-mediated: the engine's own caller context, so admit() records
    # the engine (which IS performing this re-verification) as supplier;
    # the originating caller is in actions["authority"]/reason.
    admit_result = engine.admission.admit(
        goal=rec.goal or rec.name, plan=dict(rec.plan),
        smoke=smoke, caller=_engine_caller_context(engine),
        _restore_reverification=True)
    actions["admission_verdict"] = admit_result.verdict
    actions["admission_stage"] = admit_result.stage
    if admit_result.verdict == Verdict.REJECTED:
        engine.capabilities.log(capability_id, "restore_refused",
                                f"re-verification rejected at "
                                f"{admit_result.stage}: {admit_result.reasons}")
        raise RestoreRefused(
            f"re-verification rejected at {admit_result.stage}: "
            f"{admit_result.reasons}")

    # Post-verification governed transitions. Fail closed: any failure
    # re-quarantines the store row and unregisters the primitive admission
    # just registered, so no half-restored capability can run.
    try:
        # Lifecycle: walk forward from the current state to DEPLOYED along
        # legal transitions only.
        from swarm_engine.governance.lifecycle import _TRANSITIONS
        walked = []
        current = engine.lifecycle.state_of(capability_id)
        order = ([LifecycleState.DISCOVERED] + _RESTORE_WALK +
                 [LifecycleState.MONITORED])
        if current == LifecycleState.QUARANTINED:
            seq = _RESTORE_WALK
        elif current in order:
            idx = order.index(current)
            dep_idx = order.index(LifecycleState.DEPLOYED)
            seq = order[idx + 1:dep_idx + 1] if idx < dep_idx else []
        else:
            seq = []
            if current != LifecycleState.DEPLOYED:
                raise RestoreRefused(
                    f"lifecycle in unexpected state {current.value!r}; "
                    "no legal restore walk")
        for nxt in seq:
            legal = _TRANSITIONS.get(current, [])
            if nxt not in legal:
                raise RestoreRefused(
                    f"illegal lifecycle hop {current.value} -> {nxt.value} "
                    "during restore; refusing")
            engine.lifecycle.transition(
                capability_id, nxt,
                f"restore_everywhere: {reason}", force=False)
            walked.append(nxt.value)
            current = nxt
        actions["lifecycle_walk"] = walked

        # Trust: governed transition through the tamper-evident log,
        # executed by the engine's own instrument (set_trust defaults to
        # the store's engine handle) and attributed to the engine producer.
        # The external caller never supplies trust authority directly --
        # it was already required to hold 'trust:transition' at the gate.
        _ensure_provenance(engine, capability_id)
        engine.provenance.set_trust(
            capability_id, TrustLevel.TRUSTED,
            f"restored_everywhere by {authed}: {reason}; re-verified "
            f"{admit_result.verdict} at {admit_result.stage}")
        actions["trust"] = TrustLevel.TRUSTED.value
    except RestoreRefused:
        raise
    except Exception as exc:
        try:
            engine.capabilities.set_status(capability_id, "quarantined")
            engine.capabilities.log(
                capability_id, "restore_failed",
                f"{exc!r}; store re-quarantined, primitive unregistered")
        except Exception:
            pass
        _unregister_capability_primitives(engine, capability_id)
        raise RestoreRefused(
            f"restore failed after re-verification: {exc!r}; capability "
            "left quarantined")

    engine.capabilities.log(capability_id, "restored_everywhere",
                            json.dumps({"authority": authed,
                                        "reason": reason,
                                        "admission_verdict": admit_result.verdict,
                                        "admission_stage": admit_result.stage,
                                        "lifecycle_walk": actions.get("lifecycle_walk")}))
    actions["effective_after"] = effective_status(engine, capability_id)
    return actions


def rollback_with_lineage(engine, capability_id: str,
                          reason: str) -> Optional[CapabilityRecord]:
    """Audited rollback: the store revert plus trust, lifecycle, and
    lineage updates so no table is left pointing at the bad revision."""
    store = engine.capabilities
    rec = store.get(capability_id)
    if rec is None:
        return None
    parent = store.rollback(capability_id)  # quarantines bad, activates parent
    if parent is None:
        return None
    _ensure_provenance(engine, capability_id)
    try:
        engine.provenance.set_trust(capability_id, TrustLevel.QUARANTINED,
                                    "rolled back: " + reason)
    except Exception:
        pass
    try:
        engine.lifecycle.transition(capability_id, LifecycleState.ROLLED_BACK,
                                    reason, force=True)
    except Exception:
        pass
    integ = IntegrityStore(store.db_path)
    integ.link("capability", capability_id, "capability",
               parent.capability_id, kind="rollback")
    store.log(capability_id, "rollback_reason", reason)
    store.log(parent.capability_id, "rollback_target",
              f"restored by rollback of {capability_id}: {reason}")
    return parent


# --------------------------------------------------------------------------
# verification: the full check, one call
# --------------------------------------------------------------------------

@dataclass
class IntegrityCheck:
    name: str
    status: str   # "pass" | "fail" | "warn" | "info"
    detail: str = ""


@dataclass
class IntegrityReport:
    capability_id: str
    ok: bool
    effective: str
    checks: List[IntegrityCheck] = field(default_factory=list)

    def failures(self) -> List[IntegrityCheck]:
        return [c for c in self.checks if c.status == "fail"]

    def as_dict(self) -> Dict[str, Any]:
        return {"capability_id": self.capability_id, "ok": self.ok,
                "effective": self.effective,
                "checks": [{"name": c.name, "status": c.status,
                            "detail": c.detail} for c in self.checks]}


def _structural_plan_check(plan: Any) -> List[str]:
    errors: List[str] = []
    if not isinstance(plan, dict):
        return ["plan is not a dict"]
    steps = plan.get("steps")
    if not isinstance(steps, list) or not steps:
        errors.append("plan 'steps' must be a non-empty list")
        return errors
    for i, s in enumerate(steps):
        if not isinstance(s, dict):
            errors.append(f"step {i} is not a dict")
            continue
        if not s.get("id"):
            errors.append(f"step {i} missing id")
        if not s.get("op") and not s.get("control"):
            errors.append(f"step {i} ({s.get('id')}) has neither op nor control")
    if "output" not in plan:
        errors.append("plan missing 'output'")
    return errors


def verify_capability(engine, capability_id: str) -> IntegrityReport:
    """Run every integrity check on a stored capability:

    1. plan fingerprint recomputed vs capability_id (corruption);
    2. record seal vs IntegrityStore (any-field tampering);
    3. structural plan validation + admission-grade type check on the
       *current* registry (rehydration validation);
    4. dependency existence + pinned fingerprint comparison (silent
       primitive mutation);
    5. lifecycle reconciliation -> effective status.
    """
    checks: List[IntegrityCheck] = []
    rec = engine.capabilities.get(capability_id)
    if rec is None:
        return IntegrityReport(capability_id, False, "unknown",
                               [IntegrityCheck("exists", "fail",
                                               "no such capability")])

    # 1. plan fingerprint: the id IS the checksum -- recompute it.
    recomputed = plan_fingerprint(rec.plan)
    if recomputed == capability_id:
        checks.append(IntegrityCheck("plan_id_match", "pass",
                                     "plan content matches its id"))
    else:
        checks.append(IntegrityCheck(
            "plan_id_match", "fail",
            f"stored plan does not match id: content hashes to "
            f"{recomputed}, id is {capability_id} -- plan was tampered"))

    # 2. record seal (any-field tampering).
    integ = IntegrityStore(engine.capabilities.db_path)
    seal_ok, seal_msg = integ.verify_seal("capability", capability_id,
                                          rec.as_dict())
    if seal_msg == "not_sealed":
        checks.append(IntegrityCheck("record_seal", "warn",
                                     "never sealed; seal at admission"))
    elif seal_ok:
        checks.append(IntegrityCheck("record_seal", "pass",
                                     "record matches its seal"))
    else:
        checks.append(IntegrityCheck("record_seal", "fail",
                                     "record does not match its seal -- "
                                     "a field was tampered"))

    # 3. re-validate the plan on the current registry.
    structural = _structural_plan_check(rec.plan)
    if structural:
        checks.append(IntegrityCheck("plan_schema", "fail",
                                     "; ".join(structural)))
    else:
        checks.append(IntegrityCheck("plan_schema", "pass",
                                     "steps/output shape valid"))
        try:
            analysis = engine.composer.analyze(rec.plan)
            if analysis.ok:
                checks.append(IntegrityCheck(
                    "plan_typecheck", "pass",
                    f"{len(analysis.primitives_used)} primitives, "
                    f"effects={sorted(str(e) for e in analysis.effects)}"))
            else:
                checks.append(IntegrityCheck(
                    "plan_typecheck", "fail",
                    "; ".join(analysis.errors[:5])))
        except Exception as e:
            checks.append(IntegrityCheck("plan_typecheck", "fail",
                                         f"analyzer raised: {e}"))

    # 4. dependencies: existence + pinned fingerprint comparison.
    ops = _walk_ops(rec.plan)
    missing = [op for op in set(ops) if op not in engine.primitives]
    if missing:
        checks.append(IntegrityCheck("deps_exist", "fail",
                                     "missing primitives: "
                                     + ", ".join(sorted(missing))))
    else:
        checks.append(IntegrityCheck("deps_exist", "pass",
                                     f"{len(set(ops))} ops resolve"))
    seal_rec = integ.get_seal("capability", capability_id)
    pins = (seal_rec or {}).get("dep_pins") or {}
    if not pins:
        checks.append(IntegrityCheck("deps_pinned", "warn",
                                     "no pinned fingerprints; seal at "
                                     "admission to enable drift detection"))
    else:
        drifted = []
        for op in sorted(set(ops)):
            prim = engine.primitives.get(op)
            live = fingerprint_primitive(prim) if prim else "MISSING"
            if pins.get(op) != live:
                drifted.append(f"{op}: pinned {pins.get(op)} != live {live}")
        if drifted:
            checks.append(IntegrityCheck("deps_pinned", "fail",
                                         "primitive drift: "
                                         + "; ".join(drifted)))
        else:
            checks.append(IntegrityCheck("deps_pinned", "pass",
                                         "all pinned fingerprints match"))

    # 5. lifecycle reconciliation.
    eff = effective_status(engine, capability_id)
    checks.append(IntegrityCheck(
        "lifecycle", "pass" if eff["consistent"] else "warn",
        f"effective={eff['effective']} "
        f"(store={eff['store_status']}, trust={eff['trust']}, "
        f"lifecycle={eff['lifecycle']})"))

    ok = not any(c.status == "fail" for c in checks)
    return IntegrityReport(capability_id, ok, eff["effective"], checks)


def seal_capability(engine, cap: CapabilityRecord) -> str:
    """Seal a capability at admission: record seal + pinned primitive
    fingerprints. Returns the seal."""
    integ = IntegrityStore(engine.capabilities.db_path)
    pins = snapshot_plan_deps(cap.plan, engine.primitives)
    return integ.seal_object("capability", cap.capability_id,
                             cap.as_dict(), dep_pins=pins)


# --------------------------------------------------------------------------
# quarantine reason API + self-diagnosis / self-repair drivers (M5)
#
# Frozen cross-loop interface: get_quarantine_reason(capability_id) ->
# {reason, since, system}. M3 consumes it. The single-argument form works
# against the default engine DB (SWARM_ENGINE_DB env or swarm_engine.db);
# pass engine= explicitly when a live engine is in hand.
#
# This section ADDS to integrity.py; the frozen functions above
# (effective_status, quarantine_everywhere, restore_everywhere,
# rollback_with_lineage, verify_capability, seal_capability) are untouched.
# --------------------------------------------------------------------------

#: Canonical reason-label vocabulary (frozen). Free-text reasons recorded by
#: the three status systems are preserved verbatim in `reason`; `label`
#: carries the canonical classification where one applies.
QUARANTINE_REASON_VOCABULARY = (
    "not-applicable",
    "needs-improvement",
    "not-functional",
    "missing-dependency",
    "missing-substrate",
    "limitation",  # Q10: capability limitation at scale/format, diagnosed
    "tampered",
    "verification-failed",
    "revoked",
)

#: The three status systems the reason API reads, in priority order.
QUARANTINE_SYSTEMS = ("store", "provenance", "lifecycle")

#: Store events that record a quarantine (newest-first scan).
_STORE_QUARANTINE_EVENTS = (
    "quarantined_everywhere",
    "quarantined",
    "corruption_detected",
)

#: Lifecycle states that count as quarantined for the reason API.
_LIFECYCLE_QUARANTINED = ("quarantined",)


def _quarantine_stores(engine=None, db_path=None):
    """Resolve the three status systems without booting an engine.

    A live engine wins (its stores may carry oracle bindings). Otherwise
    the three stores are constructed directly on the DB path -- read-only
    usage here; their constructors only ensure tables exist.
    """
    if engine is not None:
        return (engine.capabilities, engine.provenance, engine.lifecycle)
    from swarm_engine.governance.lifecycle import CapabilityLifecycle
    from swarm_engine.governance.provenance import ProvenanceStore
    from swarm_engine.synthesis.capability_store import CapabilityStore
    path = db_path or os.environ.get("SWARM_ENGINE_DB", "swarm_engine.db")
    return CapabilityStore(path), ProvenanceStore(path), CapabilityLifecycle(path)


def _latest_named_event(events, names):
    """Newest event whose name is in `names`; events carry 'at'."""
    best = None
    for ev in events or []:
        if ev.get("event") in names:
            if best is None or (ev.get("at") or 0) > (best.get("at") or 0):
                best = ev
    return best


def _parse_trust_reason(detail: str) -> Optional[str]:
    """Provenance logs trust changes as 'QUARANTINED: <reason> [...]'."""
    if not detail:
        return None
    d = detail.strip()
    if d.startswith("QUARANTINED:"):
        d = d[len("QUARANTINED:"):].strip()
    # strip trailing "[transition=...]" attribution
    cut = d.find(" [transition=")
    if cut != -1:
        d = d[:cut].strip()
    return d or None


def get_quarantine_reason(capability_id: str, engine=None,
                          db_path: Optional[str] = None) -> Dict[str, Any]:
    """Frozen cross-loop interface: WHY is this capability quarantined?

    Reads all three status systems:
      store      -- plan_capabilities.status + capability_events
                    (quarantined_everywhere / quarantined / corruption_detected)
      provenance -- trust level + provenance_events trust_changed to QUARANTINED
      lifecycle  -- CapabilityLifecycle.history, last transition to QUARANTINED

    Returns {"reason", "since", "system", "quarantined", "systems"} where
    `reason` is the actionable (most recent) quarantine reason, `since` its
    timestamp, and `system` is one of "store" | "provenance" | "lifecycle" |
    "all" (every reporting system agrees) | "none" (not quarantined
    anywhere). `systems` carries the per-system detail; a system that says
    quarantined but recorded no reason reports that gap honestly instead of
    inventing one.
    """
    store, prov, lc = _quarantine_stores(engine, db_path)
    per: Dict[str, Dict[str, Any]] = {}

    # -- store -----------------------------------------------------------
    store_q = False
    store_reason = None
    store_since = None
    try:
        rec = store.get(capability_id)
        store_q = rec is not None and rec.status == "quarantined"
        ev = _latest_named_event(store.events(capability_id, limit=100),
                                 _STORE_QUARANTINE_EVENTS)
        if ev is not None:
            store_reason = ev.get("detail") or None
            store_since = ev.get("at")
    except Exception as e:
        per["store"] = {"quarantined": None, "reason": None, "since": None,
                        "error": str(e)}
    if "store" not in per:
        per["store"] = {"quarantined": store_q, "reason": store_reason,
                        "since": store_since}

    # -- provenance ------------------------------------------------------
    prov_q = False
    prov_reason = None
    prov_since = None
    try:
        from swarm_engine.governance.provenance import TrustLevel
        prec = prov.get(capability_id)
        prov_q = prec is not None and prec.trust == TrustLevel.QUARANTINED
        ev = _latest_named_event(prov.events(capability_id, limit=100),
                                 ("trust_changed",))
        if ev is not None and (ev.get("detail") or "").startswith("QUARANTINED"):
            prov_reason = _parse_trust_reason(ev.get("detail"))
            prov_since = ev.get("at")
    except Exception as e:
        per["provenance"] = {"quarantined": None, "reason": None,
                             "since": None, "error": str(e)}
    if "provenance" not in per:
        per["provenance"] = {"quarantined": prov_q, "reason": prov_reason,
                             "since": prov_since}

    # -- lifecycle -------------------------------------------------------
    # Quarantined-ness is the CURRENT lifecycle state, not "was ever
    # quarantined": a capability restored through the legal walk is
    # DEPLOYED now, and reporting it quarantined here would disagree with
    # effective_status and send repair into a doomed restore_everywhere
    # (which correctly refuses: it only inverts a quarantine). History is
    # still mined for the most recent quarantine reason/since.
    lc_q = False
    lc_reason = None
    lc_since = None
    try:
        from swarm_engine.governance.lifecycle import LifecycleState
        try:
            lc_q = (lc.state_of(capability_id)
                    == LifecycleState.QUARANTINED)
        except Exception:
            lc_q = False
        try:
            hist = lc.history(capability_id)
        except Exception:
            hist = []
        last_q = None
        for t in hist:
            if t.to_state == LifecycleState.QUARANTINED:
                if last_q is None or t.at > last_q.at:
                    last_q = t
        if last_q is not None:
            lc_reason = last_q.reason or None
            lc_since = last_q.at
    except Exception as e:
        per["lifecycle"] = {"quarantined": None, "reason": None,
                            "since": None, "error": str(e)}
    if "lifecycle" not in per:
        per["lifecycle"] = {"quarantined": lc_q, "reason": lc_reason,
                            "since": lc_since}

    quarantined_systems = [s for s in QUARANTINE_SYSTEMS
                           if per[s]["quarantined"] is True]
    if not quarantined_systems:
        return {"reason": None, "since": None, "system": "none",
                "quarantined": False, "systems": per}

    # The actionable reason is the most recent quarantine record; every
    # system's record stays visible in `systems`.
    candidates = [(per[s]["since"] or 0, s) for s in quarantined_systems]
    candidates.sort()
    _, newest = candidates[-1]
    reason = per[newest]["reason"]
    since = per[newest]["since"]
    if reason is None:
        # Quarantined but no system recorded why: report the gap, don't
        # invent a reason.
        reason = ("quarantined with no recorded reason in any status "
                  "system (provenance gap)")
    if len(quarantined_systems) == len(QUARANTINE_SYSTEMS):
        system = "all"
    else:
        system = "+".join(sorted(quarantined_systems))
    return {"reason": reason, "since": since, "system": system,
            "quarantined": True, "systems": per}


# --------------------------------------------------------------------------
# diagnostic driver: is the recorded reason still true?
# --------------------------------------------------------------------------

@dataclass
class QuarantineDiagnosis:
    capability_id: str
    quarantined: bool
    reason: Optional[str]
    since: Optional[float]
    system: str
    reason_still_holds: Optional[bool]  # None: not quarantined / indeterminate
    verdict: str  # not_quarantined | reason_holds | reason_cleared | indeterminate
    checks: List[Dict[str, Any]] = field(default_factory=list)
    label: Optional[str] = None

    def as_dict(self) -> Dict[str, Any]:
        return {"capability_id": self.capability_id,
                "quarantined": self.quarantined,
                "reason": self.reason, "since": self.since,
                "system": self.system,
                "reason_still_holds": self.reason_still_holds,
                "verdict": self.verdict, "label": self.label,
                "checks": self.checks}


# ---------------------------------------------------------------------------
# Limitation-gap reason shape (Q10)
#
# A capability limitation at scale/format is a valid quarantine-reason
# shape alongside "missing dependency" (James, 2026-09-27). The shape is
#   limitation:{task}:{scale_or_format}
# carried as a structured record: attempted task, attempted scale/format,
# the observed failure, and the "what's preventing it" diagnosis. The
# diagnosis machinery below is generic -- it parses the record, replays
# its carried reproduction in a bounded subprocess to separate "can't do
# at this scale" from "can't do at all", and routes the limitation toward
# technique acquisition (M1/M2), substrate (M3), or synthesis. No
# limitation strings are hardcoded anywhere in this path.
# ---------------------------------------------------------------------------

#: Structured limitation-reason prefix. The full record follows as JSON:
#: {"task", "scale_or_format", "failure", "preventing", "reproduce"?}.
LIMITATION_PREFIX = "limitation:"

#: Strict shape for a probe callable reference: "importable.module:attr".
_CALLABLE_RE = None  # compiled lazily (re import kept local)

_TECHNIQUE_HINTS = ("technique", "algorithm", "tiled", "tiling",
                    "streaming", "chunked", "knowledge", "method",
                    "approach", "distill")

# "Missing" semantics for routing (narrower than _MISSING_HINTS): the word
# "substrate"/"dependency" alone must NOT route to substrate acquisition --
# "the PIL substrate cannot decode .obj" means the present substrate is
# incapable (synthesize or learn a technique), while "PIL is not installed"
# means acquire it. Only genuine absence routes to M3.
_SUBSTRATE_ABSENT_HINTS = ("missing", "not installed", "not importable",
                           "no module", "unavailable", "absent")

# Generic bound patterns a failure observation may name, e.g.
# "spec.width: expected int in [1,2048], got 10000".
# 2026-09-27 (Q10-R1): added the media generators' genuine refusal-text
# family "refused: dimensions out of bounds [1, 2048] (got 10000x10000)" --
# previously only "in [lo,hi]" matched, so an out-of-bounds refusal named
# no bound and routed to synthesis instead of technique (M7 incident 1).
# All patterns stay generic: no bound numbers are hardcoded anywhere.
_BOUND_PATTERNS = (
    r"in\s*\[\s*\d+\s*,\s*(\d+)\s*\]",   # "in [lo,hi]" -> hi
    r"out of bounds\s*\[\s*\d+\s*,\s*(\d+)\s*\]",  # "out of bounds [1, 2048]" -> hi
    r"maximum\s*(?:of\s*)?(\d+)",          # "maximum 2048" / "maximum of 2048"
    r"at most\s*(\d+)",                   # "at most 2048"
)


def _callable_re():
    global _CALLABLE_RE
    if _CALLABLE_RE is None:
        import re
        _CALLABLE_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.]*:[A-Za-z_][A-Za-z0-9_]*$")
    return _CALLABLE_RE


def format_limitation_reason(task: str, scale_or_format: str, *,
                             failure: str = "",
                             preventing: str = "",
                             reproduce: Optional[Dict[str, Any]] = None) -> str:
    """Build a limitation-shaped quarantine reason (the quarantining path's
    constructor). `reproduce` optionally carries a machine-readable
    reproduction for the diagnostic probe:
      {"callable": "importable.module:attr",
       "args": {...},            # the failing invocation
       "reduced_args": {...}}    # the same invocation at reduced scale
    Binary args are carried as {"$b64": "<base64>"}. The callable is
    replayed in a bounded subprocess by the diagnosis; it must be
    side-effect-free with respect to engine state (pure characterization).
    """
    import json
    record = {"task": task, "scale_or_format": scale_or_format,
              "failure": failure, "preventing": preventing}
    if reproduce is not None:
        record["reproduce"] = reproduce
    return LIMITATION_PREFIX + json.dumps(record, sort_keys=True)


def parse_limitation_reason(reason: Optional[str]) -> Optional[Dict[str, Any]]:
    """Parse a limitation-shaped reason into its record. Returns None when
    the reason is not limitation-shaped or not parseable (fail closed)."""
    import json
    if not reason or not reason.startswith(LIMITATION_PREFIX):
        return None
    try:
        rec = json.loads(reason[len(LIMITATION_PREFIX):])
    except Exception:
        return None
    if not isinstance(rec, dict):
        return None
    return {"task": rec.get("task", ""), "scale_or_format": rec.get("scale_or_format", ""),
            "failure": rec.get("failure", ""), "preventing": rec.get("preventing", ""),
            "reproduce": rec.get("reproduce")}


def _limitation_numbers(text: str) -> List[int]:
    import re
    return [int(x) for x in re.findall(r"\d+", text or "")]


def _limitation_declared_bound(failure: str) -> Optional[int]:
    """A bound the failure observation itself names (generic patterns)."""
    import re
    for pat in _BOUND_PATTERNS:
        m = re.search(pat, failure or "", re.IGNORECASE)
        if m:
            try:
                return int(m.group(1))
            except Exception:
                continue
    return None


def _route_limitation(preventing: str,
                      scale_specific: Optional[bool],
                      probe_specific: bool = False) -> str:
    """Route a diagnosed limitation toward the next acquisition path.

    Returns one of "substrate" (M3), "technique" (M1/M2), "synthesis".
    Routing is by generic hint shape on the preventing-text, never by
    limitation-specific strings.

    probe_specific carries the active separability probe's confirmation
    (reduced-scale ok + full-scale fails): a probe-confirmed specific
    limitation upgrades an UNKNOWN static separability to scale-specific,
    because smaller invocations demonstrably work. It never overrides an
    explicit format/class classification -- the axis comes from the
    static record, not the probe. (2026-09-27, Q10-R1: previously the
    probe's specific_confirmed verdict was computed but dropped, so a
    10K limitation with an unparseable refusal format routed to
    synthesis; M7 incident 1.)
    """
    p = (preventing or "").lower()
    if any(h in p for h in _SUBSTRATE_ABSENT_HINTS):
        return "substrate"
    if any(h in p for h in _TECHNIQUE_HINTS):
        return "technique"
    if scale_specific is True or (scale_specific is None and probe_specific):
        # A scale the current mechanism cannot reach but smaller scales
        # can: crossable by a better technique (tiling, streaming, ...).
        return "technique"
    if scale_specific is False:
        # A format/class the mechanism cannot handle at any scale.
        return "synthesis"
    return "synthesis"


# Fixed probe child: replays one recorded invocation in a plain
# subprocess (NOT the W4-R1 capability sandbox -- that sandbox forbids
# the very imports a characterization probe needs). The parent enforces
# a hard wall timeout and kills on expiry. The script is constant; only
# the reproduce spec travels as data.
_LIMITATION_PROBE_CHILD = """\
import base64, importlib, json, sys

def _decode(v):
    if isinstance(v, dict) and set(v.keys()) == {"$b64"}:
        return base64.b64decode(v["$b64"])
    if isinstance(v, dict):
        return {k: _decode(x) for k, x in v.items()}
    if isinstance(v, list):
        return [_decode(x) for x in v]
    return v

def main():
    spec_path, which, out_path = sys.argv[1], sys.argv[2], sys.argv[3]
    out = {"which": which, "ok": False}
    try:
        spec = json.load(open(spec_path, encoding="utf-8"))
        call = spec.get("callable", "")
        mod_name, _, attr = call.rpartition(":")
        fn = getattr(importlib.import_module(mod_name), attr)
        args = _decode(spec.get("args") if which == "full"
                       else spec.get("reduced_args", {}))
        try:
            fn(**args) if isinstance(args, dict) else fn(*args)
            out["ok"] = True
            out["result"] = "returned-ok"
        except Exception as e:
            out["error"] = "%s: %s" % (type(e).__name__, e)
    except Exception as e:
        out["error"] = "setup: %s: %s" % (type(e).__name__, e)
    json.dump(out, open(out_path, "w", encoding="utf-8"))

main()
"""


def _validate_reproduce_spec(reproduce: Any) -> Optional[str]:
    """Strict shape check on a carried reproduction. Returns an error
    string when invalid, None when the spec is usable."""
    if not isinstance(reproduce, dict):
        return "reproduce is not a dict"
    call = reproduce.get("callable")
    if not isinstance(call, str) or not _callable_re().match(call):
        return "reproduce.callable is not a module:attr reference"
    for key in ("args", "reduced_args"):
        if key in reproduce and not isinstance(reproduce[key], (dict, list)):
            return f"reproduce.{key} must be a dict or list"
    return None


def _pylib_dir() -> Optional[str]:
    """A sys.path entry that provides the swarm_engine package (for the
    probe child's environment)."""
    import os as _os
    for entry in sys.path:
        try:
            if entry and _os.path.isdir(_os.path.join(entry, "swarm_engine")):
                return entry
        except Exception:
            continue
    return None


def _run_limitation_probe(reproduce: Dict[str, Any],
                          timeout_s: float = 60.0) -> Dict[str, Any]:
    """Replay the recorded reproduction at reduced and full scale in
    bounded child processes. Returns
      {"reduced": {...}, "full": {...}, "setup_error": ...?}
    where each side is {"which", "ok", "result"|"error", "elapsed_ms"}
    or {"which", "ok": False, "error": "probe_timeout"} on expiry.
    Never raises: every failure mode is a recorded outcome."""
    import os
    import subprocess
    import tempfile
    import time as _time
    result: Dict[str, Any] = {}
    try:
        tmp = tempfile.mkdtemp(prefix="limprobe_")
        spec_path = os.path.join(tmp, "spec.json")
        child_path = os.path.join(tmp, "probe_child.py")
        with open(spec_path, "w", encoding="utf-8") as fh:
            json.dump(reproduce, fh)
        with open(child_path, "w", encoding="utf-8") as fh:
            fh.write(_LIMITATION_PROBE_CHILD)
        env = dict(os.environ)
        pylib = _pylib_dir()
        if pylib:
            env["PYTHONPATH"] = pylib + os.pathsep + env.get("PYTHONPATH", "")
        for which in ("reduced", "full"):
            out_path = os.path.join(tmp, f"out_{which}.json")
            t0 = _time.monotonic()
            side: Dict[str, Any] = {"which": which, "ok": False}
            try:
                proc = subprocess.Popen(
                    [sys.executable, child_path, spec_path, which, out_path],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    env=env, cwd=tmp)
                try:
                    proc.communicate(timeout=timeout_s)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.communicate()
                    side["error"] = "probe_timeout"
                    side["elapsed_ms"] = (_time.monotonic() - t0) * 1000.0
                    result[which] = side
                    continue
                try:
                    with open(out_path, encoding="utf-8") as fh:
                        side = json.load(fh)
                except Exception as e:
                    side["error"] = f"no probe output: {e}"
                side["elapsed_ms"] = (_time.monotonic() - t0) * 1000.0
                if proc.returncode not in (0, None):
                    side.setdefault("error",
                                    f"child exit {proc.returncode}")
                    side["ok"] = False
            except Exception as e:
                side["error"] = f"probe harness: {type(e).__name__}: {e}"
                side["elapsed_ms"] = (_time.monotonic() - t0) * 1000.0
            result[which] = side
    except Exception as e:
        result["setup_error"] = f"{type(e).__name__}: {e}"
    return result


_MISSING_HINTS = ("missing", "not installed", "not importable", "no module",
                  "substrate", "dependency", "unavailable", "absent")
_TAMPER_HINTS = ("tamper", "corrupt", "fingerprint", "mismatch", "seal")
_SMOKE_HINTS = ("smoke", "verification failed", "re-verification",
                "admission refused")


def _candidate_modules(reason: str) -> List[str]:
    """Module names a quarantine reason plausibly names: quoted tokens
    plus known substrate names mentioned in the text."""
    import re
    cands: List[str] = []
    for tok in re.findall(r"'([^']+)'|\"([^\"]+)\"|`([^`]+)`", reason or ""):
        name = next(t for t in tok if t)
        if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.]*", name):
            cands.append(name.split(".")[0])
    known = ("piper", "PIL", "pillow", "numpy", "scipy", "onnx",
             "torch", "cv2", "ffmpeg")
    low = (reason or "").lower()
    for k in known:
        if k.lower() in low and k not in cands and k.lower() not in cands:
            cands.append(k)
    seen = set()
    out = []
    for c in cands:
        if c.lower() not in seen:
            seen.add(c.lower())
            out.append(c)
    return out


def _importable(module: str) -> Tuple[bool, str]:
    import importlib.util
    try:
        spec = importlib.util.find_spec(module)
    except Exception as e:
        return False, f"find_spec raised: {e}"
    if spec is None:
        return False, "find_spec -> None (not installed)"
    return True, f"found at {spec.origin}"


def _reason_class(reason: Optional[str]) -> str:
    r = (reason or "")
    # Q10: the structured limitation shape is classified by its prefix
    # before any hint matching (a preventing-text may itself contain
    # substrate words like "unavailable").
    if r.startswith(LIMITATION_PREFIX):
        return "limitation"
    rl = r.lower()
    if any(h in rl for h in _MISSING_HINTS):
        return "missing_substrate"
    if any(h in rl for h in _TAMPER_HINTS):
        return "tampered"
    if any(h in rl for h in _SMOKE_HINTS):
        return "verification"
    if "superseded" in rl or "not-applicable" in rl or "not applicable" in rl:
        return "not_applicable"
    if "revoked" in rl:
        return "revoked"
    # Free-text fallback: only the explicit word "limitation" -- generic
    # scale/format words are too noisy to classify on.
    if "limitation" in rl:
        return "limitation"
    return "unknown"


def _stay_label(reason: Optional[str], reason_class: str) -> str:
    """Honest vocabulary for a capability that stays quarantined."""
    if reason_class == "not_applicable":
        return "not-applicable"
    if reason_class == "missing_substrate":
        return "not-functional"  # cannot run in this environment
    if reason_class == "tampered":
        return "needs-improvement"  # needs a repair action, not just time
    if reason_class == "limitation":
        # Q10: the limitation branch computes its own routed label; this
        # is only the fallback for paths that never reach it.
        return "needs-improvement"
    return "needs-improvement"


def diagnose_quarantine(engine, capability_id: str) -> QuarantineDiagnosis:
    """Determine whether the recorded quarantine reason still holds.

    Checks, in order:
      1. the frozen reason API (what was recorded, where, when);
      2. baseline integrity: plan fingerprint + dependency existence
         (re-runs the read-only parts of verify_capability);
      3. class-specific re-check:
           missing_substrate -> substrate import re-check;
           tampered          -> fingerprint/seal re-verification;
           verification/smoke, revoked, unknown -> indeterminate (fail
             closed: only a positive "cleared" signal repairs).
    """
    qr = get_quarantine_reason(capability_id, engine=engine)
    checks: List[Dict[str, Any]] = [
        {"name": "quarantine_reason", "outcome": "recorded"
         if qr["quarantined"] else "absent",
         "detail": f"system={qr['system']} reason={qr['reason']!r}"}]
    if not qr["quarantined"]:
        return QuarantineDiagnosis(
            capability_id, False, None, None, "none", None,
            "not_quarantined", checks)

    reason = qr["reason"]
    rclass = _reason_class(reason)
    checks.append({"name": "reason_class", "outcome": rclass,
                   "detail": f"classified from {reason!r}"})

    # Baseline: plan integrity + dependency existence (read-only).
    rec = engine.capabilities.get(capability_id)
    if rec is None:
        checks.append({"name": "plan_integrity", "outcome": "unknown",
                       "detail": "record not found in store"})
        fp_ok = None
    else:
        try:
            fp_ok = plan_fingerprint(rec.plan) == capability_id
        except Exception as e:
            fp_ok = False
            checks.append({"name": "plan_integrity", "outcome": "error",
                           "detail": str(e)[:200]})
        if fp_ok is not None and not any(
                c["name"] == "plan_integrity" for c in checks):
            checks.append({"name": "plan_integrity",
                           "outcome": "pass" if fp_ok else "fail",
                           "detail": "plan fingerprints to its id"
                           if fp_ok else "plan content != id (tampered)"})
        ops = _walk_ops(rec.plan) if isinstance(rec.plan, dict) else []
        missing = [op for op in set(ops) if op not in engine.primitives]
        checks.append({"name": "deps_exist",
                       "outcome": "pass" if not missing else "fail",
                       "detail": ("all ops resolve" if not missing
                                  else "missing: " + ", ".join(sorted(missing)))})

    # Class-specific re-check.
    if rclass == "missing_substrate":
        mods = _candidate_modules(reason)
        checks.append({"name": "substrate_candidates", "outcome": "listed",
                       "detail": f"modules named by reason: {mods}"})
        if not mods:
            return QuarantineDiagnosis(
                capability_id, True, reason, qr["since"], qr["system"],
                None, "indeterminate", checks,
                _stay_label(reason, rclass))
        still_missing = []
        for m in mods:
            ok, detail = _importable(m)
            checks.append({"name": f"substrate_import:{m}",
                           "outcome": "pass" if ok else "fail",
                           "detail": detail})
            if not ok:
                still_missing.append(m)
        if still_missing:
            return QuarantineDiagnosis(
                capability_id, True, reason, qr["since"], qr["system"],
                True, "reason_holds", checks,
                _stay_label(reason, rclass))
        return QuarantineDiagnosis(
            capability_id, True, reason, qr["since"], qr["system"],
            False, "reason_cleared", checks, None)

    if rclass == "tampered":
        # Re-verify: fingerprint (above) plus the record seal.
        integ = IntegrityStore(engine.capabilities.db_path)
        if rec is not None:
            seal_ok, seal_msg = integ.verify_seal("capability",
                                                  capability_id,
                                                  rec.as_dict())
            checks.append({"name": "record_seal",
                           "outcome": ("pass" if seal_ok else "fail")
                           if seal_msg != "not_sealed" else "unknown",
                           "detail": seal_msg})
        else:
            seal_ok, seal_msg = False, "no record"
        holds = (fp_ok is False) or (seal_msg not in ("match", "not_sealed")
                                     and not seal_ok)
        if holds:
            return QuarantineDiagnosis(
                capability_id, True, reason, qr["since"], qr["system"],
                True, "reason_holds", checks,
                _stay_label(reason, rclass))
        if fp_ok and seal_msg in ("match", "not_sealed"):
            return QuarantineDiagnosis(
                capability_id, True, reason, qr["since"], qr["system"],
                False, "reason_cleared", checks, None)
        return QuarantineDiagnosis(
            capability_id, True, reason, qr["since"], qr["system"],
            None, "indeterminate", checks,
            _stay_label(reason, rclass))

    # Q10: limitation at scale/format -- characterize what's preventing it.
    if rclass == "limitation":
        return _diagnose_limitation(engine, capability_id, reason, qr,
                                    checks)

    # verification / revoked / not_applicable / unknown: no safe automatic
    # re-check exists on this driver -- fail closed.
    checks.append({"name": "recheck", "outcome": "skipped",
                   "detail": f"no automatic re-check for class "
                             f"{rclass!r}; refusing to guess"})
    return QuarantineDiagnosis(
        capability_id, True, reason, qr["since"], qr["system"],
        None, "indeterminate", checks, _stay_label(reason, rclass))


def _diagnose_limitation(engine, capability_id: str,
                         reason: Optional[str],
                         qr: Dict[str, Any],
                         checks: List[Dict[str, Any]]) -> QuarantineDiagnosis:
    """Diagnose a limitation-shaped quarantine reason (Q10).

    1. Parse the structured record -- unparseable fails closed.
    2. Require an observed failure: a limitation asserted without an
       observation is indeterminate, never a verdict.
    3. Static separability: a numeric attempted scale against a bound the
       failure observation itself names ("can't do at this scale" vs
       "can't do at all" vs format/class with no scale axis).
    4. Active bounded probe when the record carries a valid reproduction:
       reduced-scale ok + full-scale fails confirms scale/format-specific;
       reduced failing too means mechanism-wide; full-scale succeeding
       means the limitation no longer holds (reason_cleared).
    5. Route toward substrate (M3) / technique (M1/M2) / synthesis and
       label honestly from the M5 vocabulary.
    """
    rec = parse_limitation_reason(reason)
    if rec is None:
        checks.append({"name": "limitation_parse", "outcome": "fail",
                       "detail": "reason classified as limitation but not "
                                 "parseable as the limitation shape"})
        return QuarantineDiagnosis(
            capability_id, True, reason, qr["since"], qr["system"],
            None, "indeterminate", checks, "needs-improvement")

    task = rec["task"]
    scale = rec["scale_or_format"]
    failure = rec["failure"]
    preventing = rec["preventing"]
    checks.append({"name": "limitation_record", "outcome": "parsed",
                   "detail": {"task": task, "scale_or_format": scale,
                              "preventing": preventing}})

    if not failure:
        checks.append({"name": "failure_observed", "outcome": "absent",
                       "detail": "limitation asserted without an observed "
                                 "failure -- refusing to diagnose a claim"})
        return QuarantineDiagnosis(
            capability_id, True, reason, qr["since"], qr["system"],
            None, "indeterminate", checks, "needs-improvement")
    checks.append({"name": "failure_observed", "outcome": "recorded",
                   "detail": failure[:300]})

    # -- static separability -------------------------------------------
    nums = _limitation_numbers(scale)
    bound = _limitation_declared_bound(failure)
    scale_specific: Optional[bool] = None
    mechanism_wide = False
    if nums and bound is not None and max(nums) > bound:
        scale_specific = True
        checks.append({"name": "separability_static",
                       "outcome": "scale_specific",
                       "detail": f"attempted scale {max(nums)} exceeds the "
                                 f"bound {bound} named by the failure "
                                 f"observation"})
    elif not nums:
        scale_specific = False
        checks.append({"name": "separability_static",
                       "outcome": "format_or_class",
                       "detail": "no numeric scale axis in "
                                 f"{scale!r}: a format/class limitation, "
                                 f"not a scale limitation"})
    else:
        checks.append({"name": "separability_static",
                       "outcome": "unknown",
                       "detail": "separability not determinable from the "
                                 "record alone"})

    # -- active bounded probe ------------------------------------------
    reproduce = rec.get("reproduce")
    probe_ran = False
    specific_confirmed = False  # reduced ok + full fails (any axis)
    if reproduce is not None:
        spec_err = _validate_reproduce_spec(reproduce)
        if spec_err:
            checks.append({"name": "limitation_probe", "outcome": "skipped",
                           "detail": f"invalid reproduce spec: {spec_err}"})
        else:
            probe = _run_limitation_probe(reproduce)
            probe_ran = True
            red = probe.get("reduced", {})
            full = probe.get("full", {})
            checks.append({"name": "limitation_probe", "outcome": "ran",
                           "detail": {
                               "reduced": {k: red.get(k) for k in
                                           ("ok", "error", "result",
                                            "elapsed_ms")},
                               "full": {k: full.get(k) for k in
                                        ("ok", "error", "result",
                                         "elapsed_ms")},
                               "setup_error": probe.get("setup_error")}})
            red_err = str(red.get("error") or "")
            full_err = str(full.get("error") or "")
            red_setup = red_err.startswith("setup:") or \
                red.get("error") == "probe_timeout"
            full_setup = full_err.startswith("setup:") or \
                full.get("error") == "probe_timeout"
            if full.get("ok") is True and not full_setup:
                # The recorded failure no longer reproduces: the
                # limitation is cleared (e.g. substrate upgraded).
                checks.append({"name": "limitation_recheck",
                               "outcome": "cleared",
                               "detail": "full-scale reproduction now "
                                         "succeeds; the recorded limitation "
                                         "no longer holds"})
                return QuarantineDiagnosis(
                    capability_id, True, reason, qr["since"], qr["system"],
                    False, "reason_cleared", checks, None)
            if red.get("ok") is True and full.get("ok") is False \
                    and not red_setup and not full_setup:
                # The limitation is specific to the full invocation, not
                # mechanism-wide. Whether that axis is scale or format
                # comes from the static record, not the probe.
                specific_confirmed = True
                checks.append({"name": "separability_probe",
                               "outcome": "specific_confirmed",
                               "detail": "reduced-scale reproduction "
                                         "succeeds; full-scale reproduction "
                                         "fails: the limitation is specific "
                                         "to the full invocation"})
            elif red.get("ok") is False and not red_setup:
                mechanism_wide = True
                checks.append({"name": "separability_probe",
                               "outcome": "mechanism_wide",
                               "detail": "reduced-scale reproduction also "
                                         f"fails ({red_err[:200]}): can't do "
                                         f"at all with the current mechanism"})
            else:
                checks.append({"name": "separability_probe",
                               "outcome": "inconclusive",
                               "detail": "probe inconclusive (setup/timeout); "
                                         "separability from the static "
                                         "record only"})

    # -- route + label ---------------------------------------------------
    # Q10-R1: the separability probe's specific_confirmed verdict feeds
    # the route (it was previously computed but dropped).
    route = _route_limitation(preventing, scale_specific,
                              probe_specific=specific_confirmed)
    if mechanism_wide:
        label = "not-functional"
    elif route == "substrate":
        label = "not-functional"  # cannot run in this environment
    else:
        label = "needs-improvement"  # technique or synthesis can cross it
    checks.append({"name": "limitation_route", "outcome": route,
                   "detail": f"preventing={preventing!r} "
                             f"scale_specific={scale_specific} "
                             f"probe_specific={specific_confirmed} "
                             f"mechanism_wide={mechanism_wide} "
                             f"probe_ran={probe_ran} -> {route}"})
    return QuarantineDiagnosis(
        capability_id, True, reason, qr["since"], qr["system"],
        True, "reason_holds", checks, label)


# --------------------------------------------------------------------------
# repair driver: cleared -> legal lifecycle walk; holds -> honest label
# --------------------------------------------------------------------------

class RepairRefused(Exception):
    """Raised when the repair driver cannot proceed. Fail closed."""


def repair_quarantine(engine, capability_id: str, *,
                      caller=None, reason: str = "",
                      smoke=None) -> Dict[str, Any]:
    """Idempotent quarantine self-repair driver.

    1. diagnose_quarantine (reason API + re-checks);
    2. not quarantined            -> no-op {"action": "none"} (idempotent:
       re-running after a successful repair changes nothing);
    3. reason holds / indeterminate -> stays quarantined with the honest
       not-applicable / needs-improvement / not-functional label, logged;
    4. reason cleared             -> restore_everywhere: the legal
       QUARANTINED -> CANDIDATE -> ... -> DEPLOYED walk with full
       re-verification through the real admission path (nothing skipped).

    caller must hold 'agent:restore' + 'trust:transition' for step 4
    (restore_everywhere's own gate); a missing caller refuses rather than
    weakening the gate.
    """
    diag = diagnose_quarantine(engine, capability_id)
    report: Dict[str, Any] = {"capability_id": capability_id,
                              "diagnosis": diag.as_dict()}
    if diag.verdict == "not_quarantined":
        report["action"] = "none"
        report["status"] = "not_quarantined"
        return report

    if diag.verdict in ("reason_holds", "indeterminate"):
        engine.capabilities.log(
            capability_id, "quarantine_diagnosed",
            json.dumps({"verdict": diag.verdict, "label": diag.label,
                        "reason": diag.reason, "system": diag.system}))
        report["action"] = "none"
        report["status"] = diag.verdict
        report["label"] = diag.label
        return report

    # reason_cleared -> the legal walk. restore_everywhere re-verifies
    # through the real admission path and walks the governed lifecycle
    # chain; it refuses (fail closed) if anything is off.
    if caller is None:
        raise RepairRefused(
            "diagnostic verdict is reason_cleared but no caller was "
            "provided; restore_everywhere requires an authorized caller "
            "('agent:restore' + 'trust:transition') -- refusing rather "
            "than weakening the gate")
    walk_reason = reason or (
        "quarantine self-repair: diagnostic verdict reason_cleared "
        f"(recorded reason no longer holds: {diag.reason!r})")
    engine.capabilities.log(
        capability_id, "quarantine_repair_started",
        json.dumps({"old_reason": diag.reason, "system": diag.system,
                    "reason": walk_reason}))
    try:
        actions = restore_everywhere(engine, capability_id, caller=caller,
                                     reason=walk_reason, smoke=smoke)
    except RestoreRefused as e:
        engine.capabilities.log(capability_id, "quarantine_repair_refused",
                                str(e)[:500])
        report["action"] = "restore_refused"
        report["status"] = "restore_refused"
        report["error"] = str(e)[:500]
        return report
    report["action"] = "restored"
    report["status"] = "restored"
    report["restore"] = actions
    return report


def repair_all_quarantined(engine, *, caller=None,
                           reason: str = "") -> Dict[str, Any]:
    """Run the repair driver over every quarantined capability. Safe to
    run repeatedly: each capability is diagnosed first, and capabilities
    that are no longer quarantined are no-ops."""
    results: Dict[str, Any] = {}
    seen = set()
    for rec in engine.capabilities.list(status="quarantined", limit=10000):
        cid = rec.capability_id
        if cid in seen:
            continue
        seen.add(cid)
        try:
            results[cid] = repair_quarantine(engine, cid, caller=caller,
                                             reason=reason)
        except RepairRefused as e:
            results[cid] = {"action": "refused", "status": "refused",
                            "error": str(e)[:500]}
        except Exception as e:
            results[cid] = {"action": "error", "status": "error",
                            "error": f"{type(e).__name__}: {e}"[:500]}
    return {"count": len(results), "results": results}


# --------------------------------------------------------------------------
# Q4: schedule-ready quarantine sweep driver (unattended execution)
#
# M5's repair_all_quarantined is safe to run repeatedly but not hardened for
# unattended scheduled execution: no runtime bounds, no sweep-level audit
# trail, and a hard crash inside one capability's repair would kill the
# whole sweep process. This section adds:
#
#   run_quarantine_sweep(...)   -- the SCHEDULING INLET (see its docstring
#                                  for the nominated call site; documented,
#                                  not built -- scheduler.py has no periodic
#                                  facility and this mission does not add one)
#   SweepAuditLog               -- persistent per-run audit trail
#                                  (sweep_runs / sweep_actions tables)
#   report_quarantine_repair_evidence -- the single, clearly-marked call
#                                  site where verified repair evidence is
#                                  recorded for M7's registry to consume
#                                  (a closure CANDIDATE -- M7 closes only
#                                  after verified utilization)
#   get_latest_sweep_report / list_sweep_runs -- retrieval for the
#                                  capabilities-view path
#
# Design notes (unattended safety):
#   * Per-capability isolation: each capability is repaired in a FRESH CHILD
#     PROCESS with a hard kill timeout. A hung diagnosis, an unexpected
#     exception, or a hard crash in one capability cannot abort or corrupt
#     the sweep of the others. The child boots its own SwarmEngine against
#     the same DB and repairs as the engine itself (engine.oracle -- the
#     same authorization M5's proof used); no credentials cross the
#     process boundary.
#   * Total runtime bound: no NEW capability starts after the total deadline;
#     unstarted capabilities are recorded as "skipped", never silently
#     dropped. The in-flight capability is additionally capped by the
#     remaining budget, so observed wall time <= total_timeout_s (+epsilon).
#   * No interactive prompts exist anywhere in the diagnose->repair path
#     (verified: no input() in integrity/admission/capability_store/
#     lifecycle/provenance repair paths).
# --------------------------------------------------------------------------

_SWEEP_WORKER_FLAG = "--sweep-worker"
# Test hook ONLY: the child worker sleeps this many seconds (from the parent's
# environment) before repairing, so the per-capability timeout path can be
# exercised causally. Never set in production; the sweep does not depend on it.
_SWEEP_WORKER_TEST_DELAY_ENV = "REMOR_SWEEP_WORKER_DELAY_S"

_SWEEP_SCHEMA = """
CREATE TABLE IF NOT EXISTS sweep_runs (
    run_id       TEXT PRIMARY KEY,
    started_at   REAL NOT NULL,
    finished_at  REAL,
    config_json  TEXT NOT NULL DEFAULT '{}',
    summary_json TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS sweep_actions (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id        TEXT NOT NULL,
    capability_id TEXT NOT NULL,
    verdict       TEXT NOT NULL DEFAULT '',
    action        TEXT NOT NULL DEFAULT '',
    status        TEXT NOT NULL DEFAULT '',
    elapsed_ms    REAL NOT NULL DEFAULT 0,
    error         TEXT NOT NULL DEFAULT '',
    detail_json   TEXT NOT NULL DEFAULT '{}',
    at            REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sweep_actions_run
    ON sweep_actions(run_id);
CREATE TABLE IF NOT EXISTS quarantine_repair_evidence (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id        TEXT NOT NULL,
    capability_id TEXT NOT NULL,
    at            REAL NOT NULL,
    detail_json   TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_repair_evidence_cap
    ON quarantine_repair_evidence(capability_id);
"""


def _resolve_sweep_db_path(engine=None, db_path=None) -> str:
    if db_path:
        return db_path
    caps = getattr(engine, "capabilities", None)
    p = getattr(caps, "db_path", None)
    if p:
        return p
    return os.environ.get("SWARM_ENGINE_DB", "swarm_engine.db")


class SweepAuditLog:
    """Persistent audit trail for quarantine sweeps.

    Every sweep run and every per-capability action/no-op/error/timeout/skip
    is written to the engine DB (sweep_runs / sweep_actions), retrievable in
    a fresh process via get_latest_sweep_report / list_sweep_runs -- the
    capabilities-view path reads the same tables.
    """

    def __init__(self, db_path: str):
        self.db_path = db_path
        with self._conn() as c:
            c.executescript(_SWEEP_SCHEMA)

    def _conn(self):
        c = sqlite3.connect(self.db_path)
        c.execute("PRAGMA journal_mode=WAL")
        return c

    def begin_run(self, run_id: str, config: Dict[str, Any]) -> None:
        with self._conn() as c:
            c.execute(
                "INSERT INTO sweep_runs (run_id, started_at, config_json)"
                " VALUES (?,?,?)",
                (run_id, time.time(), json.dumps(config)))

    def record_action(self, run_id: str, capability_id: str,
                      row: Dict[str, Any]) -> None:
        with self._conn() as c:
            c.execute(
                "INSERT INTO sweep_actions (run_id, capability_id, verdict,"
                " action, status, elapsed_ms, error, detail_json, at)"
                " VALUES (?,?,?,?,?,?,?,?,?)",
                (run_id, capability_id, str(row.get("verdict", "")),
                 str(row.get("action", "")), str(row.get("status", "")),
                 float(row.get("elapsed_ms", 0.0) or 0.0),
                 str(row.get("error", ""))[:2000],
                 json.dumps(row.get("detail") or {}), time.time()))

    def finish_run(self, run_id: str, summary: Dict[str, Any]) -> None:
        with self._conn() as c:
            c.execute(
                "UPDATE sweep_runs SET finished_at=?, summary_json=?"
                " WHERE run_id=?",
                (time.time(), json.dumps(summary), run_id))

    def report_repair_evidence(self, run_id: str, capability_id: str,
                               detail: Optional[Dict[str, Any]] = None
                               ) -> Dict[str, Any]:
        """Record verified quarantine-REPAIR evidence (a closure CANDIDATE).

        Per M7's binding rule a gap closes only after verified UTILIZATION,
        not repair/re-admission alone: restoration evidence is necessary but
        NOT sufficient for registry closure. This row is persistent, audited
        evidence for M7's unified gap registry to consume -- the registry
        performs actual closure after utilization.

        FUTURE M7 INTEGRATION POINT -- when M7's unified gap registry API is
        available, replace the body of this function with the registry's
        evidence-ingest call (e.g. gaps.report_repair_evidence(...)); the
        call site in run_quarantine_sweep stays identical.
        """
        event = {"run_id": run_id, "capability_id": capability_id,
                 "at": time.time(), "detail": detail or {}}
        with self._conn() as c:
            c.execute(
                "INSERT INTO quarantine_repair_evidence (run_id,"
                " capability_id, at, detail_json) VALUES (?,?,?,?)",
                (run_id, capability_id, event["at"],
                 json.dumps(event["detail"])))
        return event

    def latest_report(self) -> Optional[Dict[str, Any]]:
        with self._conn() as c:
            row = c.execute(
                "SELECT run_id, started_at, finished_at, config_json,"
                " summary_json FROM sweep_runs ORDER BY started_at DESC"
                " LIMIT 1").fetchone()
            if not row:
                return None
            actions = c.execute(
                "SELECT capability_id, verdict, action, status, elapsed_ms,"
                " error, at FROM sweep_actions WHERE run_id=? ORDER BY id",
                (row[0],)).fetchall()
        return {
            "run_id": row[0], "started_at": row[1], "finished_at": row[2],
            "config": json.loads(row[3] or "{}"),
            "summary": json.loads(row[4] or "{}"),
            "actions": [
                {"capability_id": a[0], "verdict": a[1], "action": a[2],
                 "status": a[3], "elapsed_ms": a[4], "error": a[5], "at": a[6]}
                for a in actions],
        }

    def list_runs(self, limit: int = 20) -> List[Dict[str, Any]]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT run_id, started_at, finished_at, summary_json"
                " FROM sweep_runs ORDER BY started_at DESC LIMIT ?",
                (limit,)).fetchall()
        return [{"run_id": r[0], "started_at": r[1], "finished_at": r[2],
                 "summary": json.loads(r[3] or "{}")} for r in rows]


def _sweep_child_path_entries() -> List[str]:
    """sys.path entries the child worker needs to import this tree."""
    import swarm_engine as _se
    # Namespace package: no __file__; use __path__.
    pkg_dirs = list(getattr(_se, "__path__", []) or [])
    roots = {os.path.dirname(os.path.abspath(p)) for p in pkg_dirs}
    cands: List[str] = []
    for root in sorted(roots):
        # '<root>/pylib' when imported via the pylib symlink layout,
        # '<root>' itself otherwise (installed / direct runtime layout).
        cands.extend([os.path.join(root, "pylib"), root])
    # Plus the canonical-layout fallback derived from this file's location.
    here_file = os.path.abspath(__file__)  # .../runtime/synthesis/integrity.py
    canon_root = os.path.dirname(os.path.dirname(os.path.dirname(here_file)))
    cands.extend([os.path.join(canon_root, "pylib"), canon_root])
    seen = set()
    out = []
    for c in cands:
        if c not in seen and os.path.isdir(c):
            seen.add(c)
            out.append(c)
    return out


def _sweep_worker_main(argv=None) -> None:
    """Child-process entry point: repair ONE quarantined capability.

    Invoked as: python -c "<bootstrap>" --sweep-worker '<json payload>'.
    Boots a fresh SwarmEngine against the payload's db_path, runs
    repair_quarantine as the engine itself (engine.oracle -- the same
    authorization M5's proof used; no credential crosses the process
    boundary), and prints exactly one JSON line to stdout:
    {"ok": true, "result": {...}} or {"ok": false, "error": "..."}.
    Exit 0 on ok, 2 on worker error.
    """
    args = list(sys.argv[1:] if argv is None else argv)
    try:
        i = args.index(_SWEEP_WORKER_FLAG)
        payload = json.loads(args[i + 1])
        db_path = payload["db_path"]
        capability_id = payload["capability_id"]
    except (ValueError, IndexError, KeyError,
            json.JSONDecodeError) as e:
        print(json.dumps({"ok": False,
                          "error": f"bad worker args: {e}"}), flush=True)
        raise SystemExit(2)
    try:
        delay = float(payload.get("delay_s") or 0)
        if delay > 0:
            # TEST HOOK ONLY (REMOR_SWEEP_WORKER_DELAY_S): sleep so the
            # parent's per-capability timeout path can be exercised.
            time.sleep(delay)
        from swarm_engine.core.engine import SwarmEngine
        eng = SwarmEngine(db_path=db_path)
        # Service-equivalent vocabulary: the production boot path
        # (services/http_adapter boot) registers the media primitive family
        # on every boot; a bare engine does not have it, and both the boot
        # audit and restore's re-verification type-check plan steps against
        # the registry. Mirror the service boot here (CALL the public APIs;
        # never edit wiring.py).
        vocab_warnings = []
        try:
            from swarm_engine.media.wiring import (
                register_image_spec_primitive, register_media_primitives)
            register_media_primitives(eng)
            register_image_spec_primitive(eng)
        except Exception as e:
            vocab_warnings.append(f"{type(e).__name__}: {e}"[:300])
        # Vocabulary-then-audit ordering: the boot audit inside
        # SwarmEngine.__init__ runs BEFORE the service registers the media
        # family, so it derived-quarantines healthy capabilities for ops
        # that are restorable in this same process (false positive).
        # Re-run the audit's own recovery AFTER the vocabulary is complete:
        # audit_all reactivates exactly the derived-only quarantines whose
        # ops are present again; deliberate quarantines stay sticky (never
        # silently reactivated). This is the architecture's own recovery
        # path, not a bypass.
        audit_report = None
        try:
            def _worker_reregister(rec):
                fn = getattr(eng.admission,
                             "_register_capability_as_primitive", None)
                if fn is None:
                    raise RuntimeError(
                        "admission has no _register_capability_as_primitive")
                fn(rec.capability_id, rec)

            audit_report = eng.capabilities.audit_all(
                eng.primitives, register_fn=_worker_reregister)
        except Exception as e:
            vocab_warnings.append(
                f"audit_all: {type(e).__name__}: {e}"[:300])
        result = repair_quarantine(
            eng, capability_id, caller=eng.oracle,
            reason=payload.get("reason", ""))
        if vocab_warnings:
            result["vocabulary_setup_warnings"] = vocab_warnings
        if audit_report is not None:
            result["vocabulary_audit"] = {
                k: audit_report.get(k)
                for k in ("checked", "healthy", "quarantined", "recovered")}
        print(json.dumps({"ok": True, "result": result}), flush=True)
    except Exception as e:
        print(json.dumps(
            {"ok": False,
             "error": f"{type(e).__name__}: {e}"[:2000]}), flush=True)
        raise SystemExit(2)


def _run_capability_subprocess(db_path: str, capability_id: str,
                               reason: str,
                               timeout_s: float) -> Dict[str, Any]:
    """Repair one capability in a fresh child process with a hard timeout."""
    payload = json.dumps({
        "db_path": db_path, "capability_id": capability_id,
        "reason": reason,
        "delay_s": float(os.environ.get(_SWEEP_WORKER_TEST_DELAY_ENV,
                                        "0") or 0),
    })
    entries = _sweep_child_path_entries()
    bootstrap = ("import sys\n" +
                 "".join(f"sys.path.insert(0, {e!r})\n"
                         for e in reversed(entries)) +
                 "from swarm_engine.synthesis.integrity import"
                 " _sweep_worker_main\n"
                 "_sweep_worker_main()\n")
    t_start = time.monotonic()
    try:
        proc = subprocess.Popen(
            [sys.executable, "-c", bootstrap,
             _SWEEP_WORKER_FLAG, payload],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    except Exception as e:
        return {"capability_id": capability_id, "action": "error",
                "status": "worker_spawn_failed",
                "error": f"{type(e).__name__}: {e}"[:500],
                "elapsed_ms": (time.monotonic() - t_start) * 1000.0}
    try:
        out, err = proc.communicate(timeout=timeout_s)
    except subprocess.TimeoutExpired:
        proc.kill()
        _, err = proc.communicate()
        return {"capability_id": capability_id, "action": "error",
                "status": "timed_out",
                "error": ("per-capability timeout "
                          f"({timeout_s:.1f}s) exceeded; worker killed"),
                "elapsed_ms": (time.monotonic() - t_start) * 1000.0,
                "stderr_tail": (err or "")[-500:]}
    elapsed_ms = (time.monotonic() - t_start) * 1000.0
    parsed = None
    for line in reversed((out or "").splitlines()):
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            cand = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(cand, dict) and "ok" in cand:
            parsed = cand
            break
    if parsed is None:
        return {"capability_id": capability_id, "action": "error",
                "status": "worker_crashed",
                "error": ("worker exited without a result envelope "
                          f"(exit={proc.returncode})"),
                "elapsed_ms": elapsed_ms,
                "stderr_tail": (err or "")[-500:],
                "stdout_tail": (out or "")[-500:]}
    if not parsed.get("ok"):
        return {"capability_id": capability_id, "action": "error",
                "status": "worker_error",
                "error": str(parsed.get("error", "unknown"))[:500],
                "elapsed_ms": elapsed_ms}
    result = parsed.get("result") or {}
    result["elapsed_ms"] = elapsed_ms
    return result


def _run_capability_in_process(engine, capability_id: str, reason: str,
                               caller) -> Dict[str, Any]:
    """Repair one capability in-process (no hard timeout possible in this
    mode -- documented; the total bound between capabilities still holds)."""
    t_start = time.monotonic()
    try:
        result = repair_quarantine(engine, capability_id,
                                   caller=caller, reason=reason)
    except Exception as e:
        return {"capability_id": capability_id, "action": "error",
                "status": "error",
                "error": f"{type(e).__name__}: {e}"[:500],
                "elapsed_ms": (time.monotonic() - t_start) * 1000.0}
    result = dict(result)
    result["elapsed_ms"] = (time.monotonic() - t_start) * 1000.0
    return result


def _classify_sweep_result(result: Dict[str, Any]) -> str:
    status = str(result.get("status", ""))
    action = str(result.get("action", ""))
    if status == "timed_out":
        return "timed_out"
    if action == "restored":
        return "restored"
    if status == "reason_holds":
        return "held"
    if status == "indeterminate":
        return "indeterminate"
    if status == "not_quarantined":
        return "no_op"
    if status in ("refused", "restore_refused"):
        return "refused"
    if status == "skipped":
        return "skipped"
    return "errors"


def run_quarantine_sweep(engine=None, db_path=None, *, caller=None,
                         per_capability_timeout_s: float = 600.0,
                         total_timeout_s: float = 3600.0,
                         reason: str = "",
                         worker_mode: str = "subprocess",
                         audit: bool = True) -> Dict[str, Any]:
    """Schedule-ready quarantine sweep: full diagnose->repair cycle, bounded
    and audited, safe for unattended execution.

    SCHEDULING INLET (nominated, not built):
      Function: swarm_engine.synthesis.integrity.run_quarantine_sweep
      Signature: (engine=None, db_path=None, *, caller=None,
                  per_capability_timeout_s=600.0, total_timeout_s=3600.0,
                  reason="", worker_mode="subprocess", audit=True) -> dict
      Call site: the periodic-job facility owned by whoever owns run-control
        / scheduling (Backend Runtime track; scheduler.py's owner).
        scheduler.py is dispatch run-control with NO periodic-job facility
        (verified 2026-09-27) -- building that facility is the scheduling
        owner's work, not this mission's. Until it exists, an OS-level cron
        entry invoking this function (db_path=..., reason="scheduled sweep")
        is the honest interim. Do NOT edit scheduler.py from here.

    Two deployment shapes (both honest; the DB's single-owner rule decides):
      * worker_mode="subprocess" (default): the sweep runs as a dedicated
        process that never boots a parent engine. It lists quarantine
        targets through the capability store directly (a plain SQLite
        read -- no boot audit, so merely listing targets cannot
        derived-quarantine healthy capabilities as a side effect), then
        repairs each capability in a FRESH CHILD PROCESS with a hard kill
        timeout. One capability's hang/crash/exception cannot abort or
        corrupt the sweep of the others, and the single-owner invariant
        holds at every instant (each child is the only engine alive while
        it runs; split-brain is impossible by construction). Passing a
        live engine with this mode is refused (fail closed): the child
        workers each boot their own engine and the DB's single-owner rule
        forbids a second owner while the caller's engine is live. Either
        pass db_path=... or use worker_mode='in_process'.
      * worker_mode="in_process": for invocation inside the live service
        process (the likely scheduling-inlet shape -- a periodic facility
        calling run_quarantine_sweep(engine=live_engine,
        worker_mode="in_process")). Per-capability failures are isolated by
        try/except (a crash-class failure cannot be contained in-process --
        that is what subprocess mode is for). The total bound is enforced
        between capabilities. A hard per-capability kill is NOT possible
        in-process (documented, not pretended): elapsed_ms is recorded per
        row so bound overruns are observable telemetry.

    Behavior (both modes):
      * caller=None -> the engine itself (engine.oracle), the system acting
        on its own behalf -- the same authorization M5's proof used. An
        explicit caller (e.g. an HTTP operator's own credentials) is honored
        when supplied (in_process mode).
      * Each quarantined capability is repaired in a FRESH CHILD PROCESS
        (subprocess mode) with a hard per-capability kill timeout, or
        in-process with per-capability error isolation (in_process mode).
      * Total bound: no new capability starts after total_timeout_s; the
        in-flight capability is additionally capped by the remaining budget,
        so observed wall time <= total_timeout_s (+epsilon). Unstarted
        capabilities are recorded as "skipped", never silently dropped.
      * Every action, no-op, error, timeout, and skip is written to the
        sweep audit tables (retrievable via get_latest_sweep_report /
        list_sweep_runs, which the capabilities-view path can read).
      * A capability restored by verified re-admission fires
        report_quarantine_repair_evidence -- the single call site that
        records a closure CANDIDATE for M7's registry (M7 closes only
        after verified utilization).
    """
    if worker_mode not in ("subprocess", "in_process"):
        raise ValueError(f"worker_mode must be 'subprocess' or 'in_process',"
                         f" got {worker_mode!r}")
    if per_capability_timeout_s <= 0 or total_timeout_s <= 0:
        raise ValueError("timeouts must be positive")
    db_path = _resolve_sweep_db_path(engine, db_path)
    if engine is None and worker_mode == "subprocess":
        # No engine boot in the parent: the target list is a plain store
        # read, and booting a full engine here would run the boot audit
        # against a not-yet-registered vocabulary (the service registers
        # its media family post-boot), derived-quarantining healthy
        # capabilities as a side effect of merely LISTING targets
        # (observed 2026-09-27: sweep 2 re-targeted sweep 1's restored
        # capabilities). CapabilityStore claims no DB ownership, so the
        # single-owner invariant is trivially preserved; each child
        # worker boots (and closes) the only engine.
        from swarm_engine.synthesis.capability_store import CapabilityStore
        list_store = CapabilityStore(db_path)
        owned_engine = False
    elif engine is None:
        from swarm_engine.core.engine import SwarmEngine
        engine = SwarmEngine(db_path=db_path)
        list_store = engine.capabilities
        owned_engine = True
    else:
        list_store = engine.capabilities
        owned_engine = False
        if worker_mode == "subprocess":
            raise ValueError(
                "run_quarantine_sweep: worker_mode='subprocess' cannot run"
                " against a caller-supplied live engine -- the child"
                " workers each boot their own engine and the DB's"
                " single-owner rule forbids a second owner while the"
                " caller's engine is live. Either pass db_path=... (the"
                " sweep then never boots a parent engine: it lists"
                " targets via the capability store and spawns workers)"
                " or use worker_mode='in_process'.")
    if caller is None and engine is not None:
        caller = engine.oracle  # the system itself, on a schedule
    # NOTE: in subprocess mode without a caller-supplied engine the parent
    # needs no caller at all: each child boots its own engine and repairs
    # as that engine's oracle (the same authorization M5's proof used; no
    # credential crosses the process boundary).

    run_id = uuid.uuid4().hex
    t0 = time.monotonic()
    deadline = t0 + total_timeout_s
    config = {"per_capability_timeout_s": per_capability_timeout_s,
              "total_timeout_s": total_timeout_s,
              "worker_mode": worker_mode,
              "reason": reason,
              "db_path": db_path}
    audit_log = SweepAuditLog(db_path) if audit else None
    if audit_log is not None:
        audit_log.begin_run(run_id, config)

    seen = set()
    targets: List[str] = []
    for rec in list_store.list(status="quarantined", limit=10000):
        cid = rec.capability_id
        if cid not in seen:
            seen.add(cid)
            targets.append(cid)

    summary: Dict[str, int] = {"swept": 0, "restored": 0, "held": 0,
                               "indeterminate": 0, "no_op": 0, "refused": 0,
                               "errors": 0, "timed_out": 0, "skipped": 0}
    rows: List[Dict[str, Any]] = []
    for cid in targets:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            row = {"capability_id": cid, "verdict": "", "action": "none",
                   "status": "skipped", "elapsed_ms": 0.0,
                   "error": "total time bound exceeded before start"}
            summary["skipped"] += 1
            rows.append(row)
            if audit_log is not None:
                audit_log.record_action(run_id, cid, row)
            continue
        cap_timeout = min(per_capability_timeout_s, remaining)
        if worker_mode == "subprocess":
            result = _run_capability_subprocess(db_path, cid, reason,
                                                cap_timeout)
        else:
            result = _run_capability_in_process(engine, cid, reason, caller)
            bound_ms = per_capability_timeout_s * 1000.0
            if float(result.get("elapsed_ms", 0.0) or 0.0) > bound_ms:
                # No hard kill is possible in-process; record the overrun
                # honestly instead of pretending the bound was enforced.
                result["per_capability_bound_exceeded"] = True
        bucket = _classify_sweep_result(result)
        summary["swept"] += 1
        summary[bucket] += 1
        diag = result.get("diagnosis") or {}
        row = {"capability_id": cid,
               "verdict": str(diag.get("verdict", "")),
               "action": str(result.get("action", "")),
               "status": str(result.get("status", "")),
               "elapsed_ms": float(result.get("elapsed_ms", 0.0) or 0.0),
               "error": str(result.get("error", ""))[:500],
               "detail": result}
        rows.append(row)
        if audit_log is not None:
            audit_log.record_action(run_id, cid, row)
            if bucket == "restored":
                # THE M7 call site: verified quarantine-repair evidence.
                # Per M7's binding rule a gap closes only after verified
                # UTILIZATION -- this row is a closure CANDIDATE for the
                # registry, not a closure claim. Single, obvious,
                # documented in SweepAuditLog.report_repair_evidence.
                audit_log.report_repair_evidence(
                    run_id, cid,
                    {"verdict": row["verdict"], "status": row["status"],
                     "elapsed_ms": row["elapsed_ms"]})

    report = {"run_id": run_id,
              "started_at": time.time() - (time.monotonic() - t0),
              "finished_at": time.time(),
              "elapsed_s": time.monotonic() - t0,
              "config": config,
              "targets": targets,
              "summary": summary,
              "capabilities": rows}
    if audit_log is not None:
        audit_log.finish_run(run_id, summary)
    if owned_engine and engine is not None:
        # The sweep booted this engine itself (in_process, no caller
        # engine): close it so a later sweep in this process does not hit
        # the DB's single-owner rule against our leftover claim.
        try:
            engine.close()
        except Exception:
            pass
    return report


def report_quarantine_repair_evidence(run_id: str, capability_id: str,
                                       detail: Optional[Dict[str, Any]] = None,
                                       *, engine=None,
                                       db_path: Optional[str] = None
                                       ) -> Dict[str, Any]:
    """Record verified quarantine-repair evidence (a closure CANDIDATE).

    This is the single, clearly-marked call site Felix/M7 asked to keep
    clean: when M7's unified gap registry API exists, the body switches to
    the registry's evidence-ingest call; the call site in
    run_quarantine_sweep does not move. Per M7's binding rule a gap closes
    only after verified utilization -- repair evidence alone does not close
    it. Today the function writes a persistent, audited evidence row (no
    tamper-evidence is claimed; the row is plain SQLite audit evidence).
    """
    db_path = _resolve_sweep_db_path(engine, db_path)
    return SweepAuditLog(db_path).report_repair_evidence(run_id,
                                                         capability_id,
                                                         detail)


def get_latest_sweep_report(engine=None,
                            db_path: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """Latest sweep run + per-capability actions, readable with only a
    db_path (fresh process; no engine boot) -- this is what the
    capabilities-view path reads. The http_adapter landing (Felix,
    coordinated) adds this to GET /api/capabilities."""
    db_path = _resolve_sweep_db_path(engine, db_path)
    return SweepAuditLog(db_path).latest_report()


def list_sweep_runs(engine=None, db_path: Optional[str] = None,
                    limit: int = 20) -> List[Dict[str, Any]]:
    """Sweep run history (summaries), newest first -- evidence retrieval."""
    db_path = _resolve_sweep_db_path(engine, db_path)
    return SweepAuditLog(db_path).list_runs(limit=limit)

def seal_epistemic_chain(engine, report: Dict[str, Any]) -> Dict[str, Any]:
    """After ``run_competition`` admits a program, seal every artifact
    and link the full lineage::

        goal -> question -> hypotheses -> experiments -> evidence
        winner hypothesis -> capability (admission)

    Returns the sealed object inventory. Tampering with any artifact
    afterwards breaks its seal; ``lineage_of`` walks the whole chain.
    """
    integ = IntegrityStore(engine.capabilities.db_path)
    store = engine.intellect.epistemic
    qid = report["question_id"]
    goal = report.get("goal", "")
    sealed: Dict[str, List[str]] = {"hypotheses": [], "experiments": [],
                                    "evidence": []}
    integ.link("goal", goal, "question", qid, kind="posed")
    for hid, v in report.get("verdicts", {}).items():
        hyp = store.get_hypothesis(hid)
        if hyp is None:
            continue
        integ.seal_object("hypothesis", hid, hyp.as_dict())
        sealed["hypotheses"].append(hid)
        integ.link("question", qid, "hypothesis", hid, kind="competes")
    for exp in store.experiments_for(qid):
        integ.seal_object("experiment", exp.experiment_id, exp.as_dict())
        sealed["experiments"].append(exp.experiment_id)
        for hid in exp.hypothesis_ids:
            integ.link("hypothesis", hid, "experiment", exp.experiment_id,
                       kind="tested_by")
    # evidence -> link via content.experiment_id
    seen_ev = set()
    for hid in report.get("verdicts", {}):
        for ev in store.evidence_for(hid):
            if ev.evidence_id in seen_ev:
                continue
            seen_ev.add(ev.evidence_id)
            integ.seal_object("evidence", ev.evidence_id, ev.as_dict())
            sealed["evidence"].append(ev.evidence_id)
            exp_id = (ev.content or {}).get("experiment_id")
            if exp_id:
                integ.link("experiment", exp_id, "evidence",
                           ev.evidence_id, kind="produced")
    cap_id = report.get("admitted_capability_id")
    if cap_id:
        winner = next((hid for hid, v in report["verdicts"].items()
                       if v.get("label") == "winner"
                       and v.get("state") == "supported"), None)
        if winner:
            integ.link("hypothesis", winner, "capability", cap_id,
                       kind="admitted_as")
        cap = engine.capabilities.get(cap_id)
        if cap is not None:
            seal_capability(engine, cap)
            sealed["capability"] = cap_id
    return sealed
