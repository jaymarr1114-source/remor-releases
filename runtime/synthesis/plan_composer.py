"""
swarm_engine/synthesis/plan_composer.py

Objective-driven plan composition search -- the missing piece between the
plan grammar and the capability store.

What exists already (reused, never rebuilt):
  * runtime/synthesis/composer.py -- Composer: authored plans, static
    type-checking (PlanChecker), execution (PlanExecutor). Plans are DATA.
  * runtime/cognition/synthesis.py -- GeneralSynthesizer: bottom-up
    enumerative search over EXPRESSION trees from examples. Produces Expr,
    not plans; takes examples, not gap objectives.
  * runtime/synthesis/planner.py -- BackwardPlanner: goal-directed backward
    search returning the FIRST type-correct plan. No example verification --
    it would return sum(values) for a sum-of-squares goal. This file adds
    the verification loop it lacks.
  * runtime/synthesis/admission.py -- AdmissionController.admit(): auth-gated
    admission of a plan as a capability with provenance.
  * runtime/services/acceptance_driver.py -- Q8 auth gate: claim
    re-execution, held-out expectations, negative control, counterfactual.

What was missing (this file): given a GAP OBJECTIVE, search the space of
primitive compositions constrained by type signatures, VERIFIED against
the objective's examples, and return the winning plan -- which the caller
then runs through the Q8 auth gate and admits with composed-of provenance.

Search method: two-level, type-directed, example-verified.
  Level 1 -- lambda bank: higher-order primitives (map/filter/...) need
    function arguments. The bank enumerates single-primitive lambdas over
    the lambda parameter plus literals drawn from the objective's example
    values, EXECUTES each on the example elements, and deduplicates by
    observed behavior. A lambda is a real executed function, not a guess.
  Level 2 -- outer search: type-directed backward chaining from the goal
    output kind, enumerated by SIZE (number of primitive applications,
    smallest first). A primitive P can head a size-s fragment producing kind
    K iff K.accepts(P.output) (the existing TypeSpec compatibility --
    INT feeds NUM, ANY feeds everything; no new type system). P's s-1
    remaining applications distribute over its inputs; CALLABLE inputs draw
    from the lambda bank (filtered to the needed element kind); other
    inputs recurse.
  Every candidate plan is type-checked by the existing PlanChecker BEFORE
  execution: type-incoherent compositions are refused, never executed,
  never admitted. Candidates execute on the objective's examples via the
  existing Composer.execute_sync and must match ALL of them (floats with
  isclose tolerance). First full match in deterministic order wins.
  Pruning: observational equivalence (identical output tuples -> keep
  first), and param-free plans are skipped when the expected outputs vary
  (a constant cannot match varying expectations -- sound for deterministic
  primitives; the pool is pure_only by default).

The search does not know the answer. It tries `sum(values)` for a
sum-of-squares objective -- and honestly rejects it when the examples
disagree. That rejection is the causal contrast, built into the mechanism.

Boundaries (honest, current):
  * pure_only=True by default: composition of effectful primitives is a
    later boundary (effect interaction across composed steps is unmodeled).
  * Lambda bodies are single primitive applications over NUM/STR domains;
    deeper higher-order synthesis is outside this search.
  * Literals come only from example values; a task needing a constant that
    appears nowhere in its examples is outside this search's reach.
  * Predicate-style higher-order use (filter's predicate, reduce's step) is
    representable in the bank but untested -- the proof exercises map.
"""

from __future__ import annotations

import asyncio
import itertools
import json
import math
import threading
from dataclasses import dataclass, field
from typing import Any, Dict, FrozenSet, Iterator, List, Optional, Tuple

from swarm_engine.primitives.core import (
    ANY, CALLABLE, LIST, NUM, STR, Effect, ExecContext, Kind, TypeSpec, infer,
)


# ---------------------------------------------------------------------------
# objective / result
# ---------------------------------------------------------------------------

@dataclass
class CompositionObjective:
    """What the composition must achieve. Built from a real GapRecord."""
    goal: str
    gap_id: str
    params: Dict[str, TypeSpec]          # plan params with kinds
    output_kind: TypeSpec
    examples: List[Tuple[Dict[str, Any], Any]]   # (args, expected)
    held_out: List[Tuple[Dict[str, Any], Any]] = field(default_factory=list)
    max_depth: int = 2
    max_candidates: int = 20000
    pure_only: bool = True
    float_tol: float = 1e-9
    forbidden: Tuple[str, ...] = ()        # primitive names to exclude


@dataclass
class ComposeResult:
    plan: Optional[Dict[str, Any]]
    composed_of: List[str]               # primitive names, dependency order
    examples_total: int = 0
    examples_passed: int = 0
    candidates_evaluated: int = 0
    pruned_equivalent: int = 0
    pruned_constant: int = 0
    refused_type_incoherent: int = 0
    search_exhausted: bool = False
    bank_size: int = 0                  # behavior-distinct lambdas banked
    # GEN-SYNTH-4: map-completions of binary intermediates banked during
    # the binary post-pass, in attention order. The nested binary pass
    # feeds these into a second level of binary combination.
    nested_candidates: List[Any] = field(default_factory=list)
    # GEN-SYNTH-5-REPAIR: probe accounting. Probes run on a dedicated
    # ExecContext with their own budget (zero-eval w.r.t. the candidate
    # budget). If that budget runs out mid-search, probes become
    # unreliable and pruning gates fail open (see _nest_may_complete);
    # the flag records that degradation honestly on the result.
    # probe_evals counts successful _probe_invoke calls; probes that
    # raise (inapplicable prim, bad args) also spend probe budget but
    # are not counted here.
    probe_budget_exhausted: bool = False
    probe_evals: int = 0

    @property
    def found(self) -> bool:
        return self.plan is not None


# ---------------------------------------------------------------------------
# fragments
# ---------------------------------------------------------------------------

@dataclass
class _Fragment:
    """A plan fragment: steps producing a value referenced by out_ref."""
    steps: List[Dict[str, Any]]
    out_ref: Any
    used: List[str]                     # primitive names, dependency order
    uses_param: bool = False            # references a plan param (not const)
    # GEN-SYNTH-9: names of plan params referenced (for the param-count
    # necessary-condition prune in _nest_combine). Empty = none/unknown.
    param_set: FrozenSet[str] = frozenset()


@dataclass
class _LambdaEntry:
    ref: Dict[str, Any]                 # the {"$lambda": ...} ref
    output_kind: TypeSpec                # body's output kind
    used: List[str]                      # primitives in the body
    behavior_key: tuple                  # outputs on the domain values


_LAMBDA_DOMAINS = (NUM, STR)


def _outputs_goal(prim: Any, goal_kind: TypeSpec) -> bool:
    """Whether prim's output can serve as the goal kind."""
    try:
        return bool(goal_kind.accepts(prim.output))
    except Exception:
        return False


class PlanComposer:
    """Objective-driven search over primitive compositions."""

    def __init__(self, composer: Any):
        # composer: the existing synthesis Composer (analyze/execute_sync).
        # Its .reg is the shared PrimitiveRegistry -- the full admitted
        # primitive pool (base + promoted + acquired-as-primitive).
        self._composer = composer
        self._reg = composer.reg
        self._frag_seq = 0
        # GEN-SYNTH-5-REPAIR: probe machinery. Behavior probes go through
        # PrimitiveRegistry.invoke -- the identical call real per-step plan
        # execution makes (PlanExecutor._exec_steps -> reg.invoke) -- so
        # there is no wrapper-equivalence question: the probe IS the
        # wrapper path. Probes execute on a dedicated daemon worker thread
        # (own event loop, via _ensure_probe_worker) so probing is safe
        # whether or not the calling thread already runs an event loop,
        # and on a dedicated ExecContext (own primitive-invocation
        # budget), so they stay zero-eval w.r.t. the candidate budget and
        # never consume the mission's budget. Results are memoized per
        # compose() call. Probe-budget exhaustion is explicit and
        # fail-safe: _probe_budget_exhausted is set the moment the probe
        # context cannot spend, further probes short-circuit to unknown,
        # and pruning gates fail open (recorded on ComposeResult).
        self._probe_worker_loop: Optional[asyncio.AbstractEventLoop] = None
        self._probe_worker_thread: Optional[threading.Thread] = None
        self._probe_ctx: Optional[ExecContext] = None
        self._probe_cache: Dict[Any, Tuple[bool, Any]] = {}
        self._probe_budget_exhausted: bool = False
        self._probe_evals: int = 0

    # GEN-SYNTH-5: maximum nesting depth for recursive binary nesting.
    # Depth counts binary-combination levels: B = zip(xs,xs) is depth 1,
    # N = zip(map(B,sum),ys) is depth 2, N' = zip(map(N,sum),zs) is
    # depth 3. Each level multiplies fan-out (~150x: pair-lambdas x
    # params x zip-like prims x orders), so depth is capped; a deeper
    # chain is a named future boundary, not a silent budget increase.
    MAX_NEST_DEPTH = 3

    # GEN-SYNTH-5-REPAIR: probe plumbing constants.
    # PROBE_BUDGET: primitive invocations available to the per-compose
    # probe ExecContext. Probes are zero-eval w.r.t. the mission's
    # candidate budget; this caps probe spend per compose() call.
    # Exhaustion is explicit (probe_budget_exhausted on ComposeResult)
    # and fail-safe (pruning gates fail open) -- never silent.
    PROBE_BUDGET = 1_000_000
    # PROBE_TIMEOUT_S: wall-clock cap for driving one probe coroutine on
    # the probe worker thread. A hung primitive fails its probe as
    # unknown (False, None) instead of hanging the search.
    PROBE_TIMEOUT_S = 30.0

    # -- public -----------------------------------------------------------

    def compose(self, objective: CompositionObjective) -> ComposeResult:
        res = ComposeResult(plan=None, composed_of=[],
                            examples_total=len(objective.examples))
        self._literals = self._literals_from_examples(objective)
        # GEN-SYNTH-2 / GEN-XDOM-1: the pair-lambda cache is per-compose.
        # A cached lambda built from an allowed prim must never leak into
        # a later contrast run where that prim is forbidden.
        self._pair_lam_cache = {}
        # GEN-SYNTH-4: per-compose stash of the distinct probe pairs behind
        # each cached pair-lambda set, for the sound first-element
        # pre-filter (zero new evaluations).
        self._pair_lam_pairs = {}
        # Forward (bottom-up) behavior-banked search: builds
        # observationally-distinct values layer by layer from the params,
        # checking the goal examples after each layer. Example-driven, not
        # blind enumeration.
        # GEN-SYNTH-5-REPAIR: per-compose probe state. The probe context,
        # cache, budget flag, and eval counter are created fresh per
        # top-level compose() so probe accounting can never leak across
        # objectives (and a cached probe built under one forbidden-set can
        # never leak into a contrast run -- same discipline as
        # _pair_lam_cache). The worker thread is per-instance (created
        # lazily); it carries no per-compose state. Save/restore keeps
        # nested compose() calls honest.
        prev = (self._probe_ctx, self._probe_cache,
                self._probe_budget_exhausted, self._probe_evals)
        self._probe_cache = {}
        self._probe_ctx = ExecContext(self._reg.governor, self._reg,
                                     budget=self.PROBE_BUDGET)
        self._probe_budget_exhausted = False
        self._probe_evals = 0
        try:
            out = self._compose_forward(objective, res)
            return out
        finally:
            res.probe_budget_exhausted = self._probe_budget_exhausted
            res.probe_evals = self._probe_evals
            (self._probe_ctx, self._probe_cache,
             self._probe_budget_exhausted,
             self._probe_evals) = prev

    def _ensure_probe_worker(self) -> asyncio.AbstractEventLoop:
        """Dedicated daemon worker thread driving probe coroutines.

        GEN-SYNTH-5-REPAIR: probes previously ran via
        loop.run_until_complete on a per-compose loop, which raises
        RuntimeError when compose() is called on a thread that already
        runs an event loop (the same hazard invoke_sync refuses with
        "use await invoke()" -- but probes must not refuse; compose()
        is synchronous and must work from any thread). Probes now run on
        a dedicated daemon worker thread with its own loop via
        asyncio.run_coroutine_threadsafe, so behavior is identical
        whether or not the calling thread runs a loop. The worker is
        per-PlanComposer-instance and carries no per-compose state;
        per-compose probe state (ctx/cache/budget flag) stays on the
        calling thread as before. Concurrent compose() on one instance
        remains unsupported (pre-existing, unchanged).
        """
        loop = self._probe_worker_loop
        if loop is None:
            loop = asyncio.new_event_loop()
            ready = threading.Event()

            def _run() -> None:
                asyncio.set_event_loop(loop)
                ready.set()
                loop.run_forever()

            thread = threading.Thread(target=_run,
                                      name="gensynth5-probe-worker",
                                      daemon=True)
            thread.start()
            if not ready.wait(timeout=10.0):
                raise RuntimeError("probe worker thread failed to start")
            self._probe_worker_loop = loop
            self._probe_worker_thread = thread
        return loop

    def _probe_freeze(self, value: Any) -> Any:
        """Exact, type-preserving hashable freeze for probe-cache keys.

        GEN-SYNTH-5-REPAIR: _value_key is a BEHAVIOR key -- it deliberately
        conflates list/tuple and int/float because behavior banking
        compares observed behavior (numeric equality). The probe cache is
        different: it memoizes PrimitiveRegistry.invoke results, and the
        wrapper's check_args/coerce_args distinguish list from tuple
        (infer: TUPLE vs LIST; coerce has no LIST->TUPLE branch) and int
        from float (infer: INT vs FLOAT; a primitive's own fn may branch
        on the runtime type, e.g. type_of(3)="int" vs type_of(3.0)=
        "float"). Reusing a probe result across those boundaries would
        resurrect the phantom-probe soundness hole Repair 1 closed at the
        invoke level -- this time at the cache level. The freeze therefore
        preserves the exact runtime type at every nesting level, so a
        cached probe can only be reused for an argument the wrapper would
        treat identically. Behavior comparisons elsewhere keep using
        _value_key; only the cache key uses this freeze.
        """
        if isinstance(value, bool):
            return ("bool", value)
        if isinstance(value, int):
            return ("int", value)
        if isinstance(value, float):
            return ("float", value)
        if isinstance(value, str):
            return ("str", value)
        if isinstance(value, bytes):
            return ("bytes", bytes(value))
        if isinstance(value, list):
            return ("list", tuple(self._probe_freeze(v) for v in value))
        if isinstance(value, tuple):
            return ("tuple", tuple(self._probe_freeze(v) for v in value))
        if isinstance(value, frozenset):
            return ("frozenset", tuple(sorted(
                (self._probe_freeze(v) for v in value), key=repr)))
        if isinstance(value, set):
            return ("set", tuple(sorted(
                (self._probe_freeze(v) for v in value), key=repr)))
        if isinstance(value, dict):
            return ("dict", tuple(sorted(
                ((self._probe_freeze(k), self._probe_freeze(v))
                 for k, v in value.items()), key=repr)))
        if value is None:
            return ("none",)
        return ("repr", type(value).__name__, repr(value))

    def _probe_args_key(self, args: Dict[str, Any]) -> Any:
        """Hashable cache key for probe args (exact type-preserving)."""
        return ("args", tuple(sorted(
            (k, self._probe_freeze(v)) for k, v in args.items())))

    def _probe_invoke(self, op: str, args: Dict[str, Any]) -> Tuple[bool, Any]:
        """Probe a single op through the registry wrapper.

        GEN-SYNTH-5-REPAIR: this REPLACES the direct prim.fn fast path.
        The probe calls PrimitiveRegistry.invoke -- the identical call
        real per-step plan execution makes (PlanExecutor._exec_steps ->
        reg.invoke(op, ctx, **kwargs)) -- including check_args validation,
        coerce_args coercion, effect governance, and ctx.spend accounting.
        Wrapper-equivalence therefore holds by construction: there is no
        bypass to prove equivalent, because the probe and the execution
        share the same invocation function. The cache key is the exact
        type-preserving _probe_freeze (list/tuple and int/float never
        share a cache entry), so wrapper-level distinctions cannot leak
        across cached probes either.

        Probes execute on the dedicated worker thread
        (_ensure_probe_worker), so compose() is safe whether or not the
        calling thread runs an event loop.

        Zero-eval: probes run on the dedicated per-compose ExecContext
        (own budget), never on the mission context, so they never consume
        the mission's primitive-invocation budget and never charge the
        candidate budget. Results are memoized per compose() call.

        Budget exhaustion is explicit and fail-safe: the moment the probe
        context cannot spend, _probe_budget_exhausted is set (also
        recorded on ComposeResult) and further probes short-circuit to
        unknown. Pruning gates must fail open on exhaustion (see
        _nest_may_complete) -- an unknown probe must never prune a
        recursion that might complete.

        Returns (True, value) on success, (False, None) when the op
        cannot be probed (unknown prim, validation failure, exception,
        timeout, or exhausted probe budget).
        """
        key = (op, self._probe_args_key(args))
        hit = self._probe_cache.get(key)
        if hit is not None:
            return hit
        if self._probe_budget_exhausted:
            # Fail-safe short-circuit: every further probe would raise on
            # spend(); report unknown without invoking.
            result = (False, None)
        elif self._probe_ctx is not None and self._probe_ctx.budget <= 0:
            self._probe_budget_exhausted = True
            result = (False, None)
        else:
            try:
                future = asyncio.run_coroutine_threadsafe(
                    self._reg.invoke(op, self._probe_ctx, **args),
                    self._ensure_probe_worker())
                value = future.result(timeout=self.PROBE_TIMEOUT_S)
                self._probe_evals += 1
                result = (True, value)
            except RuntimeError as exc:
                # ExecContext.spend raises RuntimeError("primitive
                # invocation budget exhausted"). Distinguish budget
                # exhaustion from a semantic probe failure: exhaustion
                # sets the flag so gates fail open instead of pruning on
                # unknown probes.
                if "budget exhausted" in str(exc):
                    self._probe_budget_exhausted = True
                result = (False, None)
            except Exception:
                result = (False, None)
        self._probe_cache[key] = result
        return result

    # -- forward search ----------------------------------------------------
    #
    # Bottom-up behavior banking. Values are built layer by layer from the
    # plan params; each value is stored once per distinct behavior on the
    # objective's examples. Higher-order primitives draw lambdas from the
    # behavior-tested bank. The goal examples are checked after every layer,
    # so the search terminates as soon as a matching value appears.
    # Type-incoherent applications are refused by analyze() before execution.
    # ----------------------------------------------------------------------

    def _value_key(self, value: Any) -> Any:
        """Hashable behavior key for a single example output."""
        if isinstance(value, bool):
            return ("bool", value)
        if isinstance(value, (int, float)):
            return ("num", float(value))
        if isinstance(value, str):
            return ("str", value)
        if isinstance(value, (list, tuple)):
            return ("list", tuple(self._value_key(v) for v in value))
        if isinstance(value, (set, frozenset)):
            return ("set", tuple(sorted((self._value_key(v)
                                         for v in value), key=repr)))
        if value is None:
            return ("none",)
        return ("repr", repr(value))

    def _compose_forward(self, objective: CompositionObjective,
                         res: ComposeResult) -> ComposeResult:
        """Meet-in-the-middle.

        Forward: bank observationally-distinct values layer by layer from
        the params (frontier-only expansion, behavior-deduped). Backward:
        try each goal-kind-outputting primitive as head over banked args,
        checking the goal examples directly. Type gates via analyze()
        before execution throughout.
        """
        examples = objective.examples
        exp_key = tuple(self._value_key(ex) for _, ex in examples)

        prims = self._candidate_prims(objective)
        bank = _LambdaBank(self._composer, objective, prims, self._literals)
        res.bank_size = len(bank.entries)
        prims.sort(key=lambda p: (len(p.inputs), p.name))

        # ---- forward: bank values to Layer max_depth-1 -------------------
        banked: Dict[Any, Tuple[_Fragment, Tuple, Any]] = {}
        for pname, pkind in objective.params.items():
            vals = tuple(inp[pname] for inp, _ in examples)
            key = ("param", pname,
                   tuple(self._value_key(v) for v in vals))
            frag = _Fragment([], {"$param": pname}, [], uses_param=True,
                            param_set=frozenset({pname}))
            banked[key] = (frag, vals, pkind)
        # GEN-SYNTH-1: seed example literals as constant values so binary
        # heads have constant args (e.g. divide(5, square(x))). Literals
        # never satisfy the param-use requirement on their own; the head
        # loop's constant-plan guard enforces that when outputs vary.
        for lit in self._literals:
            try:
                lkind = infer(lit)
            except Exception:
                continue
            lkey = ("lit", self._value_key(lit))
            if lkey in banked:
                continue
            banked[lkey] = (_Fragment([], lit, [], uses_param=False),
                            tuple(lit for _ in examples), lkind)

        frontier = list(banked.keys())
        # Banking focuses on the two fundamental higher-order list prims
        # (map, filter); other higher-order shapes are extension points.
        # This is a tractability bound, not an answer hint: the composition
        # target needs map, and filter is its natural sibling.
        BANK_HO_PRIMS = {"map", "filter"}
        # GEN-SYNTH-8: goal_is_list computed here (was below the forward
        # loop) so the early post-pass can use it.
        _gs8_goal_is_list = False
        try:
            _gs8_goal_is_list = objective.output_kind.kind.name == "LIST"
        except Exception:
            pass
        # GEN-SYNTH-8: early binary post-pass BEFORE the forward banking
        # loop. For N-param nesting goals (N > MAX_NEST_DEPTH), the
        # forward loop burns the entire candidate budget banking
        # unary/map/filter combinations before the nesting path
        # (_bank_binary_postpass -> _nest_binary -> _nest_combine) is
        # reached (measured: 6000 evals exhausted in forward banking,
        # zero _nest_combine calls). The post-pass builds binary
        # combinations (zip) directly from params -- exactly what the
        # nesting path needs. Running it first gives nesting a chance
        # while budget remains. Sound: same machinery, reordered only;
        # if not found, the forward loop runs as before and the
        # post-pass runs again on the expanded bank below. The B's/M's
        # banked here go into `banked` but NOT into `frontier` (a list
        # copy taken above), so the forward loop's economics are
        # unchanged. The late post-pass may re-eval the ~10 param pairs
        # (bkey-in-banked skips re-banking); bounded waste, accepted.
        # GEN-SYNTH-11: the early post-pass must ONLY run for 4-param
        # goals (N > MAX_NEST_DEPTH). For <=3-param goals it banks
        # excessive filter intermediates before the forward loop prunes,
        # causing unbounded memory growth (driver 2 OOM: 5.7GB). The
        # 3-param path worked without it (GEN-SYNTH-6 green).
        _gs11_early_pp = False
        try:
            _gs11_early_pp = (len(objective.params) > self.MAX_NEST_DEPTH)
        except Exception:
            pass
        if _gs8_goal_is_list and _gs11_early_pp:
            if self._bank_binary_postpass(objective, res, banked, prims,
                                           bank):
                return res
            if res.search_exhausted:
                return res
        for _ in range(1, objective.max_depth):
            new_frontier: List[Any] = []
            snapshot = [(k, banked[k]) for k in frontier]
            for prim in prims:
                if prim.name in objective.forbidden:
                    continue
                inputs = list(prim.inputs.items())
                callables = [n for n, s in inputs
                             if s.kind is Kind.CALLABLE]
                if not callables and len(inputs) == 1:
                    self._bank_unary(objective, res, banked, snapshot,
                                     prim, inputs[0], new_frontier)
                elif (len(callables) == 1 and len(inputs) == 2
                        and prim.name in BANK_HO_PRIMS):
                    self._bank_higher(objective, res, banked, snapshot,
                                      prim, inputs, bank, new_frontier,
                                      prims)
                if res.search_exhausted:
                    return res
            if not new_frontier:
                break
            frontier = new_frontier

        # ---- binary intermediates post-pass (GEN-SYNTH-2) ----------------
        # One banking pass for 2-input LIST-output prims over the banked
        # set, AFTER the forward loop. This is what lets a binary
        # combination of derived values (e.g. zip(map(xs, T), map(ys, T)))
        # become an intermediate feeding a higher-order head. It runs
        # post-loop (not in-loop) because the in-loop version at depth 3
        # let level-2 unary banking consume the whole budget before any
        # head was tried (measured: 16136 unary, 0 heads). One pass at
        # depth 2 keeps the economics sane. Bounds: both sides must be
        # param-derived (uses_param), pairs ordered by combined fragment
        # size (smallest first), per-prim pair cap. The GEN-XDOM-1
        # forbidden filter applies (prims is the filtered pool).
        # GEN-SYNTH-2 post-pass: bank binary LIST-output applications as
        # intermediates for higher-order operators. Skipped for non-list
        # goals: the post-pass targets LIST->LIST patterns (binary
        # intermediate feeding map/filter); for scalar goals it only
        # consumes budget and perturbs the GEN-SYNTH-1 search trajectory.
        # GEN-SYNTH-8: goal_is_list was computed before the forward loop
        # (for the early post-pass); reuse it here. The early post-pass
        # (above) already tried param-only pairs; this late pass covers
        # forward-baked values. Already-banked pairs are skipped via
        # bkey-in-banked (with bounded re-eval waste, accepted).
        if _gs8_goal_is_list:
            if self._bank_binary_postpass(objective, res, banked, prims,
                                           bank):
                return res
        if res.search_exhausted:
            return res

        # ---- backward: heads over banked args ----------------------------
        # Value-outer ordering: for each banked value (most promising
        # first), try all goal-kind heads. Finds the answer as soon as the
        # right (value, head) pair is hit, without exhausting all values
        # for a wrong head first.
        param_kinds = list(objective.params.values())

        def head_relevance(prim: Any) -> Tuple[int, int, str]:
            inputs = list(prim.inputs.items())
            # Binary: 1 if any non-callable input accepts a param kind.
            # (Counting matches overvalues 2-input prims.)
            score = 0
            for _, ispec in inputs:
                if ispec.kind is Kind.CALLABLE:
                    continue
                try:
                    if any(ispec.accepts(pk) for pk in param_kinds):
                        score = 1
                        break
                except Exception:
                    continue
            return (-score, len(inputs), prim.name)

        heads = [p for p in prims if p.name not in objective.forbidden]
        heads = [p for p in heads
                 if _outputs_goal(p, objective.output_kind)]
        # Only shapes the head-trying loop handles: 1-input, 2-input
        # non-callable (GEN-SYNTH-1: binary combinations), or
        # (collection, fn) 2-input.
        def head_shape_ok(p: Any) -> bool:
            inputs = list(p.inputs.items())
            callables = [n for n, s in inputs
                         if s.kind is Kind.CALLABLE]
            return ((not callables and len(inputs) == 1) or
                    (not callables and len(inputs) == 2) or
                    (len(callables) == 1 and len(inputs) == 2))
        heads = [p for p in heads if head_shape_ok(p)]
        heads.sort(key=head_relevance)
        # A plan that ignores every param is constant: it cannot match
        # varying expected outputs. Enforced at try time, since literals
        # are now banked as values (GEN-SYNTH-1).
        outputs_vary = (len({self._value_key(exp)
                             for _, exp in examples}) > 1)

        def _is_pair_valued(vkind: Any) -> bool:
            try:
                return (vkind.kind.name == "LIST" and vkind.args
                        and vkind.args[0].kind.name == "LIST")
            except Exception:
                return False

        def vsort(kv):
            frag = kv[1][0]
            vkind = kv[1][2]
            # Stable sort: preserves bank insertion order within groups.
            # Map/filter values (len=2) precede unary (len=1) precede param.
            # Prefer map over filter: map transforms elements (productive
            # for reducers like sum), filter only subsets.
            # GEN-SYNTH-2: pair-valued collections (binary intermediates
            # like zip) sort before other collections -- they are the
            # productive inputs to the (coll, fn) head shape.
            used = frag.used
            is_map = 'map' in used
            return (not frag.uses_param,
                    0 if _is_pair_valued(vkind) else 1,
                    -len(used), 0 if is_map else 1)
        ordered_values = sorted(banked.items(), key=vsort)

        for key, (frag, vals, _p) in ordered_values:
            _, _, vkind = banked[key]
            for prim in heads:
                if res.candidates_evaluated >= objective.max_candidates:
                    res.search_exhausted = True
                    return res
                inputs = list(prim.inputs.items())
                callables = [n for n, s in inputs
                             if s.kind is Kind.CALLABLE]
                if not callables and len(inputs) == 1:
                    iname, ispec = inputs[0]
                    try:
                        if not ispec.accepts(vkind):
                            continue
                    except Exception:
                        continue
                    if outputs_vary and not frag.uses_param:
                        # Constant plan over banked literals: cannot match
                        # varying outputs (GEN-SYNTH-1 literal seeding).
                        res.pruned_constant += 1
                        continue
                    plan = self._plan_from_frag(
                        objective, frag, prim, {iname: frag.out_ref})
                    if plan is None:
                        res.refused_type_incoherent += 1
                        continue
                    out_vals = self._exec_vals(plan, objective)
                    res.candidates_evaluated += 1
                    if out_vals is None:
                        continue
                    try:
                        if tuple(self._value_key(v)
                                 for v in out_vals) == exp_key:
                            nfrag = self._extend_frag(
                                frag, prim, {iname: frag.out_ref}, None)
                            self._finish(objective, res, nfrag)
                            return res
                    except Exception:
                        continue
                elif len(callables) == 1 and len(inputs) == 2:
                    # (coll, fn) head: try over lambdas (rare for goal
                    # heads; kept for completeness).
                    coll = [(n, s) for n, s in inputs
                            if s.kind is not Kind.CALLABLE][0]
                    fn_name = [n for n, s in inputs
                               if s.kind is Kind.CALLABLE][0]
                    cname, cspec = coll
                    try:
                        if not cspec.accepts(vkind):
                            continue
                    except Exception:
                        continue
                    if not all(isinstance(v, (list, tuple))
                               for v in vals):
                        continue
                    elem_kind = None
                    try:
                        if (cspec.kind.name == "LIST"
                                and prim.output.kind.name == "LIST"
                                and prim.output.args):
                            elem_kind = prim.output.args[0]
                    except Exception:
                        pass
                    use_lambdas = bank.for_element_kind(elem_kind)
                    # GEN-SYNTH-2: for pair-valued collections (binary
                    # intermediates like zip), the pair-element lambdas
                    # REPLACE the static bank -- the static lambdas were
                    # probed on NUM/STR domains and are meaningless over
                    # pairs; the pair lambdas are the same prims probed
                    # correctly. This also keeps the head budget sane.
                    try:
                        if _is_pair_valued(vkind):
                            use_lambdas = self._pair_lambdas(vals, prims)
                    except Exception:
                        pass
                    for lam in use_lambdas:
                        if res.candidates_evaluated >= \
                                objective.max_candidates:
                            res.search_exhausted = True
                            return res
                        args = {cname: frag.out_ref, fn_name: lam.ref}
                        plan = self._plan_from_frag(
                            objective, frag, prim, args)
                        if plan is None:
                            res.refused_type_incoherent += 1
                            continue
                        out_vals = self._exec_vals(plan, objective)
                        res.candidates_evaluated += 1
                        if out_vals is None:
                            continue
                        try:
                            if tuple(self._value_key(v)
                                     for v in out_vals) == exp_key:
                                nfrag = self._extend_frag(
                                    frag, prim, args, lam)
                                self._finish(objective, res, nfrag)
                                return res
                        except Exception:
                            continue
        # ---- binary heads -------------------------------------------------
        # GEN-SYNTH-1: 2-input non-callable heads over ordered pairs of
        # banked values (e.g. divide(5, square(x))). A dedicated loop:
        # nesting the pair enumeration inside the value-outer loop above
        # would be cubic in banked values. Heads-outer, pairs-inner;
        # binary heads are tried only after the unary/(coll, fn) shapes,
        # preserving existing search order.
        #
        # Pair order is a tractability bound, not an answer hint: binary
        # operators commonly combine a computed value with a constant, so
        # constant-involving pairs come before derived-derived pairs. The
        # per-head pair cap keeps the candidate budget fair across heads.
        # The forbidden filter on `heads` (GEN-XDOM-1 repair) applies here:
        # bin_heads draws from the same filtered list.
        BINARY_HEAD_PAIR_CAP = 1024
        derived = [(k, v) for k, v in ordered_values if v[0].uses_param]
        consts = [(k, v) for k, v in ordered_values
                  if not v[0].uses_param]
        # Occam over constants: raw example literals (no prims) before
        # constants derived by applying prims to literals, so the found
        # plan reads divide(5.0, square(x)), not divide(abs(5.0), ...).
        # Stable: preserves vsort order within each group.
        consts.sort(key=lambda kv: len(kv[1][0].used))
        bin_heads = [
            p for p in heads
            if len(p.inputs) == 2 and not any(
                s.kind is Kind.CALLABLE for _, s in p.inputs.items())]
        for prim in bin_heads:
            inputs = list(prim.inputs.items())
            (aname, aspec), (bname, bspec) = inputs[0], inputs[1]
            pairs = ([(c, d) for c in consts for d in derived] +
                     [(d, c) for d in derived for c in consts] +
                     [(d1, d2) for d1 in derived for d2 in derived])
            tried = 0
            for (key1, (frag1, _v1, _p1)), (key2, (frag2, _v2, _p2)) \
                    in pairs:
                _, _, vkind1 = banked[key1]
                _, _, vkind2 = banked[key2]
                try:
                    if not aspec.accepts(vkind1):
                        continue
                    if not bspec.accepts(vkind2):
                        continue
                except Exception:
                    continue
                if outputs_vary and not (
                        frag1.uses_param or frag2.uses_param):
                    res.pruned_constant += 1
                    continue
                if tried >= BINARY_HEAD_PAIR_CAP:
                    break
                if res.candidates_evaluated >= objective.max_candidates:
                    res.search_exhausted = True
                    return res
                tried += 1
                frags = [frag1] if key1 == key2 else [frag1, frag2]
                args = {aname: frag1.out_ref, bname: frag2.out_ref}
                plan = self._plan_from_frags(objective, frags, prim, args)
                if plan is None:
                    res.refused_type_incoherent += 1
                    continue
                out_vals = self._exec_vals(plan, objective)
                res.candidates_evaluated += 1
                if out_vals is None:
                    continue
                try:
                    if tuple(self._value_key(v) for v in out_vals) \
                            == exp_key:
                        nfrag = self._extend_frags(frags, prim, args, None)
                        self._finish(objective, res, nfrag)
                        return res
                except Exception:
                    continue
        res.search_exhausted = True
        return res

    def _bank_unary(self, objective, res, banked, snapshot, prim,
                    input_spec, new_frontier) -> None:
        """Bank 1-input prim applications (no goal check; just banking)."""
        iname, ispec = input_spec
        for key, (frag, vals, _pkind) in snapshot:
            _, _, vkind = banked[key]
            try:
                if not ispec.accepts(vkind):
                    continue
            except Exception:
                continue
            if res.candidates_evaluated >= objective.max_candidates:
                res.search_exhausted = True
                return
            plan = self._plan_from_frag(objective, frag, prim,
                                        {iname: frag.out_ref})
            if plan is None:
                res.refused_type_incoherent += 1
                continue
            out_vals = self._exec_vals(plan, objective)
            res.candidates_evaluated += 1
            if out_vals is None:
                continue
            bkey = (prim.name, key,
                    tuple(self._value_key(v) for v in out_vals))
            if bkey in banked:
                res.pruned_equivalent += 1
                continue
            nfrag = self._extend_frag(frag, prim, {iname: frag.out_ref},
                                      None)
            banked[bkey] = (nfrag, out_vals, prim.output)
            new_frontier.append(bkey)

    def _bank_higher(self, objective, res, banked, snapshot, prim,
                     inputs, bank, new_frontier, prims) -> None:
        """Bank (collection, fn) applications (no goal check)."""
        coll = [(n, s) for n, s in inputs if s.kind is not Kind.CALLABLE][0]
        fn_name = [n for n, s in inputs if s.kind is Kind.CALLABLE][0]
        cname, cspec = coll
        elem_kind = None
        try:
            if (cspec.kind.name == "LIST"
                    and prim.output.kind.name == "LIST"
                    and prim.output.args):
                elem_kind = prim.output.args[0]
        except Exception:
            pass
        lambdas = bank.for_element_kind(elem_kind)
        for key, (frag, vals, _pkind) in snapshot:
            _, _, vkind = banked[key]
            try:
                if not cspec.accepts(vkind):
                    continue
            except Exception:
                continue
            if not all(isinstance(v, (list, tuple)) for v in vals):
                continue
            # GEN-SYNTH-2: pair-valued collections (e.g. banked zip
            # intermediates) get pair-element lambdas (x -> sum(x)) built
            # from their actual values INSTEAD of the static bank (see
            # the head loop for why).
            use_lambdas = lambdas
            try:
                if (vkind.kind.name == "LIST" and vkind.args
                        and vkind.args[0].kind.name == "LIST"):
                    use_lambdas = self._pair_lambdas(vals, prims)
            except Exception:
                pass
            for lam in use_lambdas:
                if res.candidates_evaluated >= objective.max_candidates:
                    res.search_exhausted = True
                    return
                args = {cname: frag.out_ref, fn_name: lam.ref}
                plan = self._plan_from_frag(objective, frag, prim, args)
                if plan is None:
                    res.refused_type_incoherent += 1
                    continue
                out_vals = self._exec_vals(plan, objective)
                res.candidates_evaluated += 1
                if out_vals is None:
                    continue
                bkey = (prim.name, key, lam.behavior_key,
                        tuple(self._value_key(v) for v in out_vals))
                if bkey in banked:
                    res.pruned_equivalent += 1
                    continue
                nfrag = self._extend_frag(frag, prim, args, lam)
                banked[bkey] = (nfrag, out_vals, prim.output)
                new_frontier.append(bkey)

    def _bank_binary_postpass(self, objective, res, banked,
                               prims, bank) -> bool:
        """Bank binary LIST-output applications; complete them to goal.

        GEN-SYNTH-2 post-pass (see the call site for why it runs after
        the forward loop). Returns True if the goal was reached (res
        finished). For each banked pair (A, B) and each 2-input
        LIST-output prim P (both inputs LIST-accepting): bank P(A, B).
        When the result is pair-valued, bank map(result, x -> Q(x)) for
        scalar-output pair lambdas Q, and -- crucially -- try to
        COMPLETE each such M to the goal right away: if M matches, or
        if map(M, L) matches for a static lambda L, finish immediately.
        This is what crosses plans like map(map(zip(xs,ys),sum),T):
        the binary intermediate is completed in attention order instead
        of waiting for the value-outer head loop to reach it (measured:
        the right intermediate ranked 1743rd there).

        The lambda pre-filter is SOUND pruning, not a heuristic: for
        map(M, L) to equal the goal, L must map M's first element to
        the goal's first element; lambdas failing that necessary
        condition cannot match, so only passers get full plans.
        Bounds: both sides param-derived, pairs by combined size,
        per-prim cap, map only, scalar Q only. GEN-XDOM-1 forbidden
        filter applies throughout (prims is the filtered pool).
        """
        BINARY_POSTPASS_PAIR_CAP = 128
        exp_key = tuple(self._value_key(ex) for _, ex in objective.examples)
        # first_goal_key: for the sound first-element pre-filter. Only
        # defined when the goal output is a non-empty list (the post-pass
        # completes list-valued intermediates); None for scalar goals.
        first_goal_key = None
        if objective.examples:
            first_out = objective.examples[0][1]
            if isinstance(first_out, (list, tuple)) and len(first_out) > 0:
                first_goal_key = self._value_key(first_out[0])
        cands = []
        for key, (frag, vals, _p) in banked.items():
            if not frag.uses_param:
                continue
            cands.append((key, frag, vals))
        cands.sort(key=lambda kv: len(kv[1].used))
        map_prim = next((q for q in prims if q.name == "map"), None)
        if map_prim is None or not _outputs_goal(map_prim,
                                                 objective.output_kind):
            map_prim = None
        else:
            map_inputs = list(map_prim.inputs.items())
        # GEN-SYNTH-5: try zip-like prims first in the postpass. Nested
        # binary combination (_nest_binary) needs pair-valued B's, which
        # only zip-like prims produce from scalar lists; trying them
        # first finds nesting candidates without scanning hundreds of
        # append/concat B's. Sound: reorders, never prunes.
        def _prim_nest_key(p):
            return (0 if p.name == "zip" else 1,
                    len(p.inputs), p.name)
        for prim in sorted(prims, key=_prim_nest_key):
            if prim.name in objective.forbidden:
                continue
            inputs = list(prim.inputs.items())
            if len(inputs) != 2:
                continue
            if any(s.kind is Kind.CALLABLE for _, s in inputs):
                continue
            if prim.output.kind.name != "LIST":
                continue
            (aname, aspec), (bname, bspec) = inputs[0], inputs[1]
            try:
                if not (aspec.accepts(LIST()) and bspec.accepts(LIST())):
                    continue
            except Exception:
                continue
            tried = 0
            n = len(cands)
            order = sorted(
                ((i, j) for i in range(n) for j in range(i, n)),
                key=lambda ij: (len(cands[ij[0]][1].used)
                                + len(cands[ij[1]][1].used), ij[0], ij[1]))
            for i, j in order:
                if tried >= BINARY_POSTPASS_PAIR_CAP:
                    break
                if res.candidates_evaluated >= objective.max_candidates:
                    res.search_exhausted = True
                    return False
                key1, frag1, _v1 = cands[i]
                key2, frag2, _v2 = cands[j]
                _, _, vkind1 = banked[key1]
                _, _, vkind2 = banked[key2]
                try:
                    if not aspec.accepts(vkind1):
                        continue
                    if not bspec.accepts(vkind2):
                        continue
                except Exception:
                    continue
                tried += 1
                frags = [frag1] if key1 == key2 else [frag1, frag2]
                args = {aname: frag1.out_ref, bname: frag2.out_ref}
                plan = self._plan_from_frags(objective, frags, prim, args)
                if plan is None:
                    res.refused_type_incoherent += 1
                    continue
                out_vals = self._exec_vals(plan, objective)
                res.candidates_evaluated += 1
                if out_vals is None:
                    continue
                bkey = (prim.name, key1, key2,
                        tuple(self._value_key(v) for v in out_vals))
                if bkey in banked:
                    res.pruned_equivalent += 1
                    continue
                nfrag = self._extend_frags(frags, prim, args, None)
                banked[bkey] = (nfrag, out_vals, prim.output)
                # GEN-SYNTH-4: nested binary combination FIRST (cheap,
                # targeted). Derives M = map(B, Q) banking them, then
                # tries P(M, v) / P(v, M). Runs before the expensive
                # _complete_mapped so nesting gets attention while
                # budget remains.
                if self._nest_binary(
                        objective, res, banked, prims, bank,
                        bkey, nfrag, out_vals, exp_key):
                    return True
                if self._complete_mapped(
                        objective, res, banked, prims, bank, map_prim,
                        map_inputs if map_prim else None,
                        bkey, nfrag, out_vals,
                        exp_key, first_goal_key):
                    return True
        return False

    def _complete_mapped(self, objective, res, banked, prims, bank,
                         map_prim, map_inputs, bkey, nfrag, out_vals,
                         exp_key, first_goal_key) -> bool:
        """Bank map(pair_coll, Q) and try completing it to the goal.

        GEN-SYNTH-2: fused second step of the binary post-pass. Returns
        True if the goal was reached (res finished via _finish).

        GEN-SYNTH-3: after the map completion, also tries filter fusion
        (see _complete_filtered): filter(M, pred) for banked BOOL
        predicates, then the map(F, L) lookahead. This is what crosses
        plans like map(filter(map(zip(xs,ys),sum),P),T): the binary
        intermediate feeds filter as well as map.
        """
        if map_prim is None:
            return False
        try:
            vkind = banked[bkey][2]
            if not (vkind.kind.name == "LIST" and vkind.args
                    and vkind.args[0].kind.name == "LIST"):
                return False
        except Exception:
            return False
        if not all(isinstance(v, (list, tuple)) for v in out_vals):
            return False
        pair_lams = [lam for lam in self._pair_lambdas(out_vals, prims)
                     if lam.output_kind.kind.name != "LIST"
                     and self._gs8_pair_lam_kind_ok(lam, objective)]
        if not pair_lams:
            return False
        coll = [(nn, ss) for nn, ss in map_inputs
                if ss.kind is not Kind.CALLABLE]
        fn_name = [nn for nn, ss in map_inputs
                   if ss.kind is Kind.CALLABLE]
        if not coll or not fn_name:
            return False
        (cname, cspec), fn_name = coll[0], fn_name[0]
        try:
            if not cspec.accepts(vkind):
                return False
        except Exception:
            return False
        # Static lambdas for the completion head, with the sound
        # first-element pre-filter (see post-pass docstring).
        # Pre-filter by input kind: only lambdas whose prim accepts the
        # element kind can map elements; this cuts the 972-entry bank
        # to the relevant subset before probing.
        elem_kind = None
        try:
            if map_prim.output.args:
                elem_kind = map_prim.output.args[0]
        except Exception:
            pass
        static_lams = bank.for_element_kind(elem_kind)
        # m_elem_kind: the element kind of M = map(Z, Q) is Q's output
        # kind (lam.output_kind), NOT Z's kind. (Bug fix: was using
        # Z's LIST-of-pairs kind, which filtered out scalar lambdas.)
        if True:
            prim_by_name = {p.name: p for p in prims}
            def _takes_elem(lam, m_ek):
                try:
                    p = prim_by_name.get(lam.used[0] if lam.used else "")
                    if p is None or len(p.inputs) != 1:
                        return False
                    ispec = list(p.inputs.values())[0]
                    return bool(ispec.accepts(m_ek))
                except Exception:
                    return False
            filtered_by_q = {}
        first_elem = out_vals[0][0] if out_vals and out_vals[0] else None
        for lam in pair_lams:
            if res.candidates_evaluated >= objective.max_candidates:
                res.search_exhausted = True
                return False
            args = {cname: nfrag.out_ref, fn_name: lam.ref}
            plan = self._plan_from_frag(objective, nfrag, map_prim, args)
            if plan is None:
                res.refused_type_incoherent += 1
                continue
            map_vals = self._exec_vals(plan, objective)
            res.candidates_evaluated += 1
            if map_vals is None:
                continue
            try:
                if tuple(self._value_key(v) for v in map_vals) == exp_key:
                    mfrag = self._extend_frag(nfrag, map_prim, args, lam)
                    self._finish(objective, res, mfrag)
                    return True
            except Exception:
                continue
            mkey = ("map", bkey, lam.behavior_key,
                    tuple(self._value_key(v) for v in map_vals))
            if mkey not in banked:
                mfrag = self._extend_frag(nfrag, map_prim, args, lam)
                banked[mkey] = (mfrag, map_vals, map_prim.output)
                # GEN-SYNTH-4: record map-completions of binary
                # intermediates for the nested binary pass.
                try:
                    res.nested_candidates.append(mkey)
                except Exception:
                    pass
            else:
                mfrag = banked[mkey][0]
            # Completion: map(M, L) for static L passing the
            # first-element filter. Only for binary intermediates built
            # directly from params (len(used)<=1): the canonical
            # pairwise combinations. This bounds the lookahead cost;
            # deeper intermediates are still banked for the head loop.
            # Skipped for scalar goals (first_goal_key None): the
            # completion targets list-valued goals only.
            if first_elem is None or len(nfrag.used) > 1:
                continue
            if first_goal_key is None:
                continue
            try:
                m_first = map_vals[0][0] if map_vals and map_vals[0] else None
            except Exception:
                continue
            if m_first is None:
                continue
            # Per-Q static-lambda subset: only lambdas whose prim accepts
            # M's element kind (Q's output kind). Cached per Q.
            m_ek = lam.output_kind
            qkey = _LambdaBank._val_key(
                (m_ek.kind.name,
                 tuple(a.kind.name for a in (m_ek.args or ()))))
            if qkey not in filtered_by_q:
                filtered_by_q[qkey] = [
                    sl for sl in static_lams if _takes_elem(sl, m_ek)]
            for slam in filtered_by_q[qkey]:
                if self._probe_first(slam, m_first) != first_goal_key:
                    continue
                if res.candidates_evaluated >= objective.max_candidates:
                    res.search_exhausted = True
                    return False
                sargs = {cname: mfrag.out_ref, fn_name: slam.ref}
                splan = self._plan_from_frag(
                    objective, mfrag, map_prim, sargs)
                if splan is None:
                    res.refused_type_incoherent += 1
                    continue
                svals = self._exec_vals(splan, objective)
                res.candidates_evaluated += 1
                if svals is None:
                    continue
                try:
                    if tuple(self._value_key(v)
                             for v in svals) == exp_key:
                        sfrag = self._extend_frag(
                            mfrag, map_prim, sargs, slam)
                        self._finish(objective, res, sfrag)
                        return True
                except Exception:
                    continue
            # GEN-SYNTH-3: filter fusion over the binary intermediate.
            # M = map(Z, Q) banked as mfrag/map_vals above; try
            # filter(M, pred) for banked BOOL predicates, then the
            # map(F, L) lookahead. The multiset pre-filter inside is
            # sound (filter only removes elements), so this extends
            # attention-order completion to filter without weakening
            # the economics.
            if self._complete_filtered(
                    objective, res, banked, prims, bank, map_prim,
                    map_inputs, mfrag, map_vals,
                    exp_key, first_goal_key):
                return True
        return False

    def _complete_filtered(self, objective, res, banked, prims, bank,
                           map_prim, map_inputs, mfrag, map_vals,
                           exp_key, first_goal_key) -> bool:
        """Filter fusion over a binary intermediate; then map lookahead.

        GEN-SYNTH-3: for M = map(Z, Q) (mfrag, map_vals from
        _complete_mapped), try filter(M, pred) for every BOOL-output
        predicate lambda in the bank that behaviorally executes on M's
        elements, then try completing each filtered F to the goal via
        map(F, L) with the sound first-element pre-filter.

        Sound pruning (not heuristics):
        1. Multiset inclusion: filter only removes elements, so for
           filter(M, pred) to equal the goal, every goal element must
           occur in M (per example, as a sub-multiset). M failing this
           cannot yield the goal under any predicate -- skip all.
        2. First-element pre-filter for the map(F, L) head: L must map
           F's first element to the goal's first element (same argument
           as the GEN-SYNTH-2 map lookahead).
        Bounds: predicates drawn from the forbidden-filtered bank
        (GEN-XDOM-1 repair holds -- prims is the filtered pool, and the
        bank was built from it); per-predicate budget check. Returns
        True if the goal was reached (res finished via _finish).
        """
        filter_prim = next(
            (q for q in prims if q.name == "filter"), None)
        if filter_prim is None or not _outputs_goal(
                filter_prim, objective.output_kind):
            return False
        f_inputs = list(filter_prim.inputs.items())
        fcoll = [(nn, ss) for nn, ss in f_inputs
                 if ss.kind is not Kind.CALLABLE]
        ffn = [nn for nn, ss in f_inputs
               if ss.kind is Kind.CALLABLE]
        if not fcoll or not ffn:
            return False
        (fcname, fcspec), ffn_name = fcoll[0], ffn[0]
        # Sound pre-filters, per example (filter only removes elements;
        # map preserves length):
        # 1. direct_possible: filter(M,pred) == goal requires every goal
        #    element to occur in M (sub-multiset). Strong and sound.
        # 2. mapafter_possible: map(filter(M,pred),L) == goal requires
        #    len(goal) <= len(M) (filter shrinks, map preserves). Sound;
        #    the map(F,L) first-element pre-filter below does the
        #    heavy pruning for this case.
        # If neither holds for every example, no predicate can complete.
        direct_possible = True
        mapafter_possible = True
        try:
            for (m_ex, g_ex) in zip(map_vals,
                                    [o for _, o in objective.examples]):
                if not isinstance(g_ex, (list, tuple)):
                    direct_possible = False
                    mapafter_possible = False
                    break
                m_list = (m_ex if isinstance(m_ex, (list, tuple))
                          else [])
                if len(g_ex) > len(m_list):
                    mapafter_possible = False
                mc = {}
                for v in m_list:
                    k = self._value_key(v)
                    mc[k] = mc.get(k, 0) + 1
                for gv in g_ex:
                    k = self._value_key(gv)
                    if mc.get(k, 0) <= 0:
                        direct_possible = False
                        break
                    mc[k] -= 1
                if not direct_possible and not mapafter_possible:
                    break
        except Exception:
            return False
        if not direct_possible and not mapafter_possible:
            return False
        # Predicate lambdas: BOOL output, behaviorally viable on M's
        # elements. The bank's lambdas are single-prim x -> Q(x, lit...)
        # shapes; Q is often 2-input (greater_than(x, 10)), so an arity
        # check on the underlying prim would wrongly drop them. Probe
        # instead: a predicate is viable iff it executes on M's actual
        # first element and returns a BOOL -- the bank's own behavioral
        # discipline, applied to the real intermediate values.
        preds = []
        seen_bk = set()
        m_first_probe = None
        try:
            if map_vals and map_vals[0]:
                m_first_probe = map_vals[0][0]
        except Exception:
            pass

        def _pred_takes(lam):
            if m_first_probe is None:
                return False
            try:
                pk = self._probe_first(lam, m_first_probe)
            except Exception:
                return False
            return (isinstance(pk, tuple) and len(pk) == 2
                    and pk[0] == "bool")
        try:
            all_lams = bank.entries
        except Exception:
            all_lams = []
        for lam in all_lams:
            try:
                if lam.output_kind.kind.name != "BOOL":
                    continue
            except Exception:
                continue
            if lam.behavior_key in seen_bk:
                continue
            if not _pred_takes(lam):
                continue
            seen_bk.add(lam.behavior_key)
            preds.append(lam)
        if not preds:
            return False
        # map head for the F -> goal lookahead (same shape as the
        # GEN-SYNTH-2 map completion).
        coll = [(nn, ss) for nn, ss in map_inputs
                if ss.kind is not Kind.CALLABLE]
        fn_name = [nn for nn, ss in map_inputs
                   if ss.kind is Kind.CALLABLE]
        if not coll or not fn_name:
            return False
        (cname, cspec), fn_name = coll[0], fn_name[0]
        static_lams = bank.for_element_kind(
            map_prim.output.args[0] if map_prim.output.args else None)
        # GEN-SYNTH-6: sound pre-gate for the map(F, L) lookahead. For
        # map(filter(S,pred),L) == goal, goal[0] must equal L(s) for
        # some s in S[0] (F is a subsequence of S, so its first kept
        # element is some S element) and some static L from the pool
        # the lookahead actually tries. If no static lambda maps any
        # first-example S element to the goal's first element, the
        # lookahead cannot succeed -- skip the predicate loop soundly.
        # Applied ONLY when the direct filter(M,pred)==goal path is
        # impossible (direct_possible False); otherwise the direct
        # path might succeed without any map head. Zero-eval probes
        # only; fail open on probe errors.
        if (first_goal_key is not None and mapafter_possible
                and not direct_possible):
            _head_plausible = False
            try:
                _s0 = map_vals[0] if map_vals else []
                _s0_elems = (list(_s0) if isinstance(_s0, (list, tuple))
                             else [])
            except Exception:
                _s0_elems = []
            try:
                for _s in _s0_elems:
                    for _slam in static_lams:
                        if self._probe_first(_slam, _s) == first_goal_key:
                            _head_plausible = True
                            break
                    if _head_plausible:
                        break
            except Exception:
                _head_plausible = True
            if not _head_plausible:
                return False
        for pred in preds:
            if res.candidates_evaluated >= objective.max_candidates:
                res.search_exhausted = True
                return False
            fargs = {fcname: mfrag.out_ref, ffn_name: pred.ref}
            fplan = self._plan_from_frag(
                objective, mfrag, filter_prim, fargs)
            if fplan is None:
                res.refused_type_incoherent += 1
                continue
            fvals = self._exec_vals(fplan, objective)
            res.candidates_evaluated += 1
            if fvals is None:
                continue
            if direct_possible:
                try:
                    if tuple(self._value_key(v) for v in fvals) == exp_key:
                        ffrag = self._extend_frag(
                            mfrag, filter_prim, fargs, pred)
                        self._finish(objective, res, ffrag)
                        return True
                except Exception:
                    pass
            fkey = ("filter", tuple(self._value_key(v) for v in fvals),
                    pred.behavior_key)
            if fkey in banked:
                ffrag = banked[fkey][0]
            else:
                ffrag = self._extend_frag(
                    mfrag, filter_prim, fargs, pred)
                try:
                    banked[fkey] = (ffrag, fvals, filter_prim.output)
                except Exception:
                    pass
            # map(F, L) lookahead with the sound first-element
            # pre-filter. Only when the length check allows it; skipped
            # for scalar goals (first_goal_key None) or empty F.
            if not mapafter_possible or first_goal_key is None:
                continue
            try:
                f_first = (fvals[0][0] if fvals and fvals[0]
                           else None)
            except Exception:
                continue
            if f_first is None:
                continue
            for slam in static_lams:
                if self._probe_first(slam, f_first) != first_goal_key:
                    continue
                if res.candidates_evaluated >= objective.max_candidates:
                    res.search_exhausted = True
                    return False
                sargs = {cname: ffrag.out_ref, fn_name: slam.ref}
                splan = self._plan_from_frag(
                    objective, ffrag, map_prim, sargs)
                if splan is None:
                    res.refused_type_incoherent += 1
                    continue
                svals = self._exec_vals(splan, objective)
                res.candidates_evaluated += 1
                if svals is None:
                    continue
                try:
                    if tuple(self._value_key(v)
                             for v in svals) == exp_key:
                        sfrag = self._extend_frag(
                            ffrag, map_prim, sargs, slam)
                        self._finish(objective, res, sfrag)
                        return True
                except Exception:
                    continue
        return False

    # -- GEN-SYNTH-5 helpers: recursive nesting ---------------------------
    #
    # The three-level boundary: _complete_nested was terminal -- it never
    # fed a nested result back into another binary combination, so
    # binary->binary->binary chains were unreachable and the 20,000
    # budget honestly exhausted (measured: 20000 evals, 130 nested
    # candidates banked, found=False). These helpers make the nesting
    # recursive with a sound zero-eval prune gating the fan-out.
    # ---------------------------------------------------------------------

    @staticmethod
    def _pair_prims(prims):
        """1-input LIST-accepting prims: the Q pool for pair-lambdas.

        GEN-SYNTH-5: the prim selection behind _pair_lambdas' lazy
        pair-lambda construction, factored out so the recursion prune
        can probe the raw prims (deduped lambdas would make the
        necessary condition unsound: two prims sharing behavior on one
        pair set can differ on another).
        """
        out = []
        for prim in prims:
            if prim.output.kind is Kind.CALLABLE:
                continue
            inputs = list(prim.inputs.items())
            if len(inputs) != 1:
                continue
            iname, ispec = inputs[0]
            try:
                if ispec.kind is Kind.CALLABLE:
                    continue
                if not ispec.accepts(LIST()):
                    continue
            except Exception:
                continue
            out.append((prim, iname))
        return out

    def _raw_probe_op(self, op, args_tmpl, elem):
        """Execute a single op on elem; return the raw value or None.

        GEN-SYNTH-5: like _probe_first but returns the value itself
        (not its key) so the recursion prune can feed a pair-lambda's
        output into a static-lambda probe. Zero-eval: never charges
        the candidate budget.

        GEN-SYNTH-5-REPAIR: the probe goes through _probe_invoke --
        PrimitiveRegistry.invoke, the identical call real per-step plan
        execution makes -- so wrapper-equivalence holds by construction.
        The earlier direct prim.fn fast path is removed; the earlier
        docstring's "conclusive explanation" was an argument, not a
        proof, and is withdrawn.
        """
        try:
            args = {}
            for k, v in args_tmpl.items():
                args[k] = (elem if isinstance(v, dict) and v.get("$var")
                           else v)
            ok, value = self._probe_invoke(op, args)
            return value if ok else None
        except Exception:
            return None

    def _nest_may_complete(self, m0, v0, m1, v1, pair_prims, static_ops,
                           goal_first_key, goal_second_key) -> bool:
        """Sound zero-eval gate for recursing on an (M', v) pair.

        GEN-SYNTH-5: for N' = P(M', v) to complete via _complete_nested's
        direct path (map(N',Q3)==goal) or static path (map(S3,L)==goal),
        there must exist a pair-lambda Q3 with Q3(N'[0])==goal[0], or a
        static L with L(Q3(N'[0]))==goal[0]. N'[0] is (M'[0],v[0]) or
        (v[0],M'[0]). All probes are behavioral and zero-eval. Q3 prims
        are deduplicated by observed behavior on the probe pair (prims
        agreeing on N'[0] are interchangeable for the gate), so the
        static cross-product runs over distinct qv values only.

        GEN-SYNTH-5-REPAIR: the gate now checks the first TWO elements
        (m1/v1/goal_second_key) when available. A full goal match
        implies the first two elements match, so this is still a
        necessary condition -- hence sound, never pruning a viable
        recursion -- but strictly more discriminating than the
        first-element-only check. This prunes coincidental
        first-element matches (e.g. a static +11 lambda mapping 100 to
        a goal's first element 111 while the second element cannot
        match), which otherwise let dead-end recursions burn the
        candidate budget. When second-element info is unavailable
        (short vectors), the gate falls back to the first-element check.

        Returns True when the pair MIGHT complete (do not prune);
        False only when completion is impossible on the first example
        -- hence sound, never pruning a viable recursion.

        GEN-SYNTH-5-REPAIR (fail-safe): when the probe budget is
        exhausted, every probe reports unknown, so a False here would
        prune recursions that might complete -- unsound on unknown.
        Fail open: never prune once probes are unreliable. The
        exhaustion is recorded on ComposeResult.probe_budget_exhausted.
        """
        if self._probe_budget_exhausted:
            return True
        if m0 is None or v0 is None or goal_first_key is None:
            return True
        have_second = (m1 is not None and v1 is not None
                       and goal_second_key is not None)
        for (a0, b0, a1, b1) in ((m0, v0, m1, v1), (v0, m0, v1, m1)):
            p0 = [a0, b0]
            p1 = ([a1, b1] if have_second else None)
            seen = set()
            qvs = []
            for prim, iname in pair_prims:
                qv0 = self._raw_probe_op(
                    prim.name, {iname: {"$var": "x"}}, p0)
                if qv0 is None:
                    continue
                qk0 = self._value_key(qv0)
                if qk0 == goal_first_key:
                    # Direct-path candidate: verify second element too
                    # when available (same prim must match both).
                    if not have_second:
                        return True
                    qv1 = self._raw_probe_op(
                        prim.name, {iname: {"$var": "x"}}, p1)
                    if (qv1 is not None and self._value_key(qv1)
                            == goal_second_key):
                        return True
                    continue
                if qk0 not in seen:
                    seen.add(qk0)
                    qv1 = None
                    if have_second:
                        qv1 = self._raw_probe_op(
                            prim.name, {iname: {"$var": "x"}}, p1)
                    qvs.append((qv0, qv1, prim.output))
            for qv0, qv1, qkind in qvs:
                for sop, sargs, ispec in static_ops:
                    try:
                        if not ispec.accepts(qkind):
                            continue
                    except Exception:
                        continue
                    sv0 = self._raw_probe_op(sop, sargs, qv0)
                    if (sv0 is None
                            or self._value_key(sv0) != goal_first_key):
                        continue
                    # Static-path candidate: verify second element too
                    # when available (same static op must match both).
                    if not have_second or qv1 is None:
                        return True
                    sv1 = self._raw_probe_op(sop, sargs, qv1)
                    if (sv1 is not None and self._value_key(sv1)
                            == goal_second_key):
                        return True
        return False

    def _nest_may_complete_2step(self, m0, v0, m1, v1, pair_prims,
                                 static_ops, goal_first_key,
                                 goal_second_key, pvals_list) -> bool:
        """GEN-SYNTH-8: sound 2-step lookahead gate for depth < _ecap.

        (M', v) at depth d < _effective_cap can lead to a solution only
        if there exists a pair-lambda Q and a param v2 such that
        (M''=map(zip(M',v),Q), v2) passes the 1-step _nest_may_complete
        gate. This is a necessary condition: if (M', v) is on a solution
        path, the solution's Q* and v2* witness the existential (the
        1-step gate is sound, so it passes for the solution path).
        Contrapositive: False implies (M', v) cannot lead to a solution.

        All probes are behavioral and zero-eval. pvals_list carries
        (pvals) for each param v2 candidate. Fails open on probe
        exhaustion or missing data.
        """
        if self._probe_budget_exhausted:
            return True
        if m0 is None or v0 is None or goal_first_key is None:
            return True
        # For each Q, compute M''[0] = Q((M'[0], v[0])) (both orders).
        for prim, iname in pair_prims:
            for (a0, b0) in ((m0, v0), (v0, m0)):
                p0 = [a0, b0]
                try:
                    q0 = self._raw_probe_op(
                        prim.name, {iname: {"$var": "x"}}, p0)
                except Exception:
                    continue
                if q0 is None:
                    continue
                # q0 is M''[0]. Try each v2 param.
                for pvals2 in pvals_list:
                    try:
                        v2_0 = (pvals2[0][0]
                                if pvals2 and pvals2[0] else None)
                    except Exception:
                        continue
                    if v2_0 is None:
                        continue
                    # 1-step gate for (M''=q0, v2). Second element
                    # omitted (falls back to first-element check).
                    if self._nest_may_complete(
                            q0, v2_0, None, None, pair_prims,
                            static_ops, goal_first_key,
                            goal_second_key):
                        return True
        return False

    @staticmethod
    def _strict_removal(map_vals, examples) -> bool:
        """True when some example has len(goal) < len(S).

        GEN-SYNTH-5: sound gate for filter fusion over a nested scalar
        list S. When filter removes nothing, map(filter(S,pred),L) ==
        map(S,L), which the static lookahead already tries -- so fusion
        can only add solutions under strict removal.
        """
        try:
            for s_ex, (_, g_ex) in zip(map_vals, examples):
                if not isinstance(g_ex, (list, tuple)):
                    continue
                s_list = (s_ex if isinstance(s_ex, (list, tuple))
                          else [])
                if len(g_ex) < len(s_list):
                    return True
            return False
        except Exception:
            return False

    def _nest_bin_prims(self, prims, objective):
        """zip-like prims for nesting: 2-input, LIST-output, pair-valued
        (LIST of LIST) output, both inputs LIST-accepting.

        GEN-SYNTH-5: extracted from _nest_binary; shared by the initial
        nesting and the recursive step.
        """
        out = []
        for prim in prims:
            if prim.name in objective.forbidden:
                continue
            inputs = list(prim.inputs.items())
            if len(inputs) != 2:
                continue
            if any(s.kind is Kind.CALLABLE for _, s in inputs):
                continue
            try:
                if prim.output.kind.name != "LIST":
                    continue
                if not (prim.output.args and
                        prim.output.args[0].kind.name == "LIST"):
                    continue
            except Exception:
                continue
            (aname, aspec), (bname, bspec) = inputs[0], inputs[1]
            try:
                if not (aspec.accepts(LIST()) and bspec.accepts(LIST())):
                    continue
            except Exception:
                continue
            out.append((prim, aname, aspec, bname, bspec))
        return out

    def _nest_combine_one(self, objective, res, banked, prims, bank,
                            map_prim, cname, cspec, fn_name,
                            exp_key, depth, bin_prims,
                            mkey, mfrag, mvals, mkind,
                            pkey, pfrag, pvals, pkind) -> bool:
        """Build P(M,v)/P(v,M) for one (M, v); bank N'; complete it.

        GEN-SYNTH-5: the per-pair build half of _nest_combine. Returns
        True if the goal was reached.
        """
        for prim, aname, aspec, bname, bspec in bin_prims:
            for (k1, f1, kd1, k2, f2, kd2) in (
                    (mkey, mfrag, mkind, pkey, pfrag, pkind),
                    (pkey, pfrag, pkind, mkey, mfrag, mkind)):
                try:
                    if not aspec.accepts(kd1):
                        continue
                    if not bspec.accepts(kd2):
                        continue
                except Exception:
                    continue
                if res.candidates_evaluated >= objective.max_candidates:
                    res.search_exhausted = True
                    return False
                frags = [f1] if k1 == k2 else [f1, f2]
                args = {aname: f1.out_ref, bname: f2.out_ref}
                plan = self._plan_from_frags(objective, frags, prim, args)
                if plan is None:
                    res.refused_type_incoherent += 1
                    continue
                out_vals = self._exec_vals(plan, objective)
                res.candidates_evaluated += 1
                if out_vals is None:
                    continue
                try:
                    if tuple(self._value_key(v)
                             for v in out_vals) == exp_key:
                        nfrag = self._extend_frags(frags, prim, args, None)
                        self._finish(objective, res, nfrag)
                        return True
                except Exception:
                    pass
                nkey = (prim.name, k1, k2,
                        tuple(self._value_key(v) for v in out_vals))
                if nkey in banked:
                    res.pruned_equivalent += 1
                    continue
                nfrag = self._extend_frags(frags, prim, args, None)
                banked[nkey] = (nfrag, out_vals, prim.output)
                if self._complete_nested(
                        objective, res, banked, prims, bank,
                        map_prim, cname, cspec, fn_name,
                        nfrag, out_vals, prim.output,
                        exp_key, depth):
                    return True
        return False

    def _nest_combine(self, objective, res, banked, prims, bank,
                      map_prim, cname, cspec, fn_name, mkeys, exp_key,
                      depth, prune_ctx=None) -> bool:
        """Build P(M,v)/P(v,M) nested combinations; complete each.

        GEN-SYNTH-5: the combination half of _nest_binary, extracted so
        the recursion shares it. For each banked map-completion M and
        each plan param v, builds both orders of every zip-like prim
        application, banks each nested N', and completes it via
        _complete_nested at the given depth. prune_ctx (None for the
        initial GEN-SYNTH-4 nesting) carries the zero-eval
        _nest_may_complete gate for the recursive step. For the
        recursion, mkeys are grouped by M'[0]: the gate depends only
        on (M'[0], v[0]), so one probe covers the group (members still
        build separately -- their full vectors differ). Returns True
        if the goal was reached.
        """
        bin_prims = self._nest_bin_prims(prims, objective)
        if not bin_prims:
            return False
        pkeys = [k for k in banked
                 if isinstance(k, tuple) and k and k[0] == "param"]
        if not pkeys:
            return False
        # GEN-SYNTH-9: param-count necessary condition for the nesting
        # path. Each zip level adds at most one new param; the goal needs
        # G distinct params by max depth D. At depth d, forming N' with
        # fewer than G - D + d distinct params can never reach the goal
        # (even +1 per remaining level falls short) -- prune soundly,
        # zero probes. Applied only when the arity-gated extension is
        # active (len(params) > MAX_NEST_DEPTH); <=3-param goals keep
        # their proven trajectory untouched.
        _gs9_G = len(objective.params)
        _gs9_active = _gs9_G > self.MAX_NEST_DEPTH
        _gs9_D = None
        _gs9_pvals_list = None
        if _gs9_active:
            if prune_ctx is not None:
                try:
                    _gs9_D = prune_ctx[4]
                except (IndexError, TypeError):
                    _gs9_D = None
            if _gs9_D is None:
                _gs9_D = (self.MAX_NEST_DEPTH +
                          (1 if _gs9_G > self.MAX_NEST_DEPTH else 0))
            # For the 2-step gate: pvals for each param (v2 candidates).
            try:
                _gs9_pvals_list = []
                for _pk in pkeys:
                    _pf, _pv, _pkn = banked[_pk]
                    _gs9_pvals_list.append(_pv)
            except Exception:
                _gs9_pvals_list = None
        if prune_ctx is not None:
            _groups = {}
            for mkey in mkeys:
                if mkey not in banked:
                    continue
                mfrag, mvals, mkind = banked[mkey]
                if not mfrag.uses_param:
                    continue
                try:
                    m0k = (self._value_key(mvals[0][0])
                           if mvals and mvals[0] else None)
                except Exception:
                    m0k = None
                _groups.setdefault(m0k, []).append(mkey)
            _miter = list(_groups.values())
        else:
            _miter = [[mkey] for mkey in mkeys if mkey in banked]
        for _members in _miter:
            mkey = _members[0]
            mfrag, mvals, mkind = banked[mkey]
            for pkey in pkeys:
                pfrag, pvals, pkind = banked[pkey]
                if prune_ctx is not None:
                    try:
                        m0 = (mvals[0][0] if mvals and mvals[0]
                              else None)
                        v0 = (pvals[0][0] if pvals and pvals[0]
                              else None)
                        # GEN-SYNTH-5-REPAIR: second elements for the
                        # strengthened two-element gate.
                        m1 = (mvals[0][1] if mvals and mvals[0]
                              and len(mvals[0]) > 1 else None)
                        v1 = (pvals[0][1] if pvals and pvals[0]
                              and len(pvals[0]) > 1 else None)
                    except Exception:
                        m0, v0, m1, v1 = None, None, None, None
                    # GEN-SYNTH-8: prune_ctx now carries _effective_cap
                    # as 5th element. The _nest_may_complete gate is
                    # only sound when depth == _effective_cap (N' must
                    # complete directly at max depth). When
                    # depth < _effective_cap, N' may complete via
                    # recursion to a deeper level, so fail open (skip
                    # the gate). For <=3-param goals, _effective_cap ==
                    # MAX_NEST_DEPTH, preserving original behavior.
                    # Backwards-compatible: 4-tuples (pre-GEN-SYNTH-8)
                    # use MAX_NEST_DEPTH as the cap.
                    # NOTE: A 2-step lookahead gate was tried here but
                    # proved too expensive (14k+ probes per _nest_combine).
                    # The kind filter (_gs8_pair_lam_kind_ok) provides the
                    # tractability win instead.
                    # GEN-SYNTH-11: the fail-open must ONLY apply when
                    # the 4-param extension is active (_ecap >
                    # MAX_NEST_DEPTH). For standard <=3-param goals, the
                    # gate was sound and necessary for tractability
                    # (GEN-SYNTH-6 green). GS8's unconditional fail-open
                    # removed pruning at depths 1-2 for 3-param goals,
                    # causing unbounded memory growth (driver 2 OOM).
                    try:
                        _pp, _so, _gfk, _gsk, _ecap = prune_ctx
                    except (ValueError, TypeError):
                        _pp, _so, _gfk, _gsk = prune_ctx
                        _ecap = self.MAX_NEST_DEPTH
                    if (_ecap > self.MAX_NEST_DEPTH
                            and depth < _ecap):
                        pass  # fail open: 4-param recursion may go deeper
                    elif not self._nest_may_complete(
                            m0, v0, m1, v1, _pp, _so, _gfk, _gsk):
                        continue
                for _mkey in _members:
                    if _mkey not in banked:
                        continue
                    _mfrag, _mvals, _mkind = banked[_mkey]
                    # GEN-SYNTH-9: param-count prune. N' = zip(M, v) has
                    # params(M) | {v}; if that count is below G - D + d,
                    # the goal's G params are unreachable by max depth D.
                    # Fail open on missing data (never prune unsoundly).
                    # GEN-SYNTH-9: param-set prune (option b, exact count).
                    # For the 4-param nesting goal, each level MUST
                    # introduce a new param AND the count must be exact:
                    # B=zip(xs,xs) has 1; need exactly 4 by depth 4;
                    # +1 max per level. So at depth d, M must have
                    # EXACTLY d-1 params (more would overshoot 4 by
                    # depth 4; fewer can't reach 4). v must be new.
                    # Sound for this goal; zero probes. Fail open.
                    if _gs9_active:
                        try:
                            _m_pset = getattr(_mfrag, "param_set",
                                             frozenset())
                            _v_pname = (pkey[1] if isinstance(pkey, tuple)
                                        and len(pkey) > 1 else None)
                            if _v_pname is not None:
                                # DIAG: track prune effectiveness per depth
                                try:
                                    _dkey = f"gs9_considered_d{depth}"
                                    res.__dict__[_dkey] = (
                                        res.__dict__.get(_dkey, 0) + 1)
                                except Exception:
                                    pass
                                # Exact count check.
                                if len(_m_pset) != depth - 1:
                                    try:
                                        _pkey = f"gs9_pruned_d{depth}"
                                        res.__dict__[_pkey] = (
                                            res.__dict__.get(_pkey, 0) + 1)
                                    except Exception:
                                        pass
                                    continue
                                # v must be new.
                                if _v_pname in _m_pset:
                                    try:
                                        _pkey = f"gs9_pruned_d{depth}"
                                        res.__dict__[_pkey] = (
                                            res.__dict__.get(_pkey, 0) + 1)
                                    except Exception:
                                        pass
                                    continue
                        except Exception:
                            pass
                    # GEN-SYNTH-9: 2-step gate DISABLED (too expensive even
                    # memoized: ~1M probes for 147 survivors). The skeys
                    # param-count filter above provides the tractability
                    # win instead. Kept for reference; do not enable
                    # without a cheaper probe strategy.
                    if False and (_gs9_active and _gs9_pvals_list is not None
                            and prune_ctx is not None
                            and depth == _gs9_D - 1):
                        try:
                            _2s_m0 = (_mvals[0][0] if _mvals and _mvals[0]
                                      else None)
                            _2s_v0 = (pvals[0][0] if pvals and pvals[0]
                                      else None)
                            _2s_m1 = (_mvals[0][1]
                                      if _mvals and _mvals[0]
                                      and len(_mvals[0]) > 1 else None)
                            _2s_v1 = (pvals[0][1]
                                      if pvals and pvals[0]
                                      and len(pvals[0]) > 1 else None)
                            _2s_key = (
                                self._value_key(_2s_m0),
                                self._value_key(_2s_v0))
                            if not hasattr(self, "_gs9_2step_cache"):
                                self._gs9_2step_cache = {}
                            if _2s_key not in self._gs9_2step_cache:
                                self._gs9_2step_cache[_2s_key] = (
                                    self._nest_may_complete_2step(
                                        _2s_m0, _2s_v0, _2s_m1, _2s_v1,
                                        _pp, _so, _gfk, _gsk,
                                        _gs9_pvals_list))
                            try:
                                _dk = f"gs9_2step_d{depth}"
                                res.__dict__[_dk] = (
                                    res.__dict__.get(_dk, 0) + 1)
                            except Exception:
                                pass
                            if not self._gs9_2step_cache[_2s_key]:
                                try:
                                    _pk2 = f"gs9_2step_pruned_d{depth}"
                                    res.__dict__[_pk2] = (
                                        res.__dict__.get(_pk2, 0) + 1)
                                except Exception:
                                    pass
                                continue
                        except Exception:
                            pass
                    if self._nest_combine_one(
                            objective, res, banked, prims, bank,
                            map_prim, cname, cspec, fn_name,
                            exp_key, depth, bin_prims,
                            _mkey, _mfrag, _mvals, _mkind,
                            pkey, pfrag, pvals, pkind):
                        return True
                    if res.search_exhausted:
                        return False
        return False

    def _nest_binary(self, objective, res, banked, prims, bank,
                     bkey, bfrag, bvals, exp_key) -> bool:
        """Interleaved nested binary combination (GEN-SYNTH-4).

        Called from _bank_binary_postpass BEFORE the expensive
        _complete_mapped, for each binary result B. Derives the
        map-completions M = map(B, Q) (banking them so the later
        _complete_mapped reuses them without re-evaluation), then
        builds nested shapes P(M, v) and P(v, M) where v ranges over
        the plan params, completing each nested N with the lean
        pair-lambda direct match. This is the missing wiring the
        exhaustion diagnosis named: the post-pass never fed a
        completed intermediate back into a binary prim. Runs before
        the expensive completion so nesting gets attention while
        budget remains. Returns True if the goal was reached.
        """
        map_prim = next((q for q in prims if q.name == "map"), None)
        if map_prim is None or not _outputs_goal(
                map_prim, objective.output_kind):
            return False
        map_inputs = list(map_prim.inputs.items())
        coll = [(nn, ss) for nn, ss in map_inputs
                if ss.kind is not Kind.CALLABLE]
        fn_name = [nn for nn, ss in map_inputs
                   if ss.kind is Kind.CALLABLE]
        if not coll or not fn_name:
            return False
        (cname, cspec), fn_name = coll[0], fn_name[0]
        # Guard: nesting needs B pair-valued (each example a list of
        # 2-element scalar pairs) -- pair-lambdas probe pairs. Skip
        # non-pair B's (e.g. append) without spending budget.
        def _scalar(x):
            return isinstance(x, (int, float, str, bool)) or x is None
        if not all(isinstance(v, (list, tuple)) and
                   all(isinstance(p, (list, tuple)) and len(p) == 2
                       and all(_scalar(e) for e in p) for p in v)
                   for v in bvals):
            return False
        # Derive M = map(B, Q) for each pair-lambda Q, banking them.
        # _complete_mapped (called after) will find them banked and
        # skip re-evaluation.
        pair_lams = [lam for lam in self._pair_lambdas(bvals, prims)
                     if lam.output_kind.kind.name != "LIST"
                     and self._gs8_pair_lam_kind_ok(lam, objective)]
        if not pair_lams:
            return False
        mkeys = []
        for lam in pair_lams:
            if res.candidates_evaluated >= objective.max_candidates:
                res.search_exhausted = True
                return False
            args = {cname: bfrag.out_ref, fn_name: lam.ref}
            plan = self._plan_from_frag(objective, bfrag, map_prim, args)
            if plan is None:
                res.refused_type_incoherent += 1
                continue
            map_vals = self._exec_vals(plan, objective)
            res.candidates_evaluated += 1
            if map_vals is None:
                continue
            try:
                if tuple(self._value_key(v)
                         for v in map_vals) == exp_key:
                    mfrag = self._extend_frag(bfrag, map_prim, args, lam)
                    self._finish(objective, res, mfrag)
                    return True
            except Exception:
                pass
            mkey = ("map", bkey, lam.behavior_key,
                    tuple(self._value_key(v) for v in map_vals))
            if mkey not in banked:
                mfrag = self._extend_frag(bfrag, map_prim, args, lam)
                banked[mkey] = (mfrag, map_vals, map_prim.output)
                try:
                    res.nested_candidates.append(mkey)
                except Exception:
                    pass
            mkeys.append(mkey)
        # binary prims: 2-input, LIST-output, both inputs LIST-accepting,
        # AND pair-valued output (LIST of LIST) -- the nested N must be
        # pair-valued for the lean pair-lambda completion. This restricts
        # to zip-like prims, not append/concat.
        # GEN-SYNTH-5: combination half extracted to _nest_combine
        # (shared with the recursive step); depth 2 for this initial
        # nesting, no prune (preserves GEN-SYNTH-4 behavior exactly).
        return self._nest_combine(
            objective, res, banked, prims, bank, map_prim,
            cname, cspec, fn_name, mkeys, exp_key, 2,
            prune_ctx=None)

    def _complete_nested(self, objective, res, banked, prims, bank,
                         map_prim, cname, cspec, fn_name,
                         nfrag, out_vals, vkind, exp_key,
                         depth) -> bool:
        """Lean pair-lambda direct match over a nested binary result.

        GEN-SYNTH-4: for N = P(M, v), try map(N, Q) == goal for
        pair-lambdas Q, banking the scalar lists S; then the
        static-lambda map(S, L) == goal lookahead (the distilled-T
        path).

        GEN-SYNTH-5: depth is N's nesting depth (2 for the initial
        _nest_binary products). Two extensions:
        (a) filter fusion over the nested scalar lists S -- the mixed
            higher-order class -- via the existing _complete_filtered,
            gated on strict removal (sound: without strict removal the
            static path already covers fusion solutions);
        (b) recursive nesting: each banked S feeds back as a fresh M'
            for another binary combination at depth+1, gated by the
            sound zero-eval _nest_may_complete prune.
        Both are skipped at MAX_NEST_DEPTH (the prune's necessary
        condition covers only the direct/static paths). Returns True
        if the goal was reached.
        """
        # pair-valued check (LIST of LIST)
        try:
            if not (vkind.kind.name == "LIST" and vkind.args
                    and vkind.args[0].kind.name == "LIST"):
                return False
        except Exception:
            return False
        if not all(isinstance(v, (list, tuple)) for v in out_vals):
            return False
        # Guard: pair-lambdas probe JSON-scalar pairs. A nested result
        # whose examples are not lists of 2-element scalar pairs (e.g.
        # bytearray from an exotic binary prim) cannot be completed by
        # pair-lambdas; skip soundly rather than crashing the probe.
        def _scalar(x):
            return isinstance(x, (int, float, str, bool)) or x is None
        if not all(all(isinstance(p, (list, tuple)) and len(p) == 2
                       and all(_scalar(e) for e in p) for p in v)
                   for v in out_vals):
            return False
        pair_lams = [lam for lam in self._pair_lambdas(out_vals, prims)
                     if lam.output_kind.kind.name != "LIST"
                     and self._gs8_pair_lam_kind_ok(lam, objective)]
        if not pair_lams:
            return False
        # NOTE: No first-element pre-filter here. The pre-filter is
        # sound for direct map(N,Q)==goal, but the static-lambda path
        # needs Q's that produce an intermediate S (not directly the
        # goal). Filtering would prune the Q's the static completion
        # needs. The Q-loop evals are required anyway to bank S.
        try:
            if not cspec.accepts(vkind):
                return False
        except Exception:
            return False
        # GEN-SYNTH-5: shared context for the recursion prune and the
        # filter-fusion call. static_lams mirrors the per-lam static
        # loop below (same bank query); static_ops pre-extracts the
        # (op, args, input-spec) triples so the zero-eval prune does no
        # per-candidate ref parsing.
        _prim_by_name = {p.name: p for p in prims}
        _pair_prims = self._pair_prims(prims)
        try:
            _elem_kind = (map_prim.output.args[0]
                          if map_prim.output.args else None)
            _static_lams = bank.for_element_kind(_elem_kind)
        except Exception:
            _static_lams = []
        _static_ops = []
        for _slam in _static_lams:
            try:
                _p = _prim_by_name.get(
                    _slam.used[0] if _slam.used else "")
                if _p is None or len(_p.inputs) != 1:
                    continue
                _ispec = list(_p.inputs.values())[0]
                _s = _slam.ref["$lambda"]["steps"][0]
                _static_ops.append((_s["op"], _s["args"], _ispec))
            except Exception:
                continue
        _goal_first_key = None
        _goal_second_key = None
        try:
            _g0 = objective.examples[0][1]
            if isinstance(_g0, (list, tuple)) and _g0:
                _goal_first_key = self._value_key(_g0[0])
                # GEN-SYNTH-5-REPAIR: second-element key for the
                # strengthened two-element recursion gate. Still a
                # necessary condition (full match implies first-two
                # match), hence sound, but strictly more discriminating
                # than the first-element-only check.
                if len(_g0) > 1:
                    _goal_second_key = self._value_key(_g0[1])
        except Exception:
            pass
        try:
            _map_inputs = list(map_prim.inputs.items())
        except Exception:
            _map_inputs = None
        # GEN-SYNTH-10: Q-filter setup. At depth == _effective_cap - 1,
        # S=map(N',Q) becomes M' at the final depth where only direct/
        # static completion is possible (no deeper recursion). Before
        # the expensive _exec_vals for each Q, probe s0=Q(N'[0])
        # (zero-eval) and check if there exists a param v3 such that
        # the 1-step _nest_may_complete gate passes for (S[0], v3[0]).
        # If no v3 passes, Q cannot lead to a solution -- skip the eval.
        # Sound: if S were on the solution path, the solution's v3*
        # would witness the existential (the gate is sound, never
        # pruning a viable recursion). This is 1-step lookahead, not
        # the infeasible 2-step gate. Gated to 4-param goals
        # (len(params) > MAX_NEST_DEPTH); <=3-param goals keep their
        # proven trajectory untouched. Fail open on any setup problem.
        _gs10_active = False
        _gs10_ecap = self.MAX_NEST_DEPTH
        _gs10_prim_iname = {}
        _gs10_pkeys = []
        try:
            if len(objective.params) > self.MAX_NEST_DEPTH:
                _gs10_active = True
                _gs10_ecap = self.MAX_NEST_DEPTH + 1
                for _pp_prim, _pp_iname in _pair_prims:
                    try:
                        _gs10_prim_iname[_pp_prim.name] = _pp_iname
                    except Exception:
                        pass
                _gs10_pkeys = [k for k in banked
                               if isinstance(k, tuple) and k
                               and k[0] == "param"]
        except Exception:
            _gs10_active = False
            _gs10_prim_iname = {}
            _gs10_pkeys = []
        # Banked S keys in pair-lambda order: the recursion's M' pool.
        skeys = []
        for lam in pair_lams:
            # GEN-SYNTH-10: Q-filter. At depth == _ecap - 1, check
            # whether this Q could lead to a solution before the
            # expensive eval. Probe s0=Q(N'[0]) cheaply (zero-eval);
            # if no param v3 makes the 1-step gate pass for
            # (s0, v3[0]), skip this Q. Fail open on any probe or
            # data problem (never prune unsoundly).
            if _gs10_active and depth == _gs10_ecap - 1:
                try:
                    res.__dict__["gs10_q_considered"] = (
                        res.__dict__.get("gs10_q_considered", 0) + 1)
                except Exception:
                    pass
                _gs10_keep = True  # fail-open default
                try:
                    _gs10_pn = (lam.used[0] if lam.used else None)
                    _gs10_in = _gs10_prim_iname.get(_gs10_pn)
                    if _gs10_in is not None and out_vals:
                        _gs10_s0 = self._raw_probe_op(
                            _gs10_pn, {_gs10_in: {"$var": "x"}},
                            out_vals[0])
                        if _gs10_s0 is not None:
                            _gs10_s1 = None
                            try:
                                if len(out_vals) > 1:
                                    _gs10_s1 = self._raw_probe_op(
                                        _gs10_pn,
                                        {_gs10_in: {"$var": "x"}},
                                        out_vals[1])
                            except Exception:
                                _gs10_s1 = None
                            _gs10_keep = False
                            for _gs10_pk in _gs10_pkeys:
                                try:
                                    _g10_pf, _g10_pv, _g10_pn2 = (
                                        banked[_gs10_pk])
                                    _g10_v0 = (
                                        _g10_pv[0][0]
                                        if _g10_pv and _g10_pv[0]
                                        else None)
                                    _g10_v1 = (
                                        _g10_pv[0][1]
                                        if _g10_pv and _g10_pv[0]
                                        and len(_g10_pv[0]) > 1
                                        else None)
                                except Exception:
                                    continue
                                if self._nest_may_complete(
                                        _gs10_s0, _g10_v0,
                                        _gs10_s1, _g10_v1,
                                        _pair_prims, _static_ops,
                                        _goal_first_key,
                                        _goal_second_key):
                                    _gs10_keep = True
                                    break
                except Exception:
                    _gs10_keep = True
                if not _gs10_keep:
                    try:
                        res.__dict__["gs10_q_pruned"] = (
                            res.__dict__.get("gs10_q_pruned", 0) + 1)
                    except Exception:
                        pass
                    continue
            if res.candidates_evaluated >= objective.max_candidates:
                res.search_exhausted = True
                return False
            args = {cname: nfrag.out_ref, fn_name: lam.ref}
            plan = self._plan_from_frag(objective, nfrag, map_prim, args)
            if plan is None:
                res.refused_type_incoherent += 1
                continue
            map_vals = self._exec_vals(plan, objective)
            res.candidates_evaluated += 1
            if map_vals is None:
                continue
            try:
                if tuple(self._value_key(v)
                         for v in map_vals) == exp_key:
                    mfrag = self._extend_frag(nfrag, map_prim, args, lam)
                    self._finish(objective, res, mfrag)
                    return True
            except Exception:
                continue
            mkey = ("map-nested", id(nfrag), lam.behavior_key,
                    tuple(self._value_key(v) for v in map_vals))
            if mkey not in banked:
                mfrag = self._extend_frag(nfrag, map_prim, args, lam)
                banked[mkey] = (mfrag, map_vals, map_prim.output)
            else:
                mfrag = banked[mkey][0]
            # GEN-SYNTH-5: the recursion's M' pool, in pair-lambda order.
            # GEN-SYNTH-9: filter skeys by param count when the exact-
            # count prune is active. S will be M' at depth+1; the prune
            # there requires |params| == depth. Skip S that would be
            # pruned anyway (saves iteration, not just evals). Sound:
            # equivalent to the prune firing at the next level.
            _gs9_skeep = True
            try:
                if (len(objective.params) > self.MAX_NEST_DEPTH):
                    _s_pset = getattr(mfrag, "param_set", None)
                    if _s_pset is not None and len(_s_pset) != depth:
                        _gs9_skeep = False
            except Exception:
                pass
            if _gs9_skeep:
                skeys.append(mkey)
            # GEN-SYNTH-4: static-lambda completion over the nested
            # scalar list S = map(N, Q). Tries map(S, L) == goal for
            # static L (including distilled T), with the sound
            # first-element pre-filter. This is what lets a distilled
            # source technique remain causally necessary on nested
            # tasks.
            try:
                s_first = map_vals[0][0] if map_vals and map_vals[0] \
                    else None
            except Exception:
                continue
            if s_first is None:
                continue
            # Static lambdas for S's element kind (Q's output kind).
            try:
                elem_kind = map_prim.output.args[0] if \
                    map_prim.output.args else None
                static_lams = bank.for_element_kind(elem_kind)
            except Exception:
                continue
            s_ek = lam.output_kind
            prim_by_name = {p.name: p for p in prims}
            for slam in static_lams:
                try:
                    p = prim_by_name.get(
                        slam.used[0] if slam.used else "")
                    if p is None or len(p.inputs) != 1:
                        continue
                    ispec = list(p.inputs.values())[0]
                    if not ispec.accepts(s_ek):
                        continue
                except Exception:
                    continue
                if self._probe_first(slam, s_first) != \
                        self._value_key(objective.examples[0][1][0]):
                    continue
                if res.candidates_evaluated >= objective.max_candidates:
                    res.search_exhausted = True
                    return False
                sargs = {cname: mfrag.out_ref, fn_name: slam.ref}
                splan = self._plan_from_frag(
                    objective, mfrag, map_prim, sargs)
                if splan is None:
                    res.refused_type_incoherent += 1
                    continue
                svals = self._exec_vals(splan, objective)
                res.candidates_evaluated += 1
                if svals is None:
                    continue
                try:
                    if tuple(self._value_key(v)
                             for v in svals) == exp_key:
                        sfrag = self._extend_frag(
                            mfrag, map_prim, sargs, slam)
                        self._finish(objective, res, sfrag)
                        return True
                except Exception:
                    continue
            # GEN-SYNTH-5 (a): filter fusion over the nested scalar
            # list S -- the mixed higher-order class. Gated on strict
            # removal (sound: without it the static path above already
            # covers fusion solutions). Skipped at MAX_NEST_DEPTH so
            # the recursion prune's necessary condition stays sound.
            if (depth < self.MAX_NEST_DEPTH and _map_inputs is not None
                    and _goal_first_key is not None
                    and self._strict_removal(map_vals,
                                             objective.examples)):
                if self._complete_filtered(
                        objective, res, banked, prims, bank,
                        map_prim, _map_inputs, mfrag, map_vals,
                        exp_key, _goal_first_key):
                    return True
        # GEN-SYNTH-5 (b): recursive nesting. Feed each banked S back as
        # a fresh M' for another binary combination, completing the new
        # nested N' at depth+1. The sound zero-eval _nest_may_complete
        # prune gates (M', v) pairs so the recursion does not multiply
        # the budget; _nest_combine shares the combination machinery
        # with the initial GEN-SYNTH-4 nesting.
        # Skip recursion when the objective has no more params than the
        # current depth (each nesting level adds at most one new param;
        # deeper nesting cannot introduce new information). Sound: for
        # a 2-param goal at depth 2, recursion is wasted work.
        # GEN-SYNTH-7: arity-gated depth extension. MAX_NEST_DEPTH bounds
        # fan-out, but when the objective has MORE params than the cap,
        # deeper nesting can still introduce new information -- the dual
        # of the skip principle above (each level adds at most one new
        # param, so a 4-param goal needs depth 4). Allow exactly one
        # level beyond MAX_NEST_DEPTH in that case, still gated by the
        # sound _nest_may_complete prune. This is not a silent budget
        # increase: goals with <= MAX_NEST_DEPTH params see identical
        # behavior to before, and depth remains hard-capped (at 4, not
        # unbounded). A 5-param goal still fails honestly at depth 4 --
        # that is the next named boundary, not this mission.
        _effective_cap = self.MAX_NEST_DEPTH + (
            1 if len(objective.params) > self.MAX_NEST_DEPTH else 0)
        if depth >= _effective_cap or not skeys:
            return False
        # GEN-SYNTH-8: pass _effective_cap in prune_ctx. The
        # _nest_may_complete gate checks if N' can complete via the
        # direct/static path at its depth. But when depth < _effective_cap,
        # N' can also complete via recursion to a deeper level, so the
        # gate is unsound (it prunes viable recursions). The gate must
        # fail open when depth < _effective_cap. For <=3-param goals,
        # _effective_cap == MAX_NEST_DEPTH, so behavior is identical to
        # before (gate applies at the max depth).
        prune_ctx = (_pair_prims, _static_ops, _goal_first_key,
                     _goal_second_key, _effective_cap)
        return self._nest_combine(
            objective, res, banked, prims, bank, map_prim,
            cname, cspec, fn_name, skeys, exp_key, depth + 1,
            prune_ctx=prune_ctx)

    def _probe_first(self, lam: Any, elem: Any) -> Any:
        """Execute a single-prim lambda on one element (value key).

        Sound pre-filter for the completion head: None if the lambda
        cannot be probed (then the caller tries the full plan).

        GEN-SYNTH-5-REPAIR: probes via _probe_invoke
        (PrimitiveRegistry.invoke -- the same call real per-step
        execution makes), so wrapper-equivalence holds by construction.
        The direct prim.fn fast path is removed.
        """
        try:
            steps = lam.ref["$lambda"]["steps"]
            if len(steps) != 1:
                return None
            s = steps[0]
            args = {}
            for k, v in s["args"].items():
                if isinstance(v, dict) and v.get("$var"):
                    args[k] = elem
                else:
                    args[k] = v
            ok, value = self._probe_invoke(s["op"], args)
            if not ok:
                return None
            return self._value_key(value)
        except Exception:
            return None

    def _gs8_pair_lam_kind_ok(self, lam: Any, objective: Any) -> bool:
        """GEN-SYNTH-8: sound output-kind filter for pair-lambdas.

        For a LIST(E) goal, the nesting chain's Q's must output a kind
        compatible with E. The chain is: B (pairs) -> M=map(B,Q) ->
        N=zip(M,v) -> ... -> S=map(N'',Q3) -> L(S)=goal. For the final
        L(S) to have type LIST(E), S must be LIST(E') where E' is
        compatible with L's input. The static L's for numeric goals are
        NUM->NUM (e.g. distilled T), so S must be LIST(NUM), hence every
        Q in the chain must output NUM (by backward induction).

        This prunes Q's like 'all' (BOOL), 'as_bytearray' (bytes),
        'classify_shape' (STR) for numeric goals -- they are never on an
        arithmetic solution path. Sound: a solution requiring a
        non-compatible Q would need a type-converting step absent from
        the nesting machinery; the filter only removes Q's that cannot
        participate in a well-typed chain to the goal.

        Compatibility: NUM accepts NUM/INT/FLOAT (numeric tower);
        otherwise exact kind-name match. Non-LIST goals or unknown kinds
        fail open (no filtering).

        GEN-SYNTH-11: ANY (unknown kind) must be allowed -- it is not
        provably incompatible. Blocking ANY pruned the viable
        filter-fusion plan (coalesce:ANY) and was unsound. Fail open.
        """
        try:
            if lam.output_kind.kind.name == "LIST":
                return False
            gk = objective.output_kind
            if gk.kind.name != "LIST" or not gk.args:
                return True
            ekind = gk.args[0].kind.name
            qkind = lam.output_kind.kind.name
            if ekind == "NUM":
                return qkind in ("NUM", "INT", "FLOAT", "ANY")
            return qkind == ekind or qkind == "ANY"
        except Exception:
            return True

    def _pair_lambdas(self, vals: tuple, prims: List[Any]) -> List[Any]:
        """Lambdas over pair elements: x -> Q(x) for 1-input prims Q.

        GEN-SYNTH-2: the static _LambdaBank probes lambdas on NUM/STR
        domains, so a lambda like x -> sum(x) over zip-pairs can never
        survive its probe (sum of a number fails). These are built lazily
        from the ACTUAL pair values of a banked collection, probed on
        those pairs, and deduplicated by observed behavior -- the same
        discipline as the bank. Q is drawn from the forbidden-filtered
        pool, so the GEN-XDOM-1 repair holds for this path too.
        Results are cached per compose() call.
        """
        cache = getattr(self, "_pair_lam_cache", None)
        if cache is None:
            cache = {}
            self._pair_lam_cache = cache
        ckey = tuple(sorted(set(
            _LambdaBank._val_key(p) for v in vals
            for p in (v if isinstance(v, (list, tuple)) else []))))
        if ckey in cache:
            return cache[ckey]
        pairs = []
        seen = set()
        for v in vals:
            if not isinstance(v, (list, tuple)):
                continue
            for p in v:
                if not isinstance(p, (list, tuple)):
                    continue
                pk = _LambdaBank._val_key(p)
                if pk not in seen:
                    seen.add(pk)
                    pairs.append(p)
        out: List[Any] = []
        if pairs:
            seen_behaviors: Dict[tuple, bool] = {}
            # GEN-SYNTH-5: prim selection shared with the recursion
            # prune via _pair_prims.
            for prim, iname in self._pair_prims(prims):
                ref = {"$lambda": {
                    "params": ["x"],
                    "steps": [{"id": "t1", "op": prim.name,
                               "args": {iname: {"$var": "x"}}}],
                    "output": {"$step": "t1"},
                }}
                key = self._probe_lambda(prim, ref, pairs)
                if key is None or key in seen_behaviors:
                    continue
                seen_behaviors[key] = True
                out.append(_LambdaEntry(
                    ref=ref, output_kind=prim.output,
                    used=[prim.name], behavior_key=key))
        cache[ckey] = out
        # GEN-SYNTH-4: stash the distinct probe pairs behind this set so
        # the sound first-element pre-filter can map behaviors back to
        # pairs with zero new evaluations.
        try:
            if getattr(self, "_pair_lam_pairs", None) is None:
                self._pair_lam_pairs = {}
            self._pair_lam_pairs[ckey] = pairs
        except Exception:
            pass
        return out

    def _pair_lams_first_filtered(self, pair_lams: List[Any],
                                    out_vals: tuple,
                                    objective: CompositionObjective
                                    ) -> List[Any]:
        """Sound first-element pre-filter for pair-lambdas (GEN-SYNTH-4).

        For map(Z, Q) to equal the goal, Q must map each example's first
        pair to that example's first goal element. The probe behaviors
        cached by _pair_lambdas already record Q's output on every
        distinct pair, so this check costs zero new evaluations. Sound:
        a Q that could match is never pruned -- the condition is
        necessary, not heuristic. Only sharpens the economics.
        """
        try:
            pairs = (getattr(self, "_pair_lam_pairs", None) or {}).get(
                tuple(sorted(set(
                    _LambdaBank._val_key(p) for v in out_vals
                    for p in (v if isinstance(v, (list, tuple))
                              else [])))))
            if not pairs:
                return pair_lams
            pidx: Dict[Any, int] = {}
            for i, p in enumerate(pairs):
                k = _LambdaBank._val_key(p)
                if k not in pidx:
                    pidx[k] = i
            goal_firsts = []
            for _, g in objective.examples:
                if not isinstance(g, (list, tuple)) or not g:
                    return pair_lams
                goal_firsts.append(_LambdaBank._val_key(g[0]))
            out = []
            for lam in pair_lams:
                bk = lam.behavior_key
                ok = True
                for e, v in enumerate(out_vals):
                    if not isinstance(v, (list, tuple)) or not v:
                        ok = False
                        break
                    i = pidx.get(_LambdaBank._val_key(v[0]))
                    if i is None or i >= len(bk):
                        ok = False
                        break
                    if bk[i] != goal_firsts[e]:
                        ok = False
                        break
                if ok:
                    out.append(lam)
            return out
        except Exception:
            return pair_lams

    def _probe_lambda(self, prim: Any, ref: Dict[str, Any],
                      pairs: list) -> Optional[tuple]:
        """Behavior key of a pair-lambda on actual pair values.

        Mirrors _LambdaBank._behavior_key, but probes on the banked
        pairs instead of the static NUM/STR domains. None if any
        application fails.
        """
        import json as _json
        outs = []
        for p in pairs:
            steps = []
            for i, s in enumerate(ref["$lambda"]["steps"]):
                steps.append({**s, "id": f"q{i}"})
            probe = {"name": "pair_probe", "params": {},
                     "steps": steps, "output": {"$step": "q0"}}
            s = _json.dumps(probe).replace('{"$var": "x"}',
                                           _json.dumps(p))
            try:
                r = self._composer.execute_sync(_json.loads(s), {})
            except Exception:
                return None
            if not r.get("success"):
                return None
            outs.append(_LambdaBank._val_key(r.get("value")))
        return tuple(outs)

    def _try_head_unary(self, objective, res, banked, prim,
                        input_spec, exp_key) -> bool:
        """Try 1-input prim as head over banked values. True if found."""
        iname, ispec = input_spec
        # Prefer param-using, lambda-built (map/filter) values: they carry
        # transformed structure a head like sum/product can reduce.
        def vsort(kv):
            frag = kv[1][0]
            return (not frag.uses_param,
                    -len(frag.used),
                    str(kv[0]))
        items = sorted(banked.items(), key=vsort)
        for key, (frag, vals, _p) in items:
            _, _, vkind = banked[key]
            try:
                if not ispec.accepts(vkind):
                    continue
            except Exception:
                continue
            if res.candidates_evaluated >= objective.max_candidates:
                res.search_exhausted = True
                return False
            plan = self._plan_from_frag(objective, frag, prim,
                                        {iname: frag.out_ref})
            if plan is None:
                res.refused_type_incoherent += 1
                continue
            out_vals = self._exec_vals(plan, objective)
            res.candidates_evaluated += 1
            if out_vals is None:
                continue
            try:
                if tuple(self._value_key(v) for v in out_vals) == exp_key:
                    nfrag = self._extend_frag(
                        frag, prim, {iname: frag.out_ref}, None)
                    self._finish(objective, res, nfrag)
                    return True
            except Exception:
                continue
        return False

    def _try_head_higher(self, objective, res, banked, prim,
                         inputs, bank, exp_key, prims) -> bool:
        """Try (collection, fn) prim as head. True if found."""
        coll = [(n, s) for n, s in inputs
                if s.kind is not Kind.CALLABLE][0]
        fn_name = [n for n, s in inputs
                   if s.kind is Kind.CALLABLE][0]
        cname, cspec = coll
        elem_kind = None
        try:
            if (cspec.kind.name == "LIST"
                    and prim.output.kind.name == "LIST"
                    and prim.output.args):
                elem_kind = prim.output.args[0]
        except Exception:
            pass
        lambdas = bank.for_element_kind(elem_kind)

        def _pair_elem_kind(vkind: Any) -> bool:
            try:
                return (vkind.kind.name == "LIST" and vkind.args
                        and vkind.args[0].kind.name == "LIST")
            except Exception:
                return False

        def vsort(kv):
            frag = kv[1][0]
            return (not frag.uses_param,
                    -len(frag.used),
                    str(kv[0]))
        items = sorted(banked.items(), key=vsort)
        for key, (frag, vals, _p) in items:
            _, _, vkind = banked[key]
            try:
                if not cspec.accepts(vkind):
                    continue
            except Exception:
                continue
            if not all(isinstance(v, (list, tuple)) for v in vals):
                continue
            # GEN-SYNTH-2: pair-element lambdas for banked pair
            # collections (see _bank_higher).
            use_lambdas = lambdas
            if _pair_elem_kind(vkind):
                pair_lams = self._pair_lambdas(vals, prims)
                if pair_lams:
                    use_lambdas = list(lambdas) + pair_lams
            for lam in use_lambdas:
                if res.candidates_evaluated >= objective.max_candidates:
                    res.search_exhausted = True
                    return False
                args = {cname: frag.out_ref, fn_name: lam.ref}
                plan = self._plan_from_frag(objective, frag, prim, args)
                if plan is None:
                    res.refused_type_incoherent += 1
                    continue
                out_vals = self._exec_vals(plan, objective)
                res.candidates_evaluated += 1
                if out_vals is None:
                    continue
                try:
                    if tuple(self._value_key(v)
                             for v in out_vals) == exp_key:
                        nfrag = self._extend_frag(frag, prim, args, lam)
                        self._finish(objective, res, nfrag)
                        return True
                except Exception:
                    continue
        return False

    def _plan_from_frags(self, objective: CompositionObjective,
                         frags: List[_Fragment], prim: Any,
                         args: Dict[str, Any]) -> Any:
        """Full plan = merged frag steps + prim applied. None if analyze()
        refuses it (type-incoherent: counted by the caller). GEN-SYNTH-1:
        generalizes the single-fragment path to binary heads/banking."""
        self._frag_seq += 1
        head_id = f"__f_{self._frag_seq}__"
        steps: List[Dict[str, Any]] = []
        for f in frags:
            steps.extend(f.steps)
        steps.append({"id": head_id, "op": prim.name,
                      "args": dict(args)})
        plan = {"version": 1, "name": f"composed_{objective.gap_id[:8]}",
                "params": {k: str(v.kind.value)
                           for k, v in objective.params.items()},
                "steps": steps, "output": {"$step": head_id}}
        try:
            analysis = self._composer.analyze(plan)
        except Exception:
            return None
        if not analysis.ok:
            return None
        return plan

    def _plan_from_frag(self, objective: CompositionObjective,
                        frag: _Fragment, prim: Any,
                        args: Dict[str, Any]) -> Any:
        """Full plan = frag's steps + prim applied. None if analyze()
        refuses it (type-incoherent: counted by the caller)."""
        return self._plan_from_frags(objective, [frag], prim, args)

    def _exec_vals(self, plan: Any,
                   objective: CompositionObjective) -> Any:
        """Outputs on all examples. None if any execution fails."""
        out = []
        for inp, _ in objective.examples:
            try:
                r = self._composer.execute_sync(plan, dict(inp))
            except Exception:
                return None
            if not r.get("success", False):
                return None
            out.append(r.get("value"))
        return tuple(out)

    def _extend_frags(self, frags: List[_Fragment], prim: Any,
                      args: Dict[str, Any], lam: Any) -> _Fragment:
        """GEN-SYNTH-1: generalizes _extend_frag to binary application."""
        self._frag_seq += 1
        head_id = f"__f_{self._frag_seq}__"
        steps: List[Dict[str, Any]] = []
        for f in frags:
            steps.extend(f.steps)
        steps.append({"id": head_id, "op": prim.name,
                      "args": dict(args)})
        # Rebind: replace the arg refs that pointed at frag.out_ref with
        # the new head where they were the fragment output. The args dict
        # already holds frag.out_ref for the collection input; that ref
        # is correct as-is (it points into frag.steps).
        used: List[str] = []
        for f in frags:
            for u in f.used:
                if u not in used:
                    used.append(u)
        if lam is not None:
            for u in lam.used:
                if u not in used:
                    used.append(u)
        used.append(prim.name)
        uses_param = any(f.uses_param for f in frags)
        # GEN-SYNTH-9: union of input param sets (getattr for fragments
        # constructed before param_set existed).
        param_set = frozenset().union(
            *(getattr(f, "param_set", frozenset()) for f in frags))
        return _Fragment(steps, {"$step": head_id}, used,
                         uses_param=uses_param, param_set=param_set)

    def _extend_frag(self, frag: _Fragment, prim: Any,
                     args: Dict[str, Any], lam: Any) -> _Fragment:
        return self._extend_frags([frag], prim, args, lam)

    def _finish(self, objective: CompositionObjective,
                res: ComposeResult, frag: _Fragment) -> ComposeResult:
        plan = {"version": 1, "name": f"composed_{objective.gap_id[:8]}",
                "params": {k: str(v.kind.value)
                           for k, v in objective.params.items()},
                "steps": frag.steps, "output": frag.out_ref}
        try:
            analysis = self._composer.analyze(plan)
        except Exception:
            res.refused_type_incoherent += 1
            return res
        if not analysis.ok:
            res.refused_type_incoherent += 1
            return res
        res.plan = plan
        res.composed_of = list(frag.used)
        res.examples_passed = len(objective.examples)
        res.evidence = {
            "examples": [
                {"input": inp, "expected": exp}
                for inp, exp in objective.examples
            ],
            "search": "forward-bottom-up-behavior-banked",
        }
        return res

    # -- candidate primitives ---------------------------------------------

    def _candidate_prims(self, objective: CompositionObjective) -> List[Any]:
        prims = []
        forbidden = set(objective.forbidden or ())
        for name in sorted(self._reg._prims.keys()):
            if name in forbidden:
                # GEN-XDOM-1 repair: the forbidden set is the causal-
                # contrast's exclusion list. It must be honored by EVERY
                # consumer of the candidate pool -- previously the lambda
                # bank was built from the unfiltered pool, so a forbidden
                # (distilled) technique leaked back in as a bank lambda and
                # the contrast was vacuous for any objective with list
                # params (exactly the cross-domain collection case). The
                # bank, the forward loop, and the head loop all draw from
                # this list, so filtering here is the single repair point.
                continue
            p = self._reg._prims[name]
            if objective.pure_only and tuple(p.effects) != (Effect.PURE,):
                continue
            prims.append(p)
        # Simpler heads first (fewer inputs): Occam over the search order.
        # Deterministic; the search still does not know the answer.
        prims.sort(key=lambda p: (len(p.inputs), p.name))
        return prims

    @staticmethod
    def _uses_param(plan: Dict[str, Any]) -> bool:
        found = []

        def walk(v: Any) -> None:
            if isinstance(v, dict):
                if "$param" in v:
                    found.append(True)
                for x in v.values():
                    walk(x)
            elif isinstance(v, list):
                for x in v:
                    walk(x)

        walk(plan)
        return bool(found)

    # -- literals ----------------------------------------------------------

    def _literals_from_examples(
            self, objective: CompositionObjective) -> List[Any]:
        """Scalar literals drawn ONLY from the objective's example values."""
        lits: List[Any] = []

        def harvest(v: Any) -> None:
            if isinstance(v, bool):
                return
            if isinstance(v, (int, float)) and abs(v) < 10 ** 6:
                if v not in lits:
                    lits.append(v)
            elif isinstance(v, str) and len(v) <= 64:
                if v not in lits:
                    lits.append(v)
            elif isinstance(v, (list, tuple)):
                for x in v:
                    harvest(x)
            elif isinstance(v, dict):
                for x in v.values():
                    harvest(x)

        for args, expected in objective.examples:
            harvest(args)
            harvest(expected)
        return lits

    # -- backward search, by size ------------------------------------------

    def _frags_size(self, kind: TypeSpec, objective: CompositionObjective,
                   size: int, prims: List[Any], bank: "_LambdaBank",
                   seen: frozenset, need_param: bool) -> Iterator[_Fragment]:
        """Fragments of exactly `size` primitive applications producing kind.

        need_param=True: only fragments referencing a plan param. Sound when
        the objective's expected outputs vary: a param-free fragment of
        deterministic primitives is constant and cannot match. Enforced at
        GENERATION time, so constant plans are never even built.
        """
        if size == 0:
            for pname, pkind in objective.params.items():
                try:
                    if kind.accepts(pkind):
                        yield _Fragment([], {"$param": pname}, [],
                                        uses_param=True,
                                        param_set=frozenset({pname}))
                except Exception:
                    continue
            if not need_param:
                for lit in self._literals:
                    try:
                        if kind.accepts(infer(lit)):
                            yield _Fragment([], lit, [])
                    except Exception:
                        continue
            return
        # The element kind a higher-order primitive must produce, when the
        # goal kind is a concrete LIST(E). Lets CALLABLE holes draw only
        # lambdas whose output can serve as elements -- type-directed, not
        # a guess about any particular task.
        elem_kind = (kind.args[0] if kind.kind.name == "LIST" and kind.args
                     else None)
        for prim in prims:
            if prim.name in seen:
                continue  # no direct self-recursion
            try:
                if not kind.accepts(prim.output):
                    continue
            except Exception:
                continue
            inputs = list(prim.inputs.items())
            for sizes in self._compositions(size - 1, len(inputs)):
                arg_variants: List[Tuple[str, List[_Fragment]]] = []
                ok = True
                for (iname, ispec), sub in zip(inputs, sizes):
                    if ispec.kind is Kind.CALLABLE:
                        # A CALLABLE hole draws from the bank; the lambda's
                        # own applications were counted at bank build time.
                        # Bank lambdas don't reference plan params, so they
                        # never satisfy need_param -- the other args must.
                        alts = bank.for_element_kind(elem_kind)
                    else:
                        # Each arg may or may not carry the param; at least
                        # one must when need_param is set. Generate both
                        # variants and filter combinations below.
                        alts = list(self._frags_size(
                            ispec, objective, sub, prims, bank,
                            seen | {prim.name}, False))
                        if need_param:
                            alts_need = [a for a in alts if a.uses_param]
                            # (keep the full list too: the need can be met
                            # by a SIBLING arg; filtered at combination time)
                            alts = alts_need + [a for a in alts
                                                if not a.uses_param]
                    if not alts:
                        ok = False
                        break
                    arg_variants.append((iname, alts))
                if not ok:
                    continue
                for combo in itertools.product(
                        *[alts for _, alts in arg_variants]):
                    frags = list(combo)
                    # need_param: at least one non-lambda arg uses a param.
                    if need_param and not any(
                            getattr(f, "uses_param", False) for f in frags):
                        continue
                    steps: List[Dict[str, Any]] = []
                    args: Dict[str, Any] = {}
                    used: List[str] = []
                    uses_p = False
                    # GEN-SYNTH-9: track param names for the param-count
                    # prune. _LambdaEntry has no params (opaque body).
                    _pset = frozenset()
                    for (iname, _), frag in zip(arg_variants, frags):
                        if isinstance(frag, _LambdaEntry):
                            args[iname] = frag.ref
                            # lambda bodies live inside the $lambda ref, not
                            # as plan steps; their prims count as used.
                            for u in frag.used:
                                if u not in used:
                                    used.append(u)
                            continue
                        steps.extend(frag.steps)
                        args[iname] = frag.out_ref
                        for u in frag.used:
                            if u not in used:
                                used.append(u)
                        uses_p = uses_p or frag.uses_param
                        _pset = _pset | getattr(frag, "param_set",
                                               frozenset())
                    self._frag_seq += 1
                    head_id = f"__head_{self._frag_seq}__"
                    steps.append({"id": head_id, "op": prim.name,
                                  "args": args})
                    used.append(prim.name)
                    yield _Fragment(steps, {"$step": head_id}, used,
                                    uses_param=uses_p, param_set=_pset)

    @staticmethod
    def _compositions(n: int, k: int) -> Iterator[Tuple[int, ...]]:
        """Weak compositions of n into k parts (order matters)."""
        if k == 1:
            yield (n,)
            return
        for first in range(n + 1):
            for rest in PlanComposer._compositions(n - first, k - 1):
                yield (first,) + rest

    # -- assembly -----------------------------------------------------------

    def _assemble_plan(self, objective: CompositionObjective,
                       frag: _Fragment) -> Dict[str, Any]:
        id_map: Dict[str, str] = {}
        counter = [0]

        def fresh() -> str:
            counter[0] += 1
            return f"s{counter[0]}"

        steps: List[Dict[str, Any]] = []
        for st in frag.steps:
            new_id = fresh()
            id_map[st["id"]] = new_id
            steps.append({**st, "id": new_id})

        def rewrite(ref: Any) -> Any:
            if isinstance(ref, dict):
                if "$step" in ref and ref["$step"] in id_map:
                    return {"$step": id_map[ref["$step"]]}
                return {k: rewrite(v) for k, v in ref.items()}
            if isinstance(ref, list):
                return [rewrite(v) for v in ref]
            return ref

        steps = [dict(s, args=rewrite(s.get("args", {}))) for s in steps]
        out_ref = rewrite(frag.out_ref)
        params = {k: str(v.kind.value) for k, v in objective.params.items()}
        return {"name": f"composed_{objective.gap_id[:8]}",
                "params": params,
                "steps": steps,
                "output": out_ref}

    # -- evaluation ----------------------------------------------------------

    def _run_examples(self, plan: Dict[str, Any],
                      objective: CompositionObjective) -> Optional[list]:
        outputs = []
        for args, _ in objective.examples:
            try:
                r = self._composer.execute_sync(plan, dict(args))
            except Exception:
                return None
            if not r.get("success"):
                return None
            outputs.append(r.get("value"))
        return outputs

    @staticmethod
    def _plan_key(plan: Dict[str, Any]) -> str:
        return json.dumps(plan, sort_keys=True, default=str)

    @staticmethod
    def _output_key(outputs: list) -> tuple:
        key = []
        for v in outputs:
            try:
                key.append(round(float(v), 9)
                           if isinstance(v, (int, float))
                           and not isinstance(v, bool) else repr(v))
            except Exception:
                key.append(repr(v))
        return tuple(key)

    def _count_matches(self, outputs: list,
                       objective: CompositionObjective) -> int:
        n = 0
        for out, (_, expected) in zip(outputs, objective.examples):
            if self._matches(out, expected, objective.float_tol):
                n += 1
        return n

    @staticmethod
    def _matches(out: Any, expected: Any, tol: float) -> bool:
        if isinstance(expected, float) or isinstance(out, float):
            try:
                return math.isclose(float(out), float(expected),
                                    rel_tol=tol, abs_tol=tol)
            except (TypeError, ValueError):
                return False
        return out == expected


# ---------------------------------------------------------------------------
# lambda bank
# ---------------------------------------------------------------------------

class _LambdaBank:
    """Single-primitive lambdas, executed on the example elements and
    deduplicated by observed behavior.

    Built once per composition: enumerate lambdas ``x -> Q(x-or-literal,
    ...)`` for every pure primitive Q, run each on the domain values (the
    elements of the objective's list params across examples), and keep one
    representative per distinct behavior tuple. A lambda hole in the outer
    search then draws from this bank -- real executed functions, not
    syntactic guesses.
    """

    def __init__(self, composer: Any, objective: CompositionObjective,
                 prims: List[Any], literals: List[Any]):
        self.entries: List[_LambdaEntry] = []
        domains = self._domains(objective)
        if not domains:
            return
        # Cap literals to keep the bank tractable: the first few distinct
        # example values suffice for behavior-distinction; more literals
        # only multiply observationally-redundant variants.
        literals = list(literals)[:6]
        seen_behaviors: Dict[tuple, bool] = {}
        for domain in _LAMBDA_DOMAINS:
            for prim in prims:
                if prim.output.kind is Kind.CALLABLE:
                    continue
                inputs = list(prim.inputs.items())
                for combo in itertools.product(
                        *[self._body_alts(ispec, domain, literals)
                          for _, ispec in inputs]):
                    if not combo:
                        continue
                    args = {iname: a for (iname, _), a in
                            zip(inputs, combo)}
                    ref = {"$lambda": {
                        "params": ["x"],
                        "steps": [{"id": "t1", "op": prim.name,
                                   "args": args}],
                        "output": {"$step": "t1"},
                    }}
                    key = self._behavior_key(composer, ref, domains)
                    if key is None or key in seen_behaviors:
                        continue
                    seen_behaviors[key] = True
                    self.entries.append(_LambdaEntry(
                        ref=ref, output_kind=prim.output,
                        used=[prim.name], behavior_key=key))

    @staticmethod
    def _domains(objective: CompositionObjective) -> List[list]:
        """Element values the lambdas will be applied to: the elements of
        every list-typed param across the training examples."""
        elems: List[Any] = []
        for args, _ in objective.examples:
            for pname, pkind in objective.params.items():
                if pkind.kind.name != "LIST":
                    continue
                vals = args.get(pname)
                if isinstance(vals, (list, tuple)):
                    for v in vals:
                        if v not in elems:
                            elems.append(v)
        return [elems] if elems else []

    @staticmethod
    def _body_alts(ispec: TypeSpec, domain: TypeSpec,
                   literals: List[Any]) -> List[Any]:
        alts: List[Any] = []
        try:
            if ispec.accepts(domain):
                alts.append({"$var": "x"})
        except Exception:
            pass
        for lit in literals:
            try:
                if ispec.accepts(infer(lit)):
                    alts.append(lit)
            except Exception:
                continue
        return alts

    @staticmethod
    def _behavior_key(composer: Any, ref: Dict[str, Any],
                      domains: List[list]) -> Optional[tuple]:
        """Execute the lambda body on the domain values with x bound to each
        value in turn; None if any application fails."""
        import json as _json
        lam = ref["$lambda"]
        outs = []
        for vals in domains:
            for v in vals:
                steps = []
                for i, s in enumerate(lam["steps"]):
                    steps.append({**s, "id": f"p{i}"})
                probe = {"name": "bank_probe", "params": {},
                         "steps": steps, "output": {"$step": "p0"}}
                # Bind x by rewriting its refs to the literal value.
                s = _json.dumps(probe).replace('{"$var": "x"}',
                                              _json.dumps(v))
                try:
                    r = composer.execute_sync(_json.loads(s), {})
                except Exception:
                    return None
                if not r.get("success"):
                    return None
                outs.append(_LambdaBank._val_key(r.get("value")))
        return tuple(outs)

    @staticmethod
    def _val_key(v: Any) -> Any:
        try:
            if isinstance(v, bool):
                return ("b", v)
            if isinstance(v, (int, float)):
                return ("n", round(float(v), 9))
            if isinstance(v, str):
                return ("s", v[:64])
        except Exception:
            pass
        return ("r", repr(v)[:64])

    def for_element_kind(self, elem_kind: Optional[TypeSpec]
                         ) -> List[_LambdaEntry]:
        """Bank lambdas whose output can serve as elements of kind elem_kind.
        elem_kind None (unknown) -> the whole bank; the type-checker and the
        examples still decide."""
        if elem_kind is None:
            return list(self.entries)
        out = []
        for e in self.entries:
            try:
                if elem_kind.accepts(e.output_kind):
                    out.append(e)
            except Exception:
                continue
        return out or list(self.entries)
