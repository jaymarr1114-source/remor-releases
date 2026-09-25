
"""
General structural CALLABLE candidate generation (M+13/M+17).

Produces candidates that claim Kind.CALLABLE by partial application of
registered pure primitives.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple


@dataclass
class StructuralCandidate:
    kind: str
    representation: str
    op: str
    bound: Dict[str, Any] = field(default_factory=dict)
    free: List[str] = field(default_factory=list)
    origin: str = "structural_partial_application"
    notes: str = ""

    def as_dict(self) -> Dict[str, Any]:
        return {
            "kind": self.kind,
            "representation": self.representation,
            "op": self.op,
            "bound": dict(self.bound),
            "free": list(self.free),
            "origin": self.origin,
            "notes": self.notes,
        }


def _required_inputs(prim) -> List[str]:
    return [k for k, v in prim.inputs.items() if not getattr(v, "optional", False)]


def _all_numeric_inputs(prim) -> bool:
    from swarm_engine.primitives.core import Kind
    for k in _required_inputs(prim):
        spec = prim.inputs[k]
        kind = getattr(spec, "kind", None)
        if kind not in (Kind.NUM, Kind.INT, Kind.FLOAT):
            return False
    return True


def _examples_look_numeric(examples) -> bool:
    for args, expected in examples or []:
        for v in list(args.values()) + [expected]:
            vals = v if isinstance(v, (list, tuple)) else [v]
            for x in vals:
                if isinstance(x, (int, float)) and not isinstance(x, bool):
                    return True
    return False


def _constants_from_examples(
        examples: Sequence[Tuple[Dict[str, Any], Any]],
) -> List[Any]:
    found: List[Any] = []
    seen = set()
    for args, expected in examples or []:
        for v in list(args.values()) + [expected]:
            vals = v if isinstance(v, (list, tuple)) else [v]
            for x in vals:
                if isinstance(x, bool):
                    continue
                if isinstance(x, (int, float)):
                    xi = int(x) if isinstance(x, float) and x == int(x) else x
                    if isinstance(xi, (int, float)) and abs(xi) <= 1000 and xi not in seen:
                        seen.add(xi)
                        found.append(xi)
    # Minimal neutral pool for generality when examples are sparse
    for n in (0, 1, 2, 3):
        if n not in seen:
            found.append(n)
            seen.add(n)
    return found[:16]


class StructuralCallableCandidateGenerator:
    def __init__(self, registry, max_candidates: int = 300):
        self.reg = registry
        self.max_candidates = max_candidates

    def generate(
            self,
            required_kind: str = "CALLABLE",
            examples: Optional[Sequence[Tuple[Dict[str, Any], Any]]] = None,
    ) -> List[StructuralCandidate]:
        if str(required_kind).upper() != "CALLABLE":
            return []
        constants = _constants_from_examples(examples or [])
        binary: List[StructuralCandidate] = []
        unary: List[StructuralCandidate] = []

        for name in sorted(self.reg.names()):
            prim = self.reg.get(name)
            if prim is None or not getattr(prim, "pure", False):
                continue
            if getattr(prim, "family", "") in ("acquired",):
                continue
            req = _required_inputs(prim)
            if not req:
                continue
            if len(req) == 1:
                unary.append(StructuralCandidate(
                    kind="CALLABLE", representation="partial_primitive",
                    op=name, bound={}, free=[req[0]],
                    notes="unary pure primitive as callable",
                ))
            elif len(req) == 2:
                if _examples_look_numeric(examples or []) and not _all_numeric_inputs(prim):
                    continue
                a, b = req[0], req[1]
                for c in constants:
                    binary.append(StructuralCandidate(
                        kind="CALLABLE", representation="partial_primitive",
                        op=name, bound={b: c}, free=[a],
                        notes=f"partial bind {b}->{c}",
                    ))
                    binary.append(StructuralCandidate(
                        kind="CALLABLE", representation="partial_primitive",
                        op=name, bound={a: c}, free=[b],
                        notes=f"partial bind {a}->{c}",
                    ))
            # arity >2 skipped for now (combinatorial)

        # Prefer binary partials for element-wise CALLABLE use
        out = binary + unary
        return out[: self.max_candidates]


def to_partial_form(candidate: StructuralCandidate) -> Dict[str, Any]:
    if candidate.kind != "CALLABLE" or candidate.representation != "partial_primitive":
        raise ValueError("candidate is not a CALLABLE partial_primitive")
    return {
        "$partial": {
            "op": candidate.op,
            "bound": dict(candidate.bound),
            "free": list(candidate.free),
        }
    }


def map_plan_with_partial(candidate: StructuralCandidate, items_param: str = "values") -> Dict[str, Any]:
    return {
        "steps": [
            {
                "id": "m0",
                "op": "map",
                "args": {
                    "items": {"$param": items_param},
                    "fn": to_partial_form(candidate),
                },
            }
        ],
        "output": {"$step": "m0"},
        "params": {items_param: "list"},
    }
