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
import sqlite3
import time
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
# epistemic chain: seal the behavioral loop's artifacts + link the lineage
# --------------------------------------------------------------------------

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
