"""Gap genesis: from a failed NL dispatch to a composition gap with
verifiable examples (V10-GAP-GENESIS).

The boundary this crosses: the composition inlet (V10-COMPOSE-INLET)
correctly refuses example-less gaps, but nothing produced examples from
a bare failed dispatch. This module is the producer.

Honesty architecture (hard, not aspirational):
  * The examples are VERIFIED, not asserted. Every (input, output) pair
    is computed by executing the engine's own trusted primitives on
    small inputs derived from the failed request. The mechanism never
    writes down an expected output it did not compute.
  * The decomposition uses a minimal mathematical vocabulary
    (square=x^2, cube=x^3, ...). This is DOMAIN KNOWLEDGE
    (mathematics), not the capability being acquired -- analogous to the
    NLU substrate knowing English words. The CAPABILITY (the general
    composition of primitives) is still discovered by the composer's
    real search, verified by Q8, and admitted through the governed path.
    The vocabulary does not give away the composition: knowing that
    "square" means "x^2" does not tell you to sum(map(square)).
  * The transformation is ALWAYS executed via the real primitive's fn
    (looked up from the registry), never hardcoded arithmetic.
  * If no decomposition is found, or no examples can be verified, the
    mechanism reports honestly and creates nothing. An honestly-open
    gap beats a staged-closed one.

Strategy order:
  (a) decomposition (this module): break the request into subproblems
      solvable by existing primitives; derive verified pairs.
  (b) experience mining: not implemented -- no analogous-case index
      exists in the delta store with the shape this needs. Reported
      honestly, not faked.
  (c) elicitation: the mechanism records exactly what it needs
      (needs_example). The gap stays open; a future turn can ask.
"""

from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional, Tuple

REGISTERED_BY = "v10-gap-genesis"


# ---------------------------------------------------------------------------
# Mathematical definitional vocabulary.
#
# DOMAIN KNOWLEDGE, not the acquired capability. Each entry maps a noun to
# a function that executes the transformation via a REAL primitive from
# the registry. The primitive is looked up by name; if it is absent, the
# entry is unusable (fail closed, never hardcoded).
# ---------------------------------------------------------------------------

def _via_power(reg: Any, x: Any, exp: int) -> Any:
    prim = reg.get("power")
    if prim is None:
        raise KeyError("power primitive absent")
    return prim.fn(x, exp)


def _via_multiply(reg: Any, x: Any, factor: int) -> Any:
    prim = reg.get("multiply")
    if prim is None:
        raise KeyError("multiply primitive absent")
    return prim.fn(x, factor)


def _via_divide(reg: Any, x: Any, divisor: int) -> Any:
    prim = reg.get("divide")
    if prim is None:
        raise KeyError("divide primitive absent")
    return prim.fn(x, divisor)


def _via_negate(reg: Any, x: Any) -> Any:
    prim = reg.get("negate")
    if prim is None:
        raise KeyError("negate primitive absent")
    return prim.fn(x)


_MATH_TRANSFORMS: Dict[str, Tuple[str, Callable[[Any, Any], Any]]] = {
    # noun -> (description, transform(reg, x))
    "square":  ("x raised to 2", lambda reg, x: _via_power(reg, x, 2)),
    "squares": ("x raised to 2", lambda reg, x: _via_power(reg, x, 2)),
    "squared": ("x raised to 2", lambda reg, x: _via_power(reg, x, 2)),
    "cube":    ("x raised to 3", lambda reg, x: _via_power(reg, x, 3)),
    "cubes":   ("x raised to 3", lambda reg, x: _via_power(reg, x, 3)),
    "cubed":   ("x raised to 3", lambda reg, x: _via_power(reg, x, 3)),
    "double":  ("x times 2", lambda reg, x: _via_multiply(reg, x, 2)),
    "doubled": ("x times 2", lambda reg, x: _via_multiply(reg, x, 2)),
    "triple":  ("x times 3", lambda reg, x: _via_multiply(reg, x, 3)),
    "tripled": ("x times 3", lambda reg, x: _via_multiply(reg, x, 3)),
    "half":    ("x divided by 2", lambda reg, x: _via_divide(reg, x, 2)),
    "halved":  ("x divided by 2", lambda reg, x: _via_divide(reg, x, 2)),
    "negated": ("negation of x", _via_negate),
    "negative": ("negation of x", _via_negate),
}


# ---------------------------------------------------------------------------
# Decomposition
# ---------------------------------------------------------------------------

class Decomposition:
    """A verified decomposition of a failed request."""
    def __init__(self, outer_name: str, transform_name: str,
                 transform_desc: str, input_key: str):
        self.outer_name = outer_name          # primitive name, e.g. 'sum'
        self.transform_name = transform_name  # vocab noun, e.g. 'squares'
        self.transform_desc = transform_desc  # e.g. 'x raised to 2'
        self.input_key = input_key            # args key holding the list

    @property
    def provenance(self) -> str:
        return (
            f"decomposition: outer={self.outer_name} over "
            f"{self.transform_name} ({self.transform_desc}) of "
            f"args[{self.input_key!r}]; outputs computed by real "
            f"primitive execution, not asserted")


def _decompose_request(engine: Any, request_text: str,
                       args: Optional[Dict[str, Any]]) -> Optional[Decomposition]:
    """Break the failed request into (outer primitive, inner transform).

    Returns None when no honest decomposition is found.
    """
    from swarm_engine.synthesis.planner import parse_goal
    try:
        goal = parse_goal(request_text)
    except Exception:
        return None
    verbs = [v.lower() for v in (getattr(goal, "verbs", None) or [])]
    nouns = [n.lower() for n in (getattr(goal, "nouns", None) or [])]

    reg = engine.composer.reg
    # Outer: a verb that names a real primitive.
    outer_name = None
    for verb in verbs:
        if reg.get(verb) is not None:
            outer_name = verb
            break
    if outer_name is None:
        return None
    # Inner: a noun in the mathematical vocabulary.
    transform_name = None
    transform_desc = ""
    for noun in nouns:
        if noun in _MATH_TRANSFORMS:
            transform_name = noun
            transform_desc = _MATH_TRANSFORMS[noun][0]
            break
    if transform_name is None:
        return None
    # Input: a list-valued arg to decompose over.
    input_key = None
    for key, val in (args or {}).items():
        if isinstance(val, list) and len(val) > 0:
            input_key = key
            break
    if input_key is None:
        return None
    return Decomposition(outer_name, transform_name, transform_desc,
                         input_key)


def _small_inputs(values: List[Any]) -> List[List[Any]]:
    """Systematic small variants of the input list, derived mechanically.

    Prefixes, a suffix, and singletons -- small enough for the slow
    (primitive-execution) path, diverse enough to train a general plan.
    """
    out: List[List[Any]] = []
    for n in (1, 2, 3):
        if len(values) >= n:
            out.append(list(values[:n]))
    if len(values) >= 2:
        out.append(list(values[-2:]))
    if values:
        out.append([values[0]])
        if len(values) > 1:
            out.append([values[-1]])
    seen = set()
    uniq: List[List[Any]] = []
    for v in out:
        t = tuple(v)
        if t not in seen:
            seen.add(t)
            uniq.append(v)
    return uniq


def _synthesize_verified_examples(
        engine: Any, decomposition: Decomposition,
        args: Dict[str, Any]
) -> Tuple[List[Tuple[Dict[str, Any], Any]],
           List[Tuple[Dict[str, Any], Any]], List[str]]:
    """Compute verified (input, output) pairs via real primitive execution.

    For each small input: apply the transform to each element (via the
    real primitive), then apply the outer primitive to the transformed
    list. Every output is COMPUTED, never asserted. Returns
    (train, held_out, log).
    """
    reg = engine.composer.reg
    outer = reg.get(decomposition.outer_name)
    transform = _MATH_TRANSFORMS[decomposition.transform_name][1]
    log: List[str] = []
    if outer is None:
        return [], [], ["outer primitive vanished from registry"]
    values = args[decomposition.input_key]
    small = _small_inputs(list(values))
    log.append(f"small inputs derived: {small}")

    verified: List[Tuple[Dict[str, Any], Any]] = []
    for inp in small:
        try:
            transformed = [transform(reg, x) for x in inp]
            out = outer.fn(transformed)
        except Exception as exc:  # noqa: BLE001 -- primitive exec is untrusted
            log.append(f"input {inp}: primitive execution failed "
                       f"({type(exc).__name__}); skipped")
            continue
        verified.append(({decomposition.input_key: inp}, out))
        log.append(f"verified: {inp} -> {transformed} -> {out}")
    if len(verified) < 2:
        return [], [], log + ["fewer than 2 verifiable examples; refusing"]
    # Train on the small cases; hold out the last small case AND the full
    # original input (the actual request) to test generalization.
    train = verified[:-1]
    held_out = [verified[-1]]
    try:
        full_transformed = [transform(reg, x) for x in values]
        full_out = outer.fn(full_transformed)
        held_out.append(({decomposition.input_key: list(values)}, full_out))
        log.append(f"held-out includes the actual request input: "
                   f"{list(values)} -> {full_out}")
    except Exception as exc:  # noqa: BLE001
        log.append(f"full-input verification failed ({exc}); "
                   f"held-out is small-case only")
    return train, held_out, log


# ---------------------------------------------------------------------------
# Genesis entry point
# ---------------------------------------------------------------------------

def maybe_genesis_composition_gap(engine: Any, request_text: str,
                                  args: Optional[Dict[str, Any]],
                                  result: Any) -> Dict[str, Any]:
    """Attempt gap genesis for a failed NL dispatch.

    Only engages when the dispatch failed at ROUTING (unknown_intent or
    equivalent: no capability was matched). Creates a GapRecord carrying
    the failed request and VERIFIED composition examples (via
    decomposition), registers it in the unified registry, and dispatches
    it through the existing gap route dispatcher.

    Returns an outcome dict; never raises. The dispatch result is
    unaffected -- genesis is observational wiring, like dispatch_gaps.
    """
    try:
        return _genesis(engine, request_text, args, result)
    except Exception as exc:  # noqa: BLE001 -- genesis never breaks dispatch
        return {"genesis": False,
                "reason": "genesis error: %r" % (exc,)}


def _genesis(engine: Any, request_text: str,
             args: Optional[Dict[str, Any]], result: Any) -> Dict[str, Any]:
    from swarm_engine.acquisition.gaps import GapRegistry, GapRecord

    refusal = getattr(result, "refusal", "") or ""
    # Only routing failures: a matched capability's failure is handled by
    # the existing dispatch_gaps wiring (limitation / missing dependency).
    if getattr(result, "ok", False):
        return {"genesis": False, "reason": "dispatch ok: nothing to do"}
    if getattr(result, "capability_id", None):
        return {"genesis": False,
                "reason": "a capability was matched; existing wiring owns it"}
    if "unknown_intent" not in refusal and "underspecified" not in refusal:
        return {"genesis": False,
                "reason": f"refusal {refusal!r} is not a routing failure"}

    registry = GapRegistry(engine)
    # Idempotency: an open gap for this request text already covers it.
    try:
        for gap in registry.list_gaps(status="open") or []:
            for entry in gap.evidence or []:
                if (isinstance(entry, dict)
                        and entry.get("observed") == "failed_request"
                        and isinstance(entry.get("detail"), dict)
                        and entry["detail"].get("request_text") == request_text):
                    return {"genesis": False, "duplicate_of_open": True,
                            "gap_id": gap.gap_id,
                            "reason": "an open gap already covers this request"}
    except Exception:
        pass

    # Strategy (a): decomposition.
    decomposition = _decompose_request(engine, request_text, args or {})
    if decomposition is None:
        return {"genesis": False,
                "reason": "no honest decomposition: verbs/nouns do not map "
                          "to (primitive, mathematical transform); "
                          "needs_example (elicitation) or experience mining "
                          "-- not faked"}
    train, held_out, log = _synthesize_verified_examples(
        engine, decomposition, args or {})
    if not train:
        return {"genesis": False,
                "reason": "decomposition found but yielded no verifiable "
                          "examples: " + "; ".join(log[-2:])}

    record = GapRecord(
        registered_by=REGISTERED_BY,
        summary=f"dispatch failed with {refusal} for: {request_text}",
        evidence=[
            {"kind": "observation", "observed": "failed_request",
             "detail": {"request_text": request_text,
                        "refusal": refusal,
                        "reasons": list(getattr(result, "reasons", None) or []),
                        "args": dict(args or {})}},
            {"kind": "observation", "observed": "composition_examples",
             "detail": {"examples": [[a, e] for a, e in train],
                        "held_out": [[a, e] for a, e in held_out],
                        "provenance": decomposition.provenance,
                        "synthesis_log": log}},
        ],
    )
    record = registry.register(record)
    # Genesis CREATES the gap; it does not dispatch it. The composition
    # (8k+ evaluations) is expensive and belongs to the gap processing
    # path (registry.dispatch, Run Controller), not the user-facing
    # dispatch call. The caller (or proof) dispatches explicitly.
    return {"genesis": True,
            "gap_id": record.gap_id,
            "outcome": "registered (not dispatched)",
            "detail": f"gap {record.gap_id} registered with "
                      f"{len(train)} train / {len(held_out)} held-out "
                      f"verified examples; dispatch via registry.dispatch",
            "examples": len(train),
            "held_out": len(held_out)}
