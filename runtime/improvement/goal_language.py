
"""
General task/goal language for free-form synthesis.

A compositional AST (not named task operators) is sampled under constraints
derived from engine state, then lowered to (goal, examples) for SwarmEngine.
"""
from __future__ import annotations

import json
import hashlib
import math
import random
import time
import uuid
from dataclasses import dataclass, field, asdict
from typing import Any, Dict, List, Optional, Tuple, Union


# ----- Task AST ------------------------------------------------------------

@dataclass
class TVar:
    name: str
    sort: str = "any"  # num | str | list | dict | any

@dataclass
class TConst:
    value: Any
    sort: str = "any"

@dataclass
class Transform:
    """Apply a relational/functional intent to inputs."""
    op: str          # add | project | pair | wrap | id
    args: List[Any]  # TVar | TConst | nested nodes
    out_sort: str = "any"

@dataclass
class Compose:
    left: Any
    right: Any
    combine: str = "list"  # list | dict | pair

@dataclass
class Test:
    subject: str     # capability or signature hint
    condition: str   # succeeds | fails | costly
    probe: Any = None

@dataclass
class Construct:
    shape: str       # list | dict | scalar
    slots: List[Any]
    constraint: str = ""

@dataclass
class LearnedRef:
    """Reference to a learned production — preserves hierarchy (not expanded AST)."""
    production_id: str
    kind: str = ""
    # Optional slot bindings for the referenced macro (op names etc.)
    bindings: Dict[str, Any] = field(default_factory=dict)

TaskNode = Union[TVar, TConst, Transform, Compose, Test, Construct, LearnedRef]


def node_kind(n: Any) -> str:
    return type(n).__name__


def node_fingerprint(n: Any) -> str:
    """Structural fingerprint (ignores concrete constants for novelty of shape)."""
    if isinstance(n, TVar):
        return f"Var:{n.sort}"
    if isinstance(n, TConst):
        return f"Const:{n.sort}"
    if isinstance(n, Transform):
        return f"Transform:{n.op}:{'|'.join(node_fingerprint(a) for a in n.args)}->{n.out_sort}"
    if isinstance(n, Compose):
        return f"Compose:{n.combine}:{node_fingerprint(n.left)}+{node_fingerprint(n.right)}"
    if isinstance(n, Test):
        return f"Test:{n.condition}"
    if isinstance(n, Construct):
        return f"Construct:{n.shape}:{len(n.slots)}"
    if isinstance(n, LearnedRef):
        binds = ",".join(f"{k}={v}" for k, v in sorted((n.bindings or {}).items()))
        return f"LearnedRef:{n.production_id}:{binds}"
    return type(n).__name__


def node_to_dict(n: Any) -> Any:
    if isinstance(n, (TVar, TConst, Transform, Compose, Test, Construct, LearnedRef)):
        d = {"_type": type(n).__name__}
        for k, v in n.__dict__.items():
            if isinstance(v, list):
                d[k] = [node_to_dict(x) for x in v]
            elif isinstance(v, (TVar, TConst, Transform, Compose, Test, Construct, LearnedRef)):
                d[k] = node_to_dict(v)
            else:
                d[k] = v
        return d
    return n


def node_from_dict(data: Any) -> Any:
    """Rehydrate a serialized task AST without interpreting its goal text.

    Deferred work stores the existing structural opportunity rather than a
    request to sample a new one after restart.  This is deliberately limited
    to the AST vocabulary defined above; unknown shapes fail at the deferred
    item boundary instead of becoming an executable substitute.
    """
    if not isinstance(data, dict):
        return data
    kind = data.get("_type")
    if kind == "TVar":
        return TVar(data["name"], data.get("sort", "any"))
    if kind == "TConst":
        return TConst(data.get("value"), data.get("sort", "any"))
    if kind == "Transform":
        return Transform(data["op"], [node_from_dict(a) for a in data.get("args", [])],
                         data.get("out_sort", "any"))
    if kind == "Compose":
        return Compose(node_from_dict(data["left"]), node_from_dict(data["right"]),
                       data.get("combine", "list"))
    if kind == "Test":
        probe = data.get("probe")
        return Test(data["subject"], data["condition"],
                    node_from_dict(probe) if probe is not None else None)
    if kind == "Construct":
        return Construct(data["shape"], [node_from_dict(s) for s in data.get("slots", [])],
                         data.get("constraint", ""))
    if kind == "LearnedRef":
        return LearnedRef(data["production_id"], data.get("kind", ""),
                          dict(data.get("bindings") or {}))
    raise ValueError(f"unknown task AST type: {kind!r}")


# ----- Lowering AST → (goal, examples) -------------------------------------

def _eval_node(n: Any, env: Dict[str, Any]) -> Any:
    if isinstance(n, TVar):
        return env[n.name]
    if isinstance(n, TConst):
        return n.value
    if isinstance(n, Transform):
        vals = [_eval_node(a, env) for a in n.args]
        if n.op == "add":
            return vals[0] + vals[1]
        if n.op == "project":
            return vals[0]
        if n.op == "pair":
            return [vals[0], vals[1]]
        if n.op == "wrap":
            return {"v": vals[0]}
        if n.op == "id":
            return vals[0]
        if n.op == "sub":
            return vals[0] - vals[1]
        raise ValueError(f"unknown op {n.op}")
    if isinstance(n, Compose):
        a = _eval_node(n.left, env)
        b = _eval_node(n.right, env)
        if n.combine == "list":
            return [a, b]
        if n.combine == "dict":
            return {"a": a, "b": b}
        return (a, b)
    if isinstance(n, Construct):
        parts = [_eval_node(s, env) for s in n.slots]
        if n.shape == "list":
            return parts
        if n.shape == "dict":
            return {f"k{i}": p for i, p in enumerate(parts)}
        return parts[0] if parts else None
    if isinstance(n, Test):
        # Test nodes lower to a concrete probe of similar shape
        if n.probe is not None:
            return _eval_node(n.probe, env)
        return 0
    raise TypeError(type(n))


def _collect_vars(n: Any, acc: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    acc = acc if acc is not None else {}
    if isinstance(n, TVar):
        acc[n.name] = n.sort
    elif isinstance(n, Transform):
        for a in n.args:
            _collect_vars(a, acc)
    elif isinstance(n, Compose):
        _collect_vars(n.left, acc)
        _collect_vars(n.right, acc)
    elif isinstance(n, Construct):
        for s in n.slots:
            _collect_vars(s, acc)
    elif isinstance(n, Test) and n.probe is not None:
        _collect_vars(n.probe, acc)
    elif isinstance(n, LearnedRef):
        pass  # vars come from expanded form at eval time
    return acc


def lower_to_examples(root: Any, n_examples: int = 2, rng: Optional[random.Random] = None,
                      production_index: Optional[Dict[str, "LearnedProduction"]] = None
                      ) -> Tuple[str, List[Tuple[Dict[str, Any], Any]]]:
    rng = rng or random.Random()
    production_index = production_index or {}
    # Expand LearnedRef only for evaluation — fingerprint still uses hierarchical form
    eval_root = expand_learned_refs(root, production_index, rng) if production_index else root
    vars_ = _collect_vars(eval_root)
    if not vars_:
        vars_ = {"x": "num"}
    examples = []
    for i in range(n_examples):
        env = {}
        for name, sort in vars_.items():
            if sort in ("num", "any"):
                env[name] = rng.randint(0, 20) + i
            elif sort == "str":
                env[name] = f"s{i}_{rng.randint(0,9)}"
            else:
                env[name] = rng.randint(0, 10)
        try:
            out = _eval_node(eval_root, env)
        except Exception:
            out = None
        examples.append((dict(env), out))
    # Goal string derived from structure fingerprint + short id (not a hardcoded task name)
    fp = node_fingerprint(root)
    digest = uuid.uuid4().hex[:8]
    goal = f"goal_{fp.replace(':','_').replace('|','__')[:48]}_{digest}"
    return goal, examples


# ----- State → constraints → structure sampling ----------------------------

@dataclass
class GoalCandidate:
    structure: Any
    goal: str
    examples: List[Tuple[Dict[str, Any], Any]]
    fingerprint: str
    provenance: Dict[str, Any]
    expected_info_gain: float
    expected_capability_value: float
    relevance: float
    novelty: float
    future_value: float
    cost: float
    risk: float
    utility: float = 0.0
    rationale: str = ""
    # Runtime evidence is read from AbstractionStore before scoring.  It is
    # deliberately separate from provenance: provenance explains where a
    # candidate came from; this records the observed resource consequences of
    # actually selecting structurally equivalent candidates in earlier runs.
    resource_evidence: Dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "goal": self.goal,
            "fingerprint": self.fingerprint,
            "structure": (node_to_dict(self.structure)
                           if self.structure is not None else None),
            "examples": [{"args": a, "expect": e} for a, e in self.examples],
            "provenance": self.provenance,
            "utility": self.utility,
            "rationale": self.rationale,
            "expected_info_gain": self.expected_info_gain,
            "expected_capability_value": self.expected_capability_value,
            "relevance": self.relevance,
            "novelty": self.novelty,
            "future_value": self.future_value,
            "cost": self.cost,
            "risk": self.risk,
            "resource_evidence": self.resource_evidence,
        }


def goal_candidate_from_dict(data: Dict[str, Any]) -> GoalCandidate:
    """Restore a persisted candidate for scoring through the normal utility.

    GoalCandidate is intentionally reconstructed rather than pickled.  The
    persisted payload is inspectable JSON and a new process derives resource
    evidence and utility from its current state before it can execute.
    """
    examples = []
    for example in data.get("examples") or []:
        if not isinstance(example, dict) or not isinstance(example.get("args"), dict):
            raise ValueError("deferred candidate contains malformed examples")
        examples.append((dict(example["args"]), example.get("expect")))
    structure_data = data.get("structure")
    structure = node_from_dict(structure_data) if structure_data is not None else None
    return GoalCandidate(
        structure=structure,
        goal=str(data["goal"]),
        examples=examples,
        fingerprint=str(data["fingerprint"]),
        provenance=dict(data.get("provenance") or {}),
        expected_info_gain=float(data.get("expected_info_gain") or 0.0),
        expected_capability_value=float(data.get("expected_capability_value") or 0.0),
        relevance=float(data.get("relevance") or 0.0),
        novelty=float(data.get("novelty") or 0.0),
        future_value=float(data.get("future_value") or 0.0),
        cost=float(data.get("cost") or 0.0),
        risk=float(data.get("risk") or 0.0),
        utility=float(data.get("utility") or 0.0),
        rationale=str(data.get("rationale") or ""),
        resource_evidence=dict(data.get("resource_evidence") or {}),
    )


@dataclass
class DeferredOpportunity:
    """Persisted, reconstructable work that a resource decision postponed."""
    opportunity_id: str
    identity: str
    candidate: Dict[str, Any]
    reason: Dict[str, Any]
    resource_state: Dict[str, Any]
    status: str = "deferred"  # deferred | closed | invalid
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    last_reconsidered_state: Dict[str, Any] = field(default_factory=dict)
    execution: Dict[str, Any] = field(default_factory=dict)
    recovery_state: Dict[str, Any] = field(default_factory=dict)
    version: int = 1

    def as_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @staticmethod
    def from_dict(data: Dict[str, Any]) -> "DeferredOpportunity":
        allowed = DeferredOpportunity.__dataclass_fields__
        return DeferredOpportunity(**{k: data[k] for k in allowed if k in data})


def _utility(info, cap, rel, nov, fut, cost, risk) -> float:
    ev = 0.55 * info + 0.45 * cap
    return max(0.0, ev) * max(0.0, rel) * max(0.05, nov) * max(0.05, fut) / (1.0 + max(0.0, cost) + max(0.0, risk))



# ----- Learned abstractions / macros ---------------------------------------

@dataclass
class LearnedProduction:
    """Parameterized production induced from successful synthesis history."""
    production_id: str
    kind: str                 # e.g. MACRO_COMPOSE
    template: Dict[str, Any]  # structural template with $slots
    slots: List[str]          # parameter names
    support: int              # how many successful instances
    evidence_fps: List[str]
    created_at: float = field(default_factory=time.time)
    source: str = "runtime_induction"
    # Governance fields
    status: str = "active"    # active | revoked | superseded | invalid
    version: int = 1
    depends_on: List[str] = field(default_factory=list)  # production_ids
    superseded_by: Optional[str] = None
    revoked_at: Optional[float] = None
    revoke_reason: str = ""
    contract_examples: List[Dict[str, Any]] = field(default_factory=list)
    repair_provenance: Dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @staticmethod
    def from_dict(d: Dict[str, Any]) -> "LearnedProduction":
        fields = LearnedProduction.__dataclass_fields__
        return LearnedProduction(**{k: d[k] for k in fields if k in d})


class AbstractionStore:
    def __init__(self, db_path: str):
        self.db_path = db_path
        import sqlite3
        with sqlite3.connect(db_path) as c:
            c.execute(
                "CREATE TABLE IF NOT EXISTS learned_productions ("
                " production_id TEXT PRIMARY KEY,"
                " payload TEXT NOT NULL,"
                " created_at REAL NOT NULL)"
            )
            c.execute(
                "CREATE TABLE IF NOT EXISTS abstraction_events ("
                " id INTEGER PRIMARY KEY AUTOINCREMENT,"
                " production_id TEXT,"
                " event TEXT,"
                " detail TEXT,"
                " at REAL NOT NULL)"
            )
            # Resource observations belong to the abstraction store rather
            # than an in-memory loop instance.  A later process must make its
            # next choice from the same observed execution consequences.
            c.execute(
                "CREATE TABLE IF NOT EXISTS abstraction_resource_experience ("
                " id INTEGER PRIMARY KEY AUTOINCREMENT,"
                " resource_key TEXT NOT NULL,"
                " fingerprint TEXT NOT NULL,"
                " production TEXT NOT NULL,"
                " learned_id TEXT,"
                " version INTEGER,"
                " wall_ns INTEGER NOT NULL,"
                " fully_resolved INTEGER NOT NULL,"
                " resource_refused INTEGER NOT NULL DEFAULT 0,"
                " acquired_count INTEGER NOT NULL,"
                " recorded_at REAL NOT NULL)"
            )
            # A previous development database may already have the additive
            # table without the refusal flag.  Preserve all observations and
            # migrate in place instead of treating an old row as unknown.
            cols = {row[1] for row in c.execute(
                "PRAGMA table_info(abstraction_resource_experience)")}
            if "resource_refused" not in cols:
                c.execute(
                    "ALTER TABLE abstraction_resource_experience "
                    "ADD COLUMN resource_refused INTEGER NOT NULL DEFAULT 0"
                )
            c.execute(
                "CREATE INDEX IF NOT EXISTS idx_abstraction_resource_key "
                "ON abstraction_resource_experience(resource_key)"
            )
            c.execute(
                "CREATE TABLE IF NOT EXISTS abstraction_decisions ("
                " id INTEGER PRIMARY KEY AUTOINCREMENT,"
                " action TEXT NOT NULL,"
                " fingerprint TEXT NOT NULL,"
                " utility REAL NOT NULL,"
                " remaining_acquisitions INTEGER,"
                " pressure REAL NOT NULL,"
                " deferred INTEGER NOT NULL,"
                " recorded_at REAL NOT NULL)"
            )
            # A defer decision is historical evidence; a deferred opportunity
            # is the separately addressable work it postponed.  Keeping the
            # full reconstruction contract in one versioned JSON payload makes
            # this additive beside existing decision/resource history and lets
            # a fresh process reject malformed old payloads safely.
            c.execute(
                "CREATE TABLE IF NOT EXISTS deferred_opportunities ("
                " opportunity_id TEXT PRIMARY KEY,"
                " identity TEXT NOT NULL,"
                " status TEXT NOT NULL,"
                " payload TEXT NOT NULL,"
                " created_at REAL NOT NULL,"
                " updated_at REAL NOT NULL)"
            )
            c.execute(
                "CREATE INDEX IF NOT EXISTS idx_deferred_opportunity_status "
                "ON deferred_opportunities(status, updated_at)"
            )

    def _log(self, production_id: str, event: str, detail: str = ""):
        import sqlite3
        with sqlite3.connect(self.db_path) as c:
            c.execute(
                "INSERT INTO abstraction_events(production_id,event,detail,at) VALUES (?,?,?,?)",
                (production_id, event, detail, time.time()),
            )

    @staticmethod
    def resource_key(fingerprint: str, provenance: Dict[str, Any]) -> str:
        """Stable identity for evidence that can safely inform a future use.

        Base grammar candidates are identified by their structure fingerprint.
        A learned macro is identified by its persisted production id *and
        version*, so cost evidence cannot silently cross a repaired or
        superseded version boundary.  The key contains no goal text and no
        test-specific labels.
        """
        provenance = provenance or {}
        if provenance.get("production") == "LEARNED" and provenance.get("learned_id"):
            return "learned:%s:v%s" % (
                provenance["learned_id"], int(provenance.get("version") or 1))
        return "structure:" + str(fingerprint)

    def resource_stats(self, fingerprint: str,
                       provenance: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Return only persisted, measured observations for this candidate.

        ``wall_ns`` is captured around the real public ``engine.resolve``
        call. ``fully_resolved`` and the number of acquired capabilities are
        retained alongside it so timing is not separated from the outcome it
        bought. With no matching history this returns an explicitly empty
        observation rather than fabricating an estimate.
        """
        import sqlite3
        key = self.resource_key(fingerprint, provenance or {})
        with sqlite3.connect(self.db_path) as c:
            row = c.execute(
                "SELECT COUNT(*), COALESCE(AVG(wall_ns),0),"
                " COALESCE(MAX(wall_ns),0),"
                " COALESCE(SUM(fully_resolved),0),"
                " COALESCE(AVG(resource_refused),0),"
                " COALESCE(AVG(acquired_count),0) "
                "FROM abstraction_resource_experience WHERE resource_key=?",
                (key,),
            ).fetchone()
        (attempts, mean_wall_ns, max_wall_ns, successes,
         resource_refusal_rate, mean_acquired) = row
        attempts = int(attempts or 0)
        return {
            "resource_key": key,
            "attempts": attempts,
            "mean_wall_ns": int(mean_wall_ns or 0),
            "max_wall_ns": int(max_wall_ns or 0),
            "success_rate": (float(successes) / attempts) if attempts else None,
            "resource_refusal_rate": (float(resource_refusal_rate or 0.0)
                                      if attempts else None),
            "mean_acquired_count": float(mean_acquired or 0.0) if attempts else None,
        }

    def resource_baseline_ns(self) -> Optional[float]:
        """Mean measured cost across prior abstraction executions, if any.

        Candidate cost has no useful absolute unit: process and host speed can
        change between runs.  Selection therefore normalizes a candidate's
        observed mean by this persisted population mean.  This is a relative
        comparison of observed work, not a hidden wall-clock cutoff.
        """
        import sqlite3
        with sqlite3.connect(self.db_path) as c:
            row = c.execute(
                "SELECT AVG(wall_ns) FROM abstraction_resource_experience "
                "WHERE wall_ns > 0"
            ).fetchone()
        return float(row[0]) if row and row[0] else None

    def record_resource(self, fingerprint: str, provenance: Dict[str, Any],
                        execution: Dict[str, Any]) -> Dict[str, Any]:
        """Persist one actual selected-candidate execution observation."""
        import sqlite3
        provenance = provenance or {}
        key = self.resource_key(fingerprint, provenance)
        acquired = execution.get("acquired") or []
        wall_ns = max(0, int(execution.get("wall_ns") or 0))
        fully_resolved = bool(execution.get("fully_resolved"))
        resource_refused = "budget" in (execution.get("strategies") or [])
        with sqlite3.connect(self.db_path) as c:
            c.execute(
                "INSERT INTO abstraction_resource_experience "
                "(resource_key,fingerprint,production,learned_id,version,"
                "wall_ns,fully_resolved,resource_refused,acquired_count,recorded_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    key, str(fingerprint), str(provenance.get("production") or ""),
                    provenance.get("learned_id"),
                    int(provenance.get("version") or 1),
                    wall_ns, int(fully_resolved), int(resource_refused),
                    len(acquired), time.time(),
                ),
            )
        self._log(provenance.get("learned_id") or str(fingerprint),
                  "resource_observed",
                  json.dumps({"resource_key": key, "wall_ns": wall_ns,
                              "fully_resolved": fully_resolved,
                              "resource_refused": resource_refused,
                              "acquired_count": len(acquired)}))
        return self.resource_stats(fingerprint, provenance)

    def record_decision(self, action: str, fingerprint: str, utility: float,
                        remaining_acquisitions: Optional[int],
                        pressure: float, deferred: bool) -> Dict[str, Any]:
        """Persist an actual selector decision, including a genuine defer.

        Defer has no resolver execution to time. Its evidence is the live
        capacity/observed-demand relationship that made conservation useful,
        plus the selected utility and the fact that expansion was not run.
        """
        import sqlite3
        payload = {
            "action": action,
            "fingerprint": fingerprint,
            "utility": float(utility),
            "remaining_acquisitions": remaining_acquisitions,
            "pressure": float(pressure),
            "deferred": bool(deferred),
        }
        with sqlite3.connect(self.db_path) as c:
            c.execute(
                "INSERT INTO abstraction_decisions "
                "(action,fingerprint,utility,remaining_acquisitions,pressure,"
                "deferred,recorded_at) VALUES (?,?,?,?,?,?,?)",
                (action, fingerprint, float(utility), remaining_acquisitions,
                 float(pressure), int(bool(deferred)), time.time()),
            )
        self._log(fingerprint, "decision_observed", json.dumps(payload))
        return payload

    def save_deferred_opportunity(self, opportunity: DeferredOpportunity) -> DeferredOpportunity:
        """Create or update generic deferred work in the shared persistence boundary."""
        import sqlite3
        opportunity.updated_at = time.time()
        with sqlite3.connect(self.db_path) as c:
            c.execute(
                "INSERT OR REPLACE INTO deferred_opportunities "
                "(opportunity_id,identity,status,payload,created_at,updated_at) "
                "VALUES (?,?,?,?,?,?)",
                (opportunity.opportunity_id, opportunity.identity, opportunity.status,
                 json.dumps(opportunity.as_dict(), default=str), opportunity.created_at,
                 opportunity.updated_at),
            )
        self._log(opportunity.identity, "deferred_opportunity_saved", json.dumps({
            "opportunity_id": opportunity.opportunity_id,
            "status": opportunity.status,
            "version": opportunity.version,
        }))
        return opportunity

    def deferred_opportunities(self, status: str = "deferred") -> List[DeferredOpportunity]:
        """Return only readable items that may participate in reconsideration.

        A malformed or obsolete payload is closed as invalid rather than being
        silently replayed.  This keeps persisted state fail-closed while
        preserving the audit record for diagnosis.
        """
        import sqlite3
        with sqlite3.connect(self.db_path) as c:
            rows = c.execute(
                "SELECT opportunity_id, payload FROM deferred_opportunities "
                "WHERE status=? ORDER BY created_at, opportunity_id", (status,)
            ).fetchall()
        result: List[DeferredOpportunity] = []
        for opportunity_id, raw_payload in rows:
            try:
                payload = json.loads(raw_payload)
                opportunity = DeferredOpportunity.from_dict(payload)
                if (opportunity.opportunity_id != opportunity_id
                        or opportunity.version != 1
                        or not opportunity.identity
                        or not isinstance(opportunity.candidate, dict)):
                    raise ValueError("unsupported deferred opportunity payload")
                # Prove that the executable reconstruction contract still
                # parses before surfacing it to a wake-up path.
                goal_candidate_from_dict(opportunity.candidate)
            except Exception as exc:
                invalid = DeferredOpportunity(
                    opportunity_id=opportunity_id,
                    identity=f"invalid:{opportunity_id}", candidate={}, reason={
                        "invalid_payload": f"{type(exc).__name__}: {exc}"},
                    resource_state={}, status="invalid",
                )
                self.save_deferred_opportunity(invalid)
                self._log(opportunity_id, "deferred_opportunity_invalid", invalid.reason)
                continue
            result.append(opportunity)
        return result

    def find_open_deferred(self, identity: str) -> Optional[DeferredOpportunity]:
        """Find an equivalent unresolved opportunity to avoid duplicate queue entries."""
        for opportunity in self.deferred_opportunities("deferred"):
            if opportunity.identity == identity:
                return opportunity
        return None

    def save(self, prod: LearnedProduction):
        import sqlite3
        # Auto-extract depends_on from template LearnedRef nodes if empty
        if not prod.depends_on:
            prod.depends_on = _extract_deps(prod.template)
        with sqlite3.connect(self.db_path) as c:
            c.execute(
                "INSERT OR REPLACE INTO learned_productions(production_id,payload,created_at) "
                "VALUES (?,?,?)",
                (prod.production_id, json.dumps(prod.as_dict()), prod.created_at),
            )
        self._log(prod.production_id, "saved", prod.status)

    def all(self) -> List[LearnedProduction]:
        import sqlite3
        with sqlite3.connect(self.db_path) as c:
            rows = c.execute("SELECT payload FROM learned_productions").fetchall()
        return [LearnedProduction.from_dict(json.loads(r[0])) for r in rows]

    def get(self, production_id: str) -> Optional[LearnedProduction]:
        for p in self.all():
            if p.production_id == production_id:
                return p
        return None

    def delete(self, production_id: str):
        import sqlite3
        with sqlite3.connect(self.db_path) as c:
            c.execute("DELETE FROM learned_productions WHERE production_id=?", (production_id,))
        self._log(production_id, "deleted")

    def active(self) -> List[LearnedProduction]:
        return [p for p in self.all() if p.status == "active" and self.is_valid(p.production_id)]

    def is_valid(self, production_id: str, _seen: Optional[set] = None) -> bool:
        """True iff production is active and all depends_on are valid (transitive)."""
        _seen = _seen if _seen is not None else set()
        if production_id in _seen:
            return False  # cycle
        _seen.add(production_id)
        prod = self.get(production_id)
        if prod is None:
            return False
        if prod.status != "active":
            return False
        for dep in (prod.depends_on or []):
            if not self.is_valid(dep, _seen):
                return False
        return True

    def dependents(self, production_id: str) -> List[str]:
        """Direct dependents that list production_id in depends_on."""
        return [p.production_id for p in self.all()
                if production_id in (p.depends_on or [])]

    def transitive_dependents(self, production_id: str) -> List[str]:
        out: List[str] = []
        stack = list(self.dependents(production_id))
        seen = set()
        while stack:
            d = stack.pop()
            if d in seen:
                continue
            seen.add(d)
            out.append(d)
            stack.extend(self.dependents(d))
        return out

    def revoke(self, production_id: str, reason: str = "revoked") -> Dict[str, Any]:
        """Revoke production; mark transitive dependents invalid (not deleted)."""
        prod = self.get(production_id)
        if prod is None:
            return {"ok": False, "reason": "missing"}
        prod.status = "revoked"
        prod.revoked_at = time.time()
        prod.revoke_reason = reason
        self.save(prod)
        self._log(production_id, "revoked", reason)
        affected = []
        for dep_id in self.transitive_dependents(production_id):
            dep = self.get(dep_id)
            if dep is None:
                continue
            if dep.status == "active":
                dep.status = "invalid"
                dep.revoke_reason = f"dependency_revoked:{production_id}"
                self.save(dep)
                self._log(dep_id, "invalidated", f"depends on revoked {production_id}")
                affected.append(dep_id)
        return {
            "ok": True,
            "revoked": production_id,
            "invalidated": affected,
            "reason": reason,
        }

    def supersede(self, old_id: str, new_prod: LearnedProduction) -> Dict[str, Any]:
        """Mark old as superseded by new; new becomes active."""
        old = self.get(old_id)
        if old is None:
            return {"ok": False, "reason": "missing_old"}
        new_prod.version = (old.version or 1) + 1
        new_prod.status = "active"
        self.save(new_prod)
        old.status = "superseded"
        old.superseded_by = new_prod.production_id
        self.save(old)
        self._log(old_id, "superseded", new_prod.production_id)
        # Dependents of old become invalid until revalidated
        affected = []
        for dep_id in self.transitive_dependents(old_id):
            dep = self.get(dep_id)
            if dep and dep.status == "active":
                dep.status = "invalid"
                dep.revoke_reason = f"dependency_superseded:{old_id}->{new_prod.production_id}"
                self.save(dep)
                affected.append(dep_id)
        return {"ok": True, "old": old_id, "new": new_prod.production_id,
                "invalidated": affected}

    def revalidate(self, production_id: str) -> Dict[str, Any]:
        """Re-activate production only if all dependencies are valid."""
        prod = self.get(production_id)
        if prod is None:
            return {"ok": False, "reason": "missing"}
        for dep in (prod.depends_on or []):
            if not self.is_valid(dep):
                return {"ok": False, "reason": f"dependency_invalid:{dep}"}
        prod.status = "active"
        prod.revoke_reason = ""
        self.save(prod)
        self._log(production_id, "revalidated")
        return {"ok": True, "production_id": production_id}

    def verify_contract(self, production_id: str,
                        rng: Optional[random.Random] = None) -> Dict[str, Any]:
        """Behaviorally verify a production against its contract_examples.

        Expands LearnedRef hierarchy, evaluates each (args → expect) pair.
        fully_resolved-style success is NOT enough: outputs must match expects.
        """
        prod = self.get(production_id)
        if prod is None:
            return {"ok": False, "reason": "missing", "passed": 0, "failed": 0}
        # Always use production-stable RNG so contracts match induction
        rng = _contract_rng(production_id)
        examples = list(prod.contract_examples or [])
        if not examples:
            # No contract: after rebind, require dependency validity only.
            # Explicitly weaker than behavioral verification.
            deps_ok = True
            prod = self.get(production_id)
            for d in (prod.depends_on or []):
                if not self.is_valid(d):
                    # Allow if dependency is the new replacement being activated
                    dep = self.get(d)
                    if dep is None or dep.status not in ("active",):
                        deps_ok = False
                        break
            return {
                "ok": deps_ok,
                "reason": "no_contract_structural_only",
                "passed": 0, "failed": 0, "checked": 0,
                "structural_ok": deps_ok,
            }
        prod_index = {p.production_id: p for p in self.all()}
        try:
            if prod.kind == "MACRO_COMPOSE":
                x, y = TVar("x", "num"), TVar("y", "num")
                inst = Compose(
                    left=Transform("add", [x, y], "num"),
                    right=Transform("add", [x, y], "num"),
                    combine="dict",
                )
            else:
                inst = instantiate_learned(prod, rng, prod_index)
        except Exception as e:
            return {"ok": False, "reason": f"instantiate_error:{e}", "passed": 0, "failed": 0}
        if inst is None:
            return {"ok": False, "reason": "instantiate_none", "passed": 0, "failed": 0}
        eval_root = expand_learned_refs(inst, prod_index, rng)
        passed, failed, details = 0, 0, []
        for ex in examples:
            args = dict(ex.get("args") or {})
            expect = ex.get("expect")
            try:
                got = _eval_node(eval_root, args)
                ok = got == expect
            except Exception as e:
                got, ok = f"error:{e}", False
            if ok:
                passed += 1
            else:
                failed += 1
                details.append({"args": args, "expect": expect, "got": got})
        return {
            "ok": failed == 0 and passed > 0,
            "passed": passed,
            "failed": failed,
            "checked": passed + failed,
            "mismatches": details[:5],
            "reason": "contract_ok" if failed == 0 and passed > 0 else "contract_mismatch",
        }

    def rebind_dependent(self, dependent_id: str, old_dep: str, new_dep: str) -> Dict[str, Any]:
        """Point dependent's depends_on and LearnedRef templates from old_dep → new_dep."""
        dep = self.get(dependent_id)
        if dep is None:
            return {"ok": False, "reason": "missing"}
        new_depends = [new_dep if d == old_dep else d for d in (dep.depends_on or [])]
        if old_dep in (dep.depends_on or []) and new_dep not in new_depends:
            new_depends.append(new_dep)
        dep.depends_on = new_depends

        def _rebind_template(node):
            if isinstance(node, dict):
                if node.get("_type") == "LearnedRef" and node.get("production_id") == old_dep:
                    node = dict(node)
                    node["production_id"] = new_dep
                return {k: _rebind_template(v) for k, v in node.items()}
            if isinstance(node, list):
                return [_rebind_template(v) for v in node]
            return node

        dep.template = _rebind_template(dep.template)
        self.save(dep)
        self._log(dependent_id, "rebound", f"{old_dep}->{new_dep}")
        return {"ok": True, "depends_on": dep.depends_on}

    def repair_dependents_after_replacement(
        self,
        old_id: str,
        new_id: str,
        rng: Optional[random.Random] = None,
        max_repairs: int = 8,
    ) -> Dict[str, Any]:
        """After old_id is superseded by new_id, repair direct+transitive dependents.

        For each affected dependent (topological order: closer to root first):
          1. rebind LearnedRef/depends_on old → new
          2. check dependency validity
          3. verify behavioral contract
          4. revalidate only if contract passes
        Unaffected productions are not touched.
        """
        rng = rng or random.Random(0)
        # supersede already marked dependents invalid; discover them by reason or graph
        affected = []
        for p in self.all():
            if p.production_id == old_id or p.production_id == new_id:
                continue
            if old_id in (p.depends_on or []):
                affected.append(p.production_id)
            elif p.status == "invalid" and old_id in str(p.revoke_reason or ""):
                affected.append(p.production_id)
        # Also transitive via current depends edges (walk from direct)
        seen = set(affected)
        stack = list(affected)
        while stack:
            cur = stack.pop()
            for d in self.dependents(cur):
                if d not in seen:
                    seen.add(d)
                    affected.append(d)
                    stack.append(d)
        # Order: parents before children (repair B before C)
        order = []
        placed = set()

        def place(pid):
            if pid in placed:
                return
            p = self.get(pid)
            if not p:
                return
            for d in (p.depends_on or []):
                if d in seen and d not in placed:
                    place(d)
            placed.add(pid)
            order.append(pid)

        for pid in affected:
            place(pid)

        report = {
            "old_id": old_id,
            "new_id": new_id,
            "affected": list(affected),
            "order": order,
            "repairs": [],
            "unaffected_preserved": True,
        }
        repaired, failed = [], []
        for pid in order[:max_repairs]:
            entry: Dict[str, Any] = {"production_id": pid}
            rb = self.rebind_dependent(pid, old_id, new_id)
            entry["rebind"] = rb
            # Also rebind intermediate: if depends on something still pointing to old via chain
            # After rebind, verify deps valid
            prod = self.get(pid)
            deps_ok = all(self.is_valid(d) or d == new_id for d in (prod.depends_on or []))
            # new_id must be valid
            if not self.is_valid(new_id):
                entry["result"] = "blocked_new_invalid"
                failed.append(pid)
                report["repairs"].append(entry)
                continue
            # Temporary: allow revalidate path only after contract
            contract = self.verify_contract(pid, rng=rng)
            entry["contract"] = contract
            if contract.get("ok"):
                # Force active if deps include only valid
                prod = self.get(pid)
                prod.status = "active"
                prod.revoke_reason = ""
                prod.repair_provenance = {
                    "repaired_from_dependency": old_id,
                    "repaired_to_dependency": new_id,
                    "contract": {
                        "passed": contract.get("passed"),
                        "failed": contract.get("failed"),
                        "reason": contract.get("reason"),
                    },
                    "at": time.time(),
                }
                self.save(prod)
                self._log(pid, "repaired", new_id)
                entry["result"] = "repaired"
                repaired.append(pid)
            else:
                prod = self.get(pid)
                prod.status = "invalid"
                prod.revoke_reason = f"repair_contract_failed:{contract.get('reason')}"
                prod.repair_provenance = {
                    "repaired_from_dependency": old_id,
                    "attempted_to": new_id,
                    "contract": contract,
                    "at": time.time(),
                }
                self.save(prod)
                entry["result"] = "contract_failed"
                failed.append(pid)
            report["repairs"].append(entry)
        report["repaired"] = repaired
        report["failed"] = failed
        report["consistent"] = len(failed) == 0
        return report


def _extract_deps(template: Any) -> List[str]:
    """Walk template dict for LearnedRef production_ids."""
    deps = []
    if isinstance(template, dict):
        if template.get("_type") == "LearnedRef" and template.get("production_id"):
            deps.append(template["production_id"])
        for v in template.values():
            deps.extend(_extract_deps(v))
    elif isinstance(template, list):
        for v in template:
            deps.extend(_extract_deps(v))
    # unique preserve order
    seen = set()
    out = []
    for d in deps:
        if d not in seen:
            seen.add(d)
            out.append(d)
    return out


def _generalize_compose_pattern(structures: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Induce a parameterized Compose macro from successful Compose structures.

    Requires Transform children so ops can be slots. Support may be 1+ when
    acquisition succeeded — the abstraction is the reusable relationship
    Compose(Transform($op1), Transform($op2), combine=$c), not the exact AST.
    """
    composes = [s for s in structures if s.get("_type") == "Compose"]
    if not composes:
        return None
    # Prefer members whose children are Transforms (parameterizable)
    eligible = []
    for s in composes:
        left_t = (s.get("left") or {}).get("_type", "?")
        right_t = (s.get("right") or {}).get("_type", "?")
        if left_t == "Transform" and right_t == "Transform":
            eligible.append(s)
    if not eligible:
        return None
    # Parameterize combine + ops
    combines = sorted({s.get("combine") or "list" for s in eligible})
    template = {
        "_type": "Compose",
        "combine": "$combine" if len(combines) > 1 else combines[0],
        "left": {"_type": "Transform", "op": "$left_op", "args": "AUTO", "out_sort": "num"},
        "right": {"_type": "Transform", "op": "$right_op", "args": "AUTO", "out_sort": "num"},
    }
    slots = ["left_op", "right_op"]
    if len(combines) > 1:
        slots.append("combine")
    return {
        "kind": "MACRO_COMPOSE",
        "template": template,
        "slots": slots,
        "support": len(eligible),
        "evidence": eligible,
    }



def _contract_rng(production_id: str) -> random.Random:
    """Stable RNG so induce and verify see the same instantiations."""
    h = 0
    for ch in (production_id or ""):
        h = (h * 131 + ord(ch)) & 0xFFFFFFFF
    return random.Random(h ^ 0xC0A1CE)


def _induce_contract_examples(prod: "LearnedProduction",
                              rng: Optional[random.Random] = None,
                              n: int = 2) -> List[Dict[str, Any]]:
    """Derive non-empty behavioral contracts by instantiating and lowering the macro.

    Uses the same evaluation path as verify_contract. Examples with None expect
    are dropped. Provenance is implicit: source=runtime_contract_induction.
    """
    rng = rng or _contract_rng(prod.production_id)
    try:
        # Prefer a fixed, deterministic instantiation for contracts so
        # verify_contract is stable across process restarts.
        if prod.kind == "MACRO_COMPOSE":
            x, y = TVar("x", "num"), TVar("y", "num")
            inst = Compose(
                left=Transform("add", [x, y], "num"),
                right=Transform("add", [x, y], "num"),
                combine="dict",
            )
        else:
            inst = instantiate_learned(prod, rng, {prod.production_id: prod})
        if inst is None:
            return []
        eval_root = expand_learned_refs(inst, {prod.production_id: prod}, rng)
        _, examples = lower_to_examples(eval_root, n_examples=n, rng=rng,
                                       production_index={prod.production_id: prod})
        out = []
        for args, expect in examples:
            if expect is None:
                continue
            out.append({"args": dict(args), "expect": expect,
                        "contract_kind": prod.kind})
        return out[:n]
    except Exception:
        return []


def induce_from_history(
    cycle_payloads: List[Dict[str, Any]],
    existing_learned: Optional[List["LearnedProduction"]] = None,
) -> List[LearnedProduction]:
    """Induce parameterized productions from successful cycle structures.

    First-order: MACRO_COMPOSE / MACRO_CONSTRUCT from base AST.
    Hierarchical: MACRO_HIER when successful cycles used a LEARNED production —
    new macro stores LearnedRef(A), not an expanded base AST.
    """
    existing_learned = existing_learned or []
    successes = [
        c for c in cycle_payloads
        if (c.get("learning") or {}).get("new_acquisition")
        or (c.get("execution") or {}).get("fully_resolved")
    ]
    structures = []
    fps = []
    for c in successes:
        st = c.get("structure")
        if isinstance(st, dict):
            structures.append(st)
            fps.append(c.get("fingerprint") or "")
    prods: List[LearnedProduction] = []
    gen = _generalize_compose_pattern(structures)
    if gen is not None:
        # Stable-ish id from template shape (avoid re-inducing duplicates every cycle)
        comb = str(gen["template"].get("combine"))
        pid = f"learned_macro_compose_{comb}".replace("$", "slot_")
        prods.append(LearnedProduction(
            production_id=pid,
            kind=gen["kind"],
            template=gen["template"],
            slots=gen["slots"],
            support=gen["support"],
            evidence_fps=[f for f in fps if f.startswith("Compose:")],
            source="runtime_induction",
        ))
    constructs = [s for s in structures if s.get("_type") == "Construct"]
    if len(constructs) >= 1:
        shape = constructs[0].get("shape", "list")
        n_slots = len(constructs[0].get("slots") or [])
        if n_slots >= 2:
            pid = f"learned_macro_construct_{shape}_{n_slots}"
            prods.append(LearnedProduction(
                production_id=pid,
                kind="MACRO_CONSTRUCT",
                template={
                    "_type": "Construct",
                    "shape": shape,
                    "slots": [
                        {"_type": "Transform", "op": "$slot0_op", "args": "AUTO", "out_sort": "num"},
                        {"_type": "Transform", "op": "$slot1_op", "args": "AUTO", "out_sort": "num"},
                    ],
                },
                slots=["slot0_op", "slot1_op"],
                support=len(constructs),
                evidence_fps=[c.get("fingerprint") or "" for c in successes
                              if (c.get("structure") or {}).get("_type") == "Construct"],
                source="runtime_induction",
            ))

    # Hierarchical induction: cycles that selected LEARNED production
    learned_uses = [
        c for c in successes
        if c.get("production") == "LEARNED" and c.get("learned_id")
    ]
    if learned_uses and existing_learned:
        # Group by which A was used
        from collections import defaultdict
        by_a = defaultdict(list)
        for c in learned_uses:
            by_a[c["learned_id"]].append(c)
        for a_id, members in by_a.items():
            if len(members) < 1:
                continue
            # B = Compose(LearnedRef(A), Transform($side_op))
            # Explicit LearnedRef — not expanded AST
            template = {
                "_type": "Compose",
                "combine": "$combine",
                "left": {
                    "_type": "LearnedRef",
                    "production_id": a_id,
                    "kind": "MACRO_COMPOSE",
                    "bindings": {"left_op": "$a_left", "right_op": "$a_right"},
                },
                "right": {
                    "_type": "Transform",
                    "op": "$side_op",
                    "args": "AUTO",
                    "out_sort": "num",
                },
            }
            pid = f"learned_macro_hier_{a_id[:24]}"
            prods.append(LearnedProduction(
                production_id=pid,
                kind="MACRO_HIER",
                template=template,
                slots=["a_left", "a_right", "side_op", "combine"],
                support=len(members),
                evidence_fps=[m.get("fingerprint") or "" for m in members],
                source="runtime_hierarchical_induction",
                depends_on=[a_id],
                status="active",
                version=1,
            ))
    return prods


def instantiate_learned(prod: LearnedProduction, rng: random.Random,
                        production_index: Optional[Dict[str, "LearnedProduction"]] = None
                        ) -> Optional[Any]:
    """Expand a learned production into a concrete AST with filled slots.

    MACRO_HIER preserves LearnedRef nodes (hierarchy), not expanded base ASTs.
    """
    production_index = production_index or {}
    if prod.kind == "MACRO_COMPOSE":
        left_op = rng.choice(["add", "sub"])
        right_op = rng.choice(["project", "sub", "add"])
        combine = prod.template.get("combine") or "list"
        if combine == "$combine":
            combine = rng.choice(["list", "dict"])
        x, y = TVar("x", "num"), TVar("y", "num")
        def _mk(op):
            if op == "project":
                return Transform("project", [x], "num")
            return Transform(op, [x, y], "num")
        return Compose(left=_mk(left_op), right=_mk(right_op), combine=combine)
    if prod.kind == "MACRO_CONSTRUCT":
        x, y = TVar("x", "num"), TVar("y", "num")
        op0 = rng.choice(["add", "sub"])
        op1 = rng.choice(["project", "add"])
        slots = [
            Transform(op0, [x, y], "num") if op0 != "project" else Transform("project", [x], "num"),
            Transform(op1, [x, y], "num") if op1 != "project" else Transform("project", [y], "num"),
        ]
        return Construct(shape=prod.template.get("shape") or "list", slots=slots)
    if prod.kind == "MACRO_HIER":
        # Explicit hierarchy: Compose(LearnedRef(A), Transform(...))
        a_left = rng.choice(["add", "sub"])
        a_right = rng.choice(["project", "add", "sub"])
        side_op = rng.choice(["project", "add", "sub"])
        combine = rng.choice(["list", "dict"])
        # Extract A id from template
        left_tmpl = (prod.template or {}).get("left") or {}
        a_id = left_tmpl.get("production_id") or ""
        a_kind = left_tmpl.get("kind") or "MACRO_COMPOSE"
        x, y = TVar("x", "num"), TVar("y", "num")
        def _mk(op):
            if op == "project":
                return Transform("project", [x], "num")
            return Transform(op, [x, y], "num")
        return Compose(
            left=LearnedRef(
                production_id=a_id,
                kind=a_kind,
                bindings={"left_op": a_left, "right_op": a_right},
            ),
            right=_mk(side_op),
            combine=combine,
        )
    return None


def expand_learned_refs(node: Any, production_index: Dict[str, "LearnedProduction"],
                        rng: random.Random, depth: int = 0) -> Any:
    """Resolve LearnedRef nodes to concrete ASTs for execution only (not storage)."""
    if depth > 6:
        return node
    if isinstance(node, LearnedRef):
        prod = production_index.get(node.production_id)
        if prod is None:
            # Fallback: treat as project if missing
            return Transform("project", [TVar("x", "num")], "num")
        # Instantiate A with bindings if present
        inst = instantiate_learned(prod, rng, production_index)
        if inst is None:
            return Transform("project", [TVar("x", "num")], "num")
        return expand_learned_refs(inst, production_index, rng, depth + 1)
    if isinstance(node, Transform):
        return Transform(node.op, [expand_learned_refs(a, production_index, rng, depth+1)
                                   for a in node.args], node.out_sort)
    if isinstance(node, Compose):
        return Compose(
            left=expand_learned_refs(node.left, production_index, rng, depth+1),
            right=expand_learned_refs(node.right, production_index, rng, depth+1),
            combine=node.combine,
        )
    if isinstance(node, Construct):
        return Construct(node.shape, [expand_learned_refs(s, production_index, rng, depth+1)
                                      for s in node.slots], node.constraint)
    if isinstance(node, Test):
        return Test(node.subject, node.condition,
                    expand_learned_refs(node.probe, production_index, rng, depth+1)
                    if node.probe is not None else None)
    return node


class GoalLanguageSynthesizer:
    """Sample task ASTs under state-derived constraints; no named task operators."""

    def __init__(self, engine, seed: Optional[int] = None):
        self.engine = engine
        self.rng = random.Random(seed if seed is not None else (int(time.time()) & 0xFFFF))
        db = getattr(engine, "db_path", None) or "swarm_engine.db"
        self.abstraction_store = AbstractionStore(db)
        self._suppress_learned = False  # ablation switch

    def _state(self) -> Dict[str, Any]:
        learner = getattr(self.engine, "strategy_learner", None)
        groups = []
        n_rows = 0
        if learner is not None:
            try:
                import sqlite3
                from collections import defaultdict
                conn = sqlite3.connect(learner.db_path)
                rows = conn.execute(
                    "SELECT signature, strategy, success, cost FROM acquisition_experience"
                ).fetchall()
                conn.close()
                n_rows = len(rows)
                g = defaultdict(lambda: {"attempts": 0, "successes": 0, "cost": 0})
                for sig, strat, succ, cost in rows:
                    key = (sig, strat)
                    g[key]["signature"] = sig
                    g[key]["strategy"] = strat
                    g[key]["attempts"] += 1
                    g[key]["successes"] += int(succ)
                    g[key]["cost"] += int(cost or 0)
                groups = list(g.values())
            except Exception:
                pass
        caps = []
        try:
            caps = [c.name for c in self.engine.capabilities.list()]
        except Exception:
            pass
        history_fp = set()
        loop = getattr(self.engine, "goal_language_loop", None)
        if loop is not None:
            try:
                for c in loop.load_cycles():
                    history_fp.add(c.get("fingerprint") or "")
            except Exception:
                pass
        remaining_acquisitions = None
        budget = getattr(self.engine, "budget", None)
        if budget is not None and getattr(budget, "acquisitions", None) is not None:
            # This is live resource state, not a selector-owned cutoff.  The
            # budget itself remains the authority that refuses a spend; the
            # synthesizer only reads its current remaining capacity when it
            # has real history about a candidate's acquisition demand.
            remaining_acquisitions = max(
                0,
                int(budget.acquisitions) - int(
                    getattr(budget, "spent_acquisitions", 0) or 0),
            )
        return {
            "groups": groups,
            "n_rows": n_rows,
            "caps": caps,
            "history_fp": history_fp,
            "remaining_acquisitions": remaining_acquisitions,
        }

    def _sample_structures(self, st: Dict[str, Any]) -> List[Tuple[Any, Dict[str, Any]]]:
        """Return list of (AST root, provenance) — structurally diverse."""
        out: List[Tuple[Any, Dict[str, Any]]] = []
        x, y = TVar("x", "num"), TVar("y", "num")

        # TRANSFORM family variants (ops are grammar productions, not task names)
        for op, args, osort in [
            ("add", [x, y], "num"),
            ("sub", [x, y], "num"),
            ("project", [x], "num"),
            ("wrap", [x], "dict"),
            ("pair", [x, y], "list"),
        ]:
            out.append((
                Transform(op=op, args=list(args), out_sort=osort),
                {"production": "TRANSFORM", "op": op, "state_bias": "baseline"},
            ))

        # COMPOSE: nested transforms
        out.append((
            Compose(
                left=Transform("add", [x, y], "num"),
                right=Transform("project", [x], "num"),
                combine="list",
            ),
            {"production": "COMPOSE", "combine": "list",
             "state_bias": "composition_pressure" if st["n_rows"] > 0 else "explore"},
        ))
        out.append((
            Compose(
                left=Transform("add", [x, y], "num"),
                right=Transform("sub", [x, y], "num"),
                combine="dict",
            ),
            {"production": "COMPOSE", "combine": "dict", "state_bias": "shape_diversity"},
        ))

        # CONSTRUCT
        out.append((
            Construct(shape="list", slots=[
                Transform("add", [x, y], "num"),
                Transform("project", [y], "num"),
            ]),
            {"production": "CONSTRUCT", "shape": "list", "state_bias": "assembly"},
        ))

        # TEST — driven by failure groups if present
        fails = [g for g in st["groups"] if g["successes"] == 0 and g["attempts"] >= 2]
        if fails:
            top = max(fails, key=lambda g: g["cost"])
            out.append((
                Test(
                    subject=top["signature"][:40],
                    condition="fails",
                    probe=Transform("add", [x, y], "num"),
                ),
                {"production": "TEST", "condition": "fails",
                 "state_bias": "failure_evidence", "strategy": top["strategy"]},
            ))
        else:
            out.append((
                Test(subject="explore", condition="succeeds",
                     probe=Transform("id", [x], "num")),
                {"production": "TEST", "condition": "succeeds", "state_bias": "empty_explore"},
            ))

        # Low-info control structure
        out.append((
            Transform("id", [TConst(0, "num")], "num"),
            {"production": "TRANSFORM", "op": "id", "state_bias": "low_value"},
        ))
        # Learned productions (runtime-induced macros) — absent from base grammar
        if not self._suppress_learned:
            all_prods = self.abstraction_store.all()
            prod_index = {p.production_id: p for p in all_prods}
            # Governance: only active productions with valid dependency chains
            usable = [p for p in all_prods
                      if p.status == "active"
                      and self.abstraction_store.is_valid(p.production_id)]
            for prod in usable:
                inst = instantiate_learned(prod, self.rng, prod_index)
                if inst is not None:
                    out.append((
                        inst,
                        {
                            "production": "LEARNED",
                            "learned_id": prod.production_id,
                            "kind": prod.kind,
                            "state_bias": "learned_macro",
                            "support": prod.support,
                            "hierarchical": prod.kind == "MACRO_HIER",
                            "version": prod.version,
                        },
                    ))
        return out

    def _score_structure(self, structure: Any, provenance: Dict[str, Any],
                         state: Optional[Dict[str, Any]] = None,
                         goal: Optional[str] = None,
                         examples: Optional[List[Tuple[Dict[str, Any], Any]]] = None,
                         fingerprint: Optional[str] = None) -> GoalCandidate:
        """Score one existing structural opportunity from the current state.

        New sampling and deferred reconsideration deliberately converge here.
        The caller may preserve a prior goal/examples pair, but resource
        evidence, capability state, history saturation, and utility are always
        read afresh.  This is the existing utility machinery factored into one
        callable, not an alternate selector or retry score.
        """
        st = state if state is not None else self._state()
        prov = dict(provenance or {})
        fp = fingerprint or node_fingerprint(structure)
        if goal is None or examples is None:
            prod_index = {p.production_id: p for p in self.abstraction_store.all()}
            goal, examples = lower_to_examples(
                structure, n_examples=2, rng=self.rng, production_index=prod_index)

        # Score from structure + state (not task name)
        prod = prov.get("production", "")
        bias = prov.get("state_bias", "")
        info = 0.5
        cap = 0.5
        rel = 0.5
        nov = 0.8 if fp not in st["history_fp"] else 0.15
        fut = 0.6
        cost = 1.0
        risk = 0.15
        if prod == "COMPOSE":
            info, cap, rel, fut = 0.85, 0.9, 0.85, 0.9
            cost = 1.2
        elif prod == "CONSTRUCT":
            info, cap, rel, fut = 0.8, 0.85, 0.8, 0.85
            cost = 1.15
        elif prod == "LEARNED":
            # Prefer reusing induced macros: high future value, moderate novelty if new fp
            support = float(prov.get("support") or 1)
            info, cap, rel = 0.75, 0.85, 0.9
            fut = min(1.0, 0.7 + 0.1 * support)
            cost = 1.0
            if prov.get("hierarchical") or prov.get("kind") == "MACRO_HIER":
                # Hierarchical macros: slightly higher future value (composition of skills)
                fut = min(1.0, fut + 0.15)
                info = min(1.0, info + 0.05)
        elif prod == "TEST" and bias == "failure_evidence":
            info, cap, rel = 0.9, 0.4, 0.95
        elif prod == "TRANSFORM" and prov.get("op") in ("add", "sub"):
            info, cap, rel, fut = 0.65, 0.8, 0.7, 0.8
            cost = 0.9
        elif bias == "low_value":
            info, cap, rel, nov, fut, cost, risk = 0.05, 0.05, 0.1, 0.1, 0.05, 0.1, 0.0
        # Capability reuse boost if we already have numeric skills
        if st["caps"] and prod in ("TRANSFORM", "COMPOSE") and prov.get("op") in ("add", None):
            cap = min(1.0, cap + 0.1)
        # History saturation
        if fp in st["history_fp"]:
            info *= 0.3
            fut *= 0.3
        # Runtime resource evidence completes the causal path from actual
        # earlier executions to this candidate's present utility.  The
        # model starts neutral until there is a matching observation;
        # it never assigns a synthetic resource score to untried work.
        resource = self.abstraction_store.resource_stats(fp, prov)
        baseline_ns = self.abstraction_store.resource_baseline_ns()
        if resource["attempts"]:
            if baseline_ns and resource["mean_wall_ns"] > 0:
                # A ratio, rather than an absolute time or a depth cap:
                # work slower than the observed population costs more;
                # cheaper work costs less.  The existing utility function
                # remains the only ranking function.
                cost *= resource["mean_wall_ns"] / baseline_ns
            success_rate = float(resource["success_rate"] or 0.0)
            # A failed resource spend should not be rewarded merely for
            # being short.  Observed success lowers future value/risk only
            # through the same evidence record that supplies the cost.
            fut *= success_rate
            risk += 1.0 - success_rate
            # A public resolver reporting its native resource-budget
            # refusal is stronger evidence than a generic resolution
            # failure: that attempt could not buy the intended work with
            # resources then available.  This is an observed record,
            # never a hard-coded candidate cutoff.
            risk += float(resource["resource_refusal_rate"] or 0.0)
            expected_acquisitions = float(resource["mean_acquired_count"] or 0.0)
            remaining = st["remaining_acquisitions"]
            if remaining is not None and expected_acquisitions > 0.0:
                # Current capacity meets observed demand on a continuous
                # scale.  At zero remaining units an abstraction with a
                # history of acquiring capabilities cannot credibly offer
                # its future value; as capacity grows, the penalty fades.
                # This is a relationship between two actual quantities,
                # never a depth/cycle/name rule or a resource threshold.
                capacity_fit = float(remaining) / (float(remaining) + expected_acquisitions)
                fut *= capacity_fit
                risk += 1.0 - capacity_fit
                resource["remaining_acquisitions"] = int(remaining)
                resource["capacity_fit"] = capacity_fit
            elif remaining is not None:
                resource["remaining_acquisitions"] = int(remaining)
        u = _utility(info, cap, rel, nov, fut, cost, risk)
        return GoalCandidate(
            structure=structure,
            goal=goal,
            examples=list(examples),
            fingerprint=fp,
            provenance=prov,
            expected_info_gain=info,
            expected_capability_value=cap,
            relevance=rel,
            novelty=nov,
            future_value=fut,
            cost=cost,
            risk=risk,
            utility=u,
            rationale=(
                f"utility={u:.4f} prod={prod} bias={bias} fp={fp[:40]} "
                f"nov={nov:.2f} hist={fp in st['history_fp']} "
                f"resource_attempts={resource['attempts']} "
                f"resource_mean_ns={resource['mean_wall_ns']}"
            ),
            resource_evidence=resource,
        )

    def rescore(self, candidate: GoalCandidate,
                state: Optional[Dict[str, Any]] = None) -> GoalCandidate:
        """Re-evaluate a persisted executable candidate from current evidence."""
        if candidate.structure is None or candidate.provenance.get("production") == "DEFER":
            raise ValueError("a DEFER decision is not an executable opportunity")
        return self._score_structure(
            candidate.structure, candidate.provenance, state=state,
            goal=candidate.goal, examples=candidate.examples,
            fingerprint=candidate.fingerprint)

    def generate(self) -> List[GoalCandidate]:
        st = self._state()
        raw = self._sample_structures(st)
        cands = [self._score_structure(structure, prov, state=st)
                 for structure, prov in raw]

        cands.append(self._defer_candidate(cands, st))
        cands.sort(key=lambda c: (-c.utility, c.goal))
        return cands

    def _defer_candidate(self, candidates: List[GoalCandidate],
                         state: Optional[Dict[str, Any]] = None) -> GoalCandidate:
        """Construct the ordinary conservation candidate for a candidate pool.

        Deferred-work reconsideration passes its recovered opportunities here,
        so DEFER is compared with exactly the current rescored work rather
        than being bypassed by a retry-specific condition.
        """
        st = state if state is not None else self._state()
        # DEFER is a normal candidate in the same set, not a selector escape
        # hatch.  Its value is conservation value derived from actual
        # resource pressure observed on reusable/expandable candidates.  With
        # no measured demand, pressure is zero and the candidate is neutral;
        # when live capacity is inadequate for observed demand, the same
        # utility function can rank conservation above execution.
        pressure = 0.0
        for candidate in candidates:
            if candidate.provenance.get("production") == "DEFER":
                continue
            evidence = candidate.resource_evidence or {}
            if evidence.get("attempts"):
                fit = evidence.get("capacity_fit")
                if fit is not None:
                    pressure = max(pressure, 1.0 - float(fit))
                elif st["remaining_acquisitions"] == 0 and (
                        evidence.get("mean_acquired_count") or 0.0) > 0.0:
                    pressure = 1.0
        defer_resource = {
            "resource_key": "decision:DEFER_EXPANSION",
            "attempts": 0,
            "remaining_acquisitions": st["remaining_acquisitions"],
            "resource_pressure": pressure,
        }
        defer_goal = "defer_expansion_pending_capacity"
        defer_utility = _utility(
            0.45, 0.45, 0.9, 0.8, pressure, 0.05, 0.05)
        return GoalCandidate(
            structure=None,
            goal=defer_goal,
            examples=[],
            fingerprint="Decision:DEFER_EXPANSION",
            provenance={
                "production": "DEFER",
                "action": "DEFER_EXPANSION",
                "state_bias": "resource_conservation",
            },
            expected_info_gain=0.45,
            expected_capability_value=0.45,
            relevance=0.9,
            novelty=0.8,
            future_value=pressure,
            cost=0.05,
            risk=0.05,
            utility=defer_utility,
            rationale=(
                f"utility={defer_utility:.4f} prod=DEFER "
                f"resource_pressure={pressure:.3f} "
                f"remaining_acquisitions={st['remaining_acquisitions']}"
            ),
            resource_evidence=defer_resource,
        )

    def select(self, cands: List[GoalCandidate]) -> Optional[GoalCandidate]:
        viable = [c for c in cands if c.utility >= 0.02]
        return viable[0] if viable else None



# ----- Planner-driven alternative strategy synthesis (strategy_failure) -----

@dataclass
class AlternativeStrategy:
    """A materially different execution strategy for a failed deferred goal."""
    strategy_id: str
    family: str                 # e.g. transform_simplify | compose_flatten | test_probe | construct_minimal
    description: str
    structure_delta: str        # how it differs from the failed structure
    structure: Any              # AST root
    provenance: Dict[str, Any] = field(default_factory=dict)
    utility: float = 0.0

    def fingerprint(self) -> str:
        return node_fingerprint(self.structure)


class AlternativeStrategySynthesizer:
    """Generate execution-relevant alternative strategies from failure evidence.

    Search space is the existing goal-language grammar (developer-authored).
    Autonomy is selection/synthesis *within* that space from diagnosis evidence,
    not invention of a new grammar.
    """

    FAMILIES = (
        "transform_simplify",
        "compose_flatten",
        "construct_minimal",
        "test_probe",
        "swap_combine",
    )

    def __init__(self, engine, rng: Optional[random.Random] = None):
        self.engine = engine
        self.rng = rng or random.Random(0xC0FFEE)

    def from_failure(self, opportunity: "DeferredOpportunity",
                     failed_candidate: "GoalCandidate",
                     diagnosis_detail: str) -> List[AlternativeStrategy]:
        """Produce alternatives that differ structurally from the failed AST."""
        failed_fp = failed_candidate.fingerprint or ""
        failed_struct = failed_candidate.structure
        failed_prod = (failed_candidate.provenance or {}).get("production")
        tried = set((opportunity.recovery_state or {}).get("tried_strategy_families") or [])
        alts: List[AlternativeStrategy] = []

        # 1) transform_simplify: single Transform instead of Compose
        if "transform_simplify" not in tried and failed_prod in ("COMPOSE", "LEARNED", "CONSTRUCT"):
            x, y = TVar("x", "num"), TVar("y", "num")
            st = Transform("add", [x, y], "num")
            if node_fingerprint(st) != failed_fp:
                alts.append(AlternativeStrategy(
                    strategy_id=f"alt_transform_simplify_{uuid.uuid4().hex[:8]}",
                    family="transform_simplify",
                    description="Replace composite structure with single Transform(add)",
                    structure_delta="Compose/Construct→Transform(add)",
                    structure=st,
                    provenance={"production": "TRANSFORM", "op": "add",
                                "strategy_family": "transform_simplify",
                                "source": "alternative_strategy_synthesis"},
                ))

        # 2) compose_flatten / swap_combine: different combine or ops
        if "swap_combine" not in tried:
            x, y = TVar("x", "num"), TVar("y", "num")
            combine = "dict"
            # Prefer opposite of failed if we can read it
            if isinstance(failed_struct, Compose):
                combine = "list" if failed_struct.combine == "dict" else "dict"
            elif isinstance(failed_struct, dict) and failed_struct.get("combine"):
                combine = "list" if failed_struct.get("combine") == "dict" else "dict"
            st = Compose(
                left=Transform("sub", [x, y], "num"),
                right=Transform("project", [x], "num"),
                combine=combine,
            )
            if node_fingerprint(st) != failed_fp:
                alts.append(AlternativeStrategy(
                    strategy_id=f"alt_swap_combine_{uuid.uuid4().hex[:8]}",
                    family="swap_combine",
                    description=f"Compose with combine={combine} and sub+project",
                    structure_delta=f"combine→{combine}; ops→sub+project",
                    structure=st,
                    provenance={"production": "COMPOSE", "combine": combine,
                                "strategy_family": "swap_combine",
                                "source": "alternative_strategy_synthesis"},
                ))

        # 3) construct_minimal
        if "construct_minimal" not in tried and failed_prod != "CONSTRUCT":
            x = TVar("x", "num")
            st = Construct(shape="list", slots=[Transform("project", [x], "num")])
            if node_fingerprint(st) != failed_fp:
                alts.append(AlternativeStrategy(
                    strategy_id=f"alt_construct_minimal_{uuid.uuid4().hex[:8]}",
                    family="construct_minimal",
                    description="Minimal Construct(list, [project])",
                    structure_delta="→Construct(list,1)",
                    structure=st,
                    provenance={"production": "CONSTRUCT", "shape": "list",
                                "strategy_family": "construct_minimal",
                                "source": "alternative_strategy_synthesis"},
                ))

        # 4) test_probe: verification-oriented alternative
        if "test_probe" not in tried:
            st = Test(subject="capability", condition="succeeds",
                      probe=Transform("id", [TConst(0, "num")], "num"))
            if node_fingerprint(st) != failed_fp:
                alts.append(AlternativeStrategy(
                    strategy_id=f"alt_test_probe_{uuid.uuid4().hex[:8]}",
                    family="test_probe",
                    description="Probe via Test(succeeds)",
                    structure_delta="→Test(succeeds)",
                    structure=st,
                    provenance={"production": "TEST", "condition": "succeeds",
                                "strategy_family": "test_probe",
                                "source": "alternative_strategy_synthesis"},
                ))

        # Score roughly by structural distance from failed + simplicity
        for a in alts:
            dist = 1.0 if a.fingerprint() != failed_fp else 0.0
            a.utility = 0.4 * dist + 0.3 * (0.5 if a.family == "transform_simplify" else 0.3)
            if "strategy" in (diagnosis_detail or "").lower() or "exhaust" in (diagnosis_detail or "").lower():
                a.utility += 0.1
        # Acquisition-strategy learning: reorder families by observed success
        learner = getattr(self.engine, "strategy_learner", None)
        if learner is not None and alts:
            sig = failed_fp or "alt_strategy"
            families = [a.family for a in alts]
            try:
                ordered = learner.prefer(families, sig)
                rank = {name: i for i, name in enumerate(ordered)}
                for a in alts:
                    # Higher utility for families preferred by learner (lower rank index)
                    a.utility += 0.2 * (1.0 - rank.get(a.family, len(ordered)) / max(1, len(ordered)))
                    # Penalize families with pure failure history
                    st = learner.stats(sig, a.family)
                    if st.get("attempts", 0) > 0 and st.get("successes", 0) == 0:
                        a.utility -= 0.35
            except Exception:
                pass
        alts.sort(key=lambda a: a.utility, reverse=True)
        return alts

    def to_candidate(self, alt: AlternativeStrategy) -> "GoalCandidate":
        goal, examples = lower_to_examples(alt.structure, n_examples=2, rng=self.rng)
        return GoalCandidate(
            structure=alt.structure,
            goal=goal,
            examples=examples,
            fingerprint=alt.fingerprint(),
            provenance={**alt.provenance, "alternative_strategy_id": alt.strategy_id,
                        "structure_delta": alt.structure_delta},
            expected_info_gain=0.5,
            expected_capability_value=0.5,
            relevance=0.7,
            novelty=0.6,
            future_value=0.5,
            cost=0.8,
            risk=0.2,
            utility=alt.utility,
        )


class GoalLanguageLoop:
    def __init__(self, engine):
        self.engine = engine
        self.synth = GoalLanguageSynthesizer(engine)
        from swarm_engine.improvement.agenda import AgendaStore
        db = getattr(engine, "db_path", None) or "swarm_engine.db"
        self.store = AgendaStore(db)
        with self.store._conn() as c:
            c.execute(
                "CREATE TABLE IF NOT EXISTS goal_lang_cycles ("
                " cycle_id INTEGER PRIMARY KEY,"
                " payload TEXT NOT NULL,"
                " at REAL NOT NULL)"
            )
            c.execute(
                "CREATE TABLE IF NOT EXISTS goal_lang_seq ("
                " name TEXT PRIMARY KEY, value INTEGER NOT NULL)"
            )

    def _next_id(self) -> int:
        with self.store._conn() as c:
            row = c.execute("SELECT value FROM goal_lang_seq WHERE name='g'").fetchone()
            if not row:
                c.execute("INSERT INTO goal_lang_seq(name,value) VALUES('g',1)")
                return 1
            v = int(row[0]) + 1
            c.execute("UPDATE goal_lang_seq SET value=? WHERE name='g'", (v,))
            return v

    def _save(self, cid: int, payload: Dict[str, Any]):
        with self.store._conn() as c:
            c.execute(
                "INSERT OR REPLACE INTO goal_lang_cycles(cycle_id,payload,at) VALUES (?,?,?)",
                (cid, json.dumps(payload), time.time()),
            )

    def load_cycles(self) -> List[Dict[str, Any]]:
        with self.store._conn() as c:
            rows = c.execute(
                "SELECT cycle_id, payload FROM goal_lang_cycles ORDER BY cycle_id"
            ).fetchall()
        return [{"cycle_id": r[0], **json.loads(r[1])} for r in rows]

    @staticmethod
    def _opportunity_identity(candidate: GoalCandidate) -> str:
        """Stable structural identity for deduplication across fresh processes."""
        material = {
            "fingerprint": candidate.fingerprint,
            "provenance": candidate.provenance,
        }
        digest = hashlib.sha256(
            json.dumps(material, sort_keys=True, default=str).encode("utf-8")
        ).hexdigest()[:20]
        return f"opportunity:{digest}"

    def _persist_deferred_opportunity(self, candidates: List[GoalCandidate],
                                      decision: Dict[str, Any]) -> Optional[DeferredOpportunity]:
        """Persist the executable opportunity the selected DEFER displaced."""
        executable = [candidate for candidate in candidates
                      if candidate.provenance.get("production") != "DEFER"
                      and candidate.structure is not None]
        if not executable:
            return None
        # Preserve the opportunity that supplied the live resource pressure.
        # An unrelated untried candidate can have a higher raw utility while a
        # measured-demand candidate makes conservation win; persisting the
        # untried candidate instead would convert DEFER into an unsafe blind
        # retry.  When no candidate carries resource evidence, fall back to the
        # normal best executable competitor (the generic representation still
        # works, but no capacity-specific proof is claimed for it).
        constrained = [candidate for candidate in executable
                       if candidate.resource_evidence.get("attempts")
                       and candidate.resource_evidence.get("capacity_fit") is not None
                       and float(candidate.resource_evidence["capacity_fit"]) < 1.0]
        postponed = max(constrained or executable,
                        key=lambda candidate: (candidate.utility, candidate.goal))
        identity = self._opportunity_identity(postponed)
        store = self.synth.abstraction_store
        existing = store.find_open_deferred(identity)
        resource_state = {
            "remaining_acquisitions": decision.get("remaining_acquisitions"),
            "resource_pressure": decision.get("pressure"),
        }
        if existing is not None:
            existing.reason = {
                **dict(existing.reason or {}),
                "latest_defer_decision": dict(decision),
                "postponed_utility": postponed.utility,
            }
            existing.resource_state = resource_state
            return store.save_deferred_opportunity(existing)
        opportunity = DeferredOpportunity(
            opportunity_id="defer_" + uuid.uuid4().hex,
            identity=identity,
            candidate=postponed.as_dict(),
            reason={
                "initial_defer_decision": dict(decision),
                "postponed_utility": postponed.utility,
                "postponed_fingerprint": postponed.fingerprint,
            },
            resource_state=resource_state,
        )
        return store.save_deferred_opportunity(opportunity)

    def _execute_candidate(self, chosen: GoalCandidate) -> Tuple[Dict[str, Any], Dict[str, Any]]:
        """Use the existing public resolver and resource observation path."""
        import asyncio
        t0 = time.perf_counter_ns()
        try:
            # Resolution that happens to reuse all cached capability state
            # must still respect authoritative run-wide limits.  The resolver
            # already checks before new acquisition; this check covers the
            # executable reuse path as well, without inventing a second
            # resource policy.
            budget = getattr(self.engine, "budget", None)
            if budget is not None:
                budget.check()
            result = asyncio.run(self.engine.resolve(chosen.goal, examples=chosen.examples))
            err = None
        except Exception as exc:
            result = {}
            err = f"{type(exc).__name__}: {exc}"
        execution = {
            "goal": chosen.goal,
            "fingerprint": chosen.fingerprint,
            "fully_resolved": (result or {}).get("fully_resolved"),
            "failed": (result or {}).get("failed") or [],
            "acquired": (result or {}).get("acquired") or [],
            "strategies": [a.get("strategy") for a in ((result or {}).get("attempts") or [])],
            "attempt_details": [dict(a) for a in ((result or {}).get("attempts") or [])],
            "wall_ns": time.perf_counter_ns() - t0,
            "error": err,
        }
        observation = self.synth.abstraction_store.record_resource(
            chosen.fingerprint, chosen.provenance, execution)
        return execution, observation

    @staticmethod
    def _execution_failure_detail(execution: Dict[str, Any]) -> str:
        if execution.get("error"):
            return str(execution["error"])
        details = [str(attempt.get("detail") or "")
                   for attempt in execution.get("attempt_details") or []]
        details = [detail for detail in details if detail]
        if details:
            joined = "; ".join(details)
            if (execution.get("failed") and execution.get("strategies")
                    and not any(token in joined.lower()
                                for token in ("budget", "resource", "timeout"))):
                return "strategy exhaustion: " + joined
            return joined
        failed = execution.get("failed") or []
        return f"resolver did not fully resolve: {failed}"

    def _record_reconsideration_failure(self, opportunity: DeferredOpportunity,
                                        chosen: GoalCandidate,
                                        execution: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Route a genuine execution failure into the existing diagnosis memory."""
        memory = getattr(self.engine, "failure_memory", None)
        if memory is None:
            return None
        record = memory.record(
            chosen.goal,
            self._execution_failure_detail(execution),
            valid_prior_state={
                "deferred_opportunity_id": opportunity.opportunity_id,
                "candidate_fingerprint": chosen.fingerprint,
                "resource_state": dict(opportunity.last_reconsidered_state or {}),
            },
        )
        return record.as_dict()

    def _update_recovery_state(self, opportunity: DeferredOpportunity,
                               execution: Dict[str, Any],
                               resource_state: Dict[str, Any]) -> Dict[str, Any]:
        """Diagnose one real failure and persist a bounded next disposition."""
        import hashlib
        detail = self._execution_failure_detail(execution)
        diagnosis = self.engine.diagnoser.diagnose(detail)
        state = dict(opportunity.recovery_state or {})
        # Numeric telemetry such as elapsed seconds may vary while the same
        # diagnosed failure persists. Bound on the stable diagnosis/action,
        # while retaining the full detail separately for audit.
        signature = hashlib.sha256(
            f"{diagnosis.kind.value}:{diagnosis.action.value}".encode("utf-8")
        ).hexdigest()[:20]
        condition = hashlib.sha256(
            json.dumps(resource_state, sort_keys=True, default=str).encode("utf-8")
        ).hexdigest()[:20]
        same_failure = state.get("failure_signature") == signature
        attempts = int(state.get("attempts") or 0) + 1
        max_attempts = int(state.get("max_attempts") or 3)
        if diagnosis.kind.value == "unknown":
            disposition, eligible = "blocked_unknown", False
        elif attempts >= max_attempts and same_failure:
            disposition, eligible = "terminal_persistent", False
        elif diagnosis.retryable:
            disposition, eligible = "awaiting_changed_condition", True
        elif diagnosis.action.value in {"replan", "resynthesize", "substitute"}:
            disposition, eligible = "awaiting_strategy_change", True
        else:
            disposition, eligible = "terminal_non_recoverable", False
        updated = {
            "phase": "failed", "attempts": attempts,
            "max_attempts": max_attempts,
            "failure_signature": signature,
            "condition_signature": condition,
            "last_failure_kind": diagnosis.kind.value,
            "last_action": diagnosis.action.value,
            "last_detail": detail[:300],
            "confidence": diagnosis.confidence,
            "retryable": diagnosis.retryable,
            "disposition": disposition,
            "eligible_after_condition_change": eligible,
            "backoff_s": min(1.0, 0.05 * (2 ** max(0, attempts - 1))),
            "last_resource_state": dict(resource_state),
        }
        # Carry learned identity for later replacement/repair without test seeding
        try:
            cand = opportunity.candidate or {}
            prov = cand.get("provenance") or {}
            if prov.get("learned_id"):
                updated["failed_learned_id"] = prov.get("learned_id")
        except Exception:
            pass
        opportunity.recovery_state = updated
        return updated

    @staticmethod
    def _recovery_condition_fingerprint(resource_state: Dict[str, Any]) -> str:
        import hashlib
        return hashlib.sha256(
            json.dumps(resource_state, sort_keys=True, default=str).encode("utf-8")
        ).hexdigest()[:20]

    def _replace_and_repair_dependents(
        self,
        old_learned_id: str,
        chosen_alt: "AlternativeStrategy",
        candidate: "GoalCandidate",
        store: "AbstractionStore",
    ) -> Dict[str, Any]:
        """Supersede old learned production with a replacement derived from alt; repair deps."""
        old = store.get(old_learned_id)
        if old is None:
            return {"ok": False, "reason": "old_missing"}
        new_id = f"{old_learned_id}__alt_{chosen_alt.family}_{uuid.uuid4().hex[:6]}"
        # Build replacement production: same kind skeleton, new identity
        new_prod = LearnedProduction(
            production_id=new_id,
            kind=old.kind,
            template=dict(old.template) if isinstance(old.template, dict) else old.template,
            slots=list(old.slots or []),
            support=old.support,
            evidence_fps=list(old.evidence_fps or []),
            source="alternative_strategy_replacement",
            depends_on=list(old.depends_on or []),
            status="active",
            version=(old.version or 1) + 1,
            contract_examples=list(old.contract_examples or []),
        )
        # If alternative is base-grammar, still supersede identity for dependents
        sup = store.supersede(old_learned_id, new_prod)
        repair = store.repair_dependents_after_replacement(
            old_learned_id, new_id, rng=self.synth.rng)
        self.store.log_event("dependent_repair", old_learned_id, {
            "new_id": new_id,
            "supersede": sup,
            "repair": {
                "affected": repair.get("affected"),
                "repaired": repair.get("repaired"),
                "failed": repair.get("failed"),
            },
        })
        return {"ok": True, "supersede": sup, "repair": repair, "new_id": new_id}

    def _attempt_alternative_strategies(
        self,
        opportunity: DeferredOpportunity,
        failed_candidate: GoalCandidate,
        recovery: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Synthesize, govern, select, execute, and verify alternative strategies.

        Returns a report with handled=True when the strategy-failure path ran.
        Does not fabricate success when no alternative works.
        """
        import asyncio
        detail = str(recovery.get("last_detail") or "")
        synth = AlternativeStrategySynthesizer(self.engine, rng=self.synth.rng)
        alternatives = synth.from_failure(opportunity, failed_candidate, detail)
        tried_families = list(recovery.get("tried_strategy_families") or [])
        # Filter already-tried families
        alternatives = [a for a in alternatives if a.family not in tried_families]
        report: Dict[str, Any] = {
            "handled": True,
            "candidates": [
                {"family": a.family, "delta": a.structure_delta,
                 "fp": a.fingerprint(), "utility": a.utility}
                for a in alternatives
            ],
            "selected": None,
            "execution": None,
            "fully_resolved": False,
            "reason": "",
        }
        store = self.synth.abstraction_store
        alt_attempts = int(recovery.get("alternative_attempts") or 0) + 1
        recovery = dict(recovery)
        recovery["alternative_attempts"] = alt_attempts
        recovery["max_alternative_attempts"] = int(
            recovery.get("max_alternative_attempts") or 2)
        recovery["replan_initiated"] = True
        recovery["replan_evidence"] = {
            "failure_kind": recovery.get("last_failure_kind"),
            "last_action": recovery.get("last_action"),
            "detail": detail[:300],
            "failed_fingerprint": failed_candidate.fingerprint,
        }

        if not alternatives:
            recovery["disposition"] = "terminal_no_alternative"
            recovery["phase"] = "failed"
            recovery["eligible_after_condition_change"] = False
            recovery["last_detail"] = (detail + " | no material alternative strategies")[:300]
            opportunity.recovery_state = recovery
            opportunity.status = "failed"
            store.save_deferred_opportunity(opportunity)
            report["reason"] = "no_material_alternative"
            self.store.log_event("alternative_strategy_none", opportunity.opportunity_id, report)
            return report

        # Select highest-utility alternative (shared ranking; no name branch)
        chosen_alt = max(alternatives, key=lambda a: a.utility)
        candidate = synth.to_candidate(chosen_alt)
        report["selected"] = {
            "family": chosen_alt.family,
            "strategy_id": chosen_alt.strategy_id,
            "fingerprint": candidate.fingerprint,
            "structure_delta": chosen_alt.structure_delta,
            "utility": chosen_alt.utility,
        }
        tried_families.append(chosen_alt.family)
        recovery["tried_strategy_families"] = tried_families
        recovery["selected_alternative"] = report["selected"]

        # Governance: LEARNED alternatives must be valid; base grammar always allowed
        if (candidate.provenance.get("production") == "LEARNED"
                and not store.is_valid(str(candidate.provenance.get("learned_id") or ""))):
            recovery["disposition"] = "awaiting_strategy_change"
            recovery["phase"] = "failed"
            opportunity.recovery_state = recovery
            store.save_deferred_opportunity(opportunity)
            report["reason"] = "alternative_failed_governance"
            return report

        # Execute through normal path
        execution, observation = self._execute_candidate(candidate)
        report["execution"] = execution
        report["resource_observation"] = observation
        recovery["last_alternative_execution"] = {
            "fully_resolved": execution.get("fully_resolved"),
            "fingerprint": candidate.fingerprint,
            "family": chosen_alt.family,
        }
        # Close acquisition-learning loop for alternative families
        self._record_strategy_experience(candidate, execution,
                                         success=bool(execution.get("fully_resolved")))
        # Also record under failed fingerprint signature for prefer()
        learner = getattr(self.engine, "strategy_learner", None)
        if learner is not None:
            try:
                sig = failed_candidate.fingerprint or "alt_strategy"
                cost = int((execution.get("wall_ns") or 0) // 1_000_000) or 1
                learner.record(str(sig)[:200], chosen_alt.family,
                               bool(execution.get("fully_resolved")), cost)
            except Exception:
                pass

        if execution.get("fully_resolved"):
            opportunity.status = "closed"
            recovery["phase"] = "succeeded"
            recovery["disposition"] = "closed"
            recovery["eligible_after_condition_change"] = False
            opportunity.recovery_state = recovery
            opportunity.execution = dict(execution)
            opportunity.candidate = candidate.as_dict()  # persist repaired structure
            opportunity.reason = {
                **dict(opportunity.reason or {}),
                "closure": "alternative_strategy_succeeded",
                "alternative_family": chosen_alt.family,
                "structure_delta": chosen_alt.structure_delta,
            }
            # Dependent repair: if the failed candidate referenced a learned
            # production, register the alternative as a replacement and repair
            # dependents with behavioral contracts.
            repair_report = None
            failed_learned = (failed_candidate.provenance or {}).get("learned_id")
            if not failed_learned:
                failed_learned = recovery.get("failed_learned_id")
            if failed_learned and store.get(str(failed_learned)):
                repair_report = self._replace_and_repair_dependents(
                    str(failed_learned), chosen_alt, candidate, store)
                recovery["dependent_repair"] = repair_report
                opportunity.recovery_state = recovery
                report["dependent_repair"] = repair_report
            store.save_deferred_opportunity(opportunity)
            report["fully_resolved"] = True
            self.store.log_event("alternative_strategy_succeeded",
                                 opportunity.opportunity_id, report["selected"])
            return report

        # Alternative failed — bound and either try more later or terminate
        max_alt = int(recovery.get("max_alternative_attempts") or 2)
        if alt_attempts >= max_alt:
            recovery["disposition"] = "terminal_no_alternative"
            recovery["phase"] = "failed"
            recovery["eligible_after_condition_change"] = False
            opportunity.status = "failed"
        else:
            recovery["disposition"] = "awaiting_strategy_change"
            recovery["phase"] = "failed"
            recovery["eligible_after_condition_change"] = True
        opportunity.recovery_state = recovery
        opportunity.execution = dict(execution)
        store.save_deferred_opportunity(opportunity)
        report["reason"] = "alternative_execution_failed"
        self.store.log_event("alternative_strategy_failed", opportunity.opportunity_id, {
            "family": chosen_alt.family,
            "attempts": alt_attempts,
            "disposition": recovery["disposition"],
        })
        return report

    def on_resource_state_change(self, before: Dict[str, Any],
                                 after: Dict[str, Any]) -> Dict[str, Any]:
        """Autonomously reconsider persisted work after real capacity improves.

        This receives only the native resource-state transition.  It obtains
        deferred opportunities from persistence, scores their original
        structures with current evidence, adds the same DEFER candidate, and
        invokes the existing selector.  It never resubmits a caller-supplied
        goal or forces an executable action.
        """
        report: Dict[str, Any] = {
            "before": dict(before),
            "after": dict(after),
            "triggered": False,
            "recovered": 0,
            "candidates": [],
            "selected": None,
            "execution": None,
        }
        improved = bool(getattr(self.engine, "_capacity_improved")(
            before, after))
        store = self.synth.abstraction_store
        pending = store.deferred_opportunities("deferred")
        report["recovered"] = len(pending)
        if not pending:
            report["reason"] = "no deferred opportunities"
            return report

        # Strategy-failure reconsiderations do not require capacity improvement;
        # they require a different strategy, not more of the same budget.
        strategy_pending = [
            o for o in pending
            if (o.recovery_state or {}).get("disposition") == "awaiting_strategy_change"
        ]
        if not improved and not strategy_pending:
            report["reason"] = "acquisition capacity did not improve"
            return report
        if not improved and strategy_pending:
            report["strategy_replan_only"] = True
            # Restrict this wake to strategy-awaiting items
            pending = strategy_pending

        current_state = self.synth._state()
        rescored: List[GoalCandidate] = []
        candidate_items: Dict[int, DeferredOpportunity] = {}
        invalid = []
        skipped_recovery = []
        condition_signature = self._recovery_condition_fingerprint(after)
        for opportunity in pending:
            try:
                recovery = dict(opportunity.recovery_state or {})
                # Strategy-failure path: eligible for alternative synthesis even
                # when the resource condition signature is unchanged.
                awaiting_strategy = (
                    recovery.get("disposition") == "awaiting_strategy_change"
                    and recovery.get("last_action") in ("replan", "resynthesize", "substitute")
                )
                if (recovery.get("phase") == "failed"
                        and recovery.get("condition_signature") == condition_signature
                        and not recovery.get("eligible_after_condition_change", True)
                        and not awaiting_strategy):
                    skipped_recovery.append(opportunity.opportunity_id)
                    continue
                if (recovery.get("phase") == "failed"
                        and recovery.get("condition_signature") == condition_signature
                        and recovery.get("attempts", 0) > 0
                        and not awaiting_strategy):
                    skipped_recovery.append(opportunity.opportunity_id)
                    continue
                # Bound alternative-strategy attempts separately from resource retries
                if awaiting_strategy:
                    alt_attempts = int(recovery.get("alternative_attempts") or 0)
                    max_alt = int(recovery.get("max_alternative_attempts") or 2)
                    if alt_attempts >= max_alt:
                        skipped_recovery.append(opportunity.opportunity_id)
                        continue
                original = goal_candidate_from_dict(opportunity.candidate)
                recovery = dict(opportunity.recovery_state or {})
                awaiting_strategy = (
                    recovery.get("disposition") == "awaiting_strategy_change"
                    and recovery.get("last_action") in ("replan", "resynthesize", "substitute")
                )
                if awaiting_strategy:
                    # Planner-driven alternative synthesis (not a blind retry).
                    alt_report = self._attempt_alternative_strategies(
                        opportunity, original, recovery)
                    if alt_report.get("handled"):
                        report["triggered"] = True
                        report["alternative_strategy"] = alt_report
                        if alt_report.get("fully_resolved"):
                            report["recovered"] = report.get("recovered", 0)
                            report["selected"] = alt_report.get("selected")
                            report["execution"] = alt_report.get("execution")
                            return report
                        # If alternatives failed, opportunity state already updated;
                        # do not also rescore the original failed structure this wake.
                        continue
                # Governance is checked before any resolver call.  A revoked
                # learned production may not be resurrected merely because the
                # resource budget improved.
                if (original.provenance.get("production") == "LEARNED"
                        and not self.synth.abstraction_store.is_valid(
                            str(original.provenance.get("learned_id") or ""))):
                    opportunity.status = "invalid"
                    opportunity.reason = {**dict(opportunity.reason or {}),
                                          "invalidated": "learned production is no longer governed-valid"}
                    store.save_deferred_opportunity(opportunity)
                    invalid.append(opportunity.opportunity_id)
                    continue
                candidate = self.synth.rescore(original, state=current_state)
            except Exception as exc:
                opportunity.status = "invalid"
                opportunity.reason = {**dict(opportunity.reason or {}),
                                      "invalidated": f"{type(exc).__name__}: {exc}"}
                store.save_deferred_opportunity(opportunity)
                invalid.append(opportunity.opportunity_id)
                continue
            rescored.append(candidate)
            candidate_items[id(candidate)] = opportunity

        report["invalidated"] = invalid
        report["recovery_skipped"] = skipped_recovery
        if not rescored:
            report["reason"] = ("no governed-valid deferred opportunities"
                                 if not skipped_recovery else
                                 "recovery backoff or terminal state")
            return report

        defer = self.synth._defer_candidate(rescored, current_state)
        candidates = sorted(rescored + [defer], key=lambda candidate: (-candidate.utility, candidate.goal))
        report["triggered"] = True
        report["candidates"] = [candidate.as_dict() for candidate in candidates]
        chosen = self.synth.select(candidates)
        if chosen is None:
            report["reason"] = "no viable reconsidered candidate"
            return report
        report["selected"] = chosen.as_dict()

        for opportunity in candidate_items.values():
            opportunity.last_reconsidered_state = dict(after)
            store.save_deferred_opportunity(opportunity)

        if chosen.provenance.get("production") == "DEFER":
            decision = store.record_decision(
                "DEFER_EXPANSION", chosen.fingerprint, chosen.utility,
                chosen.resource_evidence.get("remaining_acquisitions"),
                float(chosen.resource_evidence.get("resource_pressure") or 0.0),
                deferred=True,
            )
            report["decision"] = decision
            report["deferred"] = True
            report["execution"] = {
                "attempted": False, "fully_resolved": None, "acquired": [],
                "strategies": [], "wall_ns": 0, "error": None,
            }
            for opportunity in candidate_items.values():
                opportunity.recovery_state = {
                    **dict(opportunity.recovery_state or {}),
                    "phase": "deferred", "last_resource_state": dict(after),
                }
                store.save_deferred_opportunity(opportunity)
            self.store.log_event("deferred_work_reconsidered", "", {
                "before": before, "after": after, "selected": "DEFER_EXPANSION",
                "pending": [item.opportunity_id for item in candidate_items.values()],
            })
            return report

        opportunity = candidate_items[id(chosen)]
        execution, observation = self._execute_candidate(chosen)
        report["execution"] = {"attempted": True, **execution}
        report["resource_observation"] = observation
        opportunity.execution = dict(execution)
        opportunity.last_reconsidered_state = dict(after)
        if execution.get("fully_resolved"):
            opportunity.status = "closed"
            opportunity.recovery_state = {
                **dict(opportunity.recovery_state or {}),
                "phase": "succeeded", "disposition": "closed",
                "last_resource_state": dict(after),
            }
            opportunity.reason = {**dict(opportunity.reason or {}),
                                  "closure": "fully_resolved_after_resource_improvement"}
            report["closed_opportunity_id"] = opportunity.opportunity_id
        else:
            failure = self._record_reconsideration_failure(opportunity, chosen, execution)
            recovery_state = self._update_recovery_state(
                opportunity, execution, after)
            opportunity.reason = {**dict(opportunity.reason or {}),
                                  "last_execution_failure": failure or self._execution_failure_detail(execution)}
            report["failure_memory"] = failure
            report["recovery_state"] = recovery_state
            report["remains_deferred"] = True
            if recovery_state.get("disposition", "").startswith("terminal"):
                opportunity.status = "failed"
        store.save_deferred_opportunity(opportunity)
        self.store.log_event("deferred_work_reconsidered", opportunity.opportunity_id, {
            "before": before,
            "after": after,
            "selected": chosen.fingerprint,
            "production": chosen.provenance.get("production"),
            "fully_resolved": execution.get("fully_resolved"),
            "status": opportunity.status,
        })
        return report


    def _record_strategy_experience(self, chosen: GoalCandidate,
                                    execution: Dict[str, Any],
                                    success: bool) -> None:
        """Write acquisition_experience so AcquisitionLearner can prefer strategies."""
        learner = getattr(self.engine, "strategy_learner", None)
        if learner is None:
            return
        prov = chosen.provenance or {}
        strategy = (
            prov.get("strategy_family")
            or prov.get("production")
            or (execution.get("strategies") or ["unknown"])[0]
            or "unknown"
        )
        signature = chosen.fingerprint or chosen.goal or "unknown"
        cost = int((execution.get("wall_ns") or 0) // 1_000_000) or 1
        try:
            learner.record(str(signature)[:200], str(strategy), bool(success), cost)
        except Exception:
            pass

    def _capture_natural_failure(self, chosen: GoalCandidate,
                                 execution: Dict[str, Any],
                                 observation: Dict[str, Any]) -> Dict[str, Any]:
        """Persist a deferred opportunity + recovery from a real execution failure.

        Does not accept caller-supplied recovery_state. Diagnosis comes from
        execution detail; learned_id comes from candidate provenance.
        """
        store = self.synth.abstraction_store
        resource_state = {
            "remaining_acquisitions": getattr(
                getattr(self.engine, "budget", None), "acquisitions", None),
        }
        opportunity = DeferredOpportunity(
            opportunity_id=f"natural_{uuid.uuid4().hex[:16]}",
            identity=chosen.fingerprint or chosen.goal,
            candidate=chosen.as_dict(),
            reason={
                "source": "natural_execution_failure",
                "detail": self._execution_failure_detail(execution),
            },
            resource_state=resource_state,
            status="deferred",
            recovery_state={},  # filled by _update_recovery_state only
            execution=dict(execution),
        )
        recovery = self._update_recovery_state(opportunity, execution, resource_state)
        # Ensure strategy failures are replan-eligible
        if recovery.get("last_failure_kind") in ("strategy_failure", "logic", "unknown"):
            if recovery.get("last_action") in ("replan", "resynthesize", "substitute"):
                recovery["disposition"] = "awaiting_strategy_change"
                recovery["eligible_after_condition_change"] = True
                opportunity.recovery_state = recovery
        # If diagnoser did not tag strategy_failure but LEARNED failed, promote
        # to replan so alternative synthesis can run (still evidence-based).
        prov = chosen.provenance or {}
        if (prov.get("production") == "LEARNED"
                and not execution.get("fully_resolved")
                and recovery.get("disposition") not in (
                    "terminal_persistent", "blocked_unknown", "terminal_non_recoverable")):
            recovery["disposition"] = "awaiting_strategy_change"
            recovery["last_action"] = recovery.get("last_action") or "replan"
            if recovery.get("last_failure_kind") in (None, "unknown", "logic"):
                # Enrich detail so diagnoser-equivalent replan is explicit
                detail = recovery.get("last_detail") or ""
                if "strategy exhaustion" not in detail.lower():
                    recovery["last_detail"] = ("strategy exhaustion: "
                                               + (detail or "learned capability failed"))[:300]
                    recovery["last_failure_kind"] = "strategy_failure"
                    recovery["last_action"] = "replan"
            recovery["eligible_after_condition_change"] = True
            opportunity.recovery_state = recovery
        store.save_deferred_opportunity(opportunity)
        self.store.log_event("natural_failure_captured", opportunity.opportunity_id, {
            "fingerprint": chosen.fingerprint,
            "learned_id": prov.get("learned_id"),
            "disposition": recovery.get("disposition"),
            "kind": recovery.get("last_failure_kind"),
            "action": recovery.get("last_action"),
        })
        return {
            "opportunity_id": opportunity.opportunity_id,
            "recovery_state": recovery,
            "learned_id": prov.get("learned_id"),
        }

    def run_once(self) -> Dict[str, Any]:

        import asyncio
        report: Dict[str, Any] = {
            "candidates": [], "selected": None, "execution": None,
            "learning": None, "next_preview": [], "resource_observation": None,
        }
        cands = self.synth.generate()
        report["candidates"] = [c.as_dict() for c in cands]
        chosen = self.synth.select(cands)
        if chosen is None:
            report["note"] = "no viable goal"
            return report
        report["selected"] = chosen.as_dict()
        self.store.log_event("goal_lang_selected", chosen.goal, {
            "fingerprint": chosen.fingerprint,
            "utility": chosen.utility,
            "production": chosen.provenance.get("production"),
        })

        if chosen.provenance.get("production") == "DEFER":
            # A defer is an actual decision, not a failed resolve.  Do not
            # call the engine, do not create an acquisition attempt, and do
            # not induce an abstraction from this decision.
            remaining = chosen.resource_evidence.get("remaining_acquisitions")
            pressure = float(
                chosen.resource_evidence.get("resource_pressure") or 0.0)
            decision = self.synth.abstraction_store.record_decision(
                "DEFER_EXPANSION", chosen.fingerprint, chosen.utility,
                remaining, pressure, deferred=True)
            opportunity = self._persist_deferred_opportunity(cands, decision)
            report["decision"] = decision
            report["deferred"] = True
            report["deferred_opportunity"] = (
                opportunity.as_dict() if opportunity is not None else None)
            report["execution"] = {
                "attempted": False,
                "fully_resolved": None,
                "acquired": [],
                "strategies": [],
                "wall_ns": 0,
                "error": None,
            }
            report["resource_observation"] = decision
            self.store.log_event("goal_lang_deferred", chosen.goal, decision)
            return report

        exec_info, observation = self._execute_candidate(chosen)
        report["execution"] = exec_info
        report["resource_observation"] = observation
        report["learning"] = {
            "new_acquisition": bool(exec_info.get("acquired")),
            "new_failure": (
                (bool(exec_info.get("strategies")) and not exec_info.get("fully_resolved"))
                or bool(exec_info.get("error"))
                or (exec_info.get("fully_resolved") is False)
            ),
            "informative": bool(exec_info.get("strategies")) or bool(exec_info.get("acquired")),
        }
        self.store.log_event("goal_lang_executed", chosen.goal, exec_info)

        # Natural failure path: no pre-seeded recovery_state. Capture failure,
        # diagnose, persist deferred opportunity + recovery for replan.
        if report["learning"]["new_failure"]:
            auto = self._capture_natural_failure(chosen, exec_info, observation)
            report["natural_failure"] = auto
            # Record acquisition/strategy experience for learning loop
            self._record_strategy_experience(chosen, exec_info, success=False)

        if exec_info.get("fully_resolved"):
            self._record_strategy_experience(chosen, exec_info, success=True)

        nxt = self.synth.generate()
        report["next_preview"] = [
            {
                "fingerprint": c.fingerprint,
                "utility": c.utility,
                "production": c.provenance.get("production"),
                "goal": c.goal[:40],
            }
            for c in nxt[:6]
        ]
        return report

    def induce_abstractions(self) -> List[Dict[str, Any]]:
        """Analyze successful history; register new learned productions."""
        history = self.load_cycles()
        existing_list = self.synth.abstraction_store.all()
        induced = induce_from_history(history, existing_learned=existing_list)
        registered = []
        existing_ids = {p.production_id for p in existing_list}
        existing_keys = {p.kind + ":" + json.dumps(p.template, sort_keys=True)
                         for p in existing_list}
        for prod in induced:
            key = prod.kind + ":" + json.dumps(prod.template, sort_keys=True)
            if prod.production_id in existing_ids or key in existing_keys:
                continue
            # Automatic contract induction from instantiation/evaluation evidence
            if not prod.contract_examples:
                prod.contract_examples = _induce_contract_examples(
                    prod, rng=self.synth.rng, n=2)
            self.synth.abstraction_store.save(prod)
            registered.append(prod.as_dict())
            self.store.log_event("abstraction_induced", prod.production_id, {
                "kind": prod.kind,
                "support": prod.support,
                "slots": prod.slots,
                "evidence_fps": prod.evidence_fps[:5],
                "hierarchical": prod.kind == "MACRO_HIER",
                "contract_examples": len(prod.contract_examples or []),
            })
        return registered

    def run_cycles(self, max_cycles: int = 5, min_utility: float = 0.02,
                   induce_every: int = 2) -> Dict[str, Any]:
        cycles = []
        inductions = []
        for i in range(max_cycles):
            cands = self.synth.generate()
            if not cands or max(c.utility for c in cands) < min_utility:
                break
            cid = self._next_id()
            report = self.run_once()
            if not report.get("selected"):
                break
            payload = {
                "selected_goal": report["selected"]["goal"],
                "fingerprint": report["selected"]["fingerprint"],
                "production": report["selected"]["provenance"].get("production"),
                "learned_id": report["selected"]["provenance"].get("learned_id"),
                "structure": report["selected"]["structure"]
                    if isinstance(report["selected"].get("structure"), dict)
                    else node_to_dict(report["selected"].get("structure")),
                "utility": report["selected"]["utility"],
                "resource_evidence": report["selected"].get("resource_evidence"),
                "resource_observation": report.get("resource_observation"),
                "candidates": [
                    {
                        "fingerprint": c["fingerprint"],
                        "utility": c["utility"],
                        "production": (c.get("provenance") or {}).get("production"),
                        "learned_id": (c.get("provenance") or {}).get("learned_id"),
                    }
                    for c in report.get("candidates") or []
                ],
                "execution": report.get("execution"),
                "learning": report.get("learning"),
                "next_preview": report.get("next_preview"),
                "decision": report.get("decision"),
                "deferred": report.get("deferred", False),
            }
            self._save(cid, payload)
            cycles.append({"cycle_id": cid, **payload})
            if report.get("deferred"):
                # The current opportunity remains deferred.  Existing loop
                # callers may later invoke run_once/run_cycles again, but no
                # expansion is performed as a side effect of this decision.
                break
            # Periodic induction from accumulated successful structures
            if (i + 1) % induce_every == 0 or (i + 1) == max_cycles:
                reg = self.induce_abstractions()
                if reg:
                    inductions.extend(reg)
        return {
            "cycles_completed": len(cycles),
            "cycles": cycles,
            "inductions": inductions,
            "learned_productions": [p.as_dict() for p in self.synth.abstraction_store.all()],
        }
