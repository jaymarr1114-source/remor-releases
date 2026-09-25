"""
swarm_engine/primitives/families_pure.py

The effect-free half of the vocabulary. Everything here is deterministic given
its arguments (except `random`/`time`, which declare CLOCK/RANDOM effects), so
the composer can reorder, cache, parallelise and constant-fold these freely.

Families in this module:
    computation   data      logic       text
    time          typeops   probability abstraction
"""
from __future__ import annotations

import base64
import hashlib
import json
import math
import random as _random
import re
import statistics
import time as _time
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional

from swarm_engine.primitives.core import (
    ANY, BOOL, BYTES, CALLABLE, DICT, FLOAT, INT, LIST, NONE, NUM, OPT, STR,
    TUPLE, UNION, Effect, Primitive, PrimitiveRegistry, TypeSpec, coerce, infer,
    unify,
)
from swarm_engine.pow_safety import _ensure_pow_safe


def _p(reg: PrimitiveRegistry, name, family, fn, inputs, output,
       effects=(Effect.PURE,), variadic=False, needs_ctx=False, doc=""):
    reg.register(Primitive(name=name, family=family, fn=fn, inputs=inputs,
                           output=output, effects=effects, variadic=variadic,
                           needs_ctx=needs_ctx, doc=doc), overwrite=True)


# ===========================================================================
# 1. COMPUTATION
# ===========================================================================

def register_computation(reg: PrimitiveRegistry) -> None:
    F = "computation"
    N2 = {"a": NUM, "b": NUM}
    N1 = {"x": NUM}

    def _require_num(x, name="value"):
        # Align runtime evaluation with declared NUM types so GeneralSynthesizer
        # cannot accept host-language quirks (e.g. str+str) that Composer rejects.
        if isinstance(x, bool) or not isinstance(x, (int, float)):
            raise TypeError(f"{name} expects num, got {type(x).__name__}")
        return x

    def _div(a, b):
        a, b = _require_num(a, "a"), _require_num(b, "b")
        if b == 0:
            raise ZeroDivisionError("division by zero")
        return a / b

    def _mod(a, b):
        a, b = _require_num(a, "a"), _require_num(b, "b")
        if b == 0:
            raise ZeroDivisionError("modulo by zero")
        return a % b

    def _power(base, exponent):
        # R16 authoritative enforcement: the magnitude check lives here, in
        # the primitive itself, so EVERY execution path (fast interpreter,
        # composer, scalar fallbacks, probes, blind enumeration, production
        # evaluation) is covered by the same guard before `**` can
        # materialize a pathological value.
        base, exponent = _require_num(base, "base"), _require_num(exponent, "exponent")
        _ensure_pow_safe(base, exponent)
        return base ** exponent

    def _sqrt(x):
        if x < 0:
            raise ValueError("sqrt of negative number")
        return math.sqrt(x)

    def _log(x, base=math.e):
        if x <= 0:
            raise ValueError("log of non-positive number")
        return math.log(x, base)

    def _nonempty(vs):
        if not vs:
            raise ValueError("empty sequence")
        return vs

    defs = [
        ("add", lambda a, b: _require_num(a,"a") + _require_num(b,"b"), N2, NUM, "Sum of two numbers."),
        ("subtract", lambda a, b: _require_num(a,"a") - _require_num(b,"b"), N2, NUM, "Difference a - b."),
        ("multiply", lambda a, b: _require_num(a,"a") * _require_num(b,"b"), N2, NUM, "Product a * b."),
        ("divide", _div, N2, FLOAT, "Quotient a / b; raises on zero."),
        ("modulo", _mod, N2, NUM, "Remainder of a / b."),
        ("power", _power, {"base": NUM, "exponent": NUM}, NUM, "base raised to exponent."),
        ("negate", lambda x: -_require_num(x,"x"), N1, NUM, "Unary minus."),
        ("abs", lambda x: abs(x), N1, NUM, "Absolute value."),
        ("sqrt", _sqrt, N1, FLOAT, "Square root."),
        ("log", _log, {"x": NUM, "base": OPT(NUM)}, FLOAT, "Logarithm of x in given base (default e)."),
        ("exp", lambda x: math.exp(x), N1, FLOAT, "e raised to x."),
        ("sin", lambda x: math.sin(x), N1, FLOAT, "Sine (radians)."),
        ("cos", lambda x: math.cos(x), N1, FLOAT, "Cosine (radians)."),
        ("tan", lambda x: math.tan(x), N1, FLOAT, "Tangent (radians)."),
        ("atan2", lambda y, x: math.atan2(y, x), {"y": NUM, "x": NUM}, FLOAT, "Two-argument arctangent."),
        ("hypot", lambda a, b: math.hypot(_require_num(a,"a"), _require_num(b,"b")), N2, FLOAT, "Euclidean norm."),
        ("floor", lambda x: math.floor(x), N1, INT, "Round toward -inf."),
        ("ceil", lambda x: math.ceil(x), N1, INT, "Round toward +inf."),
        ("round", lambda x, decimals=0: round(x, int(decimals)), {"x": NUM, "decimals": OPT(INT)}, NUM, "Round to N decimals."),
        ("sign", lambda x: (x > 0) - (x < 0), N1, INT, "-1, 0 or 1."),
        ("clamp", lambda x, low, high: max(low, min(x, high)), {"x": NUM, "low": NUM, "high": NUM}, NUM, "Constrain x to [low, high]."),
        ("lerp", lambda a, b, t: (_require_num(a,"a") + (_require_num(b,"b") - _require_num(a,"a")) * _require_num(t,"t")), {"a": NUM, "b": NUM, "t": NUM}, FLOAT, "Linear interpolation."),
        ("normalize_range", lambda x, low, high: 0.0 if high == low else (x - low) / (high - low),
         {"x": NUM, "low": NUM, "high": NUM}, FLOAT, "Map [low,high] onto [0,1]."),
        ("min", lambda values: min(_nonempty(values)), {"values": LIST(NUM)}, NUM, "Smallest element."),
        ("max", lambda values: max(_nonempty(values)), {"values": LIST(NUM)}, NUM, "Largest element."),
        ("sum", lambda values: sum(values), {"values": LIST(NUM)}, NUM, "Sum of a list."),
        ("product", lambda values: math.prod(values) if values else 1, {"values": LIST(NUM)}, NUM, "Product of a list."),
        ("mean", lambda values: statistics.fmean(_nonempty(values)), {"values": LIST(NUM)}, FLOAT, "Arithmetic mean."),
        ("median", lambda values: statistics.median(_nonempty(values)), {"values": LIST(NUM)}, NUM, "Median value."),
        ("mode", lambda values: statistics.mode(_nonempty(values)), {"values": LIST(NUM)}, NUM, "Most common value."),
        ("stdev", lambda values: statistics.pstdev(values) if len(values) > 1 else 0.0, {"values": LIST(NUM)}, FLOAT, "Population standard deviation."),
        ("variance", lambda values: statistics.pvariance(values) if len(values) > 1 else 0.0, {"values": LIST(NUM)}, FLOAT, "Population variance."),
        ("cumsum", lambda values: [sum(values[: i + 1]) for i in range(len(values))], {"values": LIST(NUM)}, LIST(NUM), "Running total."),
        ("range_of", lambda values: max(_nonempty(values)) - min(values), {"values": LIST(NUM)}, NUM, "max - min."),
        ("gcd", lambda a, b: math.gcd(int(a), int(b)), {"a": INT, "b": INT}, INT, "Greatest common divisor."),
        ("lcm", lambda a, b: abs(int(a) * int(b)) // math.gcd(int(a), int(b)) if a and b else 0, {"a": INT, "b": INT}, INT, "Least common multiple."),
    ]
    for name, fn, inp, out, doc in defs:
        _p(reg, name, F, fn, inp, out, doc=doc)

    _p(reg, "random_float", F, lambda: _random.random(), {}, FLOAT,
       effects=(Effect.RANDOM,), doc="Uniform random in [0,1).")
    _p(reg, "random_int", F, lambda low, high: _random.randint(int(low), int(high)),
       {"low": INT, "high": INT}, INT, effects=(Effect.RANDOM,), doc="Uniform random integer, inclusive.")
    def _seeded_random(seed, n):
        # One generator, drawn n times: constructing a Random per element
        # re-seeds per draw (O(n) seeding) and dominated search time once
        # deep searches evaluated this primitive heavily (found directly:
        # 10.7M Random constructions / 115s in a single stress search).
        # NOTE: this corrects the original semantics. The old code,
        # [_random.Random(seed).random() for _ in range(int(n))],
        # re-seeded per element, so every element was the SAME first draw
        # repeated n times -- not a sequence. The documented intent
        # ("Deterministic random sequence from a seed") is a proper
        # sequence of successive draws, which is what this implements.
        rng = _random.Random(int(seed))
        count = int(n)
        return [rng.random() for _ in range(count)] if count > 0 else []

    _p(reg, "seeded_random", F, _seeded_random,
       {"seed": INT, "n": INT}, LIST(FLOAT), doc="Deterministic random sequence from a seed.")


# ===========================================================================
# 2. DATA
# ===========================================================================

def register_data(reg: PrimitiveRegistry) -> None:
    F = "data"

    def get_path(obj, path, default=None):
        cur = obj
        for part in str(path).split("."):
            if isinstance(cur, dict):
                if part not in cur:
                    return default
                cur = cur[part]
            elif isinstance(cur, (list, tuple)):
                try:
                    cur = cur[int(part)]
                except (ValueError, IndexError):
                    return default
            else:
                return default
        return cur

    def set_path(obj, path, value):
        import copy
        out = copy.deepcopy(obj)
        parts = str(path).split(".")
        cur = out
        for part in parts[:-1]:
            if not isinstance(cur.get(part), dict):
                cur[part] = {}
            cur = cur[part]
        cur[parts[-1]] = value
        return out

    def del_path(obj, path):
        import copy
        out = copy.deepcopy(obj)
        parts = str(path).split(".")
        cur = out
        for part in parts[:-1]:
            if not isinstance(cur, dict) or part not in cur:
                return out
            cur = cur[part]
        if isinstance(cur, dict):
            cur.pop(parts[-1], None)
        return out

    def group_by(items, key):
        out: Dict[Any, List[Any]] = {}
        for it in items:
            k = key(it) if callable(key) else get_path(it, key)
            out.setdefault(k, []).append(it)
        return out

    def index_by(items, key):
        return {(key(it) if callable(key) else get_path(it, key)): it for it in items}

    def unique(items, key=None):
        seen, out = set(), []
        for it in items:
            k = key(it) if callable(key) else it
            try:
                hashable = hash(k)
            except TypeError:
                hashable = json.dumps(k, sort_keys=True, default=str)
            if hashable not in seen:
                seen.add(hashable)
                out.append(it)
        return out

    def join_on(left, right, left_key, right_key=None):
        rk = right_key or left_key
        idx: Dict[Any, List[dict]] = {}
        for r in right:
            idx.setdefault(get_path(r, rk), []).append(r)
        out = []
        for l in left:
            for r in idx.get(get_path(l, left_key), []):
                merged = dict(l)
                merged.update(r)
                out.append(merged)
        return out

    def pivot(items, row_key, col_key, value_key):
        table: Dict[Any, Dict[Any, Any]] = {}
        for it in items:
            table.setdefault(get_path(it, row_key), {})[get_path(it, col_key)] = get_path(it, value_key)
        return table

    def validate(obj, schema):
        errs = []
        for k, kind in schema.items():
            if k not in obj:
                errs.append(f"missing field {k!r}")
                continue
            want = {"int": int, "float": (int, float), "str": str,
                    "bool": bool, "list": list, "dict": dict}.get(kind)
            if want and not isinstance(obj[k], want):
                errs.append(f"field {k!r} expected {kind}, got {type(obj[k]).__name__}")
        return {"valid": not errs, "errors": errs}

    defs = [
        ("get", get_path, {"obj": ANY, "path": STR, "default": OPT(ANY)}, ANY, "Read a dotted path out of a nested structure."),
        ("set", set_path, {"obj": DICT(), "path": STR, "value": ANY}, DICT(), "Return a copy with a dotted path set."),
        ("delete", del_path, {"obj": DICT(), "path": STR}, DICT(), "Return a copy with a dotted path removed."),
        ("keys", lambda obj: list(obj.keys()), {"obj": DICT()}, LIST(), "Keys of a mapping."),
        ("values", lambda obj: list(obj.values()), {"obj": DICT()}, LIST(), "Values of a mapping."),
        ("items", lambda obj: [list(kv) for kv in obj.items()], {"obj": DICT()}, LIST(), "Key/value pairs."),
        ("has_key", lambda obj, key: key in obj, {"obj": DICT(), "key": ANY}, BOOL, "Membership test."),
        ("pick", lambda obj, fields: {k: obj[k] for k in fields if k in obj}, {"obj": DICT(), "fields": LIST(STR)}, DICT(), "Subset of fields."),
        ("omit", lambda obj, fields: {k: v for k, v in obj.items() if k not in fields}, {"obj": DICT(), "fields": LIST(STR)}, DICT(), "All fields except these."),
        ("rename", lambda obj, mapping: {mapping.get(k, k): v for k, v in obj.items()}, {"obj": DICT(), "mapping": DICT()}, DICT(ANY, ANY), "Rename keys."),
        ("merge", lambda a, b: {**a, **b}, {"a": DICT(), "b": DICT()}, DICT(), "Shallow merge, b wins."),
        ("deep_merge", None, {"a": DICT(), "b": DICT()}, DICT(), "Recursive merge, b wins on leaves."),
        ("invert", lambda obj: {v: k for k, v in obj.items()}, {"obj": DICT()}, DICT(ANY, ANY), "Swap keys and values."),
        ("length", lambda collection: len(collection), {"collection": ANY}, INT, "Element count."),
        ("first", lambda items, default=None: items[0] if items else default, {"items": LIST(), "default": OPT(ANY)}, ANY, "First element."),
        ("last", lambda items, default=None: items[-1] if items else default, {"items": LIST(), "default": OPT(ANY)}, ANY, "Last element."),
        ("nth", lambda items, index, default=None: items[int(index)] if -len(items) <= index < len(items) else default,
         {"items": LIST(), "index": INT, "default": OPT(ANY)}, ANY, "Element at index."),
        ("slice", lambda items, start=0, stop=None: items[int(start): (None if stop is None else int(stop))],
         {"items": LIST(), "start": OPT(INT), "stop": OPT(INT)}, LIST(), "Sublist."),
        ("append", lambda items, value: list(items) + [value], {"items": LIST(), "value": ANY}, LIST(), "List with value appended."),
        ("prepend", lambda items, value: [value] + list(items), {"items": LIST(), "value": ANY}, LIST(), "List with value prepended."),
        ("list_concat", lambda a, b: list(a) + list(b), {"a": LIST(), "b": LIST()}, LIST(), "Concatenate two lists."),
        ("repeat", None, {"value": ANY, "count": INT}, LIST(),
         "Repeat value exactly count times (length-indexed list constructor)."),
        ("tuple_concat", lambda a, b: tuple(a) + tuple(b), {"a": ANY, "b": ANY}, ANY, "Concatenate two sequences as a tuple."),
        ("flatten", lambda items: [x for sub in items for x in (sub if isinstance(sub, list) else [sub])], {"items": LIST()}, LIST(), "One level of flattening."),
        ("chunk", lambda items, size: [items[i:i + int(size)] for i in range(0, len(items), int(size))], {"items": LIST(), "size": INT}, LIST(LIST()), "Split into fixed-size chunks."),
        ("zip", lambda a, b: [list(p) for p in zip(a, b)], {"a": LIST(), "b": LIST()}, LIST(LIST()), "Pairwise combine."),
        ("enumerate", lambda items: [[i, v] for i, v in enumerate(items)], {"items": LIST()}, LIST(LIST()), "Index/value pairs."),
        ("reverse", lambda items: list(reversed(items)), {"items": LIST()}, LIST(), "Reversed copy."),
        ("unique", unique, {"items": LIST(), "key": OPT(CALLABLE)}, LIST(), "Deduplicate preserving order."),
        ("group_by", group_by, {"items": LIST(), "key": UNION(STR, CALLABLE)}, DICT(ANY, LIST()), "Partition into buckets by key."),
        ("index_by", index_by, {"items": LIST(), "key": UNION(STR, CALLABLE)}, DICT(ANY, ANY), "Build a lookup table."),
        ("join_on", join_on, {"left": LIST(DICT()), "right": LIST(DICT()), "left_key": STR, "right_key": OPT(STR)}, LIST(DICT()), "Relational inner join."),
        ("pivot", pivot, {"items": LIST(DICT()), "row_key": STR, "col_key": STR, "value_key": STR}, DICT(ANY, ANY), "Cross-tabulate records."),
        ("count_by", lambda items, key: {k: len(v) for k, v in group_by(items, key).items()}, {"items": LIST(), "key": UNION(STR, CALLABLE)}, DICT(ANY, INT), "Frequency table."),
        ("serialize", lambda obj, indent=None: json.dumps(obj, default=str, indent=(int(indent) if indent else None), sort_keys=True),
         {"obj": ANY, "indent": OPT(INT)}, STR, "Object to JSON text."),
        ("deserialize", lambda text: json.loads(text), {"text": STR}, ANY, "JSON text to object."),
        ("encode_base64", lambda data: base64.b64encode(data if isinstance(data, bytes) else str(data).encode()).decode(),
         {"data": ANY}, STR, "Base64 encode."),
        ("decode_base64", lambda text: base64.b64decode(text).decode("utf-8", "replace"), {"text": STR}, STR, "Base64 decode."),
        ("hash", lambda obj, algorithm="sha256": hashlib.new(algorithm, json.dumps(obj, sort_keys=True, default=str).encode()).hexdigest(),
         {"obj": ANY, "algorithm": OPT(STR)}, STR, "Stable content hash of any structure."),
        ("validate", validate, {"obj": DICT(), "schema": DICT()}, DICT(), "Check an object against a {field: kind} schema."),
        ("diff", None, {"a": DICT(), "b": DICT()}, DICT(), "Field-level difference between two mappings."),
    ]

    _REPEAT_CAP = 10000

    def repeat(value, count):
        # bool is a subclass of int; refuse before int().
        if isinstance(count, bool) or not isinstance(count, (int, float)):
            raise TypeError("repeat count expects int")
        n = int(count)
        if n < 0:
            raise ValueError("repeat count must be >= 0")
        if n > _REPEAT_CAP:
            raise ValueError(f"repeat count {n} exceeds cap {_REPEAT_CAP}")
        return [value] * n

    def deep_merge(a, b):
        out = dict(a)
        for k, v in b.items():
            if isinstance(out.get(k), dict) and isinstance(v, dict):
                out[k] = deep_merge(out[k], v)
            else:
                out[k] = v
        return out

    def diff(a, b):
        added = {k: b[k] for k in b if k not in a}
        removed = {k: a[k] for k in a if k not in b}
        changed = {k: [a[k], b[k]] for k in a if k in b and a[k] != b[k]}
        return {"added": added, "removed": removed, "changed": changed}

    impls = {"deep_merge": deep_merge, "diff": diff, "repeat": repeat}
    for name, fn, inp, out, doc in defs:
        _p(reg, name, F, fn or impls[name], inp, out, doc=doc)

    # higher-order — take callables, so they stay pure w.r.t. the substrate
    _p(reg, "map", F, lambda items, fn: [fn(x) for x in items],
       {"items": LIST(), "fn": CALLABLE}, LIST(), doc="Apply fn to every element.")
    _p(reg, "filter", F, lambda items, predicate: [x for x in items if predicate(x)],
       {"items": LIST(), "predicate": CALLABLE}, LIST(), doc="Keep elements matching predicate.")
    _p(reg, "reduce", F, lambda items, fn, initial: __import__("functools").reduce(fn, items, initial),
       {"items": LIST(), "fn": CALLABLE, "initial": ANY}, ANY, doc="Fold a list into a single value.")
    _p(reg, "sort", F, lambda items, key=None, reverse=False: sorted(items, key=key, reverse=bool(reverse)),
       {"items": LIST(), "key": OPT(CALLABLE), "reverse": OPT(BOOL)}, LIST(), doc="Sorted copy.")
    _p(reg, "sort_by_field", F, lambda items, field, reverse=False: sorted(items, key=lambda d: get_path(d, field), reverse=bool(reverse)),
       {"items": LIST(DICT()), "field": STR, "reverse": OPT(BOOL)}, LIST(DICT()), doc="Sort records by a dotted field.")
    _p(reg, "partition", F, lambda items, predicate: [[x for x in items if predicate(x)], [x for x in items if not predicate(x)]],
       {"items": LIST(), "predicate": CALLABLE}, LIST(LIST()), doc="Split into [matching, non-matching].")


# ===========================================================================
# 3. LOGIC / CONTROL FLOW
# ===========================================================================

def register_logic(reg: PrimitiveRegistry) -> None:
    F = "logic"

    def _matches(value, pattern):
        return bool(re.search(pattern, str(value)))

    def _assert(condition, message="assertion failed"):
        if not condition:
            raise AssertionError(message)
        return True

    def _contains(container, item):
        try:
            return item in container
        except TypeError:
            return False

    defs = [
        ("if_else", lambda condition, then, otherwise: then if condition else otherwise,
         {"condition": BOOL, "then": ANY, "otherwise": ANY}, ANY, "Ternary selection."),
        ("switch", lambda value, cases, default=None: cases.get(value, default),
         {"value": ANY, "cases": DICT(), "default": OPT(ANY)}, ANY, "Table-driven dispatch."),
        ("coalesce", lambda values: next((v for v in values if v is not None), None),
         {"values": LIST()}, ANY, "First non-null value."),
        ("and", lambda a, b: bool(a) and bool(b), {"a": BOOL, "b": BOOL}, BOOL, "Logical conjunction."),
        ("or", lambda a, b: bool(a) or bool(b), {"a": BOOL, "b": BOOL}, BOOL, "Logical disjunction."),
        ("not", lambda x: not bool(x), {"x": BOOL}, BOOL, "Logical negation."),
        ("xor", lambda a, b: bool(a) != bool(b), {"a": BOOL, "b": BOOL}, BOOL, "Exclusive or."),
        ("all", lambda values: all(values), {"values": LIST()}, BOOL, "True if every element is truthy."),
        ("any", lambda values: any(values), {"values": LIST()}, BOOL, "True if some element is truthy."),
        ("equals", lambda a, b: a == b, {"a": ANY, "b": ANY}, BOOL, "Structural equality."),
        ("not_equals", lambda a, b: a != b, {"a": ANY, "b": ANY}, BOOL, "Structural inequality."),
        ("less_than", lambda a, b: a < b, {"a": ANY, "b": ANY}, BOOL, "a < b."),
        ("greater_than", lambda a, b: a > b, {"a": ANY, "b": ANY}, BOOL, "a > b."),
        ("less_or_equal", lambda a, b: a <= b, {"a": ANY, "b": ANY}, BOOL, "a <= b."),
        ("greater_or_equal", lambda a, b: a >= b, {"a": ANY, "b": ANY}, BOOL, "a >= b."),
        ("between", lambda x, low, high: low <= x <= high, {"x": NUM, "low": NUM, "high": NUM}, BOOL, "Inclusive range test."),
        ("is_none", lambda value: value is None, {"value": ANY}, BOOL, "Null check."),
        ("exists", lambda value: value is not None, {"value": ANY}, BOOL, "Non-null check."),
        ("is_empty", lambda value: value is None or (hasattr(value, "__len__") and len(value) == 0),
         {"value": ANY}, BOOL, "Empty/None check."),
        ("contains", _contains, {"container": ANY, "item": ANY}, BOOL, "Membership."),
        ("matches", _matches, {"value": ANY, "pattern": STR}, BOOL, "Regex search."),
        ("assert", _assert, {"condition": BOOL, "message": OPT(STR)}, BOOL, "Raise unless condition holds."),
        ("identity", lambda value: value, {"value": ANY}, ANY, "Return the input unchanged."),
        ("constant", lambda value: value, {"value": ANY}, ANY, "Produce a literal."),
    ]
    for name, fn, inp, out, doc in defs:
        _p(reg, name, F, fn, inp, out, doc=doc)


# ===========================================================================
# 4. TEXT
# ===========================================================================

def register_text(reg: PrimitiveRegistry) -> None:
    F = "text"

    def tokenize(text, pattern=r"\w+"):
        return re.findall(pattern, text)

    def template(template, values):
        out = template
        for k, v in values.items():
            out = out.replace("{" + str(k) + "}", str(v))
        return out

    def extract_numbers(text):
        return [float(m) for m in re.findall(r"-?\d+\.?\d*", text)]

    def word_stats(text):
        words = re.findall(r"\w+", text.lower())
        freq: Dict[str, int] = {}
        for w in words:
            freq[w] = freq.get(w, 0) + 1
        sentences = [s for s in re.split(r"[.!?]+", text) if s.strip()]
        return {
            "characters": len(text),
            "words": len(words),
            "unique_words": len(freq),
            "sentences": len(sentences),
            "avg_word_length": round(sum(len(w) for w in words) / len(words), 3) if words else 0.0,
            "top_words": sorted(freq.items(), key=lambda kv: -kv[1])[:10],
        }

    def similarity(a, b):
        """Jaccard similarity over word sets — cheap, local, no model needed."""
        sa, sb = set(re.findall(r"\w+", a.lower())), set(re.findall(r"\w+", b.lower()))
        if not sa and not sb:
            return 1.0
        return len(sa & sb) / len(sa | sb)

    def summarize_extractive(text, sentences=3):
        """Frequency-scored extractive summary. Purely local: no provider call."""
        sents = [s.strip() for s in re.split(r"(?<=[.!?])\s+", text) if s.strip()]
        if len(sents) <= sentences:
            return " ".join(sents)
        words = re.findall(r"\w+", text.lower())
        freq: Dict[str, int] = {}
        for w in words:
            freq[w] = freq.get(w, 0) + 1
        scored = [(sum(freq.get(w, 0) for w in re.findall(r"\w+", s.lower())) / (len(s.split()) or 1), i, s)
                  for i, s in enumerate(sents)]
        chosen = sorted(sorted(scored, reverse=True)[: int(sentences)], key=lambda t: t[1])
        return " ".join(s for _, _, s in chosen)

    defs = [
        ("concat", lambda a, b: str(a) + str(b), {"a": STR, "b": STR}, STR, "Join two strings."),
        ("join", lambda parts, separator="": str(separator).join(str(p) for p in parts),
         {"parts": LIST(), "separator": OPT(STR)}, STR, "Join a list into one string."),
        ("split", lambda text, separator=None, limit=-1: text.split(separator, int(limit)),
         {"text": STR, "separator": OPT(STR), "limit": OPT(INT)}, LIST(STR), "Split on a separator."),
        ("split_lines", lambda text: text.splitlines(), {"text": STR}, LIST(STR), "Split into lines."),
        ("tokenize", tokenize, {"text": STR, "pattern": OPT(STR)}, LIST(STR), "Regex tokenizer."),
        ("replace", lambda text, old, new, count=-1: text.replace(old, new, int(count)),
         {"text": STR, "old": STR, "new": STR, "count": OPT(INT)}, STR, "Literal substring replace."),
        ("regex_replace", lambda text, pattern, replacement: re.sub(pattern, replacement, text),
         {"text": STR, "pattern": STR, "replacement": STR}, STR, "Pattern-based replace."),
        ("regex_match", lambda text, pattern: bool(re.search(pattern, text)),
         {"text": STR, "pattern": STR}, BOOL, "Does the pattern occur?"),
        ("regex_extract", lambda text, pattern: re.findall(pattern, text),
         {"text": STR, "pattern": STR}, LIST(), "All pattern matches."),
        ("regex_groups", lambda text, pattern: list(re.search(pattern, text).groups()) if re.search(pattern, text) else [],
         {"text": STR, "pattern": STR}, LIST(), "Capture groups of the first match."),
        ("upper", lambda text: text.upper(), {"text": STR}, STR, "Uppercase."),
        ("lower", lambda text: text.lower(), {"text": STR}, STR, "Lowercase."),
        ("title", lambda text: text.title(), {"text": STR}, STR, "Title case."),
        ("trim", lambda text, chars=None: text.strip(chars), {"text": STR, "chars": OPT(STR)}, STR, "Strip whitespace/chars."),
        ("pad", lambda text, width, char=" ", side="left": text.rjust(int(width), char) if side == "left" else text.ljust(int(width), char),
         {"text": STR, "width": INT, "char": OPT(STR), "side": OPT(STR)}, STR, "Pad to a width."),
        ("truncate", lambda text, length, suffix="...": text if len(text) <= length else text[: max(0, int(length) - len(suffix))] + suffix,
         {"text": STR, "length": INT, "suffix": OPT(STR)}, STR, "Shorten with an ellipsis."),
        ("starts_with", lambda text, prefix: text.startswith(prefix), {"text": STR, "prefix": STR}, BOOL, "Prefix test."),
        ("ends_with", lambda text, suffix: text.endswith(suffix), {"text": STR, "suffix": STR}, BOOL, "Suffix test."),
        ("index_of", lambda text, sub: text.find(sub), {"text": STR, "sub": STR}, INT, "First index or -1."),
        ("slugify", lambda text: re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-"), {"text": STR}, STR, "URL-safe slug."),
        ("normalize_whitespace", lambda text: re.sub(r"\s+", " ", text).strip(), {"text": STR}, STR, "Collapse whitespace."),
        ("template", template, {"template": STR, "values": DICT()}, STR, "Fill {placeholders} from a mapping."),
        ("format_number", lambda value, decimals=2: f"{float(value):,.{int(decimals)}f}", {"value": NUM, "decimals": OPT(INT)}, STR, "Human-readable number."),
        ("extract_numbers", extract_numbers, {"text": STR}, LIST(FLOAT), "Pull all numbers out of text."),
        ("word_stats", word_stats, {"text": STR}, DICT(), "Counts, averages and top words."),
        ("similarity", similarity, {"a": STR, "b": STR}, FLOAT, "Jaccard word-set similarity in [0,1]."),
        ("summarize", summarize_extractive, {"text": STR, "sentences": OPT(INT)}, STR, "Local extractive summary."),
        ("levenshtein", None, {"a": STR, "b": STR}, INT, "Edit distance."),
    ]

    def levenshtein(a, b):
        if len(a) < len(b):
            a, b = b, a
        prev = list(range(len(b) + 1))
        for i, ca in enumerate(a, 1):
            cur = [i]
            for j, cb in enumerate(b, 1):
                cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
            prev = cur
        return prev[-1]

    impls = {"levenshtein": levenshtein}
    for name, fn, inp, out, doc in defs:
        _p(reg, name, F, fn or impls[name], inp, out, doc=doc)


# ===========================================================================
# 5. TIME
# ===========================================================================

def register_time(reg: PrimitiveRegistry) -> None:
    F = "time"

    _p(reg, "now", F, lambda: _time.time(), {}, FLOAT, effects=(Effect.CLOCK,), doc="Unix timestamp.")
    _p(reg, "now_iso", F, lambda: datetime.now(timezone.utc).isoformat(), {}, STR,
       effects=(Effect.CLOCK,), doc="Current UTC time, ISO 8601.")
    _p(reg, "timestamp_to_iso", F, lambda timestamp: datetime.fromtimestamp(timestamp, timezone.utc).isoformat(),
       {"timestamp": NUM}, STR, doc="Epoch seconds to ISO 8601.")
    _p(reg, "parse_datetime", F, lambda text: datetime.fromisoformat(text).timestamp(),
       {"text": STR}, FLOAT, doc="ISO 8601 to epoch seconds.")
    _p(reg, "format_datetime", F, lambda timestamp, pattern="%Y-%m-%d %H:%M:%S": datetime.fromtimestamp(timestamp, timezone.utc).strftime(pattern),
       {"timestamp": NUM, "pattern": OPT(STR)}, STR, doc="strftime a timestamp.")
    def _add_duration(timestamp, seconds):
        if isinstance(timestamp, bool) or not isinstance(timestamp, (int, float)):
            raise TypeError("timestamp expects num")
        if isinstance(seconds, bool) or not isinstance(seconds, (int, float)):
            raise TypeError("seconds expects num")
        return timestamp + seconds
    _p(reg, "add_duration", F, _add_duration,
       {"timestamp": NUM, "seconds": NUM}, FLOAT, doc="Shift a timestamp.")
    _p(reg, "duration_between", F, lambda start, end: end - start,
       {"start": NUM, "end": NUM}, FLOAT, doc="Seconds between two timestamps.")
    _p(reg, "humanize_duration", F, lambda seconds: str(timedelta(seconds=int(seconds))),
       {"seconds": NUM}, STR, doc="Seconds as H:MM:SS.")
    _p(reg, "day_of_week", F, lambda timestamp: datetime.fromtimestamp(timestamp, timezone.utc).strftime("%A"),
       {"timestamp": NUM}, STR, doc="Weekday name.")
    _p(reg, "is_before", F, lambda a, b: a < b, {"a": NUM, "b": NUM}, BOOL, doc="Chronological comparison.")
    _p(reg, "elapsed_since", F, lambda timestamp: _time.time() - timestamp,
       {"timestamp": NUM}, FLOAT, effects=(Effect.CLOCK,), doc="Seconds since a timestamp.")

    async def _sleep(seconds):
        import asyncio as _a
        await _a.sleep(min(float(seconds), 30.0))
        return float(seconds)

    _p(reg, "sleep", F, _sleep, {"seconds": NUM}, FLOAT, effects=(Effect.CLOCK,),
       doc="Yield for N seconds (capped at 30).")


# ===========================================================================
# 6. TYPEOPS
# ===========================================================================

def register_typeops(reg: PrimitiveRegistry) -> None:
    F = "typeops"

    def type_of(value):
        return str(infer(value))

    def cast(value, kind):
        target = {"int": INT, "float": FLOAT, "str": STR, "bool": BOOL,
                  "bytes": BYTES, "list": LIST(), "dict": DICT()}.get(kind)
        if target is None:
            raise ValueError(f"unknown target kind {kind!r}")
        ok, out = coerce(value, target)
        if not ok:
            raise TypeError(f"cannot coerce {type(value).__name__} to {kind}")
        return out

    def can_cast(value, kind):
        try:
            cast(value, kind)
            return True
        except Exception:
            return False

    def unify_types(a, b):
        u = unify(infer(a), infer(b))
        return str(u) if u else "none"

    def check_schema(value, spec):
        """spec is a type string like 'list[int]' produced by type_of."""
        return str(infer(value)) == spec

    def pattern_match(value, patterns):
        """patterns: {type_string: result}. Falls back to '_'."""
        t = str(infer(value))
        if t in patterns:
            return patterns[t]
        for k, v in patterns.items():
            if k != "_" and k.split("[")[0] == t.split("[")[0]:
                return v
        return patterns.get("_")

    defs = [
        ("type_of", type_of, {"value": ANY}, STR, "Structural type of a value."),
        ("cast", cast, {"value": ANY, "kind": STR}, ANY, "Coerce to a named type."),
        ("can_cast", can_cast, {"value": ANY, "kind": STR}, BOOL, "Would coercion succeed?"),
        ("unify_types", unify_types, {"a": ANY, "b": ANY}, STR, "Common supertype of two values."),
        ("check_schema", check_schema, {"value": ANY, "spec": STR}, BOOL, "Exact structural type match."),
        ("pattern_match", pattern_match, {"value": ANY, "patterns": DICT()}, ANY, "Dispatch on structural type."),
        ("is_type", lambda value, kind: str(infer(value)).split("[")[0] == kind, {"value": ANY, "kind": STR}, BOOL, "Base-kind test."),
        ("default_for", lambda kind: {"int": 0, "float": 0.0, "str": "", "bool": False,
                                      "list": [], "dict": {}}.get(kind), {"kind": STR}, ANY, "Zero value of a type."),
    ]
    for name, fn, inp, out, doc in defs:
        _p(reg, name, F, fn, inp, out, doc=doc)


# ===========================================================================
# 7. PROBABILITY / STATISTICS
# ===========================================================================

def register_probability(reg: PrimitiveRegistry) -> None:
    F = "probability"

    def sample(values, n, seed=None):
        rng = _random.Random(seed)
        return [rng.choice(values) for _ in range(int(n))]

    def bootstrap(values, iterations=1000, seed=0):
        rng = _random.Random(seed)
        means = [statistics.fmean([rng.choice(values) for _ in values]) for _ in range(int(iterations))]
        means.sort()
        lo = means[int(0.025 * len(means))]
        hi = means[min(len(means) - 1, int(0.975 * len(means)))]
        return {"mean": statistics.fmean(values), "ci_low": lo, "ci_high": hi, "iterations": int(iterations)}

    def confidence_interval(values, confidence=0.95):
        n = len(values)
        if n < 2:
            return {"mean": values[0] if values else 0.0, "low": None, "high": None}
        m = statistics.fmean(values)
        se = statistics.stdev(values) / math.sqrt(n)
        z = {0.90: 1.645, 0.95: 1.96, 0.99: 2.576}.get(round(confidence, 2), 1.96)
        return {"mean": m, "low": m - z * se, "high": m + z * se, "confidence": confidence}

    def bayes_update(prior, likelihood, evidence_likelihood):
        if evidence_likelihood == 0:
            raise ValueError("evidence likelihood cannot be zero")
        return (prior * likelihood) / evidence_likelihood

    def normal_pdf(x, mu=0.0, sigma=1.0):
        return math.exp(-((x - mu) ** 2) / (2 * sigma ** 2)) / (sigma * math.sqrt(2 * math.pi))

    def entropy(probabilities):
        return -sum(p * math.log2(p) for p in probabilities if p > 0)

    def correlation(xs, ys):
        if len(xs) != len(ys) or len(xs) < 2:
            raise ValueError("need two equal-length series of length >= 2")
        return statistics.correlation(xs, ys)

    def linear_regression(xs, ys):
        n = len(xs)
        mx, my = statistics.fmean(xs), statistics.fmean(ys)
        denom = sum((x - mx) ** 2 for x in xs)
        slope = 0.0 if denom == 0 else sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / denom
        return {"slope": slope, "intercept": my - slope * mx}

    def monte_carlo(fn, iterations=1000, seed=0):
        rng = _random.Random(seed)
        samples = [fn(rng.random()) for _ in range(int(iterations))]
        return {"mean": statistics.fmean(samples), "stdev": statistics.pstdev(samples), "n": len(samples)}

    def percentile(values, p):
        # 2026-09-22 (R9): honor the declared LIST(NUM) -> NUM contract.
        # The old lambda applied sorted() blindly, so on nested input it
        # returned a LIST element while declaring NUM -- the symbolic
        # synthesizer then built behaviorally-working but type-invalid plans
        # (e.g. nth(percentile(m,0),0)) that admission correctly rejected,
        # silently killing the generate attempt. Fail closed instead.
        vals = list(values)
        if not vals or not all(
                isinstance(v, (int, float)) and not isinstance(v, bool)
                for v in vals):
            raise TypeError("percentile expects a non-empty list[num]")
        return sorted(vals)[min(len(vals) - 1,
                                int(len(vals) * float(p) / 100))]

    defs = [
        ("sample", sample, {"values": LIST(), "n": INT, "seed": OPT(INT)}, LIST(), "Sample with replacement."),
        ("bootstrap", bootstrap, {"values": LIST(NUM), "iterations": OPT(INT), "seed": OPT(INT)}, DICT(), "Bootstrap CI of the mean."),
        ("confidence_interval", confidence_interval, {"values": LIST(NUM), "confidence": OPT(NUM)}, DICT(), "Normal-approximation CI."),
        ("bayes_update", bayes_update, {"prior": NUM, "likelihood": NUM, "evidence_likelihood": NUM}, FLOAT, "Posterior from Bayes' rule."),
        ("normal_pdf", normal_pdf, {"x": NUM, "mu": OPT(NUM), "sigma": OPT(NUM)}, FLOAT, "Gaussian density."),
        ("entropy", entropy, {"probabilities": LIST(NUM)}, FLOAT, "Shannon entropy in bits."),
        ("correlation", correlation, {"xs": LIST(NUM), "ys": LIST(NUM)}, FLOAT, "Pearson correlation."),
        ("linear_regression", linear_regression, {"xs": LIST(NUM), "ys": LIST(NUM)}, DICT(), "Least-squares fit."),
        ("percentile", percentile,
         {"values": LIST(NUM), "p": NUM}, NUM, "Nth percentile."),
        ("zscore", lambda x, values: 0.0 if statistics.pstdev(values) == 0 else (x - statistics.fmean(values)) / statistics.pstdev(values),
         {"x": NUM, "values": LIST(NUM)}, FLOAT, "Standard score."),
        ("monte_carlo", monte_carlo, {"fn": CALLABLE, "iterations": OPT(INT), "seed": OPT(INT)}, DICT(), "Estimate a distribution by sampling."),
    ]
    for name, fn, inp, out, doc in defs:
        _p(reg, name, F, fn, inp, out, doc=doc)


# ===========================================================================
# 8. ABSTRACTION
# ===========================================================================

def register_abstraction(reg: PrimitiveRegistry) -> None:
    F = "abstraction"

    def parameterize(plan, holes):
        """Turn literal values in a plan into named parameters."""
        text = json.dumps(plan)
        for name, literal in holes.items():
            text = text.replace(json.dumps(literal), json.dumps("$" + name))
        return json.loads(text)

    def instantiate(template, bindings):
        text = json.dumps(template)
        for name, value in bindings.items():
            text = text.replace(json.dumps("$" + name), json.dumps(value))
        return json.loads(text)

    def common_shape(items):
        """Fields present in every record — the abstraction they share."""
        if not items:
            return []
        shared = set(items[0])
        for it in items[1:]:
            shared &= set(it)
        return sorted(shared)

    def generalize_values(items):
        """Widest type that covers every element."""
        if not items:
            return "any"
        t = infer(items[0])
        for it in items[1:]:
            t = unify(t, infer(it)) or ANY
        return str(t)

    def specialize(template, overrides):
        out = dict(template)
        out.update(overrides)
        return out


    _RANGE_LIST_CAP = 10000
    _BYTES_LIST_CAP = 10000

    def range_list(count):
        if isinstance(count, bool) or not isinstance(count, (int, float)):
            raise TypeError("range_list count expects int")
        n = int(count)
        if n < 0:
            raise ValueError("range_list count must be >= 0")
        if n > _RANGE_LIST_CAP:
            raise ValueError(f"range_list count {n} exceeds cap {_RANGE_LIST_CAP}")
        return list(range(n))

    def as_bytes(items):
        if not isinstance(items, (list, tuple)):
            raise TypeError("as_bytes expects a list")
        if len(items) > _BYTES_LIST_CAP:
            raise ValueError(f"as_bytes length {len(items)} exceeds cap")
        return bytes(int(x) & 0xFF for x in items)

    def as_bytearray(items):
        if not isinstance(items, (list, tuple)):
            raise TypeError("as_bytearray expects a list")
        if len(items) > _BYTES_LIST_CAP:
            raise ValueError(f"as_bytearray length {len(items)} exceeds cap")
        return bytearray(int(x) & 0xFF for x in items)


    def set_union(a, b):
        return set(a) | set(b)

    def set_intersect(a, b):
        return set(a) & set(b)

    def set_difference(a, b):
        return set(a) - set(b)

    def frozenset_union(a, b):
        return frozenset(a) | frozenset(b)


    defs = [
        ("parameterize", parameterize, {"plan": ANY, "holes": DICT()}, ANY, "Replace literals with $named parameters."),
        ("instantiate", instantiate, {"template": ANY, "bindings": DICT()}, ANY, "Fill $named parameters with values."),
        ("common_shape", common_shape, {"items": LIST(DICT())}, LIST(STR), "Fields shared by all records."),
        ("generalize_values", generalize_values, {"items": LIST()}, STR, "Widest type covering a collection."),
        ("specialize", specialize, {"template": DICT(), "overrides": DICT()}, DICT(), "Narrow a template with fixed values."),
        ("lift", lambda value: [value], {"value": ANY}, LIST(), "Wrap a scalar as a collection."),
        ("lift_tuple", lambda value: (value,), {"value": ANY}, TUPLE(), "Wrap a value as a singleton tuple."),
        ("lift_set", lambda value: {value}, {"value": ANY}, ANY, "Wrap a hashable value as a singleton set."),
        ("as_set", lambda items: set(items), {"items": LIST()}, ANY,
         "Build a set from a list of hashable elements (order ignored)."),
        ("as_frozenset", lambda items: frozenset(items), {"items": LIST()}, ANY,
         "Build a frozenset from a list of hashable elements."),
        ("to_bytes", lambda value: bytes([int(value) & 0xFF]), {"value": NUM}, BYTES,
         "Pack an integer into a length-1 bytes object."),
        ("range_list", range_list, {"count": INT}, LIST(INT),
         "List of ints [0, 1, ..., count-1] (resource-capped)."),
        ("as_bytes", as_bytes, {"items": LIST()}, BYTES,
         "Pack a list of ints into a bytes object (each & 0xFF)."),
        ("as_bytearray", as_bytearray, {"items": LIST()}, BYTES,
         "Pack a list of ints into a bytearray (each & 0xFF)."),
        ("set_union", set_union, {"a": ANY, "b": ANY}, ANY,
         "Set union of two set-like collections."),
        ("set_intersect", set_intersect, {"a": ANY, "b": ANY}, ANY,
         "Set intersection of two set-like collections."),
        ("set_difference", set_difference, {"a": ANY, "b": ANY}, ANY,
         "Set difference a\\b of two set-like collections."),
        ("frozenset_union", frozenset_union, {"a": ANY, "b": ANY}, ANY,
         "Frozenset union of two set-like collections."),
        ("kv", lambda key, value: {key: value}, {"key": ANY, "value": ANY}, DICT(), "Singleton mapping from key to value."),
        ("unwrap_singleton", lambda items, default=None: items[0] if len(items) == 1 else default,
         {"items": LIST(), "default": OPT(ANY)}, ANY, "Unwrap a singleton collection."),
    ]
    for name, fn, inp, out, doc in defs:
        _p(reg, name, F, fn, inp, out, doc=doc)


def register_all_pure(reg: PrimitiveRegistry) -> None:
    register_computation(reg)
    register_data(reg)
    register_logic(reg)
    register_text(reg)
    register_time(reg)
    register_typeops(reg)
    register_probability(reg)
    register_abstraction(reg)
