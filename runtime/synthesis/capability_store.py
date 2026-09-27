"""
swarm_engine/synthesis/capability_store.py

Persistence, rehydration, versioning and rollback for plan-based capabilities.

The old KnowledgeBase stored capabilities as `code` strings, which meant a
reloaded capability had to be exec'd to find out what it did. Plans are data,
so this store keeps the plan itself as the canonical artefact and treats the
`code` column as a human-readable rendering only. That makes three things
possible that weren't before:

  * rehydration without exec — a stored capability is loaded, re-checked
    against the *current* registry, and only then made runnable
  * dependency integrity — a plan records exactly which primitives it needs, so
    a capability that references a primitive that has since been removed fails
    loudly at load instead of silently at 3am
  * versioning and rollback — revisions are chained, and a bad revision can be
    reverted to the last known-good one

Schema is additive: the existing `capabilities` table is left untouched.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from swarm_engine.primitives.core import Effect, PrimitiveRegistry
from swarm_engine.representation.json_codec import (
    canonical_json_dumps as _cj, from_tagged as _from_tagged)


def plan_fingerprint(plan: Dict[str, Any]) -> str:
    """Stable id derived from plan structure, ignoring cosmetic fields."""
    from swarm_engine.representation.json_codec import canonical_json_dumps
    trimmed = {k: v for k, v in plan.items() if k not in ("name", "rationale", "defaults")}
    blob = canonical_json_dumps(trimmed)
    return "cap_" + hashlib.sha256(blob.encode()).hexdigest()[:20]


def stable_code_id(prefix: str, name: str, code: str) -> str:
    """Deterministic capability id for acquired source code.

    2026-09-19 (R9): this was f"{prefix}_{name}_{abs(hash(code)) % 10**10}",
    but Python's hash() is randomized per process (PYTHONHASHSEED), so the
    "same" capability got a different id in every process -- orphaning its
    provenance/lifecycle/event rows on every reacquire and making
    revoke-then-reacquire identity untestable. sha256 of (name, code) is
    stable across processes for identical content, like plan_fingerprint.
    """
    digest = hashlib.sha256(f"{name}\x00{code}".encode()).hexdigest()
    return f"{prefix}_{name}_{int(digest[:12], 16) % 10**10:010d}"


def render_plan(plan: Dict[str, Any]) -> str:
    """Readable rendering of a plan. Never executed — this exists so a human
    (or the generation log) can see what was admitted."""
    lines = [f"# capability: {plan.get('name', 'unnamed')}"]
    params = plan.get("params") or {}
    if params:
        lines.append("# params: " + ", ".join(f"{k}: {v}" for k, v in params.items()))

    def ref(v, indent=""):
        if isinstance(v, dict):
            if "$param" in v:
                return f"param.{v['$param']}"
            if "$step" in v:
                return f"<{v['$step']}>"
            if "$var" in v:
                return v["$var"]
            if "$lambda" in v:
                lam = v["$lambda"]
                inner = "; ".join(
                    f"{s.get('id')} = {s.get('op', s.get('control'))}" for s in lam.get("steps", [])
                )
                return f"λ({', '.join(lam.get('params', []))}) {{ {inner} }}"
            if "$list" in v:
                return "[" + ", ".join(ref(x) for x in v["$list"]) + "]"
            if "$dict" in v:
                return "{" + ", ".join(f"{k}: {ref(x)}" for k, x in v["$dict"].items()) + "}"
        return repr(v)

    def walk(steps, depth=0):
        pad = "  " * depth
        for s in steps or []:
            ctl = s.get("control")
            if ctl:
                lines.append(f"{pad}{s.get('id')}: [{ctl}]")
                for key in ("then", "else", "body", "catch"):
                    if isinstance(s.get(key), list):
                        lines.append(f"{pad}  .{key}:")
                        walk(s[key], depth + 2)
                if isinstance(s.get("branches"), dict):
                    for bname, bsteps in s["branches"].items():
                        lines.append(f"{pad}  .{bname}:")
                        walk(bsteps, depth + 2)
                continue
            args = ", ".join(f"{k}={ref(v)}" for k, v in (s.get("args") or {}).items())
            lines.append(f"{pad}{s.get('id')} = {s.get('op')}({args})")

    walk(plan.get("steps"))
    out = plan.get("output")
    if out is not None:
        lines.append("return " + ref(out))
    return "\n".join(lines)


@dataclass
class CapabilityRecord:
    capability_id: str
    name: str
    goal: str
    plan: Dict[str, Any]
    ops: List[str]
    effects: List[str]
    version: int = 1
    parent_id: Optional[str] = None
    status: str = "active"          # active | superseded | quarantined
    created_at: float = field(default_factory=time.time)
    use_count: int = 0
    success_count: int = 0
    fail_count: int = 0

    @property
    def success_rate(self) -> float:
        total = self.success_count + self.fail_count
        return self.success_count / total if total else 0.0

    def as_dict(self) -> Dict[str, Any]:
        return {
            "capability_id": self.capability_id, "name": self.name,
            "goal": self.goal, "plan": self.plan, "ops": self.ops,
            "effects": self.effects, "version": self.version,
            "parent_id": self.parent_id, "status": self.status,
            "created_at": self.created_at, "use_count": self.use_count,
            "success_count": self.success_count, "fail_count": self.fail_count,
            "success_rate": round(self.success_rate, 3),
        }


class CapabilityStore:
    """Plan-native capability persistence, layered on the same sqlite file the
    KnowledgeBase uses so there is one database, not two."""

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
                CREATE TABLE IF NOT EXISTS plan_capabilities (
                    capability_id  TEXT PRIMARY KEY,
                    name           TEXT NOT NULL,
                    goal           TEXT NOT NULL,
                    plan_json      TEXT NOT NULL,
                    ops_json       TEXT NOT NULL,
                    effects_json   TEXT NOT NULL,
                    rendered       TEXT NOT NULL,
                    version        INTEGER DEFAULT 1,
                    parent_id      TEXT,
                    status         TEXT DEFAULT 'active',
                    created_at     REAL NOT NULL,
                    use_count      INTEGER DEFAULT 0,
                    success_count  INTEGER DEFAULT 0,
                    fail_count     INTEGER DEFAULT 0
                )
            """)
            c.execute("""
                CREATE TABLE IF NOT EXISTS capability_goals (
                    goal_key      TEXT PRIMARY KEY,
                    capability_id TEXT NOT NULL,
                    bound_at      REAL NOT NULL
                )
            """)
            c.execute("""
                CREATE TABLE IF NOT EXISTS capability_events (
                    id            INTEGER PRIMARY KEY AUTOINCREMENT,
                    capability_id TEXT,
                    event         TEXT,
                    detail        TEXT,
                    at            REAL
                )
            """)
            c.execute("CREATE INDEX IF NOT EXISTS idx_pc_status ON plan_capabilities(status)")
            c.execute("CREATE INDEX IF NOT EXISTS idx_pc_parent ON plan_capabilities(parent_id)")

    # -- writes -------------------------------------------------------------
    def store(self, record: CapabilityRecord) -> CapabilityRecord:
        # Admission -> execution binding (2026-09-27, Worker 2): the
        # capability_id IS the plan fingerprint
        # (plan_fingerprint(plan)). A record whose plan does not
        # fingerprint to its id is either a substitution (same id,
        # different bytes -- the INSERT OR REPLACE this method performs
        # would silently promote unadmitted bytes to an admitted
        # identity) or an unadmitted identity outright. Either way it
        # must never enter the store. Fail closed: refuse the write.
        # All legitimate writers (AdmissionController.admit stage 5,
        # CapabilityStore.revise) derive the id from the plan, so this
        # changes nothing for them.
        try:
            fp = plan_fingerprint(record.plan)
        except Exception as exc:
            self.log(record.capability_id, "store_refused",
                     "plan unreadable, identity binding cannot be verified: "
                     f"{exc!r}")
            raise ValueError(
                f"store refused: plan for {record.capability_id!r} is "
                "unreadable; identity binding cannot be verified -- "
                "fail closed")
        if fp != record.capability_id:
            self.log(record.capability_id, "store_refused",
                     f"plan fingerprint {fp[:24]}... != capability_id; "
                     "substitution of unadmitted bytes under an admitted "
                     "identity refused")
            raise ValueError(
                f"store refused: plan fingerprint {fp[:24]}... does not "
                f"match capability_id {record.capability_id!r}; refusing "
                "substitution of unadmitted bytes under an admitted identity")
        with self._conn() as c:
            c.execute("""
                INSERT OR REPLACE INTO plan_capabilities
                (capability_id, name, goal, plan_json, ops_json, effects_json,
                 rendered, version, parent_id, status, created_at,
                 use_count, success_count, fail_count)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """, (record.capability_id, record.name, record.goal,
                  _cj(record.plan), _cj(record.ops),
                  _cj(record.effects), render_plan(record.plan),
                  record.version, record.parent_id, record.status,
                  record.created_at, record.use_count,
                  record.success_count, record.fail_count))
        self.log(record.capability_id, "stored", f"v{record.version} {record.name}")
        return record

    def bind_goal(self, goal_key: str, capability_id: str) -> None:
        """Remember that this goal resolves to this capability, so the same
        request never re-synthesizes."""
        with self._conn() as c:
            c.execute("INSERT OR REPLACE INTO capability_goals VALUES (?,?,?)",
                      (goal_key.strip().lower(), capability_id, time.time()))

    def unbind_goal_if(self, goal_key: str, capability_id: str) -> bool:
        """Remove an exact-goal winner only if it is the stale record itself."""
        normalized = goal_key.strip().lower()
        with self._conn() as c:
            row = c.execute("SELECT capability_id FROM capability_goals WHERE goal_key=?",
                            (normalized,)).fetchone()
            if not row or row[0] != capability_id:
                return False
            c.execute("DELETE FROM capability_goals WHERE goal_key=?", (normalized,))
        self.log(capability_id, "goal_unbound", normalized)
        return True

    def goal_bindings(self) -> Dict[str, str]:
        """Current exact-goal bindings: normalized goal key -> capability id."""
        with self._conn() as c:
            rows = c.execute(
                "SELECT goal_key, capability_id FROM capability_goals").fetchall()
        return {r[0]: r[1] for r in rows}

    def delete_capability(self, capability_id: str) -> bool:
        """Hard-delete a capability record and its goal bindings.

        Used by acquisition rollback: a graph that fails to resolve must
        not leave a partially acquired capability stored. Historical
        evidence (capability_events, provenance) is deliberately kept —
        rollback removes the capability, not the record that it was tried.
        Returns True if a record was deleted.
        """
        with self._conn() as c:
            cur = c.execute("DELETE FROM plan_capabilities WHERE capability_id=?",
                            (capability_id,))
            removed = cur.rowcount > 0
            c.execute("DELETE FROM capability_goals WHERE capability_id=?",
                      (capability_id,))
        if removed:
            self.log(capability_id, "deleted",
                     "acquisition rollback: graph did not resolve")
        return removed

    def record_use(self, capability_id: str, success: bool) -> None:
        col = "success_count" if success else "fail_count"
        with self._conn() as c:
            c.execute(f"UPDATE plan_capabilities SET use_count = use_count + 1, "
                      f"{col} = {col} + 1 WHERE capability_id = ?", (capability_id,))

    def set_status(self, capability_id: str, status: str) -> None:
        with self._conn() as c:
            c.execute("UPDATE plan_capabilities SET status=? WHERE capability_id=?",
                      (status, capability_id))
        self.log(capability_id, "status", status)

    def log(self, capability_id: str, event: str, detail: str = "") -> None:
        with self._conn() as c:
            c.execute("INSERT INTO capability_events (capability_id, event, detail, at) "
                      "VALUES (?,?,?,?)", (capability_id, event, detail, time.time()))

    # -- reads --------------------------------------------------------------
    def _row_to_record(self, row) -> CapabilityRecord:
        return CapabilityRecord(
            capability_id=row[0], name=row[1], goal=row[2],
            plan=_from_tagged(json.loads(row[3])), ops=json.loads(row[4]),
            effects=json.loads(row[5]), version=row[7], parent_id=row[8],
            status=row[9], created_at=row[10], use_count=row[11],
            success_count=row[12], fail_count=row[13],
        )

    _COLS = ("capability_id, name, goal, plan_json, ops_json, effects_json, "
             "rendered, version, parent_id, status, created_at, use_count, "
             "success_count, fail_count")

    def get(self, capability_id: str) -> Optional[CapabilityRecord]:
        with self._conn() as c:
            row = c.execute(f"SELECT {self._COLS} FROM plan_capabilities "
                            "WHERE capability_id=?", (capability_id,)).fetchone()
        return self._row_to_record(row) if row else None

    def resolve_goal(self, goal_key: str) -> Optional[CapabilityRecord]:
        with self._conn() as c:
            row = c.execute("SELECT capability_id FROM capability_goals WHERE goal_key=?",
                            (goal_key.strip().lower(),)).fetchone()
        if not row:
            return None
        rec = self.get(row[0])
        if rec and rec.status == "active":
            return rec
        return None


    def find_compatible(self, goal: str,
                        examples: Optional[List[Tuple[Dict[str, Any], Any]]] = None,
                        payload: Optional[Dict[str, Any]] = None,
                        composer=None,
                        registry=None,
                        semantic_store=None,
                        min_score: float = 0.55,
                        limit: int = 20) -> List[CapabilityRecord]:
        """Semantic/structural capability match beyond exact goal_key binding.

        Returns active records ordered by compatibility score. Empty if none
        clear the threshold. Exact resolve_goal remains the fast path when
        no examples (or no composer) are supplied; when the caller supplies
        worked examples and a composer, the exact-bound plan must still
        reproduce them behaviorally, otherwise matching falls through to
        the behavioral ranking below.

        Besides plan-native capabilities, behaviorally considers
        `acquired_code` entries (code-acquired capabilities such as
        recursively-acquired decomposition children): each is wrapped as a
        single-step calling plan and scored by executing it on the query
        examples, exactly like a stored plan. A match on such an entry is
        returned as its behaviorally-tested wrapper (marked
        via="acquired_code"), since it has no plan_capabilities row.
        """
        from swarm_engine.synthesis.capability_match import (
            rank_compatible, acquired_code_candidates, behavioral_score)
        exact = self.resolve_goal(goal)
        if exact is not None:
            if examples and composer is not None:
                # An exact goal binding is not a verification bypass: the
                # admitted plan must still reproduce the caller's examples.
                # If it does not, fall through to behavioral ranking
                # instead of trusting the binding.
                try:
                    exact_score, _ = behavioral_score(
                        exact, examples, composer)
                except Exception:
                    exact_score = 0.0
                if exact_score >= 1.0:
                    return [exact]
            else:
                return [exact]
        candidates = self.list(status="active", limit=200)
        acq = acquired_code_candidates(self._acquired_entries(),
                                       registry=registry)
        ranked = rank_compatible(
            candidates, goal, examples=examples, payload=payload,
            composer=composer, registry=registry, semantic_store=semantic_store,
            min_score=min_score, acquired_candidates=acq)
        by_acq = {c.capability_id: c for c in acq}
        out: List[CapabilityRecord] = []
        for m in ranked[:limit]:
            rec = self.get(m.capability_id)
            if rec is not None and rec.status == "active":
                out.append(rec)
                continue
            cand = by_acq.get(m.capability_id)
            if cand is not None:
                out.append(cand)
        return out

    def _acquired_entries(self) -> List[Dict[str, Any]]:
        """Raw `acquired_code` rows from the shared DB file.

        The acquired_code table is owned by AcquiredCodeStore but lives in
        this same sqlite file; reading it here (guarded) is what lets
        behavioral discovery see code-acquired capabilities without new
        wiring. A missing table simply yields no entries.
        """
        try:
            with self._conn() as c:
                rows = c.execute(
                    "SELECT name, capability_id, spec FROM acquired_code"
                ).fetchall()
        except sqlite3.Error:
            return []
        out: List[Dict[str, Any]] = []
        for name, cap_id, spec_json in rows:
            try:
                spec = json.loads(spec_json or "{}")
            except Exception:
                spec = {}
            out.append({"name": name, "capability_id": cap_id,
                        "spec": spec if isinstance(spec, dict) else {}})
        return out

    def list(self, status: str = "active", limit: int = 200) -> List[CapabilityRecord]:
        with self._conn() as c:
            rows = c.execute(f"SELECT {self._COLS} FROM plan_capabilities "
                             "WHERE status=? ORDER BY created_at DESC LIMIT ?",
                             (status, limit)).fetchall()
        return [self._row_to_record(r) for r in rows]

    def history(self, capability_id: str) -> List[CapabilityRecord]:
        """Full revision chain, oldest first."""
        chain: List[CapabilityRecord] = []
        seen = set()
        cur = self.get(capability_id)
        while cur and cur.capability_id not in seen:
            seen.add(cur.capability_id)
            chain.append(cur)
            cur = self.get(cur.parent_id) if cur.parent_id else None
        return list(reversed(chain))

    def events(self, capability_id: str, limit: int = 50) -> List[Dict[str, Any]]:
        with self._conn() as c:
            rows = c.execute("SELECT event, detail, at FROM capability_events "
                             "WHERE capability_id=? ORDER BY id DESC LIMIT ?",
                             (capability_id, limit)).fetchall()
        return [{"event": e, "detail": d, "at": t} for e, d, t in rows]

    # -- integrity ----------------------------------------------------------
    def rehydrate(self, capability_id: str, registry: PrimitiveRegistry
                  ) -> Tuple[Optional[CapabilityRecord], List[str]]:
        """Load a capability and verify every primitive it depends on still
        exists in the current registry. Returns (record, problems).

        Also verifies the admission -> execution binding: the stored plan
        must fingerprint to its own id. A swapped row (same id, different
        bytes) is quarantined here -- never returned for execution --
        matching the nl_dispatch "capability_tampered" refusal and the
        corruption handling of the admission reuse guard. The
        "corruption_detected" event keeps deliberate-quarantine
        stickiness, so the tampered identity cannot be silently
        resurrected; recovery goes through the governed
        integrity.restore_everywhere path.
        """
        rec = self.get(capability_id)
        if rec is None:
            return None, [f"capability {capability_id!r} not found"]
        try:
            fp = plan_fingerprint(rec.plan)
        except Exception:
            fp = None
        if fp != capability_id:
            self.set_status(capability_id, "quarantined")
            self.log(capability_id, "corruption_detected",
                     "rehydrate: stored plan does not fingerprint to its id "
                     f"(computed {(fp[:24] + '...') if fp else 'unreadable'}); "
                     "quarantined -- refusing execution of unadmitted bytes")
            return None, [
                f"capability_tampered: stored plan for {capability_id!r} "
                "does not fingerprint to its id; unadmitted bytes refused"]
        missing = [op for op in rec.ops if op not in registry]
        if missing:
            self.set_status(capability_id, "quarantined")
            return rec, [f"missing primitive: {m}" for m in missing]
        return rec, []

    # Events that mark a quarantine as a DELIBERATE revocation decision
    # (as opposed to derived dependency/integrity state). A deliberately
    # revoked capability must NOT be silently reactivated by the recovery
    # loop -- revocation has to survive a reboot -- it can only come back
    # through an explicit restore (re-acquisition, vindication, or the
    # restore path that inverts the revocation).
    _DELIBERATE_QUARANTINE_EVENTS = frozenset({
        "quarantined_everywhere", "epistemic_revocation",
        "heldout_rejected", "rolled_back", "corruption_detected",
    })
    # Quarantine events that record DERIVED state (eligible for automatic
    # recovery once dependencies are present again).
    _DERIVED_QUARANTINE_EVENTS = frozenset({
        "dependency_revocation", "dependency_quarantined",
    })

    def _quarantine_was_deliberate(self, capability_id: str) -> bool:
        """Was this capability's quarantine a deliberate revocation decision?

        Consults the machinery's own event log: the most recent
        quarantine-causing event decides. Derived quarantines (dependency
        propagation, audit's own missing-op quarantine) return False, so
        the recovery loop keeps working for them exactly as before.
        """
        try:
            for ev in self.events(capability_id, limit=60):
                e = ev.get("event")
                if e in self._DELIBERATE_QUARANTINE_EVENTS:
                    return True
                if e in self._DERIVED_QUARANTINE_EVENTS:
                    return False
                if e == "status" and (ev.get("detail") or "") == "quarantined":
                    return False
        except Exception:
            pass
        return False

    def audit_all(self, registry: PrimitiveRegistry,
                  register_fn=None) -> Dict[str, Any]:
        """Check every stored capability against the live registry. Run this on
        boot: it's how a removed primitive gets caught before a task uses it.

        Also recovers quarantined capabilities whose dependencies are present
        again (generic dependency lifecycle: revoke → quarantine → restore
        deps → reactivate). Recovery is a fixpoint over the persisted plan
        graph, is reason-aware (deliberate revocations are sticky -- they
        survive reboot and are never silently reactivated), and re-registers
        each recovered capability's primitive via register_fn so a
        reactivated capability is actually executable in this process, not
        merely marked active.
        """
        ok, broken, recovered = [], {}, []
        for rec in self.list(status="active", limit=10_000):
            missing = [op for op in rec.ops if op not in registry]
            if missing:
                broken[rec.capability_id] = missing
                self.set_status(rec.capability_id, "quarantined")
            else:
                ok.append(rec.capability_id)
        # Recovery path: fixpoint over quarantined records whose quarantine
        # was derived (not deliberate) and whose ops are all present again.
        while True:
            progressed = False
            for rec in self.list(status="quarantined", limit=10_000):
                if self._quarantine_was_deliberate(rec.capability_id):
                    continue
                missing = [op for op in rec.ops if op not in registry]
                if missing:
                    continue
                if register_fn is not None:
                    try:
                        register_fn(rec)
                    except Exception as exc:
                        self.log(rec.capability_id, "recovery_failed",
                                 f"primitive re-registration failed: {exc!r} "
                                 "-- recovery refused, fail closed")
                        continue
                self.set_status(rec.capability_id, "active")
                self.log(rec.capability_id, "dependency_recovered",
                         "all dependency ops present again; reactivated")
                recovered.append(rec.capability_id)
                ok.append(rec.capability_id)
                progressed = True
            if not progressed:
                break
        return {"checked": len(ok) + len(broken), "healthy": len(ok),
                "quarantined": len(broken), "broken": broken,
                "recovered": recovered}

    # -- versioning ---------------------------------------------------------
    def revise(self, parent: CapabilityRecord, new_plan: Dict[str, Any],
               ops: List[str], effects: List[str], note: str = "") -> CapabilityRecord:
        """Create a successor revision. The parent is marked superseded but is
        kept, so rollback is always possible."""
        rec = CapabilityRecord(
            capability_id=plan_fingerprint(new_plan),
            name=parent.name, goal=parent.goal, plan=new_plan,
            ops=ops, effects=effects,
            version=parent.version + 1, parent_id=parent.capability_id,
        )
        if rec.capability_id == parent.capability_id:
            return parent  # identical plan, nothing to revise
        # Quarantine stickiness: plan_fingerprint is a content hash, so a
        # "revision" whose plan is byte-identical to a DELIBERATELY
        # quarantined capability's plan lands on that capability's id --
        # and store() is INSERT OR REPLACE, which would silently flip the
        # quarantined row back to active. That is the same silent
        # resurrection the admission 0c guard refuses; refuse it here too.
        existing = self.get(rec.capability_id)
        if (existing is not None and existing.status == "quarantined"
                and self._quarantine_was_deliberate(rec.capability_id)):
            self.log(rec.capability_id, "resurrection_refused",
                     "revise refused: the new plan fingerprints to a "
                     "deliberately quarantined capability; deliberate "
                     "revocations are sticky -- restore via "
                     "integrity.restore_everywhere with trust:transition "
                     "authority")
            raise ValueError(
                f"revise refused: plan fingerprints to deliberately "
                f"quarantined capability {rec.capability_id[:16]}...; "
                "deliberate revocations are sticky")
        self.store(rec)
        self.set_status(parent.capability_id, "superseded")
        self.bind_goal(parent.goal, rec.capability_id)
        self.log(rec.capability_id, "revised",
                 note or f"revision of {parent.capability_id} (v{parent.version})")
        return rec

    def rollback(self, capability_id: str) -> Optional[CapabilityRecord]:
        """Revert to the parent revision and quarantine the bad one."""
        rec = self.get(capability_id)
        if not rec or not rec.parent_id:
            return None
        parent = self.get(rec.parent_id)
        if not parent:
            return None
        # Quarantine stickiness: a rollback must not silently resurrect a
        # parent that was DELIBERATELY quarantined (e.g. via
        # integrity.quarantine_everywhere). Activating it here would flip
        # a sticky revocation back to active with no authority and no
        # audit -- the same silent-resurrection the admission 0c guard
        # refuses. Fail closed: refuse the rollback, log it, change
        # nothing.
        if self._quarantine_was_deliberate(parent.capability_id):
            self.log(parent.capability_id, "rollback_refused",
                     f"rollback of {capability_id} refused: parent revision "
                     "is deliberately quarantined; deliberate revocations "
                     "are sticky -- restore via "
                     "integrity.restore_everywhere with trust:transition "
                     "authority")
            return None
        self.set_status(capability_id, "quarantined")
        self.set_status(parent.capability_id, "active")
        self.bind_goal(parent.goal, parent.capability_id)
        self.log(capability_id, "rolled_back", f"reverted to {parent.capability_id}")
        return parent

    def stats(self) -> Dict[str, Any]:
        with self._conn() as c:
            total = c.execute("SELECT COUNT(*) FROM plan_capabilities").fetchone()[0]
            by_status = dict(c.execute(
                "SELECT status, COUNT(*) FROM plan_capabilities GROUP BY status"
            ).fetchall())
            uses = c.execute("SELECT COALESCE(SUM(use_count),0), "
                             "COALESCE(SUM(success_count),0), "
                             "COALESCE(SUM(fail_count),0) FROM plan_capabilities").fetchone()
            goals = c.execute("SELECT COUNT(*) FROM capability_goals").fetchone()[0]
        return {"capabilities": total, "by_status": by_status,
                "bound_goals": goals, "uses": uses[0],
                "successes": uses[1], "failures": uses[2]}
