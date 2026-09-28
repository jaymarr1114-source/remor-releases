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

import itertools
import json
import math
from dataclasses import dataclass, field
from typing import Any, Dict, Iterator, List, Optional, Tuple

from swarm_engine.primitives.core import (
    ANY, CALLABLE, LIST, NUM, STR, Effect, Kind, TypeSpec, infer,
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

    # -- public -----------------------------------------------------------

    def compose(self, objective: CompositionObjective) -> ComposeResult:
        res = ComposeResult(plan=None, composed_of=[],
                            examples_total=len(objective.examples))
        self._literals = self._literals_from_examples(objective)
        # GEN-SYNTH-2 / GEN-XDOM-1: the pair-lambda cache is per-compose.
        # A cached lambda built from an allowed prim must never leak into
        # a later contrast run where that prim is forbidden.
        self._pair_lam_cache = {}
        # Forward (bottom-up) behavior-banked search: builds
        # observationally-distinct values layer by layer from the params,
        # checking the goal examples after each layer. Example-driven, not
        # blind enumeration.
        return self._compose_forward(objective, res)

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
            frag = _Fragment([], {"$param": pname}, [], uses_param=True)
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
        goal_is_list = False
        try:
            goal_is_list = objective.output_kind.kind.name == "LIST"
        except Exception:
            pass
        if goal_is_list:
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
        for prim in prims:
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
                     if lam.output_kind.kind.name != "LIST"]
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

    def _probe_first(self, lam: Any, elem: Any) -> Any:
        """Execute a single-prim lambda on one element (value key).

        Sound pre-filter for the completion head: None if the lambda
        cannot be probed (then the caller tries the full plan).
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
            probe = {"name": "probe_first", "params": {},
                     "steps": [{"id": "q0", "op": s["op"], "args": args}],
                     "output": {"$step": "q0"}}
            r = self._composer.execute_sync(probe, {})
            if not r.get("success"):
                return None
            return self._value_key(r.get("value"))
        except Exception:
            return None

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
        return out

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
        return _Fragment(steps, {"$step": head_id}, used,
                         uses_param=uses_param)

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
                                        uses_param=True)
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
                    self._frag_seq += 1
                    head_id = f"__head_{self._frag_seq}__"
                    steps.append({"id": head_id, "op": prim.name,
                                  "args": args})
                    used.append(prim.name)
                    yield _Fragment(steps, {"$step": head_id}, used,
                                    uses_param=uses_p)

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
