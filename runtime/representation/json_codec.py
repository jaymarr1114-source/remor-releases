"""Canonical JSON encoding for REMOR values that stdlib json rejects.

General adapters — not goal-specific. Tagged forms are deterministic and
distinguish types that would otherwise collide if naively stringified:

  set/frozenset → {"$set": [sorted elems...]} / {"$frozenset": [...]}
  bytes/bytearray → {"$bytes": "<hex>"} / {"$bytearray": "<hex>"}

Tuples are encoded as {"$tuple": [...]} so they do not collapse into lists.
Other objects fall back to repr under {"$repr": "..."} only as last resort
for hashing (not a round-trip claim).
"""
from __future__ import annotations

import json
from typing import Any


def json_default(obj: Any) -> Any:
    if isinstance(obj, set):
        return {"$set": [_normalize(x) for x in _sorted_elems(obj)]}
    if isinstance(obj, frozenset):
        return {"$frozenset": [_normalize(x) for x in _sorted_elems(obj)]}
    if isinstance(obj, bytes):
        return {"$bytes": obj.hex()}
    if isinstance(obj, bytearray):
        return {"$bytearray": bytes(obj).hex()}
    if isinstance(obj, tuple):
        return {"$tuple": [_normalize(x) for x in obj]}
    if isinstance(obj, complex):
        return {"$complex": [obj.real, obj.imag]}
    return {"$repr": repr(obj)}


def _sorted_elems(xs):
    try:
        return sorted(xs)
    except TypeError:
        return sorted(xs, key=lambda x: (type(x).__name__, repr(x)))


def _normalize(x: Any) -> Any:
    """Recursively replace non-JSON values so dumps stays total."""
    if isinstance(x, (str, int, float, bool)) or x is None:
        return x
    if isinstance(x, (set, frozenset, bytes, bytearray, tuple, complex)):
        return json_default(x)
    if isinstance(x, list):
        return [_normalize(v) for v in x]
    if isinstance(x, dict):
        # keep string keys; tag non-str keys
        out = {}
        for k, v in x.items():
            if isinstance(k, str):
                out[k] = _normalize(v)
            else:
                out[f"$key:{type(k).__name__}:{repr(k)}"] = _normalize(v)
        return out
    return json_default(x)


def canonical_json_dumps(obj: Any) -> str:
    return json.dumps(_normalize(obj), sort_keys=True, separators=(",", ":"))


def _decode_key(k: Any) -> Any:
    """Inverse of the $key:{type}:{repr} tagging in _normalize."""
    if isinstance(k, str) and k.startswith("$key:"):
        rest = k[len("$key:"):]
        typename, _, rep = rest.partition(":")
        if typename in ("int", "float", "bool", "NoneType", "tuple",
                        "complex"):
            # Note: frozenset-typed keys are intentionally not inverted
            # (their repr is a call, not a literal); best-effort only.
            try:
                import ast as _ast
                v = _ast.literal_eval(rep)
            except Exception:
                return k
            if v is None and typename == "NoneType":
                return None
            if type(v).__name__ == typename:
                return v
    return k


def from_tagged(obj: Any) -> Any:
    """Best-effort inverse of tagged encoding (for tests/adapters)."""
    if isinstance(obj, list):
        return [from_tagged(v) for v in obj]
    if isinstance(obj, dict):
        if set(obj.keys()) == {"$set"}:
            return set(from_tagged(v) for v in obj["$set"])
        if set(obj.keys()) == {"$frozenset"}:
            return frozenset(from_tagged(v) for v in obj["$frozenset"])
        if set(obj.keys()) == {"$bytes"}:
            return bytes.fromhex(obj["$bytes"])
        if set(obj.keys()) == {"$bytearray"}:
            return bytearray.fromhex(obj["$bytearray"])
        if set(obj.keys()) == {"$tuple"}:
            return tuple(from_tagged(v) for v in obj["$tuple"])
        if set(obj.keys()) == {"$complex"}:
            r, i = obj["$complex"]
            return complex(r, i)
        return {_decode_key(k): from_tagged(v) for k, v in obj.items()}
    return obj
