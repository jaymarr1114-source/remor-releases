"""
swarm_engine/governance/provenance.py

Provenance and graduated trust.

Two questions the capability store could not answer: where did this capability
come from, and how much should we believe it? Both matter the moment SWarm
starts acquiring capabilities rather than only synthesizing them, because an
acquired capability is code the engine did not write and cannot vouch for.

Trust is a ladder, not a boolean. A capability enters UNKNOWN and climbs only
by evidence: it is scanned, sandboxed, tested, then used successfully enough
times to be trusted in production. It falls immediately on failure. Governance
asks "may this run?"; trust asks "how much rope does it get?" — and the two
compose, because a TRUSTED capability still cannot exceed its grants.

Lineage is a graph, not a field. A capability derived from two others records
both parents, so when a primitive is found faulty every descendant can be
located rather than guessed at.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple


class TrustLevel(Enum):
    """Graduated trust. Order matters: comparisons use the ordinal."""
    QUARANTINED = 0   # failed verification or regressed; may not run
    UNKNOWN = 1       # newly acquired, nothing established
    SCANNED = 2       # static analysis passed
    SANDBOXED = 3     # executed under isolation without violating limits
    TESTED = 4        # passed a generated or supplied test suite
    TRUSTED = 5       # sustained successful use

    def __lt__(self, other: "TrustLevel") -> bool:
        return self.value < other.value

    def __ge__(self, other: "TrustLevel") -> bool:
        return self.value >= other.value


class Origin(Enum):
    SYNTHESIZED = "synthesized"   # composed from primitives by the planner
    ACQUIRED = "acquired"         # obtained from outside the engine
    DERIVED = "derived"           # an improvement on an existing capability
    BUILTIN = "builtin"           # shipped with the engine


@dataclass
class ProvenanceRecord:
    capability_id: str
    origin: Origin
    trust: TrustLevel = TrustLevel.UNKNOWN
    source: str = ""                       # URL, package name, or planner strategy
    parents: List[str] = field(default_factory=list)
    primitives_used: List[str] = field(default_factory=list)
    effects: List[str] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)
    successes: int = 0
    failures: int = 0
    events: List[Dict[str, Any]] = field(default_factory=list)

    @property
    def total_uses(self) -> int:
        return self.successes + self.failures

    @property
    def success_rate(self) -> float:
        return self.successes / self.total_uses if self.total_uses else 0.0

    @property
    def confidence(self) -> float:
        """Success rate weighted by how much evidence stands behind it.

        A capability that succeeded twice out of two is not as reliable as one
        that succeeded 400 times out of 402, and reporting both as 100% is how
        a planner ends up preferring the untested option. The denominator
        damping makes small samples honest.
        """
        if not self.total_uses:
            return 0.0
        return self.success_rate * (self.total_uses / (self.total_uses + 5.0))

    def as_dict(self) -> Dict[str, Any]:
        return {
            "capability_id": self.capability_id,
            "origin": self.origin.value,
            "trust": self.trust.name,
            "source": self.source,
            "parents": self.parents,
            "primitives_used": self.primitives_used,
            "effects": self.effects,
            "successes": self.successes,
            "failures": self.failures,
            "success_rate": round(self.success_rate, 4),
            "confidence": round(self.confidence, 4),
        }


class ProvenanceStore:
    """Persistent lineage and trust ledger.

    ORACLE BINDING (2026-09-25): when constructed with an oracle registry,
    trust changes go through the tamper-evident ob_trust_transitions log via
    ``transition_trust``. A trust flip is an authorized, attributed, chained
    event -- not an arbitrary UPDATE. ``audit_trust`` detects out-of-band
    mutation (direct SQL writes that bypass the log) by comparing the
    provenance row against the log head, and quarantines on mismatch.
    Without a registry the store keeps its legacy direct-write behavior
    (documented as unbound).
    """

    # Promotion thresholds. Deliberately conservative: trust is cheap to grant
    # and expensive to withdraw once something has run with real permissions.
    PROMOTE_TO_TRUSTED_AFTER = 10
    PROMOTE_MIN_SUCCESS_RATE = 0.95

    def __init__(self, db_path: str = "swarm_engine.db",
                 oracle_registry: Optional[Any] = None,
                 engine_oracle: Optional[Any] = None):
        self.db_path = db_path
        self.oracle_registry = oracle_registry
        self.engine_oracle = engine_oracle
        self._init_schema()

    def _conn(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_schema(self) -> None:
        with self._conn() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS provenance (
                    capability_id TEXT PRIMARY KEY,
                    origin TEXT NOT NULL,
                    trust INTEGER NOT NULL,
                    source TEXT,
                    parents TEXT,
                    primitives_used TEXT,
                    effects TEXT,
                    created_at REAL,
                    successes INTEGER DEFAULT 0,
                    failures INTEGER DEFAULT 0
                )""")
            conn.execute("""
                CREATE TABLE IF NOT EXISTS provenance_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    capability_id TEXT NOT NULL,
                    at REAL NOT NULL,
                    event TEXT NOT NULL,
                    detail TEXT
                )""")
            # A plan capability's revision lineage is not its project
            # prerequisite lineage.  This additive table preserves the actual
            # predecessor artifacts selected in a verified project run.
            conn.execute("""
                CREATE TABLE IF NOT EXISTS capability_prerequisites (
                    capability_id TEXT NOT NULL,
                    graph_fingerprint TEXT NOT NULL,
                    prerequisites_json TEXT NOT NULL,
                    validation_context_json TEXT NOT NULL,
                    behavioral_evidence_json TEXT NOT NULL,
                    updated_at REAL NOT NULL,
                    PRIMARY KEY (capability_id, graph_fingerprint)
                )""")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_cp_capability "
                         "ON capability_prerequisites(capability_id)")

    # -- prerequisite binding ------------------------------------------------
    @staticmethod
    def _canonical_prerequisites(prerequisites: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Keep only durable resolved-artifact identity fields in edge order."""
        fields = ("requirement", "capability_id", "version", "plan_fingerprint", "primitive_name", "dependency_context_fingerprint")
        return [{key: item.get(key) for key in fields} for item in prerequisites]

    def record_prerequisites(self, capability_id: str, graph_fingerprint: str,
                             prerequisites: Sequence[Dict[str, Any]],
                             validation_context: Optional[Dict[str, Any]] = None,
                             behavioral_evidence: Optional[Dict[str, Any]] = None) -> None:
        """Persist a verified child-to-current-predecessor binding.

        The graph fingerprint scopes an explicit ordered predecessor list; it
        is an audit index, not the only causal representation.
        """
        canonical = self._canonical_prerequisites(prerequisites)
        context = dict(validation_context or {})
        evidence = dict(behavioral_evidence or {})
        with self._conn() as conn:
            conn.execute("""
                INSERT OR REPLACE INTO capability_prerequisites
                (capability_id, graph_fingerprint, prerequisites_json,
                 validation_context_json, behavioral_evidence_json, updated_at)
                VALUES (?,?,?,?,?,?)""", (
                capability_id, graph_fingerprint,
                json.dumps(canonical, sort_keys=True),
                json.dumps(context, sort_keys=True, default=str),
                json.dumps(evidence, sort_keys=True, default=str), time.time()))
        self.log(capability_id, "prerequisites_recorded",
                 f"graph={graph_fingerprint} count={len(canonical)}")

    def prerequisite_bindings(self, capability_id: str) -> List[Dict[str, Any]]:
        with self._conn() as conn:
            rows = conn.execute("""
                SELECT graph_fingerprint, prerequisites_json,
                       validation_context_json, behavioral_evidence_json, updated_at
                FROM capability_prerequisites WHERE capability_id=?
                ORDER BY updated_at DESC""", (capability_id,)).fetchall()
        out: List[Dict[str, Any]] = []
        for row in rows:
            try:
                prerequisites = json.loads(row["prerequisites_json"])
                context = json.loads(row["validation_context_json"])
                evidence = json.loads(row["behavioral_evidence_json"])
                valid = isinstance(prerequisites, list) and isinstance(context, dict) and isinstance(evidence, dict)
            except Exception:
                prerequisites, context, evidence, valid = [], {}, {}, False
            out.append({"graph_fingerprint": row["graph_fingerprint"],
                        "prerequisites": prerequisites,
                        "validation_context": context,
                        "behavioral_evidence": evidence,
                        "updated_at": row["updated_at"], "valid": valid})
        return out

    @classmethod
    def prerequisite_context_fingerprint(cls, capability_id: str,
                                         graph_fingerprint: str,
                                         prerequisites: Sequence[Dict[str, Any]]) -> str:
        """Deterministic identity for a canonical prerequisite binding.

        This has no persistence side effect.  Project execution uses it while
        traversing a DAG so a descendant can see a current, still-provisional
        predecessor context.  The identical representation is committed only
        after the whole project verifier succeeds.
        """
        raw = json.dumps({"capability_id": capability_id,
                          "graph_fingerprint": graph_fingerprint,
                          "prerequisites": cls._canonical_prerequisites(prerequisites)},
                         sort_keys=True, separators=(",", ":"), default=str)
        return "context_" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:20]

    def binding_fingerprint(self, capability_id: str, graph_fingerprint: str) -> Optional[str]:
        """Stable identity of a child's verified prerequisite context."""
        binding = next((b for b in self.prerequisite_bindings(capability_id)
                        if b["graph_fingerprint"] == graph_fingerprint), None)
        if binding is None or not binding["valid"]:
            return None
        return self.prerequisite_context_fingerprint(
            capability_id, graph_fingerprint, binding["prerequisites"])

    def compare_prerequisites(self, capability_id: str, graph_fingerprint: str,
                              prerequisites: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
        """Compare current resolved predecessor artifacts to verified state."""
        bindings = self.prerequisite_bindings(capability_id)
        if not bindings:
            return {"decision": "unbound", "capability_id": capability_id,
                    "graph_fingerprint": graph_fingerprint}
        binding = next((b for b in bindings if b["graph_fingerprint"] == graph_fingerprint), None)
        if binding is None:
            return {"decision": "stale_graph_context", "capability_id": capability_id,
                    "graph_fingerprint": graph_fingerprint,
                    "known_graph_fingerprints": [b["graph_fingerprint"] for b in bindings]}
        if not binding["valid"]:
            return {"decision": "invalid_provenance", "capability_id": capability_id,
                    "graph_fingerprint": graph_fingerprint}
        current = self._canonical_prerequisites(prerequisites)
        if binding["prerequisites"] != current:
            return {"decision": "stale_prerequisites", "capability_id": capability_id,
                    "graph_fingerprint": graph_fingerprint,
                    "stored_prerequisites": binding["prerequisites"],
                    "current_prerequisites": current}
        return {"decision": "matched", "capability_id": capability_id,
                "graph_fingerprint": graph_fingerprint,
                "validation_context": binding["validation_context"],
                "behavioral_evidence": binding["behavioral_evidence"]}

    # -- writing ------------------------------------------------------------
    def record(self, record: ProvenanceRecord) -> ProvenanceRecord:
        with self._conn() as conn:
            conn.execute("""
                INSERT OR REPLACE INTO provenance
                (capability_id, origin, trust, source, parents, primitives_used,
                 effects, created_at, successes, failures)
                VALUES (?,?,?,?,?,?,?,?,?,?)""", (
                record.capability_id, record.origin.value, record.trust.value,
                record.source, json.dumps(record.parents),
                json.dumps(record.primitives_used), json.dumps(record.effects),
                record.created_at, record.successes, record.failures))
        self.log(record.capability_id, "recorded",
                 f"origin={record.origin.value} trust={record.trust.name}")
        return record

    def log(self, capability_id: str, event: str, detail: str = "") -> None:
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO provenance_events (capability_id, at, event, detail) "
                "VALUES (?,?,?,?)", (capability_id, time.time(), event, detail[:500]))

    def set_trust(self, capability_id: str, level: TrustLevel, reason: str = "",
                authority: Optional[Any] = None) -> None:
        """Set trust, routed through the tamper-evident transition log when
        an oracle registry is present. ``authority`` may be an
        EngineOracleHandle (or any object with ``transition_trust``); when
        omitted the store's own engine handle is used -- i.e. this store is
        the engine's instrument and its transitions are attributed to the
        engine producer. Direct callers holding only a registry (no engine
        handle) must use ``OracleRegistry.transition_trust`` with their own
        credentials, which refuses producers lacking 'trust:transition'
        authority."""
        if self.oracle_registry is not None:
            handle = authority or self.engine_oracle
            if handle is None:
                raise RuntimeError(
                    "set_trust refused: trust transitions require oracle "
                    "authority and none is available")
            current = self.get(capability_id)
            from_state = current.trust.name if current else None
            logged_head = self.oracle_registry.current_trust(capability_id)
            # If the log already disagrees with the row, the row was mutated
            # out of band: do not stack a transition on a corrupted base.
            if (logged_head is not None and current is not None
                    and logged_head != current.trust.name):
                self.log(capability_id, "trust_tamper_detected",
                         f"provenance row trust={current.trust.name} != "
                         f"transition log head={logged_head}: quarantining")
                level = TrustLevel.QUARANTINED
                reason = (reason + " [tamper detected: row disagreed with "
                          "transition log]").strip()
            trans_id = handle.transition_trust(
                capability_id, from_state, level.name, reason)
            with self._conn() as conn:
                conn.execute("UPDATE provenance SET trust=? WHERE capability_id=?",
                             (level.value, capability_id))
            self.log(capability_id, "trust_changed",
                     f"{level.name}: {reason} [transition={trans_id}]")
            return
        with self._conn() as conn:
            conn.execute("UPDATE provenance SET trust=? WHERE capability_id=?",
                         (level.value, capability_id))
        self.log(capability_id, "trust_changed", f"{level.name}: {reason}")

    def audit_trust(self, capability_id: str) -> Tuple[bool, str]:
        """Verify the provenance trust row agrees with the transition log
        head and that the log chain is intact. On mismatch the capability
        is quarantined (fail closed)."""
        if self.oracle_registry is None:
            return True, "no oracle registry: trust not bound (unbound store)"
        ok, detail = self.oracle_registry.audit_chain("ob_trust_transitions")
        if not ok:
            self._quarantine_raw(
                capability_id,
                f"trust transition log tampered: {detail}")
            return False, f"transition log broken: {detail}"
        current = self.get(capability_id)
        head = self.oracle_registry.current_trust(capability_id)
        if head is None:
            return True, "no transitions recorded for capability"
        if current is None or current.trust.name != head:
            self._quarantine_raw(
                capability_id,
                f"trust row ({current.trust.name if current else 'missing'}) "
                f"!= transition log head ({head})")
            return False, "trust row disagrees with transition log: quarantined"
        return True, "trust row matches transition log head"

    def _quarantine_raw(self, capability_id: str, reason: str) -> None:
        with self._conn() as conn:
            conn.execute("UPDATE provenance SET trust=? WHERE capability_id=?",
                         (TrustLevel.QUARANTINED.value, capability_id))
        self.log(capability_id, "trust_tamper_detected", reason[:500])

    def record_use(self, capability_id: str, success: bool) -> Optional[TrustLevel]:
        """Record an outcome and re-evaluate trust. Returns the new level if it
        moved.

        A single failure demotes. This is asymmetric on purpose: the cost of
        trusting a broken capability is unbounded, while the cost of making a
        good one re-earn its place is a few extra test runs.
        """
        record = self.get(capability_id)
        if record is None:
            return None

        column = "successes" if success else "failures"
        with self._conn() as conn:
            conn.execute(f"UPDATE provenance SET {column}={column}+1 "
                         "WHERE capability_id=?", (capability_id,))

        record = self.get(capability_id)
        if not success:
            if record.trust >= TrustLevel.TESTED:
                self.set_trust(capability_id, TrustLevel.SANDBOXED,
                               "demoted after a failure in use")
                return TrustLevel.SANDBOXED
            return None

        if (record.trust == TrustLevel.TESTED
                and record.successes >= self.PROMOTE_TO_TRUSTED_AFTER
                and record.success_rate >= self.PROMOTE_MIN_SUCCESS_RATE):
            self.set_trust(capability_id, TrustLevel.TRUSTED,
                           f"{record.successes} successes at "
                           f"{record.success_rate:.0%}")
            return TrustLevel.TRUSTED
        return None

    # -- reading ------------------------------------------------------------
    def get(self, capability_id: str) -> Optional[ProvenanceRecord]:
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM provenance WHERE capability_id=?",
                               (capability_id,)).fetchone()
        if row is None:
            return None
        return ProvenanceRecord(
            capability_id=row["capability_id"],
            origin=Origin(row["origin"]),
            trust=TrustLevel(row["trust"]),
            source=row["source"] or "",
            parents=json.loads(row["parents"] or "[]"),
            primitives_used=json.loads(row["primitives_used"] or "[]"),
            effects=json.loads(row["effects"] or "[]"),
            created_at=row["created_at"] or 0.0,
            successes=row["successes"], failures=row["failures"])

    def events(self, capability_id: str, limit: int = 50) -> List[Dict[str, Any]]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT at, event, detail FROM provenance_events "
                "WHERE capability_id=? ORDER BY id DESC LIMIT ?",
                (capability_id, limit)).fetchall()
        return [dict(r) for r in rows]

    def lineage(self, capability_id: str) -> Dict[str, Any]:
        """Walk ancestry. Cycles are tolerated rather than fatal: a corrupt
        parent link should not make the audit trail unreadable."""
        seen: Set[str] = set()

        def walk(cid: str, depth: int) -> Dict[str, Any]:
            record = self.get(cid)
            if record is None:
                return {"capability_id": cid, "missing": True}
            if cid in seen or depth > 20:
                return {"capability_id": cid, "cycle": True}
            seen.add(cid)
            return {
                "capability_id": cid,
                "origin": record.origin.value,
                "trust": record.trust.name,
                "source": record.source,
                "parents": [walk(p, depth + 1) for p in record.parents],
            }

        return walk(capability_id, 0)

    def descendants(self, capability_id: str) -> List[str]:
        """Everything derived from this capability, transitively. This is what
        makes a faulty capability recallable instead of merely regrettable."""
        with self._conn() as conn:
            rows = conn.execute("SELECT capability_id, parents FROM provenance").fetchall()
        children: Dict[str, List[str]] = {}
        for row in rows:
            for parent in json.loads(row["parents"] or "[]"):
                children.setdefault(parent, []).append(row["capability_id"])

        out, stack, seen = [], list(children.get(capability_id, [])), set()
        while stack:
            cid = stack.pop()
            if cid in seen:
                continue
            seen.add(cid)
            out.append(cid)
            stack.extend(children.get(cid, []))
        return out

    def quarantine_lineage(self, capability_id: str, reason: str) -> List[str]:
        """Quarantine a capability and everything built on it."""
        affected = [capability_id] + self.descendants(capability_id)
        for cid in affected:
            self.set_trust(cid, TrustLevel.QUARANTINED, reason)
        return affected

    def list_by_trust(self, minimum: TrustLevel) -> List[ProvenanceRecord]:
        with self._conn() as conn:
            rows = conn.execute("SELECT capability_id FROM provenance WHERE trust>=?",
                                (minimum.value,)).fetchall()
        return [self.get(r["capability_id"]) for r in rows]

    def summary(self) -> Dict[str, Any]:
        with self._conn() as conn:
            rows = conn.execute("SELECT trust, origin, COUNT(*) c FROM provenance "
                                "GROUP BY trust, origin").fetchall()
        by_trust: Dict[str, int] = {}
        by_origin: Dict[str, int] = {}
        for row in rows:
            by_trust[TrustLevel(row["trust"]).name] = by_trust.get(
                TrustLevel(row["trust"]).name, 0) + row["c"]
            by_origin[row["origin"]] = by_origin.get(row["origin"], 0) + row["c"]
        return {"by_trust": by_trust, "by_origin": by_origin,
                "total": sum(by_trust.values())}


class AcquiredCodeStore:
    """Persists the source of acquired capabilities so they survive restart.

    Without this, acquisition produces a capability that exists only until the
    process ends, which is code generation with extra steps. Persisting the
    source plus its verification evidence is what makes the difference between
    "SWarm solved it once" and "SWarm is now able to do this".

    Only the source and its evidence are stored, never a pickled callable: a
    rehydrated capability is re-scanned and re-loaded into the restricted
    sandbox on the way back in, so trust established in one process is not
    silently inherited as executable authority in the next.
    """

    def __init__(self, db_path: str = "swarm_engine.db"):
        self.db_path = db_path
        with self._conn() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS acquired_code (
                    name TEXT PRIMARY KEY,
                    capability_id TEXT NOT NULL,
                    code TEXT NOT NULL,
                    entrypoint TEXT NOT NULL,
                    source TEXT,
                    effects TEXT,
                    spec TEXT,
                    evidence TEXT,
                    created_at REAL
                )""")
            # 2026-09-19 (R7): revocation lifecycle for acquired-code
            # capabilities. Previously only plan_capabilities had a status,
            # so quarantining an acquired-code entry was a no-op in its own
            # store and boot's restore loop silently resurrected it --
            # the A-lifecycle proof did not cover code-acquired deps.
            # status is 'active' or 'quarantined' (deliberate revocation,
            # sticky across reboot); re-admission flips it back to active.
            try:
                conn.execute("ALTER TABLE acquired_code ADD COLUMN status TEXT")
            except Exception:
                pass  # already migrated
            conn.execute("UPDATE acquired_code SET status='active' "
                         "WHERE status IS NULL")

    def _conn(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def save(self, name: str, capability_id: str, code: str, entrypoint: str,
             source: str, effects: List[str], spec: Dict[str, Any],
             evidence: Dict[str, Any]) -> None:
        with self._conn() as conn:
            conn.execute("""
                INSERT OR REPLACE INTO acquired_code
                (name, capability_id, code, entrypoint, source, effects, spec,
                 evidence, created_at, status) VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (name, capability_id, code, entrypoint, source,
                 json.dumps(effects), json.dumps(spec, default=str),
                 json.dumps(evidence, default=str), time.time(), "active"))

    def set_status_by_id(self, capability_id: str, status: str) -> bool:
        """Set status for the entry with this capability_id. Returns True
        iff a row was updated (False: not an acquired-code entry)."""
        with self._conn() as conn:
            cur = conn.execute("UPDATE acquired_code SET status=? "
                               "WHERE capability_id=?", (status, capability_id))
            return cur.rowcount > 0

    def all(self) -> List[Dict[str, Any]]:
        with self._conn() as conn:
            rows = conn.execute("SELECT * FROM acquired_code").fetchall()
        out = []
        for row in rows:
            out.append({
                "name": row["name"], "capability_id": row["capability_id"],
                "code": row["code"], "entrypoint": row["entrypoint"],
                "source": row["source"],
                "effects": json.loads(row["effects"] or "[]"),
                "spec": json.loads(row["spec"] or "{}"),
                "evidence": json.loads(row["evidence"] or "{}"),
                "status": row["status"] if "status" in row.keys() else "active",
            })
        return out

    def get(self, name: str) -> Optional[Dict[str, Any]]:
        for record in self.all():
            if record["name"] == name:
                return record
        return None

    def forget(self, name: str) -> None:
        with self._conn() as conn:
            conn.execute("DELETE FROM acquired_code WHERE name=?", (name,))
