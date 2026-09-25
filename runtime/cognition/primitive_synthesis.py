"""
swarm_engine/cognition/primitive_synthesis.py

PrimitiveConstructor: the mechanism for crossing the boundary
GeneralSynthesizer cannot cross on its own — genuine, unbounded iteration.

GeneralSynthesizer builds TREES of primitive applications, bounded by
max_size. No finite tree can express "repeat until a condition holds,
where the number of repetitions depends on the input" — counting how many
times a value can be halved before reaching zero needs a real loop whose
trip count varies per input, not a composition of fixed depth.

Honest scope, stated once: this is NOT general recursive program synthesis
(an open research problem). It is ONE established, principled schema —
iterate a discovered same-type step function from the input, counting
applications, until a discovered stop value is reached — bounded by a hard
safety cap on iteration count. The schema (iterate-and-count) is fixed and
authored; what is genuinely DISCOVERED, not templated, is:
  - the step function (via the same GeneralSynthesizer machinery used
    everywhere else, reused rather than duplicated)
  - the stop value (drawn from the same literal-constant pool and observed
    input/output values GeneralSynthesizer already uses)

This is deliberately the smallest version of "genuine iteration" that is
still honestly general — not "a halving-counter synthesizer," but what it
happens to find when given examples shaped that way.
"""
from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

from swarm_engine.cognition.representations import Expr, SearchBias
from swarm_engine.cognition.synthesis import GeneralSynthesizer
from swarm_engine.services.run_control import checkpoint
from swarm_engine.primitives.core import Effect, Primitive


@dataclass
class IterationHypothesis:
    step_expr: Expr
    stop_value: Any
    param_name: str
    max_iterations: int
    candidates_tried: int = 0

    def ops_used(self) -> Tuple[str, ...]:
        return self.step_expr.ops_used()

    def as_dict(self) -> Dict[str, Any]:
        return {"step_expr": self.step_expr.as_dict(), "stop_value": self.stop_value,
                "param_name": self.param_name, "max_iterations": self.max_iterations,
                "ops_used": list(self.ops_used())}


class PrimitiveConstructor:
    """Searches for a (step function, stop value) pair such that iterating
    the step function from each example's input, counting applications
    until the stop value is reached, matches every example's output count.

    Reuses GeneralSynthesizer's own bank-building for step-function
    candidates rather than a separate enumeration mechanism.
    """

    def __init__(self, registry, bias: SearchBias, step_search_max_size: int = 2,
                max_iterations: int = 1000, max_step_candidates: int = 20000,
                enumeration_wall_clock_s: float = 10.0):
        self.reg = registry
        self.bias = bias
        self.step_search_max_size = step_search_max_size
        self.max_iterations = max_iterations
        self.enumeration_wall_clock_s = enumeration_wall_clock_s
        # A smaller pool than GeneralSynthesizer's own default: with a
        # single sample value (not a full example set), the type-compatible
        # candidate space at each level is far smaller, and step functions
        # in this schema are expected to be shallow. Found necessary
        # directly: reusing GeneralSynthesizer's full default pool_per_type
        # (150) / max_arg_combinations (3000) with NO resource bound at all
        # in the enumeration loop (unlike search(), which checks
        # max_candidates and wall-clock every iteration) hung indefinitely.
        #
        # A FIXED 30 later proved too small once literal inference was
        # added (see _infer_literals): with many examples, inference can
        # propose dozens of candidate literals, and a fixed pool of 30
        # silently truncated a needed one (literal 7) out of the size-0
        # leaf pool before "subtract(n, 7)" was ever tried — found
        # directly, the same class of bug as the pool-truncation issue
        # fixed in GeneralSynthesizer earlier. Scaled with the number of
        # examples (the actual driver of how many literals get proposed)
        # rather than left fixed.
        self.pool_per_type_base = 30
        self._synth = GeneralSynthesizer(registry, bias, max_size=step_search_max_size,
                                         max_candidates=max_step_candidates,
                                         pool_per_type=30, max_arg_combinations=400)

    def synthesize(self, examples: Sequence[Tuple[Dict[str, Any], Any]],
                   param_name: str, max_matching_attempts: int = 60000,
                   matching_wall_clock_s: float = 30.0) -> Optional[IterationHypothesis]:
        if not examples:
            return None
        # Multiple distinct sample points, not one. Found necessary
        # directly: with a single sample value, two DIFFERENT
        # parameter-dependent expressions can coincidentally produce the
        # same output at that one point (e.g. two unrelated formulas both
        # happening to equal 4.0 when n=8), and whichever was tried first
        # would permanently claim the dedup slot, making the other
        # structurally unreachable regardless of budget. This is a strictly
        # stronger equivalence check — two candidates now have to agree at
        # every sample point to be treated as redundant — the same
        # principle GeneralSynthesizer's own dedup already uses across a
        # full example set, applied here across a few representative points
        # instead of just one.
        sample_values = []
        for args, _ in examples:
            v = args[param_name]
            if v not in sample_values:
                sample_values.append(v)
            if len(sample_values) >= 3:
                break

        inferred_literals = self._infer_literals(examples, param_name)
        # Widen the pool only as much as the actual literal load requires
        # — enough headroom for every leaf (parameter + fixed constants +
        # inferred literals) to survive size-0 truncation, capped so a
        # pathological number of examples can't blow the budget open
        # unboundedly.
        needed_pool = len(GeneralSynthesizer.LITERAL_CONSTANTS) + len(inferred_literals) + 1
        self._synth.pool_per_type = max(self.pool_per_type_base, min(needed_pool, 200))

        candidates = self._enumerate_same_type_step_candidates(
            param_name, sample_values, extra_literals=inferred_literals)
        stop_candidates = self._stop_value_candidates(examples)

        # The enumeration phase above has its own wall-clock/candidate
        # bound; this matching phase previously had none of its own. Found
        # necessary directly: fixing enumeration's dedup bug legitimately
        # increased the number of surviving candidates, and testing each
        # against every example for up to max_iterations steps with no
        # bound here hung just as the earlier, already-fixed hangs did — a
        # genuine consequence of a correct fix elsewhere, not a new bug in
        # this loop's logic.
        started = time.time()
        tried = 0
        for step_expr in candidates:
            for stop_value in stop_candidates:
                tried += 1
                if tried >= max_matching_attempts:
                    return None
                # Cooperation point (throttled): iteration-hypothesis
                # matching is a genuinely long CPU loop. No-op when no
                # control is installed.
                if tried % 200 == 0:
                    checkpoint("iterate:match")
                if tried % 200 == 0 and time.time() - started > matching_wall_clock_s:
                    return None
                if self._matches_all(step_expr, stop_value, examples, param_name):
                    return IterationHypothesis(
                        step_expr=step_expr, stop_value=stop_value,
                        param_name=param_name, max_iterations=self.max_iterations,
                        candidates_tried=tried)
        return None

    def _infer_literals(self, examples: Sequence[Tuple[Dict[str, Any], Any]],
                        param_name: str, max_literals: int = 12) -> List[Any]:
        """Candidate literals derived from the examples themselves, not
        invented. For a step-counting relationship, the natural step size
        is often recoverable directly: between two examples where the
        input changed by delta_in and the count changed by delta_out, a
        step of (delta_in / delta_out) is exactly what a constant-step
        iteration would need. Also includes each example's own raw values.

        Capped at max_literals — found directly, uncapped inference (up to
        ~36 pairs for 9 examples, each contributing several candidate
        values) turned a previously-fast case (halving, ~3.7s) into a
        31.5s one, by expanding the size-0 pool and the matching search
        space even for cases that never needed the extra literals at all.
        Consecutive-example deltas are prioritized (index i, i+1) over
        all-pairs, since a genuine constant step size shows up in EVERY
        consecutive pair, not just distant ones — this keeps the cap from
        discarding the most likely genuine candidates first."""
        numeric = [(args[param_name], out) for args, out in examples
                  if isinstance(args.get(param_name), (int, float)) and
                  isinstance(out, (int, float))]
        inferred: List[Any] = []

        def add(value):
            if value not in inferred and len(inferred) < max_literals:
                inferred.append(value)

        inferred_steps: Dict[Any, int] = {}
        for i in range(len(numeric) - 1):
            in_i, out_i = numeric[i]
            in_j, out_j = numeric[i + 1]
            delta_in, delta_out = in_j - in_i, out_j - out_i
            if delta_out != 0:
                step = delta_in / delta_out
                step = int(step) if step == int(step) else step
                inferred_steps[step] = inferred_steps.get(step, 0) + 1

        # Ranked by how many consecutive pairs agree on this step, not
        # discovery order. Found directly: for a genuinely linear
        # relationship (constant-step subtraction), every consecutive pair
        # agrees on the same step, so it dominates the ranking
        # immediately. For a non-linear one (halving is multiplicative,
        # not additive), the "step" computed this way varies pair to pair
        # and never repeats — ranking by frequency means that noise stays
        # at the bottom and gets excluded by the cap instead of crowding
        # out a genuine, consistently-recurring signal.
        ranked_steps = sorted(inferred_steps, key=lambda s: -inferred_steps[s])
        for step in ranked_steps:
            add(step)
            if len(inferred) >= max_literals:
                return inferred
        for in_i, out_i in numeric:
            add(in_i)
            add(out_i)
            if len(inferred) >= max_literals:
                break
        return inferred

    def _enumerate_same_type_step_candidates(self, param_name: str,
                                              sample_values: Any,
                                              extra_literals: Sequence[Any] = ()
                                             ) -> List[Expr]:
        from swarm_engine.primitives.core import infer
        synth = self._synth
        if not isinstance(sample_values, list):
            sample_values = [sample_values]
        args_list = [{param_name: v} for v in sample_values]
        n_samples = len(args_list)

        bank_by_size: Dict[int, List[Tuple[Expr, Tuple[Any, ...], Any]]] = {}
        seen_value_keys: Dict[str, bool] = {}
        same_type_candidates: List[Expr] = []
        input_type = infer(sample_values[0])

        def references_param(expr: Expr) -> bool:
            if expr.is_leaf():
                return not expr.is_literal
            return any(references_param(child) for _, child in expr.children)

        def try_add(expr: Expr, values: Tuple[Any, ...], size: int) -> None:
            key = synth._value_key(values)
            depends_on_param = references_param(expr)
            if key in seen_value_keys:
                # A constant expression (doesn't reference the parameter at
                # all) can never be a useful iteration step — applying it
                # repeatedly produces the same value forever, regardless of
                # input. A parameter-dependent candidate must never lose its
                # slot to a constant, regardless of which was tried first.
                if depends_on_param and not seen_value_keys[key]:
                    pass  # fall through and add anyway
                else:
                    return
            seen_value_keys[key] = depends_on_param or seen_value_keys.get(key, False)
            # Same guard as GeneralSynthesizer.search(), applied here because
            # this loop duplicates its bank-building logic rather than
            # calling search() directly (this enumeration works from sample
            # values, not full examples, which search() doesn't support).
            # Found necessary by reproducing the exact seeded_random hang
            # search() was already fixed against — a value this loop itself
            # constructs (via ordinary primitives like power/factorial) can
            # be fed as the count argument to something like
            # seeded_random(seed, n), which calls range(int(n)) and never
            # returns. The fix must live wherever bank entries are admitted,
            # not just in the one loop that was tested first.
            if not all(synth._value_is_safe(v) for v in values):
                return
            out_type = infer(values[0]) if values else None
            bank_by_size.setdefault(size, []).append((expr, values, out_type))
            # Kind equality was too strict for this purpose, found directly:
            # divide(8, 2) = 4.0 (FLOAT) was excluded when the sample input
            # was an INT (8), even though Python treats them as
            # interchangeable (4 == 4.0) and this schema's whole point is
            # applying the same step repeatedly to a value that stays in the
            # same logical domain — int vs float is a within-domain
            # distinction here, not a categorical one. A genuinely different
            # domain (e.g. a step that turns a number into a string) should
            # still be excluded, which _numerically_compatible preserves.
            if out_type is not None and self._numerically_compatible(out_type, input_type):
                same_type_candidates.append(expr)

        leaf = Expr(param=param_name)
        values = synth._eval_all(leaf, args_list)
        if values is not None:
            try_add(leaf, values, 0)

        # Same reason literal seeding exists in GeneralSynthesizer.search():
        # without it, a step function like divide(n, 2) is structurally
        # unreachable — the only leaf available is the parameter itself, so
        # every candidate ends up dividing/combining n with itself (e.g.
        # divide(n, n) = 1 always) rather than with any actual constant.
        # Found directly: enumeration for a halving task never produced
        # divide(n, 2) at all, only coincidental fits using n alone, because
        # this loop duplicates search()'s bank-building without duplicating
        # the fix already made there.
        # Data-driven literal inference, generalized beyond the fixed
        # {0,1,2,-1} set: the fixed set alone made a real, non-contrived
        # task ("count subtractions of 7 to reach zero") structurally
        # unreachable, since 7 is not among the defaults and isn't
        # constructible from them within the enumeration's own size
        # budget. extra_literals is computed by the caller from the
        # ACTUAL examples given (e.g. the difference between consecutive
        # inputs), never invented or task-specific — any step-counting
        # problem's natural step size is discoverable this way, not just
        # this one benchmark's.
        all_literals = list(GeneralSynthesizer.LITERAL_CONSTANTS) + \
            [v for v in extra_literals if v not in GeneralSynthesizer.LITERAL_CONSTANTS]
        for literal_value in all_literals:
            lit_leaf = Expr(literal=literal_value, is_literal=True)
            lit_values = tuple(literal_value for _ in args_list)
            try_add(lit_leaf, lit_values, 0)

        started = time.time()
        candidates_examined = 0
        for target_size in range(1, self.step_search_max_size + 1):
            ordered_ops = sorted(synth._ops, key=lambda o: -synth._entry_priority(Expr(op=o)))
            for op in ordered_ops:
                if time.time() - started > self.enumeration_wall_clock_s:
                    return same_type_candidates
                prim = self.reg.get(op)
                required = [k for k, v in prim.inputs.items() if not v.optional]
                remaining = target_size - 1
                if remaining < 0:
                    continue
                for size_partition in synth._size_partitions(remaining, len(required)):
                    slot_pools = []
                    feasible = True
                    for arg_name, size in zip(required, size_partition):
                        arg_type = prim.inputs[arg_name]
                        pool = [(e, v) for e, v, t in bank_by_size.get(size, [])
                               if t is not None and arg_type.accepts(t)]
                        if not pool:
                            feasible = False
                            break
                        pool.sort(key=lambda ev: (
                            -synth._entry_priority(ev[0]),
                            synth._stable_tiebreak(
                                ev[0].op if not ev[0].is_leaf()
                                else f"leaf:{ev[0].param}:{ev[0].literal}")))
                        slot_pools.append(pool[: synth.pool_per_type])
                    if not feasible:
                        continue
                    for combo in synth._bounded_product(slot_pools):
                        candidates_examined += 1
                        if candidates_examined >= synth.max_candidates:
                            return same_type_candidates
                        if candidates_examined % 500 == 0 and \
                                time.time() - started > self.enumeration_wall_clock_s:
                            return same_type_candidates
                        child_exprs = {name: expr for name, (expr, _) in
                                      zip(required, combo)}
                        kwargs_values = {name: values for name, (_, values) in
                                         zip(required, combo)}
                        result_values = synth._apply_across_examples(
                            prim, required, kwargs_values, n_samples)
                        if result_values is None:
                            continue
                        new_expr = Expr(op=op, children=tuple(
                            (name, child_exprs[name]) for name in required))
                        try_add(new_expr, result_values, target_size)

        return same_type_candidates

    @staticmethod
    def _numerically_compatible(a, b) -> bool:
        from swarm_engine.primitives.core import Kind
        numeric = {Kind.INT, Kind.FLOAT, Kind.NUM}
        if a.kind in numeric and b.kind in numeric:
            return True
        return a.kind == b.kind

    def _stop_value_candidates(self, examples: Sequence[Tuple[Dict[str, Any], Any]]
                               ) -> List[Any]:
        candidates = list(GeneralSynthesizer.LITERAL_CONSTANTS)
        for args, output in examples:
            for v in args.values():
                if v not in candidates:
                    candidates.append(v)
            if output not in candidates:
                candidates.append(output)
        return candidates

    def _matches_all(self, step_expr: Expr, stop_value: Any,
                     examples: Sequence[Tuple[Dict[str, Any], Any]],
                     param_name: str) -> bool:
        for args, expected_count in examples:
            count = self._iterate_count(step_expr, args[param_name], stop_value)
            if count != expected_count:
                return False
        return True

    def _iterate_count(self, step_expr: Expr, start_value: Any,
                       stop_value: Any) -> Optional[int]:
        current = start_value
        param_name = self._first_param_name(step_expr)
        for i in range(self.max_iterations):
            if current == stop_value:
                return i
            try:
                current = self._synth._eval(step_expr, {param_name: current})
            except Exception:
                return None
            # A step function whose value grows without bound across
            # repeated application (e.g. squaring) is not "counting down to
            # a stop value" in any sense this schema is meant to discover —
            # found necessary directly: GeneralSynthesizer's per-call value
            # guard catches a single unsafe ARGUMENT, but nothing previously
            # checked whether repeatedly APPLYING an otherwise-safe step
            # function drives the running value itself unbounded, which
            # multiply(n, n) does within a handful of iterations and which
            # then makes every subsequent bignum multiplication more
            # expensive than the last. Treating this as "not a match" is the
            # correct outcome either way — a genuinely diverging sequence
            # was never going to reach the stop value.
            if not self._synth._value_is_safe(current):
                return None
        return None

    def _first_param_name(self, expr: Expr) -> str:
        if expr.is_leaf():
            return expr.param
        for _, child in expr.children:
            name = self._first_param_name(child)
            if name is not None:
                return name
        return None

    def compile_primitive_fn(self, hypothesis: IterationHypothesis):
        step_expr = hypothesis.step_expr
        stop_value = hypothesis.stop_value
        max_iterations = hypothesis.max_iterations
        synth = self._synth
        param_name = self._first_param_name(step_expr)

        def _iterate_fn(**kwargs):
            value = next(iter(kwargs.values()))
            for i in range(max_iterations):
                if value == stop_value:
                    return i
                value = synth._eval(step_expr, {param_name: value})
                if not synth._value_is_safe(value):
                    raise RuntimeError(
                        f"iteration value grew unsafely large before "
                        f"reaching stop value {stop_value!r} — refusing to "
                        f"continue rather than risk unbounded computation")
            raise RuntimeError(f"iteration did not reach stop value "
                              f"{stop_value!r} within {max_iterations} steps")
        return _iterate_fn

class IteratedPrimitiveStore:
    """Persists iteration-derived primitives, mirroring
    capability/primitive_promotion.py's PromotedPrimitiveStore — same
    pattern, different underlying representation (an IterationHypothesis's
    step_expr + stop_value rather than a single composed Expr), because the
    two ways a new primitive can be constructed are architecturally
    distinct and conflating their storage would hide that."""

    def __init__(self, db_path: str = "swarm_engine.db"):
        self.db_path = db_path
        with self._conn() as conn:
            conn.execute("""CREATE TABLE IF NOT EXISTS iterated_primitives (
                name TEXT PRIMARY KEY, data TEXT NOT NULL, updated_at REAL)""")

    def _conn(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def save(self, name: str, hyp: IterationHypothesis, source_goal: str,
            input_kind: str) -> None:
        with self._conn() as conn:
            conn.execute("""INSERT OR REPLACE INTO iterated_primitives
                (name, data, updated_at) VALUES (?,?,?)""",
                (name, json.dumps({"name": name, "hyp": hyp.as_dict(),
                                  "step_expr": hyp.step_expr.as_dict(),
                                  "source_goal": source_goal,
                                  "input_kind": input_kind}), time.time()))

    def all(self) -> List[Dict[str, Any]]:
        with self._conn() as conn:
            rows = conn.execute("SELECT data FROM iterated_primitives").fetchall()
        return [json.loads(r["data"]) for r in rows]


class IterativePrimitiveGrower:
    """Connects PrimitiveConstructor's discovery to the same
    registration/persistence/provenance discipline PrimitivePromoter uses —
    a genuinely new primitive here crosses ExpressivenessAnalyzer's real
    type-reachability ceiling (no fixed-depth Expr tree can express a
    variable-trip-count loop), unlike promotion, which only ever
    rearranges what was already reachable.

    Independent re-verification, not self-certification: `synthesize()`'s
    own `_matches_all` check is not trusted as the final word — every
    example is re-run against the freshly compiled primitive function
    before registration, the same discipline PrimitivePromoter applies to
    its own candidates."""

    def __init__(self, registry, constructor: PrimitiveConstructor,
                store: IteratedPrimitiveStore, provenance=None):
        self.reg = registry
        self.constructor = constructor
        self.store = store
        self.provenance = provenance

    def grow(self, goal: str, examples: Sequence[Tuple[Dict[str, Any], Any]],
            param_name: str) -> Optional[str]:
        hyp = self.constructor.synthesize(examples, param_name)
        if hyp is None:
            return None

        compiled = self.constructor.compile_primitive_fn(hyp)
        for args, expected in examples:
            try:
                value = compiled(**{param_name: args[param_name]})
            except Exception:
                return None
            if value != expected:
                return None

        from swarm_engine.primitives.core import infer
        input_kind = infer(examples[0][0][param_name]).kind.value
        digest = hashlib_sha256(hyp.step_expr.canonical() + "|" +
                                str(hyp.stop_value) + "|" + param_name)
        name = f"iterated_{digest}"
        if name in self.reg:
            return name

        prim = Primitive(
            name=name, family="iterated", fn=lambda **kw: compiled(**kw),
            inputs={param_name: _kind_typespec(input_kind)},
            output=_kind_typespec("int"), effects=(Effect.PURE,),
            doc=f"iterated (step={' '.join(hyp.step_expr.ops_used())}, "
               f"stop={hyp.stop_value}): counts applications of the step "
               f"until the stop value is reached")
        self.reg.register(prim)
        self.store.save(name, hyp, goal, input_kind)

        if self.provenance is not None:
            from swarm_engine.governance.provenance import (
                Origin, ProvenanceRecord, TrustLevel,
            )
            self.provenance.record(ProvenanceRecord(
                capability_id=f"primitive:{name}", origin=Origin.SYNTHESIZED,
                trust=TrustLevel.TESTED, source="primitive_iteration",
                primitives_used=list(hyp.step_expr.ops_used())))
            self.provenance.log(f"primitive:{name}", "iterated_and_registered",
                                f"from goal {goal!r}: step="
                                f"{' '.join(hyp.step_expr.ops_used())}, "
                                f"stop={hyp.stop_value}")
        return name

    def rehydrate(self) -> List[str]:
        restored = []
        for record in self.store.all():
            name = record["name"]
            if name in self.reg:
                continue
            step_expr = Expr.from_dict(record["step_expr"])
            hyp = IterationHypothesis(
                step_expr=step_expr, stop_value=record["hyp"]["stop_value"],
                param_name=record["hyp"]["param_name"],
                max_iterations=record["hyp"]["max_iterations"])
            compiled = self.constructor.compile_primitive_fn(hyp)
            # Bind compiled at definition time via a factory: a bare
            # `lambda **kw: compiled(**kw)` over the loop variable would make
            # every rehydrated iterated primitive run the LAST record's
            # compiled function (classic closure-in-a-loop bug).
            def make_fn(bound_compiled):
                return lambda **kw: bound_compiled(**kw)
            prim = Primitive(
                name=name, family="iterated", fn=make_fn(compiled),
                inputs={hyp.param_name: _kind_typespec(record["input_kind"])},
                output=_kind_typespec("int"), effects=(Effect.PURE,),
                doc=f"iterated (rehydrated): step="
                   f"{' '.join(step_expr.ops_used())}, stop={hyp.stop_value}")
            self.reg.register(prim, overwrite=True)
            restored.append(name)
        return restored



def hashlib_sha256(basis: str) -> str:
    import hashlib
    return hashlib.sha256(basis.encode()).hexdigest()[:12]


def _kind_typespec(kind_name: str):
    from swarm_engine.primitives.core import Kind, TypeSpec
    mapping = {k.value: TypeSpec(kind=k) for k in Kind}
    return mapping.get(kind_name, mapping["any"])
