"""
swarm_engine/capability/primitive_promotion.py

PrimitivePromoter: takes a verified Expr (a composition GeneralSynthesizer
already proved matches its examples) and crystallizes it into a genuinely
new, named, registered Primitive — not a lookup-table shortcut, an actual
entry in the same PrimitiveRegistry every other primitive lives in, so
GeneralSynthesizer's own search can compose THROUGH it in later, unrelated
problems.

The honest boundary, stated once here: promotion does NOT expand what is
computationally reachable. A promoted primitive's function body still just
calls existing, already-trusted primitives — ExpressivenessAnalyzer's
type-reachability closure is mathematically unchanged by any amount of
promotion. What promotion actually changes:

  1. Practical reachability within ExpressivenessAnalyzer's ROUND-BOUNDED
     closure computation — collapsing an N-step composition into one
     primitive can bring something within the round limit that needed more
     rounds before.
  2. Practical reachability within GeneralSynthesizer's CANDIDATE-BUDGET-
     BOUNDED search — a composition that needed several specific
     intermediate values to simultaneously survive pool-per-type truncation
     (a low-probability joint event on a cold search) becomes a single,
     bias-reinforced, reliably-found atomic lookup instead.

Naming is deterministic and hash-derived, never a guessed semantic label —
promoting power(x, 2) does not get called "square"; SWarm has no way to know
that name means anything.

Independent re-verification, not self-certification: registering a
primitive never trusts the caller's claim that the underlying Expr was
already verified. The exact same evidence is re-checked against the NEW
primitive's own calling convention before it is ever registered.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

from swarm_engine.cognition.representations import Expr, evaluate_expr
from swarm_engine.primitives.core import Effect, Primitive, TypeSpec, infer


@dataclass
class PromotionRecord:
    name: str
    expr: Expr
    param_names: Tuple[str, ...]
    input_kinds: Dict[str, str]
    output_kind: str
    source_goal: str
    support: int = 1
    provenance: Dict[str, Any] = field(default_factory=dict)
    created_at: float = field(default_factory=time.time)
    # Epistemic standing: the governed grounding laws this promotion was
    # verified from, as [{"predicate":..., "semantic_id":...}]. Empty means
    # not law-derived (e.g. promoted from a cognition synthesis); the
    # evidence-driven revocation sync only ever assesses law-derived
    # promotions, so law-agnostic ones are never touched by it.
    law_links: List[Dict[str, str]] = field(default_factory=list)
    # "active" | "quarantined". Quarantine (never delete) preserves the
    # audit trail and lets a later re-promotion from new-regime evidence
    # replace the record cleanly.
    status: str = "active"
    quarantine_detail: Optional[Dict[str, Any]] = None

    def as_dict(self) -> Dict[str, Any]:
        return {"name": self.name, "expr": self.expr.as_dict(),
                "param_names": self.param_names, "input_kinds": self.input_kinds,
                "output_kind": self.output_kind, "source_goal": self.source_goal,
                "support": self.support, "provenance": self.provenance,
                "created_at": self.created_at, "law_links": self.law_links,
                "status": self.status,
                "quarantine_detail": self.quarantine_detail}


class PromotedPrimitiveStore:
    def __init__(self, db_path: str = "swarm_engine.db"):
        self.db_path = db_path
        with self._conn() as conn:
            conn.execute("""CREATE TABLE IF NOT EXISTS promoted_primitives (
                name TEXT PRIMARY KEY, data TEXT NOT NULL, updated_at REAL)""")

    def _conn(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def save(self, record: PromotionRecord) -> None:
        with self._conn() as conn:
            conn.execute("""INSERT OR REPLACE INTO promoted_primitives
                (name, data, updated_at) VALUES (?,?,?)""",
                (record.name, json.dumps(record.as_dict()), time.time()))

    def get(self, name: str) -> Optional[PromotionRecord]:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT data FROM promoted_primitives WHERE name=?",
                (name,)).fetchone()
        return self._row(row) if row else None

    @staticmethod
    def _row(row: Any) -> PromotionRecord:
        d = json.loads(row["data"])
        # .get() defaults: rows persisted before law-links / quarantine
        # existed are treated as active with no law attribution.
        return PromotionRecord(
            name=d["name"], expr=Expr.from_dict(d["expr"]),
            param_names=tuple(d["param_names"]), input_kinds=d["input_kinds"],
            output_kind=d["output_kind"], source_goal=d["source_goal"],
            support=d.get("support", 1), provenance=d.get("provenance", {}),
            created_at=d.get("created_at", 0.0),
            law_links=d.get("law_links", []),
            status=d.get("status", "active"),
            quarantine_detail=d.get("quarantine_detail"))

    def all(self) -> List[PromotionRecord]:
        with self._conn() as conn:
            rows = conn.execute("SELECT data FROM promoted_primitives").fetchall()
        return [self._row(r) for r in rows]


_KIND_TO_TYPESPEC = None


def _kind_typespec(kind_name: str) -> TypeSpec:
    global _KIND_TO_TYPESPEC
    if _KIND_TO_TYPESPEC is None:
        from swarm_engine.primitives.core import Kind
        _KIND_TO_TYPESPEC = {k.value: TypeSpec(kind=k) for k in Kind}
    return _KIND_TO_TYPESPEC.get(kind_name, list(_KIND_TO_TYPESPEC.values())[0])


def _dedupe_law_links(
        links: Optional[Sequence[Dict[str, str]]]) -> List[Dict[str, str]]:
    """Canonicalize law links; identity is (predicate, semantic_id)."""
    seen: Set[Tuple[Optional[str], Optional[str]]] = set()
    out: List[Dict[str, str]] = []
    for l in links or []:
        key = (l.get("predicate"), l.get("semantic_id"))
        if key not in seen:
            seen.add(key)
            out.append({"predicate": key[0], "semantic_id": key[1]})
    return out


class PrimitivePromoter:
    """Crystallizes a verified Expr into a real, registered Primitive."""

    PROMOTION_BIAS_REINFORCEMENT = 3
    # An explicit, stated design choice, not hidden: a primitive earns
    # boosted bias the moment it is promoted, on the reasoning that
    # promotion itself already required real verified evidence. Starting it
    # at neutral bias would waste the entire point of promotion, which is
    # to make it preferentially survive pool-per-type truncation later.

    def __init__(self, registry, store: PromotedPrimitiveStore, bias,
                provenance=None):
        self.reg = registry
        self.store = store
        self.bias = bias
        self.provenance = provenance

    def promote(self, expr: Expr, param_names: Sequence[str],
               examples: Sequence[Tuple[Dict[str, Any], Any]],
               source_goal: str,
               law_links: Optional[Sequence[Dict[str, str]]] = None
               ) -> Optional[str]:
        name = self._deterministic_name(expr, param_names)
        links = _dedupe_law_links(law_links)
        if name in self.reg:
            # Already registered: the computation stands; fold in any new
            # epistemic justification so the record reflects every law
            # whose verified program contained this structure.
            if links:
                self.merge_law_links(name, links)
            return name

        for args, expected in examples:
            try:
                value = evaluate_expr(expr, {p: args[p] for p in param_names}, self.reg)
            except Exception:
                return None
            if value != expected:
                return None

        input_kinds = {p: infer(examples[0][0][p]).kind.value for p in param_names}
        output_kind = infer(examples[0][1]).kind.value

        def closure(**kwargs):
            return evaluate_expr(expr, kwargs, self.reg)

        prim = Primitive(
            name=name, family="promoted", fn=closure,
            inputs={p: _kind_typespec(input_kinds[p]) for p in param_names},
            output=_kind_typespec(output_kind), effects=(Effect.PURE,),
            doc=f"promoted from verified composition: {' -> '.join(expr.ops_used())}")

        for args, expected in examples:
            try:
                value = prim.fn(**{p: args[p] for p in param_names})
            except Exception:
                return None
            if value != expected:
                return None

        self.reg.register(prim)
        self.bias.record((name,) * (self.PROMOTION_BIAS_REINFORCEMENT + 1), True)

        record = PromotionRecord(
            name=name, expr=expr, param_names=tuple(param_names),
            input_kinds=input_kinds, output_kind=output_kind,
            source_goal=source_goal, law_links=links, status="active",
            provenance={"underlying_ops": list(expr.ops_used())})
        self.store.save(record)

        if self.provenance is not None:
            from swarm_engine.governance.provenance import (
                Origin, ProvenanceRecord, TrustLevel,
            )
            self.provenance.record(ProvenanceRecord(
                capability_id=f"primitive:{name}", origin=Origin.SYNTHESIZED,
                trust=TrustLevel.TESTED, source="primitive_promotion",
                primitives_used=list(expr.ops_used())))
            self.provenance.log(f"primitive:{name}", "promoted",
                                f"from goal {source_goal!r}: "
                                f"{' -> '.join(expr.ops_used())}")

        return name

    def merge_law_links(self, name: str,
                        links: Sequence[Dict[str, str]]) -> bool:
        """Fold additional law-justification links into a stored record.

        Used when an already-registered computation recurs in another
        law's verified program: the promotion gains justification without
        re-registration. Never changes status by itself. Returns True when
        the record changed.
        """
        links = _dedupe_law_links(links)
        if not links:
            return False
        rec = self.store.get(name)
        if rec is None:
            return False
        merged = _dedupe_law_links(list(rec.law_links) + links)
        if len(merged) == len(rec.law_links):
            return False
        rec.law_links = merged
        self.store.save(rec)
        return True

    def rehydrate(self) -> List[str]:
        restored = []
        for record in self.store.all():
            if record.status != "active":
                # Epistemically quarantined: a fresh boot must never
                # resurrect a promotion whose justification was revoked.
                continue
            if record.name in self.reg:
                continue
            expr = record.expr

            # Bind expr at definition time: a bare closure over the loop
            # variable would make every rehydrated primitive evaluate the
            # LAST record's expression (classic closure-in-a-loop bug),
            # corrupting all but one and recursing infinitely when the last
            # expression references an earlier promoted primitive.
            def make_closure(bound_expr):
                def closure(**kwargs):
                    return evaluate_expr(bound_expr, kwargs, self.reg)
                return closure

            prim = Primitive(
                name=record.name, family="promoted", fn=make_closure(expr),
                inputs={p: _kind_typespec(record.input_kinds[p])
                       for p in record.param_names},
                output=_kind_typespec(record.output_kind), effects=(Effect.PURE,),
                doc=f"promoted (rehydrated): {' -> '.join(record.expr.ops_used())}")
            self.reg.register(prim, overwrite=True)
            self.bias.record((record.name,) * (self.PROMOTION_BIAS_REINFORCEMENT + 1), True)
            restored.append(record.name)
        return restored

    def _deterministic_name(self, expr: Expr, param_names: Sequence[str]) -> str:
        basis = expr.canonical() + "|" + ",".join(param_names)
        digest = hashlib.sha256(basis.encode()).hexdigest()[:12]
        return f"promoted_{digest}"
