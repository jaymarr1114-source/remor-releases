"""
swarm_engine/cognition/constants.py

Evidence-derived constant discovery for the synthesizer's literal pool.

The synthesizer seeds a small fixed set of literal leaves (0, 1, 2, -1)
plus caller-supplied extra_literals. That leaves a real, generic gap:
an arbitrary constant implied by the behavioral evidence -- a scaling
factor of 100, an additive offset of 25, a threshold of 50, a modulus of
9 -- has no path into the search as a leaf unless something derives it.
Without derivation the search must either construct the constant from
the fixed leaves (often structurally impossible within the size budget)
or fail.

This module closes that gap WITHOUT merely appending observed outputs
to the literal pool (which would flood the search with uninformative
leaves). Every constant here is derived from a RELATIONSHIP present in
the evidence:

- inputs / outputs themselves (bounded; equality-predicate vocabulary)
- per-example differences (additive offsets) and ratios (scaling factors)
- cross-example CONSISTENT differences / ratios (a ratio that is the
  same on every example is strong evidence for a multiplicative
  constant, whatever its value)
- linear fits verified across all examples (output = k*in + c yields
  both k and c)
- quadratic-consistent ratios (output / in^2, for in^2-scaling)
- gcd of input-output differences (modulus discovery for integer
  wrap-around behavior: if output = in mod m, then m divides every
  (in - output), so m divides their gcd)
- threshold midpoints between consecutive sorted values of a parameter
  or of the outputs (predicate-boundary vocabulary)
- small discrete output sets (branch-constant candidates: if the
  outputs take only 2-3 distinct values across many examples, those
  values are candidate branch constants)
- consecutive-output offsets

Nothing here encodes any domain, field name, operator, or expected
value. Every rule is a generic numeric relationship; the same code
mines a threshold of 50 from signal data and a modulus of 9 from
counter data.

Each mined constant carries provenance (what relationship produced it)
and a tier (cross-example-consistent relationships outrank
per-example ones, which outrank raw values), so callers can bound the
pool by evidence strength instead of by arbitrary truncation. Output
is deterministic: ties break by value, never by dict order or example
order accidents.
"""
from __future__ import annotations

from dataclasses import dataclass
from math import gcd
from typing import Any, Dict, List, Sequence, Tuple


@dataclass(frozen=True)
class MinedConstant:
    value: float
    provenance: str
    tier: int        # 0 = cross-example consistent, 1 = structural, 2 = per-example, 3 = raw
    support: int     # how many examples back this derivation


def _is_num(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _close(a: float, b: float, tol: float = 1e-9) -> bool:
    return abs(a - b) <= tol * max(1.0, abs(a), abs(b))


def _solve3(M: List[List[float]], y: Sequence[float]
            ) -> Tuple[float, float, float] | None:
    """Solve a 3x3 linear system by Gaussian elimination with partial
    pivoting. Pure-python (no numpy dependency); returns None on
    singular/degenerate systems."""
    n = 3
    A = [list(map(float, row)) + [float(y[i])] for i, row in enumerate(M)]
    for col in range(n):
        piv = max(range(col, n), key=lambda r: abs(A[r][col]))
        if abs(A[piv][col]) < 1e-12:
            return None
        A[col], A[piv] = A[piv], A[col]
        for r in range(col + 1, n):
            f = A[r][col] / A[col][col]
            for k in range(col, n + 1):
                A[r][k] -= f * A[col][k]
    x = [0.0] * n
    for i in range(n - 1, -1, -1):
        s = A[i][n] - sum(A[i][j] * x[j] for j in range(i + 1, n))
        if abs(A[i][i]) < 1e-12:
            return None
        x[i] = s / A[i][i]
    return (x[0], x[1], x[2])


def quadratic_fit(in_vals: Sequence[float], out_vals: Sequence[float]
                  ) -> Tuple[float, float, float] | None:
    """Fit output = a*in^2 + b*in + c exactly through the first three
    examples; verify on ALL examples. Returns (a, b, c) or None.
    Generic numeric relationship (the degree-2 analogue of the linear
    fit in mine_constants): no domain, field, or expected value is
    encoded. Requires >= 4 examples so verification is non-vacuous
    (three points always determine a quadratic)."""
    if len(in_vals) < 4 or len(out_vals) < 4:
        return None
    xs = [float(v) for v in in_vals]
    ys = [float(v) for v in out_vals]
    M = [[x * x, x, 1.0] for x in xs[:3]]
    abc = _solve3(M, ys[:3])
    if abc is None:
        return None
    a, b, c = abc
    if not all(_close(a * x * x + b * x + c, y)
               for x, y in zip(xs, ys)):
        return None
    return (a, b, c)


def mine_constants(examples: Sequence[Tuple[Dict[str, Any], Any]],
                   param_names: Sequence[str],
                   cap: int = 24) -> List[MinedConstant]:
    """Derive candidate literal constants from the evidence's own numeric
    relationships. Returns at most `cap` constants, ranked by tier then
    support then value (deterministic)."""
    if not examples:
        return []
    n = len(examples)
    args_list = [a for a, _ in examples]
    outputs = [o for _, o in examples]
    out: List[MinedConstant] = []
    seen: set = set()

    def add(value: float, provenance: str, tier: int, support: int) -> None:
        if not _is_num(value):
            return
        f = float(value)
        if abs(f) > 1e6:
            return
        key = round(f, 6)
        if key in seen:
            return
        seen.add(key)
        out.append(MinedConstant(round(f, 6), provenance, tier, support))

    num_params = [p for p in param_names
                  if all(_is_num(a.get(p)) for a in args_list)]
    num_outputs = [_is_num(o) for o in outputs]
    all_num_out = all(num_outputs)

    for p in num_params:
        in_vals = [float(a[p]) for a in args_list]

        # ---- tier 0: cross-example consistent relationships ----
        if all_num_out:
            o_vals = [float(o) for o in outputs]
            # Consistent ratio output/in -> multiplicative constant.
            ratios = [o / v for o, v in zip(o_vals, in_vals) if v != 0]
            if len(ratios) == n and all(_close(r, ratios[0]) for r in ratios):
                add(ratios[0],
                    f"output/{p} ratio consistent across {n} examples",
                    0, n)
            # Consistent difference output-in -> additive constant.
            diffs = [o - v for o, v in zip(o_vals, in_vals)]
            if all(_close(d, diffs[0]) for d in diffs):
                add(diffs[0],
                    f"output-{p} difference consistent across {n} examples",
                    0, n)
            # Consistent quadratic ratio output/in^2.
            sq = [o / (v * v) for o, v in zip(o_vals, in_vals) if v != 0]
            if len(sq) == n and all(_close(s, sq[0]) for s in sq):
                add(sq[0],
                    f"output/{p}^2 ratio consistent across {n} examples",
                    0, n)
            # Linear fit output = k*in + c verified on ALL examples.
            if n >= 2:
                (x0, y0), (x1, y1) = (in_vals[0], o_vals[0]), (in_vals[1], o_vals[1])
                if x1 != x0:
                    k = (y1 - y0) / (x1 - x0)
                    c = y0 - k * x0
                    if all(_close(k * x + c, y) for x, y in zip(in_vals, o_vals)):
                        add(k, f"linear fit output=k*{p}+c verified on {n} examples",
                            0, n)
                        add(c, f"linear fit output=k*{p}+c verified on {n} examples",
                            0, n)
            # Quadratic fit output = a*in^2 + b*in + c verified on ALL
            # examples (degree-2 analogue of the linear fit above; the
            # generic numeric relationship, not a target pattern). Mines
            # the polynomial coefficients as construction vocabulary.
            if n >= 4:
                _abc = quadratic_fit(in_vals, o_vals)
                if _abc is not None:
                    for _coef, _nm in zip(_abc, ("a", "b", "c")):
                        add(_coef, f"quadratic fit output=a*{p}^2+b*{p}+c "
                                   f"verified on {n} examples", 0, n)
            # Modulus via gcd of input-output differences (integer wrap).
            if all(float(v).is_integer() and float(o).is_integer()
                   for v, o in zip(in_vals, outputs)):
                diffs_int = [abs(int(v) - int(o)) for v, o in zip(in_vals, outputs)]
                g = 0
                for d in diffs_int:
                    g = gcd(g, d)
                if g > 1:
                    add(float(g),
                        f"gcd of |{p}-output| differences over {n} examples",
                        0, n)

        # ---- tier 1: structural thresholds ----
        uniq = sorted(set(in_vals))
        for x, y in zip(uniq, uniq[1:]):
            if y > x:
                add((x + y) / 2.0,
                    f"midpoint of {p} values {x:g},{y:g}", 1, 2)

        # ---- tier 2: per-example relationships ----
        if all_num_out:
            for v, o in zip(in_vals, outputs):
                if v != 0:
                    add(o / v, f"output/{p} ratio on one example", 2, 1)
                add(o - v, f"output-{p} difference on one example", 2, 1)

        # ---- tier 3: raw input values (equality-predicate vocabulary) ----
        for v in uniq:
            add(v, f"observed {p} value", 3, 1)

    # ---- output-geometry derivations (param-independent) ----
    if all_num_out:
        o_vals = [float(o) for o in outputs]
        # Tier 1: output thresholds.
        uniq_o = sorted(set(o_vals))
        for x, y in zip(uniq_o, uniq_o[1:]):
            if y > x:
                add((x + y) / 2.0, f"midpoint of output values {x:g},{y:g}", 1, 2)
        # Tier 1: small discrete output set -> branch-constant candidates.
        # NOT "append all outputs": only when the outputs collapse to a
        # few distinct values across strictly more examples, which is
        # positive evidence of branch constants rather than noise.
        if len(uniq_o) <= 4 < n:
            for v in uniq_o:
                add(v, f"distinct output value over {n} examples "
                       f"(branch-constant candidate)", 1, n)
        # Tier 2: consecutive-output offsets.
        for a, b in zip(o_vals, o_vals[1:]):
            add(b - a, "consecutive output offset", 2, 1)

    # Rank: tier, then support (descending), then value (deterministic).
    out.sort(key=lambda m: (m.tier, -m.support, m.value))
    return out[:cap]
