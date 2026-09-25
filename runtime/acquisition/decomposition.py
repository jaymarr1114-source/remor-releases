"""
swarm_engine/acquisition/decomposition.py

Behavioral gap decomposition (P8): split one scalar-output requirement into
independently acquirable behavioral sub-requirements, discovered from the
requirement's own worked examples -- never from goal text.

Where the structural decomposer (gap_reasoner._match_composite_output) splits
by output SHAPE (tuple/list/dict element), this module splits by output
VALUE: when the target value vector is exactly the elementwise sum of a few
small-expression value vectors (an additive fold), or a unary op applied to
such a sum (a nest chain), each summand becomes a child requirement whose
projected examples are the parent's inputs paired with the summand's values.
The children are then acquired by the normal recursive machinery (GENERATE /
COMPOSE / ...); the parent is re-assembled by folding the acquired children
with a re-discovered combining op, validated exactly on the parent's
examples before admission.

v1+recursion bounds (stated, not TODOs):
  * numeric scalar outputs (int/float) via additive folds; bit-domain
    outputs (bool / {0,1} int, cross-representation) via xor folds;
  * additive/xor folds and single-...-deep nest chains (max depth 3);
  * at most 7 components per decomposition;
  * recursive compound-residual children when flat arity exceeds
    HIERARCHY_FLAT_TRIGGER (general recursive extension; preserves the
    P8 depth-1 2-/3-way band);
  * one best contract per analysis (fewest components, then shallowest);
  * the component pool is drawn from the EXISTING generator grammar --
    the same productions the enumerative synthesizer can emit within its
    bounded grammar: literal vocabulary, unary/binary pure-numeric
    primitive applications over parameters (which subsume the fixed
    numeric skeletons: square=power(x,2), double=multiply(2,x),
    negate=negate(x), add_two=add(x,2), multiply_two=multiply(x,2),
    subtract_two=subtract(x,2), abs_value=abs(x)), affine evidence fits,
    and degree 2-4 polynomial evidence fits -- plus one bounded level of
    unary chaining ("bounded two-stage compositions"). No acquired-family
    primitive ever enters the pool: decomposition children must be
    independently acquirable, never aliases of existing capabilities.

Anti-hardcode rule (hard): no branch on objective names, capability names,
goal words, or formula literals anywhere in this module. The decisive
objectives (2^x+3^x+x^2 and friends) appear NOWHERE here -- not as
literals, not as special cases, not as comments describing them. The pool
is generic; the search is generic; exact == on the requirement's own
examples is the only acceptance criterion.

The contract carries NO composition plan, NO Expr, and NO code: only the
reconstructor kind, the child names in application order, and the
`resolved_primitives` map the reasoner fills in. Re-assembly re-discovers
the combining op empirically (exact behavioral validation on the parent's
examples), exactly as pair-assembly does for structural decompositions.
"""
from __future__ import annotations

import hashlib
import json
import math
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

from swarm_engine.cognition.synthesis import (
    _is_numeric_input_type, _is_numeric_output_prim)
from swarm_engine.pow_safety import _pow_safe_scalar
from swarm_engine.primitives.core import Effect

try:
    import numpy as _np
except Exception:  # pragma: no cover - numpy is a hard venv dependency
    _np = None


# ---------------------------------------------------------------------------
# v1 bounds and constants
# ---------------------------------------------------------------------------

#: Maximum components in one decomposition (design v1 bound).
MAX_COMPONENTS = 7
#: Maximum nesting depth of the decomposition search (design v1 bound).
MAX_DEPTH = 3
#: Default wall-clock budget (seconds) for one decompose() call.
DEFAULT_WALL_CLOCK_S = 30.0
#: Candidate literal vocabulary: generic small integers. This plays the same
#: role as the fixed literal set in the enumerative synthesizer; it is NOT
#: tuned to any objective (the pool additionally mines constants from the
#: examples themselves via the evidence-fit productions).
LITERAL_VOCAB: Tuple[int, ...] = tuple(range(-5, 11))
#: Single magnitude constant for pool vectors (mirrors
#: pow_safety._POW_MAG_CAP; imported there, not duplicated in spirit -- the
#: literal matches so the bound is identical).
MAGNITUDE_CAP = 10 ** 18
#: Fixed determinism salt: a constant string, never random, never derived
#: from goal text. Two analyses of the same examples must produce
#: byte-identical contracts.
DETERMINISM_SALT = "p8-gap-decomposition-v1"
#: Cap on exact alternatives counted per winning (components, depth); bounds
#: the counting work on highly ambiguous inputs. Recorded in provenance.
MAX_ALTERNATIVES_COUNTED = 256
#: Flat folds with k <= this prefer the all-pool partition (P8 depth-1
#: band). Hierarchical compound-residual candidates compete only when the
#: flat best has more components than this, or no flat partition exists.
#: This is an arity threshold on the flat answer -- not a depth-2 special
#: case -- and preserves proven 2- and 3-way depth-1 folds bit-for-bit.
HIERARCHY_FLAT_TRIGGER = 3
#: Maximum recursive compound-residual depth (parent -> child -> ...).
MAX_RECURSION_DEPTH = 8
# Max number of literal-bound extras for partial application
# (arity = |num_in| + k, 1 <= k <= MAX_PARTIAL_EXTRA). Caps combinatorial
# product(|lits|^k * C(arity,k) * |num_in|!) while allowing multi-bind.
MAX_PARTIAL_EXTRA = 3


class _Deadline(Exception):
    """Internal: the wall-clock budget for decompose() expired."""


def _is_num_scalar(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _canon(v: Any) -> Any:
    """Canonicalize a numeric scalar: integral floats become ints.

    Exact -- no tolerance, no rounding of non-integral values. Two value
    vectors compare with == only after this canonicalization.
    """
    if isinstance(v, float) and v.is_integer():
        return int(v)
    return v


def _is_bit_scalar(v: Any) -> bool:
    """True for bool or integer {0,1} (cross-representation bit domain)."""
    if isinstance(v, bool):
        return True
    return isinstance(v, int) and not isinstance(v, bool) and v in (0, 1)


def _canon_bit(v: Any) -> bool:
    """Unify bool / {0,1} int into a bool bit (cross-representation)."""
    if isinstance(v, bool):
        return v
    return bool(v)


def _is_list_value(v: Any) -> bool:
    """True for list or tuple sequences (structured monoid elements)."""
    return isinstance(v, (list, tuple))


def _canon_list(v: Any) -> Tuple[Any, ...]:
    """Hashable list canon: tuples (nested lists/dicts recursively canonized)."""
    def _elem(x: Any) -> Any:
        if isinstance(x, dict):
            return _canon_dict(x)
        if _is_list_value(x):
            return _canon_list(x)
        return x
    if isinstance(v, tuple):
        return tuple(_elem(x) for x in v)
    if isinstance(v, list):
        return tuple(_elem(x) for x in v)
    return v  # type: ignore[return-value]


def _is_dict_value(v: Any) -> bool:
    """True for plain dicts (structured merge-monoid elements)."""
    return isinstance(v, dict)


def _canon_dict(v: Any) -> Tuple[Tuple[Any, Any], ...]:
    """Hashable dict canon: sorted (k, canon(v)) pairs."""
    if not isinstance(v, dict):
        raise TypeError("canon_dict expects dict")
    items = []
    for k in sorted(v.keys(), key=lambda x: (str(type(x)), str(x))):
        val = v[k]
        if _is_dict_value(val):
            val = _canon_dict(val)
        elif _is_list_value(val):
            val = _canon_list(val)
        items.append((k, val))
    return tuple(items)


def _list_from_canon(c: Any) -> list:
    """Inverse of _canon_list: restore lists; dict-shaped elems become dicts."""
    if not isinstance(c, tuple):
        return list(c) if isinstance(c, list) else [c]
    out = []
    for x in c:
        if (isinstance(x, tuple) and x
                and all(isinstance(p, tuple) and len(p) == 2 for p in x)):
            # Nested dict canon (tuple-of-pairs).
            out.append(_dict_from_canon(x))  # type: ignore[arg-type]
        elif isinstance(x, tuple):
            out.append(_list_from_canon(x))
        else:
            out.append(x)
    return out


def _dict_from_canon(c: Tuple[Tuple[Any, Any], ...]) -> dict:
    """Inverse of _canon_dict for residual assembly (preserves nested types)."""
    out = {}
    for k, val in c:
        if isinstance(val, tuple) and val and isinstance(val[0], tuple) and len(val[0]) == 2:
            # Nested dict canon (tuple-of-pairs) vs list-of-pairs ambiguity:
            # prefer dict (matches _canon_dict nesting). List-of-pairs as a
            # dict value remains a known edge; projection-from-originals
            # avoids it for child examples.
            try:
                if all(isinstance(p, tuple) and len(p) == 2 for p in val):
                    out[k] = _dict_from_canon(val)  # type: ignore[arg-type]
                    continue
            except Exception:
                pass
        if isinstance(val, tuple):
            # List canon stored under a dict key.
            out[k] = _list_from_canon(val)
            continue
        out[k] = val
    return out


def _deep_merge_dicts(a: Any, b: Any) -> dict:
    """Recursive dict merge; b wins on non-dict leaves (deep_merge prim)."""
    if isinstance(a, tuple):
        a = dict(a)
    if isinstance(b, tuple):
        b = dict(b)
    out = dict(a)
    for k, v in b.items():
        if (k in out and _is_dict_value(out[k]) and _is_dict_value(v)):
            out[k] = _deep_merge_dicts(out[k], v)
        else:
            out[k] = v
    return out


@dataclass(frozen=True)
class _FoldAlgebra:
    """Associative combining algebra for behavioral fold search.

    Numeric add (group under +), bit xor (group under ^, self-inverse
    residual), string concat (monoid under +, prefix residual), list/
    tuple concat (monoid under +, prefix residual; elements canonicalized
    to tuples for hashability), and dict merge (monoid under shallow
    merge with key-disjoint residual) share the residual-search machinery.
    Cross-representation bit domain accepts bool and {0,1} int
    interchangeably via `_canon_bit`. String/list/tuple residual is
    prefix-peel; dict residual is key-subset peel (part items must match
    total on shared keys). Non-commutative monoids use k=2 scan.
    """
    op_name: str          # registered binary primitive name
    domain: str           # "numeric"|"bit"|"string"|"list"|"tuple"|"dict"

    def is_scalar(self, v: Any) -> bool:
        if self.domain == "numeric":
            return _is_num_scalar(v)
        if self.domain == "bit":
            return _is_bit_scalar(v)
        if self.domain == "list":
            return _is_list_value(v)
        if self.domain == "tuple":
            return isinstance(v, tuple)
        if self.domain == "dict":
            return _is_dict_value(v)
        return isinstance(v, str)

    def canon(self, v: Any) -> Any:
        if self.domain == "numeric":
            return _canon(v)
        if self.domain == "bit":
            return _canon_bit(v)
        if self.domain in ("list", "tuple"):
            return _canon_list(v)
        if self.domain == "dict":
            return _canon_dict(v)
        return v  # strings: exact

    def combine(self, a: Any, b: Any) -> Any:
        if self.domain == "numeric":
            return a + b
        if self.domain == "bit":
            return bool(a) ^ bool(b)
        if self.domain in ("list", "tuple"):
            return tuple(a) + tuple(b)
        if self.domain == "dict":
            # v39: deep_merge (b wins on leaves; recursive on dict values).
            # Disjoint-key case matches shallow merge; nested overlap keeps
            # structure (no silent whole-key overwrite).
            return _canon_dict(_deep_merge_dicts(a, b))
        return a + b  # string concat

    def residual(self, total: Any, part: Any) -> Any:
        # total == combine(part, residual(total, part)); None = inapplicable
        if self.domain == "numeric":
            return total - part
        if self.domain == "bit":
            return bool(total) ^ bool(part)
        if self.domain in ("list", "tuple"):
            if not _is_list_value(total) or not _is_list_value(part):
                return None
            t, p = tuple(total), tuple(part)
            if len(p) <= len(t) and t[:len(p)] == p:
                return t[len(p):]
            return None
        if self.domain == "dict":
            # Key-subset peel: part items must equal total on shared keys;
            # residual is total without part's keys. Requires no overlap
            # conflict so combine(part, residual)==total under merge.
            if not isinstance(total, tuple) or not isinstance(part, tuple):
                # Allow raw dicts too
                if _is_dict_value(total):
                    total = _canon_dict(total)
                if _is_dict_value(part):
                    part = _canon_dict(part)
            if not isinstance(total, tuple) or not isinstance(part, tuple):
                return None
            tmap = dict(total)
            pmap = dict(part)
            for k, v in pmap.items():
                if k not in tmap or tmap[k] != v:
                    return None
            resid = tuple((k, v) for k, v in total if k not in pmap)
            return resid
        # string prefix peel
        if not isinstance(total, str) or not isinstance(part, str):
            return None
        if total.startswith(part):
            return total[len(part):]
        return None

    def combine_vec(self, u: Tuple[Any, ...], v: Tuple[Any, ...]
                    ) -> Tuple[Any, ...]:
        return tuple(self.combine(a, b) for a, b in zip(u, v))

    def residual_vec(self, t: Tuple[Any, ...], v: Tuple[Any, ...]
                     ) -> Optional[Tuple[Any, ...]]:
        out = []
        for a, b in zip(t, v):
            r = self.residual(a, b)
            if r is None:
                return None
            out.append(r)
        return tuple(out)

    def canon_vec(self, v: Tuple[Any, ...]) -> Tuple[Any, ...]:
        return tuple(self.canon(x) for x in v)


_NUMERIC_ALGEBRA = _FoldAlgebra(op_name="add", domain="numeric")
_BIT_ALGEBRA = _FoldAlgebra(op_name="xor", domain="bit")
_STRING_ALGEBRA = _FoldAlgebra(op_name="concat", domain="string")
_LIST_ALGEBRA = _FoldAlgebra(op_name="list_concat", domain="list")
_TUPLE_ALGEBRA = _FoldAlgebra(op_name="tuple_concat", domain="tuple")
_DICT_ALGEBRA = _FoldAlgebra(op_name="deep_merge", domain="dict")


def _check_deadline(t0: float, budget_s: float) -> None:
    if time.perf_counter() - t0 > budget_s:
        raise _Deadline()


def _json_scalar(v: Any) -> Any:
    if isinstance(v, bool):
        return v
    if isinstance(v, int):
        return v
    if isinstance(v, float):
        return v if math.isfinite(v) else repr(v)
    if isinstance(v, str):
        return v
    if isinstance(v, (list, tuple)):
        return [_json_scalar(x) for x in v]
    if isinstance(v, dict):
        return {str(k): _json_scalar(val) for k, val in sorted(v.items(), key=lambda kv: str(kv[0]))}
    return repr(v)


def _sha256_examples(
        ex: Sequence[Tuple[Dict[str, Any], Any]]) -> str:
    """Content hash tying a contract to its exact evidence (I6)."""
    canon = json.dumps(
        [{"inputs": {k: _json_scalar(v)
                     for k, v in sorted(a.items())},
          "output": _json_scalar(o)} for a, o in ex],
        sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canon.encode()).hexdigest()


# ---------------------------------------------------------------------------
# Contract dataclasses
# ---------------------------------------------------------------------------

@dataclass
class DecomposedChild:
    """One independently acquirable behavioral sub-requirement.

    `projected_examples` are the PARENT's input dicts paired with this
    child's component values -- the same projection idea as the structural
    decomposer's per-part examples, but over value rather than shape.

    `value_vector` is the [v_i] aligned to parent example order (design
    §2.2); derived from projected_examples and stored explicitly for
    JSON-safe contract serialization.

    `sub_contract` (optional): when this child is itself a compound
    residual that further decomposes, the nested DecompositionContract
    whose parent node IS this child (mirrors structural
    `sub_decomposition_or_None`). Leaves have sub_contract=None.
    """
    name: str
    index: int
    description: str
    projected_examples: List[Tuple[Dict[str, Any], Any]] = field(
        default_factory=list)
    value_vector: List[Any] = field(default_factory=list)
    source: str = "behavioral_fold"
    # "behavioral_fold" | "behavioral_nest_inner" |
    # "behavioral_compound_residual" | "behavioral_reuse" |
    # "behavioral_param"
    sub_contract: Optional["DecompositionContract"] = None
    # When source == behavioral_reuse: registered primitive name whose
    # observed value vector matched this child (behavioral match, never
    # a goal/name/hash identity check as the causal reason).
    # When source == behavioral_param: parent input column name whose
    # values equal this child's value vector (param-exact component).
    reuse_primitive: Optional[str] = None
    # When source == behavioral_reuse and prim input names differ from
    # parent params: map prim_input -> parent_param for assembly remap.
    reuse_arg_bind: Optional[Dict[str, str]] = None
    # When source == behavioral_const: the constant scalar shared by every
    # entry of this child's value vector (inlined at assembly; no acquire).
    const_value: Any = None

    def __post_init__(self) -> None:
        if not self.name or not isinstance(self.name, str):
            raise ValueError("DecomposedChild requires a non-empty name")
        if not isinstance(self.index, int) or self.index < 0:
            raise ValueError("DecomposedChild requires index >= 0")
        if not self.description:
            raise ValueError("DecomposedChild requires a description")
        if self.source not in ("behavioral_fold", "behavioral_nest_inner",
                               "behavioral_compound_residual",
                               "behavioral_reuse",
                               "behavioral_param",
                               "behavioral_const",
                               "behavioral_kv"):
            raise ValueError(f"unknown child source {self.source!r}")
        if len(self.projected_examples) < 2:
            raise ValueError("DecomposedChild needs >= 2 projected examples")
        # value_vector: explicit per design §2.2; derive if not supplied.
        if not self.value_vector:
            object.__setattr__(
                self, "value_vector",
                [_json_scalar(o) for _, o in self.projected_examples])
        if self.source in ("behavioral_reuse", "behavioral_param",
                           "behavioral_kv"):
            if not self.reuse_primitive:
                raise ValueError(
                    f"{self.source} child requires reuse_primitive")
        elif self.source == "behavioral_const":
            if not self.value_vector:
                raise ValueError("behavioral_const needs value_vector")
            if any(v != self.value_vector[0] for v in self.value_vector):
                raise ValueError("behavioral_const vector must be constant")
            # Prefer projected output (preserves tuple/dict) over
            # JSON-scalarized value_vector (which listifies tuples).
            const_v = self.projected_examples[0][1] if self.projected_examples else self.value_vector[0]
            object.__setattr__(self, "const_value", const_v)
        elif self.reuse_primitive is not None:
            raise ValueError(
                "reuse_primitive only valid for behavioral_reuse/"
                "behavioral_param children")
        key_sets = set()
        for a, o in self.projected_examples:
            if not isinstance(a, dict) or not a:
                raise ValueError("child projected inputs must be non-empty dicts")
            if not (_is_num_scalar(o) or _is_bit_scalar(o)
                    or isinstance(o, str) or _is_list_value(o)
                    or _is_dict_value(o)
                    or isinstance(o, (set, frozenset, bytes, bytearray))):
                raise ValueError(
                    "child projected outputs must be numeric, bit, str, "
                    "list/tuple, dict, set/frozenset, or bytes")
            key_sets.add(tuple(sorted(a.keys())))
        if len(key_sets) != 1:
            raise ValueError("child projected inputs must be uniform dicts")
        if self.sub_contract is not None:
            if not isinstance(self.sub_contract, DecompositionContract):
                raise ValueError("sub_contract must be a DecompositionContract")
            # The nested parent node IS this child.
            if self.sub_contract.reconstructor.child_refs:
                pass  # names rebound at emit time; checked in graph builder

    def as_dict(self) -> Dict[str, Any]:
        d = {
            "name": self.name,
            "index": self.index,
            "description": self.description,
            "projected_examples": [
                {"inputs": dict(a), "output": o}
                for a, o in self.projected_examples
            ],
            "value_vector": list(self.value_vector),
            "source": self.source,
        }
        if self.sub_contract is not None:
            d["sub_contract"] = self.sub_contract.as_dict()
        if self.reuse_primitive is not None:
            d["reuse_primitive"] = self.reuse_primitive
        if self.reuse_arg_bind is not None:
            d["reuse_arg_bind"] = dict(self.reuse_arg_bind)
        if self.source == "behavioral_const":
            d["const_value"] = self.const_value
        return d

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "DecomposedChild":
        sub = d.get("sub_contract")
        return cls(
            name=d["name"],
            index=int(d["index"]),
            description=d["description"],
            projected_examples=[
                (dict(e["inputs"]), e["output"])
                for e in d["projected_examples"]
            ],
            value_vector=list(d.get("value_vector") or []),
            source=d.get("source", "behavioral_fold"),
            sub_contract=(DecompositionContract.from_dict(sub)
                          if sub else None),
            reuse_primitive=d.get("reuse_primitive"),
            reuse_arg_bind=d.get("reuse_arg_bind"),
            const_value=d.get("const_value"),
        )


@dataclass
class Reconstructor:
    """How the parent is re-assembled from its children.

    Deliberately impoverished: the family, the registered op name(s), and
    the child names in application order. NO composition plan, NO Expr, NO
    code -- reconstruction is re-discovered through the normal fold/nest
    composition machinery and validated exactly on the parent's examples.
    `op` is a primitive NAME ("add" for a fold), never a plan; for
    nest_chain it is the list of outer op specs innermost first: unary names (str) or binary-with-literal dicts {op, free_index, bound}.
    """
    family: str  # fold | nest_chain | structural_wrap | structural_product
    op: Any = "add"  # str folds; List nests; dict wrap/product
    child_refs: List[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.family not in (
                "associative_fold", "nest_chain",
                "structural_wrap", "structural_product",
                "varlen_sequence", "varlen_set", "varlen_bytes",
                "set_algebra"):
            raise ValueError(f"unknown reconstructor family {self.family!r}")
        if self.family == "associative_fold":
            if not isinstance(self.op, str) or not self.op:
                raise ValueError("fold reconstructor op must be a primitive name")
        elif self.family in ("structural_wrap", "structural_product",
                             "varlen_sequence", "varlen_set",
                             "varlen_bytes", "set_algebra"):
            # wrap: {"op": "kv"|"lift"|"lift_tuple", ...}
            # product: {"op": "pack_list"|"pack_tuple"}
            # varlen: {"op": "repeat"|"map_elements"}
            # varlen_set: {"op": "map_to_set"|"range_set"|"as_set_of"}
            # varlen_bytes: {"op": "map_to_bytes"|"as_bytes_of"|"bytes_product"}
            if not (isinstance(self.op, dict) and self.op.get("op")):
                raise ValueError(
                    f"{self.family} op must be a dict with 'op' key")
            if self.family == "structural_product":
                if self.op.get("op") not in (
                        "pack_list", "pack_tuple",
                        "pack_set", "pack_frozenset"):
                    raise ValueError(
                        "structural_product op must be "
                        "pack_list|pack_tuple|pack_set|pack_frozenset")
            if self.family == "varlen_sequence":
                if self.op.get("op") not in ("repeat", "map_elements"):
                    raise ValueError(
                        "varlen_sequence op must be repeat|map_elements")
            if self.family == "varlen_set":
                if self.op.get("op") not in (
                        "map_to_set", "range_set", "as_set_of"):
                    raise ValueError(
                        "varlen_set op must be "
                        "map_to_set|range_set|as_set_of")
            if self.family == "varlen_bytes":
                if self.op.get("op") not in (
                        "map_to_bytes", "as_bytes_of", "bytes_product",
                        "range_bytes"):
                    raise ValueError(
                        "varlen_bytes op must be "
                        "map_to_bytes|as_bytes_of|bytes_product|range_bytes")
            if self.family == "set_algebra":
                if self.op.get("op") not in (
                        "set_union", "set_intersect", "set_difference",
                        "frozenset_union"):
                    raise ValueError(
                        "set_algebra op must be set_union|set_intersect|"
                        "set_difference|frozenset_union")
        else:
            if not isinstance(self.op, list) or not self.op:
                raise ValueError(
                    "nest reconstructor op must be a non-empty list "
                    "(innermost first)")
            for o in self.op:
                if isinstance(o, str) and o:
                    continue
                if (isinstance(o, dict) and o.get("op")
                        and isinstance(o.get("free_index"), int)
                        and isinstance(o.get("bound"), dict)
                        and o["bound"]):
                    continue
                raise ValueError(
                    "nest reconstructor op entries must be unary primitive "
                    "names or binary-with-literal specs "
                    "{op, free_index, bound} (innermost first); "
                    f"got {o!r}")
        if self.family == "structural_wrap":
            if len(self.child_refs) != 1:
                raise ValueError(
                    "structural_wrap needs exactly 1 child ref "
                    f"(got {len(self.child_refs)})")
        elif self.family == "varlen_sequence":
            if len(self.child_refs) != 2:
                raise ValueError(
                    "varlen_sequence needs exactly 2 child refs "
                    f"(got {len(self.child_refs)})")
        elif self.family == "set_algebra":
            if len(self.child_refs) != 2:
                raise ValueError(
                    "set_algebra needs exactly 2 child refs "
                    f"(got {len(self.child_refs)})")
        elif self.family in ("varlen_set", "varlen_bytes"):
            # range_set / as_set_of / as_bytes_of: 1 child
            # map_to_set / map_to_bytes / bytes_product: 2..MAX children
            opname = self.op.get("op")
            if opname in ("range_set", "as_set_of", "as_bytes_of",
                          "range_bytes"):
                if len(self.child_refs) != 1:
                    raise ValueError(
                        f"{self.family}/{opname} needs exactly 1 child ref "
                        f"(got {len(self.child_refs)})")
            elif opname in ("map_to_set", "map_to_bytes"):
                if len(self.child_refs) != 2:
                    raise ValueError(
                        f"{self.family}/{opname} needs exactly 2 child refs "
                        f"(got {len(self.child_refs)})")
            elif opname == "bytes_product":
                if not (2 <= len(self.child_refs) <= MAX_COMPONENTS):
                    raise ValueError(
                        f"bytes_product needs 2..{MAX_COMPONENTS} child refs "
                        f"(got {len(self.child_refs)})")
            else:
                raise ValueError(f"unknown {self.family} op {opname!r}")
        elif not (2 <= len(self.child_refs) <= MAX_COMPONENTS):
            raise ValueError(
                "Reconstructor needs 2..7 child refs "
                f"(got {len(self.child_refs)})")
        if any(not isinstance(c, str) or not c for c in self.child_refs):
            raise ValueError("child refs must be non-empty strings")
        if len(set(self.child_refs)) != len(self.child_refs):
            raise ValueError("child refs must be unique")

    def assembly_dict(self) -> Dict[str, Any]:
        """The constraint dict the reasoner attaches to the parent node.

        Carries "family"/"op"/"child_refs" (the design's schema) plus
        "children" (the key the orchestrator's Gap-A bookkeeping and the
        strategy selector look up); "children" and "child_refs" name the
        same list. "resolved_primitives" starts empty and is filled by the
        reasoner (analysis time) and the orchestrator (acquisition time) --
        child node name -> registered primitive name. Still names only,
        never goal text, never a plan.
        """
        d = {
            "family": self.family,
            "op": (list(self.op) if isinstance(self.op, list)
                   else self.op),
            "children": list(self.child_refs),
            "child_refs": list(self.child_refs),
            "resolved_primitives": {},
        }
        # Structural guarantee for I8: this dict can never smuggle a plan,
        # an Expr, or code -- it is built from fixed keys only.
        json.dumps(d)
        return d

    def as_dict(self) -> Dict[str, Any]:
        return {"family": self.family,
                "op": (list(self.op) if isinstance(self.op, list)
                       else self.op),
                "child_refs": list(self.child_refs)}

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "Reconstructor":
        return cls(family=d["family"], op=d["op"],
                   child_refs=list(d["child_refs"]))


@dataclass
class DecompositionContract:
    """The full decomposition answer for one analysis.

    Invariants (checked in __post_init__):
      I1: 2..MAX_COMPONENTS children; child_refs match children 1:1 in order.
      I2: no acquired-family content -- guaranteed by construction (the pool
          excludes acquired primitives); documented, not re-verifiable here.
      I3: no composition plan / Expr / code anywhere (JSON-safe provenance,
          assembly_dict built from fixed keys).
      I4: children are non-degenerate -- guaranteed by construction (the
          search rejects const-exact and target-exact matches); documented.
      I5: every child's projected examples are uniform and length n_examples.
      I6: determinism_salt is the fixed constant.
      I7: n_examples >= n_components + 2 (evidence sufficiency -- one
          redundant point beyond the vacuity bound; P8-AdjC: n=k+1 admits
          spurious residual coincidences at a measured 3/60, 0/60 at n>=k+2).
    """
    parent_goal: str
    reconstructor: Reconstructor
    children: List[DecomposedChild] = field(default_factory=list)
    n_examples: int = 0
    input_names: List[str] = field(default_factory=list)
    confidence: float = 0.0
    provenance: Dict[str, Any] = field(default_factory=dict)
    policy_version: int = 0
    determinism_salt: str = DETERMINISM_SALT

    def __post_init__(self) -> None:
        if not self.parent_goal:
            raise ValueError("contract requires a parent goal")
        if not isinstance(self.reconstructor, Reconstructor):
            raise ValueError("contract requires a Reconstructor")
        k = len(self.children)
        if self.reconstructor.family == "structural_wrap":
            if k != 1:
                raise ValueError(
                    f"I1: structural_wrap needs exactly 1 child (got {k})")
        elif self.reconstructor.family == "varlen_sequence":
            if k != 2:
                raise ValueError(
                    f"I1: varlen_sequence needs exactly 2 children (got {k})")
        elif self.reconstructor.family == "set_algebra":
            if k != 2:
                raise ValueError(
                    f"I1: set_algebra needs exactly 2 children (got {k})")
        elif self.reconstructor.family in ("varlen_set", "varlen_bytes"):
            opname = (self.reconstructor.op or {}).get("op")
            if opname in ("range_set", "as_set_of", "as_bytes_of",
                          "range_bytes"):
                if k != 1:
                    raise ValueError(
                        f"I1: {self.reconstructor.family}/{opname} needs "
                        f"exactly 1 child (got {k})")
            elif opname in ("map_to_set", "map_to_bytes"):
                if k != 2:
                    raise ValueError(
                        f"I1: {self.reconstructor.family}/{opname} needs "
                        f"exactly 2 children (got {k})")
            elif opname == "bytes_product":
                if not (2 <= k <= MAX_COMPONENTS):
                    raise ValueError(
                        f"I1: bytes_product needs 2..{MAX_COMPONENTS} "
                        f"children (got {k})")
            else:
                raise ValueError(
                    f"I1: unknown {self.reconstructor.family} op {opname!r}")
        elif not (2 <= k <= MAX_COMPONENTS):
            raise ValueError(
                f"I1: contract needs 2..{MAX_COMPONENTS} children (got {k})")
        if [c.name for c in self.children] != list(
                self.reconstructor.child_refs):
            raise ValueError(
                "I1: reconstructor child_refs must match children 1:1 in order")
        if self.n_examples < k + 2:
            raise ValueError(
                f"I7: need n_examples >= components+2 "
                f"(got {self.n_examples} < {k + 2})")
        if self.n_examples < 4:
            raise ValueError("I7: need n_examples >= 4")
        for c in self.children:
            if not isinstance(c, DecomposedChild):
                raise ValueError("children must be DecomposedChild")
            if len(c.projected_examples) != self.n_examples:  # I5
                raise ValueError(
                    "I5: child projected examples must match n_examples")
        if sorted(self.input_names) != list(self.input_names):
            raise ValueError("input_names must be sorted")
        if not (0.0 <= self.confidence <= 1.0):
            raise ValueError("confidence must be in [0, 1]")
        if self.determinism_salt != DETERMINISM_SALT:  # I6
            raise ValueError("I6: determinism_salt must be the fixed constant")
        if not isinstance(self.policy_version, int) or self.policy_version <= 0:
            raise ValueError("policy_version must be a positive int")
        # I3: provenance must be JSON-serializable (no plan/Expr/code smuggling)
        try:
            json.dumps(self.provenance)
        except Exception as exc:
            raise ValueError(f"I3: provenance must be JSON-safe: {exc}")

    def as_dict(self) -> Dict[str, Any]:
        return {
            "parent_goal": self.parent_goal,
            "reconstructor": self.reconstructor.as_dict(),
            "children": [c.as_dict() for c in self.children],
            "n_examples": self.n_examples,
            "input_names": list(self.input_names),
            "confidence": self.confidence,
            "provenance": json.loads(json.dumps(self.provenance)),
            "policy_version": self.policy_version,
            "determinism_salt": self.determinism_salt,
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "DecompositionContract":
        return cls(
            parent_goal=d["parent_goal"],
            reconstructor=Reconstructor.from_dict(d["reconstructor"]),
            children=[DecomposedChild.from_dict(c) for c in d["children"]],
            n_examples=int(d["n_examples"]),
            input_names=list(d["input_names"]),
            confidence=float(d["confidence"]),
            provenance=json.loads(json.dumps(d.get("provenance", {}))),
            policy_version=int(d["policy_version"]),
            determinism_salt=d.get("determinism_salt", DETERMINISM_SALT),
        )


# ---------------------------------------------------------------------------
# The decomposer
# ---------------------------------------------------------------------------

class BehavioralDecomposer:
    """Splits a scalar-output requirement into behavioral components.

    The component pool is drawn from the EXISTING generator grammar (see
    module docstring); the search is residual value-vector closure
    (matching pursuit) for additive folds plus a bounded unary-peel search
    for nest chains; the exact-fit gate demands == on every example.
    One best contract per analysis. Deterministic: same examples in ->
    byte-identical contract out (fixed literal order, sorted primitive
    names, rank-ordered pool, fixed salt).
    """

    def __init__(self, registry,
                 max_components: int = MAX_COMPONENTS,
                 max_depth: int = MAX_DEPTH,
                 wall_clock_s: float = DEFAULT_WALL_CLOCK_S,
                 literals: Sequence[int] = LITERAL_VOCAB):
        self.reg = registry
        self.max_components = max(2, int(max_components))
        self.max_depth = max(1, int(max_depth))
        self.wall_clock_s = float(wall_clock_s)
        self.literals = tuple(literals)
        self._unary: Optional[List[Tuple[str, Any, List[str]]]] = None
        self._binary: Optional[List[Tuple[str, Any, List[str]]]] = None
        # Active fold algebra for the current decompose() call.
        self._algebra: _FoldAlgebra = _NUMERIC_ALGEBRA
        self._force_bit: bool = False

    # -- primitive discovery -------------------------------------------------
    def _prim_lists(self):
        """Pure, numeric-in/numeric-out, non-acquired primitives by arity.

        Mirrors the probe filters (R13 parity): numeric value vectors can
        only be combined by numeric primitives; the acquired family is
        excluded here as OPERATORS (I2-ops) -- pool *expressions* must not
        be built by invoking existing capabilities (that would alias
        synthesis). Acquired capabilities may still contribute as
        behavioral MATCH components via `_reuse_entries` (I2-match), so a
        parent can reuse a composed capability whose value vector exactly
        matches a residual/component. Sorted by name: deterministic
        enumeration order.
        """
        if self._unary is None:
            unary: List[Tuple[str, Any, List[str]]] = []
            binary: List[Tuple[str, Any, List[str]]] = []
            for pname in self.reg.names():
                try:
                    prim = self.reg.get(pname)
                except Exception:
                    continue
                if getattr(prim, "family", "") == "acquired":
                    continue  # I2: never decompose into existing capabilities
                if Effect.PURE not in tuple(
                        getattr(prim, "effects", ()) or ()):
                    continue
                try:
                    inputs = prim.inputs
                    preq = [k for k, v in inputs.items() if not v.optional]
                except Exception:
                    continue
                try:
                    if not all(_is_numeric_input_type(inputs[k])
                               for k in preq):
                        continue
                    if not _is_numeric_output_prim(prim):
                        continue
                except Exception:
                    continue
                if len(preq) == 1:
                    unary.append((pname, prim, preq))
                elif len(preq) == 2:
                    binary.append((pname, prim, preq))
            self._unary, self._binary = unary, binary
        return self._unary, self._binary

    # -- guarded value-vector application ------------------------------------
    def _apply_vec(self, pname: str, prim: Any, preq: List[str],
                   argvecs: List[Tuple[Any, ...]]) -> Optional[Tuple[Any, ...]]:
        """Apply a primitive elementwise to value vectors, guarded.

        Guards (mirroring the probes' R13/R16 parity): per-pair try/except
        (any failure -> the vector is unusable -> None, never partial);
        the _pow_safe_scalar pre-check for power (cheap early filter before
        the primitive's own authoritative guard); the single magnitude cap;
        numeric-scalar outputs only. Canonicalized: integral floats -> int.
        """
        n = len(argvecs[0])
        is_pow = (pname == "power")
        fn = prim.fn
        out: List[Any] = []
        for i in range(n):
            try:
                if is_pow and not _pow_safe_scalar(
                        argvecs[0][i], argvecs[1][i]):
                    return None
                v = fn(**{preq[j]: argvecs[j][i]
                          for j in range(len(preq))})
            except Exception:
                return None
            if not _is_num_scalar(v):
                return None
            try:
                if not math.isfinite(v) or abs(v) > MAGNITUDE_CAP:
                    return None
            except Exception:
                return None
            out.append(_canon(v))
        return tuple(out)

    # -- component pool -------------------------------------------------------
    @staticmethod
    def _dedup(entries: List[Tuple[int, str, Tuple[Any, ...]]
                             ]) -> List[Tuple[Tuple[Any, ...], str]]:
        """Dedupe pool entries by value vector, keeping the lowest rank.

        Observational equivalence pruning (the same family as the
        enumerative synthesizer's bank): two expressions with identical
        outputs on every example are redundant for a value-vector search;
        the simpler (lower-rank) one is kept, so true components -- which
        are small expressions -- are found early in rank order.
        """
        seen: Dict[Tuple[Any, ...], Tuple[int, str]] = {}
        for rank, label, vec in entries:
            if vec not in seen:
                seen[vec] = (rank, label)
        ordered = sorted(seen.items(), key=lambda kv: kv[1][0])
        return [(vec, label) for vec, (_, label) in ordered]

    def _evidence_fits(self, xvec: Tuple[Any, ...], target: Tuple[Any, ...],
                       n: int) -> List[Tuple[str, Tuple[Any, ...]]]:
        """Exact evidence-fit vectors: the source grammar's fit productions.

        Affine fits from example pairs (exact rational arithmetic -- no
        float noise) and degree 2-4 polynomial fits (least-squares, rounded
        coefficients, degeneracy and magnitude guards). A fit is admitted
        to the pool ONLY when it reproduces the target exactly on every
        example: these productions exist so the atomicity fail-fast below
        recognises what the existing generator grammar already reaches.
        """
        from fractions import Fraction
        out: List[Tuple[str, Tuple[Any, ...]]] = []

        def _fr(v: Any) -> Fraction:
            return Fraction(v) if isinstance(v, int) else Fraction(float(v))

        def _num(fr: Fraction) -> Any:
            if fr.denominator == 1:
                return int(fr.numerator)
            f = float(fr)
            return _canon(f)
        # Affine fits from example pairs.
        for i in range(n):
            for j in range(i + 1, n):
                try:
                    xi, xj = _fr(xvec[i]), _fr(xvec[j])
                    dx = xj - xi
                    if dx == 0:
                        continue
                    yi, yj = _fr(target[i]), _fr(target[j])
                    scale = (yj - yi) / dx
                    offset = yi - scale * xi
                    vec = tuple(_num(scale * _fr(xv) + offset)
                                for xv in xvec)
                except Exception:
                    continue
                if len(vec) != n:
                    continue
                if any(not _is_num_scalar(v) or abs(v) > MAGNITUDE_CAP
                       for v in vec):
                    continue
                if tuple(vec) == target:
                    out.append((f"affine_fit(pair {i},{j})", tuple(vec)))
        # Polynomial fits, degrees 2..4.
        if _np is not None:
            xs = [float(v) for v in xvec]
            ys = [float(v) for v in target]
            for deg in (2, 3, 4):
                if n < deg + 2:
                    continue  # verification would be vacuous
                try:
                    V = _np.vander(xs, deg + 1, increasing=True)
                    coef, *_ = _np.linalg.lstsq(V, ys, rcond=None)
                    coef = [round(float(c), 6) for c in coef]
                except Exception:
                    continue
                if abs(coef[-1]) < 1e-9:
                    continue  # degenerate leading term
                try:
                    vec = tuple(
                        _canon(sum(c * (x ** p)
                                   for p, c in enumerate(coef)))
                        for x in xs)
                except Exception:
                    continue
                if any(not _is_num_scalar(v) or abs(v) > MAGNITUDE_CAP
                       for v in vec):
                    continue
                if tuple(vec) == target:
                    out.append((f"poly_fit(deg={deg})", tuple(vec)))
        return out


    def _build_string_pool(
            self, examples: Sequence[Tuple[Dict[str, Any], Any]],
            num_in: List[str], in_names: List[str],
            target: Tuple[Any, ...], counter: List[int]
            ) -> Tuple[List[Tuple[Tuple[Any, ...], str]],
                       List[Tuple[Tuple[Any, ...], str]]]:
        """Tier-A/all string component pool for concat folds.

        Sources (generic, no goal literals): serialize(num_in), str-typed
        input columns, constant strings mined as common prefixes/suffixes
        of the target vector and as single-example suffixes after peeling
        a serialize prefix. Deduped by value; Tier A == Tier all (no
        evidence-fit stage for strings).
        """
        n = len(examples)
        entries: List[Tuple[int, str, Tuple[Any, ...]]] = []

        def _add(label: str, vec: Tuple[Any, ...]) -> None:
            if len(vec) != n or any(not isinstance(x, str) for x in vec):
                return
            entries.append((len(entries), label, vec))
            counter[0] += 1

        # serialize each numeric input (JSON text of the scalar).
        ser = None
        try:
            ser = self.reg.get("serialize")
        except Exception:
            ser = None
        if ser is not None:
            for nm in num_in:
                vec_list = []
                ok = True
                for a, _ in examples:
                    try:
                        v = ser.fn(obj=a[nm])
                    except Exception:
                        ok = False
                        break
                    if not isinstance(v, str):
                        ok = False
                        break
                    vec_list.append(v)
                if ok:
                    _add(f"serialize({nm})", tuple(vec_list))
            # v38: serialize(acquired/promoted numeric unary) — cross-rep
            # residual bridge (numeric fold → string domain) without
            # serialize-everything. Exact value vectors only; no goal names.
            for pname in sorted(self.reg.names()):
                try:
                    prim = self.reg.get(pname)
                except Exception:
                    continue
                if getattr(prim, "family", "") not in ("acquired", "promoted"):
                    continue
                if Effect.PURE not in tuple(getattr(prim, "effects", ()) or ()):
                    continue
                try:
                    pins = [k for k, v in prim.inputs.items() if not v.optional]
                except Exception:
                    continue
                if len(pins) != 1:
                    continue
                for nm in num_in:
                    vec_list = []
                    ok = True
                    for a, _ in examples:
                        try:
                            nv = prim.fn(**{pins[0]: a[nm]})
                            if not _is_num_scalar(nv):
                                ok = False
                                break
                            v = ser.fn(obj=nv)
                        except Exception:
                            ok = False
                            break
                        if not isinstance(v, str):
                            ok = False
                            break
                        vec_list.append(v)
                    if ok:
                        _add(f"serialize(reuse:{pname}|{pins[0]}={nm})",
                             tuple(vec_list))

        # Already-string input columns.
        for nm in in_names:
            if all(isinstance(a[nm], str) for a, _ in examples):
                _add(f"param:{nm}", tuple(a[nm] for a, _ in examples))

        # Mine constant strings: common prefix, common suffix, and empty.
        _add("const:", tuple("" for _ in range(n)))
        if target:
            cp = target[0]
            for s in target[1:]:
                i = 0
                while i < len(cp) and i < len(s) and cp[i] == s[i]:
                    i += 1
                cp = cp[:i]
            if cp:
                _add(f"const:{cp}", tuple(cp for _ in range(n)))
            cs = target[0]
            for s in target[1:]:
                i = 0
                while (i < len(cs) and i < len(s)
                       and cs[-(i + 1)] == s[-(i + 1)]):
                    i += 1
                cs = cs[-i:] if i else ""
            if cs:
                _add(f"const:{cs}", tuple(cs for _ in range(n)))

        # Per-row suffix after peeling serialize(*) prefix (covers
        # str(x)+"!" and serialize(reuse)+"!" when bang is constant).
        ser_labels = [label for _, label, _ in entries
                      if label.startswith("serialize(")]
        for slab in ser_labels:
            ser_vec = None
            for _, label, vec in entries:
                if label == slab:
                    ser_vec = vec
                    break
            if ser_vec is None:
                continue
            suffixes = []
            ok = True
            for t, p in zip(target, ser_vec):
                if not t.startswith(p):
                    ok = False
                    break
                suffixes.append(t[len(p):])
            if ok and suffixes and all(s == suffixes[0] for s in suffixes):
                _add(f"const:{suffixes[0]}", tuple(suffixes))

        tier_a = self._dedup(entries)
        # Tier-B: one concat of Tier-A pairs (ordered). Lets parents rebuild
        # composed string fragments (e.g. serialize(x)+"!") after a reuse
        # target is revoked, so child acquisition can re-decomp those
        # fragments. Bounded: |A|^2, typically < 100.
        entries_b = list(entries)
        for i, (u, lu) in enumerate(tier_a):
            for v, lv in tier_a:
                if u == v and lu == lv and lu.startswith("const:"):
                    continue  # skip const+const noise except via loop
                cv = tuple(a + b for a, b in zip(u, v))
                entries_b.append(
                    (len(entries_b), f"concat({lu},{lv})", cv))
                counter[0] += 1
        tier_all = self._dedup(entries_b)
        return tier_a, tier_all


    def _build_list_pool(
            self, examples: Sequence[Tuple[Dict[str, Any], Any]],
            num_in: List[str], in_names: List[str],
            target: Tuple[Any, ...], counter: List[int]
            ) -> Tuple[List[Tuple[Tuple[Any, ...], str]],
                       List[Tuple[Tuple[Any, ...], str]]]:
        """Tier-A/all list component pool for list_concat folds.

        Sources (generic): singleton lists of each numeric input, already-
        list input columns (canonicalized to tuples), empty list const,
        common prefix/suffix list fragments of the target. Tier-B: one
        list_concat of Tier-A pairs so composed fragments remain visible
        after reuse revoke (mirrors string Tier-B).
        """
        n = len(examples)
        entries: List[Tuple[int, str, Tuple[Any, ...]]] = []

        def _add(label: str, vec: Tuple[Any, ...]) -> None:
            if len(vec) != n:
                return
            if any(not isinstance(x, tuple) for x in vec):
                return
            entries.append((len(entries), label, vec))
            counter[0] += 1

        # Singleton wrap of each numeric input: [x] as (x,).
        for nm in num_in:
            _add(f"wrap({nm})",
                 tuple(( _canon(a[nm]), ) for a, _ in examples))

        # Already-list input columns.
        for nm in in_names:
            if all(_is_list_value(a[nm]) for a, _ in examples):
                _add(f"param:{nm}",
                     tuple(_canon_list(a[nm]) for a, _ in examples))

        # Empty const + common prefix/suffix list fragments.
        _add("const:()", tuple(() for _ in range(n)))
        if target:
            cp = target[0]
            for s in target[1:]:
                i = 0
                while i < len(cp) and i < len(s) and cp[i] == s[i]:
                    i += 1
                cp = cp[:i]
            if cp:
                _add(f"const:{cp!r}", tuple(cp for _ in range(n)))
            # common suffix
            cs = target[0]
            for s in target[1:]:
                i = 0
                while (i < len(cs) and i < len(s)
                       and cs[-(i + 1)] == s[-(i + 1)]):
                    i += 1
                cs = cs[-i:] if i else ()
            if cs:
                _add(f"const:{cs!r}", tuple(cs for _ in range(n)))

        # Per-row suffix after peeling wrap(num_in) prefix.
        for nm in num_in:
            wrap_vec = None
            for _, label, vec in entries:
                if label == f"wrap({nm})":
                    wrap_vec = vec
                    break
            if wrap_vec is None:
                continue
            suffixes = []
            ok = True
            for t, p in zip(target, wrap_vec):
                if len(p) <= len(t) and t[:len(p)] == p:
                    suffixes.append(t[len(p):])
                else:
                    ok = False
                    break
            if ok and suffixes and all(s == suffixes[0] for s in suffixes):
                _add(f"const:{suffixes[0]!r}", tuple(suffixes))

        tier_a = self._dedup(entries)
        entries_b = list(entries)
        for u, lu in tier_a:
            for v, lv in tier_a:
                cv = tuple(a + b for a, b in zip(u, v))
                entries_b.append(
                    (len(entries_b), f"list_concat({lu},{lv})", cv))
                counter[0] += 1
        return tier_a, self._dedup(entries_b)

    def _build_dict_pool(
            self, examples: Sequence[Tuple[Dict[str, Any], Any]],
            num_in: List[str], in_names: List[str],
            target: Tuple[Any, ...], counter: List[int]
            ) -> Tuple[List[Tuple[Tuple[Any, ...], str]],
                       List[Tuple[Tuple[Any, ...], str]]]:
        """Tier-A/all dict component pool for merge folds.

        Sources (generic): singleton dicts wrapping each numeric input
        under a small key vocab, already-dict input columns, empty dict
        const, per-key peels of the target (const and varying). Tier-B:
        merge of Tier-A pairs so composed fragments remain visible after
        reuse revoke.
        """
        n = len(examples)
        entries: List[Tuple[int, str, Tuple[Any, ...]]] = []
        key_vocab = ("a", "b", "c", "k", "v", "x", "y", "z")

        def _add(label: str, vec: Tuple[Any, ...]) -> None:
            if len(vec) != n:
                return
            if any(not isinstance(x, tuple) for x in vec):
                return
            entries.append((len(entries), label, vec))
            counter[0] += 1

        # Empty const.
        _add("const:{}", tuple(() for _ in range(n)))

        # Singleton wrap of each numeric input under each key.
        for nm in num_in:
            for key in key_vocab:
                vec = tuple(_canon_dict({key: _canon(a[nm])})
                            for a, _ in examples)
                _add(f"wrap({key}:{nm})", vec)

        # Already-dict input columns.
        for nm in in_names:
            if all(_is_dict_value(a[nm]) for a, _ in examples):
                _add(f"param:{nm}",
                     tuple(_canon_dict(a[nm]) for a, _ in examples))

        # Per-key peels of the target: for each key present in every
        # example, emit the singleton {key: value_i} vector (const or
        # varying). This is the dict analog of list prefix/suffix mining.
        if target:
            key_sets = [set(dict(t).keys()) for t in target]
            common_keys = set.intersection(*key_sets) if key_sets else set()
            for key in sorted(common_keys, key=str):
                vec = tuple(((key, dict(t)[key]),) for t in target)
                _add(f"key:{key!r}", vec)

        tier_a = self._dedup(entries)
        # Tier-B: merge of Tier-A pairs (disjoint-key only to avoid noise).
        entries_b = list(entries)
        for i, (u, lu) in enumerate(tier_a):
            umap = dict(u[0]) if u else {}
            for v, lv in tier_a:
                if lu == lv and lu.startswith("const:"):
                    continue
                # Skip if any example has key overlap between u and v.
                ok = True
                cv_list = []
                for a, b in zip(u, v):
                    am, bm = dict(a), dict(b)
                    if set(am) & set(bm):
                        ok = False
                        break
                    merged = _deep_merge_dicts(am, bm)
                    cv_list.append(_canon_dict(merged))
                if not ok:
                    continue
                entries_b.append(
                    (len(entries_b), f"merge({lu},{lv})", tuple(cv_list)))
                counter[0] += 1
        tier_all = self._dedup(entries_b)
        return tier_a, tier_all


    def _build_pool(self, examples: Sequence[Tuple[Dict[str, Any], Any]],
                    num_in: List[str], target: Tuple[Any, ...],
                    counter: List[int]
                    ) -> Tuple[List[Tuple[Tuple[Any, ...], str]],
                               List[Tuple[Tuple[Any, ...], str]]]:
        """Build the two-tier component pool.

        Tier A (residual-search tier): parameters, literals, unary/binary
        pure-numeric primitive applications over (param) and (param, lit)
        in both orders. This tier subsumes the fixed NUMERIC skeletons
        (square=power(x,2), double=multiply(2,x), negate=negate(x),
        add_two=add(x,2), multiply_two=multiply(x,2),
        subtract_two=subtract(x,2), abs_value=abs(x)).
        Tier B (atomicity tier): Tier A plus one bounded level of unary
        chaining over Tier-A vectors ("bounded two-stage compositions")
        plus the exact evidence-fit vectors (affine, polynomial deg 2-4).
        Both tiers deduplicated by value, rank order preserved.
        """
        n = len(examples)
        unary, binary = self._prim_lists()
        in_vecs = {nm: tuple(_canon(a[nm]) for a, _ in examples)
                   for nm in num_in}
        entries: List[Tuple[int, str, Tuple[Any, ...]]] = []

        def _add(label: str, vec: Optional[Tuple[Any, ...]]) -> None:
            if vec is None or len(vec) != n:
                return
            entries.append((len(entries), label, vec))
            counter[0] += 1

        for nm in num_in:
            _add(f"param:{nm}", in_vecs[nm])
        litvecs: List[Tuple[int, Tuple[Any, ...]]] = []
        for lit in self.literals:
            lv = tuple([lit] * n)
            litvecs.append((lit, lv))
            _add(f"lit:{lit}", lv)
        level1: List[Tuple[Any, ...]] = []
        for pname, prim, preq in unary:
            for nm in num_in:
                v = self._apply_vec(pname, prim, preq, [in_vecs[nm]])
                if v is not None:
                    _add(f"{pname}({nm})", v)
                    level1.append(v)
        for pname, prim, preq in binary:
            for nm in num_in:
                for lit, lv in litvecs:
                    v1 = self._apply_vec(pname, prim, preq,
                                         [in_vecs[nm], lv])
                    if v1 is not None:
                        _add(f"{pname}({nm},{lit})", v1)
                        level1.append(v1)
                    v2 = self._apply_vec(pname, prim, preq,
                                         [lv, in_vecs[nm]])
                    if v2 is not None:
                        _add(f"{pname}({lit},{nm})", v2)
                        level1.append(v2)
        tier_a = self._dedup(entries)
        # Tier B: bounded two-stage compositions.
        entries_b = list(entries)
        seen_l1 = set()
        for v in level1:
            if v in seen_l1:
                continue
            seen_l1.add(v)
            for pname, prim, preq in unary:
                w = self._apply_vec(pname, prim, preq, [v])
                if w is not None:
                    entries_b.append((len(entries_b), f"{pname}(<l1>)", w))
                    counter[0] += 1
        # Tier B: exact evidence fits (atomicity recognition only).
        for nm in num_in:
            for label, vec in self._evidence_fits(in_vecs[nm], target, n):
                entries_b.append((len(entries_b), label, vec))
                counter[0] += 1
        pool_all = self._dedup(entries_b)
        return tier_a, pool_all

    def _acquired_realizes(
            self, examples: Sequence[Tuple[Dict[str, Any], Any]],
            in_name: str, target: Tuple[Any, ...]
            ) -> bool:
        """True if an acquired/promoted unary already equals `target`."""
        for pname in sorted(self.reg.names()):
            try:
                prim = self.reg.get(pname)
            except Exception:
                continue
            if getattr(prim, "family", "") not in ("acquired", "promoted"):
                continue
            try:
                preq = [k for k, v in prim.inputs.items() if not v.optional]
                if len(preq) != 1:
                    continue
                if not _is_numeric_output_prim(prim):
                    continue
                vec = tuple(
                    _canon(prim.fn(**{preq[0]: a[in_name]}))
                    for a, _ in examples)
            except Exception:
                continue
            if vec == target:
                return True
            # v40: confidence-gated approximate identity (float-stable).
            # Does not soften residual algebra; only complete-vector identity
            # for acquired/promoted unary reuse recognition.
            try:
                from swarm_engine.acquisition.approx_match import (
                    approx_identity_admit)
                ok, _conf = approx_identity_admit(vec, target)
                if ok:
                    return True
            except Exception:
                pass
        return False

    def _reuse_entries(
            self, examples: Sequence[Tuple[Dict[str, Any], Any]],
            num_in: List[str], target: Tuple[Any, ...],
            existing: Dict[Tuple[Any, ...], str],
            counter: List[int],
            t0: Optional[float] = None,
            ) -> List[Tuple[Tuple[Any, ...], str]]:
        """Behavioral-match components from existing acquired capabilities.

        Scans the registry for pure numeric unary acquired/promoted
        primitives, evaluates them on the parent's examples, and returns
        value vectors that are NOT already in `existing` (the grammar
        Tier-A map). I2-ops is preserved: acquired caps are never used as
        operators when *building* pool expressions; they only contribute
        here as exact value-vector matches (I2-match / hierarchical reuse).

        Multi-input parents (v21): a unary acquired cap may bind to ANY
        numeric input column of the parent; each (cap, column) evaluation
        that yields a novel numeric vector is a reuse candidate. Single-
        input parents behave as before (one column). Cross-arity (v22):
        an acquired cap whose arity equals |num_in| may bind via any
        permutation of the parent columns (exact value-vector match).

        v36: partial-app (k>=2 extras) is residual-guided under wall-clock:
        only lit-bind plans whose first example (then full vector) matches
        a needed residual ``target - pool_vec`` are fully evaluated /
        admitted. This is reuse-first / incremental-residual ranking, not
        a raised budget -- k=1 and full-arity paths unchanged. Deadline
        checks run inside the partial enum.

        Deterministic tie-break when several caps share a vector: prefer
        names that do not start with ``acquired.`` (stable goal-bound
        aliases), then lexicographic. No per-capability identity bonus.
        Caps whose vector equals the parent target are skipped (atomicity
        -- the normal strategies already own that case via fail-fast).
        """
        if not num_in:
            return []
        # v36: needed residuals for k>=2 partial early-reject / admit.
        # A partial vector is useful for a 2-way fold iff it equals
        # target - some existing pool vector. First-example membership
        # in need0 prunes |lits|^k before full evaluation.
        needed: Dict[Tuple[Any, ...], bool] = {}
        need0: set = set()
        if (self._algebra.domain == "numeric"
                and target is not None and existing):
            for v in existing:
                try:
                    r = tuple(_canon(t - x) for t, x in zip(target, v))
                except Exception:
                    continue
                needed[r] = True
                need0.add(r[0])
        # Collect candidates per value vector: list of (prim_name, bind_map).
        by_vec: Dict[Tuple[Any, ...], List[Tuple[str, Dict[str, str]]]] = {}
        # Cost-rank: prefer lower partial-extra before enumerating.
        def _prim_cost(pname: str) -> Tuple[int, str]:
            try:
                prim = self.reg.get(pname)
                preq = [k for k, v in prim.inputs.items() if not v.optional]
                extra = max(0, len(preq) - len(num_in))
                return (extra, pname)
            except Exception:
                return (999, pname)
        for pname in sorted(self.reg.names(), key=_prim_cost):
            try:
                prim = self.reg.get(pname)
            except Exception:
                continue
            if getattr(prim, "family", "") not in ("acquired", "promoted"):
                continue
            if Effect.PURE not in tuple(getattr(prim, "effects", ()) or ()):
                continue
            try:
                inputs = prim.inputs
                preq = [k for k, v in inputs.items() if not v.optional]
            except Exception:
                continue
            # Arity gate (v21+/v28/v29):
            #   * unary -> any single parent column
            #   * arity-N == |num_in| -> permutation of parent columns
            #   * arity == |num_in|+k (1<=k<=MAX_PARTIAL_EXTRA) -> partial
            #     application: k prim inputs bound to literals from the
            #     shared vocab, the rest to a permutation of parent
            #     columns. Bound values come from the literal vocabulary
            #     (task/capability context), never from an injected
            #     desired solution. All binding-position combinations and
            #     multi-bind literal tuples are tried; fold search selects
            #     which survive. Covers residual arity >1 when |num_in|>1
            #     and k-extra binds on composed multi-input caps.
            from itertools import permutations as _perms
            from itertools import combinations as _combs
            from itertools import product as _product
            bind_plans = []  # each: (cols_for_free, bound_map)
            # bound_map: prim_input -> literal (or empty for full arity match)
            if len(preq) == 1:
                for col in num_in:
                    bind_plans.append(([col], {}))
            elif len(preq) == len(num_in) and len(preq) >= 2:
                for p in _perms(num_in):
                    bind_plans.append((list(p), {}))
            elif (len(num_in) >= 1
                    and self._algebra.domain == "numeric"
                    and len(preq) > len(num_in)):
                extra = len(preq) - len(num_in)
                if 1 <= extra <= MAX_PARTIAL_EXTRA:
                    # Choose which `extra` prim inputs are literal-bound;
                    # remaining free inputs permute onto parent columns.
                    # Resource gate for k>1: intersect shared vocab with a
                    # compact non-neg band to bound |lits|^k growth. Still
                    # the shared vocab (no injected answers); k=1 keeps
                    # the full literal set.
                    # Resource gate: k=2 uses 0..5; k>=3 uses 1..5
                    # (drops 0 noise) to keep |lits|^k searchable.
                    if extra >= 3:
                        lit_pool = tuple(
                            lit for lit in self.literals
                            if isinstance(lit, int) and 1 <= lit <= 5)
                    elif extra > 1:
                        lit_pool = tuple(
                            lit for lit in self.literals
                            if isinstance(lit, int) and 0 <= lit <= 5)
                    else:
                        lit_pool = tuple(self.literals)
                    if not lit_pool:
                        continue
                    for bound_names in _combs(preq, extra):
                        free_names = [n for n in preq if n not in bound_names]
                        if len(free_names) != len(num_in):
                            continue
                        for cols in _perms(num_in):
                            cols = list(cols)
                            for lits in _product(lit_pool, repeat=extra):
                                bind_plans.append(
                                    (cols, dict(zip(bound_names, lits))))
            else:
                continue
            try:
                # Inputs should be numeric-typed when declared; acquired
                # caps often declare output "any" so we do NOT require
                # _is_numeric_output_prim -- empirical numeric vectors
                # below are the acceptance criterion (behavioral match).
                if not all(_is_numeric_input_type(inputs[k]) for k in preq):
                    # acquired often uses "any" inputs too; allow "any"
                    if not all(
                        str(getattr(inputs[k], "type", "")).lower()
                        in ("any", "number", "int", "float", "numeric", "")
                        or _is_numeric_input_type(inputs[k])
                        for k in preq):
                        continue
            except Exception:
                continue
            for cols, bound_lits in bind_plans:
                vec_list: List[Any] = []
                ok = True
                # Map free prim inputs (those not in bound_lits) to cols.
                free_names = [n for n in preq if n not in bound_lits]
                if len(free_names) != len(cols):
                    continue
                # v35/v36: first-example domain/magnitude (+ residual) probe.
                # v36: for k>=2 lit-binds, also require v0 in need0 so
                # |lits|^k does not exhaust the wall-clock before fold.
                if bound_lits:
                    if t0 is not None and (counter[0] & 63) == 0:
                        _check_deadline(t0, self.wall_clock_s)
                    try:
                        a0, _ = examples[0]
                        kwargs0 = {free_names[i]: a0[cols[i]]
                                   for i in range(len(free_names))}
                        kwargs0.update(bound_lits)
                        v0 = prim.fn(**kwargs0)
                        if self._algebra.domain == "numeric":
                            if not _is_num_scalar(v0) or abs(v0) > MAGNITUDE_CAP:
                                continue
                            # Residual-guided early reject (k>=2 only).
                            if (len(bound_lits) >= 2 and need0
                                    and _canon(v0) not in need0):
                                continue
                        elif self._algebra.domain == "bit":
                            if not _is_bit_scalar(v0):
                                continue
                        elif self._algebra.domain == "string":
                            if not isinstance(v0, str):
                                continue
                        elif self._algebra.domain in ("list", "tuple"):
                            if not _is_list_value(v0):
                                continue
                        elif self._algebra.domain == "dict":
                            if not _is_dict_value(v0):
                                continue
                    except Exception:
                        continue
                for a, _ in examples:
                    try:
                        kwargs = {free_names[i]: a[cols[i]]
                                  for i in range(len(free_names))}
                        kwargs.update(bound_lits)
                        v = prim.fn(**kwargs)
                    except Exception:
                        ok = False
                        break
                    if self._algebra.domain == "bit":
                        if not _is_bit_scalar(v):
                            ok = False
                            break
                        vec_list.append(self._algebra.canon(v))
                    elif self._algebra.domain == "string":
                        if not isinstance(v, str):
                            ok = False
                            break
                        vec_list.append(v)
                    elif self._algebra.domain == "list":
                        if not _is_list_value(v):
                            ok = False
                            break
                        vec_list.append(self._algebra.canon(v))
                    elif self._algebra.domain == "tuple":
                        if not isinstance(v, tuple):
                            ok = False
                            break
                        vec_list.append(self._algebra.canon(v))
                    elif self._algebra.domain == "dict":
                        if not _is_dict_value(v):
                            ok = False
                            break
                        vec_list.append(self._algebra.canon(v))
                    else:
                        if not _is_num_scalar(v) or abs(v) > MAGNITUDE_CAP:
                            ok = False
                            break
                        vec_list.append(_canon(v))
                counter[0] += 1
                if not ok or len(vec_list) != len(examples):
                    continue
                vec = tuple(vec_list)
                if vec == target:
                    continue  # whole-target match: atomicity owns it
                if vec in existing:
                    continue  # grammar already expresses this vector
                # v36: k>=2 partials must hit a needed residual exactly
                # (incremental residual admit). k=1 keeps broad enum.
                if (bound_lits and len(bound_lits) >= 2 and needed
                        and vec not in needed):
                    continue
                bind_map = {free_names[i]: cols[i]
                            for i in range(len(free_names))}
                # Partial-app const binds travel alongside arg binds under
                # a reserved "__const__" key map stored in reuse_arg_bind
                # via a parallel channel: we encode as bind_map entries
                # with a sentinel prefix that assembly recognizes.
                if bound_lits:
                    for bn, lit in bound_lits.items():
                        bind_map[f"__lit__:{bn}"] = lit  # type: ignore[assignment]
                by_vec.setdefault(vec, []).append((pname, bind_map))
        out: List[Tuple[Tuple[Any, ...], str]] = []
        for vec in sorted(by_vec.keys(), key=lambda v: (
                min(nm for nm, _b in by_vec[v]),)):
            entries = by_vec[vec]

            def _rank(entry: Tuple[str, Dict[str, str]]) -> Tuple[int, int, str]:
                # v30: prefer fewer __lit__ binds so a materialized
                # residual-arity capability wins over re-partial-applying
                # the higher-arity base on the same vector. Then prefer
                # non-acquired. aliases, then lexicographic.
                nm, bmap = entry
                n_lit = sum(1 for k in bmap
                            if isinstance(k, str) and k.startswith("__lit__:"))
                return (n_lit,
                        1 if nm.startswith("acquired.") else 0,
                        nm)

            chosen_name, chosen_bind = sorted(entries, key=_rank)[0]
            # Encode arg binding in label when any prim input differs from
            # the parent column it binds (cross-name remapping).
            if any(k != v for k, v in chosen_bind.items()):
                bind_s = ",".join(f"{k}={chosen_bind[k]}"
                                  for k in sorted(chosen_bind,
                                                  key=str))
                out.append((vec, f"reuse:{chosen_name}|{bind_s}"))
            else:
                out.append((vec, f"reuse:{chosen_name}"))
        return out

    # -- residual search (additive folds) --------------------------------------
    def _fold_solutions(self, target: Tuple[Any, ...],
                        vecs: List[Tuple[Any, ...]],
                        by_value: Dict[Tuple[Any, ...], int],
                        k: int, t0: float, counter: List[int]
                        ) -> List[Tuple[Tuple[Any, ...], ...]]:
        """Exact k-component additive decompositions of `target`.

        Matching pursuit over value vectors. k=2 is a direct scan. For
        k>=3 a meet-in-the-middle over pair sums keeps the miss case
        bounded: pair_sums holds every b+c once (21k entries for a
        205-vector pool); k=3 scans a (205 lookups), k=4 scans (a,b)
        pairs (21k), k=5 scans (a,b,c) triples (1.4M, ~2-3s) -- all
        far below the direct 73M-iteration k=5 blowup. k>=6 (which needs
        n>=8 examples by I7) uses direct enumeration under the
        wall-clock budget and fail-closes. Rank order puts small
        expressions first, so genuine few-term decompositions surface
        early. Returns up to MAX_ALTERNATIVES_COUNTED solutions.
        """
        sols: List[Tuple[Tuple[Any, ...], ...]] = []
        n = len(vecs)
        cap = MAX_ALTERNATIVES_COUNTED
        if k == 2:
            for v in vecs:
                _check_deadline(t0, self.wall_clock_s)
                r = self._algebra.residual_vec(target, v)
                counter[0] += 1
                if r is None:
                    continue
                if r in by_value:
                    sols.append((v, r))
                    if len(sols) >= cap:
                        break
            return sols
        # Pair sums, first-found representative per sum (deterministic).
        pair_sums: Dict[Tuple[Any, ...],
                        Tuple[Tuple[Any, ...], Tuple[Any, ...]]] = {}
        for i in range(n):
            _check_deadline(t0, self.wall_clock_s)
            vi = vecs[i]
            for j in range(i, n):
                s = self._algebra.combine_vec(vi, vecs[j])
                counter[0] += 1
                if s not in pair_sums:
                    pair_sums[s] = (vi, vecs[j])
        if k == 3:
            for a in vecs:
                _check_deadline(t0, self.wall_clock_s)
                r = self._algebra.residual_vec(target, a)
                counter[0] += 1
                if r in pair_sums:
                    b, c = pair_sums[r]
                    sols.append((a, b, c))
                    if len(sols) >= cap:
                        break
            return sols
        if k == 4:
            for i in range(n):
                for j in range(i, n):
                    _check_deadline(t0, self.wall_clock_s)
                    r = self._algebra.residual_vec(target, self._algebra.combine_vec(vecs[i], vecs[j]))
                    counter[0] += 1
                    if r in pair_sums:
                        c, d = pair_sums[r]
                        sols.append((vecs[i], vecs[j], c, d))
                        if len(sols) >= cap:
                            return sols
            return sols
        if k == 5:
            for i in range(n):
                for j in range(i, n):
                    vij = self._algebra.combine_vec(vecs[i], vecs[j])
                    for l in range(j, n):
                        _check_deadline(t0, self.wall_clock_s)
                        r = self._algebra.residual_vec(target, self._algebra.combine_vec(vij, vecs[l]))
                        counter[0] += 1
                        if r in pair_sums:
                            d, e = pair_sums[r]
                            sols.append((vecs[i], vecs[j], vecs[l], d, e))
                            if len(sols) >= cap:
                                return sols
            return sols
        # k >= 6: direct enumeration under the wall-clock budget.
        cur: List[Tuple[Any, ...]] = []

        class _Done(Exception):
            pass

        def rec(depth: int, start: int,
                residual: Tuple[Any, ...]) -> None:
            _check_deadline(t0, self.wall_clock_s)
            if depth == k - 1:
                counter[0] += 1
                if residual in by_value:
                    sols.append(tuple(cur) + (residual,))
                    if len(sols) >= cap:
                        raise _Done()
                return
            for j in range(start, n):
                v = vecs[j]
                new_res = self._algebra.residual_vec(residual, v)
                counter[0] += 1
                cur.append(v)
                try:
                    rec(depth + 1, j, new_res)
                finally:
                    cur.pop()

        try:
            rec(0, 0, target)
        except _Done:
            pass
        return sols

    # -- nest search ------------------------------------------------------------
    def _nest_solutions(self, target: Tuple[Any, ...],
                        pool_all: List[Tuple[Tuple[Any, ...], str]],
                        vecs_a: List[Tuple[Any, ...]],
                        by_a: Dict[Tuple[Any, ...], int],
                        limit_k: int, t0: float, counter: List[int],
                        depth: int = 1,
                        reuse_vecs: Optional[Sequence[Tuple[Any, ...]]] = None,
                        ) -> List[Tuple[int, int, Tuple[str, ...],
                                          Tuple[Tuple[Any, ...], ...]]]:
        """Exact nest-chain decompositions: target == u(v) or bin(v,lit)/bin(lit,v), v a fold.

        For each pure numeric unary op u and each candidate intermediate
        v, apply u elementwise; on an exact match with a non-degenerate
        intermediate, run the bounded additive search on v (and recurse
        for deeper peels, up to max_depth). Returns flat (components,
        depth, outer_ops, component_vectors) tuples -- the contract is
        flattened (I1), so the outer op is recorded in provenance only;
        re-assembly re-discovers it empirically.

        Intermediate candidates (v21 nest-chain reuse):
          * every vector in ``pool_all`` (generative grammar; prior behavior)
          * involutory preimages under each unary (u(u(target))==target)
          * when ``reuse_vecs`` is non-empty: elementwise sums of each reuse
            vector with each ``vecs_a`` entry (and 2*reuse). These cover
            intermediates that are folds involving hierarchical-reuse
            components and are therefore absent from the generative pool.
        """
        cands: List[Tuple[int, int, Tuple[str, ...],
                          Tuple[Tuple[Any, ...], ...]]] = []
        unary, _ = self._prim_lists()
        # Build peel-candidate list once (deterministic order).
        # Sources:
        #   (1) generative pool_all (prior behavior)
        #   (2) involutory preimages: for each unary u, if u(u(target))==target
        #       and u(target)!=target, then u(target) is a candidate
        #       intermediate for target==u(v). Recovers nests whose
        #       intermediate is absent from the generative pool (e.g.
        #       negate(P1+C) after P1 revoke, when the fold of -target
        #       still exists over Tier-A / remaining reuse).
        #   (3) reuse-fold intermediates: reuse_vec + vecs_a (and 2*reuse)
        #       when hierarchical-reuse components are present -- covers
        #       non-involution outers whose intermediate is a reuse fold.
        peel_vs: List[Tuple[Any, ...]] = []
        seen_v: set = set()
        for v, _label in pool_all:
            if v not in seen_v:
                seen_v.add(v)
                peel_vs.append(v)
        for pname, prim, preq in unary:
            v_cand = self._apply_vec(pname, prim, preq, [target])
            counter[0] += 1
            if v_cand is None or v_cand == target or v_cand in seen_v:
                continue
            back = self._apply_vec(pname, prim, preq, [v_cand])
            counter[0] += 1
            if back is not None and back == target:
                seen_v.add(v_cand)
                peel_vs.append(v_cand)
        if reuse_vecs:
            for rv in reuse_vecs:
                for other in vecs_a:
                    s = self._algebra.combine_vec(rv, other)
                    if s not in seen_v:
                        seen_v.add(s)
                        peel_vs.append(s)
        # Binary-with-literal outers (v26): target == bin(v, lit) or
        # bin(lit, v). Peel set is deliberately compact -- reuse-fold
        # intermediates (and 2*reuse) plus involution peels already in
        # peel_vs that are NOT the full generative pool -- so we do not
        # O(|pool|*|binary|*|lit|) fold-search on accidental matches.
        # Full-pool peels remain available to the unary outer loop above.
        binlit_peels: List[Tuple[Any, ...]] = []
        seen_bl: set = set()
        if reuse_vecs:
            for rv in reuse_vecs:
                dub = self._algebra.combine_vec(rv, rv)
                if dub not in seen_bl:
                    seen_bl.add(dub)
                    binlit_peels.append(dub)
                for other in vecs_a:
                    s = self._algebra.combine_vec(rv, other)
                    if s not in seen_bl:
                        seen_bl.add(s)
                        binlit_peels.append(s)
        # Only reuse-fold peels: each is a combine of known components, so
        # a k=2 fold exists when a binary-with-literal outer matches.
        # Involution peels (e.g. -target under multiply(_, -1)) are excluded
        # -- they trigger fruitless deep recursion before genuine outers
        # like power(_, 2) are reached.
        if binlit_peels:
            _, binary = self._prim_lists()
            litvecs = [(lit, tuple([lit] * len(target)))
                       for lit in self.literals]
            for pname, prim, preq in binary:
                if len(preq) != 2:
                    continue
                for lit, lv in litvecs:
                    for free_index in (0, 1):
                        ospec = {
                            "op": pname,
                            "free_index": free_index,
                            "bound": {preq[1 - free_index]: lit},
                        }
                        for v in binlit_peels:
                            _check_deadline(t0, self.wall_clock_s)
                            if v == target:
                                continue
                            if free_index == 0:
                                argvecs = [v, lv]
                            else:
                                argvecs = [lv, v]
                            w = self._apply_vec(pname, prim, preq, argvecs)
                            counter[0] += 1
                            if w is None or w != target:
                                continue
                            if len(set(v)) == 1 or len(set(w)) == 1:
                                continue
                            inner_best = []
                            inner_k = 0
                            for k in range(2, limit_k + 1):
                                sols = [s for s in self._fold_solutions(
                                    v, vecs_a, by_a, k, t0, counter)
                                    if self._nondegenerate(v, s)]
                                if sols:
                                    inner_best = sols
                                    inner_k = k
                                    break
                            for sol in inner_best[:MAX_ALTERNATIVES_COUNTED]:
                                cands.append((inner_k, depth, (ospec,), sol))
                                if len(cands) >= MAX_ALTERNATIVES_COUNTED:
                                    return cands
                            # No deep recursion on binary-with-literal misses:
                            # only accept when the inner fold succeeds.
        # Prefer compact binary-with-literal hits; avoid burning the
        # wall-clock on full-pool unary peels + involution recursion when
        # a nest outer is already known.
        if cands:
            return cands
        for pname, prim, preq in unary:
            for v in peel_vs:
                _check_deadline(t0, self.wall_clock_s)
                if v == target:
                    continue  # degenerate: u would be identity on values
                w = self._apply_vec(pname, prim, preq, [v])
                counter[0] += 1
                if w is None or w != target:
                    continue
                if len(set(v)) == 1 or len(set(w)) == 1:
                    continue  # constant intermediate/target: degenerate
                # Inner: bounded additive search on the intermediate vector.
                inner_best: List[Tuple[Tuple[Any, ...], ...]] = []
                inner_k = 0
                for k in range(2, limit_k + 1):
                    sols = [s for s in self._fold_solutions(
                        v, vecs_a, by_a, k, t0, counter)
                        if self._nondegenerate(v, s)]
                    if sols:
                        inner_best = sols
                        inner_k = k
                        break
                for sol in inner_best[:MAX_ALTERNATIVES_COUNTED]:
                    cands.append((inner_k, depth, (pname,), sol))
                    if len(cands) >= MAX_ALTERNATIVES_COUNTED:
                        return cands
                # Deeper peels (bounded by max_depth).
                if depth < self.max_depth and inner_k == 0:
                    for (ik, idepth, iops, isol) in self._nest_solutions(
                            v, pool_all, vecs_a, by_a, limit_k,
                            t0, counter, depth + 1, reuse_vecs):
                        if ik <= limit_k:
                            cands.append((ik, idepth, (pname,) + iops, isol))
                            if len(cands) >= MAX_ALTERNATIVES_COUNTED:
                                return cands
        return cands

    # -- compound-residual (recursive) search ----------------------------------
    def _rebind_contract(self, contract: "DecompositionContract",
                         parent_name: str,
                         parent_goal: str) -> "DecompositionContract":
        """Rebind a nested contract so its parent node IS `parent_name`.

        Children are renamed `{parent_name}_c{i}` (and nested sub-contracts
        rebound recursively). Used when a compound residual discovered
        under a temporary goal is attached as a named child of the parent.
        """
        children: List[DecomposedChild] = []
        child_refs: List[str] = []
        for i, ch in enumerate(contract.children):
            cname = f"{parent_name}_c{i}"
            child_refs.append(cname)
            sub = ch.sub_contract
            if sub is not None:
                sub = self._rebind_contract(sub, cname, ch.description)
            children.append(DecomposedChild(
                name=cname,
                index=i,
                description=ch.description,
                projected_examples=list(ch.projected_examples),
                value_vector=list(ch.value_vector),
                source=ch.source,
                sub_contract=sub,
                reuse_primitive=ch.reuse_primitive,
                reuse_arg_bind=(dict(ch.reuse_arg_bind)
                                if ch.reuse_arg_bind else None),
            ))
        reconstructor = Reconstructor(
            family=contract.reconstructor.family,
            op=(list(contract.reconstructor.op)
                if isinstance(contract.reconstructor.op, list)
                else contract.reconstructor.op),
            child_refs=child_refs,
        )
        return DecompositionContract(
            parent_goal=parent_goal,
            reconstructor=reconstructor,
            children=children,
            n_examples=contract.n_examples,
            input_names=list(contract.input_names),
            confidence=contract.confidence,
            provenance=dict(contract.provenance),
            policy_version=contract.policy_version,
            determinism_salt=contract.determinism_salt,
        )

    def _compound_residual_solutions(
            self, target: Tuple[Any, ...],
            examples: Sequence[Tuple[Dict[str, Any], Any]],
            goal: str,
            vecs_a: List[Tuple[Any, ...]],
            by_a: Dict[Tuple[Any, ...], int],
            t0: float, counter: List[int],
            recursion_depth: int,
            flat_best: List[Tuple[Tuple[Any, ...], ...]],
            fold_best_k: int,
            ) -> List[Tuple[int, int, Tuple[str, ...],
                            Tuple[Tuple[Any, ...], ...],
                            Optional["DecompositionContract"], int]]:
        """Hierarchical k=2 candidates from a known flat partition.

        Given a flat all-pool k-way split (k > HIERARCHY_FLAT_TRIGGER),
        each hierarchical candidate peels one (or more) Tier-A components
        as the atomic side and treats the exact residual sum of the rest
        as a compound child -- admitted only when a recursive decompose()
        of that residual returns a contract. This is O(k) residual probes
        (not O(|pool|)), shares the parent wall-clock, and fail-closes on
        deadline without aborting the parent (deadline caught locally).

        Also tries contiguous bipartitions of the flat component list for
        balanced 2+2-style splits when k >= 4.

        Returns (k, depth, ops, comp_vecs, sub_contract, compound_index).
        """
        out: List[Tuple[int, int, Tuple[str, ...],
                        Tuple[Tuple[Any, ...], ...],
                        Optional["DecompositionContract"], int]] = []
        if recursion_depth >= MAX_RECURSION_DEPTH:
            return out
        if fold_best_k < 2 or not flat_best:
            return out
        memo: Dict[Tuple[Any, ...], Optional["DecompositionContract"]] = {}

        def _probe_residual(residual: Tuple[Any, ...]):
            if residual in memo:
                return memo[residual]
            if residual in by_a:
                memo[residual] = None
                return None
            if any(not _is_num_scalar(x) or abs(x) > MAGNITUDE_CAP
                   for x in residual):
                memo[residual] = None
                return None
            if len(set(residual)) == 1:
                memo[residual] = None
                return None
            residual_ex = [
                (dict(a), rv) for (a, _), rv in zip(examples, residual)]
            try:
                sub = self._decompose_inner(
                    f"{goal} [compound residual]",
                    residual_ex, t0,
                    _recursion_depth=recursion_depth + 1)
            except _Deadline:
                memo[residual] = None
                return None
            except Exception:
                sub = None
            memo[residual] = sub
            return sub

        # Use the rank-best flat solution (first in fold_best; deterministic).
        comps = flat_best[0]
        k = len(comps)
        # Type A: peel one atomic component; residual = sum of the rest.
        for i, v in enumerate(comps):
            _check_deadline(t0, self.wall_clock_s)
            counter[0] += 1
            rest = None
            for j, w in enumerate(comps):
                if j == i:
                    continue
                rest = w if rest is None else tuple(
                    self._algebra.combine(a, b) for a, b in zip(rest, w))
            if rest is None:
                continue
            if not self._nondegenerate(target, (v, rest)):
                continue
            sub = _probe_residual(rest)
            if sub is None:
                continue
            sub_depth = int(sub.provenance.get("nest_depth", 0) or 0)
            sub_h = int(sub.provenance.get("hierarchy_depth", 0) or 0)
            depth = 1 + max(sub_depth, sub_h)
            out.append((2, depth, (), (v, rest), sub, 1))
            if len(out) >= MAX_ALTERNATIVES_COUNTED:
                return out
        # Type B: contiguous bipartition into two compound groups (k>=4).
        if k >= 4:
            for split in range(2, k - 1):  # both sides >= 2 components
                _check_deadline(t0, self.wall_clock_s)
                left = comps[0]
                for w in comps[1:split]:
                    left = tuple(self._algebra.combine(a, b) for a, b in zip(left, w))
                right = comps[split]
                for w in comps[split + 1:]:
                    right = tuple(self._algebra.combine(a, b) for a, b in zip(right, w))
                if not self._nondegenerate(target, (left, right)):
                    continue
                # Both sides compound (neither in pool, or probe both).
                subL = _probe_residual(left)
                subR = _probe_residual(right)
                if subL is None or subR is None:
                    continue
                # Represent as (left, right) with compound on BOTH sides.
                # Emit with compound_idx=1 and attach subR; subL is nested
                # by rebinding left as a compound too via a dual attach.
                # For the contract emitter (single compound_idx), prefer
                # encoding left as atomic-looking only if in pool; else
                # store subL on index 0 by a parallel channel.
                # Simplest correct form: treat as two-compound by packing
                # subL into a synthetic wrapper -- handled below via
                # compound_idx=-2 sentinel meaning both sides compound,
                # with win_sub being (subL, subR) tuple... Keep emitter
                # simple: only emit when ONE side is preferred. Skip Type
                # B dual-compound here; Type A already yields recursion.
                # (Balanced dual-compound is a follow-on deepening.)
                del subL, subR  # reserved for deepening; Type A suffices
                continue
        return out

    # -- non-degeneracy ----------------------------------------------------------
    @staticmethod
    def _nondegenerate(target: Tuple[Any, ...],
                       comp_vecs: Sequence[Tuple[Any, ...]]) -> bool:
        """I4: reject const-exact and target-exact component matches.

        A component constantly equal to a literal, or equal to the whole
        target, is not a decomposition -- it is the atomicity the
        fail-fast already rules out, or a trivial restatement. (Param-exact
        components, e.g. the identity, are allowed: they are genuine
        sub-behaviors.)
        """
        seen_consts = set()
        for v in comp_vecs:
            if v == target:
                return False
            if len(set(v)) == 1:
                seen_consts.add(v[0])
        # All components constant means the target is constant -- but then
        # the fail-fast would have fired (const vectors are pool Tier A).
        # Guard anyway: a constant target is not decomposable, it is atomic.
        if len(comp_vecs) == len(seen_consts) and len(comp_vecs) > 0:
            if all(len(set(v)) == 1 for v in comp_vecs):
                return False
        return True

    def _apply_reconstructor(
            self, family: str, op: Any,
            comp_vecs: Sequence[Tuple[Any, ...]]
            ) -> Optional[Tuple[Any, ...]]:
        """Apply the reconstructor to child value vectors (I3 gate).

        Fold: left-associative elementwise addition of the component
        vectors. Nest: inner additive fold, then the outer unary op names
        applied innermost-first through the guarded primitive-application
        path (magnitude safety included). Returns None if any application
        is unusable.
        """
        acc = comp_vecs[0]
        for vec in comp_vecs[1:]:
            acc = self._algebra.combine_vec(acc, vec)
        if family == "associative_fold":
            return acc
        outer_ops = list(op) if isinstance(op, list) else [op]
        for ospec in outer_ops:
            if isinstance(ospec, str):
                prim = self.reg.get(ospec)
                if prim is None:
                    return None
                try:
                    preq = [k for k, v in prim.inputs.items()
                            if not v.optional]
                except Exception:
                    return None
                if len(preq) != 1:
                    return None
                acc = self._apply_vec(ospec, prim, preq, [acc])
                if acc is None:
                    return None
                continue
            if not isinstance(ospec, dict):
                return None
            pname = ospec.get("op")
            free_index = ospec.get("free_index")
            bound = ospec.get("bound") or {}
            prim = self.reg.get(pname) if pname else None
            if prim is None:
                return None
            try:
                preq = [k for k, v in prim.inputs.items()
                        if not v.optional]
            except Exception:
                return None
            if (len(preq) != 2
                    or not isinstance(free_index, int)
                    or free_index not in (0, 1)
                    or preq[1 - free_index] not in bound):
                return None
            n = len(acc)
            argvecs = []
            for i, pin in enumerate(preq):
                if i == free_index:
                    argvecs.append(acc)
                else:
                    lit = bound[pin]
                    argvecs.append(tuple([lit] * n))
            acc = self._apply_vec(pname, prim, preq, argvecs)
            if acc is None:
                return None
        return acc

    # -- entry point --------------------------------------------------------------
    def _try_structural_wrap(
            self, goal: str,
            ex: Sequence[Tuple[Dict[str, Any], Any]],
            in_names: List[str],
            num_in: List[str],
            target: Tuple[Any, ...],
            t0: float,
            counter: List[int],
            _recursion_depth: int = 0,
            ) -> Optional["DecompositionContract"]:
        """Singleton nested container → wrap(child_value) contract.

        When every output is a same-key singleton dict whose values are
        non-numeric (structured list/dict/tuple or other), peel the value
        as the sole child and reconstruct via kv. Also: singleton list of
        structured OR numeric/param/reuse values via lift (v37 numeric→list). Generic — no per-shape wrappers.
        """
        from swarm_engine.acquisition.gap_reasoner import (
            _decomposition_base_name)
        from swarm_engine.cognition.synthesis import SEARCH_POLICY_VERSION

        outs = [o for _, o in ex]
        n = len(ex)

        def _vc(v: Any) -> Any:
            if _is_num_scalar(v):
                return _canon(v)
            if _is_dict_value(v):
                return _canon_dict(v)
            if _is_list_value(v):
                return _canon_list(v)
            return v

        # --- set/frozenset singleton → lift_set (representation adapter) ---
        if all(isinstance(o, (set, frozenset)) and len(o) == 1 for o in outs):
            inners = [next(iter(o)) for o in outs]
            # Prefer set (not frozenset) reconstruction; examples may be either.
            if not all(isinstance(o, set) for o in outs):
                # frozenset examples: still peel; assembly uses lift_set then
                # frozenset() only if needed — require set equality on expects
                # when expects are set; for frozenset expects, refuse for now
                # unless all expects are set-compatible via set(expect)==...
                if not all(isinstance(o, frozenset) for o in outs):
                    return None
                # Uniform frozenset: peel + lift_set then wrap is insufficient
                # without lift_frozenset; skip (bytes/set are the primary unlock).
                return None
            numeric_inners = all(_is_num_scalar(v) for v in inners)
            if not numeric_inners and not all(isinstance(v, (str, bool)) for v in inners):
                # Allow hashable scalars only for singleton set wrap
                try:
                    for v in inners:
                        hash(v)
                except TypeError:
                    return None
            val_ex = [(dict(a), next(iter(o))) for a, o in ex]
            base = _decomposition_base_name(goal)
            cname = f"{base}_w0"
            child_src = "behavioral_fold"
            child_sub = None
            reuse_prim = None
            reuse_bind = None
            const_v = None
            vvec = tuple(_vc(v) for v in inners)
            if all(v == inners[0] for v in inners):
                child_src = "behavioral_const"
                const_v = inners[0]
            else:
                for col in in_names:
                    try:
                        colv = tuple(_vc(a[col]) for a, _ in ex)
                    except Exception:
                        continue
                    if colv == vvec:
                        child_src = "behavioral_param"
                        reuse_prim = col
                        break
            if child_src == "behavioral_fold":
                return None  # leave non-param set wraps to future search
            child = DecomposedChild(
                name=cname,
                index=0,
                description=f"{goal} [structural lift_set value]",
                projected_examples=list(val_ex),
                value_vector=[o for _, o in val_ex],
                source=child_src,
                sub_contract=child_sub,
                reuse_primitive=reuse_prim,
                reuse_arg_bind=reuse_bind,
                const_value=const_v,
            )
            recon = Reconstructor(
                family="structural_wrap",
                op={"op": "lift_set"},
                child_refs=[cname],
            )
            evidence = (n - 1) / n
            search = 1.0 / (1.0 + math.log10(1.0 + max(counter[0], 1)))
            return DecompositionContract(
                parent_goal=goal,
                reconstructor=recon,
                children=[child],
                n_examples=n,
                input_names=list(in_names),
                confidence=round(evidence * search, 4),
                provenance={
                    "builder": "behavioral_decomposer.v44_structural_wrap",
                    "search_policy_version": SEARCH_POLICY_VERSION,
                    "method": "structural_wrap_lift_set",
                    "wrap_op": "lift_set",
                    "n_components": 1,
                    "hierarchy_depth": 1,
                    "hierarchical": False,
                    "policy_version": SEARCH_POLICY_VERSION,
                    "examples_sha256": _sha256_examples(ex),
                    "recursion_depth": _recursion_depth,
                },
                policy_version=SEARCH_POLICY_VERSION,
            )

        # --- length-1 bytes → to_bytes (representation adapter) ---
        if all(isinstance(o, (bytes, bytearray)) and len(o) == 1 for o in outs):
            inners = [o[0] for o in outs]  # int 0..255
            if not all(isinstance(v, int) for v in inners):
                return None
            if not all(isinstance(o, bytes) for o in outs):
                return None  # bytearray expects deferred
            val_ex = [(dict(a), o[0]) for a, o in ex]
            base = _decomposition_base_name(goal)
            cname = f"{base}_w0"
            child_src = "behavioral_fold"
            reuse_prim = None
            reuse_bind = None
            const_v = None
            vvec = tuple(inners)
            if all(v == inners[0] for v in inners):
                child_src = "behavioral_const"
                const_v = inners[0]
            else:
                for col in in_names:
                    try:
                        colv = tuple(int(a[col]) & 0xFF for a, _ in ex)
                    except Exception:
                        continue
                    if colv == vvec:
                        child_src = "behavioral_param"
                        reuse_prim = col
                        break
            if child_src == "behavioral_fold":
                return None
            child = DecomposedChild(
                name=cname,
                index=0,
                description=f"{goal} [structural to_bytes value]",
                projected_examples=list(val_ex),
                value_vector=[o for _, o in val_ex],
                source=child_src,
                sub_contract=None,
                reuse_primitive=reuse_prim,
                reuse_arg_bind=reuse_bind,
                const_value=const_v,
            )
            recon = Reconstructor(
                family="structural_wrap",
                op={"op": "to_bytes"},
                child_refs=[cname],
            )
            evidence = (n - 1) / n
            search = 1.0 / (1.0 + math.log10(1.0 + max(counter[0], 1)))
            return DecompositionContract(
                parent_goal=goal,
                reconstructor=recon,
                children=[child],
                n_examples=n,
                input_names=list(in_names),
                confidence=round(evidence * search, 4),
                provenance={
                    "builder": "behavioral_decomposer.v44_structural_wrap",
                    "search_policy_version": SEARCH_POLICY_VERSION,
                    "method": "structural_wrap_to_bytes",
                    "wrap_op": "to_bytes",
                    "n_components": 1,
                    "hierarchy_depth": 1,
                    "hierarchical": False,
                    "policy_version": SEARCH_POLICY_VERSION,
                    "examples_sha256": _sha256_examples(ex),
                    "recursion_depth": _recursion_depth,
                },
                policy_version=SEARCH_POLICY_VERSION,
            )

        # --- dict singleton wrap via kv ---
        if (self._algebra.domain == "dict"
                and all(isinstance(o, dict) and len(o) == 1 for o in outs)):
            key0 = next(iter(outs[0]))
            if not all(next(iter(o)) == key0 for o in outs):
                return None
            val_ex = [(dict(a), o[key0]) for a, o in ex]
            vals = [o for _, o in val_ex]
            numeric_vals = all(_is_num_scalar(v) for v in vals)

            child_src = "behavioral_fold"
            reuse_prim = None
            reuse_bind = None
            child_sub = None
            const_v = None
            vvec = tuple(_vc(v) for v in vals)

            if all(v == vals[0] for v in vals):
                child_src = "behavioral_const"
                const_v = vals[0]
            else:
                for col in in_names:
                    try:
                        colv = tuple(_vc(a[col]) for a, _ in ex)
                    except Exception:
                        continue
                    if colv == vvec:
                        child_src = "behavioral_param"
                        reuse_prim = col
                        break
                if child_src == "behavioral_fold":
                    for pname in sorted(self.reg.names()):
                        try:
                            prim = self.reg.get(pname)
                        except Exception:
                            continue
                        if getattr(prim, "family", "") not in (
                                "acquired", "promoted"):
                            continue
                        if Effect.PURE not in tuple(
                                getattr(prim, "effects", ()) or ()):
                            continue
                        try:
                            pins = [k for k, v in prim.inputs.items()
                                    if not v.optional]
                        except Exception:
                            continue
                        if len(pins) != 1:
                            continue
                        for col in in_names:
                            try:
                                got = tuple(
                                    _vc(prim.fn(**{pins[0]: a[col]}))
                                    for a, _ in ex)
                            except Exception:
                                continue
                            if got == vvec:
                                child_src = "behavioral_reuse"
                                reuse_prim = pname
                                reuse_bind = {pins[0]: col}
                                break
                        if child_src == "behavioral_reuse":
                            break
                if (child_src == "behavioral_fold"
                        and _recursion_depth < MAX_RECURSION_DEPTH
                        and (all(_is_list_value(v) for v in vals)
                             or all(_is_dict_value(v) for v in vals)
                             or all(isinstance(v, tuple) for v in vals))):
                    try:
                        child_sub = self._decompose_inner(
                            f"{goal} [wrap value]",
                            val_ex, t0,
                            _recursion_depth=_recursion_depth + 1)
                    except Exception:
                        child_sub = None
                    if child_sub is not None:
                        # v39: trivial lift(param/const/reuse) sub-wraps
                        # flatten to behavioral_fold so GENERATE/lift
                        # acquires the list child (nested compound wrap
                        # assembly is fragile on recovery after revoke).
                        sub_srcs = [ch.source for ch in child_sub.children]
                        if (child_sub.reconstructor.family == "structural_wrap"
                                and len(sub_srcs) == 1
                                and sub_srcs[0] in (
                                    "behavioral_param",
                                    "behavioral_const",
                                    "behavioral_reuse")
                                # v42: do NOT flatten when the wrap value
                                # itself is a dict — flatten+dict-refuse
                                # left singleton nested dicts (e.g.
                                # {"b":{"v":x}}) with no acquisitive path
                                # (wrap refused; deep_merge needs 2+ keys).
                                and not all(_is_dict_value(v) for v in vals)):
                            child_sub = None
                            child_src = "behavioral_fold"
                        else:
                            child_src = "behavioral_compound_residual"

            # Numeric singletons without param/reuse/const/sub stay with
            # GENERATE (truly atomic kv-param). Nested-dict structured
            # children that failed compound/reuse/param/const fall through
            # so a parent-level deep_merge fold can compete (v39). List/
            # tuple structured children keep the wrap path (GENERATE/lift
            # acquires them — required for dict⊃list recovery after revoke).
            if (child_src == "behavioral_fold" and child_sub is None):
                if numeric_vals:
                    return None
                if vals and all(_is_dict_value(v) for v in vals):
                    return None
                # else: list/tuple/other → emit wrap for GENERATE child

            base = _decomposition_base_name(goal)
            cname = f"{base}_w0"
            # v42: rebind nested compound (product/wrap) under wrap child
            # name so assembly finds the intermediate as {base}_w0.
            if (child_sub is not None
                    and child_src == "behavioral_compound_residual"):
                child_sub = self._rebind_contract(
                    child_sub, cname,
                    f"{goal} [structural wrap value of key {key0!r}]")
            child = DecomposedChild(
                name=cname,
                index=0,
                description=(
                    f"{goal} [structural wrap value of key {key0!r}]"),
                projected_examples=list(val_ex),
                value_vector=[o for _, o in val_ex],
                source=child_src,
                sub_contract=child_sub,
                reuse_primitive=reuse_prim,
                reuse_arg_bind=reuse_bind,
                const_value=const_v,
            )
            recon = Reconstructor(
                family="structural_wrap",
                op={"op": "kv", "key": key0},
                child_refs=[cname],
            )
            evidence = (n - 1) / n
            search = 1.0 / (1.0 + math.log10(1.0 + max(counter[0], 1)))
            hier = (
                1 + int((child_sub.provenance or {}).get(
                    "hierarchy_depth", 0) or 0)
                if child_sub is not None else 1)
            return DecompositionContract(
                parent_goal=goal,
                reconstructor=recon,
                children=[child],
                n_examples=n,
                input_names=list(in_names),
                confidence=round(evidence * search, 4),
                provenance={
                    "builder": "behavioral_decomposer.v33_structural_wrap",
                    "search_policy_version": SEARCH_POLICY_VERSION,
                    "method": "structural_wrap_kv",
                    "wrap_key": key0,
                    "wrap_op": "kv",
                    "n_components": 1,
                    "hierarchy_depth": hier,
                    "hierarchical": child_sub is not None,
                    "policy_version": SEARCH_POLICY_VERSION,
                    "examples_sha256": _sha256_examples(ex),
                    "recursion_depth": _recursion_depth,
                },
                policy_version=SEARCH_POLICY_VERSION,
            )

        # --- list singleton → lift (structured OR numeric/param/reuse) ---
        # v37: numeric→list cross-domain wrap (e.g. [f(x)]) mirrors dict
        # kv wrap — no serialize-everything; lift(child) reconstruction.
        if (self._algebra.domain == "list"
                and all(isinstance(o, list) and len(o) == 1 for o in outs)):
            inners = [o[0] for o in outs]
            structured_inners = (
                all(_is_dict_value(v) for v in inners)
                or all(isinstance(v, tuple) for v in inners)
                or all(isinstance(v, list) for v in inners))
            numeric_inners = all(_is_num_scalar(v) for v in inners)
            if not structured_inners and not numeric_inners:
                return None
            val_ex = [(dict(a), o[0]) for a, o in ex]
            base = _decomposition_base_name(goal)
            cname = f"{base}_w0"
            child_src = "behavioral_fold"
            child_sub = None
            reuse_prim = None
            reuse_bind = None
            const_v = None
            vvec = tuple(_vc(v) for v in inners)
            if all(v == inners[0] for v in inners):
                child_src = "behavioral_const"
                const_v = inners[0]
            else:
                for col in in_names:
                    try:
                        colv = tuple(_vc(a[col]) for a, _ in ex)
                    except Exception:
                        continue
                    if colv == vvec:
                        child_src = "behavioral_param"
                        reuse_prim = col
                        break
                if child_src == "behavioral_fold":
                    for pname in sorted(self.reg.names()):
                        try:
                            prim = self.reg.get(pname)
                        except Exception:
                            continue
                        if getattr(prim, "family", "") not in (
                                "acquired", "promoted"):
                            continue
                        if Effect.PURE not in tuple(
                                getattr(prim, "effects", ()) or ()):
                            continue
                        try:
                            pins = [k for k, v in prim.inputs.items()
                                    if not v.optional]
                        except Exception:
                            continue
                        if len(pins) != 1:
                            continue
                        for col in in_names:
                            try:
                                got = tuple(
                                    _vc(prim.fn(**{pins[0]: a[col]}))
                                    for a, _ in ex)
                            except Exception:
                                continue
                            if got == vvec:
                                child_src = "behavioral_reuse"
                                reuse_prim = pname
                                reuse_bind = {pins[0]: col}
                                break
                        if child_src == "behavioral_reuse":
                            break
                if (child_src == "behavioral_fold"
                        and _recursion_depth < MAX_RECURSION_DEPTH
                        and structured_inners):
                    try:
                        child_sub = self._decompose_inner(
                            f"{goal} [lift value]",
                            val_ex, t0,
                            _recursion_depth=_recursion_depth + 1)
                    except Exception:
                        child_sub = None
                    if child_sub is not None:
                        child_src = "behavioral_compound_residual"
            # Numeric lift without param/reuse/const: leave to GENERATE
            # (atomic lift(param)); classified children take wrap path.
            if (numeric_inners and child_src == "behavioral_fold"
                    and child_sub is None):
                return None
            if (child_sub is not None
                    and child_src == "behavioral_compound_residual"):
                child_sub = self._rebind_contract(
                    child_sub, cname,
                    f"{goal} [structural lift value]")
            child = DecomposedChild(
                name=cname,
                index=0,
                description=f"{goal} [structural lift value]",
                projected_examples=list(val_ex),
                value_vector=[o for _, o in val_ex],
                source=child_src,
                sub_contract=child_sub,
                reuse_primitive=reuse_prim,
                reuse_arg_bind=reuse_bind,
                const_value=const_v,
            )
            recon = Reconstructor(
                family="structural_wrap",
                op={"op": "lift"},
                child_refs=[cname],
            )
            evidence = (n - 1) / n
            search = 1.0 / (1.0 + math.log10(1.0 + max(counter[0], 1)))
            hier = (
                1 + int((child_sub.provenance or {}).get(
                    "hierarchy_depth", 0) or 0)
                if child_sub is not None else 1)
            return DecompositionContract(
                parent_goal=goal,
                reconstructor=recon,
                children=[child],
                n_examples=n,
                input_names=list(in_names),
                confidence=round(evidence * search, 4),
                provenance={
                    "builder": "behavioral_decomposer.v33_structural_wrap",
                    "search_policy_version": SEARCH_POLICY_VERSION,
                    "method": "structural_wrap_lift",
                    "wrap_op": "lift",
                    "n_components": 1,
                    "hierarchy_depth": hier,
                    "hierarchical": child_sub is not None,
                    "policy_version": SEARCH_POLICY_VERSION,
                    "examples_sha256": _sha256_examples(ex),
                    "recursion_depth": _recursion_depth,
                },
                policy_version=SEARCH_POLICY_VERSION,
            )

        # --- tuple singleton → lift_tuple (mirror of list lift) ---
        # v41: singleton tuples were previously unwrapped-less, so mixed
        # dict⊃list⊃dict⊃tuple nests failed held-out with list/tuple
        # morph (got=[[x]] expect=([x],)). Generic container parity.
        if (self._algebra.domain == "tuple"
                and all(isinstance(o, tuple) and len(o) == 1 for o in outs)):
            inners = [o[0] for o in outs]
            structured_inners = (
                all(_is_dict_value(v) for v in inners)
                or all(isinstance(v, tuple) for v in inners)
                or all(isinstance(v, list) for v in inners))
            numeric_inners = all(_is_num_scalar(v) for v in inners)
            if not structured_inners and not numeric_inners:
                return None
            val_ex = [(dict(a), o[0]) for a, o in ex]
            base = _decomposition_base_name(goal)
            cname = f"{base}_w0"
            child_src = "behavioral_fold"
            child_sub = None
            reuse_prim = None
            reuse_bind = None
            const_v = None
            vvec = tuple(_vc(v) for v in inners)
            if all(v == inners[0] for v in inners):
                child_src = "behavioral_const"
                const_v = inners[0]
            else:
                for col in in_names:
                    try:
                        colv = tuple(_vc(a[col]) for a, _ in ex)
                    except Exception:
                        continue
                    if colv == vvec:
                        child_src = "behavioral_param"
                        reuse_prim = col
                        break
                if child_src == "behavioral_fold":
                    for pname in sorted(self.reg.names()):
                        try:
                            prim = self.reg.get(pname)
                        except Exception:
                            continue
                        if getattr(prim, "family", "") not in (
                                "acquired", "promoted"):
                            continue
                        if Effect.PURE not in tuple(
                                getattr(prim, "effects", ()) or ()):
                            continue
                        try:
                            pins = [k for k, v in prim.inputs.items()
                                    if not v.optional]
                        except Exception:
                            continue
                        if len(pins) != 1:
                            continue
                        for col in in_names:
                            try:
                                got = tuple(
                                    _vc(prim.fn(**{pins[0]: a[col]}))
                                    for a, _ in ex)
                            except Exception:
                                continue
                            if got == vvec:
                                child_src = "behavioral_reuse"
                                reuse_prim = pname
                                reuse_bind = {pins[0]: col}
                                break
                        if child_src == "behavioral_reuse":
                            break
                if (child_src == "behavioral_fold"
                        and _recursion_depth < MAX_RECURSION_DEPTH
                        and structured_inners):
                    try:
                        child_sub = self._decompose_inner(
                            f"{goal} [lift_tuple value]",
                            val_ex, t0,
                            _recursion_depth=_recursion_depth + 1)
                    except Exception:
                        child_sub = None
                    if child_sub is not None:
                        child_src = "behavioral_compound_residual"
            if (numeric_inners and child_src == "behavioral_fold"
                    and child_sub is None):
                return None
            if (child_sub is not None
                    and child_src == "behavioral_compound_residual"):
                child_sub = self._rebind_contract(
                    child_sub, cname,
                    f"{goal} [structural lift_tuple value]")
            child = DecomposedChild(
                name=cname,
                index=0,
                description=f"{goal} [structural lift_tuple value]",
                projected_examples=list(val_ex),
                value_vector=[o for _, o in val_ex],
                source=child_src,
                sub_contract=child_sub,
                reuse_primitive=reuse_prim,
                reuse_arg_bind=reuse_bind,
                const_value=const_v,
            )
            recon = Reconstructor(
                family="structural_wrap",
                op={"op": "lift_tuple"},
                child_refs=[cname],
            )
            evidence = (n - 1) / n
            search = 1.0 / (1.0 + math.log10(1.0 + max(counter[0], 1)))
            hier = (
                1 + int((child_sub.provenance or {}).get(
                    "hierarchy_depth", 0) or 0)
                if child_sub is not None else 1)
            return DecompositionContract(
                parent_goal=goal,
                reconstructor=recon,
                children=[child],
                n_examples=n,
                input_names=list(in_names),
                confidence=round(evidence * search, 4),
                provenance={
                    "builder": "behavioral_decomposer.v41_structural_wrap",
                    "search_policy_version": SEARCH_POLICY_VERSION,
                    "method": "structural_wrap_lift_tuple",
                    "wrap_op": "lift_tuple",
                    "n_components": 1,
                    "hierarchy_depth": hier,
                    "hierarchical": child_sub is not None,
                    "policy_version": SEARCH_POLICY_VERSION,
                    "examples_sha256": _sha256_examples(ex),
                    "recursion_depth": _recursion_depth,
                },
                policy_version=SEARCH_POLICY_VERSION,
            )



    def _try_varlen_induction(
            self, goal: str,
            ex: Sequence[Tuple[Dict[str, Any], Any]],
            in_names: List[str],
            num_in: List[str],
            target: Tuple[Any, ...],
            t0: float,
            counter: List[int],
            _recursion_depth: int = 0,
            ) -> Optional["DecompositionContract"]:
        """Length-indexed / element-wise list induction (variable length).

        Fixed-k ``structural_product`` requires uniform arity across examples.
        This path owns ragged (or multi-length) list outputs via two general
        reconstructors — never per-length branches:

          * repeat(value, count) — each output list is uniform in its
            elements; length and element are independent children.
          * map_elements — a list-valued input is transformed element-wise
            by a unary child; length equals the input list length.

        Requires >=2 distinct lengths in the evidence so the rule is not a
        disguised fixed-k product. Generic — no goal-text or length tables.
        """
        from swarm_engine.acquisition.gap_reasoner import (
            _decomposition_base_name)
        from swarm_engine.cognition.synthesis import SEARCH_POLICY_VERSION

        if self._algebra.domain != "list":
            return None
        outs = [o for _, o in ex]
        n = len(ex)
        if not outs or not all(isinstance(o, list) for o in outs):
            return None
        lengths = [len(o) for o in outs]
        if len(set(lengths)) < 2:
            return None  # single cardinality → fixed-k / GENERATE own it
        if n < 4:
            return None

        def _vc(v: Any) -> Any:
            if _is_num_scalar(v):
                return _canon(v)
            if _is_dict_value(v):
                return _canon_dict(v)
            if _is_list_value(v):
                return _canon_list(v)
            if isinstance(v, tuple):
                return tuple(_vc(x) for x in v)
            if isinstance(v, (set, frozenset)):
                return ("$set", tuple(sorted((_vc(x) for x in v), key=str)))
            return v

        def _classify_values(vals: List[Any], slot_ex, cname: str, desc: str,
                             allow_nested: bool = True) -> Optional[DecomposedChild]:
            """Classify a projected child (const/param/reuse/fold/compound)."""
            child_src = "behavioral_fold"
            child_sub = None
            reuse_prim = None
            reuse_bind = None
            const_v = None
            vvec = tuple(_vc(v) for v in vals)
            if all(v == vals[0] for v in vals):
                child_src = "behavioral_const"
                const_v = vals[0]
            else:
                for col in in_names:
                    try:
                        colv = tuple(_vc(a[col]) for a, _ in ex)
                    except Exception:
                        continue
                    if colv == vvec:
                        child_src = "behavioral_param"
                        reuse_prim = col
                        break
                if child_src == "behavioral_fold":
                    for pname in sorted(self.reg.names()):
                        try:
                            prim = self.reg.get(pname)
                        except Exception:
                            continue
                        if getattr(prim, "family", "") not in (
                                "acquired", "promoted"):
                            continue
                        if Effect.PURE not in tuple(
                                getattr(prim, "effects", ()) or ()):
                            continue
                        try:
                            pins = [kk for kk, vv in prim.inputs.items()
                                    if not vv.optional]
                        except Exception:
                            continue
                        if len(pins) != 1:
                            continue
                        for col in in_names:
                            try:
                                got = tuple(
                                    _vc(prim.fn(**{pins[0]: a[col]}))
                                    for a, _ in ex)
                            except Exception:
                                continue
                            if got == vvec:
                                child_src = "behavioral_reuse"
                                reuse_prim = pname
                                reuse_bind = {pins[0]: col}
                                break
                        if child_src == "behavioral_reuse":
                            break
                if (child_src == "behavioral_fold"
                        and allow_nested
                        and _recursion_depth < MAX_RECURSION_DEPTH
                        and all(isinstance(v, (dict, list, tuple))
                                for v in vals)):
                    try:
                        child_sub = self._decompose_inner(
                            desc, slot_ex, t0,
                            _recursion_depth=_recursion_depth + 1)
                    except Exception:
                        child_sub = None
                    if child_sub is not None:
                        child_sub = self._rebind_contract(
                            child_sub, cname, desc)
                        child_src = "behavioral_compound_residual"
            return DecomposedChild(
                name=cname,
                index=0,  # overwritten by caller
                description=desc,
                projected_examples=list(slot_ex),
                value_vector=list(vals),
                source=child_src,
                sub_contract=child_sub,
                reuse_primitive=reuse_prim,
                reuse_arg_bind=reuse_bind,
                const_value=const_v,
            )

        base = _decomposition_base_name(goal)

        # --- Pattern A: map_elements over a list-valued input ---------------
        list_cols = [
            nm for nm in in_names
            if all(isinstance(a[nm], list) for a, _ in ex)
        ]
        for col in list_cols:
            if not all(len(a[col]) == len(o) for a, o in ex):
                continue
            # Functional consistency: same element → same image everywhere.
            pairs = {}
            consistent = True
            for a, o in ex:
                for x, y in zip(a[col], o):
                    k = _vc(x)
                    yk = _vc(y)
                    if k in pairs and pairs[k] != yk:
                        consistent = False
                        break
                    pairs[k] = yk
                if not consistent:
                    break
            if not consistent:
                continue
            # Pre-check: identity map?
            is_identity = all(
                all(_vc(x) == _vc(y) for x, y in zip(a[col], o))
                for a, o in ex)
            # Pre-check reuse of unary acquired/promoted on elements
            reuse_prim = None
            reuse_bind = None
            if not is_identity:
                for pname in sorted(self.reg.names()):
                    try:
                        prim = self.reg.get(pname)
                    except Exception:
                        continue
                    if getattr(prim, "family", "") not in (
                            "acquired", "promoted"):
                        continue
                    if Effect.PURE not in tuple(
                            getattr(prim, "effects", ()) or ()):
                        continue
                    try:
                        pins = [kk for kk, vv in prim.inputs.items()
                                if not vv.optional]
                    except Exception:
                        continue
                    if len(pins) != 1:
                        continue
                    try:
                        match = True
                        for a, o in ex:
                            for x, y in zip(a[col], o):
                                if _vc(prim.fn(**{pins[0]: x})) != _vc(y):
                                    match = False
                                    break
                            if not match:
                                break
                    except Exception:
                        match = False
                    if match:
                        reuse_prim = pname
                        reuse_bind = {pins[0]: "item"}
                        break
            # Need enough non-empty samples for element-rule evidence.
            nonempty = [(a, o) for a, o in ex if o]
            if len(nonempty) < 3 and reuse_prim is None and not is_identity:
                continue
            # Build children
            items_child = DecomposedChild(
                name=f"{base}_c0",
                index=0,
                description=f"{goal} [varlen map items]",
                projected_examples=[(dict(a), a[col]) for a, _ in ex],
                value_vector=[a[col] for a, _ in ex],
                source="behavioral_param",
                reuse_primitive=col,
            )
            if is_identity:
                # identity on foreach item — assembly inlines identity prim
                elem_child = DecomposedChild(
                    name=f"{base}_c1",
                    index=1,
                    description=f"{goal} [varlen map element]",
                    projected_examples=[
                        ({"item": a[col][0]}, a[col][0])
                        if a[col] else ({"item": 0}, 0)
                        for a, _ in ex],
                    value_vector=[
                        (a[col][0] if a[col] else 0) for a, _ in ex],
                    source="behavioral_reuse",
                    reuse_primitive="identity",
                    reuse_arg_bind={"value": "item"},
                )
            elif reuse_prim is not None:
                elem_child = DecomposedChild(
                    name=f"{base}_c1",
                    index=1,
                    description=f"{goal} [varlen map element]",
                    projected_examples=[
                        ({"item": a[col][0]}, o[0])
                        if o else ({"item": None}, None)
                        for a, o in ex],
                    value_vector=[(o[0] if o else None) for _, o in ex],
                    source="behavioral_reuse",
                    reuse_primitive=reuse_prim,
                    reuse_arg_bind=reuse_bind,
                )
            else:
                # Leave as behavioral_fold so GENERATE/compose acquires the
                # element rule from per-row element samples. Parent assembly
                # still validates full lists element-wise.
                # Replace empty-row placeholders with a duplicate of a
                # nonempty sample so I5 length matches and GENERATE sees
                # only real pairs.
                samples = []
                for a, o in ex:
                    if o:
                        samples.append(({"item": a[col][0]}, o[0]))
                    else:
                        samples.append(None)
                fill = next((s for s in samples if s is not None), None)
                if fill is None:
                    continue
                proj = [s if s is not None else (dict(fill[0]), fill[1])
                        for s in samples]
                # Optional nested decomp on structured element outputs
                child_sub = None
                child_src = "behavioral_fold"
                vals = [v for _, v in proj]
                if (_recursion_depth < MAX_RECURSION_DEPTH
                        and all(isinstance(v, (dict, list, tuple))
                                for v in vals)):
                    try:
                        child_sub = self._decompose_inner(
                            f"{goal} [varlen map element]",
                            proj, t0,
                            _recursion_depth=_recursion_depth + 1)
                    except Exception:
                        child_sub = None
                    if child_sub is not None:
                        child_sub = self._rebind_contract(
                            child_sub, f"{base}_c1",
                            f"{goal} [varlen map element]")
                        child_src = "behavioral_compound_residual"
                elem_child = DecomposedChild(
                    name=f"{base}_c1",
                    index=1,
                    description=f"{goal} [varlen map element]",
                    projected_examples=proj,
                    value_vector=vals,
                    source=child_src,
                    sub_contract=child_sub,
                )
            recon = Reconstructor(
                family="varlen_sequence",
                op={"op": "map_elements", "items_param": col},
                child_refs=[items_child.name, elem_child.name],
            )
            evidence = (n - 1) / n
            search = 1.0 / (1.0 + math.log10(1.0 + max(counter[0], 1)))
            return DecompositionContract(
                parent_goal=goal,
                reconstructor=recon,
                children=[items_child, elem_child],
                n_examples=n,
                input_names=list(in_names),
                confidence=round(evidence * search, 4),
                provenance={
                    "builder": "behavioral_decomposer.v45_varlen_sequence",
                    "search_policy_version": SEARCH_POLICY_VERSION,
                    "method": "varlen_map_elements",
                    "items_param": col,
                    "n_distinct_lengths": len(set(lengths)),
                    "lengths_sample": sorted(set(lengths))[:8],
                    "hierarchy_depth": 1 + (
                        int((elem_child.sub_contract.provenance or {}).get(
                            "hierarchy_depth", 0) or 0)
                        if elem_child.sub_contract is not None else 0),
                    "policy_version": SEARCH_POLICY_VERSION,
                    "examples_sha256": _sha256_examples(ex),
                    "recursion_depth": _recursion_depth,
                },
                policy_version=SEARCH_POLICY_VERSION,
            )

        # --- Pattern B: repeat(value, count) --------------------------------
        # Each non-empty list must be uniform in its elements.
        elem_vals: List[Any] = []
        for o in outs:
            if not o:
                elem_vals.append(None)  # placeholder; filled below
                continue
            head = o[0]
            if any(_vc(x) != _vc(head) for x in o):
                return None  # non-uniform → not a repeat
            elem_vals.append(head)
        nonempty_elems = [v for v in elem_vals if v is not None]
        if not nonempty_elems:
            return None
        # Fill empty-row placeholders with a representative so child
        # classification still sees n examples; count=0 reconstructs [].
        fill_e = nonempty_elems[0]
        elem_vals = [v if v is not None else fill_e for v in elem_vals]
        count_vals = lengths  # length IS the count child

        elem_ex = [(dict(a), ev) for (a, _), ev in zip(ex, elem_vals)]
        count_ex = [(dict(a), cv) for (a, _), cv in zip(ex, count_vals)]

        elem_child = _classify_values(
            elem_vals, elem_ex, f"{base}_c0",
            f"{goal} [varlen repeat value]")
        if elem_child is None:
            return None
        elem_child.index = 0
        count_child = _classify_values(
            count_vals, count_ex, f"{base}_c1",
            f"{goal} [varlen repeat count]",
            allow_nested=False)
        if count_child is None:
            return None
        count_child.index = 1
        # Count must be numeric non-negative ints
        if not all(isinstance(c, int) and not isinstance(c, bool) and c >= 0
                   for c in count_vals):
            return None

        recon = Reconstructor(
            family="varlen_sequence",
            op={"op": "repeat"},
            child_refs=[elem_child.name, count_child.name],
        )
        evidence = (n - 1) / n
        search = 1.0 / (1.0 + math.log10(1.0 + max(counter[0], 1)))
        hier = 1
        if elem_child.sub_contract is not None:
            hier = max(hier, 1 + int(
                (elem_child.sub_contract.provenance or {}).get(
                    "hierarchy_depth", 0) or 0))
        return DecompositionContract(
            parent_goal=goal,
            reconstructor=recon,
            children=[elem_child, count_child],
            n_examples=n,
            input_names=list(in_names),
            confidence=round(evidence * search, 4),
            provenance={
                "builder": "behavioral_decomposer.v45_varlen_sequence",
                "search_policy_version": SEARCH_POLICY_VERSION,
                "method": "varlen_repeat",
                "n_distinct_lengths": len(set(lengths)),
                "lengths_sample": sorted(set(lengths))[:8],
                "hierarchy_depth": hier,
                "policy_version": SEARCH_POLICY_VERSION,
                "examples_sha256": _sha256_examples(ex),
                "recursion_depth": _recursion_depth,
            },
            policy_version=SEARCH_POLICY_VERSION,
        )



    def _try_set_product(
            self, goal: str,
            ex: Sequence[Tuple[Dict[str, Any], Any]],
            in_names: List[str],
            num_in: List[str],
            target: Tuple[Any, ...],
            t0: float,
            counter: List[int],
            _recursion_depth: int = 0,
            ) -> Optional["DecompositionContract"]:
        """Fixed-cardinality multi-element set/frozenset → positional product.

        Singleton sets stay with structural_wrap(lift_set). Uniform-size
        sets of size k in [2, MAX_COMPONENTS] are peeled in canonical
        sorted order into independent children and reconstructed via
        as_set(pack_list(...)) / as_frozenset(...). Generic — no per-size
        branches.
        """
        from swarm_engine.acquisition.gap_reasoner import (
            _decomposition_base_name)
        from swarm_engine.cognition.synthesis import SEARCH_POLICY_VERSION

        outs = [o for _, o in ex]
        n = len(ex)
        if not outs:
            return None
        if all(isinstance(o, set) and not isinstance(o, frozenset)
               for o in outs):
            kind = "set"
            pack_op = "pack_set"
        elif all(isinstance(o, frozenset) for o in outs):
            kind = "frozenset"
            pack_op = "pack_frozenset"
        else:
            return None
        if not all(len(o) == len(outs[0]) for o in outs):
            return None
        k = len(outs[0])
        if not (2 <= k <= MAX_COMPONENTS):
            return None
        if n < k + 2:
            return None

        def _vc(v: Any) -> Any:
            if _is_num_scalar(v):
                return _canon(v)
            if _is_dict_value(v):
                return _canon_dict(v)
            if _is_list_value(v):
                return _canon_list(v)
            if isinstance(v, tuple):
                return tuple(_vc(x) for x in v)
            return v

        def _canon_order(s):
            # Prefer natural ordering for homogeneous comparable elements
            # (ints/floats/str); fall back to typed repr — never raw str()
            # alone, which mis-orders multi-digit ints ("12" < "2").
            try:
                return sorted(s)
            except TypeError:
                return sorted(
                    s, key=lambda v: (type(v).__name__, repr(_vc(v))))

        ordered = [_canon_order(o) for o in outs]
        # Uniform per-slot type name across examples
        for i in range(k):
            shapes = [type(row[i]).__name__ for row in ordered]
            if len(set(shapes)) != 1:
                return None

        base = _decomposition_base_name(goal)
        children: List[DecomposedChild] = []
        child_refs: List[str] = []
        hierarchy_depth = 1
        for i in range(k):
            cname = f"{base}_c{i}"
            child_refs.append(cname)
            slot_ex = [(dict(a), ordered[j][i]) for j, (a, _) in enumerate(ex)]
            vals = [o for _, o in slot_ex]
            vvec = tuple(_vc(v) for v in vals)
            child_src = "behavioral_fold"
            child_sub = None
            reuse_prim = None
            reuse_bind = None
            const_v = None
            if all(v == vals[0] for v in vals):
                child_src = "behavioral_const"
                const_v = vals[0]
            else:
                for col in in_names:
                    try:
                        colv = tuple(_vc(a[col]) for a, _ in ex)
                    except Exception:
                        continue
                    if colv == vvec:
                        child_src = "behavioral_param"
                        reuse_prim = col
                        break
                if child_src == "behavioral_fold":
                    for pname in sorted(self.reg.names()):
                        try:
                            prim = self.reg.get(pname)
                        except Exception:
                            continue
                        if getattr(prim, "family", "") not in (
                                "acquired", "promoted"):
                            continue
                        if Effect.PURE not in tuple(
                                getattr(prim, "effects", ()) or ()):
                            continue
                        try:
                            pins = [kk for kk, vv in prim.inputs.items()
                                    if not vv.optional]
                        except Exception:
                            continue
                        if len(pins) != 1:
                            continue
                        for col in in_names:
                            try:
                                got = tuple(
                                    _vc(prim.fn(**{pins[0]: a[col]}))
                                    for a, _ in ex)
                            except Exception:
                                continue
                            if got == vvec:
                                child_src = "behavioral_reuse"
                                reuse_prim = pname
                                reuse_bind = {pins[0]: col}
                                break
                        if child_src == "behavioral_reuse":
                            break
                if (child_src == "behavioral_fold"
                        and _recursion_depth < MAX_RECURSION_DEPTH
                        and all(isinstance(v, (dict, list, tuple, set,
                                               frozenset))
                                for v in vals)):
                    try:
                        child_sub = self._decompose_inner(
                            f"{goal} [set product slot {i}]",
                            slot_ex, t0,
                            _recursion_depth=_recursion_depth + 1)
                    except Exception:
                        child_sub = None
                    if child_sub is not None:
                        child_sub = self._rebind_contract(
                            child_sub, cname,
                            f"{goal} [set product slot {i}]")
                        child_src = "behavioral_compound_residual"
                        hierarchy_depth = max(
                            hierarchy_depth,
                            1 + int((child_sub.provenance or {}).get(
                                "hierarchy_depth", 0) or 0))
            children.append(DecomposedChild(
                name=cname,
                index=i,
                description=f"{goal} [set product slot {i}]",
                projected_examples=list(slot_ex),
                value_vector=list(vals),
                source=child_src,
                sub_contract=child_sub,
                reuse_primitive=reuse_prim,
                reuse_arg_bind=reuse_bind,
                const_value=const_v,
            ))

        recon = Reconstructor(
            family="structural_product",
            op={"op": pack_op},
            child_refs=child_refs,
        )
        evidence = (n - 1) / n
        search = 1.0 / (1.0 + math.log10(1.0 + max(counter[0], 1)))
        return DecompositionContract(
            parent_goal=goal,
            reconstructor=recon,
            children=children,
            n_examples=n,
            input_names=list(in_names),
            confidence=round(evidence * search, 4),
            provenance={
                "builder": "behavioral_decomposer.v45_set_product",
                "search_policy_version": SEARCH_POLICY_VERSION,
                "method": f"structural_product_{pack_op}",
                "pack_op": pack_op,
                "set_kind": kind,
                "n_components": k,
                "hierarchy_depth": hierarchy_depth,
                "hierarchical": any(
                    ch.sub_contract is not None for ch in children),
                "policy_version": SEARCH_POLICY_VERSION,
                "examples_sha256": _sha256_examples(ex),
                "recursion_depth": _recursion_depth,
            },
            policy_version=SEARCH_POLICY_VERSION,
        )



    def _try_varlen_set_induction(
            self, goal: str,
            ex: Sequence[Tuple[Dict[str, Any], Any]],
            in_names: List[str],
            num_in: List[str],
            target: Tuple[Any, ...],
            t0: float,
            counter: List[int],
            _recursion_depth: int = 0,
            ) -> Optional["DecompositionContract"]:
        """Variable-cardinality set/frozenset induction (ragged sets).

        Fixed-k ``structural_product`` / ``pack_set`` requires uniform
        cardinality. This path owns ragged set outputs via general
        reconstructors — never per-cardinality branches:

          * as_set_of — output equals set(list_input) (dups collapse)
          * map_to_set — list input mapped element-wise then as_set
          * range_set — output equals set(range(count_child))

        Requires >=2 distinct cardinalities so the rule is not a disguised
        fixed-k product. Generic — no goal-text or size tables.
        """
        from swarm_engine.acquisition.gap_reasoner import (
            _decomposition_base_name)
        from swarm_engine.cognition.synthesis import SEARCH_POLICY_VERSION

        outs = [o for _, o in ex]
        n = len(ex)
        if not outs:
            return None
        if all(isinstance(o, set) and not isinstance(o, frozenset)
               for o in outs):
            set_kind = "set"
            as_name = "as_set"
        elif all(isinstance(o, frozenset) for o in outs):
            set_kind = "frozenset"
            as_name = "as_frozenset"
        else:
            return None
        cards = [len(o) for o in outs]
        if len(set(cards)) < 2:
            return None  # single cardinality → fixed-k / singleton wrap
        if n < 4:
            return None

        def _vc(v: Any) -> Any:
            if _is_num_scalar(v):
                return _canon(v)
            if _is_dict_value(v):
                return _canon_dict(v)
            if _is_list_value(v):
                return _canon_list(v)
            if isinstance(v, tuple):
                return tuple(_vc(x) for x in v)
            if isinstance(v, (set, frozenset)):
                return ("$set", tuple(sorted((_vc(x) for x in v), key=str)))
            return v

        def _classify_values(vals: List[Any], slot_ex, cname: str, desc: str,
                             allow_nested: bool = True
                             ) -> Optional[DecomposedChild]:
            child_src = "behavioral_fold"
            child_sub = None
            reuse_prim = None
            reuse_bind = None
            const_v = None
            vvec = tuple(_vc(v) for v in vals)
            if all(v == vals[0] for v in vals):
                child_src = "behavioral_const"
                const_v = vals[0]
            else:
                for col in in_names:
                    try:
                        colv = tuple(_vc(a[col]) for a, _ in ex)
                    except Exception:
                        continue
                    if colv == vvec:
                        child_src = "behavioral_param"
                        reuse_prim = col
                        break
                if child_src == "behavioral_fold":
                    for pname in sorted(self.reg.names()):
                        try:
                            prim = self.reg.get(pname)
                        except Exception:
                            continue
                        if getattr(prim, "family", "") not in (
                                "acquired", "promoted"):
                            continue
                        if Effect.PURE not in tuple(
                                getattr(prim, "effects", ()) or ()):
                            continue
                        try:
                            pins = [kk for kk, vv in prim.inputs.items()
                                    if not vv.optional]
                        except Exception:
                            continue
                        if len(pins) != 1:
                            continue
                        for col in in_names:
                            try:
                                got = tuple(
                                    _vc(prim.fn(**{pins[0]: a[col]}))
                                    for a, _ in ex)
                            except Exception:
                                continue
                            if got == vvec:
                                child_src = "behavioral_reuse"
                                reuse_prim = pname
                                reuse_bind = {pins[0]: col}
                                break
                        if child_src == "behavioral_reuse":
                            break
                if (child_src == "behavioral_fold"
                        and allow_nested
                        and _recursion_depth < MAX_RECURSION_DEPTH
                        and all(isinstance(v, (dict, list, tuple))
                                for v in vals)):
                    try:
                        child_sub = self._decompose_inner(
                            desc, slot_ex, t0,
                            _recursion_depth=_recursion_depth + 1)
                    except Exception:
                        child_sub = None
                    if child_sub is not None:
                        child_sub = self._rebind_contract(
                            child_sub, cname, desc)
                        child_src = "behavioral_compound_residual"
            return DecomposedChild(
                name=cname,
                index=0,
                description=desc,
                projected_examples=list(slot_ex),
                value_vector=list(vals),
                source=child_src,
                sub_contract=child_sub,
                reuse_primitive=reuse_prim,
                reuse_arg_bind=reuse_bind,
                const_value=const_v,
            )

        base = _decomposition_base_name(goal)

        # --- Pattern A: as_set_of / map_to_set over a list-valued input -----
        list_cols = [
            nm for nm in in_names
            if all(isinstance(a[nm], list) for a, _ in ex)
        ]
        for col in list_cols:
            # Functional consistency: same element → same image everywhere,
            # and output set equals {f(x) for x in xs} (dups may collapse).
            pairs: Dict[Any, Any] = {}
            consistent = True
            covers = True
            for a, o in ex:
                images = []
                for x in a[col]:
                    k = _vc(x)
                    # provisional identity; refine below via pairs
                    images.append(x)
                    if k not in pairs:
                        pairs[k] = None  # filled after we know f
                # Will fill after determining transform
            # Try identity first: set(xs) == o
            if all(set(a[col]) == o for a, o in ex):
                items_vals = [list(a[col]) for a, _ in ex]
                items_ex = [(dict(a), list(a[col])) for a, _ in ex]
                items_child = _classify_values(
                    items_vals, items_ex, f"{base}_c0",
                    f"{goal} [varlen set items]", allow_nested=False)
                if items_child is None:
                    continue
                items_child.index = 0
                # Force param binding to the list column when classification
                # found it (usual case).
                if items_child.source == "behavioral_fold":
                    items_child.source = "behavioral_param"
                    items_child.reuse_primitive = col
                recon = Reconstructor(
                    family="varlen_set",
                    op={"op": "as_set_of", "items_param": col,
                        "set_kind": set_kind, "as_op": as_name},
                    child_refs=[items_child.name],
                )
                evidence = (n - 1) / n
                search = 1.0 / (1.0 + math.log10(1.0 + max(counter[0], 1)))
                return DecompositionContract(
                    parent_goal=goal,
                    reconstructor=recon,
                    children=[items_child],
                    n_examples=n,
                    input_names=list(in_names),
                    confidence=round(evidence * search, 4),
                    provenance={
                        "builder": "behavioral_decomposer.v46_varlen_set",
                        "search_policy_version": SEARCH_POLICY_VERSION,
                        "method": "varlen_as_set_of",
                        "items_param": col,
                        "set_kind": set_kind,
                        "n_distinct_cardinalities": len(set(cards)),
                        "cardinalities_sample": sorted(set(cards))[:8],
                        "policy_version": SEARCH_POLICY_VERSION,
                        "examples_sha256": _sha256_examples(ex),
                        "recursion_depth": _recursion_depth,
                    },
                    policy_version=SEARCH_POLICY_VERSION,
                )

            # map_to_set: unary element transform then as_set.
            # Order is lost in sets, so do NOT sorted-zip. Prefer:
            #   (1) reuse of a known unary acquired/promoted prim
            #   (2) singleton-list evidence to seed an element fold child;
            #       parent validation still checks full set equality.
            reuse_prim = None
            reuse_bind = None
            for pname in sorted(self.reg.names()):
                try:
                    prim = self.reg.get(pname)
                except Exception:
                    continue
                if getattr(prim, "family", "") not in (
                        "acquired", "promoted"):
                    continue
                if Effect.PURE not in tuple(
                        getattr(prim, "effects", ()) or ()):
                    continue
                try:
                    pins = [kk for kk, vv in prim.inputs.items()
                            if not vv.optional]
                except Exception:
                    continue
                if len(pins) != 1:
                    continue
                try:
                    ok = True
                    for a, o in ex:
                        got = {prim.fn(**{pins[0]: x}) for x in a[col]}
                        if got != o:
                            ok = False
                            break
                except Exception:
                    ok = False
                if ok:
                    reuse_prim = pname
                    reuse_bind = {pins[0]: "item"}
                    break

            singleton_pairs = []
            for a, o in ex:
                if len(a[col]) == 1 and len(o) == 1:
                    singleton_pairs.append(
                        (a[col][0], next(iter(o))))

            if reuse_prim is not None or len(singleton_pairs) >= 2:
                items_vals = [list(a[col]) for a, _ in ex]
                items_ex = [(dict(a), list(a[col])) for a, _ in ex]
                items_child = DecomposedChild(
                    name=f"{base}_c0",
                    index=0,
                    description=f"{goal} [varlen set map items]",
                    projected_examples=list(items_ex),
                    value_vector=list(items_vals),
                    source="behavioral_param",
                    reuse_primitive=col,
                )
                if reuse_prim is not None:
                    elem_vals = []
                    elem_ex = []
                    for a, o in ex:
                        x0 = a[col][0] if a[col] else None
                        try:
                            y0 = self.reg.get(reuse_prim).fn(
                                **{list(reuse_bind.keys())[0]: x0})
                        except Exception:
                            y0 = next(iter(o)) if o else None
                        elem_vals.append(y0)
                        elem_ex.append(({"item": x0}, y0))
                    elem_child = DecomposedChild(
                        name=f"{base}_c1",
                        index=1,
                        description=f"{goal} [varlen set map element]",
                        projected_examples=list(elem_ex),
                        value_vector=list(elem_vals),
                        source="behavioral_reuse",
                        reuse_primitive=reuse_prim,
                        reuse_arg_bind=reuse_bind,
                    )
                else:
                    # Seed element fold from singleton evidence only.
                    # Deduplicate by canonical input.
                    seen = {}
                    for x, y in singleton_pairs:
                        k = _vc(x)
                        if k in seen and _vc(seen[k]) != _vc(y):
                            seen = None
                            break
                        seen[k] = y
                    if not seen:
                        continue
                    elem_ex = [({"item": x}, y)
                               for x, y in singleton_pairs]
                    # Pad to n examples for I5 using cycling singletons
                    while len(elem_ex) < n:
                        elem_ex.append(elem_ex[len(elem_ex) % len(singleton_pairs)])
                    elem_ex = elem_ex[:n]
                    elem_vals = [y for _, y in elem_ex]
                    elem_child = _classify_values(
                        elem_vals, elem_ex, f"{base}_c1",
                        f"{goal} [varlen set map element]")
                    if elem_child is None:
                        continue
                    elem_child.index = 1
                recon = Reconstructor(
                    family="varlen_set",
                    op={"op": "map_to_set", "items_param": col,
                        "set_kind": set_kind, "as_op": as_name},
                    child_refs=[items_child.name, elem_child.name],
                )
                evidence = (n - 1) / n
                search = 1.0 / (1.0 + math.log10(1.0 + max(counter[0], 1)))
                return DecompositionContract(
                    parent_goal=goal,
                    reconstructor=recon,
                    children=[items_child, elem_child],
                    n_examples=n,
                    input_names=list(in_names),
                    confidence=round(evidence * search, 4),
                    provenance={
                        "builder": "behavioral_decomposer.v46_varlen_set",
                        "search_policy_version": SEARCH_POLICY_VERSION,
                        "method": "varlen_map_to_set",
                        "items_param": col,
                        "set_kind": set_kind,
                        "n_distinct_cardinalities": len(set(cards)),
                        "cardinalities_sample": sorted(set(cards))[:8],
                        "seeded_from_singletons": reuse_prim is None,
                        "policy_version": SEARCH_POLICY_VERSION,
                        "examples_sha256": _sha256_examples(ex),
                        "recursion_depth": _recursion_depth,
                    },
                    policy_version=SEARCH_POLICY_VERSION,
                )

        # --- Pattern B: range_set — set(range(count)) ----------------------
        # Each output must equal set(range(k)) for k = len(o).
        if all(
            o == (frozenset(range(len(o))) if set_kind == "frozenset"
                  else set(range(len(o))))
            for o in outs
        ):
            count_vals = list(cards)
            count_ex = [(dict(a), c) for (a, _), c in zip(ex, count_vals)]
            count_child = _classify_values(
                count_vals, count_ex, f"{base}_c0",
                f"{goal} [varlen set range count]", allow_nested=False)
            if count_child is not None:
                count_child.index = 0
                recon = Reconstructor(
                    family="varlen_set",
                    op={"op": "range_set", "set_kind": set_kind,
                        "as_op": as_name},
                    child_refs=[count_child.name],
                )
                evidence = (n - 1) / n
                search = 1.0 / (1.0 + math.log10(1.0 + max(counter[0], 1)))
                return DecompositionContract(
                    parent_goal=goal,
                    reconstructor=recon,
                    children=[count_child],
                    n_examples=n,
                    input_names=list(in_names),
                    confidence=round(evidence * search, 4),
                    provenance={
                        "builder": "behavioral_decomposer.v46_varlen_set",
                        "search_policy_version": SEARCH_POLICY_VERSION,
                        "method": "varlen_range_set",
                        "set_kind": set_kind,
                        "n_distinct_cardinalities": len(set(cards)),
                        "cardinalities_sample": sorted(set(cards))[:8],
                        "policy_version": SEARCH_POLICY_VERSION,
                        "examples_sha256": _sha256_examples(ex),
                        "recursion_depth": _recursion_depth,
                    },
                    policy_version=SEARCH_POLICY_VERSION,
                )

        return None

    def _try_varlen_bytes_induction(
            self, goal: str,
            ex: Sequence[Tuple[Dict[str, Any], Any]],
            in_names: List[str],
            num_in: List[str],
            target: Tuple[Any, ...],
            t0: float,
            counter: List[int],
            _recursion_depth: int = 0,
            ) -> Optional["DecompositionContract"]:
        """Multi-byte / bytearray induction (beyond length-1 to_bytes).

        Length-1 bytes stay with structural_wrap(to_bytes). This path owns
        multi-byte outputs via general reconstructors — never per-length
        branches:

          * as_bytes_of — output equals as_bytes(list_input)
          * map_to_bytes — list input mapped element-wise then as_bytes
          * bytes_product — fixed-length multi-byte peel into ordinal children

        For ragged lengths require >=2 distinct lengths. For uniform
        length k in [2, MAX_COMPONENTS], bytes_product applies.
        """
        from swarm_engine.acquisition.gap_reasoner import (
            _decomposition_base_name)
        from swarm_engine.cognition.synthesis import SEARCH_POLICY_VERSION

        outs = [o for _, o in ex]
        n = len(ex)
        if not outs:
            return None
        if all(isinstance(o, bytes) and not isinstance(o, bytearray)
               for o in outs):
            byte_kind = "bytes"
            as_name = "as_bytes"
        elif all(isinstance(o, bytearray) for o in outs):
            byte_kind = "bytearray"
            as_name = "as_bytearray"
        else:
            return None
        lengths = [len(o) for o in outs]
        # Length-1 is owned by to_bytes wrap
        if all(L == 1 for L in lengths):
            return None
        if n < 4:
            return None

        def _vc(v: Any) -> Any:
            if _is_num_scalar(v):
                return _canon(v)
            if _is_dict_value(v):
                return _canon_dict(v)
            if _is_list_value(v):
                return _canon_list(v)
            if isinstance(v, tuple):
                return tuple(_vc(x) for x in v)
            return v

        def _classify_values(vals: List[Any], slot_ex, cname: str, desc: str,
                             allow_nested: bool = True
                             ) -> Optional[DecomposedChild]:
            child_src = "behavioral_fold"
            child_sub = None
            reuse_prim = None
            reuse_bind = None
            const_v = None
            vvec = tuple(_vc(v) for v in vals)
            if all(v == vals[0] for v in vals):
                child_src = "behavioral_const"
                const_v = vals[0]
            else:
                for col in in_names:
                    try:
                        colv = tuple(_vc(a[col]) for a, _ in ex)
                    except Exception:
                        continue
                    if colv == vvec:
                        child_src = "behavioral_param"
                        reuse_prim = col
                        break
                if child_src == "behavioral_fold":
                    for pname in sorted(self.reg.names()):
                        try:
                            prim = self.reg.get(pname)
                        except Exception:
                            continue
                        if getattr(prim, "family", "") not in (
                                "acquired", "promoted"):
                            continue
                        if Effect.PURE not in tuple(
                                getattr(prim, "effects", ()) or ()):
                            continue
                        try:
                            pins = [kk for kk, vv in prim.inputs.items()
                                    if not vv.optional]
                        except Exception:
                            continue
                        if len(pins) != 1:
                            continue
                        for col in in_names:
                            try:
                                got = tuple(
                                    _vc(prim.fn(**{pins[0]: a[col]}))
                                    for a, _ in ex)
                            except Exception:
                                continue
                            if got == vvec:
                                child_src = "behavioral_reuse"
                                reuse_prim = pname
                                reuse_bind = {pins[0]: col}
                                break
                        if child_src == "behavioral_reuse":
                            break
            return DecomposedChild(
                name=cname,
                index=0,
                description=desc,
                projected_examples=list(slot_ex),
                value_vector=list(vals),
                source=child_src,
                sub_contract=child_sub,
                reuse_primitive=reuse_prim,
                reuse_arg_bind=reuse_bind,
                const_value=const_v,
            )

        base = _decomposition_base_name(goal)
        ordinals = [list(o) for o in outs]  # list of ints 0..255

        # --- Pattern A: as_bytes_of list input ------------------------------
        list_cols = [
            nm for nm in in_names
            if all(isinstance(a[nm], list) for a, _ in ex)
        ]
        for col in list_cols:
            if all(list(a[col]) == ord_row
                   for (a, _), ord_row in zip(ex, ordinals)):
                items_vals = [list(a[col]) for a, _ in ex]
                items_ex = [(dict(a), list(a[col])) for a, _ in ex]
                items_child = _classify_values(
                    items_vals, items_ex, f"{base}_c0",
                    f"{goal} [varlen bytes items]", allow_nested=False)
                if items_child is None:
                    continue
                items_child.index = 0
                if items_child.source == "behavioral_fold":
                    items_child.source = "behavioral_param"
                    items_child.reuse_primitive = col
                # Ragged OR uniform multi-byte both OK for as_bytes_of
                if len(set(lengths)) < 2 and lengths[0] < 2:
                    continue
                recon = Reconstructor(
                    family="varlen_bytes",
                    op={"op": "as_bytes_of", "items_param": col,
                        "byte_kind": byte_kind, "as_op": as_name},
                    child_refs=[items_child.name],
                )
                evidence = (n - 1) / n
                search = 1.0 / (1.0 + math.log10(1.0 + max(counter[0], 1)))
                return DecompositionContract(
                    parent_goal=goal,
                    reconstructor=recon,
                    children=[items_child],
                    n_examples=n,
                    input_names=list(in_names),
                    confidence=round(evidence * search, 4),
                    provenance={
                        "builder": "behavioral_decomposer.v46_varlen_bytes",
                        "search_policy_version": SEARCH_POLICY_VERSION,
                        "method": "varlen_as_bytes_of",
                        "items_param": col,
                        "byte_kind": byte_kind,
                        "n_distinct_lengths": len(set(lengths)),
                        "lengths_sample": sorted(set(lengths))[:8],
                        "policy_version": SEARCH_POLICY_VERSION,
                        "examples_sha256": _sha256_examples(ex),
                        "recursion_depth": _recursion_depth,
                    },
                    policy_version=SEARCH_POLICY_VERSION,
                )

            # map_to_bytes: set(f(x)) style but for lists of ordinals
            # Require equal lengths and functional element map
            if not all(len(a[col]) == len(o) for a, o in ex):
                continue
            pairs = {}
            consistent = True
            for a, o in ex:
                for x, y in zip(a[col], o):
                    k = _vc(x)
                    yk = int(y) & 0xFF
                    if k in pairs and pairs[k] != yk:
                        consistent = False
                        break
                    pairs[k] = yk
                if not consistent:
                    break
            if not consistent:
                continue
            if len(set(lengths)) < 2:
                # Still allow map_to_bytes for uniform multi if k>=2
                if lengths[0] < 2:
                    continue
            items_vals = [list(a[col]) for a, _ in ex]
            items_ex = [(dict(a), list(a[col])) for a, _ in ex]
            items_child = DecomposedChild(
                name=f"{base}_c0",
                index=0,
                description=f"{goal} [varlen bytes map items]",
                projected_examples=list(items_ex),
                value_vector=list(items_vals),
                source="behavioral_param",
                reuse_primitive=col,
            )
            elem_vals = []
            elem_ex = []
            for a, o in ex:
                x0 = a[col][0]
                y0 = int(o[0]) & 0xFF
                elem_vals.append(y0)
                elem_ex.append(({"item": x0}, y0))
            elem_child = _classify_values(
                elem_vals, elem_ex, f"{base}_c1",
                f"{goal} [varlen bytes map element]")
            if elem_child is None:
                continue
            elem_child.index = 1
            recon = Reconstructor(
                family="varlen_bytes",
                op={"op": "map_to_bytes", "items_param": col,
                    "byte_kind": byte_kind, "as_op": as_name},
                child_refs=[items_child.name, elem_child.name],
            )
            evidence = (n - 1) / n
            search = 1.0 / (1.0 + math.log10(1.0 + max(counter[0], 1)))
            return DecompositionContract(
                parent_goal=goal,
                reconstructor=recon,
                children=[items_child, elem_child],
                n_examples=n,
                input_names=list(in_names),
                confidence=round(evidence * search, 4),
                provenance={
                    "builder": "behavioral_decomposer.v46_varlen_bytes",
                    "search_policy_version": SEARCH_POLICY_VERSION,
                    "method": "varlen_map_to_bytes",
                    "items_param": col,
                    "byte_kind": byte_kind,
                    "n_distinct_lengths": len(set(lengths)),
                    "lengths_sample": sorted(set(lengths))[:8],
                    "policy_version": SEARCH_POLICY_VERSION,
                    "examples_sha256": _sha256_examples(ex),
                    "recursion_depth": _recursion_depth,
                },
                policy_version=SEARCH_POLICY_VERSION,
            )

        # --- Pattern B: fixed-k bytes_product (uniform length >= 2) --------
        if len(set(lengths)) == 1:
            k = lengths[0]
            if 2 <= k <= MAX_COMPONENTS and n >= k + 2:
                children = []
                child_refs = []
                for i in range(k):
                    cname = f"{base}_c{i}"
                    child_refs.append(cname)
                    vals = [ord_row[i] for ord_row in ordinals]
                    slot_ex = [(dict(a), vals[j])
                               for j, (a, _) in enumerate(ex)]
                    ch = _classify_values(
                        vals, slot_ex, cname,
                        f"{goal} [bytes product slot {i}]")
                    if ch is None:
                        children = []
                        break
                    ch.index = i
                    children.append(ch)
                if children and len(children) == k:
                    recon = Reconstructor(
                        family="varlen_bytes",
                        op={"op": "bytes_product",
                            "byte_kind": byte_kind, "as_op": as_name,
                            "n_components": k},
                        child_refs=child_refs,
                    )
                    evidence = (n - 1) / n
                    search = 1.0 / (1.0 + math.log10(
                        1.0 + max(counter[0], 1)))
                    return DecompositionContract(
                        parent_goal=goal,
                        reconstructor=recon,
                        children=children,
                        n_examples=n,
                        input_names=list(in_names),
                        confidence=round(evidence * search, 4),
                        provenance={
                            "builder":
                                "behavioral_decomposer.v46_varlen_bytes",
                            "search_policy_version": SEARCH_POLICY_VERSION,
                            "method": "varlen_bytes_product",
                            "byte_kind": byte_kind,
                            "n_components": k,
                            "policy_version": SEARCH_POLICY_VERSION,
                            "examples_sha256": _sha256_examples(ex),
                            "recursion_depth": _recursion_depth,
                        },
                        policy_version=SEARCH_POLICY_VERSION,
                    )


        # --- Pattern C: range_bytes — bytes(range(count)) -------------------
        if all(list(o) == list(range(len(o))) for o in outs):
            if len(set(lengths)) < 2:
                pass  # uniform owned by bytes_product if k>=2
            else:
                count_vals = list(lengths)
                count_ex = [(dict(a), c)
                            for (a, _), c in zip(ex, count_vals)]
                count_child = _classify_values(
                    count_vals, count_ex, f"{base}_c0",
                    f"{goal} [varlen bytes range count]",
                    allow_nested=False)
                if count_child is not None:
                    count_child.index = 0
                    recon = Reconstructor(
                        family="varlen_bytes",
                        op={"op": "as_bytes_of",  # assembled via range_list
                            "via": "range_list",
                            "byte_kind": byte_kind, "as_op": as_name},
                        child_refs=[count_child.name],
                    )
                    # Wait: as_bytes_of with 1 child that's count needs
                    # special assembly. Use dedicated op range_bytes.
                    recon = Reconstructor(
                        family="varlen_bytes",
                        op={"op": "range_bytes",
                            "byte_kind": byte_kind, "as_op": as_name},
                        child_refs=[count_child.name],
                    )
                    evidence = (n - 1) / n
                    search = 1.0 / (1.0 + math.log10(
                        1.0 + max(counter[0], 1)))
                    return DecompositionContract(
                        parent_goal=goal,
                        reconstructor=recon,
                        children=[count_child],
                        n_examples=n,
                        input_names=list(in_names),
                        confidence=round(evidence * search, 4),
                        provenance={
                            "builder":
                                "behavioral_decomposer.v46_varlen_bytes",
                            "search_policy_version": SEARCH_POLICY_VERSION,
                            "method": "varlen_range_bytes",
                            "byte_kind": byte_kind,
                            "n_distinct_lengths": len(set(lengths)),
                            "lengths_sample": sorted(set(lengths))[:8],
                            "policy_version": SEARCH_POLICY_VERSION,
                            "examples_sha256": _sha256_examples(ex),
                            "recursion_depth": _recursion_depth,
                        },
                        policy_version=SEARCH_POLICY_VERSION,
                    )

        return None



    def _try_set_algebra(
            self, goal: str,
            ex: Sequence[Tuple[Dict[str, Any], Any]],
            in_names: List[str],
            num_in: List[str],
            target: Tuple[Any, ...],
            t0: float,
            counter: List[int],
            _recursion_depth: int = 0,
            ) -> Optional["DecompositionContract"]:
        """Binary set algebra over two set-valued inputs.

        Detects union / intersect / difference (and frozenset_union) when
        both inputs are set-like and the output matches the op on every
        example. Generic — no per-size branches.
        """
        from swarm_engine.acquisition.gap_reasoner import (
            _decomposition_base_name)
        from swarm_engine.cognition.synthesis import SEARCH_POLICY_VERSION

        outs = [o for _, o in ex]
        n = len(ex)
        if n < 4 or not outs:
            return None
        if all(isinstance(o, set) and not isinstance(o, frozenset)
               for o in outs):
            out_kind = "set"
            candidates = (
                ("set_union", lambda a, b: set(a) | set(b)),
                ("set_intersect", lambda a, b: set(a) & set(b)),
                ("set_difference", lambda a, b: set(a) - set(b)),
            )
        elif all(isinstance(o, frozenset) for o in outs):
            out_kind = "frozenset"
            candidates = (
                ("frozenset_union",
                 lambda a, b: frozenset(a) | frozenset(b)),
                ("set_intersect",
                 lambda a, b: frozenset(set(a) & set(b))),
                ("set_difference",
                 lambda a, b: frozenset(set(a) - set(b))),
            )
        else:
            return None

        set_cols = [
            nm for nm in in_names
            if all(isinstance(a[nm], (set, frozenset)) for a, _ in ex)
        ]
        if len(set_cols) < 2:
            return None

        def _as_param_child(col, cname, desc, idx):
            vals = [a[col] for a, _ in ex]
            slot_ex = [(dict(a), a[col]) for a, _ in ex]
            return DecomposedChild(
                name=cname,
                index=idx,
                description=desc,
                projected_examples=list(slot_ex),
                value_vector=list(vals),
                source="behavioral_param",
                reuse_primitive=col,
            )

        base = _decomposition_base_name(goal)
        for col_a in set_cols:
            for col_b in set_cols:
                if col_a == col_b:
                    continue
                for op_name, fn in candidates:
                    try:
                        if all(fn(a[col_a], a[col_b]) == o
                               for a, o in ex):
                            pass
                        else:
                            continue
                    except Exception:
                        continue
                    child_a = _as_param_child(
                        col_a, f"{base}_c0",
                        f"{goal} [set algebra left]", 0)
                    child_b = _as_param_child(
                        col_b, f"{base}_c1",
                        f"{goal} [set algebra right]", 1)
                    recon = Reconstructor(
                        family="set_algebra",
                        op={"op": op_name, "left_param": col_a,
                            "right_param": col_b, "out_kind": out_kind},
                        child_refs=[child_a.name, child_b.name],
                    )
                    evidence = (n - 1) / n
                    search = 1.0 / (1.0 + math.log10(
                        1.0 + max(counter[0], 1)))
                    return DecompositionContract(
                        parent_goal=goal,
                        reconstructor=recon,
                        children=[child_a, child_b],
                        n_examples=n,
                        input_names=list(in_names),
                        confidence=round(evidence * search, 4),
                        provenance={
                            "builder":
                                "behavioral_decomposer.v46_set_algebra",
                            "search_policy_version": SEARCH_POLICY_VERSION,
                            "method": f"set_algebra_{op_name}",
                            "left_param": col_a,
                            "right_param": col_b,
                            "out_kind": out_kind,
                            "policy_version": SEARCH_POLICY_VERSION,
                            "examples_sha256": _sha256_examples(ex),
                            "recursion_depth": _recursion_depth,
                        },
                        policy_version=SEARCH_POLICY_VERSION,
                    )
        return None


    def _try_structural_product(
            self, goal: str,
            ex: Sequence[Tuple[Dict[str, Any], Any]],
            in_names: List[str],
            num_in: List[str],
            target: Tuple[Any, ...],
            t0: float,
            counter: List[int],
            _recursion_depth: int = 0,
            ) -> Optional["DecompositionContract"]:
        """Fixed-length heterogeneous sequence → positional product.

        Singleton wraps cover len==1 containers. Fixed-length lists/tuples
        with at least one structured slot (dict/list/tuple) peel each
        position as an independent child and reconstruct via pack_list /
        pack_tuple (assembled as lift+append / lift_tuple+tuple_concat).
        Generic — no per-shape or per-goal handlers. All-atomic sequences
        stay with GENERATE/fold.
        """
        from swarm_engine.acquisition.gap_reasoner import (
            _decomposition_base_name)
        from swarm_engine.cognition.synthesis import SEARCH_POLICY_VERSION

        if self._algebra.domain not in ("list", "tuple"):
            return None
        outs = [o for _, o in ex]
        n = len(ex)
        is_list = self._algebra.domain == "list"
        kind = list if is_list else tuple
        if not all(isinstance(o, kind) for o in outs):
            return None
        if not outs or not all(len(o) == len(outs[0]) for o in outs):
            return None
        k = len(outs[0])
        if not (2 <= k <= MAX_COMPONENTS):
            return None
        if n < k + 2:
            return None
        # Require at least one structured slot so all-atomic fixed lists
        # remain owned by GENERATE / list_concat fold.
        def _structured(v: Any) -> bool:
            return isinstance(v, (dict, list, tuple))
        if not any(_structured(outs[0][i]) for i in range(k)):
            return None
        # Uniform per-slot container-ness across examples (ragged morph refuse).
        for i in range(k):
            shapes = [type(o[i]).__name__ for o in outs]
            if len(set(shapes)) != 1:
                return None

        def _vc(v: Any) -> Any:
            if _is_num_scalar(v):
                return _canon(v)
            if _is_dict_value(v):
                return _canon_dict(v)
            if _is_list_value(v):
                return _canon_list(v)
            if isinstance(v, tuple):
                return tuple(_vc(x) for x in v)
            return v

        base = _decomposition_base_name(goal)
        children: List[DecomposedChild] = []
        child_refs: List[str] = []
        hierarchy_depth = 1
        for i in range(k):
            cname = f"{base}_c{i}"
            child_refs.append(cname)
            slot_ex = [(dict(a), o[i]) for a, o in ex]
            vals = [o for _, o in slot_ex]
            vvec = tuple(_vc(v) for v in vals)
            child_src = "behavioral_fold"
            child_sub = None
            reuse_prim = None
            reuse_bind = None
            const_v = None
            if all(v == vals[0] for v in vals):
                child_src = "behavioral_const"
                const_v = vals[0]
            else:
                for col in in_names:
                    try:
                        colv = tuple(_vc(a[col]) for a, _ in ex)
                    except Exception:
                        continue
                    if colv == vvec:
                        child_src = "behavioral_param"
                        reuse_prim = col
                        break
                if child_src == "behavioral_fold":
                    for pname in sorted(self.reg.names()):
                        try:
                            prim = self.reg.get(pname)
                        except Exception:
                            continue
                        if getattr(prim, "family", "") not in (
                                "acquired", "promoted"):
                            continue
                        if Effect.PURE not in tuple(
                                getattr(prim, "effects", ()) or ()):
                            continue
                        try:
                            pins = [kk for kk, vv in prim.inputs.items()
                                    if not vv.optional]
                        except Exception:
                            continue
                        if len(pins) != 1:
                            continue
                        for col in in_names:
                            try:
                                got = tuple(
                                    _vc(prim.fn(**{pins[0]: a[col]}))
                                    for a, _ in ex)
                            except Exception:
                                continue
                            if got == vvec:
                                child_src = "behavioral_reuse"
                                reuse_prim = pname
                                reuse_bind = {pins[0]: col}
                                break
                        if child_src == "behavioral_reuse":
                            break
                if (child_src == "behavioral_fold"
                        and _recursion_depth < MAX_RECURSION_DEPTH
                        and all(_structured(v) for v in vals)):
                    try:
                        child_sub = self._decompose_inner(
                            f"{goal} [product slot {i}]",
                            slot_ex, t0,
                            _recursion_depth=_recursion_depth + 1)
                    except Exception:
                        child_sub = None
                    if child_sub is not None:
                        child_sub = self._rebind_contract(
                            child_sub, cname,
                            f"{goal} [product slot {i}]")
                        child_src = "behavioral_compound_residual"
                        hierarchy_depth = max(
                            hierarchy_depth,
                            1 + int((child_sub.provenance or {}).get(
                                "hierarchy_depth", 0) or 0))
            children.append(DecomposedChild(
                name=cname,
                index=i,
                description=f"{goal} [structural product slot {i}]",
                projected_examples=list(slot_ex),
                value_vector=list(vals),
                source=child_src,
                sub_contract=child_sub,
                reuse_primitive=reuse_prim,
                reuse_arg_bind=reuse_bind,
                const_value=const_v,
            ))

        pack_op = "pack_list" if is_list else "pack_tuple"
        recon = Reconstructor(
            family="structural_product",
            op={"op": pack_op},
            child_refs=child_refs,
        )
        evidence = (n - 1) / n
        search = 1.0 / (1.0 + math.log10(1.0 + max(counter[0], 1)))
        return DecompositionContract(
            parent_goal=goal,
            reconstructor=recon,
            children=children,
            n_examples=n,
            input_names=list(in_names),
            confidence=round(evidence * search, 4),
            provenance={
                "builder": "behavioral_decomposer.v42_structural_product",
                "search_policy_version": SEARCH_POLICY_VERSION,
                "method": f"structural_product_{pack_op}",
                "pack_op": pack_op,
                "n_components": k,
                "hierarchy_depth": hierarchy_depth,
                "hierarchical": any(
                    ch.sub_contract is not None for ch in children),
                "policy_version": SEARCH_POLICY_VERSION,
                "examples_sha256": _sha256_examples(ex),
                "recursion_depth": _recursion_depth,
            },
            policy_version=SEARCH_POLICY_VERSION,
        )

    def decompose(self, goal: str,
                  examples: Sequence[Tuple[Dict[str, Any], Any]]
                  ) -> Optional[DecompositionContract]:
        """Decompose one requirement's worked examples, or return None.

        Fails closed (None) for: too few examples, non-uniform inputs,
        non-numeric-scalar outputs, objectives the existing grammar already
        reaches atomically (fail-fast), no exact bounded decomposition
        within the wall-clock budget, or any internal error. Deterministic:
        identical examples always yield the identical contract.
        """
        t0 = time.perf_counter()
        try:
            return self._decompose_inner(goal, examples, t0,
                                         _recursion_depth=0)
        except _Deadline:
            return None
        except Exception:
            # Fail closed: a decomposition search must never break
            # acquisition with an exception. (Unit tests call the
            # internals directly so bugs still surface loudly there.)
            return None

    def _decompose_inner(self, goal: str,
                         examples: Sequence[Tuple[Dict[str, Any], Any]],
                         t0: float,
                         _recursion_depth: int = 0
                         ) -> Optional[DecompositionContract]:
        _saved_algebra = self._algebra
        try:
            from swarm_engine.acquisition.gap_reasoner import (
                _decomposition_base_name)
            from swarm_engine.cognition.synthesis import SEARCH_POLICY_VERSION

            ex = [(dict(a), o) for a, o in (examples or [])]
            n = len(ex)
            if n < 4:
                return None  # I7 needs n >= k+2 >= 4 (k >= 2 always)
            in_names = sorted(ex[0][0].keys())
            if not in_names:
                return None
            if any(set(a.keys()) != set(in_names) for a, _ in ex):
                return None
            outs = [o for _, o in ex]
            all_num = all(_is_num_scalar(o) for o in outs)
            all_bit = all(_is_bit_scalar(o) for o in outs)
            all_str = all(isinstance(o, str) for o in outs)
            all_tuple = all(isinstance(o, tuple) for o in outs)
            all_list = (all(_is_list_value(o) for o in outs) and not all_tuple)
            all_dict = all(_is_dict_value(o) for o in outs)
            # Domain selection (v23/v24/v27/v31): numeric add, bit xor, string
            # concat, list/tuple concat, or dict merge (prefix/key residual).
            if getattr(self, "_force_bit", False) and all_bit:
                self._algebra = _BIT_ALGEBRA
            elif all_num:
                self._algebra = _NUMERIC_ALGEBRA
            elif all_bit:
                self._algebra = _BIT_ALGEBRA
            elif all_str:
                self._algebra = _STRING_ALGEBRA
            elif all_tuple:
                self._algebra = _TUPLE_ALGEBRA
            elif all_list:
                self._algebra = _LIST_ALGEBRA
            elif all_dict:
                self._algebra = _DICT_ALGEBRA
            elif (all(isinstance(o, (set, frozenset)) for o in outs)
                  or all(isinstance(o, (bytes, bytearray)) for o in outs)):
                # Representation adapters (set/bytes): no fold algebra yet.
                # Borrow list algebra as a harmless placeholder so the
                # structural-wrap path can run; wrap must succeed or we refuse.
                self._algebra = _LIST_ALGEBRA
            else:
                return None  # unsupported output representation
            num_in = [nm for nm in in_names
                      if all(_is_num_scalar(a[nm]) for a, _ in ex)]
            # Bit parents may still have numeric inputs (cross-representation:
            # numeric columns in, bit/bool out). Non-numeric inputs are ignored
            # by the generative pool; reuse may still bind them later.
            if all(isinstance(o, (set, frozenset, bytes, bytearray)) for o in outs):
                target = tuple(outs)  # identity; fold algebra unused
            else:
                target = tuple(self._algebra.canon(o) for _, o in ex)
            _check_deadline(t0, self.wall_clock_s)

            counter = [0]
            # Representation adapters (set/bytes): try structural wrap BEFORE
            # list/dict pool construction — those pools assume foldable
            # containers and can abort on set/bytes targets.
            if all(isinstance(o, (set, frozenset, bytes, bytearray)) for o in outs):
                wrap_contract = self._try_structural_wrap(
                    goal, ex, in_names, num_in, target, t0, counter,
                    _recursion_depth=_recursion_depth)
                if wrap_contract is not None:
                    return wrap_contract
                # v45: fixed-cardinality multi-element set/frozenset product
                if all(isinstance(o, (set, frozenset)) for o in outs):
                    set_contract = self._try_set_product(
                        goal, ex, in_names, num_in, target, t0, counter,
                        _recursion_depth=_recursion_depth)
                    if set_contract is not None:
                        return set_contract
                    # v46: ragged / variable-cardinality sets
                    vset = self._try_varlen_set_induction(
                        goal, ex, in_names, num_in, target, t0, counter,
                        _recursion_depth=_recursion_depth)
                    if vset is not None:
                        return vset
                    salg = self._try_set_algebra(
                        goal, ex, in_names, num_in, target, t0, counter,
                        _recursion_depth=_recursion_depth)
                    if salg is not None:
                        return salg
                if all(isinstance(o, (bytes, bytearray)) for o in outs):
                    # v46: multi-byte / bytearray (beyond length-1 to_bytes)
                    vbytes = self._try_varlen_bytes_induction(
                        goal, ex, in_names, num_in, target, t0, counter,
                        _recursion_depth=_recursion_depth)
                    if vbytes is not None:
                        return vbytes
                return None  # no fold path for these representations yet

            # Pool: numeric generative grammar (bit-filtered when needed), or
            # dedicated string pool (serialize + mined const fragments).
            if self._algebra.domain == "string":
                tier_a, pool_all = self._build_string_pool(
                    ex, num_in, in_names, target, counter)
            elif self._algebra.domain in ("list", "tuple"):
                tier_a, pool_all = self._build_list_pool(
                    ex, num_in, in_names, target, counter)
            elif self._algebra.domain == "dict":
                tier_a, pool_all = self._build_dict_pool(
                    ex, num_in, in_names, target, counter)
            else:
                tier_a, pool_all = self._build_pool(ex, num_in, target, counter)
                if self._algebra.domain == "bit":
                    def _bitify(entries):
                        out = []
                        for vec, label in entries:
                            if all(_is_bit_scalar(x) for x in vec):
                                out.append((self._algebra.canon_vec(vec), label))
                        return out
                    tier_a = _bitify(tier_a)
                    pool_all = _bitify(pool_all)
            by_all = {vec: i for i, (vec, _label) in enumerate(pool_all)}
            # Atomicity fail-fast: if the existing generator grammar already
            # expresses this objective exactly, decomposition adds nothing --
            # the normal strategies own the atomic case. This keeps every
            # previously-atomic acquisition on its existing route.
            # String Tier-B (concat-of-Tier-A) is residual-search only: it does
            # NOT imply GENERATE can emit the target atomically, so atomicity
            # for strings is Tier-A only.
            # v33: structural wrap for singleton nested containers that the
            # Tier-A key-peel would otherwise mark atomic (e.g. {"v": [x]}).
            # Emits structural_wrap(kv/lift) with one child = the nested value.
            wrap_contract = self._try_structural_wrap(
                goal, ex, in_names, num_in, target, t0, counter,
                _recursion_depth=_recursion_depth)
            if wrap_contract is not None:
                return wrap_contract
            # v42: fixed-length heterogeneous sequences (non-singleton) peel
            # positional slots and reconstruct via pack_list/pack_tuple.
            product_contract = self._try_structural_product(
                goal, ex, in_names, num_in, target, t0, counter,
                _recursion_depth=_recursion_depth)
            if product_contract is not None:
                return product_contract
            # v45: variable-length induction (repeat / map_elements) when
            # fixed-k product cannot apply (ragged lengths or list-map).
            varlen_contract = self._try_varlen_induction(
                goal, ex, in_names, num_in, target, t0, counter,
                _recursion_depth=_recursion_depth)
            if varlen_contract is not None:
                return varlen_contract

            atomic_pool = (
                {vec for vec, _ in tier_a}
                if self._algebra.domain in ("string", "list", "tuple", "dict")
                else by_all)
            if target in atomic_pool:
                return None
            _check_deadline(t0, self.wall_clock_s)

            # Reuse tier (P10): acquired/promoted caps whose value vectors are
            # NOT already in Tier A join the residual-search component set.
            # Map: value vector -> registered primitive name (behavioral match).
            reuse_map: Dict[Tuple[Any, ...], str] = {}
            # Optional arg remapping for cross-name cross-arity reuse:
            # prim_input -> parent_param.
            reuse_bind_map: Dict[Tuple[Any, ...], Dict[str, str]] = {}
            # v38: serialize(reuse:prim|pin=col) string-pool entries → assembly
            # can inline serialize(prim(col)) without a separate GENERATE child.
            serialize_reuse_map: Dict[Tuple[Any, ...], Dict[str, str]] = {}
            if self._algebra.domain == "string":
                for vec, label in list(tier_a) + list(pool_all):
                    if not isinstance(label, str):
                        continue
                    if not label.startswith("serialize(reuse:"):
                        continue
                    # serialize(reuse:NAME|pin=col)
                    try:
                        body = label[len("serialize(reuse:"):]
                        if body.endswith(")"):
                            body = body[:-1]
                        pname, bind_s = body.split("|", 1)
                        pin, col = bind_s.split("=", 1)
                        serialize_reuse_map[vec] = {
                            "prim": pname, "pin": pin, "col": col,
                        }
                    except Exception:
                        continue
            by_a_labels = {vec: label for vec, label in tier_a}
            for vec, label in self._reuse_entries(
                    ex, num_in, target, by_a_labels, counter, t0=t0):
                # label is "reuse:<primname>" or "reuse:<primname>|a=x,b=y"
                rest = label.split(":", 1)[1]
                if "|" in rest:
                    prim_name, bind_s = rest.split("|", 1)
                    bind = {}
                    for pair in bind_s.split(","):
                        if not pair or "=" not in pair:
                            continue
                        k, v = pair.split("=", 1)
                        bind[k] = v
                    if bind:
                        reuse_bind_map[vec] = bind
                else:
                    prim_name = rest
                reuse_map[vec] = prim_name
            # Merge: Tier-A first (stable ranks), then reuse vectors.
            # String domain: also include Tier-B (concat-of-Tier-A) so
            # residual search can see composed fragments like serialize(x)+"!".
            if self._algebra.domain in ("string", "list", "tuple", "dict"):
                vecs_a = [vec for vec, _label in pool_all]
            else:
                vecs_a = [vec for vec, _label in tier_a]
            by_a = {vec: i for i, vec in enumerate(vecs_a)}
            for vec in reuse_map:
                if vec not in by_a:
                    by_a[vec] = len(vecs_a)
                    vecs_a.append(vec)
            # Whole-target acquired match: if some acquired/promoted unary
            # already realizes the parent vector exactly, fail closed -- the
            # existing capability owns the objective (reuse atomicity).
            # _reuse_entries skips equals-target for components; probe here.
            # Whole-target acquired match on any numeric column (multi-input
            # atomicity): if some acquired unary already realizes the parent
            # vector on a column, fail closed.
            if any(self._acquired_realizes(ex, col, target) for col in num_in):
                return None
            k_max = min(self.max_components, n - 2)  # I7: n >= k+2
            if self._algebra.domain in ("string", "list", "tuple", "dict"):
                k_max = min(k_max, 2)  # prefix-residual concat is k=2

            # Fold search, fewest components first.
            fold_best_k = 0
            fold_best: List[Tuple[Tuple[Any, ...], ...]] = []
            for k in range(2, k_max + 1):
                sols = [s for s in self._fold_solutions(
                    target, vecs_a, by_a, k, t0, counter)
                    if self._nondegenerate(target, s)]
                if sols:
                    fold_best_k, fold_best = k, sols
                    break

            # Nest search: only when it can improve on the fold answer
            # (fewer components; ties go to the shallower fold).
            # Bit-domain (xor) folds: skip nest — outer unaries in the
            # generative numeric grammar do not apply to bool vectors; a
            # bool-not peel is a follow-on deepening, not required to cross
            # the representation wall.
            nest_cands: List[Tuple[int, int, Tuple[str, ...],
                                     Tuple[Tuple[Any, ...], ...]]] = []
            if (self._algebra.domain == "numeric"
                    and (fold_best_k == 0 or fold_best_k > 2)):
                limit_k = (fold_best_k - 1) if fold_best_k else k_max
                if limit_k >= 2:
                    nest_cands = [
                        c for c in self._nest_solutions(
                            target, pool_all, vecs_a, by_a, limit_k,
                            t0, counter,
                            reuse_vecs=list(reuse_map.keys()))
                        if self._nondegenerate(target, c[3])]

            flat: List[Tuple[int, int, Tuple[str, ...],
                             Tuple[Tuple[Any, ...], ...]]] = []
            for s in fold_best:
                flat.append((fold_best_k, 0, (), s))
            flat.extend(nest_cands)

            # Hierarchical compound-residual search (P9): compete with flat
            # only when flat arity exceeds HIERARCHY_FLAT_TRIGGER (or no flat
            # exists). Preserves the P8 depth-1 2-/3-way band bit-for-bit;
            # general recursive extension for larger folds.
            hier_raw: List[Tuple[int, int, Tuple[str, ...],
                                 Tuple[Tuple[Any, ...], ...],
                                 Optional["DecompositionContract"], int]] = []
            if fold_best_k > HIERARCHY_FLAT_TRIGGER:
                try:
                    hier_raw = self._compound_residual_solutions(
                        target, ex, goal, vecs_a, by_a, t0, counter,
                        _recursion_depth, fold_best, fold_best_k)
                except _Deadline:
                    hier_raw = []  # keep flat candidates; hierarchy optional

            if not flat and not hier_raw:
                # Cross-representation fallback: a {0,1}-int target with no
                # additive partition may still be an xor fold over bit
                # components. Forced bit retry via domain override flag.
                if (self._algebra.domain == "numeric"
                        and all(_is_bit_scalar(o) for _, o in ex)
                        and not getattr(self, "_force_bit", False)
                        and _recursion_depth == 0):
                    self._force_bit = True
                    try:
                        return self._decompose_inner(
                            goal, examples, t0, _recursion_depth=0)
                    finally:
                        self._force_bit = False
                return None
            # Adjudication (P8-AdjA + P9): collapse enumeration rotations into
            # order-free DISTINCT partitions -- rotations of one component
            # multiset are not ambiguity (the old raw count made the ambiguity
            # term a disguised 1/(1+k)). Selection is fewest components, then
            # shallowest depth, then lowest total pool rank (simplest
            # components first). Hierarchical candidates carry an optional
            # sub-contract on the compound residual side; flat/nest carry None.
            # Extended entries: (k, depth, ops, vecs, sub_or_None, compound_idx)
            # with compound_idx=-1 for flat/nest.
            def _freeze_op(o):
                if isinstance(o, dict):
                    return ("binlit", o.get("op"), o.get("free_index"),
                            tuple(sorted((o.get("bound") or {}).items())))
                return ("unary", o)

            def _partition_key(e):
                kk, dd, oo, vecs = e[0], e[1], e[2], e[3]
                return (kk, dd, tuple(_freeze_op(o) for o in oo),
                        tuple(sorted(vecs)))

            extended: List[Tuple] = []
            for e in flat:
                extended.append((e[0], e[1], e[2], e[3], None, -1))
            for e in hier_raw:
                extended.append(e)

            dedup: Dict[Tuple, Tuple[int, int, Tuple]] = {}
            for e in extended:
                key = _partition_key(e)
                # Pool rank: Tier-A / reuse sides use by_a index; compound
                # residual sides get a large sentinel so ties prefer more
                # atomic splits. Reuse count: general (identity-independent)
                # preference for partitions that match more existing caps.
                total_rank = 0
                n_reuse = 0
                for v in e[3]:
                    if v in reuse_map:
                        n_reuse += 1
                    if v in by_a:
                        total_rank += by_a[v]
                    else:
                        total_rank += 10 ** 9
                prev = dedup.get(key)
                # Store (-n_reuse, total_rank, entry) so more reuse sorts first
                # after (k, depth).
                score = (-n_reuse, total_rank)
                if prev is None or score < (prev[0], prev[1]):
                    dedup[key] = (-n_reuse, total_rank, e)
            ranked = sorted(
                dedup.values(),
                key=lambda te: (te[2][0], te[2][1], te[0], te[1]))
            best_nreuse_neg, best_rank, (k, depth, ops, comp_vecs, win_sub,
                                         compound_idx) = ranked[0]
            winner_n_reuse = -best_nreuse_neg
            # I7 (P8-AdjC): one redundant example beyond the vacuity bound.
            # n=k+1 admits spurious residual coincidences (measured 3/60
            # truth-divergent emissions at n=3, 0/60 at n>=4 on adversarial
            # targets with partially out-of-pool truth). The winner has the
            # fewest components, so if it violates the bound every
            # alternative does too -- fail closed.
            if n < k + 2:
                return None
            # Ambiguity counts distinct same-(k, depth) partitions, winner
            # included (formula 1/(1+n)); rotations no longer inflate it.
            same = [(nr, tr, e) for nr, tr, e in ranked
                    if e[0] == k and e[1] == depth]
            n_alt = len(same)
            alternatives = [
                {"component_vectors": [[_json_scalar(x) for x in v]
                                       for v in sorted(e[3])],
                 "outer_ops": list(e[2]),
                 "total_pool_rank": tr,
                 "n_reuse": -nr,
                 "hierarchical": e[4] is not None}
                for nr, tr, e in same[1:17]  # winner excluded; record capped
            ]

            # Hierarchical winners are associative folds at this level (the
            # compound residual carries its own family). Nest winners keep
            # depth>0 from the nest search (compound_idx == -1).
            is_hierarchical = win_sub is not None
            if is_hierarchical or depth == 0:
                family: str = "associative_fold"
                op: Any = self._algebra.op_name
            else:
                # ops is outermost-first; the reconstructor records the outer
                # unary op names innermost-first for direct chained application.
                family = "nest_chain"
                op = list(reversed(ops))
            # I2: the op(s) must name registered primitives -- checked here,
            # not trusted from the search.
            if isinstance(op, str):
                _op_names = [op]
            else:
                _op_names = []
                for o in list(op):
                    if isinstance(o, str):
                        _op_names.append(o)
                    elif isinstance(o, dict) and o.get("op"):
                        _op_names.append(o["op"])
                    else:
                        return None
            if any(self.reg.get(nm) is None for nm in _op_names):
                return None
            # I3 (gate): apply the reconstructor to the child value vectors and
            # demand exact reproduction of the parent outputs. I4: children
            # pairwise value-distinct.
            if len(set(comp_vecs)) != len(comp_vecs):
                return None
            _recon = self._apply_reconstructor(family, op, comp_vecs)
            if _recon is None or _recon != target:
                return None
            base = _decomposition_base_name(goal)
            # Generative Tier-A vectors (not reuse): membership gates the
            # out-of-pool child enrichment below.
            tier_a_vecs = {vec for vec, _label in tier_a}
            children: List[DecomposedChild] = []
            child_refs: List[str] = []
            hierarchy_depth = 0
            for i, vec in enumerate(comp_vecs):
                cname = f"{base}_c{i}"
                child_refs.append(cname)
                child_sub = None
                reuse_prim = None
                reuse_bind = None
                # Param-exact: child's values equal a parent input column --
                # bind via identity at assembly; not an acquisition gap.
                param_col = None
                for nm in in_names:
                    colvec = tuple(_canon(a[nm]) for a, _ in ex)
                    if colvec == vec:
                        param_col = nm
                        break
                if is_hierarchical and i == compound_idx:
                    src = "behavioral_compound_residual"
                    # Rebind nested contract so its parent node IS this child.
                    child_sub = self._rebind_contract(
                        win_sub, cname,
                        f"{goal} [behavioral component {i + 1} of {k}]")
                    hierarchy_depth = 1 + int(
                        child_sub.provenance.get("hierarchy_depth", 0) or 0)
                elif param_col is not None:
                    src = "behavioral_param"
                    reuse_prim = param_col
                elif vec in reuse_map:
                    src = "behavioral_reuse"
                    reuse_prim = reuse_map[vec]
                    reuse_bind = reuse_bind_map.get(vec)
                elif vec in serialize_reuse_map:
                    # Cross-rep residual: string component = serialize(acquired).
                    # Assembly sees __wrap__=serialize on the reuse bind and
                    # emits serialize(prim(col)) inline (no GENERATE child).
                    src = "behavioral_reuse"
                    meta = serialize_reuse_map[vec]
                    reuse_prim = meta["prim"]
                    reuse_bind = {
                        meta["pin"]: meta["col"],
                        "__wrap__": "serialize",
                    }
                elif len(vec) > 0 and all(v == vec[0] for v in vec):
                    # Constant component: inline at assembly (no acquire).
                    # Covers string/bit literals mined into the fold pool.
                    src = "behavioral_const"
                    reuse_prim = None
                elif is_hierarchical or depth == 0:
                    src = "behavioral_fold"
                else:
                    src = "behavioral_nest_inner"
                # Out-of-pool fold/nest-inner enrichment (v25): when a child
                # vector is absent from the generative Tier-A pool (so normal
                # GENERATE/compose cannot be assumed to rebuild it) and is not
                # already a reuse/param/const/hierarchical compound, attempt a
                # recursive decompose on the projected examples. On success the
                # child becomes a compound residual with a sub_contract -- the
                # resolver then autonomously reconstructs the missing dependency
                # closure (same-goal recovery after a reused leaf is revoked)
                # without any manual leaf re-acquire. Fail-closed: recursion
                # depth / deadline / decomp None leave the child as fold.
                if (src in ("behavioral_fold", "behavioral_nest_inner")
                        and child_sub is None
                        and vec not in tier_a_vecs
                        and _recursion_depth < MAX_RECURSION_DEPTH):
                    # v43: project ORIGINAL parent slices for dict/list/tuple
                    # domains. Canon vectors are hashable tuples-of-pairs /
                    # nested tuples; feeding them as examples makes the
                    # recursive decomposer switch domain (dict→tuple) and
                    # emit spurious pack_tuple over key-value pairs.
                    if self._algebra.domain == "dict" and vec:
                        try:
                            part0 = (_dict_from_canon(vec[0])
                                     if isinstance(vec[0], tuple)
                                     else dict(vec[0]))
                            keys = list(part0.keys())
                            proj = []
                            for (a, o), v in zip(ex, vec):
                                if isinstance(o, dict) and all(
                                        kk in o for kk in keys):
                                    proj.append(
                                        (dict(a), {kk: o[kk] for kk in keys}))
                                else:
                                    proj.append((dict(a), _dict_from_canon(v)
                                                 if isinstance(v, tuple)
                                                 else v))
                        except Exception:
                            proj = [(dict(a), v) for (a, _), v in zip(ex, vec)]
                    elif self._algebra.domain == "list" and vec:
                        proj = [(dict(a), _list_from_canon(v)
                                 if isinstance(v, tuple) else v)
                                for (a, _), v in zip(ex, vec)]
                    elif self._algebra.domain == "tuple" and vec:
                        def _to_tuple(x):
                            if isinstance(x, list):
                                return tuple(_to_tuple(y) for y in x)
                            if isinstance(x, dict):
                                return {k: _to_tuple(val)
                                        for k, val in x.items()}
                            return x
                        proj = [(dict(a), _to_tuple(_list_from_canon(v)
                                 if isinstance(v, tuple) else v))
                                for (a, _), v in zip(ex, vec)]
                    else:
                        proj = [(dict(a), v) for (a, _), v in zip(ex, vec)]
                    try:
                        sub = self._decompose_inner(
                            f"{goal} [behavioral component {i + 1} of {k}]",
                            proj, t0,
                            _recursion_depth=_recursion_depth + 1)
                    except _Deadline:
                        sub = None
                    except Exception:
                        sub = None
                    if sub is not None:
                        src = "behavioral_compound_residual"
                        child_sub = self._rebind_contract(
                            sub, cname,
                            f"{goal} [behavioral component {i + 1} of {k}]")
                        hierarchy_depth = max(
                            hierarchy_depth,
                            1 + int(child_sub.provenance.get(
                                "hierarchy_depth", 0) or 0))
                # reset bind unless reuse branch set it
                if src != "behavioral_reuse":
                    reuse_bind = None
                # v33: dict singleton wrap — if component is {K: vals} and
                # vals match a parent column or an acquired unary on a column
                # (numeric OR structured list/dict/tuple), emit behavioral_kv.
                if (self._algebra.domain == "dict"
                        and src == "behavioral_fold"):
                    try:
                        maps = [
                            (_dict_from_canon(v) if isinstance(v, tuple)
                             else dict(v))
                            for v in vec]
                    except Exception:
                        maps = []
                    if (maps and all(isinstance(m, dict) and len(m) == 1
                                     for m in maps)):
                        key0 = next(iter(maps[0]))
                        if all(next(iter(m)) == key0 for m in maps):
                            # Prefer ORIGINAL parent values for type fidelity.
                            raw_vals = []
                            for (a, o), m in zip(ex, maps):
                                if isinstance(o, dict) and key0 in o:
                                    raw_vals.append(o[key0])
                                else:
                                    raw_vals.append(m[key0])
                            raw_vals = tuple(raw_vals)

                            def _val_canon(v: Any) -> Any:
                                if _is_num_scalar(v):
                                    return _canon(v)
                                if _is_dict_value(v):
                                    return _canon_dict(v)
                                if _is_list_value(v):
                                    return _canon_list(v)
                                return v

                            vals = tuple(_val_canon(v) for v in raw_vals)
                            matched_col = None
                            # Match any parent input column (not only numeric).
                            for col in in_names:
                                try:
                                    colv = tuple(_val_canon(a[col]) for a, _ in ex)
                                except Exception:
                                    continue
                                if colv == vals:
                                    matched_col = col
                                    break
                            matched_prim = None
                            matched_pcol = None
                            if matched_col is None:
                                for pname in sorted(self.reg.names()):
                                    try:
                                        prim = self.reg.get(pname)
                                    except Exception:
                                        continue
                                    if getattr(prim, "family", "") not in (
                                            "acquired", "promoted"):
                                        continue
                                    if Effect.PURE not in tuple(
                                            getattr(prim, "effects", ()) or ()):
                                        continue
                                    try:
                                        pins = [k for k, v in prim.inputs.items()
                                                if not v.optional]
                                    except Exception:
                                        continue
                                    if len(pins) != 1:
                                        continue
                                    for col in in_names:
                                        try:
                                            got = tuple(
                                                _val_canon(
                                                    prim.fn(**{pins[0]: a[col]}))
                                                for a, _ in ex)
                                        except Exception:
                                            continue
                                        if got == vals:
                                            matched_prim = pname
                                            matched_pcol = col
                                            break
                                    if matched_prim:
                                        break
                            if matched_col is not None:
                                src = "behavioral_kv"
                                reuse_prim = matched_col
                                reuse_bind = {
                                    "__kv_key__": key0,
                                    "__kv_mode__": "param",
                                }
                            elif matched_prim is not None:
                                src = "behavioral_kv"
                                reuse_prim = matched_prim
                                reuse_bind = {
                                    "__kv_key__": key0,
                                    "__kv_mode__": "reuse",
                                    "__kv_col__": matched_pcol,
                                }
                            elif (child_sub is None
                                  and _recursion_depth < MAX_RECURSION_DEPTH
                                  and any(_is_dict_value(v)
                                          or _is_list_value(v)
                                          or isinstance(v, tuple)
                                          for v in raw_vals)):
                                # v43: singleton dict fold child whose value is
                                # structured. key-isolation may place the
                                # vector in Tier-A (skipping out-of-pool
                                # enrichment) while GENERATE still cannot
                                # emit nested structure. Recurse on the
                                # original singleton projection.
                                singleton_proj = []
                                for a, o in ex:
                                    if isinstance(o, dict) and key0 in o:
                                        singleton_proj.append(
                                            (dict(a), {key0: o[key0]}))
                                    else:
                                        singleton_proj = []
                                        break
                                if len(singleton_proj) == n:
                                    try:
                                        sub = self._decompose_inner(
                                            f"{goal} [dict component {key0!r}]",
                                            singleton_proj, t0,
                                            _recursion_depth=_recursion_depth + 1)
                                    except _Deadline:
                                        sub = None
                                    except Exception:
                                        sub = None
                                    if sub is not None:
                                        src = "behavioral_compound_residual"
                                        child_sub = self._rebind_contract(
                                            sub, cname,
                                            f"{goal} [dict component {key0!r}]")
                                        hierarchy_depth = max(
                                            hierarchy_depth,
                                            1 + int(child_sub.provenance.get(
                                                "hierarchy_depth", 0) or 0))
                def _project_out(v: Any) -> Any:
                    # List algebra stores hashable tuples; acquisition examples
                    # and downstream validation expect real lists. Tuple domain
                    # keeps tuples. Dict algebra stores sorted-pair canons;
                    # project back to plain dicts with nested lists restored.
                    if self._algebra.domain == "list":
                        return _list_from_canon(v)
                    if self._algebra.domain == "tuple":
                        lst = _list_from_canon(v)
                        def _to_tuple(x: Any) -> Any:
                            if isinstance(x, list):
                                return tuple(_to_tuple(y) for y in x)
                            if isinstance(x, dict):
                                return {k: _to_tuple(val) for k, val in x.items()}
                            return x
                        return _to_tuple(lst)
                    if self._algebra.domain == "dict":
                        if isinstance(v, tuple):
                            return _dict_from_canon(v)
                        if isinstance(v, dict):
                            return dict(v)
                        return v
                    return v
                # Prefer peeling component keys from ORIGINAL parent outputs so
                # nested list/dict/tuple types survive (canon round-trip can
                # leave list values as tuples). Falls back to _project_out.
                if self._algebra.domain == "dict" and vec:
                    try:
                        part0 = _dict_from_canon(vec[0]) if isinstance(vec[0], tuple) else dict(vec[0])
                        keys = list(part0.keys())
                        proj = []
                        for (a, o), v in zip(ex, vec):
                            if isinstance(o, dict) and all(k in o for k in keys):
                                proj.append((dict(a), {k: o[k] for k in keys}))
                            else:
                                proj.append((dict(a), _project_out(v)))
                    except Exception:
                        proj = [(dict(a), _project_out(v)) for (a, _), v in zip(ex, vec)]
                else:
                    proj = [(dict(a), _project_out(v)) for (a, _), v in zip(ex, vec)]
                children.append(DecomposedChild(
                    name=cname,
                    index=i,
                    description=f"{goal} [behavioral component {i + 1} of {k}]",
                    projected_examples=proj,
                    value_vector=[o for _, o in proj],
                    source=src,
                    sub_contract=child_sub,
                    reuse_primitive=reuse_prim,
                    reuse_arg_bind=reuse_bind,
                ))
            reconstructor = Reconstructor(family=family, op=op,
                                          child_refs=child_refs)
            # Confidence (design 2.5): all terms from recorded integers, no
            # learned weights. Scores the hypothesis, not the fit (the fit is
            # exact by the I3 gate above).
            evidence = (n - k) / n
            search = 1.0 / (1.0 + math.log10(1.0 + counter[0]))
            ambiguity = 1.0 / (1.0 + n_alt)
            confidence = round(evidence * search * ambiguity, 4)
            provenance = {
                "builder": (
                    "behavioral_decomposer.v3_reuse" if winner_n_reuse > 0
                    else ("behavioral_decomposer.v2_recursive"
                          if is_hierarchical
                          else "behavioral_decomposer.v1")),
                "search_policy_version": SEARCH_POLICY_VERSION,
                "method": (
                    "reuse_augmented_residual" if winner_n_reuse > 0
                    else ("compound_residual_hierarchy" if is_hierarchical
                          else "residual_value_closure")),
                "pool_tier_a": len(tier_a),
                "pool_all": len(pool_all),
                "pool_size": len(pool_all),
                "candidates_tested": counter[0],
                "n_alternatives": n_alt,
                "alternatives_capped": n_alt >= MAX_ALTERNATIVES_COUNTED,
                "alternatives": alternatives,
                "alternatives_recorded": len(alternatives),
                "selection_rule": (
                    ("fewest_components,shallowest_depth,"
                     "most_reuse,lowest_total_pool_rank;"
                     "hierarchy_when_flat_k>"
                     + str(HIERARCHY_FLAT_TRIGGER))
                    if (is_hierarchical or winner_n_reuse > 0) else
                    ("fewest_components,shallowest_depth,"
                     "lowest_total_pool_rank")),
                "winner_total_pool_rank": best_rank,
                "winner_n_reuse": winner_n_reuse,
                "reuse_pool_size": len(reuse_map),
                "evidence_rule": "n_examples >= n_components + 2 (P8-AdjC)",
                "wall_clock_s": self.wall_clock_s,
                "peak_rss_mb": None,
                "n_components": k,
                "nest_depth": depth if not is_hierarchical else 0,
                "hierarchy_depth": hierarchy_depth,
                "hierarchical": is_hierarchical,
                "outer_ops_observed": list(ops),
                "fail_fast_atomic": False,
                "determinism_salt": DETERMINISM_SALT,
                "policy_version": SEARCH_POLICY_VERSION,
                "examples_sha256": _sha256_examples(ex),
                "recursion_depth": _recursion_depth,
                "confidence_breakdown": {
                    "evidence": round(evidence, 4),
                    "search": round(search, 4),
                    "ambiguity": round(ambiguity, 4),
                },
            }
            return DecompositionContract(
                parent_goal=goal,
                reconstructor=reconstructor,
                children=children,
                n_examples=n,
                input_names=in_names,
                confidence=confidence,
                provenance=provenance,
                policy_version=SEARCH_POLICY_VERSION,
                determinism_salt=DETERMINISM_SALT,
            )
        finally:
            self._algebra = _saved_algebra
