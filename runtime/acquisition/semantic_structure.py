"""
Native SemanticStructure substrate (M+28.12).

Represents compositional semantic relationships independently of executable
primitives. Does NOT map lemmas to registry ops. Does NOT parse English.
Does NOT set polarity, validation, or admission.

Structured input only — future dictionary learners may target this form.
"""
from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple


@dataclass(frozen=True)
class SemanticNode:
    """A node in a semantic structure. Labels are abstract role tokens,
    not executable primitive names and not English lemmas-as-ops."""
    node_id: str
    role: str
    attributes: Tuple[Tuple[str, Any], ...] = ()

    def as_dict(self) -> Dict[str, Any]:
        return {
            "node_id": self.node_id,
            "role": self.role,
            "attributes": dict(self.attributes),
        }

    @staticmethod
    def from_dict(d: Dict[str, Any]) -> "SemanticNode":
        attrs = d.get("attributes") or {}
        if isinstance(attrs, dict):
            attrs_t = tuple(sorted(attrs.items()))
        else:
            attrs_t = tuple(attrs)
        return SemanticNode(str(d["node_id"]), str(d["role"]), attrs_t)


@dataclass(frozen=True)
class SemanticEdge:
    source: str
    relation: str
    target: str

    def as_dict(self) -> Dict[str, Any]:
        return {"source": self.source, "relation": self.relation, "target": self.target}

    @staticmethod
    def from_dict(d: Dict[str, Any]) -> "SemanticEdge":
        return SemanticEdge(str(d["source"]), str(d["relation"]), str(d["target"]))


@dataclass
class SemanticStructure:
    """Inspectable compositional semantic structure.

    Intentionally excludes: polarity, validation result, admission state,
    and executable operation selection.
    """
    structure_id: str
    nodes: Tuple[SemanticNode, ...]
    edges: Tuple[SemanticEdge, ...]
    provenance: Dict[str, Any] = field(default_factory=dict)
    source_evidence_ids: Tuple[str, ...] = ()
    version: int = 1
    parent_ids: Tuple[str, ...] = ()
    created_at: float = field(default_factory=time.time)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "structure_id": self.structure_id,
            "nodes": [n.as_dict() for n in self.nodes],
            "edges": [e.as_dict() for e in self.edges],
            "provenance": dict(self.provenance),
            "source_evidence_ids": list(self.source_evidence_ids),
            "version": self.version,
            "parent_ids": list(self.parent_ids),
            "created_at": self.created_at,
        }

    @staticmethod
    def from_dict(d: Dict[str, Any]) -> "SemanticStructure":
        return SemanticStructure(
            structure_id=str(d["structure_id"]),
            nodes=tuple(SemanticNode.from_dict(n) for n in d.get("nodes") or []),
            edges=tuple(SemanticEdge.from_dict(e) for e in d.get("edges") or []),
            provenance=dict(d.get("provenance") or {}),
            source_evidence_ids=tuple(d.get("source_evidence_ids") or []),
            version=int(d.get("version") or 1),
            parent_ids=tuple(d.get("parent_ids") or []),
            created_at=float(d.get("created_at") or time.time()),
        )

    def fingerprint(self) -> str:
        """Structural identity ignoring provenance/timestamps."""
        payload = {
            "nodes": [n.as_dict() for n in sorted(self.nodes, key=lambda x: x.node_id)],
            "edges": [e.as_dict() for e in sorted(
                self.edges, key=lambda x: (x.source, x.relation, x.target))],
            "version": self.version,
        }
        blob = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return "ss_" + hashlib.sha256(blob.encode()).hexdigest()[:20]

    def node_roles(self) -> Tuple[str, ...]:
        return tuple(sorted({n.role for n in self.nodes}))

    def relations(self) -> Tuple[str, ...]:
        return tuple(sorted({e.relation for e in self.edges}))


def build_structure(
        nodes: Sequence[Dict[str, Any]],
        edges: Sequence[Dict[str, Any]],
        *,
        evidence_ids: Sequence[str] = (),
        provenance: Optional[Dict[str, Any]] = None,
        structure_id: Optional[str] = None,
) -> SemanticStructure:
    """Construct a structure from explicit structured input (not English).

    Rejects empty role/relation strings. Does not interpret lemmas as ops.
    """
    ns: List[SemanticNode] = []
    for n in nodes:
        role = str(n.get("role") or "").strip()
        nid = str(n.get("node_id") or "").strip()
        if not role or not nid:
            raise ValueError("node requires non-empty node_id and role")
        attrs = n.get("attributes") or {}
        if not isinstance(attrs, dict):
            raise ValueError("attributes must be a dict")
        ns.append(SemanticNode(nid, role, tuple(sorted(attrs.items()))))
    node_ids = {n.node_id for n in ns}
    es: List[SemanticEdge] = []
    for e in edges:
        rel = str(e.get("relation") or "").strip()
        src, tgt = str(e.get("source") or ""), str(e.get("target") or "")
        if not rel or not src or not tgt:
            raise ValueError("edge requires source, relation, target")
        if src not in node_ids or tgt not in node_ids:
            raise ValueError(f"edge endpoints must exist in nodes: {src}->{tgt}")
        es.append(SemanticEdge(src, rel, tgt))
    if not ns:
        raise ValueError("structure requires at least one node")
    prov = dict(provenance or {})
    prov.setdefault("builder", "semantic_structure.build_structure.v1")
    sid = structure_id or _id_for(ns, es)
    return SemanticStructure(
        structure_id=sid,
        nodes=tuple(ns),
        edges=tuple(es),
        provenance=prov,
        source_evidence_ids=tuple(evidence_ids),
    )


def _id_for(nodes: Sequence[SemanticNode], edges: Sequence[SemanticEdge]) -> str:
    payload = {
        "n": [n.as_dict() for n in nodes],
        "e": [e.as_dict() for e in edges],
    }
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return "ss_" + hashlib.sha256(blob.encode()).hexdigest()[:20]


def compose(
        left: SemanticStructure,
        right: SemanticStructure,
        bridge: Sequence[Dict[str, Any]],
        *,
        provenance: Optional[Dict[str, Any]] = None,
) -> SemanticStructure:
    """Compose two structures with explicit bridge edges.

    Bridge edges may reference node_ids from either parent. Causal dependence:
    removing a parent node referenced by the bridge invalidates composition.
    """
    # Disjoint node_id namespaces via prefix if collision
    def _prefix(ss: SemanticStructure, pfx: str) -> Tuple[Tuple[SemanticNode, ...], Tuple[SemanticEdge, ...], Dict[str, str]]:
        mapping = {n.node_id: f"{pfx}{n.node_id}" for n in ss.nodes}
        nodes = tuple(
            SemanticNode(mapping[n.node_id], n.role, n.attributes) for n in ss.nodes
        )
        edges = tuple(
            SemanticEdge(mapping[e.source], e.relation, mapping[e.target]) for e in ss.edges
        )
        return nodes, edges, mapping

    ln, le, lm = _prefix(left, "L:")
    rn, re, rm = _prefix(right, "R:")
    id_map = {**lm, **rm}
    # Also allow already-prefixed ids in bridge
    id_map.update({n.node_id: n.node_id for n in ln + rn})

    bridge_edges: List[SemanticEdge] = []
    for b in bridge:
        rel = str(b.get("relation") or "").strip()
        src = str(b.get("source") or "")
        tgt = str(b.get("target") or "")
        if not rel:
            raise ValueError("bridge edge requires relation")
        src_m = id_map.get(src, id_map.get(f"L:{src}", id_map.get(f"R:{src}")))
        tgt_m = id_map.get(tgt, id_map.get(f"L:{tgt}", id_map.get(f"R:{tgt}")))
        if src_m is None or tgt_m is None:
            raise ValueError(f"bridge endpoints unknown: {src}, {tgt}")
        bridge_edges.append(SemanticEdge(src_m, rel, tgt_m))

    if not bridge_edges:
        raise ValueError("compose requires at least one bridge edge")

    all_nodes = ln + rn
    all_edges = le + re + tuple(bridge_edges)
    prov = {
        "builder": "semantic_structure.compose.v1",
        "parents": [left.structure_id, right.structure_id],
        **(provenance or {}),
    }
    return SemanticStructure(
        structure_id=_id_for(all_nodes, all_edges),
        nodes=all_nodes,
        edges=all_edges,
        provenance=prov,
        source_evidence_ids=tuple(
            dict.fromkeys(left.source_evidence_ids + right.source_evidence_ids)
        ),
        parent_ids=(left.structure_id, right.structure_id),
    )


class SemanticStructureStore:
    """SQLite-backed persistence for SemanticStructure (not capabilities)."""

    def __init__(self, db_path: str):
        self.db_path = db_path
        self._init()

    def _conn(self):
        import sqlite3
        return sqlite3.connect(self.db_path)

    def _init(self) -> None:
        with self._conn() as c:
            c.execute(
                """CREATE TABLE IF NOT EXISTS semantic_structures (
                    structure_id TEXT PRIMARY KEY,
                    fingerprint TEXT NOT NULL,
                    body_json TEXT NOT NULL,
                    created_at REAL NOT NULL
                )"""
            )

    def store(self, ss: SemanticStructure) -> SemanticStructure:
        with self._conn() as c:
            c.execute(
                "INSERT OR REPLACE INTO semantic_structures "
                "(structure_id, fingerprint, body_json, created_at) VALUES (?,?,?,?)",
                (ss.structure_id, ss.fingerprint(), json.dumps(ss.as_dict()), ss.created_at),
            )
        return ss

    def get(self, structure_id: str) -> Optional[SemanticStructure]:
        with self._conn() as c:
            row = c.execute(
                "SELECT body_json FROM semantic_structures WHERE structure_id=?",
                (structure_id,),
            ).fetchone()
        if not row:
            return None
        return SemanticStructure.from_dict(json.loads(row[0]))

    def list_ids(self) -> List[str]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT structure_id FROM semantic_structures ORDER BY created_at"
            ).fetchall()
        return [r[0] for r in rows]


# ---------------------------------------------------------------------------
# M+28.13 — Consequence generation (structural, not executable ops)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class SemanticConsequence:
    """A testable claim derived from a SemanticStructure.

    Not an executable primitive. Not a polarity/admission decision.
    """
    consequence_id: str
    kind: str
    payload: Tuple[Tuple[str, Any], ...]
    source_structure_id: str
    rule_id: str
    provenance: Tuple[Tuple[str, Any], ...] = ()

    def as_dict(self) -> Dict[str, Any]:
        return {
            "consequence_id": self.consequence_id,
            "kind": self.kind,
            "payload": dict(self.payload),
            "source_structure_id": self.source_structure_id,
            "rule_id": self.rule_id,
            "provenance": dict(self.provenance),
        }

    @staticmethod
    def from_dict(d: Dict[str, Any]) -> "SemanticConsequence":
        return SemanticConsequence(
            consequence_id=str(d["consequence_id"]),
            kind=str(d["kind"]),
            payload=tuple(sorted((d.get("payload") or {}).items())),
            source_structure_id=str(d["source_structure_id"]),
            rule_id=str(d["rule_id"]),
            provenance=tuple(sorted((d.get("provenance") or {}).items())),
        )


def _cid(kind: str, payload: Dict[str, Any], sid: str, rule: str) -> str:
    blob = json.dumps(
        {"k": kind, "p": payload, "s": sid, "r": rule},
        sort_keys=True, separators=(",", ":"),
    )
    return "sc_" + hashlib.sha256(blob.encode()).hexdigest()[:16]


def derive_consequences(ss: SemanticStructure) -> List[SemanticConsequence]:
    """Derive structural consequences from a SemanticStructure.

    Rules are purely graph-topological / role-based. They never name
    registry primitives (multiply, divide, …) or English lemmas-as-ops.
    """
    out: List[SemanticConsequence] = []
    # Rule 1: role counts
    role_counts: Dict[str, int] = {}
    for n in ss.nodes:
        role_counts[n.role] = role_counts.get(n.role, 0) + 1
    for role, count in sorted(role_counts.items()):
        payload = {"role": role, "count": count}
        out.append(SemanticConsequence(
            consequence_id=_cid("role_count", payload, ss.structure_id, "role_count.v1"),
            kind="role_count",
            payload=tuple(sorted(payload.items())),
            source_structure_id=ss.structure_id,
            rule_id="role_count.v1",
            provenance=(("builder", "derive_consequences.v1"),),
        ))

    # Rule 2: relation presence
    for rel in ss.relations():
        payload = {"relation": rel, "present": True}
        out.append(SemanticConsequence(
            consequence_id=_cid("relation_present", payload, ss.structure_id, "relation_present.v1"),
            kind="relation_present",
            payload=tuple(sorted(payload.items())),
            source_structure_id=ss.structure_id,
            rule_id="relation_present.v1",
            provenance=(("builder", "derive_consequences.v1"),),
        ))

    # Rule 3: transitive closure of SAME (derived edges)
    # Build adjacency for SAME only
    adj: Dict[str, set] = {}
    for e in ss.edges:
        if e.relation != "SAME":
            continue
        adj.setdefault(e.source, set()).add(e.target)
        adj.setdefault(e.target, set()).add(e.source)  # treat SAME as symmetric
    # Floyd-like reachability
    nodes = [n.node_id for n in ss.nodes]
    reach = {n: set(adj.get(n, ())) for n in nodes}
    changed = True
    while changed:
        changed = False
        for n in nodes:
            add: set = set()
            for m in list(reach[n]):
                add |= reach.get(m, set())
            add -= reach[n]
            add.discard(n)
            if add:
                reach[n] |= add
                changed = True
    # Direct SAME pairs already in graph
    direct = {(e.source, e.target) for e in ss.edges if e.relation == "SAME"}
    direct |= {(b, a) for a, b in direct}
    for a in nodes:
        for b in sorted(reach.get(a, ())):
            if a >= b:
                continue
            if (a, b) in direct or (b, a) in direct:
                continue
            payload = {"source": a, "relation": "SAME", "target": b, "derived": True}
            out.append(SemanticConsequence(
                consequence_id=_cid("derived_same", payload, ss.structure_id, "same_transitive.v1"),
                kind="derived_edge",
                payload=tuple(sorted(payload.items())),
                source_structure_id=ss.structure_id,
                rule_id="same_transitive.v1",
                provenance=(("builder", "derive_consequences.v1"),),
            ))

    # Rule 4: PART-OF-WHOLE path existence (PART --*→ WHOLE via any edges)
    part_ids = {n.node_id for n in ss.nodes if n.role == "PART"}
    whole_ids = {n.node_id for n in ss.nodes if n.role == "WHOLE"}
    if part_ids and whole_ids:
        # BFS from each PART
        all_adj: Dict[str, set] = {}
        for e in ss.edges:
            all_adj.setdefault(e.source, set()).add(e.target)
            all_adj.setdefault(e.target, set()).add(e.source)
        for p in sorted(part_ids):
            seen = {p}
            stack = [p]
            while stack:
                cur = stack.pop()
                for nxt in all_adj.get(cur, ()):
                    if nxt not in seen:
                        seen.add(nxt)
                        stack.append(nxt)
            for w in sorted(whole_ids):
                if w in seen:
                    payload = {"part": p, "whole": w, "connected": True}
                    out.append(SemanticConsequence(
                        consequence_id=_cid("part_whole", payload, ss.structure_id, "part_whole_path.v1"),
                        kind="part_whole_connected",
                        payload=tuple(sorted(payload.items())),
                        source_structure_id=ss.structure_id,
                        rule_id="part_whole_path.v1",
                        provenance=(("builder", "derive_consequences.v1"),),
                    ))
    return out


# ---------------------------------------------------------------------------
# M+28.14 — Semantic constraints for native search (no op naming)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class SemanticSearchConstraint:
    """Generic constraints derived from SemanticStructure.

    Intentionally excludes: expected_op, expected_family, expected_primitive.
    May include numeric parameters present as semantic attributes.
    rejected=True when composed graph contains CONTRADICTION.
    """
    param_names: Tuple[str, ...]
    extra_literals: Tuple[Any, ...]
    source_structure_id: str
    provenance: Tuple[Tuple[str, Any], ...] = ()
    rejected: bool = False
    rejection_reason: str = ""
    source_fingerprint: str = ""
    relation_types: Tuple[str, ...] = ()

    def as_dict(self) -> Dict[str, Any]:
        return {
            "param_names": list(self.param_names),
            "extra_literals": list(self.extra_literals),
            "source_structure_id": self.source_structure_id,
            "provenance": dict(self.provenance),
            "rejected": self.rejected,
            "rejection_reason": self.rejection_reason,
            "source_fingerprint": self.source_fingerprint,
            "relation_types": list(self.relation_types),
        }


def constraints_from_structure(ss: SemanticStructure) -> SemanticSearchConstraint:
    """Derive search constraints from structure without naming primitives.

    - QUANTITY / free variable roles → param_names (generic x0, x1, ...)
    - Numeric attributes on any node (e.g. COUNT.n) → extra_literals
    Never sets expected_op / family / primitive.
    """
    # Params: one per QUANTITY node without numeric-only identity, or at least one
    qty = [n for n in ss.nodes if n.role in ("QUANTITY", "INPUT", "VARIABLE")]
    if not qty:
        # fallback: one generic param if structure has any node
        param_names = ("x",) if ss.nodes else ()
    else:
        param_names = tuple(f"x{i}" for i in range(len(qty)))

    literals: List[Any] = []
    for n in ss.nodes:
        for k, v in n.attributes:
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                literals.append(v)
            # also parse numeric strings
            if isinstance(v, str):
                try:
                    literals.append(int(v) if v.isdigit() or (v.startswith("-") and v[1:].isdigit()) else float(v))
                except ValueError:
                    pass
    # stable unique preserve order
    seen = set()
    uniq: List[Any] = []
    for lit in literals:
        key = (type(lit).__name__, lit)
        if key not in seen:
            seen.add(key)
            uniq.append(lit)

    return SemanticSearchConstraint(
        param_names=param_names,
        extra_literals=tuple(uniq),
        source_structure_id=ss.structure_id,
        provenance=(
            ("builder", "constraints_from_structure.v1"),
            ("roles", tuple(ss.node_roles())),
        ),
        rejected=False,
        rejection_reason="",
        source_fingerprint=ss.fingerprint(),
        relation_types=tuple(ss.relations()),
    )


def constraints_from_composed_structure(ss: SemanticStructure) -> SemanticSearchConstraint:
    """Compose relations then derive search constraints.

    - Runs compose_relations (typed edge composition, no ops)
    - If CONTRADICTION present → rejected constraint (no search)
    - Otherwise params/literals from structure attributes/roles
    - relation_types recorded for provenance only (not op selection)
    """
    composed = compose_relations(ss)
    base = constraints_from_structure(composed)
    has_contra = "CONTRADICTION" in composed.relations()
    if has_contra:
        return SemanticSearchConstraint(
            param_names=base.param_names,
            extra_literals=base.extra_literals,
            source_structure_id=composed.structure_id,
            provenance=(
                ("builder", "constraints_from_composed_structure.v1"),
                ("parent_fingerprint", ss.fingerprint()),
                ("composed_fingerprint", composed.fingerprint()),
            ),
            rejected=True,
            rejection_reason="semantic CONTRADICTION present",
            source_fingerprint=composed.fingerprint(),
            relation_types=tuple(composed.relations()),
        )
    return SemanticSearchConstraint(
        param_names=base.param_names,
        extra_literals=base.extra_literals,
        source_structure_id=composed.structure_id,
        provenance=(
            ("builder", "constraints_from_composed_structure.v1"),
            ("parent_fingerprint", ss.fingerprint()),
            ("composed_fingerprint", composed.fingerprint()),
            ("derived_edge_count", composed.provenance.get("derived_edge_count", 0)),
        ),
        rejected=False,
        rejection_reason="",
        source_fingerprint=composed.fingerprint(),
        relation_types=tuple(composed.relations()),
    )


# ---------------------------------------------------------------------------
# M+28.15 — Formal observations extracted from structure (not op-derived)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class FormalObservation:
    """A formal I/O observation carried by or attached to a SemanticStructure.

    Extracted only from explicit SAMPLE / OBSERVED nodes — not invented by
    mapping relations to executable operations.
    """
    observation_id: str
    inputs: Tuple[Tuple[str, Any], ...]
    output: Any
    source_structure_id: str
    source_node_ids: Tuple[str, ...]
    rule_id: str
    provenance: Tuple[Tuple[str, Any], ...] = ()

    def as_dict(self) -> Dict[str, Any]:
        return {
            "observation_id": self.observation_id,
            "inputs": dict(self.inputs),
            "output": self.output,
            "source_structure_id": self.source_structure_id,
            "source_node_ids": list(self.source_node_ids),
            "rule_id": self.rule_id,
            "provenance": dict(self.provenance),
        }

    def as_example(self) -> Tuple[Dict[str, Any], Any]:
        return (dict(self.inputs), self.output)


def _oid(inputs, output, sid, nodes, rule) -> str:
    blob = json.dumps(
        {"i": dict(inputs), "o": output, "s": sid, "n": list(nodes), "r": rule},
        sort_keys=True, separators=(",", ":"), default=str,
    )
    return "fo_" + hashlib.sha256(blob.encode()).hexdigest()[:16]


def observations_from_structure(ss: SemanticStructure) -> List[FormalObservation]:
    """Extract formal observations from explicit sample nodes.

    Supported node patterns (no relation→op tables):
      role SAMPLE or OBSERVED with attributes:
        - input / in / x  (scalar input)
        - output / out / y (scalar output)
      OR paired attributes input_0.. and a single output

    Structures without sample nodes yield [].
    This does NOT invent I/O pairs from COUNT/QUANTITY topology alone —
    that would require selecting an executable transformation (forbidden).
    """
    out: List[FormalObservation] = []
    for n in ss.nodes:
        if n.role not in ("SAMPLE", "OBSERVED", "EXAMPLE"):
            continue
        attrs = dict(n.attributes)
        # scalar pair
        inp_val = None
        out_val = None
        for ik in ("input", "in", "x", "arg"):
            if ik in attrs:
                inp_val = attrs[ik]
                break
        for ok in ("output", "out", "y", "result"):
            if ok in attrs:
                out_val = attrs[ok]
                break
        if inp_val is None or out_val is None:
            continue
        inputs = (("x", inp_val),)
        oid = _oid(inputs, out_val, ss.structure_id, (n.node_id,), "sample_node.v1")
        out.append(FormalObservation(
            observation_id=oid,
            inputs=inputs,
            output=out_val,
            source_structure_id=ss.structure_id,
            source_node_ids=(n.node_id,),
            rule_id="sample_node.v1",
            provenance=(
                ("builder", "observations_from_structure.v1"),
                ("role", n.role),
            ),
        ))
    return out


# ---------------------------------------------------------------------------
# M+28.18 — Relational composition over typed SemanticEdges
# ---------------------------------------------------------------------------
# Rules operate on relation *types*, not lexical terms.
# Transitive: SAME_AS, PART_OF (directed)
# Symmetric: SAME_AS
# Contradiction: SAME_AS ∧ DIFFERENT_FROM on same pair

_TRANSITIVE_RELATIONS = frozenset({"SAME_AS", "PART_OF", "SAME"})
_SYMMETRIC_RELATIONS = frozenset({"SAME_AS", "SAME"})
_CONTRADICT_PAIRS = frozenset({
    ("SAME_AS", "DIFFERENT_FROM"),
    ("DIFFERENT_FROM", "SAME_AS"),
    ("SAME", "DIFFERENT"),
    ("DIFFERENT", "SAME"),
})


def compose_relations(ss: SemanticStructure) -> SemanticStructure:
    """Derive new edges by relational composition; return enriched structure.

    Does not remove premises. Derived edges carry provenance of the rule.
    Contradictions are recorded as CONTRADICTION edges (inspectable).
    """
    nodes = [n.as_dict() for n in ss.nodes]
    edges = [e.as_dict() for e in ss.edges]
    existing = {(e["source"], e["relation"], e["target"]) for e in edges}
    node_ids = {n["node_id"] for n in nodes}

    # Adjacency per relation
    adj: Dict[str, Dict[str, set]] = {}
    for e in ss.edges:
        adj.setdefault(e.relation, {}).setdefault(e.source, set()).add(e.target)
        if e.relation in _SYMMETRIC_RELATIONS:
            adj.setdefault(e.relation, {}).setdefault(e.target, set()).add(e.source)

    derived: List[Dict[str, Any]] = []

    def _add(src: str, rel: str, tgt: str, rule: str, depth: int) -> None:
        if src == tgt:
            return
        key = (src, rel, tgt)
        if key in existing:
            return
        if src not in node_ids or tgt not in node_ids:
            return
        existing.add(key)
        derived.append({
            "source": src,
            "relation": rel,
            "target": tgt,
            # attributes not on SemanticEdge; provenance via structure
        })
        # track meta on a side channel via edge relation only; rule in structure provenance

    # Transitive closure for each transitive relation
    for rel in _TRANSITIVE_RELATIONS:
        if rel not in adj:
            continue
        reach = {n: set(adj[rel].get(n, ())) for n in node_ids}
        # include reverse for symmetric
        if rel in _SYMMETRIC_RELATIONS:
            for n in list(node_ids):
                for m in list(reach.get(n, ())):
                    reach.setdefault(m, set()).add(n)
        changed = True
        depth = 0
        while changed and depth < 16:
            changed = False
            depth += 1
            for n in node_ids:
                add: set = set()
                for m in list(reach.get(n, ())):
                    add |= reach.get(m, set())
                add -= reach.get(n, set())
                add.discard(n)
                if add:
                    reach.setdefault(n, set()).update(add)
                    changed = True
        direct = set()
        for e in ss.edges:
            if e.relation == rel:
                direct.add((e.source, e.target))
                if rel in _SYMMETRIC_RELATIONS:
                    direct.add((e.target, e.source))
        for a in node_ids:
            for b in sorted(reach.get(a, ())):
                if a == b:
                    continue
                if (a, b) in direct:
                    continue
                # only add a<b for symmetric to reduce dupes when both directions
                if rel in _SYMMETRIC_RELATIONS and a > b:
                    continue
                _add(a, rel, b, f"transitive.{rel}.v1", depth)

    # Contradictions: same pair has conflicting relations
    pair_rels: Dict[Tuple[str, str], set] = {}
    for e in list(ss.edges) + [
        type("E", (), {"source": d["source"], "relation": d["relation"], "target": d["target"]})()
        for d in derived
    ]:
        a, b = e.source, e.target
        key = (a, b) if a <= b else (b, a)
        pair_rels.setdefault(key, set()).add(e.relation)
        if e.relation in _SYMMETRIC_RELATIONS:
            pair_rels.setdefault(key, set()).add(e.relation)

    for (a, b), rels in pair_rels.items():
        for r1, r2 in _CONTRADICT_PAIRS:
            if r1 in rels and r2 in rels:
                key = (a, "CONTRADICTION", b)
                if key not in existing and a in node_ids and b in node_ids:
                    existing.add(key)
                    derived.append({
                        "source": a,
                        "relation": "CONTRADICTION",
                        "target": b,
                    })

    all_edges = edges + derived
    return build_structure(
        nodes=nodes,
        edges=all_edges,
        evidence_ids=list(ss.source_evidence_ids) + [f"compose:{ss.structure_id}"],
        provenance={
            "builder": "compose_relations.v1",
            "parent_structure_id": ss.structure_id,
            "parent_fingerprint": ss.fingerprint(),
            "rules": ["transitive.SAME_AS", "transitive.PART_OF", "transitive.SAME",
                      "contradiction.SAME_AS/DIFFERENT_FROM"],
            "derived_edge_count": len(derived),
        },
        structure_id=_id_for(
            [SemanticNode.from_dict(n) for n in nodes],
            [SemanticEdge.from_dict(e) for e in all_edges],
        ),
    )


# ---------------------------------------------------------------------------
# M+28.20 — Candidate structural properties & constraint satisfaction
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class CandidateStructuralProperty:
    """Operation-independent structural properties of a candidate plan."""
    candidate_id: str
    param_count: int
    step_count: int
    depth: int
    literals_used: Tuple[Any, ...]
    params_used: Tuple[str, ...]
    uses_literal: bool
    provenance: Tuple[Tuple[str, Any], ...] = ()

    def as_dict(self) -> Dict[str, Any]:
        return {
            "candidate_id": self.candidate_id,
            "param_count": self.param_count,
            "step_count": self.step_count,
            "depth": self.depth,
            "literals_used": list(self.literals_used),
            "params_used": list(self.params_used),
            "uses_literal": self.uses_literal,
            "provenance": dict(self.provenance),
        }


@dataclass(frozen=True)
class CandidateConstraintEvaluation:
    candidate_id: str
    constraint_source_id: str
    satisfied: str  # "yes" | "no" | "unknown"
    reasons: Tuple[str, ...]
    properties: CandidateStructuralProperty
    provenance: Tuple[Tuple[str, Any], ...] = ()

    def as_dict(self) -> Dict[str, Any]:
        return {
            "candidate_id": self.candidate_id,
            "constraint_source_id": self.constraint_source_id,
            "satisfied": self.satisfied,
            "reasons": list(self.reasons),
            "properties": self.properties.as_dict(),
            "provenance": dict(self.provenance),
        }


def _walk_args(obj: Any, literals: List[Any], params: List[str]) -> None:
    if isinstance(obj, dict):
        if "$param" in obj:
            params.append(str(obj["$param"]))
        elif "$step" in obj:
            pass
        else:
            for v in obj.values():
                _walk_args(v, literals, params)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            _walk_args(v, literals, params)
    elif isinstance(obj, (int, float)) and not isinstance(obj, bool):
        literals.append(obj)
    elif isinstance(obj, str) and obj.isdigit():
        literals.append(int(obj))


def analyze_candidate_structure(candidate: Any) -> CandidateStructuralProperty:
    """Extract structural properties from Hypothesis or plan dict.

    Does not interpret which primitive is "correct" — only counts structure.
    """
    plan = candidate.plan if hasattr(candidate, "plan") else candidate
    if not isinstance(plan, dict):
        plan = {}
    steps = plan.get("steps") or []
    params_decl = plan.get("params") or {}
    lits: List[Any] = []
    pars: List[str] = []
    for st in steps:
        _walk_args(st.get("args") or {}, lits, pars)
    # unique preserve order
    def uniq(seq):
        seen = set()
        out = []
        for x in seq:
            k = (type(x).__name__, x)
            if k not in seen:
                seen.add(k)
                out.append(x)
        return out
    lits_u = uniq(lits)
    pars_u = uniq(pars)
    cid = getattr(candidate, "derivation", None) or plan.get("name") or "candidate"
    if not isinstance(cid, str):
        cid = str(cid)[:40]
    return CandidateStructuralProperty(
        candidate_id=cid,
        param_count=len(params_decl) if params_decl else len(pars_u),
        step_count=len(steps),
        depth=len(steps),  # flat plans: depth ≈ steps
        literals_used=tuple(lits_u),
        params_used=tuple(str(p) for p in pars_u),
        uses_literal=len(lits_u) > 0,
        provenance=(("builder", "analyze_candidate_structure.v1"),),
    )


def evaluate_candidate_constraint(
        candidate: Any,
        constraint: SemanticSearchConstraint,
) -> CandidateConstraintEvaluation:
    """Test whether candidate structure satisfies generic semantic constraint.

    Rules (operation-blind):
    - if constraint.rejected → no
    - if constraint.extra_literals non-empty → candidate should use ≥1 of them
      (unknown if candidate has no literal slots)
    - if constraint.param_names → candidate param_count should be ≥ 1 when names set
    """
    props = analyze_candidate_structure(candidate)
    reasons: List[str] = []
    if constraint.rejected:
        return CandidateConstraintEvaluation(
            candidate_id=props.candidate_id,
            constraint_source_id=constraint.source_structure_id,
            satisfied="no",
            reasons=("constraint rejected: " + (constraint.rejection_reason or "contradiction"),),
            properties=props,
            provenance=(("builder", "evaluate_candidate_constraint.v1"),),
        )
    sat = "yes"
    if constraint.extra_literals:
        needed = set(constraint.extra_literals)
        used = set(props.literals_used)
        if not props.uses_literal:
            sat = "unknown"
            reasons.append("constraint requires literals but candidate uses none")
        elif needed.isdisjoint(used):
            sat = "no"
            reasons.append("candidate literals do not intersect constraint extra_literals")
        else:
            reasons.append("literal intersection satisfied")
    if constraint.param_names:
        if props.param_count < 1:
            sat = "no" if sat == "yes" else sat
            reasons.append("constraint requires params; candidate has none")
        else:
            reasons.append("param_count ok")
    if not reasons:
        reasons.append("no structural filters applied")
    return CandidateConstraintEvaluation(
        candidate_id=props.candidate_id,
        constraint_source_id=constraint.source_structure_id,
        satisfied=sat,
        reasons=tuple(reasons),
        properties=props,
        provenance=(("builder", "evaluate_candidate_constraint.v1"),),
    )


