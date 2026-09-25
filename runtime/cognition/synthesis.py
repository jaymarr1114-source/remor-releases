"""
swarm_engine/cognition/synthesis.py

Compositional synthesis (E): construct plans as chains of existing primitives,
searched at runtime, rather than selected from a fixed list of ~60 hand-written
code strings.

Explicit scope, stated once here: this searches "single-chainable" primitives
only — pure effect, exactly one required input — so a candidate at depth N is
simply primitive_N(...primitive_1(x)). This excludes primitives needing a
synthesized predicate/comparator (filter, reduce with a custom fn) because
constructing those arguments is program synthesis of a different, harder
kind that this pass does not attempt. That is a named limitation, not an
oversight.

A candidate is only ever a Hypothesis (a plan dict) — never registered, never
trusted — until the caller runs it through the unchanged Composer.analyze /
AdmissionController.admit path.
"""
from __future__ import annotations

import time
import asyncio
import threading

# R16 execution-safety policy lives in swarm_engine.pow_safety (single
# authoritative implementation shared by the native `power` primitive and
# the probe pre-checks below).
from swarm_engine.pow_safety import (
    _POW_MAG_CAP,
    _UnsafeMagnitude,
    _ensure_pow_safe,
    _pow_safe_scalar,
)
from swarm_engine.services.run_control import checkpoint


def _is_numeric_input_type(_v):
    """True iff a primitive input type spec denotes a scalar number.

    Excludes containers (list[...], dict[..]): the value-vector probes
    feed scalar numbers, not collections. Shared by the probe
    binary-primitive filters (R13 parity): non-numeric prims are not
    meaningful for numeric vectors, and some (e.g. seeded_random with a
    huge n) OOM on large numeric inputs.
    """
    _t = str(_v).lower().strip()
    if _t.startswith("list") or _t.startswith("dict"):
        return False
    return ("num" in _t or _t in ("int", "float", "number", "integer"))


def _is_numeric_output_prim(_p):
    """True iff a primitive's output type is a scalar number (not a
    list/dict). The probes build scalar value vectors; a combining
    primitive must return a single number per scalar."""
    _t = str(getattr(_p, "output", "")).lower().strip()
    if _t.startswith("list") or _t.startswith("dict"):
        return False
    return ("num" in _t or _t in ("int", "float", "number", "integer"))


_probe_loops = threading.local()

# Deepening cap for the multiway probe's dedicated tracks. After the base
# pass (max_leaves) misses, the nest/fold tracks extend one level at a
# time up to this many leaves. 7 is the validated frontier (six- and
# seven-way proven 2026-09-20); higher values are untested and the nest
# track's |unary|^N cost grows steeply.
_MULTIWAY_DEEPEN_MAX = 7
_MULTIWAY_DEEPEN_LEVEL_BUDGET = 60.0
_MULTIWAY_DEEPEN_TOTAL_BUDGET = 300.0

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

from swarm_engine.cognition.representations import Expr, Hypothesis, SearchBias
from swarm_engine.cognition.constants import mine_constants
from swarm_engine.primitives.core import Effect, Kind, infer

# Search-policy version. An exhaustion record ("this search failed") is
# evidence about the POLICY that was active when it failed, not a timeless
# fact: once the search ordering/policy changes, prior exhaustions are
# stale and must not block retrying a goal the new policy can solve.
# The exhaustion signature incorporates this version, so old records are
# naturally orphaned on a policy change (they can never match a new
# signature). BUMP THIS whenever the search ordering, level schedule, or
# op-scheduling policy in this module changes.
#   1 = pre-2026-09-14 policy (admission-pollution deferral gated on
#       s_min == 2 only)
#   2 = generalized admission-pollution deferral (fires for any probe
#       hypothesis size when explosive acquired ops are present)
#   3 = blind-case admission-pollution deferral (no hypothesis + explosive
#       ops: level-1 split into [1-cheap, 2..max, 1-explosive])
#   4 = identity pre-pass: explosive acquired ops' canonical bindings are
#       banked in the blind-case cheap pass so deeper levels can compose
#       over them (without this, deferring the ops starves the bank)
#   5 = composition probe: blind-case check of primitive(acquired,
#       acquired) by value vector (the structural probe is primitive-only
#       and cannot hypothesize acquired-op compositions)
#   6 = nesting probe: blind-case check of acquired(acquired(x)) by value
#       vector (the positional variant the flat composition probe cannot
#       hypothesize); acquisition-learner strategy evidence is now scoped
#       to the policy version so a repair is not blocked by the stale
#       ordering the failure it repairs produced
#   7 = multi-way probe: bounded value-vector closure over acquired
#       capabilities to three leaves (p1(p2(a1,a2),a3), a1(a2(a3(x)))),
#       the three-way compositions the pairwise probes cannot hypothesize
#   8 = acquired+constant probe (policy v8 era)
#   9 = quadratic probe: evidence-fitted Horner-form quadratics
#       (policy v9 era)
#   10 = four-way multiway probe: max_leaves=4, pure nest-chain track,
#       V0 value-dedup (policy v10 era)
#   11 = five-way multiway probe: default max_leaves=5 plus the
#       associative-fold track (one empirically-associative op folded
#       over each leaf subset; associativity verified on the value
#       vectors, never assumed). The binary DP's level cap provably
#       crowds out 5-leaf left-deep chains (4-leaf prefix evicted from
#       V3's 4096 slots); the fold track reaches them in |subsets| x
#       |ops| candidates.
#   12 = binary-DP resource guard: the DP is capped at 4-way (round 3).
#       Round 4 (5-way binary DP) OOMs on a miss -- measured 2.8GB+ RSS,
#       and the v10 code OOMs identically, so this is a pre-existing
#       resource cliff, not a regression. The fold and nest tracks (both
#       sub-exponential) safely cover 5-way pure shapes; 5-way MIXED
#       shapes via the binary DP are not attempted rather than attempted
#       unsafely.
#   13 = six-way deepening: after the base pass (dedicated tracks at
#       max_leaves, then the capped binary DP) misses, the nest/fold
#       tracks extend one level at a time to _MULTIWAY_DEEPEN_MAX (7),
#       reusing already-built depths (incremental _extend_nest /
#       _extend_folds). The binary DP is not deepened. Also: the
#       acquired-plan fast interpreter (_interpret_plan_values) now
#       enforces the _pow_safe magnitude guard on power steps
#       (_UnsafeMagnitude), closing an OOM via 2**1e18-class
#       computations that the binary-op path already guarded.
#   14 = cost-ordered fold scheduling (P7): the base pass now exhausts
#       the sub-exponential fold track to _MULTIWAY_DEEPEN_MAX BEFORE
#       the super-exponential V1 / binary-DP geometries, instead of
#       gating fold deepening behind the binary DP via the deepening
#       loop. Generic and parameterized (max(max_leaves,
#       _MULTIWAY_DEEPEN_MAX)); no leaf-count, objective, operation, or
#       capability-specific branch. For non-fold objectives the extra
#       fold subsets miss cheaply and the remaining tracks run unchanged.
#   15 = behavioral gap decomposition (P8): scalar composite objectives
#       the generator grammar cannot reach atomically are split into
#       independently acquirable behavioral children (additive fold /
#       nest-chain contracts from value-vector closure); the parent is
#       re-assembled by COMPOSE from acquired children, validated exactly
#       on the parent's examples.
#   16 = gap-driven decomposition fail-closed (P8 validation):
#       an ASSOCIATIVE-FOLD behavioral contract is suppressed when every
#       discovered child is already satisfiable from the current inventory
#       (canonical _resolve checks on scratch nodes) -- no capability gap,
#       so the existing composition route (multiway fold track, proven
#       through 7-way) owns the parent instead of redundantly invoking
#       the decomposition search. Nest-chain contracts are never
#       suppressed: the flat path cannot re-discover the outer-op chain
#       (measured fail on -|x-5| with pre-acquired children).
#   17 = P8 adjacent-boundary repairs (P8-AdjA + P8-AdjC):
#       (a) decomposition ambiguity adjudication: same-(components,depth)
#       exact alternatives are collapsed to distinct order-free
#       partitions (enumeration rotations no longer inflate the ambiguity
#       count) and ties are broken by lowest total pool rank, replacing
#       raw enumeration-order selection -- the winning contract can
#       differ in genuinely ambiguous cases; (b) I7 evidence-sufficiency
#       strengthened n>=k+1 -> n>=k+2 (measured truth-divergent spurious
#       emissions at n=k+1) -- minimal-evidence decompositions now fail
#       closed instead of emitting.
SEARCH_POLICY_VERSION = 46


@dataclass
class SynthesisTrace:
    candidates_tried: int = 0
    depths_explored: int = 0
    rejected: List[str] = field(default_factory=list)
    # Instrumentation added to actually measure where >100x candidate-count
    # variance between nominally similar problems comes from, rather than
    # guess. Populated per target_size level so a cheap and an expensive
    # search of the same nominal shape can be compared level-by-level.
    per_level: List[Dict[str, Any]] = field(default_factory=list)
    dedup_hits: int = 0
    unsafe_value_rejections: int = 0
    dedup_by_op: Dict[str, int] = field(default_factory=dict)
    bool_space_skips: int = 0
    # Structural exclusions (M+4): required arg slots with no constructible
    # producer of the required type in the current search bank/grammar.
    # Distinct from value-level rejected[] messages.
    unsatisfiable_args: List[Dict[str, Any]] = field(default_factory=list)
    # Ambiguity/identifiability (conditional-completion correctness
    # repair): every conditional candidate that fit the training data but
    # was SUPPRESSED because rival explanations -- same predicate mask on
    # training, same target agreement, different value tuples on
    # discriminating (unconstrained) probe inputs -- disagreed with it.
    # Training consistency alone is not treated as proof of
    # identifiability; when multiple materially different explanations
    # remain possible the search fails closed instead of admitting an
    # arbitrary one. Each entry records the distinguishing probe.
    ambiguous: List[Dict[str, Any]] = field(default_factory=list)
    # Probe evaluations performed by the identifiability gate. Counted
    # separately from candidates_tried (which measures the enumerative
    # sweep) so search-efficiency comparisons stay commensurable.
    ambiguity_probe_evals: int = 0
    # Input-dependency guidance consumed by this search (ranking,
    # hypotheses, proven-relevant). Recorded for audit/causal proof.
    dependency_guidance: Dict[str, Any] = field(default_factory=dict)
    # Composite (two-level) structural hypotheses from the nested exact
    # relational probe: outer_op(inner_op(x, y), z) chains that exactly
    # fit the examples. Ordering guidance only -- the sweep still
    # constructs and verifies every candidate through the normal path.
    composite_hypotheses: List[Dict[str, Any]] = field(default_factory=list)
    # Hierarchical search bookkeeping: levels whose blind sweep was
    # deferred until after a guided deeper level (structural-hypothesis
    # jump), and the explicit minimality caveat for that deferral.
    levels_deferred: List[int] = field(default_factory=list)
    minimality_caveat: str = ""
    # Number of hypothesis trees successfully staged bottom-up into the
    # bank before the level loop (construction, not admission).
    staged_hypothesis_nodes: int = 0
    # Explicit contradiction verdict: the training examples themselves map
    # the same inputs to different outputs, so no expression can fit them.
    # Fails closed with a named reason instead of silently returning None.
    contradiction: Optional[str] = None
    # Evidence-derived constants mined for this search (value, provenance,
    # tier, support) -- the literal pool actually seeded as leaves, so a
    # result can be audited for where its constants came from.
    mined_constants: List[Dict[str, Any]] = field(default_factory=list)


class CompositionalSynthesizer:
    """A single-purpose linear-chain plan builder, kept ONLY because
    ReasoningEngine._slot_substitute still needs a way to compile a linear
    op sequence into a plan for its narrow, explicitly-scoped analogical
    adaptation (substituting one step in an already-known chain).

    This class previously also contained `search()` and `search_binary()` —
    two separate, hand-written, arity-special-cased search functions (one
    for exactly one required argument, one for exactly two). Both are
    deleted, not deprecated-in-place: GeneralSynthesizer fully replaces them
    for actual hypothesis search, handling any arity through one mechanism
    instead of one function per arity. Keeping the old search methods here
    "just in case" would have been exactly the outcome asked against —
    removing a boundary means removing the code that enforced it, not
    leaving it dormant alongside its replacement.
    """

    def __init__(self, registry, bias: SearchBias):
        self.reg = registry
        self.bias = bias
        self._chainable = self._find_chainable()

    def _find_chainable(self) -> List[str]:
        out = []
        for name in self.reg.names():
            prim = self.reg.get(name)
            if prim.variadic or prim.effects != (Effect.PURE,):
                continue
            required = [k for k, v in prim.inputs.items() if not v.optional]
            if len(required) == 1:
                out.append(name)
        return out

    def _arg_name(self, op: str) -> str:
        prim = self.reg.get(op)
        required = [k for k, v in prim.inputs.items() if not v.optional]
        return required[0]

    def _apply_all(self, op: str, values: List[Any]) -> Optional[List[Any]]:
        prim = self.reg.get(op)
        arg_name = self._arg_name(op)
        out = []
        for value in values:
            try:
                result = prim.fn(**{arg_name: value})
            except Exception:
                return None
            out.append(result)
        return out

    def _matches_all(self, values: List[Any],
                     examples: Sequence[Tuple[Dict[str, Any], Any]]) -> bool:
        return all(v == expected for v, (_, expected) in zip(values, examples))

    def _build_plan(self, op_sequence: List[str], param_name: str) -> Dict[str, Any]:
        steps = []
        prev_ref: Dict[str, Any] = {"$param": param_name}
        for i, op in enumerate(op_sequence):
            arg_name = self._arg_name(op)
            step_id = f"s{i+1}"
            steps.append({"id": step_id, "op": op, "args": {arg_name: prev_ref}})
            prev_ref = {"$step": step_id}
        return {"name": "synthesized", "params": {param_name: "any"},
                "steps": steps, "output": prev_ref}


class GeneralSynthesizer:
    """Bottom-up enumerative synthesis over expression trees of arbitrary
    arity and nesting — the general mechanism that replaces
    CompositionalSynthesizer's separate unary-chain and binary-application
    special cases (`search` / `search_binary` above), which structurally
    could not reach a primitive like `clamp(x, low, high)` regardless of
    search budget, because they only ever considered exactly one or exactly
    two required arguments.

    Named for what it actually is: bottom-up enumerative synthesis with
    observational-equivalence pruning — a standard, citable technique (the
    same family behind programming-by-example systems such as FlashFill),
    not a novel algorithm. At each size level it builds every well-typed
    application of a primitive to expressions already in the bank, keeps at
    most one representative per distinct *output-value tuple* across the
    examples (two expressions producing identical outputs on every example
    are redundant to keep building from), and returns as soon as a
    candidate's output matches the goal on every example. Arity is not
    special-cased anywhere in this loop — a primitive with three required
    arguments is handled by the same code path as one with one, because the
    loop asks "does the primitive registry say this takes N arguments and
    can the bank fill all N" rather than assuming N is 1 or 2.

    Minimality (no unnecessary sub-expression in the returned tree) holds by
    the same construction as before: every size level is fully explored
    before the next begins, so a smaller matching expression is always found
    and returned first.
    """

    # A value-magnitude guard applied before every primitive call during
    # search — found necessary by tracing an actual hang: the search can
    # construct an astronomically large number (via exponentiation, factorial
    # — themselves perfectly ordinary primitives) and feed it as the COUNT
    # argument to something like seeded_random(seed, n), which calls
    # range(int(n)) and never returns. Every primitive here is individually
    # safe on the inputs it was designed for; none of them is safe against an
    # argument the search itself constructed specifically because nothing
    # bounded it. This is not a per-primitive fix — it is a guard at the one
    # place every candidate value passes through before being used, which is
    # the right place to fix a problem the search creates.
    MAX_NUMERIC_MAGNITUDE = 10 ** 6
    # Deliberately small and generic — NOT tuned to any task this session
    # happens to be testing. Including a number here because it is known to
    # solve a specific target problem would be hardcoding the answer under a
    # different name. Task-specific constants are supplied separately, as
    # `extra_literals` to `search()`, sourced from retrieved knowledge rather
    # than from this class.
    LITERAL_CONSTANTS = (0, 1, 2, -1)
    # Well-known mathematically commutative binary operators. A static,
    # objective-independent property of the operator itself (op(A,B) ==
    # op(B,A) for all A, B), never derived from or specific to any
    # particular search's vocabulary.
    _COMMUTATIVE_OPS = frozenset({"add", "multiply", "equals", "and", "or", "max", "min"})
    MAX_COLLECTION_LENGTH = 10 ** 4

    @staticmethod
    def _value_is_safe(value: Any) -> bool:
        if isinstance(value, bool):
            return True
        if isinstance(value, (int, float)):
            return abs(value) <= GeneralSynthesizer.MAX_NUMERIC_MAGNITUDE
        if isinstance(value, (list, tuple, str, bytes)):
            return len(value) <= GeneralSynthesizer.MAX_COLLECTION_LENGTH
        return True

    def __init__(self, registry, bias: SearchBias, max_size: int = 3,
                 pool_per_type: int = 150, max_arg_combinations: int = 3000,
                 max_candidates: int = 60000, wall_clock_limit_s: float = 20.0,
                 skip_exhausted_bool_space: bool = True):
        # pool_per_type=24 / max_arg_combinations=300 / max_candidates=20000
        # were the original defaults, set before literal-constant seeding
        # existed. Found directly while testing a plain 2*(w+h) composition:
        # bank_by_size[1] routinely holds 300-400+ entries once literals are
        # included, and the needed entry (add_duration(w, h)) was excluded
        # at every pool_per_type up to 400 because max_arg_combinations=300
        # cut off itertools.product's nested traversal before reaching it —
        # pool size wasn't the binding constraint, combination count was.
        # Raising pool/combination limits without raising max_candidates
        # caused its own regression, also found directly: add(abs(a), b),
        # working before this change, started hitting the 20000-candidate
        # ceiling and failing, because every level now costs more per op
        # tried. max_candidates raised to compensate; both changes verified
        # together against every previously-demonstrated case.
        self.reg = registry
        self.bias = bias
        self.max_size = max_size
        self.pool_per_type = pool_per_type
        self.max_arg_combinations = max_arg_combinations
        self.max_candidates = max_candidates
        self.wall_clock_limit_s = wall_clock_limit_s
        # Boolean-space exhaustion skip ("boolean-trigger" optimization):
        # the space of boolean-output value tuples over n examples has
        # exactly 2^n members, so once every reachable tuple has been seen,
        # further boolean-output candidates are mathematically guaranteed
        # duplicates and are skipped without being evaluated. Kept ON by
        # default; set False only for the controlled ablation measuring
        # what the optimization actually saves (it must never change
        # correctness, only cost).
        self.skip_exhausted_bool_space = skip_exhausted_bool_space
        # Per-search state, reset at the top of search(): value-tuple ->
        # structurally distinct Exprs sharing that tuple (observational
        # equivalence classes; the bank itself keeps one representative).
        self._alt_exprs: Dict[str, List[Expr]] = {}
        # Signatures of conditional shapes already proven ambiguous this
        # search -- the completion scan skips them instead of re-finding
        # the same underdetermined shape at every deeper level.
        self._poisoned_signatures: set = set()
        # Signatures of conditionals currently stashed for the
        # level-end identifiability gate (dedup across find sites).
        self._stashed_signatures: set = set()
        # Conditional candidates stashed during a level (found by the
        # completion pass or the sweep); gated for identifiability at
        # level end with the complete bank, so a simpler non-conditional
        # solution at the same level still wins.
        self._stashed_conditionals: List[Expr] = []
        # The current search's training args, for signature/probe work.
        self._search_args_list: List[Dict[str, Any]] = []
        # The current search's literal vocabulary: full leaf list and
        # the numeric float pool the identifiability gate perturbs
        # through. Set at the top of search().
        self._search_leaf_literals: List[Any] = []
        self._search_literal_pool: set = set()
        # Incremental conditional-completion pools (see
        # _find_conditional_completions): per-size classification cursor
        # plus the persistent boolean / partial-mask pools.
        self._cc_seen: Dict[int, int] = {}
        self._cc_bools: list = []
        self._cc_by_type: dict = {}
        # self._ops is now a property (see below) — it must reflect the
        # LIVE registry, not a snapshot taken here. Found directly: a
        # primitive promoted after this constructor ran was registered
        # correctly but never became reachable by this synthesizer's own
        # search, because _ops was a plain attribute frozen at construction
        # time.

    def _find_pure_ops(self) -> List[str]:
        out = []
        for name in self.reg.names():
            prim = self.reg.get(name)
            if prim.variadic or prim.effects != (Effect.PURE,):
                continue
            required = [k for k, v in prim.inputs.items() if not v.optional]
            if required:
                out.append(name)
        return out

    def _nonlearned_max_arity(self) -> int:
        """Max required arity over pure non-learned ops (built-in envelope)."""
        cap = 0
        for name in self.reg.names():
            prim = self.reg.get(name)
            if prim.variadic or prim.effects != (Effect.PURE,):
                continue
            if getattr(prim, "family", "") in ("acquired", "promoted"):
                continue
            ar = sum(1 for v in prim.inputs.values() if not v.optional)
            if ar > cap:
                cap = ar
        return cap

    def _priority_learned_ops(self) -> Set[str]:
        """Learned shortcuts entitled to a pre-pass before guided level 2.

        The system's own verified learned knowledge -- admitted
        capabilities (family="acquired") and promoted primitives
        (family="promoted") whose required arity is within the built-in
        vocabulary's maximum (i.e. not explosive). These are cheap,
        legitimate shortcuts that must keep a level-1 position ahead of
        the probe-guided level 2; otherwise a guided success would
        systematically bypass them even when they solve the objective
        directly. This is the general learned-vs-built-in distinction
        (registry metadata set at admission/promotion time), never
        word-, law-, or benchmark-specific.
        """
        cap = self._nonlearned_max_arity()
        out = set()
        for name in self._ops:
            try:
                prim = self.reg.get(name)
            except Exception:
                continue
            if getattr(prim, "family", "") not in ("acquired", "promoted"):
                continue
            ar = sum(1 for v in prim.inputs.values() if not v.optional)
            if ar <= cap:
                out.add(name)
        return out

    def _composition_probe(
            self, examples: Sequence[Tuple[Dict[str, Any], Any]],
            param_names: Sequence[str], target: Tuple[Any, ...],
            trace: "SynthesisTrace") -> Optional[Tuple["Expr", Tuple[Any, ...]]]:
        """Check primitive(acquired, acquired) compositions by value vector.

        2026-09-14: the blind-case growth probe (see call site). Returns
        (expr, values) for the first pure binary primitive p and ordered
        acquired-capability pair (a1, a2) with p(a1, a2) == target, where
        a1/a2 use their canonical (identity) bindings. None if no such
        composition exists. Pure vector arithmetic -- no plan enumeration.
        """
        # Canonical value vectors for acquired capabilities. Uses the
        # identity binding (op's role keys must be present in the current
        # objective's inputs); role-shifted variants are skipped here --
        # the blind-case level sweep still covers them via enumeration.
        acq_vecs: Dict[str, Tuple[Any, ...]] = {}
        acq_exprs: Dict[str, "Expr"] = {}
        for _eop in sorted(self._ops):
            try:
                _eprim = self.reg.get(_eop)
            except Exception:
                continue
            if getattr(_eprim, "family", "") != "acquired":
                continue
            _ereq = [k for k, v in _eprim.inputs.items() if not v.optional]
            if not _ereq or not all(r in param_names for r in _ereq):
                continue
            _eexpr = Expr(op=_eop,
                          children=tuple((r, Expr(param=r)) for r in _ereq))
            _ekw = {r: tuple(a[r] for a, _ in examples) for r in _ereq}
            _evals = self._apply_across_examples(
                _eprim, _ereq, _ekw, len(examples))
            if _evals is None:
                continue
            acq_vecs[_eop] = _evals
            acq_exprs[_eop] = _eexpr
        if len(acq_vecs) < 1:
            return None
        # Pure binary primitives, deterministic order.
        _binprims = []
        for _pname in sorted(self._ops):
            if _pname in acq_vecs:
                continue
            try:
                _pprim = self.reg.get(_pname)
            except Exception:
                continue
            _peff = getattr(_pprim, "effects", ())
            # 2026-09-14: the attribute is `effects` (tuple), not `effect`.
            if Effect.PURE not in _peff:
                continue
            _preq = [k for k, v in _pprim.inputs.items() if not v.optional]
            if len(_preq) != 2:
                continue
            # R13 parity: the probe combines numeric value vectors, so
            # only numeric-input, numeric-output primitives are tried.
            # Besides being meaningless for numbers, list-output prims
            # (e.g. seeded_random) OOM on large numeric inputs by
            # allocating input-magnitude-sized lists.
            if not all(_is_numeric_input_type(_pprim.inputs[_k])
                       for _k in _preq):
                continue
            if not _is_numeric_output_prim(_pprim):
                continue
            _binprims.append((_pname, _pprim, _preq))
        _names = sorted(acq_vecs)
        for _pname, _pprim, _preq in _binprims:
            for _a1 in _names:
                for _a2 in _names:
                    _kw = {_preq[0]: acq_vecs[_a1], _preq[1]: acq_vecs[_a2]}
                    # R16 parity: pre-reject power candidates whose result
                    # would provably exceed the magnitude cap BEFORE the
                    # primitive attempts the materialization. The native
                    # `power` primitive enforces the same check
                    # authoritatively; this is the cheap early filter,
                    # mirroring the binary-DP guard.
                    if "pow" in _pname.lower() and not all(
                            _pow_safe_scalar(_bv, _ev)
                            for _bv, _ev in zip(_kw[_preq[0]],
                                               _kw[_preq[1]])):
                        trace.candidates_tried += 1
                        continue
                    _vals = self._apply_across_examples(
                        _pprim, _preq, _kw, len(examples))
                    if _vals is None or _vals != target:
                        continue
                    _cexpr = Expr(
                        op=_pname,
                        children=((_preq[0], acq_exprs[_a1]),
                                  (_preq[1], acq_exprs[_a2])))
                    trace.candidates_tried += 1
                    return _cexpr, _vals
        return None

    def _nesting_probe(self, examples, param_names, target, trace):
        """Check acquired∘acquired nestings by value vector.

        Structural counterpart of _composition_probe: where the flat probe
        checks p(a1, a2) for pure binary primitives p, this checks
        a1(a2(x)) for ordered pairs of acquired capabilities -- the
        positional variant "apply one acquired capability to another's
        output". Blind level-1/2 enumeration cannot reach this shape
        within the wall-clock once the acquired vocabulary grows (each new
        acquired op multiplies the level-1 candidate space), while the
        probe is O(A^2) vector operations.

        Guidance-only: a hit goes through the same identifiability gate as
        any synthesized candidate, and the probe's placement (after the
        flat probe, after the primitive size-3 sweep and Check 0) means
        simpler shapes are still preferred. Returns (Expr, values) or None.
        Canonical (identity) bindings only, mirroring _composition_probe.
        """
        acq_vecs: Dict[str, Tuple[Any, ...]] = {}
        acq_exprs: Dict[str, "Expr"] = {}
        acq_outer: Dict[str, Tuple[Any, str]] = {}
        for _eop in sorted(self._ops):
            try:
                _eprim = self.reg.get(_eop)
            except Exception:
                continue
            if getattr(_eprim, "family", "") != "acquired":
                continue
            _ereq = [k for k, v in _eprim.inputs.items() if not v.optional]
            if len(_ereq) != 1 or _ereq[0] not in param_names:
                continue
            _eexpr = Expr(op=_eop,
                          children=((_ereq[0], Expr(param=_ereq[0])),))
            _ekw = {_ereq[0]: tuple(a[_ereq[0]] for a, _ in examples)}
            _evals = self._apply_across_examples(
                _eprim, _ereq, _ekw, len(examples))
            if _evals is None:
                continue
            acq_vecs[_eop] = _evals
            acq_exprs[_eop] = _eexpr
            acq_outer[_eop] = (_eprim, _ereq[0])
        _names = sorted(acq_vecs)
        if len(_names) < 2:
            return None
        for _outer in _names:
            _oprim, _oreq = acq_outer[_outer]
            for _inner in _names:
                _kw = {_oreq: acq_vecs[_inner]}
                _vals = self._apply_across_examples(
                    _oprim, [_oreq], _kw, len(examples))
                trace.candidates_tried += 1
                if _vals is None or _vals != target:
                    continue
                _nexpr = Expr(op=_outer,
                              children=((_oreq, acq_exprs[_inner]),))
                return _nexpr, _vals
        return None

    def _acquired_const_probe(self, examples, param_names, target, trace):
        """Check p(acquired, const) by value vectors.

        The pairwise probes cover p(a1,a2) (both acquired) and a1(a2(x)),
        but not p(a1, c) where c is a small constant -- e.g. x^2+3 =
        add(vab(x), 3) with vab=x^2 acquired. Blind enumeration cannot
        reach this shape once the acquired vocabulary grows (the level-1
        candidate space exhausts the budget before size 2). This probe
        tries small integer constants with numeric binary primitives, by
        value vectors not plan enumeration. Guidance-only: a hit goes
        through the same identifiability gate. No goal words or recipes:
        pure structural search.
        """
        # Acquired vectors (same filter as the pairwise probes).
        _acq_vecs = {}
        _acq_exprs = {}
        for _eop in sorted(self._ops):
            try:
                _eprim = self.reg.get(_eop)
            except Exception:
                continue
            if getattr(_eprim, "family", "") != "acquired":
                continue
            _ereq = [k for k, v in _eprim.inputs.items() if not v.optional]
            if not _ereq or not all(r in param_names for r in _ereq):
                continue
            _eexpr = Expr(op=_eop,
                          children=tuple((r, Expr(param=r)) for r in _ereq))
            _ekw = {r: tuple(a[r] for a, _ in examples) for r in _ereq}
            _evals = self._apply_across_examples(
                _eprim, _ereq, _ekw, len(examples))
            if _evals is None:
                continue
            _acq_vecs[_eop] = _evals
            _acq_exprs[_eop] = _eexpr
        if not _acq_vecs:
            return None
        # Numeric binary primitives (same type filter as _multiway_probe).
        def _is_num(_v):
            _t = str(_v).lower().strip()
            if _t.startswith("list") or _t.startswith("dict"):
                return False
            return ("num" in _t or _t in ("int", "float", "number",
                                          "integer"))
        _binprims = []
        for _pname in sorted(self._ops):
            if _pname in _acq_vecs:
                continue
            try:
                _pprim = self.reg.get(_pname)
            except Exception:
                continue
            if Effect.PURE not in getattr(_pprim, "effects", ()):
                continue
            _preq = [k for k, v in _pprim.inputs.items() if not v.optional]
            if len(_preq) != 2:
                continue
            if not all(_is_num(_pprim.inputs[_k]) for _k in _preq):
                continue
            _ot = str(getattr(_pprim, "output", "")).lower()
            if not ("num" in _ot or _ot in ("int", "float", "number",
                                            "integer")):
                continue
            _binprims.append((_pname, _pprim, _preq))
        # Small integer constants; const vector replicates across examples.
        _n = len(examples)
        for _a1 in sorted(_acq_vecs):
            _avec = _acq_vecs[_a1]
            _aexpr = _acq_exprs[_a1]
            for _pname, _pprim, _preq in _binprims:
                for _c in range(-5, 11):
                    _cvec = tuple(_c for _ in range(_n))
                    _cexpr = Expr(literal=_c, is_literal=True)
                    # Try const as second arg, then as first arg.
                    for _swap in (False, True):
                        _kw = {_preq[0]: _avec if not _swap else _cvec,
                               _preq[1]: _cvec if not _swap else _avec}
                        # R16 parity: pre-reject power candidates whose
                        # result would provably exceed the magnitude cap
                        # (e.g. power(10, acquired_vec) with a huge
                        # acquired vector). Same cheap early filter as the
                        # other probes; the primitive enforces it too.
                        if "pow" in _pname.lower() and not all(
                                _pow_safe_scalar(_bv, _ev)
                                for _bv, _ev in zip(_kw[_preq[0]],
                                                   _kw[_preq[1]])):
                            trace.candidates_tried += 1
                            continue
                        _vals = self._apply_across_examples(
                            _pprim, _preq, _kw, _n)
                        trace.candidates_tried += 1
                        if _vals is None or _vals != target:
                            continue
                        if _swap:
                            _expr = Expr(
                                op=_pname,
                                children=((_preq[0], _cexpr),
                                          (_preq[1], _aexpr)))
                        else:
                            _expr = Expr(
                                op=_pname,
                                children=((_preq[0], _aexpr),
                                          (_preq[1], _cexpr)))
                        return _expr, _vals
        return None

    @staticmethod
    def _resolve_plan_ref_sync(ref, env):
        """Sync mirror of PlanComposer._resolve for pure value plans."""
        if isinstance(ref, dict) and len(ref) == 1:
            key, val = next(iter(ref.items()))
            if key == "$step" or key == "$var":
                return env[val]
            if key == "$param":
                return env["__params__"][val]
            if key == "$list":
                return [GeneralSynthesizer._resolve_plan_ref_sync(v, env)
                        for v in val]
            if key == "$dict":
                return {k: GeneralSynthesizer._resolve_plan_ref_sync(v, env)
                        for k, v in val.items()}
            if key == "$tuple":
                return tuple(GeneralSynthesizer._resolve_plan_ref_sync(v, env)
                             for v in val)
            raise ValueError(f"unsupported plan ref {ref!r}")
        if isinstance(ref, list):
            return [GeneralSynthesizer._resolve_plan_ref_sync(v, env)
                    for v in ref]
        return ref

    def _interpret_plan_values(self, plan, args_list, _seen=frozenset()):
        """Minimal synchronous interpreter for pure admitted plans.

        Evaluates plan over args_list WITHOUT the composer: no event
        loop, no re-analysis, no per-step type/governor/trace overhead
        (each reg.invoke costs ~40us; a 7-step plan over 4 examples is
        ~1ms, which makes the N-way probe's tens of thousands of
        applications infeasible). Nested acquired ops recurse through
        their own admitted plans. Bails (returns None) on control
        flow, $lambda/$partial, ctx-needing primitives, cycles, or any
        error -- the caller falls back to the real execution path. The
        identifiability gate re-checks any hit via real semantics, so
        this can only affect speed, never admission correctness.
        """
        _pid = id(plan)
        if _pid in _seen:
            return None
        _seen = _seen | {_pid}
        steps = plan.get("steps") or []
        for _s in steps:
            if _s.get("control"):
                return None
        out_ref = plan.get("output")
        results = []
        for args in args_list:
            env = {"__params__": dict(args)}
            try:
                for _s in steps:
                    _prim = self.reg.get(_s["op"])
                    if _prim is None or getattr(_prim, "needs_ctx", False):
                        return None
                    _kwargs = {}
                    for _k, _v in (_s.get("args") or {}).items():
                        _kwargs[_k] = self._resolve_plan_ref_sync(_v, env)
                    _sub = getattr(getattr(_prim, "fn", None),
                                   "_remor_plan", None)
                    if _sub is not None:
                        _sv = self._interpret_plan_values(_sub, [_kwargs],
                                                          _seen)
                        if _sv is None:
                            return None
                        env[_s["id"]] = _sv[0]
                    else:
                        if "pow" in str(_s["op"]).lower():
                            # Same guard the probe applies to binary power
                            # candidates: a power step applied to DP-scale
                            # inputs (e.g. 2**1e18 via an acquired
                            # exponential) would OOM before the magnitude
                            # filter could reject it. Skip the candidate.
                            # Authoritative R16 enforcement lives in the
                            # native `power` primitive itself; this is the
                            # cheap pre-materialization check for plan
                            # steps (the primitive re-checks on call).
                            _req = [k for k, v in _prim.inputs.items()
                                    if not v.optional]
                            if len(_req) >= 2:
                                _ensure_pow_safe(_kwargs.get(_req[0]),
                                                 _kwargs.get(_req[1]))
                        env[_s["id"]] = _prim.fn(**_kwargs)
                results.append(
                    self._resolve_plan_ref_sync(out_ref, env))
            except _UnsafeMagnitude:
                raise
            except Exception:
                return None
        return tuple(results)

    def _apply_acquired_vector(self, prim, required, kwargs_values, n):
        """Apply an acquired primitive across examples as one batch.

        The default _apply_across_examples calls prim.fn per scalar; for
        composer-backed acquired ops each call pays asyncio.run() plus a
        redundant static analysis of the already-admitted plan (~1ms),
        which makes the N-way probe's tens of thousands of applications
        infeasible. When the prim exposes its admitted plan (see
        admission._register_capability_as_primitive), run all n
        applications in a single thread-local event loop and skip
        re-analysis. Failures raise the same RuntimeError as the slow
        path, so behavior is identical; anything unexpected falls back
        to _apply_across_examples.
        """
        _fn = getattr(prim, "fn", None)
        _plan = getattr(_fn, "_remor_plan", None)
        _composer = getattr(_fn, "_remor_composer", None)
        if _plan is None or _fn is None:
            return self._apply_across_examples(prim, required,
                                               kwargs_values, n)
        # Fastest: interpret the admitted plan synchronously (no event
        # loop, no per-step invoke overhead). Falls back below on any
        # unsupported construct.
        _args_list = [{name: kwargs_values[name][_i] for name in required}
                      for _i in range(n)]
        try:
            _iv = self._interpret_plan_values(_plan, _args_list)
        except _UnsafeMagnitude:
            # The candidate would compute a value beyond the magnitude
            # cap (e.g. 2**1e18 through an acquired exponential): skip
            # it the same way a failed application is skipped. Never
            # fall through to the slower paths here -- they would
            # attempt the same exploding computation.
            return None
        if _iv is not None:
            return _iv
        if _composer is None:
            return self._apply_across_examples(prim, required,
                                               kwargs_values, n)
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            pass
        else:
            # Nested inside a running loop: run_until_complete would
            # raise; fall back (the slow path handles nesting itself).
            return self._apply_across_examples(prim, required,
                                               kwargs_values, n)

        async def _batch():
            _out = []
            for _i in range(n):
                _r = await _composer.execute(
                    _plan,
                    {name: kwargs_values[name][_i] for name in required},
                    skip_check=True)
                if not _r.get("success"):
                    raise RuntimeError(_r.get(
                        "error", "acquired capability execution failed"))
                _out.append(_r.get("value"))
            return _out

        try:
            _loop = getattr(_probe_loops, "loop", None)
            if _loop is None or _loop.is_closed():
                _loop = asyncio.new_event_loop()
                _probe_loops.loop = _loop
            _vals = _loop.run_until_complete(_batch())
        except Exception:
            return self._apply_across_examples(prim, required,
                                               kwargs_values, n)
        return tuple(_vals)

    def _multiway_probe(self, examples, param_names, target, trace,
                        max_leaves: int = 5):
        """Bounded value-vector closure over acquired capabilities (N-way).

        Generalizes _composition_probe (p(a1,a2), two acquired leaves) and
        _nesting_probe (a1(a2(x)), two acquired leaves) to three-or-more
        acquired leaves via dynamic programming over value vectors: V0
        holds the acquired-op vectors; V1 holds every binary/nesting
        combination of V0 pairs; V_k (k>=2) combines each V_{k-1}
        composite with each V0 acquired op (binary both orders, plus
        unary-acquired nesting) and checks the target inline during
        construction. Round 2 checks 3-leaf shapes like p1(p2(a1,a2),a3)
        -- size 5, beyond the synthesizer's max_size -- and
        a1(a2(a3(x))), which blind enumeration cannot reach within the
        wall-clock once the acquired vocabulary grows, and which the
        pairwise probes cannot hypothesize; round 3 checks 4-leaf
        shapes. The binary DP stops at 4-way (resource guard -- round 4
        OOMs on a miss, pre-existing); 5-way pure shapes are covered by
        the dedicated fold and nest tracks below, both parameterized by
        max_leaves (default 5).

        Guidance-only: a hit goes through the same identifiability gate as
        any synthesized candidate, and placement after the pairwise probes
        means simpler shapes still win when they exist. Vectors are
        deduplicated by value; intermediate magnitude is capped purely as
        a pathological-blowup guard (target matches are tested inline
        before the cap, so no target-matching vector is ever pruned --
        the cap only bounds what the NEXT round builds from). V1 is
        uncapped exactly as in the original 3-way probe, so the 3-way
        path is behaviorally identical; deeper levels carry a generous
        vector cap as the resource guard for the N-way extension.
        Two dedicated cheap tracks complement the binary DP: pure unary
        nesting chains (one of only |unary|^k value vectors, built
        directly) and associative folds (one empirically-associative op
        folded over each leaf subset -- |subsets| x |ops| candidates,
        not |V0|^N). Both are parameterized by max_leaves, and both are
        unioned into the V_k banks so mixed shapes can build on them.
        Returns (Expr, values) or None. No goal words, capability ids,
        or recipes: pure structural search over the registry's acquired
        family.
        """
        # V0: acquired ops with identity bindings (same filter as the
        # pairwise probes).
        _v0 = []
        # value_key -> (apply_seconds, index in _v0). On a value tie the
        # faster behaviorally-identical primitive wins: registration
        # paths differ in cost (a direct sandboxed callable vs a
        # composer round-trip per scalar application), and the probe is
        # a hot loop. Timing reuses the n applications already done
        # below; the ~1000x cost gap dwarfs measurement noise.
        _v0_seen = {}
        for _eop in sorted(self._ops):
            try:
                _eprim = self.reg.get(_eop)
            except Exception:
                continue
            if getattr(_eprim, "family", "") != "acquired":
                continue
            _ereq = [k for k, v in _eprim.inputs.items() if not v.optional]
            if not _ereq or not all(r in param_names for r in _ereq):
                continue
            _eexpr = Expr(op=_eop,
                          children=tuple((r, Expr(param=r)) for r in _ereq))
            _ekw = {r: tuple(a[r] for a, _ in examples) for r in _ereq}
            _t0 = time.perf_counter()
            _evals = self._apply_acquired_vector(
                _eprim, _ereq, _ekw, len(examples))
            _dt = time.perf_counter() - _t0
            if _evals is None:
                continue
            # 2026-09-20: dedup V0 by value vector. Each acquired
            # capability is registered under both its name and its
            # content-addressed id (acquired.cap_<hash>), so without
            # this V0 holds every capability twice and the N-way
            # combinatorics blow up (pairs quadruple). Two V0 entries
            # with identical value vectors are interchangeable for a
            # value-vector probe: combining with either yields the
            # same result vector, and the identifiability gate judges
            # the returned Expr behaviorally.
            _evk = self._value_key(_evals)
            if _evk in _v0_seen:
                _prev_dt, _prev_idx = _v0_seen[_evk]
                if _dt < _prev_dt:
                    _v0[_prev_idx] = (_eop, _eprim, _ereq, _evals, _eexpr)
                    _v0_seen[_evk] = (_dt, _prev_idx)
                continue
            _v0_seen[_evk] = (_dt, len(_v0))
            _v0.append((_eop, _eprim, _ereq, _evals, _eexpr))
        if len(_v0) < 2:
            return None
        # All acquired-op names (including value-duplicate aliases
        # pruned from _v0 above): acquired ops are never used as the
        # combining primitive.
        _v0_names = set()
        for _eop in sorted(self._ops):
            try:
                _eprim = self.reg.get(_eop)
            except Exception:
                continue
            if getattr(_eprim, "family", "") == "acquired":
                _v0_names.add(_eop)
        # Pure binary primitives, deterministic order (same as the flat
        # probe); acquired ops are never used as the combining primitive.
        # 2026-09-19: restrict to numeric-input primitives. The probe
        # combines numeric value vectors; non-numeric pure primitives
        # (sample, similarity, regex_*, ...) are not meaningful for
        # numbers and some OOM on large numeric inputs (e.g. sample
        # with n=1e9 tries to allocate a billion-element list).
        def _is_numeric_input(_v):
            _t = str(_v).lower().strip()
            # Exclude containers (list[num], dict[..]) -- the probe
            # feeds scalar numbers, not collections.
            if _t.startswith("list") or _t.startswith("dict"):
                return False
            return ("num" in _t or _t in ("int", "float", "number",
                                          "integer"))
        def _is_numeric_output(_p):
            _t = str(getattr(_p, "output", "")).lower()
            return ("num" in _t or _t in ("int", "float", "number",
                                          "integer"))
        _binprims = []
        for _pname in sorted(self._ops):
            if _pname in _v0_names:
                continue
            try:
                _pprim = self.reg.get(_pname)
            except Exception:
                continue
            if Effect.PURE not in getattr(_pprim, "effects", ()):
                continue
            _preq = [k for k, v in _pprim.inputs.items() if not v.optional]
            if len(_preq) != 2:
                continue
            if not all(_is_numeric_input(_pprim.inputs[_k])
                       for _k in _preq):
                continue
            # The probe builds numeric value vectors; the combining
            # primitive must return a single number, not a list/dict.
            if not _is_numeric_output(_pprim):
                continue
            _binprims.append((_pname, _pprim, _preq))
        # Unary acquired ops for the nesting dimension.
        _unary = [(_n, _p, _r) for (_n, _p, _r, _, _) in _v0 if len(_r) == 1]

        def _mag_ok(vals):
            for _v in vals:
                if isinstance(_v, bool):
                    continue
                if isinstance(_v, (int, float)) and abs(_v) > _POW_MAG_CAP:
                    return False
            return True

        def _pow_safe(_base_vals, _exp_vals):
            """True iff power(base, exp) cannot exceed _POW_MAG_CAP.
            Prevents pathological computations (e.g. 1e18**1e18) that
            would OOM before the magnitude cap can filter them. Pure
            safety guard: delegates per-pair to _pow_safe_scalar."""
            for _bv, _ev in zip(_base_vals, _exp_vals):
                if not _pow_safe_scalar(_bv, _ev, _POW_MAG_CAP):
                    return False
            return True

        # Pure unary nesting chains, tracked separately from the binary
        # DP. A k-deep chain A(B(...(x))) is one of only |unary|^k value
        # vectors, but the binary DP's level cap (4096) crowds such
        # chains out: by round 3 the binary explosion fills V2 and the
        # 3-deep chains needed for a 4-nest are evicted before U is
        # applied. The chains are tiny, so build them directly (no cap)
        # and check the target at each depth. They are also unioned
        # into each V_k below so mixed shapes (binary of nests, nest
        # of binary) can build on them.
        # Incremental pure-nesting chains. _nest[d] holds the deduped
        # value vectors of (d+1)-deep chains; _extend_nest grows it to a
        # requested depth, checking the target inline. Incremental so a
        # later deepening pass reuses already-built depths instead of
        # recomputing them. _deadline=None means no time limit (the
        # base pass keeps its exact historical behavior).
        _nest = [[(_v, _e) for _, _, _, _v, _e in _v0]]
        _nest_exhausted = False

        def _extend_nest(_upto, _deadline):
            """Extend pure-nesting chains to depth _upto.

            Returns (hit, completed): hit is (expr, vals) or None;
            completed is False iff the level could not finish within
            _deadline (deeper nest levels are only more expensive, so
            the caller should stop extending nests).
            """
            nonlocal _nest_exhausted
            if _nest_exhausted:
                return None, True
            while len(_nest) < _upto:
                _cur = {}
                for _un, _up, _ur in _unary:
                    for _pv, _pe in _nest[-1]:
                        if (_deadline is not None
                                and time.time() > _deadline):
                            # Budget spent: this depth is incomplete;
                            # deeper depths are only more expensive.
                            return None, False
                        _kw = {_ur[0]: _pv}
                        _vals = self._apply_acquired_vector(
                            _up, _ur, _kw, len(examples))
                        trace.candidates_tried += 1
                        if _vals is None or not _mag_ok(_vals):
                            continue
                        if _vals == target:
                            return ((Expr(op=_un,
                                          children=((_ur[0], _pe),)),
                                     _vals), True)
                        _nkk = self._value_key(_vals)
                        if _nkk not in _cur:
                            _cur[_nkk] = (_vals, Expr(
                                op=_un, children=((_ur[0], _pe),)))
                _nest.append(list(_cur.values()))
                if not _cur:
                    # Deeper chains impossible.
                    _nest_exhausted = True
                    return None, True
            return None, True

        _hit, _ = _extend_nest(max_leaves, None)
        if _hit is not None:
            return _hit

        # Associative-fold track: N-leaf single-op compositions. The
        # binary DP below appends one V0 leaf per round and truncates
        # each level to the first 4096 entries in iteration order; a
        # 5-leaf left-deep chain ((((A+B)+C)+D)+E) needs its 4-leaf
        # prefix to survive V3's truncation, but V3 holds ~600k
        # candidates -- the prefix is evicted and the 5-fold is
        # unreachable no matter how long the DP runs (measured
        # 2026-09-20: 1.3M candidates, miss). For an associative op,
        # though, every binary tree over the same leaves has the same
        # value, so instead of searching trees the track folds each
        # leaf-subset directly: |subsets| x |assoc ops| candidates,
        # not |V0|^N. Associativity is verified empirically on the
        # value vectors (never assumed from the op name); a hit is
        # checked against the target inline and goes through the same
        # identifiability gate as any probe result. Parameterized by
        # max_leaves -- any N, any empirically-associative op -- not
        # a leaf-count special case. Fold values are unioned into the
        # V_k banks so mixed shapes (binary of folds) can build on
        # them, exactly as the pure nesting chains are.
        def _fold_pow_ok(_pname, _xvals, _yvals):
            if "pow" in _pname.lower():
                return _pow_safe(_xvals, _yvals)
            return True

        _assoc_ops = []
        _v0_vals = [_evals for _, _, _, _evals, _ in _v0]
        for _pname, _pprim, _preq in _binprims:
            _ok = True
            for _ia in range(len(_v0_vals)):
                for _ib in range(_ia + 1, len(_v0_vals)):
                    for _ic in range(_ib + 1, len(_v0_vals)):
                        if not _fold_pow_ok(
                                _pname, _v0_vals[_ia], _v0_vals[_ib]):
                            continue
                        _ab = self._apply_across_examples(
                            _pprim, _preq,
                            {_preq[0]: _v0_vals[_ia],
                             _preq[1]: _v0_vals[_ib]}, len(examples))
                        _bc = self._apply_across_examples(
                            _pprim, _preq,
                            {_preq[0]: _v0_vals[_ib],
                             _preq[1]: _v0_vals[_ic]}, len(examples))
                        trace.candidates_tried += 2
                        if _ab is None or _bc is None:
                            _ok = False
                            break
                        if not _fold_pow_ok(_pname, _ab, _v0_vals[_ic]):
                            continue
                        if not _fold_pow_ok(_pname, _v0_vals[_ia], _bc):
                            continue
                        _abc1 = self._apply_across_examples(
                            _pprim, _preq,
                            {_preq[0]: _ab,
                             _preq[1]: _v0_vals[_ic]}, len(examples))
                        _abc2 = self._apply_across_examples(
                            _pprim, _preq,
                            {_preq[0]: _v0_vals[_ia],
                             _preq[1]: _bc}, len(examples))
                        trace.candidates_tried += 2
                        if (_abc1 is None or _abc2 is None
                                or _abc1 != _abc2):
                            _ok = False
                            break
                    if not _ok:
                        break
                if not _ok:
                    break
            if _ok:
                _assoc_ops.append((_pname, _pprim, _preq))
        # _folds[k]: value_key -> (vals, Expr) for k-leaf folds.
        # Incremental like _nest: _extend_folds grows it to subset size
        # _upto, checking the target inline, reusing the one
        # associativity pre-pass above.
        _folds = {}
        _folds_max_r = 1

        def _extend_folds(_upto, _deadline):
            """Extend fold subsets to size _upto. Returns (hit, completed)
            like _extend_nest. Folds are O(subsets) and essentially never
            time out, but the deadline is honored for uniformity."""
            nonlocal _folds_max_r
            if not _assoc_ops or len(_v0) < 2:
                return None, True
            import itertools as _it
            for _r in range(_folds_max_r + 1, min(_upto, len(_v0)) + 1):
                _fr = {}
                for _combo in _it.combinations(range(len(_v0)), _r):
                    for _pname, _pprim, _preq in _assoc_ops:
                        if (_deadline is not None
                                and time.time() > _deadline):
                            return None, False
                        _acc = _v0_vals[_combo[0]]
                        _expr = _v0[_combo[0]][4]
                        _good = True
                        for _ci in _combo[1:]:
                            if not _fold_pow_ok(_pname, _acc,
                                                _v0_vals[_ci]):
                                _good = False
                                break
                            _acc = self._apply_across_examples(
                                _pprim, _preq,
                                {_preq[0]: _acc,
                                 _preq[1]: _v0_vals[_ci]},
                                len(examples))
                            trace.candidates_tried += 1
                            if _acc is None:
                                _good = False
                                break
                            _expr = Expr(
                                op=_pname,
                                children=((_preq[0], _expr),
                                          (_preq[1], _v0[_ci][4])))
                            if _acc == target:
                                return (_expr, _acc), True
                            if not _mag_ok(_acc):
                                _good = False
                                break
                        if not _good:
                            continue
                        _fkk = self._value_key(_acc)
                        if _fkk not in _fr:
                            _fr[_fkk] = (_acc, _expr)
                _folds[_r] = _fr
                _folds_max_r = _r
            return None, True

        # 2026-09-21 (P7): cost-ordered scheduling. The fold track is
        # sub-exponential (O(2^N) subsets x O(assoc ops)) while the V1 /
        # binary-DP geometries below are super-exponential and the base
        # nest track is exponential in |unary|. Exhaust the cheap fold
        # track to the full policy budget BEFORE those expensive tracks,
        # rather than gating fold deepening behind the binary DP via the
        # deepening loop. Generic and parameterized (uses the existing
        # _MULTIWAY_DEEPEN_MAX; no leaf-count, objective, operation, or
        # capability-specific branch): for non-fold objectives the extra
        # fold subsets simply miss cheaply and the expensive tracks run
        # unchanged afterwards. The deepening loop below is unaffected
        # (_extend_folds is incremental, so its fold calls become no-ops).
        _hit, _ = _extend_folds(max(max_leaves, _MULTIWAY_DEEPEN_MAX),
                                None)
        if _hit is not None:
            return _hit

        # V1: every pairwise combination (vectors only -- the pairwise
        # probes already tested these against the target). Dedup by the
        # standard value key (vectors may contain unhashable values).
        _v1 = {}
        for _a1n, _a1p, _a1r, _a1v, _a1e in _v0:
            for _a2n, _a2p, _a2r, _a2v, _a2e in _v0:
                for _pname, _pprim, _preq in _binprims:
                    _kw = {_preq[0]: _a1v, _preq[1]: _a2v}
                    # R16 parity: same pre-rejection as the binary-DP
                    # rounds below (V0 vectors are training-scale, but an
                    # acquired exponential on a large training input can
                    # still exceed the cap).
                    if "pow" in _pname.lower() and not _pow_safe(
                            _kw[_preq[0]], _kw[_preq[1]]):
                        trace.candidates_tried += 1
                        continue
                    _vals = self._apply_across_examples(
                        _pprim, _preq, _kw, len(examples))
                    trace.candidates_tried += 1
                    if _vals is None or not _mag_ok(_vals):
                        continue
                    _vk = self._value_key(_vals)
                    if _vk not in _v1:
                        _v1[_vk] = (_vals, Expr(
                            op=_pname,
                            children=((_preq[0], _a1e), (_preq[1], _a2e))))
                for _un, _up, _ur in _unary:
                    _kw = {_ur[0]: _a2v}
                    _vals = self._apply_acquired_vector(
                        _up, _ur, _kw, len(examples))
                    trace.candidates_tried += 1
                    if _vals is None or not _mag_ok(_vals):
                        continue
                    _vk = self._value_key(_vals)
                    if _vk not in _v1:
                        _v1[_vk] = (_vals, Expr(
                            op=_un, children=((_ur[0], _a2e),)))
        if not _v1:
            return None
        # Union the pure 1-deep nesting chains so the binary DP and
        # later rounds can build mixed shapes on them. (Deeper chains
        # are unioned into their V_k in the round loop.)
        if len(_nest) > 1:
            for _nv, _ne in _nest[1]:
                _nkk = self._value_key(_nv)
                if _nkk not in _v1:
                    _v1[_nkk] = (_nv, _ne)
        # Union the associative folds so the binary DP and later
        # rounds can build mixed shapes (binary of folds) on them.
        # The folds are few; the level cap stays soft for them.
        for _fv, _fe in _folds.get(2, {}).values():
            _fkk = self._value_key(_fv)
            if _fkk not in _v1:
                _v1[_fkk] = (_fv, _fe)
        # V_k for k>=2: each V_{k-1} composite against each V0 acquired
        # op (binary both orders, plus unary-acquired nesting). The
        # target check happens inline during construction at every
        # round, so a target-matching vector is always tested before
        # any cap; the cap only bounds what the next round builds from.
        # Round 2 == the original 3-way scope; round 3 reaches 4-leaf
        # shapes; max_leaves bounds the extension.
        _LEVEL_CAP = 4096
        # 2026-09-20: the binary DP is capped at 4-way (round 3). Round 4
        # (5-way binary DP) OOMs on a miss -- measured 2.8GB+ RSS, and the
        # original v10 code OOMs identically, so this is a pre-existing
        # resource cliff, not a regression. The dedicated associative-fold
        # and pure-nest tracks above (both O(subsets)/O(|unary|^k), not
        # O(cap^rounds)) safely cover 5-way pure shapes at max_leaves=5;
        # 5-way MIXED shapes via the binary DP are not attempted rather
        # than attempted unsafely. A miss here fails closed to the other
        # routes; it never OOMs.
        _DP_MAX_ROUND = 3

        def _ordered(_pname, _preq, _vprev_expr, _a1e, _swap):
            if _swap:
                return Expr(op=_pname,
                            children=((_preq[0], _a1e),
                                      (_preq[1], _vprev_expr)))
            return Expr(op=_pname,
                        children=((_preq[0], _vprev_expr),
                                  (_preq[1], _a1e)))

        _levels = [_v1]
        for _round in range(2, min(max_leaves, _DP_MAX_ROUND + 1)):
            _vk = {}
            for _vprev_vals, _vprev_expr in _levels[-1].values():
                for _a1n, _a1p, _a1r, _a1v, _a1e in _v0:
                    for _pname, _pprim, _preq in _binprims:
                        for _swap in (False, True):
                            _kw = {_preq[0]: _vprev_vals if not _swap else _a1v,
                                   _preq[1]: _a1v if not _swap else _vprev_vals}
                            if "pow" in _pname.lower() and not _pow_safe(
                                    _kw[_preq[0]], _kw[_preq[1]]):
                                trace.candidates_tried += 1
                                continue
                            _vals = self._apply_across_examples(
                                _pprim, _preq, _kw, len(examples))
                            trace.candidates_tried += 1
                            if _vals is None or not _mag_ok(_vals):
                                continue
                            if _vals == target:
                                return (_ordered(_pname, _preq, _vprev_expr,
                                                 _a1e, _swap), _vals)
                            if len(_vk) < _LEVEL_CAP:
                                _vkk = self._value_key(_vals)
                                if _vkk not in _vk:
                                    _vk[_vkk] = (_vals, _ordered(
                                        _pname, _preq, _vprev_expr,
                                        _a1e, _swap))
                    for _un, _up, _ur in _unary:
                        _kw = {_ur[0]: _vprev_vals}
                        _vals = self._apply_acquired_vector(
                            _up, _ur, _kw, len(examples))
                        trace.candidates_tried += 1
                        if _vals is None or not _mag_ok(_vals):
                            continue
                        if _vals == target:
                            _nexpr = Expr(op=_un,
                                          children=((_ur[0], _vprev_expr),))
                            return _nexpr, _vals
                        if len(_vk) < _LEVEL_CAP:
                            _vkk = self._value_key(_vals)
                            if _vkk not in _vk:
                                _vk[_vkk] = (_vals, Expr(
                                    op=_un,
                                    children=((_ur[0], _vprev_expr),)))
            if not _vk:
                return None
            # Union the pure _round-deep nesting chains so mixed shapes
            # (binary of nests) can build on them. The chains are few;
            # the level cap stays soft for them.
            if _round < len(_nest):
                for _nv, _ne in _nest[_round]:
                    _nkk = self._value_key(_nv)
                    if _nkk not in _vk:
                        _vk[_nkk] = (_nv, _ne)
            # Union the (_round+1)-leaf associative folds for the same
            # reason (binary of folds). Few; cap stays soft.
            for _fv, _fe in _folds.get(_round + 1, {}).values():
                _fkk = self._value_key(_fv)
                if _fkk not in _vk:
                    _vk[_fkk] = (_fv, _fe)
            _levels.append(_vk)

        # Deepening pass: if the base pass (dedicated tracks at
        # max_leaves, then the capped binary DP) missed, the shape may
        # need MORE leaves than the default budget allows. The
        # dedicated tracks are sub-exponential (nest: |unary|^N chains,
        # fold: subsets x assoc ops), so they -- and only they -- are
        # extended one level at a time, reusing already-built depths.
        # The binary DP is NOT deepened (its miss cost is
        # super-exponential; it stays capped). Each level gets a time
        # budget; if a level cannot complete inside it, deeper levels
        # (strictly more expensive) are not attempted. This is
        # parameterized deepening of the generic tracks, not a
        # leaf-count special case.
        if _v0 and max_leaves < _MULTIWAY_DEEPEN_MAX:
            _deepen_t0 = time.time()
            _nest_open = True
            for _dn in range(max_leaves + 1, _MULTIWAY_DEEPEN_MAX + 1):
                if time.time() - _deepen_t0 > _MULTIWAY_DEEPEN_TOTAL_BUDGET:
                    break
                # Folds first: O(subsets), essentially never the
                # bottleneck; they get no per-level deadline.
                _hit, _fcompleted = _extend_folds(_dn, None)
                if _hit is not None:
                    return _hit
                # Nests are O(|unary|^N): budgeted per level, and closed
                # for deeper levels once a level cannot complete. Folds
                # continue independently (a nest timeout must not block
                # a cheap fold hit at a deeper level).
                if _nest_open:
                    _nest_deadline = (time.time()
                                      + _MULTIWAY_DEEPEN_LEVEL_BUDGET)
                    _hit, _ncompleted = _extend_nest(_dn, _nest_deadline)
                    if _hit is not None:
                        return _hit
                    if not _ncompleted:
                        _nest_open = False
                if not _nest_open and not _fcompleted:
                    break
        return None

    def _quadratic_probe(self, examples, param_names, target, trace):
        """Check quadratic-in-one-param by value vectors (Horner normal form).

        The blind enumerator cannot reach multi-term polynomial shapes
        within its size budget: the minimal Horner tree for a*x^2+b*x+c
        is size 4 while GENERATE runs at max_size 3, and non-trivial
        coefficients (3, 5, ...) are not constructible from the fixed
        literal set within budget either. The pairwise/multiway probes
        cannot hypothesize this family (no acquired capability need be
        involved). This probe fits output = a*x^2+b*x+c exactly on the
        examples -- a generic numeric procedure (the degree-2 analogue
        of the linear fit already used for constant mining), verified
        on ALL examples with n>=4 so verification is non-vacuous --
        and on a verified fit constructs the Horner normal form
        add(mul(add(mul(a,x),b),x),c) by value vectors, not plan
        enumeration. Guidance-only: a hit goes through the same
        identifiability gate as any synthesized candidate, and placement
        after the multiway probe means smaller shape families still win
        when they exist. Returns (Expr, values) or None. No goal words,
        no capability ids, no target-specific recipes: the coefficients
        come from the evidence, the shape is the generic quadratic
        normal form.
        """
        from swarm_engine.cognition.constants import quadratic_fit
        _n = len(examples)
        if _n < 4:
            return None
        try:
            _add_prim = self.reg.get("add")
            _mul_prim = self.reg.get("multiply")
        except Exception:
            return None
        if _add_prim is None or _mul_prim is None:
            return None
        _add_req = [k for k, v in _add_prim.inputs.items()
                    if not v.optional]
        _mul_req = [k for k, v in _mul_prim.inputs.items()
                    if not v.optional]
        if len(_add_req) != 2 or len(_mul_req) != 2:
            return None
        for _p in param_names:
            _in_vals = [a.get(_p) for a, _ in examples]
            if not all(isinstance(v, (int, float))
                       and not isinstance(v, bool) for v in _in_vals):
                continue
            _o_vals = [o for _, o in examples]
            if not all(isinstance(v, (int, float))
                       and not isinstance(v, bool) for v in _o_vals):
                continue
            try:
                _abc = quadratic_fit(_in_vals, _o_vals)
            except Exception:
                continue
            if _abc is None:
                continue
            _a, _b, _c = _abc
            # Round the fitted coefficients to 6 decimals (same as the
            # constant miner): Gaussian elimination leaves float noise
            # (e.g. 2.0000000000000004, or 1e-17 for a true zero) that
            # would make the constructed expression miss the target by
            # an ulp and fail the exact vector check below, and would
            # defeat the degeneracy checks. Rounding is sound because
            # the vector check itself is exact -- a wrongly-rounded
            # coefficient simply does not fire the probe.
            _a, _b, _c = (round(float(v), 6) for v in (_a, _b, _c))
            # Degenerate to linear/constant: owned by the linear-fit and
            # leaf machinery, not this probe.
            if _a == 0:
                continue
            # Monomial a*x^2 (only one nonzero coefficient): the blind
            # enumerator owns size-1/2 shapes (power/multiply), so the
            # probe -- chartered for multi-term shapes beyond the blind
            # budget -- does not fire. This preserves minimality for the
            # shapes the blind loop can reach.
            if sum(1 for v in (_a, _b, _c) if v != 0) < 2:
                continue
            # Coefficients must be safe as literals.
            if any(abs(float(v)) > 10 ** 6 for v in (_a, _b, _c)):
                continue
            _x = Expr(param=_p)
            _la = Expr(literal=float(_a), is_literal=True)
            _lb = Expr(literal=float(_b), is_literal=True)
            _lc = Expr(literal=float(_c), is_literal=True)
            _ax = Expr(op="multiply",
                       children=((_mul_req[0], _la), (_mul_req[1], _x)))
            _axb = Expr(op="add",
                        children=((_add_req[0], _ax), (_add_req[1], _lb)))
            _axbx = Expr(op="multiply",
                         children=((_mul_req[0], _axb), (_mul_req[1], _x)))
            _expr = Expr(op="add",
                         children=((_add_req[0], _axbx), (_add_req[1], _lc)))
            trace.candidates_tried += 1
            _vals = self._eval_all(
                _expr, [a for a, _ in examples])
            if _vals is None or tuple(_vals) != target:
                continue
            return _expr, tuple(_vals)
        return None

    def _piecewise_univariate_probe(self, examples, param_names, target, trace):
        """1-D numeric piecewise: split on an evidence-derived threshold,
        fit each side with constant/linear/quadratic, emit if_else.

        Generic: thresholds are midpoints between consecutive sorted
        input values; branch families are the same poly fits already
        used as whole-function probes. No task name, no hardcoded
        predicate. Requires both sides nonempty and at least one
        split that verifies on ALL training examples.
        """
        from swarm_engine.cognition.representations import Expr
        from swarm_engine.cognition.constants import quadratic_fit, _close
        if self.reg.get("if_else") is None or self.reg.get("less_than") is None:
            return None
        if len(examples) < 4 or len(param_names) != 1:
            return None
        p = param_names[0]
        xs = [a.get(p) for a, _ in examples]
        ys = [o for _, o in examples]
        if not all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in xs + ys):
            return None
        uniq = sorted(set(float(v) for v in xs))
        if len(uniq) < 3:
            return None

        def _lin(xv, yv):
            if len(xv) < 2:
                if len(xv) == 1:
                    return (0.0, 0.0, float(yv[0]))
                return None
            if abs(xv[1] - xv[0]) < 1e-12:
                return None
            k = (yv[1] - yv[0]) / (xv[1] - xv[0])
            c = yv[0] - k * xv[0]
            if not all(_close(k * x + c, y) for x, y in zip(xv, yv)):
                return None
            return (0.0, float(k), float(c))

        def _fit_side(xv, yv):
            if not xv:
                return None
            if all(_close(y, yv[0]) for y in yv):
                return (0.0, 0.0, float(yv[0]))
            q = None
            if len(xv) >= 4:
                try:
                    q = quadratic_fit(xv, yv)
                except Exception:
                    q = None
            if q is not None and abs(q[0]) > 1e-12:
                return tuple(round(float(v), 6) for v in q)
            return _lin(xv, yv)

        def _expr_of(coeffs):
            c2, c1, c0 = (round(float(v), 6) for v in coeffs)
            add_p = self.reg.get("add")
            mul_p = self.reg.get("multiply")
            ar = [k for k, v in add_p.inputs.items() if not v.optional]
            mr = [k for k, v in mul_p.inputs.items() if not v.optional]
            x = Expr(param=p)
            if c2 == 0 and c1 == 0:
                return Expr(literal=float(c0), is_literal=True)
            if c2 == 0:
                if abs(c1 - 1.0) < 1e-12 and abs(c0) < 1e-12:
                    return x
                term = Expr(op="multiply",
                            children=((mr[0], Expr(literal=float(c1), is_literal=True)),
                                      (mr[1], x)))
                if abs(c0) < 1e-12:
                    return term
                return Expr(op="add", children=((ar[0], term),
                                                (ar[1], Expr(literal=float(c0), is_literal=True))))
            la = Expr(literal=float(c2), is_literal=True)
            lb = Expr(literal=float(c1), is_literal=True)
            lc = Expr(literal=float(c0), is_literal=True)
            ax = Expr(op="multiply", children=((mr[0], la), (mr[1], x)))
            axb = Expr(op="add", children=((ar[0], ax), (ar[1], lb)))
            axbx = Expr(op="multiply", children=((mr[0], axb), (mr[1], x)))
            return Expr(op="add", children=((ar[0], axbx), (ar[1], lc)))

        lt = self.reg.get("less_than")
        lt_req = [k for k, v in lt.inputs.items() if not v.optional]
        if len(lt_req) != 2:
            return None
        # Midpoints between consecutive unique inputs.
        thresholds = []
        for a, b in zip(uniq, uniq[1:]):
            thresholds.append((a + b) / 2.0)
            thresholds.append(b)  # also the right endpoint as < b
        seen_t = set()
        for t in thresholds:
            t = round(float(t), 6)
            if t in seen_t:
                continue
            seen_t.add(t)
            left_x, left_y, right_x, right_y = [], [], [], []
            for x, y in zip(xs, ys):
                if float(x) < t:
                    left_x.append(float(x)); left_y.append(float(y))
                else:
                    right_x.append(float(x)); right_y.append(float(y))
            if len(left_x) < 2 or len(right_x) < 2:
                continue
            lf = _fit_side(left_x, left_y)
            rf = _fit_side(right_x, right_y)
            if lf is None or rf is None:
                continue
            # Both sides same poly → not piecewise
            if all(_close(a, b) for a, b in zip(lf, rf)):
                continue
            lex = _expr_of(lf)
            rex = _expr_of(rf)
            if lex is None or rex is None:
                continue
            cond = Expr(op="less_than",
                        children=((lt_req[0], Expr(param=p)),
                                  (lt_req[1], Expr(literal=float(t), is_literal=True))))
            expr = Expr(op="if_else",
                        children=(("condition", cond), ("then", lex), ("otherwise", rex)))
            trace.candidates_tried += 1
            vals = self._eval_all(expr, [a for a, _ in examples])
            if vals is None:
                continue
            if tuple(vals) != target:
                # numeric tolerance
                ok = True
                for v, tgv in zip(vals, target):
                    if not _close(float(v), float(tgv)):
                        ok = False
                        break
                if not ok:
                    continue
            return expr, tuple(vals)
        return None

    def _type_partition_probe(self, examples, param_names, target, trace):
        """Dispatch on classify_shape (or Python type) of one parameter.

        Groups training examples by the shape label of a single argument.
        Each group is fitted independently from a small generic operator
        pool (identity, univariate poly, unary primitives with matching
        arity). Groups are then reassembled with nested
        if_else(equals(classify_shape(x), label), branch, ...).

        No type names are hardcoded as the *solution* — labels come from
        classify_shape on the evidence. Requires ≥2 nonempty groups and
        a full-training exact match.
        """
        from swarm_engine.cognition.representations import Expr
        from swarm_engine.cognition.constants import quadratic_fit, _close
        if len(examples) < 4 or len(param_names) != 1:
            return None
        if self.reg.get("if_else") is None or self.reg.get("equals") is None:
            return None
        p = param_names[0]
        cls_prim = self.reg.get("classify_shape")

        def _label(v):
            if cls_prim is not None:
                try:
                    return str(cls_prim.fn(value=v))
                except Exception:
                    pass
            if isinstance(v, bool):
                return "bool"
            if isinstance(v, (int, float)):
                return "scalar"
            if isinstance(v, str):
                return "text"
            if isinstance(v, list):
                return "series"
            if isinstance(v, dict):
                return "record"
            return type(v).__name__

        buckets = {}
        for args, out in examples:
            buckets.setdefault(_label(args.get(p)), []).append((args, out))
        if len(buckets) < 2:
            return None
        if any(len(g) < 1 for g in buckets.values()):
            return None

        def _poly_expr(coeffs):
            add_p = self.reg.get("add")
            mul_p = self.reg.get("multiply")
            if add_p is None or mul_p is None:
                return None
            ar = [k for k, v in add_p.inputs.items() if not v.optional]
            mr = [k for k, v in mul_p.inputs.items() if not v.optional]
            c2, c1, c0 = (round(float(v), 6) for v in coeffs)
            x = Expr(param=p)
            if c2 == 0 and c1 == 0:
                return Expr(literal=float(c0), is_literal=True)
            if c2 == 0:
                if abs(c1 - 1.0) < 1e-12 and abs(c0) < 1e-12:
                    return x
                term = Expr(op="multiply",
                            children=((mr[0], Expr(literal=float(c1), is_literal=True)),
                                      (mr[1], x)))
                if abs(c0) < 1e-12:
                    return term
                return Expr(op="add", children=((ar[0], term),
                                                (ar[1], Expr(literal=float(c0), is_literal=True))))
            la = Expr(literal=float(c2), is_literal=True)
            lb = Expr(literal=float(c1), is_literal=True)
            lc = Expr(literal=float(c0), is_literal=True)
            ax = Expr(op="multiply", children=((mr[0], la), (mr[1], x)))
            axb = Expr(op="add", children=((ar[0], ax), (ar[1], lb)))
            axbx = Expr(op="multiply", children=((mr[0], axb), (mr[1], x)))
            return Expr(op="add", children=((ar[0], axbx), (ar[1], lc)))

        def _fit_group(group):
            xs = [a.get(p) for a, _ in group]
            ys = [o for _, o in group]
            args_g = [a for a, _ in group]
            # identity
            if xs == ys:
                return Expr(param=p)
            # numeric poly on scalar inputs
            if (all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in xs)
                    and all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in ys)):
                if all(_close(float(y), float(ys[0])) for y in ys):
                    return Expr(literal=float(ys[0]), is_literal=True)
                if len(xs) >= 2:
                    if abs(float(xs[1]) - float(xs[0])) > 1e-12:
                        k = (float(ys[1]) - float(ys[0])) / (float(xs[1]) - float(xs[0]))
                        c = float(ys[0]) - k * float(xs[0])
                        if all(_close(k * float(x) + c, float(y)) for x, y in zip(xs, ys)):
                            ex = _poly_expr((0.0, k, c))
                            if ex is not None:
                                return ex
                if len(xs) >= 4:
                    q = quadratic_fit([float(v) for v in xs], [float(v) for v in ys])
                    if q is not None:
                        ex = _poly_expr(q)
                        if ex is not None:
                            return ex
            # unary primitives whose single required input accepts the value
            for name in self.reg.names():
                try:
                    prim = self.reg.get(name)
                except Exception:
                    continue
                if prim is None:
                    continue
                fam = getattr(prim, "family", "")
                if fam == "acquired":
                    continue
                if getattr(prim, "pure", True) is False:
                    continue
                if getattr(prim, "effects", ()) and any(
                        str(ef) not in ("Effect.PURE", "pure") for ef in (prim.effects or ())):
                    # still allow PURE-only
                    if not getattr(prim, "pure", False):
                        continue
                req = [k for k, v in prim.inputs.items() if not v.optional]
                if len(req) != 1:
                    continue
                key = req[0]
                ok = True
                for a, expect in group:
                    try:
                        got = prim.fn(**{key: a.get(p)})
                    except Exception:
                        ok = False
                        break
                    if got != expect:
                        if not (isinstance(got, (int, float)) and isinstance(expect, (int, float))
                                and _close(float(got), float(expect))):
                            ok = False
                            break
                if ok:
                    return Expr(op=name, children=((key, Expr(param=p)),))
            return None

        fitted = {}
        for lab, group in buckets.items():
            fx = _fit_group(group)
            if fx is None:
                return None
            fitted[lab] = fx

        # Assemble nested if_else on equals(classify_shape(x), label)
        labels = sorted(fitted.keys())
        if cls_prim is not None:
            cls_expr = Expr(op="classify_shape",
                            children=(("value", Expr(param=p)),))
        else:
            return None
        eq_req = [k for k, v in self.reg.get("equals").inputs.items() if not v.optional]
        if len(eq_req) != 2:
            return None
        expr = fitted[labels[-1]]
        for lab in reversed(labels[:-1]):
            cond = Expr(op="equals",
                        children=((eq_req[0], cls_expr),
                                  (eq_req[1], Expr(literal=lab, is_literal=True))))
            expr = Expr(op="if_else",
                        children=(("condition", cond),
                                  ("then", fitted[lab]),
                                  ("otherwise", expr)))
        trace.candidates_tried += 1
        vals = self._eval_all(expr, [a for a, _ in examples])
        if vals is None:
            return None
        if tuple(vals) != tuple(target):
            ok = True
            for v, tgv in zip(vals, target):
                if v != tgv:
                    if not (isinstance(v, (int, float)) and isinstance(tgv, (int, float))
                            and _close(float(v), float(tgv))):
                        ok = False
                        break
            if not ok:
                return None
        return expr, tuple(vals)

    def _explosive_acquired_ops(self) -> Set[str]:
        """Names of acquired composites too costly for the blind level-1 sweep.

        Admission registers each admitted portable capability as a real
        primitive (family="acquired") so future searches can compose it.
        The pollution mechanism is specifically HIGH-arity acquired
        composites (one required input per plan parameter, often 8-12):
        their level-1 blind enumeration is combinatorially explosive --
        thousands of plan-execution-cost candidates that burn the
        wall-clock before the guided levels run. An acquired op whose
        required arity is within the built-in vocabulary's maximum is
        NOT explosive (see _priority_learned_ops). This is the general
        built-in-vs-admitted-arity distinction (registry metadata set at
        admission time), never word-, law-, or benchmark-specific.
        """
        cap = self._nonlearned_max_arity()
        out = set()
        for name in self._ops:
            try:
                prim = self.reg.get(name)
            except Exception:
                continue
            if getattr(prim, "family", "") != "acquired":
                continue
            ar = sum(1 for v in prim.inputs.values() if not v.optional)
            if ar > cap:
                out.add(name)
        return out

    @property
    def _ops(self) -> List[str]:
        """Recomputed on every access rather than cached at construction —
        cheap (a linear scan over ~230-300 primitives, a few hundred dict
        lookups) relative to what a single search already costs, and it is
        what makes a primitive promoted mid-session immediately usable by
        every live synthesizer, not just ones constructed afterward.

        Optional shape-focused override (self._ops_override) may temporarily
        restrict the active set for a search without pruning the registry.
        """
        override = getattr(self, "_ops_override", None)
        if override is not None:
            return list(override)
        return self._find_pure_ops()

    def search(self, examples: Sequence[Tuple[Dict[str, Any], Any]],
              param_names: Sequence[str],
              extra_literals: Sequence[Any] = (),
              oracle: Optional[Any] = None,
              ) -> Tuple[Optional[Hypothesis], SynthesisTrace]:
        """Iterates by true ascending expression SIZE (number of primitive
        applications), not by loop-iteration count. The first version of this
        method conflated the two: each round could combine two already-large
        bank entries into something whose total size exceeded max_size,
        which is exactly how a spurious 3-application chain got returned for
        a problem whose real answer has size 2 — the round count said "2"
        but nothing enforced that the resulting expression actually was.
        Indexing the bank by exact size and, for each primitive, enumerating
        integer partitions of (target_size - 1) across its argument slots is
        what makes "level k is fully explored before level k+1" — and
        therefore minimality — actually true rather than merely documented.
        """
        self._ops_override = None  # cleared each search; may be set by shape focus
        trace = SynthesisTrace()
        if not examples:
            return None, trace

        args_list = [args for args, _ in examples]
        target = tuple(expected for _, expected in examples)
        # NOTE: the wall-clock budget starts AFTER the guidance probe
        # below (see the second `started = time.time()`), not here: the
        # probe is advisory/honesty infrastructure (relevance ranking,
        # structural hypotheses, R2'' rival detection), and its cost must
        # not starve the combinatorial search budget. (2026-09-14: the
        # F-free expansion-rival enumeration inside the probe cost ~240s
        # on a 12-input case, tripping the search budget before the
        # first level ran -- the search returned None for a solvable
        # problem.)

        # Per-search state for the identifiability machinery.
        self._alt_exprs = {}
        self._poisoned_signatures = set()
        self._stashed_signatures = set()
        self._stashed_conditionals = []
        self._search_oracle = oracle  # optional callable(args_dict)->value
        self._search_args_list = args_list
        self._search_leaf_literals = []
        self._search_literal_pool = set()
        self._cc_seen = {}
        self._cc_bools = []
        self._cc_by_type = {}

        # Fail closed on contradictory training data: the same inputs map
        # to different expected outputs, so no expression can fit them.
        # This is a property of the evidence, not a search failure, and is
        # reported as such rather than as an ordinary miss.
        contradiction = self._find_contradiction(examples)
        if contradiction is not None:
            trace.contradiction = contradiction
            trace.rejected.append(f"contradictory training data: {contradiction}")
            return None, trace

        # Input-dependency guidance: relevance ranking + exact-fit (op, inputs)
        # hypotheses, inferred from the examples' VALUES only (never names
        # or positions) by cognition/input_dependencies.py. This is search
        # ORDERING, not pruning: the pool keeps every entry and the op loop
        # keeps every op, so completeness and by-size minimality are
        # preserved exactly. When the guidance is wrong the search simply
        # continues past it at a small bounded cost.
        self._relevance_rank: Dict[str, int] = {}
        self._hypothesis_ops: List[str] = []
        try:
            from swarm_engine.cognition.input_dependencies import analyze as _analyze_deps
            # The structural probe is complete over the generic template
            # vocabulary through size 7 (B_3 value set with on-demand
            # reconstruction covers the (3,3) split); size 8 is covered
            # for balanced (3,4)/(4,3) splits with multiply roots (flat
            # divisibility-filtered witness); size 9 is covered for
            # balanced (4,4) splits with multiply roots (lazily
            # generated divisibility-filtered B_4 candidate set); size
            # 10 is covered for balanced (4,5)/(5,4) splits with
            # multiply roots (divisibility-filtered B_4 side, complete
            # size-5 witness for the other side); size 11 is covered
            # for the balanced (5,5) split with multiply roots
            # (divisibility-filtered B_5 candidate set, both sides
            # reconstructed via the complete size-5 witness).
            # Add/subtract roots and unbalanced splits stay
            # resource-constrained. Cap the probe there. The probe stays
            # advisory: hypotheses only reorder search, never prune it.
            # Backward-compatible call: a monkeypatched two-argument
            # analyze (as in adversarial tests) still works.
            _probe_cap = min(self.max_size, 11)
            try:
                _dep_report = _analyze_deps(
                    examples, list(param_names),
                    probe_max_size=_probe_cap)
            except TypeError:
                _dep_report = _analyze_deps(examples, list(param_names))
            self._relevance_rank = {
                nm: i for i, nm in enumerate(_dep_report.ranking)}
            seen_ops = set()
            for _h in _dep_report.hypotheses:
                if _h.op not in seen_ops:
                    seen_ops.add(_h.op)
                    self._hypothesis_ops.append(_h.op)
            trace.dependency_guidance = {
                "ranking": _dep_report.ranking,
                "hypotheses": [(h.op, list(h.inputs)) for h in
                               _dep_report.hypotheses],
                "proven_relevant": sorted(_dep_report.proven_relevant),
            }
            # Composite structural hypotheses: two-level exact-fit chains
            # outer(inner(x, y), z) discovered from example values by the
            # nested relational probe. These become search ORDERING below
            # (hypothesized ops tried first, hypothesized wirings'
            # partitions and pool entries sorted first). Pool MEMBERSHIP
            # and the op loop are unchanged, so completeness and by-size
            # minimality are preserved exactly -- a wrong hypothesis only
            # costs a few wasted candidates before the ordinary search
            # continues past it.
            self._structural_hyps: List[Any] = []
            for _sh in (getattr(_dep_report, "composite_hypotheses", None)
                        or []):
                _ops_needed = _sh.all_ops()
                if all(_op in self._ops for _op in _ops_needed):
                    self._structural_hyps.append(_sh)
                    # Root op before deeper ops: at the hypothesis's own
                    # size level the root wiring is what needs to fire
                    # first (smaller subexpressions are already banked by
                    # the completed smaller-size sweeps). Within one size
                    # level this is pure ordering -- minimality across
                    # sizes is untouched.
                    for _op in _ops_needed:
                        if _op not in seen_ops:
                            seen_ops.add(_op)
                            self._hypothesis_ops.append(_op)
            # Smallest hypothesized size drives the level order below.
            # A depth-1 exact-fit OpHypothesis means a size-1 solution
            # exists -- it takes precedence over any larger structural
            # hypothesis (minimality), so no jump.
            _has_depth1_exact = any(
                getattr(h, "fit_fraction", 0) == 1.0
                for h in _dep_report.hypotheses)
            if _has_depth1_exact:
                self._hyp_min_size = 1
            else:
                self._hyp_min_size = min(
                    (h.size() for h in self._structural_hyps),
                    default=None)
            trace.composite_hypotheses = [h.to_dict() for h in
                                         self._structural_hyps]
        except Exception:
            trace.dependency_guidance = {"ranking": list(param_names),
                                        "hypotheses": [],
                                        "proven_relevant": []}
            self._structural_hyps = []
            self._hyp_min_size = None

        # 2026-09-14: composition probe for the blind case. The structural
        # probe works in PRIMITIVE space and is blind to acquired
        # capabilities as composable units -- yet growth means building on
        # what was learned, so "primitive applied to acquired capabilities"
        # (e.g. snarf = add(plink, dax)) is a natural solution shape the
        # probe can never hypothesize. When the probe found nothing, check
        # directly (by value vectors, not plan enumeration): for each pure
        # binary primitive p and each ordered pair of acquired capabilities
        # (canonical bindings), whether p(acq1, acq2) hits the target. This
        # is O(A^2 * P) vector operations -- cheap -- versus the blind
        # level-3 enumeration it replaces, which is intractable in a
        # polluted registry (level 2 alone exceeds the wall-clock). If found,
        # it goes through the same identifiability gate as any synthesized
        # candidate; it is a size-3 solution, and the probe's own sweep
        # (through primitive size 3) plus Check 0 (behavioral single-cap
        # match) already rule out smaller shapes, so minimality is preserved.
        if self._hyp_min_size is None:
            _comp = self._composition_probe(examples, param_names, target,
                                            trace)
            if _comp is not None:
                _cexpr, _cvals = _comp
                if self._general_identifiability(
                        _cexpr, args_list, param_names, target, trace):
                    trace.per_level.append({
                        "target_size": 3,
                        "candidates_this_level": trace.candidates_tried,
                        "found": True,
                        "found_by": "composition-probe",
                    })
                    return self._finish(_cexpr, param_names, trace), trace
                # Gate-suppressed: fall through to the normal level loop;
                # the candidate was valid enough to probe, so the sweep
                # may still find it (or a rival) by enumeration.
        # Nesting probe: the positional variant the flat probe cannot
        # hypothesize -- one acquired capability applied to another's
        # output. Same placement logic and same identifiability gate as
        # the flat probe; simpler shapes still win when they exist.
        if self._hyp_min_size is None:
            _nest = self._nesting_probe(examples, param_names, target,
                                        trace)
            if _nest is not None:
                _nexpr, _nvals = _nest
                if self._general_identifiability(
                        _nexpr, args_list, param_names, target, trace):
                    trace.per_level.append({
                        "target_size": 3,
                        "candidates_this_level": trace.candidates_tried,
                        "found": True,
                        "found_by": "nesting-probe",
                    })
                    return self._finish(_nexpr, param_names, trace), trace
                # Gate-suppressed: fall through to the normal level loop.

        # Acquired-plus-constant probe: p(a1, c) shapes like x^2+3 that
        # the pairwise probes cannot hypothesize (they require both
        # operands acquired) and blind enumeration cannot reach once the
        # vocabulary grows. Same placement logic and identifiability gate.
        if self._hyp_min_size is None:
            _constp = self._acquired_const_probe(examples, param_names,
                                                 target, trace)
            if _constp is not None:
                _cexpr, _cvals = _constp
                if self._general_identifiability(
                        _cexpr, args_list, param_names, target, trace):
                    trace.per_level.append({
                        "target_size": _cexpr.size(),
                        "candidates_this_level": trace.candidates_tried,
                        "found": True,
                        "found_by": "acquired-const-probe",
                    })
                    return self._finish(_cexpr, param_names, trace), trace
                # Gate-suppressed: fall through to the normal level loop.

        # Multi-way probe: three-or-more-acquired-leaf compositions the
        # pairwise probes cannot hypothesize (p1(p2(a1,a2),a3),
        # a1(a2(a3(x))), and the bounded N-way generalization).
        # Same placement logic and identifiability gate; simpler shapes
        # still win because the pairwise probes ran first.
        if self._hyp_min_size is None:
            _multi = self._multiway_probe(examples, param_names, target,
                                          trace)
            if _multi is not None:
                _mexpr, _mvals = _multi
                if self._general_identifiability(
                        _mexpr, args_list, param_names, target, trace):
                    trace.per_level.append({
                        "target_size": _mexpr.size(),
                        "candidates_this_level": trace.candidates_tried,
                        "found": True,
                        "found_by": "multiway-probe",
                    })
                    return self._finish(_mexpr, param_names, trace), trace
                # Gate-suppressed: fall through to the normal level loop.

        # Quadratic probe: multi-term polynomial shapes (a*x^2+b*x+c)
        # the blind enumerator cannot reach within its size budget and
        # the acquired-capability probes cannot hypothesize. Same
        # placement logic and identifiability gate; it runs last among
        # the probes because its Horner normal form is the largest
        # shape family, so smaller families still win when they exist.
        # Minimality caveat: a smaller non-template solution is not
        # exhaustively ruled out before this probe fires; the
        # identifiability gate and held-out evidence are the backstop.
        if self._hyp_min_size is None:
            _quad = self._quadratic_probe(examples, param_names, target,
                                          trace)
            if _quad is not None:
                _qexpr, _qvals = _quad
                if self._general_identifiability(
                        _qexpr, args_list, param_names, target, trace):
                    trace.per_level.append({
                        "target_size": _qexpr.size(),
                        "candidates_this_level": trace.candidates_tried,
                        "found": True,
                        "found_by": "quadratic-probe",
                    })
                    trace.minimality_caveat = (
                        "quadratic-probe: Horner normal form returned; "
                        "smaller non-template solutions not exhaustively "
                        "ruled out before the probe fired")
                    return self._finish(_qexpr, param_names, trace), trace
                # Gate-suppressed: fall through to the normal level loop.

        # Piecewise univariate probe: split on evidence thresholds and
        # fit each side. Same identifiability gate as other probes.
        if self._hyp_min_size is None:
            _pw = self._piecewise_univariate_probe(
                examples, param_names, target, trace)
            if _pw is not None:
                _pexpr, _pvals = _pw
                if self._general_identifiability(
                        _pexpr, args_list, param_names, target, trace):
                    trace.per_level.append({
                        "target_size": _pexpr.size(),
                        "candidates_this_level": trace.candidates_tried,
                        "found": True,
                        "found_by": "piecewise-univariate-probe",
                    })
                    trace.minimality_caveat = (
                        "piecewise-univariate-probe: threshold split + "
                        "per-side poly fit; smaller solutions not "
                        "exhaustively ruled out")
                    return self._finish(_pexpr, param_names, trace), trace

        # Type/shape partition probe: dispatch via classify_shape.
        if self._hyp_min_size is None:
            _tp = self._type_partition_probe(
                examples, param_names, target, trace)
            if _tp is not None:
                _texpr, _tvals = _tp
                if self._general_identifiability(
                        _texpr, args_list, param_names, target, trace):
                    trace.per_level.append({
                        "target_size": _texpr.size(),
                        "candidates_this_level": trace.candidates_tried,
                        "found": True,
                        "found_by": "type-partition-probe",
                    })
                    trace.minimality_caveat = (
                        "type-partition-probe: classify_shape dispatch + "
                        "per-partition fit")
                    return self._finish(_texpr, param_names, trace), trace

        # Evidence-derived constants, mined once per search from the
        # examples' own numeric relationships (consistent ratios and
        # differences, verified linear fits, gcd-moduli, threshold
        # midpoints, branch-constant candidates -- see
        # cognition/constants.py). The miner is generic: it derives
        # whatever the evidence implies, so a scaling factor of 100 or
        # a threshold of 50 enters the search as a leaf with no
        # task-specific knowledge anywhere. The pool is stored on self
        # for the identifiability gate's literal-perturbation rivals;
        # provenance is recorded on the trace for audit.
        mined = mine_constants(examples, param_names)
        trace.mined_constants = [
            {"value": m.value, "provenance": m.provenance,
             "tier": m.tier, "support": m.support} for m in mined]
        # Full leaf vocabulary (may include non-numeric caller-supplied
        # literals, seeded verbatim as before) and the numeric float
        # pool the identifiability gate perturbs through.
        self._search_leaf_literals = list({
            *self.LITERAL_CONSTANTS, *(m.value for m in mined),
            *extra_literals})
        # Observed output values are legitimate constants (branch
        # results). Include them generically so residual/then branches
        # can reference them without requiring a mined relationship.
        for _, _out in examples:
            # v40: any hashable scalar observed output (str/bool/num) is a
            # legitimate branch/result constant. Numeric-only seeding starved
            # conditional→string completions (if_else branches never entered
            # the leaf bank).
            if isinstance(_out, (list, dict, set)):
                continue
            try:
                hash(_out)
            except TypeError:
                continue
            if _out not in self._search_leaf_literals:
                self._search_leaf_literals.append(_out)
        # Structural identity elements derived from observed output shape.
        # If every example output is a list, seed [] so list constructors
        # (append/prepend) can build from the empty list without requiring
        # the empty list to appear as an example value. Same for dict/{}.
        # Shape-derived only — does not encode any particular composition.
        try:
            _outs = [exp for _, exp in examples]
            if _outs and all(isinstance(v, list) for v in _outs):
                if [] not in self._search_leaf_literals:
                    self._search_leaf_literals.append([])
            if _outs and all(isinstance(v, dict) for v in _outs):
                if {} not in self._search_leaf_literals:
                    self._search_leaf_literals.append({})
        except Exception:
            pass
        self._search_literal_pool = set()
        for v in self._search_leaf_literals:
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                self._search_literal_pool.add(float(v))

        # Shape-focused operator set (completeness-preserving fallback):
        # When all example outputs share a Kind K, restrict the active op
        # list to ops that can produce K or ANY, plus all acquired.* ops.
        # Runtime evidence: full 170-op registry exhausts 300k candidates
        # without finding expressible list structure; a shape-focused set
        # discovers it in ~2k. Non-matching pure ops remain available via
        # explicit fallback (restore full _ops and continue) if focused
        # search fails — implemented as: focused first by replacing _ops
        # for this search when the reduction is material.
        try:
            from swarm_engine.primitives.core import infer as _inf, Kind as _K
            _outs = [exp for _, exp in examples]
            _kinds = []
            for _v in _outs:
                try:
                    _kinds.append(_inf(_v).kind)
                except Exception:
                    _kinds.append(None)
            if _kinds and all(k is not None and k == _kinds[0] for k in _kinds):
                _tk = _kinds[0]
                _full = list(self._ops)
                def _shape_relevant(op, kind=_tk):
                    # Acquired/promoted capabilities are identified by
                    # registry FAMILY metadata, not by name prefix. The
                    # acquisition orchestrator registers admitted
                    # capabilities under goal-derived names (e.g.
                    # "zib_the_value") with family="acquired"; a
                    # startswith("acquired.") test misses every one of
                    # them and silently drops them from the focused set,
                    # making acquired composition unreachable whenever
                    # the shape focus is active. Family is the canonical
                    # marker (the "acquired." prefix is only one of
                    # several binding paths).
                    try:
                        _prim = self.reg.get(op)
                    except Exception:
                        _prim = None
                    if _prim is not None and getattr(
                            _prim, "family", "") in ("acquired", "promoted"):
                        return True
                    if str(op).startswith("acquired."):
                        return True
                    prim = _prim
                    if prim is None:
                        return False
                    try:
                        ok = prim.output.kind
                    except Exception:
                        return True
                    if ok == kind or ok == _K.ANY:
                        return True
                    # v40: BOOL-producing ops are predicate scaffolding for
                    # conditional completion of ANY output kind (e.g. STR via
                    # if_else). Without them, shape-focus on STR/etc. drops
                    # equals/not_equals and conditional→scalar is unreachable.
                    if ok == _K.BOOL:
                        return True
                    # Arithmetic core + modulo: feeds numeric predicates
                    # (equals(modulo(x,2),0)) used by conditional completion.
                    # Kept for every target kind — small, generic set.
                    _arith_core = {"add", "subtract", "multiply", "divide", "modulo"}
                    if op in _arith_core:
                        return True
                    return False
                _focused = [o for o in _full if _shape_relevant(o)]
                if 0 < len(_focused) < len(_full):
                    self._ops_override = _focused
                    if not hasattr(trace, "dependency_guidance") or trace.dependency_guidance is None:
                        trace.dependency_guidance = {}
                    trace.dependency_guidance["shape_focused_ops"] = len(_focused)
                    trace.dependency_guidance["shape_focused_kind"] = str(_tk)
        except Exception:
            pass

        # The wall-clock budget bounds the combinatorial search below, not
        # the guidance probe above: probe cost (ranking, structural
        # hypotheses, rival detection) is advisory/honesty infrastructure
        # and must not starve the search. Without this, an expensive
        # probe trips the budget before the first level runs and the
        # search returns None for a solvable problem.
        started = time.time()
        bank_by_size: Dict[int, List[Tuple[Expr, Tuple[Any, ...], Any]]] = {}
        seen_value_keys = set()
        # Found directly, from real per-op dedup measurement: boolean-
        # output primitives (contains, equals, greater_than, etc. — 31 of
        # 322 primitives) dominated dedup waste at ~35% combined, because
        # the space of possible outputs for a boolean-output candidate is
        # bounded and small: exactly 2^n_examples distinct value-tuples
        # (e.g. 8 for 3 examples), not the unbounded range a numeric
        # primitive has. Once every reachable boolean tuple has actually
        # been found, EVERY further boolean-output candidate is
        # mathematically guaranteed to be a duplicate — this is not a
        # heuristic, it is a closed combinatorial fact, so skipping them
        # entirely (not even calling the primitive) cannot exclude a
        # genuinely reachable value.
        bool_tuples_seen: set = set()
        bool_space_size = 2 ** len(examples) if examples else 0
        # A memoization cache keyed by (op, argument values) was tried here
        # — reasoned to be safe since every composable primitive is
        # verified pure, and motivated by a real measurement showing 64%
        # of "candidates tried" at one search level were immediate dedup
        # rejections. Built, tested for correctness (found the same
        # answers on every known case), then measured with a clean,
        # controlled A/B (3 trials each, cache-bypassing patch vs
        # unmodified): WITH the cache averaged 2.91s, WITHOUT averaged
        # 2.80s — the cache was measurably SLOWER, not faster. Only ~5.4%
        # of candidates were actual cache hits (most dedup collisions are
        # DIFFERENT (op, values) pairs converging on the same final value,
        # not the same pair reached twice), so the per-candidate cost of
        # constructing and hashing a cache key on every one of the other
        # ~95% outweighed what little the hits saved, since a primitive
        # call here is cheap arithmetic to begin with. Removed rather than
        # kept as a false improvement, per "do not blindly optimize based
        # on intuition" — the instrumentation that surfaced this is kept;
        # the optimization it motivated is not, because it did not survive
        # being measured properly.

        def try_add(expr: Expr, values: Tuple[Any, ...], size: int) -> bool:
            key = self._value_key(values)
            if key in seen_value_keys:
                trace.dedup_hits += 1
                op_key = expr.op if not expr.is_leaf() else "<leaf>"
                trace.dedup_by_op[op_key] = trace.dedup_by_op.get(op_key, 0) + 1
                # Observational-equivalence bookkeeping: the bank keeps one
                # representative per value tuple, which makes "which
                # predicate?" look decided when it is not. Keep a bounded
                # set of structurally distinct alternatives per tuple so
                # the identifiability gate can see the rival explanations
                # the dedup hid. Bounded and generic: no domain vocabulary,
                # just structural identity of the system's own candidates.
                bucket = self._alt_exprs.get(key)
                if bucket is not None and len(bucket) < (12 if all(isinstance(v, bool) for v in values) else 6):
                    canon = expr.canonical()
                    if all(e.canonical() != canon for e in bucket):
                        bucket.append(expr)
                # Acquired ops stay bank-visible on value collision for composition.
                _prim = self.reg.get(expr.op) if not expr.is_leaf() else None
                if (_prim is not None
                        and getattr(_prim, "family", "") in ("acquired", "promoted")
                        and not all(isinstance(v, bool) for v in values)):
                    out_type = infer(values[0]) if values else None
                    bank_by_size.setdefault(size, []).append((expr, values, out_type))
                    return values == target
                return False
            seen_value_keys.add(key)
            # The first expression to produce a tuple is the bank's
            # representative; it heads the tuple's equivalence class.
            self._alt_exprs[key] = [expr]
            if all(isinstance(v, bool) for v in values):
                bool_tuples_seen.add(key)
            # Reject before the value can ever be fed to a later primitive as
            # an argument — this is what actually stops the hang, since a
            # value that never enters the bank can never reach
            # seeded_random's count parameter or anything like it.
            if not all(self._value_is_safe(v) for v in values):
                trace.unsafe_value_rejections += 1
                trace.rejected.append(
                    f"{expr.ops_used()[-1] if expr.ops_used() else 'leaf'}: "
                    f"produced a value outside the safety bound, excluded "
                    f"from further composition")
                return False
            out_type = infer(values[0]) if values else None
            bank_by_size.setdefault(size, []).append((expr, values, out_type))
            return values == target

        for name in param_names:
            leaf = Expr(param=name)
            values = self._eval_all(leaf, args_list)
            if values is None:
                continue
            if try_add(leaf, values, 0):
                # A fitting leaf is still subject to identifiability: a
                # constant-valued parameter is indistinguishable from a
                # literal, and two always-equal parameters from each
                # other. Fail closed rather than return an arbitrary one.
                if not self._general_identifiability(
                        leaf, args_list, param_names, target, trace):
                    continue
                return self._finish(leaf, param_names, trace), trace

        # Literal leaves: the small fixed set, caller-supplied extra
        # literals, and the mined evidence-derived constants.
        for literal_value in sorted(self._search_leaf_literals, key=repr):
            leaf = Expr(literal=literal_value, is_literal=True)
            values = tuple(literal_value for _ in args_list)
            if try_add(leaf, values, 0):
                if not self._general_identifiability(
                        leaf, args_list, param_names, target, trace):
                    continue
                return self._finish(leaf, param_names, trace), trace

        # Seed boolean-producing comparisons between parameters and
        # search literals so conditional completion can see predicates.
        # Generic: every pure comparison op in the registry × each
        # (param, literal) pair — not a task-specific threshold recipe.
        # Without this, bool-output ops are deprioritized when the target
        # Kind is non-bool and often never enter the bank before budget
        # exhaustion, leaving _find_conditional_completions with an empty
        # boolean pool.
        _cmp_ops = []
        for _opn in self._find_pure_ops():
            _p = self.reg.get(_opn)
            if _p is None:
                continue
            try:
                if _p.output.kind != Kind.BOOL:
                    continue
            except Exception:
                continue
            _req = [k for k, v in _p.inputs.items() if not v.optional]
            if len(_req) == 2:
                _cmp_ops.append((_opn, _req[0], _req[1]))
        for _opn, _a, _b in _cmp_ops:
            _prim = self.reg.get(_opn)
            if _prim is None:
                continue
            for _pname in param_names:
                # Sample one param value for type acceptance check
                _sample = None
                for _args, _ in examples:
                    if _pname in _args:
                        _sample = _args[_pname]
                        break
                if _sample is None:
                    continue
                _pt = infer(_sample)
                # Prefer observed input values as comparison thresholds
                # before midpoints/outputs so train-equivalent predicates
                # that pin boundaries at actual sample points enter the
                # bank (and _alt_exprs rivals) rather than being crowded
                # out by midpoint-only variants of the same mask.
                _obs_vals = []
                _seen_obs = set()
                for _a0 in args_list:
                    for _pv in _a0.values():
                        if isinstance(_pv, (int, float)) and not isinstance(_pv, bool):
                            if _pv not in _seen_obs:
                                _seen_obs.add(_pv)
                                _obs_vals.append(_pv)
                _other = [v for v in self._search_leaf_literals
                          if isinstance(v, (int, float)) and not isinstance(v, bool)
                          and v not in _seen_obs]
                _seed_lits = list(_obs_vals) + list(_other)[:24]
                for _lit in _seed_lits:
                    if not isinstance(_lit, (int, float)) or isinstance(_lit, bool):
                        continue
                    _lt = infer(_lit)
                    # Only seed bindings that satisfy the op's declared
                    # input types -- prevents and(x, 1) style Exprs that
                    # evaluate via Python truthiness but fail Composer.
                    if not (_prim.inputs[_a].accepts(_pt)
                            and _prim.inputs[_b].accepts(_lt)):
                        # try swapped types for swapped binding below
                        pass
                    else:
                        _expr = Expr(
                            op=_opn,
                            children=(
                                (_a, Expr(param=_pname)),
                                (_b, Expr(literal=_lit, is_literal=True)),
                            ),
                        )
                        _vals = self._eval_all(_expr, args_list)
                        if _vals is not None:
                            if try_add(_expr, _vals, _expr.size()):
                                if not self._general_identifiability(
                                        _expr, args_list, param_names, target, trace):
                                    continue
                                return self._finish(_expr, param_names, trace), trace
                    if _a != _b and (_prim.inputs[_a].accepts(_lt)
                                     and _prim.inputs[_b].accepts(_pt)):
                        _expr2 = Expr(
                            op=_opn,
                            children=(
                                (_a, Expr(literal=_lit, is_literal=True)),
                                (_b, Expr(param=_pname)),
                            ),
                        )
                        _vals2 = self._eval_all(_expr2, args_list)
                        if _vals2 is None:
                            continue
                        if try_add(_expr2, _vals2, _expr2.size()):
                            if not self._general_identifiability(
                                    _expr2, args_list, param_names, target, trace):
                                continue
                            return self._finish(_expr2, param_names, trace), trace


        # Seed arithmetic param×literal applications for pure numeric
        # binary ops so branch expressions (e.g. x*2, x+1) enter the
        # bank even when the main combinatorial sweep spends its budget
        # on high-arity ops such as if_else. Generic: every pure op with
        # numeric output and two required inputs, over (param, literal)
        # pairs from the search literal pool -- not a task-specific recipe.
        _arith_ops = []
        for _opn in self._find_pure_ops():
            _p = self.reg.get(_opn)
            if _p is None:
                continue
            try:
                from swarm_engine.primitives.core import Kind as _K
                if _p.output.kind not in (_K.INT, _K.FLOAT, _K.NUM):
                    continue
            except Exception:
                continue
            _req = [k for k, v in _p.inputs.items() if not v.optional]
            if len(_req) != 2:
                continue
            _arith_ops.append((_opn, _req[0], _req[1]))
        for _opn, _a, _b in _arith_ops:
            for _pname in param_names:
                for _lit in list(self._search_leaf_literals)[:12]:
                    if not isinstance(_lit, (int, float)) or isinstance(_lit, bool):
                        continue
                    for _children in (
                        ((_a, Expr(param=_pname)),
                         (_b, Expr(literal=_lit, is_literal=True))),
                        ((_a, Expr(literal=_lit, is_literal=True)),
                         (_b, Expr(param=_pname))),
                    ):
                        _expr = Expr(op=_opn, children=_children)
                        _vals = self._eval_all(_expr, args_list)
                        if _vals is None:
                            continue
                        if try_add(_expr, _vals, _expr.size()):
                            if not self._general_identifiability(
                                    _expr, args_list, param_names, target, trace):
                                continue
                            return self._finish(_expr, param_names, trace), trace

        _level_start_time = started
        _level_start_candidates = 0
        _level_start_dedup = 0
        _level_start_unsafe = 0
        _level_start_bank_size = sum(len(v) for v in bank_by_size.values())

        # --- Staged hypothesis construction ---
        # Build the DEFERRED-LEVEL subtrees of each structural hypothesis
        # bottom-up through the normal try_add path (dedup, safety,
        # banking; candidates counted honestly toward the budget). This
        # ensures hypothesized intermediates EXIST in the bank when the
        # guided level fires -- critical for chain hypotheses whose
        # intermediates live at a deferred blind level (e.g. a size-2
        # subtree needed at level 3 while level 2's blind sweep is
        # deferred). Only deferred-level subtrees are staged; anything
        # at a level the blind sweep will reach is left for the sweep
        # (avoiding redundant work on spurious-but-equivalent
        # hypotheses). Staging is construction, not admission: nothing
        # is returned without the identifiability gate, and the level
        # loop below still runs for minimality.
        _s_min_stage = getattr(self, "_hyp_min_size", None)
        # Admission-pollution deferral: when EXPLOSIVE admitted composites
        # are present (family="acquired" with required arity above the
        # built-in vocabulary's maximum), their blind level-1 sweep is no
        # longer cheap -- each burns its full per-op combination budget
        # with plan-execution-cost evaluations (measured: one 12-input
        # admitted composite consumed 23s of a 20s wall-clock budget at
        # level 1, before the probe-guided level 2 was ever attempted;
        # hypothesis-op prioritization is within-level only and cannot
        # skip that sweep). The deferral splits level 1 around the
        # probe-guided level s_min: a LEARNED-SHORTCUT pre-pass (admitted /
        # promoted ops within the built-in arity envelope -- the system's
        # own verified cached knowledge, cheap to enumerate) runs FIRST,
        # so a learned shortcut still wins immediately; then the guided
        # level s_min, so a guided solution is found before explosive
        # composites can burn the wall-clock; then the level-1 RESIDUAL
        # (all remaining ops, built-ins and explosive), so completeness
        # is deferred, not pruned. Order: [1-learned, s_min-guided,
        # 1-residual, ...rest]. Ordering only: every level is still swept
        # with the same pools, partitions, and caps; the bank is shared
        # across the reorder. Minimality caveat recorded on the trace.
        #
        # 2026-09-14 generalization: the original gate (_s_min_stage == 2)
        # only deferred explosive ops when the probe found a size-2
        # hypothesis. Demonstrated failure (growth EP6 "plink"): the probe
        # found a size-3 hypothesis, so the blind level-1 sweep enumerated
        # all 7 explosive acquired ops (~21k plan-execution-cost
        # candidates, 21.7s) and tripped the 20s wall-clock before the
        # guided level 3 ran -- the same pollution mechanism at a larger
        # hypothesis size (fresh-registry control solves plink in 1.7s).
        # The gate now fires whenever the probe produced a hypothesis AND
        # explosive ops are present, whatever s_min is. The no-hypothesis
        # blind-search case remains a documented residual (no guided
        # level exists to order around; needs budget-aware op scheduling,
        # a new subsystem).
        _explosive_ops = self._explosive_acquired_ops()
        _learned_prepass_ops = self._priority_learned_ops()
        _acquired_defer = (
            _s_min_stage is not None and _s_min_stage >= 2
            and bool(_explosive_ops) and _s_min_stage <= self.max_size)
        # 2026-09-14 (blind-case): the probe produced NO structural
        # hypothesis, but explosive acquired ops are present. Without a
        # guided level to order around, the blind level-1 sweep
        # enumerates thousands of expensive acquired-plan candidates and
        # trips the wall-clock before deeper levels run -- observed
        # directly on the snarf growth objective (plink + dax, a size-3
        # solution): 15,000 candidates / ~22s at level 1, levels 2+
        # never ran, search returned None. This is the documented
        # no-hypothesis residual; the repair is the same ordering-only
        # principle as the guided deferral: split level 1 into a cheap
        # first pass (primitives and non-explosive acquired ops) and an
        # explosive residual that runs AFTER the deeper levels. A
        # nominal-size-1 solution via an explosive op is largely
        # pre-screened before synthesis runs (Check 0 behavioral match
        # over the capability store; the probe's depth-1 exact-fit
        # check), so deferring it is safe in practice and strictly
        # better than starving every deeper level. Ordering only --
        # nothing is pruned.
        _blind_defer = (_s_min_stage is None and bool(_explosive_ops))
        _level1_pass = 0  # counts level-1 sweeps under the deferral split
        _defer_from = 1 if _acquired_defer else 2
        _deferred_sizes = set(range(_defer_from, _s_min_stage)) \
            if _s_min_stage is not None and \
            (_s_min_stage >= 3 or _acquired_defer) else set()
        _to_stage: List[Any] = []
        _seen_stage_keys = set()

        def _collect_deferred(tree) -> None:
            if isinstance(tree, str):
                return
            # Canonical key for dedup: structurally identical subtrees
            # (common under spurious-equivalent hypotheses) stage once.
            key = repr(sorted(tree.to_dict().items())) if hasattr(
                tree, "to_dict") else repr(tree)
            if tree.size() in _deferred_sizes and \
                    key not in _seen_stage_keys:
                _seen_stage_keys.add(key)
                _to_stage.append(tree)
            for c in tree.children:
                _collect_deferred(c)

        for _h in self._structural_hyps:
            _collect_deferred(_h)

        def _stage_tree(tree):
            """Build tree bottom-up. Returns (expr, values) or (None, None)."""
            if isinstance(tree, str):
                expr = Expr(param=tree)
                values = self._eval_all(expr, args_list)
                return (expr, tuple(values)) if values is not None else \
                    (None, None)
            child_exprs = []
            child_vals = []
            for c in tree.children:
                ce, cv = _stage_tree(c)
                if ce is None:
                    return None, None
                child_exprs.append(ce)
                child_vals.append(cv)
            prim = self.reg.get(tree.op)
            if prim is None:
                return None, None
            required = [k for k, v in prim.inputs.items()
                        if not v.optional]
            if len(required) != len(child_exprs):
                return None, None
            # Type-check args the way the slot pools do.
            for arg_name, cv in zip(required, child_vals):
                arg_type = prim.inputs[arg_name]
                try:
                    t = infer(cv[0]) if cv else None
                    if t is not None and not arg_type.accepts(t):
                        return None, None
                except Exception:
                    return None, None
            kwargs_values = {name: cv for name, cv in
                             zip(required, child_vals)}
            values = self._apply_across_examples(
                prim, required, kwargs_values, len(examples))
            if values is None:
                return None, None
            new_expr = Expr(op=tree.op, children=tuple(
                (name, ce) for name, ce in zip(required, child_exprs)))
            if trace.candidates_tried >= self.max_candidates:
                return None, None
            trace.candidates_tried += 1
            try_add(new_expr, tuple(values), new_expr.size())
            return new_expr, tuple(values)

        _staged = 0
        for _t in _to_stage:
            if trace.candidates_tried >= self.max_candidates:
                break
            _se, _sv = _stage_tree(_t)
            if _se is not None:
                _staged += 1
        trace.staged_hypothesis_nodes = _staged

        # Hierarchical level order. When the structural probe found a
        # size-s_min >= 3 hypothesis (and no smaller one), the blind
        # sweep of levels 2..s_min-1 is DEFERRED until after the guided
        # s_min level: a full blind sweep of those levels exceeds the
        # remaining candidate budget, while the probe's negative result
        # at smaller sizes is the template-vocabulary minimality
        # evidence. If the guided level fails to produce an admissible
        # candidate, the deferred levels still run blind with whatever
        # budget remains (honest fallback -- completeness is deferred,
        # not pruned). If it succeeds, the trace records the minimality
        # caveat explicitly: size <= 1 is proven by the complete sweep,
        # size-2 template compositions are ruled out by the probe, but
        # size-2 NON-template compositions are not exhaustively ruled
        # out. Held-out evidence and the revocation lifecycle are the
        # backstop for that residual.
        _level_order = list(range(1, self.max_size + 1))
        _s_min = getattr(self, "_hyp_min_size", None)
        if _acquired_defer:
            # Admission-pollution deferral, generalized to any probe
            # hypothesis size (see staging above): level 1 is split
            # around the probe-guided level _s_min. The learned pre-pass
            # (admitted/promoted shortcuts within the built-in arity
            # envelope) runs first, so a learned shortcut still wins
            # immediately; then the guided level _s_min, so a guided
            # solution is found before explosive composites can burn the
            # wall-clock; then the level-1 residual (everything else),
            # so completeness is deferred, not pruned.
            # Order: [1-learned, s_min-guided, 1-residual, ...rest].
            _level_order = [1, _s_min, 1] + [lv for lv in _level_order
                                             if lv not in (1, _s_min)]
            trace.levels_deferred = [1] + [lv for lv in range(2, _s_min)]
            trace.minimality_caveat = (
                "admission-pollution deferral: level-1 blind sweep split "
                "around probe-guided level %d; learned-shortcut pre-pass "
                "(admitted/promoted ops within the built-in arity "
                "envelope) ran before level %d, residual level-1 sweep "
                "(built-ins and explosive acquired composites) deferred "
                "until after; a nominal-size-1 solution via a residual "
                "op is not ruled out before the level-%d result is "
                "returned; deferred-level hypothesis subtrees staged, "
                "not skipped; blind sweep of level(s) %s deferred until "
                "after the guided level"
                % (_s_min, _s_min, _s_min,
                   [lv for lv in range(2, _s_min)] or "none"))
        elif _s_min is not None and 3 <= _s_min <= self.max_size:
            _level_order = [1, _s_min] + [lv for lv in _level_order
                                          if lv not in (1, _s_min)]
            trace.levels_deferred = [lv for lv in range(2, _s_min)]
            trace.minimality_caveat = (
                "structural-hypothesis jump to size %d: blind sweep of "
                "level(s) %s deferred; minimality proven for size <= 1 "
                "(complete sweep) and size-2 template compositions "
                "(structural probe negative); size-2 non-template "
                "compositions not exhaustively ruled out"
                % (_s_min, trace.levels_deferred))
        elif _blind_defer:
            # Blind-case admission-pollution deferral (see above): cheap
            # level-1 first, then the deeper levels in order, then the
            # explosive level-1 residual last. Order:
            # [1-cheap, 2, 3, ..., max_size, 1-explosive].
            _level_order = [1] + [lv for lv in _level_order if lv != 1] + [1]
            trace.levels_deferred = ["1-explosive"]
            trace.minimality_caveat = (
                "blind-case admission-pollution deferral: no structural "
                "hypothesis, explosive acquired ops present; level-1 "
                "sweep split into a cheap first pass (primitives and "
                "non-explosive acquired ops) and an explosive residual "
                "deferred until after all deeper levels; a nominal-size-1 "
                "solution via an explosive acquired op is not ruled out "
                "before a deeper-level result is returned; deferred, not "
                "pruned")
        _has_acquired = any(
            getattr(self.reg.get(n), "family", "") in ("acquired", "promoted")
            for n in self.reg.names())
        _size1_share = max(400, self.max_candidates // 5) if (
            _has_acquired or self.max_size > 2) else None
        _prev_size: Optional[int] = None
        for target_size in _level_order:
            _stop_size = False
            if _prev_size is not None:
                trace.per_level.append({
                    "target_size": _prev_size,
                    "candidates_this_level": trace.candidates_tried - _level_start_candidates,
                    "seconds_this_level": round(time.time() - _level_start_time, 3),
                    "dedup_hits_this_level": trace.dedup_hits - _level_start_dedup,
                    "unsafe_rejections_this_level": trace.unsafe_value_rejections - _level_start_unsafe,
                    "bank_size_after": sum(len(v) for v in bank_by_size.values()),
                })
                _level_start_time = time.time()
                _level_start_candidates = trace.candidates_tried
                _level_start_dedup = trace.dedup_hits
                _level_start_unsafe = trace.unsafe_value_rejections
            if time.time() - started > self.wall_clock_limit_s:
                trace.rejected.append(
                    f"wall-clock limit ({self.wall_clock_limit_s}s) reached; "
                    f"stopped rather than continuing an open-ended search")
                return None, trace
            # Cooperation point (one per level): an operator stop/pause
            # takes effect here rather than only at the wall-clock bound.
            # No-op when no control is installed.
            checkpoint("synthesize:level")
            trace.depths_explored += 1
            # Tie-break by a stable hash of the op name, not by the name
            # itself. Python's sort is stable, so with a neutral/cold bias
            # (every score tied at the same default), sorting purely by
            # score silently falls back to preserving _ops's original
            # order — which is alphabetical, since _ops is built from
            # registry.names(), itself sorted(). That means every cold
            # search was *systematically* biased toward primitives whose
            # names start with early letters, for a reason with zero
            # connection to usefulness. Found directly: `subtract` (rank
            # 199/228) and `modulo` (rank 129/228) were both excluded from
            # every pool width up to 80, and a solution requiring both
            # together was unreachable at any practical pool size as a
            # result — not because the composition doesn't exist, but
            # because alphabetical position pushed two needed primitives
            # out of contention together. A stable hash keeps ordering
            # deterministic (same search, same order, every run) without
            # that bias.
            ordered_ops = sorted(
                self._ops,
                key=lambda o: (-self.bias.score_op(o), self._stable_tiebreak(o)))
            if _has_acquired:
                _arith = {"add", "subtract", "multiply", "divide"}
                _front = [o for o in ordered_ops if o in _arith]
                if _front:
                    _fs = set(_front)
                    ordered_ops = _front + [o for o in ordered_ops if o not in _fs]
                # Learned-root priority: non-explosive acquired/promoted
                # ops (the system's own verified knowledge) are tried as
                # roots BEFORE built-ins. Symmetric with _entry_priority's
                # boost for acquired-rooted exprs as arguments; without
                # it, nested compositions (acquired(acquired(x))) sort by
                # hash tiebreak (observed at position 212/234) and the
                # wall-clock budget expires before they are ever tried as
                # roots. Explosive acquired ops are excluded -- their
                # deferral is handled separately below. Family-based,
                # never word- or law-specific.
                _learned_front = [o for o in ordered_ops
                                  if o in _learned_prepass_ops]
                if _learned_front:
                    _lfs = set(_learned_front)
                    ordered_ops = _learned_front + [
                        o for o in ordered_ops if o not in _lfs]

            # Output-shape guidance: when all example outputs share a Kind
            # (e.g. list / str / num), prioritize ops whose declared output
            # matches that Kind. Ordering only — full completeness retained.
            # Derived solely from observed example values, not from any
            # expected composition structure.
            try:
                from swarm_engine.primitives.core import infer as _infer_kind
                _out_vals = [exp for _, exp in examples]
                if _out_vals:
                    _kinds = []
                    for _v in _out_vals:
                        try:
                            _kinds.append(_infer_kind(_v).kind)
                        except Exception:
                            _kinds.append(None)
                    if _kinds and all(k is not None and k == _kinds[0] for k in _kinds):
                        _target_kind = _kinds[0]
                        def _out_matches(op_name, kind=_target_kind):
                            prim = self.reg.get(op_name)
                            if prim is None:
                                return False
                            try:
                                ok = prim.output.kind
                                if ok == kind:
                                    return True
                                # Kind.ANY output means "can produce any
                                # kind" (e.g. acquired capabilities whose
                                # output type was not inferred). Such ops
                                # must NOT be demoted behind kind-matching
                                # ops -- otherwise acquired capabilities
                                # (output=ANY) are systematically pushed
                                # late and nested compositions become
                                # unreachable within the wall-clock budget.
                                # Symmetric with the shape-focused
                                # _ops_override filter, which already
                                # treats ANY as matching.
                                if ok == Kind.ANY:
                                    return True
                                # Numeric lattice: NUM ops (add/multiply)
                                # must remain first-class for INT/FLOAT
                                # targets; otherwise shape prioritization
                                # defers them behind gcd/lcm/ceil and they
                                # never enter the bank before budget ends.
                                _num = {Kind.INT, Kind.FLOAT, Kind.NUM}
                                return ok in _num and kind in _num
                            except Exception:
                                return False
                        _shape_ops = [o for o in ordered_ops if _out_matches(o)]
                        if _shape_ops:
                            _shape_set = set(_shape_ops)
                            ordered_ops = _shape_ops + [o for o in ordered_ops
                                                        if o not in _shape_set]
            except Exception:
                pass
            # Dependency-guided op prioritization: ops that the exact
            # relational probe found exactly fitting the examples (with the
            # relevant inputs) are tried first. Evidence-driven search
            # guidance, not a decision -- every op is still tried if the
            # hypotheses miss, so completeness is preserved.
            _hyp_ops = [o for o in getattr(self, "_hypothesis_ops", [])
                        if o in self._ops]
            if _hyp_ops:
                _hyp_set = set(_hyp_ops)
                ordered_ops = _hyp_ops + [o for o in ordered_ops
                                          if o not in _hyp_set]
            if target_size == 1 and (_acquired_defer or _blind_defer):
                # Learned-shortcut split of the deferred level 1 (see
                # staging above): first pass enumerates only the learned
                # pre-pass ops (admitted/promoted shortcuts within the
                # built-in arity envelope); second pass enumerates the
                # residual (everything else). Ordering only -- the union
                # is the full level-1 op set.
                # 2026-09-14 (blind-case): first pass enumerates the
                # cheap ops (everything except the explosive acquired
                # composites); the explosive residual runs after the
                # deeper levels (see level-order construction above).
                if _acquired_defer:
                    _cheap = _learned_prepass_ops
                else:
                    _cheap = None  # blind-case: cheap == non-explosive
                if _level1_pass == 0:
                    ordered_ops = [o for o in ordered_ops
                                   if (o in _cheap if _cheap is not None
                                       else o not in _explosive_ops)]
                    if _blind_defer:
                        # Identity pre-pass: bank the CANONICAL binding
                        # of each explosive acquired op (every required
                        # input fed the leaf of the same name -- the
                        # admitted behavior). Without this, the deferred
                        # explosive ops can never appear in the bank, and
                        # deeper levels cannot compose over them (observed:
                        # snarf = plink + dax needs plink/dax banked at
                        # level 1 to reach the size-3 solution). One
                        # candidate per op -- not the 3000-combination
                        # arg-permutation sweep, which stays deferred in
                        # the explosive residual. Ordering only.
                        for _eop in sorted(_explosive_ops):
                            try:
                                _eprim = self.reg.get(_eop)
                            except Exception:
                                continue
                            _ereq = [k for k, v in _eprim.inputs.items()
                                     if not v.optional]
                            if not _ereq or not all(
                                    r in param_names for r in _ereq):
                                continue
                            _eexpr = Expr(
                                op=_eop,
                                children=tuple(
                                    (r, Expr(param=r)) for r in _ereq))
                            _ekw = {r: tuple(a[r] for a, _ in examples)
                                    for r in _ereq}
                            _evals = self._apply_across_examples(
                                _eprim, _ereq, _ekw, len(examples))
                            if _evals is None:
                                trace.rejected.append(
                                    f"{_eop}: identity binding raised")
                                continue
                            trace.candidates_tried += 1
                            if try_add(_eexpr, _evals, 1):
                                # Degenerate: a single acquired op fits.
                                # (Check 0 normally pre-empts this; handle
                                # it through the same gate, not a shortcut.)
                                if not self._general_identifiability(
                                        _eexpr, args_list, param_names,
                                        target, trace):
                                    continue
                                trace.per_level.append({
                                    "target_size": target_size,
                                    "candidates_this_level":
                                        trace.candidates_tried -
                                        _level_start_candidates,
                                    "seconds_this_level": round(
                                        time.time() - _level_start_time, 3),
                                    "found": True,
                                    "found_by": "identity-prepass",
                                })
                                return (self._finish(
                                    _eexpr, param_names, trace), trace)
                else:
                    ordered_ops = [o for o in ordered_ops
                                   if (o not in _cheap if _cheap is not None
                                       else o in _explosive_ops)]

            # Generic conditional-completion check, using only what is
            # already in the bank from smaller sizes -- run before this
            # size level's own ordinary sweep so a discoverable piecewise
            # solution is found as early as possible rather than waiting
            # for the combinatorial sweep to reach whatever depth the
            # resulting if_else naturally falls at.
            #
            # A fitting conditional is STASHED, not returned: fitting the
            # training data is not proof the conditional is identified
            # (several structurally different predicates/branches can
            # share the same training behavior yet disagree unseen), and
            # a simpler non-conditional solution at this same level must
            # still win. Both are decided at level end, with the complete
            # bank, by the identifiability gate.
            for cond_expr, a_expr, b_expr in self._find_conditional_completions(
                    bank_by_size, target, trace):
                self._stash_conditional(Expr(
                    op="if_else",
                    children=(("condition", cond_expr),
                              ("then", a_expr),
                              ("otherwise", b_expr))))

            # Bank unary acquired capabilities at size 1 for composition.
            if target_size == 1 and _has_acquired:
                for _lop in list(self.reg.names()):
                    try:
                        _lprim = self.reg.get(_lop)
                    except Exception:
                        continue
                    if _lprim is None or getattr(_lprim, "family", "") not in ("acquired", "promoted"):
                        continue
                    _lreq = [k for k, v in _lprim.inputs.items() if not v.optional]
                    if len(_lreq) != 1:
                        continue
                    _larg = _lreq[0]
                    for _pname in param_names:
                        _lexpr = Expr(op=_lop, children=((_larg, Expr(param=_pname)),))
                        _lvals = self._eval_all(_lexpr, args_list)
                        if _lvals is None:
                            continue
                        try_add(_lexpr, _lvals, _lexpr.size())
            for op in ordered_ops:
                if _stop_size:
                    break
                prim = self.reg.get(op)
                if self.skip_exhausted_bool_space and \
                        prim.output.kind == Kind.BOOL and \
                        len(bool_tuples_seen) >= bool_space_size:
                    trace.bool_space_skips += 1
                    continue
                required = [k for k, v in prim.inputs.items() if not v.optional]
                remaining = target_size - 1
                if remaining < 0:
                    continue

                # Structural-hypothesis partition priority: at a
                # hypothesis's firing level, the partition matching the
                # root's child sizes is tried first (for a
                # non-commutative root the child ORDER selects the
                # wiring). Every partition is still enumerated, so
                # completeness is unchanged. (Commutative roots only
                # enumerate the canonical partition -- the mirror is
                # skipped as redundant -- and the slot-size-based pool
                # patterns below cover it.)
                _partitions = list(
                    self._size_partitions(remaining, len(required)))
                _shyps = getattr(self, "_structural_hyps", None) or []
                _root_h = [h for h in _shyps
                           if h.op == op and h.size() == target_size]
                if _root_h and op not in self._COMMUTATIVE_OPS:
                    def _pkey(part):
                        for h in _root_h:
                            if tuple(h.child_sizes()) == tuple(part):
                                return 0
                        return 1
                    _partitions.sort(key=_pkey)
                for size_partition in _partitions:
                    if op in self._COMMUTATIVE_OPS and len(required) == 2 and \
                            size_partition[0] > size_partition[1]:
                        # The mirror partition (e.g. (1,0) when (0,1) is
                        # also a valid partition) would only ever produce
                        # argument orderings that (0,1) already covers for
                        # a commutative op -- skip it entirely rather than
                        # rely on the same-partition, same-pool dedup below
                        # to catch it after the fact, since these draw from
                        # DIFFERENT slot pools and would never collide there.
                        continue
                    slot_pools = []
                    feasible = True
                    for slot_idx, (arg_name, size) in enumerate(
                            zip(required, size_partition)):
                        arg_type = prim.inputs[arg_name]
                        pool = [(e, v) for e, v, t in bank_by_size.get(size, [])
                               if t is not None and arg_type.accepts(t)]
                        if not pool:
                            feasible = False
                            # Record structural exclusion once per (op, arg).
                            key = (op, arg_name, str(arg_type))
                            already = any(
                                (e.get("op"), e.get("arg_name"), e.get("required_type"))
                                == key
                                for e in trace.unsatisfiable_args
                            )
                            if not already:
                                trace.unsatisfiable_args.append({
                                    "op": op,
                                    "arg_name": arg_name,
                                    "required_type": str(arg_type),
                                    "reason": "no_constructible_producer",
                                })
                            break
                        # Sort by priority BEFORE truncating to pool_per_type,
                        # not by raw insertion order. Found directly: with
                        # ~228 primitives, bank_by_size[1] can hold 300+
                        # entries in one search, and truncating by insertion
                        # order meant a needed entry (e.g. power(w, 2)) could
                        # sit at position 322 while pool_per_type=24 only ever
                        # looked at the first 24 — structurally excluded no
                        # matter how relevant, for a reason with zero
                        # connection to usefulness. Same class of bug as the
                        # op-level stable-hash/bias ordering fix, at the
                        # bank-entry level instead of op-selection.
                        #
                        # Priority alone was not enough, found directly too:
                        # on a cold search every entry ties at the same
                        # neutral bias score, and Python's STABLE sort then
                        # silently falls back to preserving insertion order
                        # — the exact same bug, one level down. add_duration
                        # (w, h) sat at position 126 with pool_per_type=60;
                        # raising pool_per_type didn't help because nothing
                        # was reordering the pool, only truncating it later.
                        # The tiebreak makes cold-start truncation
                        # hash-ordered instead of insertion-ordered.
                        pool.sort(key=lambda ev, _op=op, _sp=size_partition,
                                          _si=slot_idx: (
                            # Composite-hypothesis structural patterns
                            # sort first (ordering, not pruning -- pool
                            # membership is unchanged).
                            self._hyp_pool_rank(_op, _sp, _si, ev[0]),
                            -self._entry_priority(ev[0], ev[1], target),
                            self._relevance_tiebreak(ev[0]),
                            self._stable_tiebreak(
                                ev[0].op if not ev[0].is_leaf()
                                else f"leaf:{ev[0].param}:{ev[0].literal}")))
                        slot_pools.append(pool[: self.pool_per_type])

                    if not feasible:
                        continue

                    # Commutative canonical-order dedup is only valid when
                    # the mirror combo is actually enumerated in THIS
                    # partition. The mirror SIZE partition is skipped above
                    # (e.g. (3,0) when (0,3) is kept), so for cross-size
                    # partitions each combo is already a unique unordered
                    # pair -- skipping by value order there silently drops
                    # reachable behaviors (found directly: a guided
                    # size-4 hypothesis failed exactly when the leaf's
                    # value tuple ordered after the staged subtree's).
                    # Only dedup when both slots draw from the same entry
                    # set, where (B, A) really is the mirror of (A, B).
                    _comm_dedup = (
                        op in self._COMMUTATIVE_OPS
                        and len(required) == 2
                        and size_partition[0] == size_partition[1]
                        and {id(_e) for _e, _ in slot_pools[0]}
                        == {id(_e) for _e, _ in slot_pools[1]})
                    for combo in self._bounded_product(slot_pools):
                        if _comm_dedup:
                            try:
                                skip = combo[0][1] > combo[1][1]
                            except TypeError:
                                skip = False
                            if skip:
                                # Skip the redundant ordering of a commutative
                                # binary op's arguments -- op(A, B) and op(B, A)
                                # are guaranteed to produce identical value
                                # tuples for any commutative op, so only one
                                # ordering (chosen by a stable, arbitrary
                                # comparison of the already-computed value
                                # tuples themselves) is ever evaluated. This is
                                # a property of the operator, never of any
                                # objective's vocabulary.
                                continue
                        if (_size1_share is not None and target_size <= 1
                                and (trace.candidates_tried - _level_start_candidates) >= _size1_share
                                and trace.candidates_tried < self.max_candidates):
                            _stop_size = True
                            break
                        if trace.candidates_tried >= self.max_candidates:
                            trace.per_level.append({
                                "target_size": target_size,
                                "candidates_this_level": trace.candidates_tried - _level_start_candidates,
                                "seconds_this_level": round(time.time() - _level_start_time, 3),
                                "dedup_hits_this_level": trace.dedup_hits - _level_start_dedup,
                                "unsafe_rejections_this_level": trace.unsafe_value_rejections - _level_start_unsafe,
                                "bank_size_after": sum(len(v) for v in bank_by_size.values()),
                                "stopped_reason": "max_candidates",
                            })
                            return None, trace
                        if trace.candidates_tried % 500 == 0:
                            # Mid-sweep conditional check: same stash
                            # discipline as the pre-level check -- a fitting
                            # conditional never preempts the sweep's own
                            # search for a simpler non-conditional solution.
                            for cond_expr, a_expr, b_expr in self._find_conditional_completions(
                                    bank_by_size, target, trace):
                                self._stash_conditional(Expr(
                                    op="if_else",
                                    children=(("condition", cond_expr),
                                              ("then", a_expr),
                                              ("otherwise", b_expr))))
                        if trace.candidates_tried % 500 == 0 and \
                                time.time() - started > self.wall_clock_limit_s:
                            trace.rejected.append(
                                f"wall-clock limit ({self.wall_clock_limit_s}s) "
                                f"reached mid-level")
                            trace.per_level.append({
                                "target_size": target_size,
                                "candidates_this_level": trace.candidates_tried - _level_start_candidates,
                                "seconds_this_level": round(time.time() - _level_start_time, 3),
                                "dedup_hits_this_level": trace.dedup_hits - _level_start_dedup,
                                "unsafe_rejections_this_level": trace.unsafe_value_rejections - _level_start_unsafe,
                                "bank_size_after": sum(len(v) for v in bank_by_size.values()),
                                "stopped_reason": "wall_clock",
                            })
                            return None, trace
                        # Cooperation point (throttled by the % 500 sweep
                        # stride): a stop takes effect mid-level, not only
                        # at the per-level or wall-clock checks. No-op when
                        # no control is installed.
                        checkpoint("synthesize:sweep")
                        trace.candidates_tried += 1
                        child_exprs = {name: expr for name, (expr, _) in
                                      zip(required, combo)}
                        kwargs_values = {name: values for name, (_, values) in
                                         zip(required, combo)}
                        values = self._apply_across_examples(
                            prim, required, kwargs_values, len(examples))
                        if values is None:
                            trace.rejected.append(f"{op}: raised on at least one example")
                            continue
                        new_expr = Expr(op=op, children=tuple(
                            (name, child_exprs[name]) for name in required))
                        # new_expr.size() == target_size by construction
                        # (1 root + children whose sizes sum to target_size-1)
                        if values == target:
                            if op == "if_else":
                                # A conditional that fits training is not
                                # proof it is identified -- stash it for the
                                # level-end identifiability gate instead of
                                # returning it outright. (Non-conditional
                                # solutions still return immediately: at any
                                # given size they are the simpler
                                # explanation and win outright.)
                                self._stash_conditional(new_expr)
                                try_add(new_expr, values, target_size)
                                continue
                            # Wrapper hole: a non-conditional that fits but
                            # contains a poisoned (ambiguous-suppressed)
                            # conditional inherits its ambiguity. Do not
                            # return it outright; skip it so the search
                            # cannot re-admit a suppressed explanation
                            # wrapped in a no-op.
                            if self._contains_poisoned_conditional(new_expr):
                                trace.rejected.append(
                                    "wrapper around poisoned conditional "
                                    "skipped: %s" % new_expr.canonical()[:80])
                                continue
                            # Generalized identifiability: a fitting
                            # non-conditional is not proof it is
                            # identified (observed: clamp(a, 1, b)
                            # fitting min-training examples while
                            # clamp(a, 0, b) etc. are equally simple and
                            # disagree unseen). Suppress ambiguous ones
                            # and keep searching; the suppressed
                            # candidate is banked so later candidates
                            # see it as the rival it is.
                            if not self._general_identifiability(
                                    new_expr, args_list, param_names,
                                    target, trace):
                                try_add(new_expr, values, target_size)
                                continue
                            trace.per_level.append({
                                "target_size": target_size,
                                "candidates_this_level": trace.candidates_tried - _level_start_candidates,
                                "seconds_this_level": round(time.time() - _level_start_time, 3),
                                "dedup_hits_this_level": trace.dedup_hits - _level_start_dedup,
                                "unsafe_rejections_this_level": trace.unsafe_value_rejections - _level_start_unsafe,
                                "bank_size_after": sum(len(v) for v in bank_by_size.values()),
                                "found": True,
                            })
                            return self._finish(new_expr, param_names, trace), trace
                        try_add(new_expr, values, target_size)

            # Level end: decide every stashed conditional with the complete
            # bank (all sizes up to target_size). A stashed conditional is
            # returned only if the identifiability gate proves no rival
            # explanation fits the training data yet disagrees on
            # discriminating probe inputs; otherwise it is suppressed
            # (fail closed), its shape poisoned against re-discovery, and
            # the search continues at deeper sizes.
            if self._stashed_conditionals:
                stashed = self._stashed_conditionals
                self._stashed_conditionals = []
                # One shared gate view for the whole stashed set: the
                # bank is complete and unchanged at level end, so rival
                # pools, probes, and probe evaluations are built once,
                # not once per stashed conditional.
                gate_view = self._conditional_gate_view(
                    bank_by_size, target, args_list, param_names, trace)
                for cand in stashed:
                    identifiable, _detail = self._gate_conditional(
                        cand, target, args_list, param_names,
                        gate_view, trace)
                    if identifiable:
                        _plan = self._build_general_plan(cand, param_names)
                        if not self._plan_typechecks(_plan):
                            continue
                        trace.per_level.append({
                            "target_size": target_size,
                            "candidates_this_level": trace.candidates_tried - _level_start_candidates,
                            "seconds_this_level": round(time.time() - _level_start_time, 3),
                            "dedup_hits_this_level": trace.dedup_hits - _level_start_dedup,
                            "unsafe_rejections_this_level": trace.unsafe_value_rejections - _level_start_unsafe,
                            "bank_size_after": sum(len(v) for v in bank_by_size.values()),
                            "found": True, "found_by": "conditional_completion_gated",
                        })
                        return self._finish(cand, param_names, trace), trace

                # Evidence closure: when the pure-training gate rejects
                # every stashed conditional as ambiguous, but the example
                # set is large enough to split, treat the held-out third
                # as a discriminating experiment. Admit only if exactly
                # one training-consistent candidate also fits held-out.
                # Fail-closed when 0 or 2+ candidates survive -- no
                # arbitrary winner.
                if len(examples) >= 6 and stashed:
                    n_hold = max(1, len(examples) // 3)
                    train_ex = examples[:-n_hold]
                    hold_ex = examples[-n_hold:]
                    train_args = [a for a, _ in train_ex]
                    train_target = tuple(v for _, v in train_ex)
                    survivors = []
                    for cand in stashed:
                        tvals = self._eval_all(cand, train_args)
                        if tvals is None or tuple(tvals) != train_target:
                            continue
                        ok_hold = True
                        for hargs, hexpect in hold_ex:
                            try:
                                got = self._eval(cand, hargs)
                            except Exception:
                                ok_hold = False
                                break
                            if not self._values_close(got, hexpect):
                                ok_hold = False
                                break
                        if ok_hold:
                            survivors.append(cand)
                    if len(survivors) == 1:
                        winner = survivors[0]
                        trace.per_level.append({
                            "target_size": target_size,
                            "found": True,
                            "found_by": "conditional_heldout_evidence_closure",
                            "heldout_n": n_hold,
                            "eliminated": len(stashed) - 1,
                        })
                        return self._finish(winner, param_names, trace), trace
                    else:
                        trace.rejected.append(
                            "conditional evidence closure: %d training-fit "
                            "candidates, %d also fit held-out; fail-closed"
                            % (len(stashed), len(survivors)))
                # Oracle-backed ambiguity resolution: when training and
                # held-out evidence leave multiple survivors, and a
                # callable oracle is available, select an input where
                # candidates disagree, query the oracle, and eliminate
                # inconsistent candidates. Generic -- not target-specific.
                oracle = getattr(self, "_search_oracle", None)
                if oracle is not None and stashed:
                    pool = list(stashed)
                    # Prefer survivors of held-out filter if any
                    if len(examples) >= 6:
                        n_hold = max(1, len(examples) // 3)
                        train_ex = examples[:-n_hold]
                        hold_ex = examples[-n_hold:]
                        train_args = [a for a, _ in train_ex]
                        train_target = tuple(v for _, v in train_ex)
                        filt = []
                        for cand in pool:
                            tvals = self._eval_all(cand, train_args)
                            if tvals is None:
                                continue
                            # numeric-tolerant train fit
                            ok = all(self._values_close(a, b)
                                     for a, b in zip(tvals, train_target))
                            if not ok:
                                continue
                            ok_h = True
                            for hargs, hexpect in hold_ex:
                                try:
                                    got = self._eval(cand, hargs)
                                except Exception:
                                    ok_h = False
                                    break
                                if not self._values_close(got, hexpect):
                                    ok_h = False
                                    break
                            if ok_h:
                                filt.append(cand)
                        if filt:
                            pool = filt
                    # Find discriminating input from probe geometry
                    disc_input = None
                    try:
                        probes = self._discrimination_probes(
                            args_list, param_names,
                            list(self._search_leaf_literals)[:20])
                    except Exception:
                        probes = []
                    # Also sample param values around training range
                    for pname in param_names:
                        vals = []
                        for a, _ in examples:
                            if pname in a and isinstance(a[pname], (int, float)):
                                vals.append(float(a[pname]))
                        if vals:
                            lo, hi = min(vals), max(vals)
                            for x in (lo - 1, hi + 1, (lo + hi) / 2,
                                      lo - 5, hi + 5, 0, -7, 7, 100, -100):
                                probes.append({pname: x})
                    for pr in probes:
                        preds = []
                        for cand in pool:
                            try:
                                preds.append(self._eval(cand, pr))
                            except Exception:
                                preds.append(None)
                        distinct = set()
                        for p in preds:
                            if p is None:
                                continue
                            key = round(float(p), 6) if isinstance(p, (int, float)) else p
                            distinct.add(key)
                        if len(distinct) >= 2:
                            disc_input = pr
                            break
                    # Build query set: disagreement probes first, then
                    # novelty samples. Query oracle on each until the
                    # candidate pool shrinks to 0 or 1, or queries exhaust.
                    query_inputs = []
                    if disc_input is not None:
                        query_inputs.append(disc_input)
                    for pr in probes:
                        if pr not in query_inputs:
                            query_inputs.append(pr)
                    # Cap oracle queries
                    query_inputs = query_inputs[:12]
                    if not query_inputs:
                        trace.rejected.append(
                            "oracle available but no query inputs among %d "
                            "candidates" % len(pool))
                    remaining = list(pool)
                    oracle_log = []
                    for q in query_inputs:
                        if len(remaining) <= 1:
                            break
                        try:
                            observed = oracle(dict(q))
                        except Exception as _oe:
                            oracle_log.append({"input": dict(q), "error": str(_oe)})
                            continue
                        nxt = []
                        preds = []
                        for cand in remaining:
                            try:
                                pred = self._eval(cand, q)
                            except Exception:
                                pred = None
                            preds.append(pred)
                            if pred is not None and self._values_close(pred, observed):
                                nxt.append(cand)
                        oracle_log.append({
                            "input": dict(q), "observed": observed,
                            "n_before": len(remaining), "n_after": len(nxt),
                        })
                        remaining = nxt
                    if len(remaining) == 1:
                        winner = remaining[0]
                        plan = self._build_general_plan(winner, param_names)
                        if not self._plan_typechecks(plan):
                            trace.rejected.append(
                                "oracle unique survivor fails plan typecheck; "
                                "fail-closed")
                        else:
                            trace.per_level.append({
                                "target_size": target_size,
                                "found": True,
                                "found_by": "oracle_backed_ambiguity_resolution",
                                "oracle_log": oracle_log,
                                "eliminated": len(pool) - 1,
                            })
                            return self._finish(winner, param_names, trace), trace
                    elif len(remaining) > 1:
                        # Behaviorally equivalent under oracle queries:
                        # prefer smallest expression whose compiled plan
                        # type-checks. No unchecked fallback -- a plan
                        # that cannot pass Composer must not be admitted.
                        remaining.sort(key=lambda e: e.size())
                        winner = None
                        for cand in remaining:
                            wvals = self._eval_all(cand, args_list)
                            if wvals is None or not all(
                                    self._values_close(a, b)
                                    for a, b in zip(wvals, target)):
                                continue
                            plan = self._build_general_plan(cand, param_names)
                            if not self._plan_typechecks(plan):
                                continue
                            winner = cand
                            break
                        if winner is not None:
                            trace.per_level.append({
                                "target_size": target_size,
                                "found": True,
                                "found_by": "oracle_backed_equivalent_parsimony",
                                "oracle_log": oracle_log,
                                "n_equivalent": len(remaining),
                            })
                            return self._finish(winner, param_names, trace), trace
                        trace.rejected.append(
                            "oracle: %d remain but none type-check; "
                            "fail-closed" % len(remaining))
                    else:
                        trace.rejected.append(
                            "oracle eliminated all %d candidates; fail-closed"
                            % len(pool))
            _prev_size = target_size
            if target_size == 1 and (_acquired_defer or _blind_defer):
                _level1_pass += 1

        trace.per_level.append({
            "target_size": _prev_size if _prev_size is not None
            else self.max_size,
            "candidates_this_level": trace.candidates_tried - _level_start_candidates,
            "seconds_this_level": round(time.time() - _level_start_time, 3),
            "dedup_hits_this_level": trace.dedup_hits - _level_start_dedup,
            "unsafe_rejections_this_level": trace.unsafe_value_rejections - _level_start_unsafe,
            "bank_size_after": sum(len(v) for v in bank_by_size.values()),
        })
        return None, trace

    # ------------------------------------------------------------------
    # Conditional completion with an identifiability gate.
    #
    # Fitting the training data is not proof a conditional is
    # identified: several structurally different predicates/branches can
    # share the same training behavior yet disagree on unseen inputs
    # (observed failure: is_before(30, signal) vs signal<50, and an
    # arbitrary literal threshold mask vs a relational comparator, both
    # fitting every training example). So completion only FINDS
    # candidate conditionals; each is STASHED and decided at level end
    # by _gate_conditional, which fails closed when rival
    # training-consistent explanations disagree on discriminating probe
    # inputs. Ambiguity is reported in the trace as its own verdict,
    # never misclassified as ordinary search exhaustion.
    # ------------------------------------------------------------------

    def _find_contradiction(self, examples) -> Optional[str]:
        """Same inputs -> different expected outputs: no expression can
        fit. A property of the evidence itself, reported as such rather
        than as a search failure."""
        seen: Dict[str, Any] = {}
        for args, expected in examples:
            try:
                key = repr(tuple(sorted((k, repr(v))
                                        for k, v in args.items())))
            except Exception:
                continue
            if key in seen:
                if seen[key] != expected:
                    return ("identical inputs %r expect both %r and %r"
                            % (args, seen[key], expected))
            else:
                seen[key] = expected
        return None

    @staticmethod
    def _values_close(x: Any, y: Any) -> bool:
        """Behavioral equality for probe comparison: exact for bools and
        structural values, tight relative tolerance for floats so
        floating-point noise never manufactures a disagreement."""
        if x is None or y is None:
            return x is None and y is None
        if isinstance(x, bool) or isinstance(y, bool):
            return x == y
        if isinstance(x, (int, float)) and isinstance(y, (int, float)):
            fx, fy = float(x), float(y)
            return abs(fx - fy) <= 1e-9 * max(1.0, abs(fx), abs(fy))
        try:
            return x == y
        except Exception:
            return False

    def _conditional_signature(self, cond_vals: Tuple[Any, ...],
                               a_vals: Tuple[Any, ...],
                               b_vals: Tuple[Any, ...]) -> str:
        """Behavioral identity of a conditional shape on the training
        data: predicate mask + both branch tuples. Two triples with the
        same signature are indistinguishable on the evidence; poisoning
        one poisons the shape, not just the spelling."""
        return "|".join((self._value_key(tuple(cond_vals)),
                         self._value_key(tuple(a_vals)),
                         self._value_key(tuple(b_vals))))

    def _signature_of(self, expr: Expr) -> Optional[str]:
        """Training-behavior signature of an if_else-rooted Expr, via the
        current search's own training args. None if it cannot be
        evaluated/decomposed (fail closed downstream)."""
        parts = self._decompose_conditional(expr)
        if parts is None:
            return None
        cond, a_expr, b_expr = parts
        args_list = self._search_args_list
        cv = self._eval_all(cond, args_list)
        av = self._eval_all(a_expr, args_list)
        bv = self._eval_all(b_expr, args_list)
        if cv is None or av is None or bv is None:
            return None
        return self._conditional_signature(cv, av, bv)

    def _stash_conditional(self, expr: Expr) -> None:
        """Stash a training-fitting conditional for the level-end
        identifiability gate. Never returns anything directly: fitting
        is not identification, and a simpler non-conditional solution at
        this level must still win outright. Shapes already stashed or
        poisoned are not duplicated."""
        sig = self._signature_of(expr)
        if sig is None:
            return
        if sig in self._poisoned_signatures:
            return
        if sig in self._stashed_signatures:
            return
        self._stashed_signatures.add(sig)
        self._stashed_conditionals.append(expr)

    def _contains_poisoned_conditional(self, expr: Expr) -> bool:
        """True if expr contains an if_else subexpression whose
        training-behavior signature was poisoned (suppressed as
        ambiguous by the identifiability gate). Such an expr is a
        wrapper around a suppressed conditional and inherits its
        ambiguity -- it must not be returned as a solution without
        gating. (The wrapper is always larger than the inner
        conditional, so by the time the sweep finds it, the inner
        conditional has already been gated and poisoned at an
        earlier level end.)"""
        if "if_else" not in expr.ops_used():
            return False
        stack = [expr]
        while stack:
            e = stack.pop()
            if e.op == "if_else":
                sig = self._signature_of(e)
                if sig is not None and sig in self._poisoned_signatures:
                    return True
                if e.canonical() in self._poisoned_signatures:
                    return True
            for _, child in e.children:
                stack.append(child)
        return False

    def _suppress_ambiguous(self, cand: Expr, rival_pred: Optional[Expr],
                            rival_branches, probe: Optional[Dict[str, Any]],
                            detail: Dict[str, Any], trace,
                            rival_probe: Any = None,
                            cand_probe_val: Any = None) -> None:
        """Fail closed on an underdetermined conditional: poison its
        training-behavior shape so the search never re-admits it, and
        record the distinguishing evidence in the trace as ambiguity --
        a distinct verdict, not ordinary search exhaustion."""
        sig = self._signature_of(cand)
        if sig is not None:
            self._stashed_signatures.discard(sig)
            self._poisoned_signatures.add(sig)
        else:
            # Undecomposable shape: poison by spelling as a fallback so
            # it cannot be re-stashed either.
            self._poisoned_signatures.add(cand.canonical())
        detail["suppressed"] = True
        trace.ambiguous.append({
            "conditional": cand.canonical(),
            "reason": detail.get("reason", "rival explanation disagrees"),
            "rival_predicate": rival_pred.canonical() if rival_pred else None,
            "rival_branches": ([b.canonical() for b in rival_branches]
                               if rival_branches else None),
            "distinguishing_probe": probe,
            "candidate_output": cand_probe_val,
            "rival_output": rival_probe,
        })
        trace.rejected.append(
            "ambiguous conditional suppressed: %s" % cand.canonical())

    # ------------------------------------------------------------------
    # Generalized identifiability gate (non-conditionals).
    #
    # The conditional gate fixed one shape of a general problem:
    # fitting the training data is not proof a hypothesis is
    # identified. The same failure occurs without any if_else --
    # observed directly: with examples (42,7)->7, (7,42)->7, (5,3)->3,
    # (3,9)->3 the search returned clamp(a, 1, b), which fits every
    # training example yet is wrong on unseen input (min(0,0) -> 1),
    # while clamp(a, 0, b), clamp(a, 2, b), ... are equally simple and
    # equally consistent. The literal 1 is a free, unconstrained
    # degree of freedom; the evidence does not identify the hypothesis.
    #
    # So every non-conditional training-fitting candidate is checked
    # before it is returned: it is suppressed (fail closed) iff there
    # EXISTS a rival that (a) fits all training examples, (b) disagrees
    # with the candidate on a discriminating probe, and (c) is no more
    # complex (Expr.size()). Rival sources are generic, not
    # task-specific: literal perturbations of the candidate through
    # the search's own literal pool (a free constant reveals itself
    # when another value fits training just as well), other leaves
    # with the same training tuple (for leaf candidates), and the
    # observational-equivalence archive. Conditionals keep their
    # specialized gate (_gate_conditional); this method returns True
    # for them without deciding.
    def _structural_rival_expr(self, tree):
        """Compile a probe StructuralHypothesis rival to an Expr for the
        identifiability gate. Returns None when the op is unknown to the
        registry (fail closed: no rival rather than a wrong one)."""
        if isinstance(tree, str):
            return Expr(param=tree)
        prim = self.reg.get(tree.op)
        if prim is None:
            return None
        required = [k for k, v in prim.inputs.items()
                    if not v.optional]
        if len(required) != len(tree.children):
            return None
        kids = []
        for name, c in zip(required, tree.children):
            ce = self._structural_rival_expr(c)
            if ce is None:
                return None
            kids.append((name, ce))
        return Expr(op=tree.op, children=tuple(kids))

    def _general_identifiability(self, cand: Expr,
                                 args_list: List[Dict[str, Any]],
                                 param_names: Sequence[str],
                                 target: Tuple[Any, ...],
                                 trace: SynthesisTrace) -> bool:
        if cand.op == "if_else":
            return True  # owned by _gate_conditional
        n = len(args_list)

        # Numeric literal occurrences in the candidate's structure.
        occurrences: List[Tuple[Tuple[int, ...], float]] = []
        mentioned: set = set()

        def walk(e: Expr, path: Tuple[int, ...]) -> None:
            if e.is_leaf():
                if e.is_literal and isinstance(e.literal, (int, float)) \
                        and not isinstance(e.literal, bool):
                    occurrences.append((path, float(e.literal)))
                elif not e.is_literal and e.param:
                    mentioned.add(e.param)
                return
            for j, (_, child) in enumerate(e.children):
                walk(child, path + (j,))

        walk(cand, ())

        # Substitution vocabulary: the search's own literal pool plus
        # midpoints between consecutive training values of mentioned
        # parameters (the evidence's own geometry -- same source the
        # conditional gate's perturbation uses).
        subs = set(getattr(self, "_search_literal_pool", set()) or set())
        for p in mentioned:
            if p not in param_names:
                continue
            vals = sorted({float(a[p]) for a in args_list
                           if isinstance(a.get(p), (int, float))
                           and not isinstance(a.get(p), bool)})
            for x, y in zip(vals, vals[1:]):
                if y > x:
                    subs.add((x + y) / 2.0)
        subs = sorted(subs)

        probes: Optional[List[Dict[str, Any]]] = None
        probe_cache: Dict[str, Tuple[Any, ...]] = {}

        def get_probes() -> List[Dict[str, Any]]:
            nonlocal probes
            if probes is None:
                literals = sorted({v for _, v in occurrences})
                probes = self._discrimination_probes(
                    args_list, param_names, literals)
                trace.ambiguity_probe_evals += 0  # counted below per eval
            return probes

        def probe_tuple(expr: Expr) -> Tuple[Any, ...]:
            c = expr.canonical()
            t = probe_cache.get(c)
            if t is None:
                ps = get_probes()
                t = tuple(self._safe_eval(expr, pr) for pr in ps)
                trace.ambiguity_probe_evals += len(ps)
                probe_cache[c] = t
            return t

        def suppress_with(rival: Expr, why: str) -> bool:
            """True when rival is a legitimate suppressing rival:
            no more complex than the candidate, fits training (checked
            by caller for generated variants; true by construction for
            archive/leaf rivals), and behaviorally different on a
            discriminating probe."""
            if rival.size() > cand.size():
                return False
            if rival.canonical() == cand.canonical():
                return False
            ps = get_probes()
            c_p = probe_tuple(cand)
            r_p = probe_tuple(rival)
            for k in range(len(ps)):
                if not self._values_close(r_p[k], c_p[k]):
                    trace.ambiguous.append({
                        "kind": "general",
                        "candidate": cand.canonical(),
                        "candidate_expr": cand,
                        "reason": why,
                        "rival": rival.canonical(),
                        "rival_expr": rival,
                        "distinguishing_probe": ps[k],
                        "candidate_output": c_p[k],
                        "rival_output": r_p[k],
                    })
                    trace.rejected.append(
                        "ambiguous hypothesis suppressed: %s"
                        % cand.canonical()[:80])
                    return True
            return False

        # Rival source 1: literal perturbation. A constant the evidence
        # does not constrain reveals itself here: another pool value in
        # its place still fits every training example.
        if not (cand.is_leaf() and cand.is_literal):
            for path, old_v in occurrences:
                for s in subs:
                    if s == old_v:
                        continue
                    var = self._substitute_literal(cand, path, s)
                    if var is None:
                        continue
                    vals = self._eval_all(var, args_list)
                    if vals is None or tuple(vals) != target:
                        continue
                    if suppress_with(
                            var, "rival literal substitution fits training "
                            "but disagrees on a discriminating probe"):
                        return False

        # Rival source 2: for leaf candidates, every other leaf with
        # the same training tuple (e.g. a constant param vs a literal,
        # or two always-equal params -- indistinguishable on the
        # evidence, whatever their spelling).
        if cand.is_leaf():
            rival_leaves: List[Tuple[Expr, Tuple[Any, ...]]] = []
            for name in param_names:
                leaf = Expr(param=name)
                lv = self._eval_all(leaf, args_list)
                if lv is not None:
                    rival_leaves.append((leaf, tuple(lv)))
            for lit in sorted(subs):
                rival_leaves.append(
                    (Expr(literal=lit, is_literal=True),
                     tuple(lit for _ in args_list)))
            for rival, lv in rival_leaves:
                if lv != target:
                    continue
                if suppress_with(
                        rival, "rival leaf fits training but disagrees on "
                        "a discriminating probe"):
                    return False

        # Rival source 3: the observational-equivalence archive --
        # structurally distinct expressions the dedup hid.
        key = self._value_key(target)
        for alt in self._alt_exprs.get(key, []):
            if suppress_with(
                    alt, "archived rival explanation fits training but "
                    "disagrees on a discriminating probe"):
                return False

        # Rival source 4: probe-reported structural rivals. The
        # structural probe found multiple distinct trees with the same
        # training value vector that disagree on synthetic probes: the
        # training evidence cannot distinguish which pairing is real
        # (e.g. (a+b)*(c+d) vs (a+c)*(b+d) when b==c on every training
        # example). A training-fitting candidate is one realization among
        # rivals; admitting it would be a guess, so fail closed.
        # Expansion rivals (rival_kind == "expansion") are owned by
        # rival source 5 below -- they are legitimately LARGER than
        # the candidate, so source 4's size bound cannot apply to them.
        for _h in self._structural_hyps:
            for _r in getattr(_h, "rivals", None) or []:
                if getattr(_r, "rival_kind", None) == "expansion":
                    continue  # rival source 5 owns expansion rivals
                _rexp = self._structural_rival_expr(_r)
                if _rexp is None:
                    continue
                # Rival fits training by construction (same value
                # vector); verify explicitly for safety.
                try:
                    _rvals = self._eval_all(_rexp, args_list)
                except Exception:
                    continue
                if _rvals is None or tuple(_rvals) != target:
                    continue
                if suppress_with(
                        _rexp, "probe structural rival: a distinct tree "
                        "fits the same training values but disagrees on "
                        "a discriminating probe"):
                    return False

        # Rival source 5 (R2''): probe-reported dependency-expansion
        # rivals. The probe found that a leaf or subexpression the
        # candidate spells directly is functionally determined by
        # other inputs on the training evidence, and the full
        # expansion disagrees on a synthetic probe -- the training
        # evidence cannot tell which law is true (the silent
        # wrong-commit gap: e.g. (c*d)+e vs ((a+b)*d)+e when c==a+b
        # on every training row).
        #
        # POLICY (evidence-bounded, general): EVERY detected
        # expansion rival suppresses into ambiguity -- input-widening
        # or input-narrowing. Rationale:
        #  (1) The probe flagged underdetermination
        #      (n_underdetermined >= 1): the evidence cannot
        #      distinguish the pair. Silently resolving a flagged
        #      underdetermination by parsimony would be a guess, not
        #      an inference -- parsimony is the system's tie-breaker
        #      for what to try first, not a license to claim
        #      knowledge the evidence does not support.
        #  (2) Consistency: duplicate-column (channel a) and size-1/2
        #      subtree (channel b) rivals already fail closed
        #      regardless of input narrowing; the expansion channel
        #      is not special.
        #  (3) Deceptive parsimony: the candidate's small size is
        #      parasitic on the degenerate evidence -- a leaf's
        #      atomicity is fake when the leaf equals a template tree
        #      of other inputs on the only data seen. The
        #      widening/narrowing line itself is fragile (it depends
        #      on which valid expansion the search reports first; a
        #      zero-padded variant can flip the classification), so
        #      it cannot bear epistemic weight.
        #  (4) Decision-theoretic: suppression plus the one-query
        #      seeker recovers truth in both directions; silent
        #      parsimony commits are 0/20 wrong in the unlucky
        #      direction.
        # Unlike other rivals, an expansion is legitimately LARGER
        # than the candidate, so no size bound applies. Suppression
        # goes to AMBIGUITY, never promotion: the rival is never
        # admitted, only recorded with its discriminating probe for
        # the seeker. Synthetic disagreement proves behavioral
        # DISTINCTION only, never which law is true.
        for _h in self._structural_hyps:
            for _r in getattr(_h, "rivals", None) or []:
                if getattr(_r, "rival_kind", None) != "expansion":
                    continue
                _rexp = self._structural_rival_expr(_r)
                if _rexp is None:
                    continue
                try:
                    _rvals = self._eval_all(_rexp, args_list)
                except Exception:
                    continue
                if _rvals is None or tuple(_rvals) != target:
                    continue
                # No input-widening/narrowing split: any detected
                # expansion rival suppresses (policy above).
                _ps = get_probes()
                _c_p = probe_tuple(cand)
                _r_p = probe_tuple(_rexp)
                for _k in range(len(_ps)):
                    if not self._values_close(_r_p[_k], _c_p[_k]):
                        trace.ambiguous.append({
                            "kind": "expansion",
                            "candidate": cand.canonical(),
                            "candidate_expr": cand,
                            "reason": "probe dependency-expansion rival: "
                            "a leaf or subexpression of the candidate is "
                            "functionally determined by other inputs on "
                            "the training evidence; the full expansion "
                            "fits training but disagrees on a "
                            "discriminating probe",
                            "rival": _rexp.canonical(),
                            "rival_expr": _rexp,
                            "distinguishing_probe": _ps[_k],
                            "candidate_output": _c_p[_k],
                            "rival_output": _r_p[_k],
                            "expands_leaf": getattr(
                                _r, "expands_leaf", None),
                            "expands_subtree": getattr(
                                _r, "expands_subtree", None),
                        })
                        trace.rejected.append(
                            "ambiguous hypothesis suppressed "
                            "(dependency expansion): %s"
                            % cand.canonical()[:80])
                        return False

        return True

    def _decompose_conditional(self, cand: Expr
                               ) -> Optional[Tuple[Expr, Expr, Expr]]:
        """Split an if_else-rooted Expr into (predicate, then, otherwise)
        using the registry's own argument names, with a positional
        fallback. Returns None for malformed shapes (fail closed)."""
        if cand.op != "if_else" or len(cand.children) != 3:
            return None
        prim = self.reg.get("if_else")
        if prim is not None:
            order = [k for k, v in prim.inputs.items() if not v.optional]
        else:
            order = ["condition", "then", "otherwise"]
        by_name = dict(cand.children)
        try:
            return by_name[order[0]], by_name[order[1]], by_name[order[2]]
        except (KeyError, IndexError):
            pass
        try:
            return cand.children[0][1], cand.children[1][1], cand.children[2][1]
        except (KeyError, IndexError):
            return None

    def _safe_eval(self, expr: Expr, args: Dict[str, Any]) -> Any:
        """Evaluate on a probe input; failure is a behavioral outcome
        (None), not an exception, so a rival that crashes where the
        candidate survives counts as disagreeing."""
        try:
            return self._eval(expr, args)
        except Exception:
            return None

    def _numeric_literals(self, exprs) -> List[float]:
        """Every numeric literal appearing anywhere in the given
        expressions' own structure -- the pool's vocabulary, not the
        objective's. Used to place probe inputs at literal boundaries,
        where threshold-style rivals change behavior."""
        out: List[float] = []
        seen = set()
        stack = list(exprs)
        while stack:
            e = stack.pop()
            if e.is_literal:
                v = e.literal
                if isinstance(v, (int, float)) and not isinstance(v, bool):
                    f = float(v)
                    if f not in seen:
                        seen.add(f)
                        out.append(f)
            else:
                stack.extend(child for _, child in e.children)
        return out

    def _discrimination_probes(self, args_list: List[Dict[str, Any]],
                               param_names: Sequence[str],
                               literals: List[float]
                               ) -> List[Dict[str, Any]]:
        """Bounded behavioral evidence beyond the training data: for each
        numeric parameter, probe midpoints between consecutive training
        values and ±1/±0.5/exact neighborhoods of every literal in the
        candidate pool's vocabulary, with other parameters held at real
        training rows. Generic -- no domain knowledge, just the
        evidence's own geometry. Training rows themselves are excluded:
        every rival fits them by construction, so they cannot
        discriminate."""
        probes: List[Dict[str, Any]] = []
        seen_keys = set()

        def add_probe(d: Dict[str, Any]) -> None:
            try:
                key = repr(sorted((k, repr(v)) for k, v in d.items()))
            except Exception:
                return
            if key not in seen_keys:
                seen_keys.add(key)
                probes.append(d)

        n = len(args_list)
        bases = [args_list[0]]
        if n > 1:
            bases.append(args_list[-1])
        num_params = [p for p in param_names
                      if all(isinstance(a.get(p), (int, float))
                             and not isinstance(a.get(p), bool)
                             for a in args_list)]
        train_keys = set()
        for a in args_list:
            try:
                train_keys.add(repr(sorted((k, repr(v))
                                           for k, v in a.items())))
            except Exception:
                pass
        for base in bases:
            for p in num_params:
                vals = sorted({float(a[p]) for a in args_list})
                for x, y in zip(vals, vals[1:]):
                    if y > x:
                        d = dict(base)
                        d[p] = (x + y) / 2.0
                        key = repr(sorted((k, repr(v))
                                          for k, v in d.items()))
                        if key not in train_keys:
                            add_probe(d)
                for k in literals[:12]:
                    for delta in (-1.0, -0.5, 0.0, 0.5, 1.0):
                        d = dict(base)
                        d[p] = k + delta
                        key = repr(sorted((k2, repr(v))
                                          for k2, v in d.items()))
                        if key not in train_keys:
                            add_probe(d)
                if len(probes) >= 400:
                    break
            if len(probes) >= 400:
                break
        return probes

    def _find_conditional_completions(self, bank_by_size, target, trace):
        """Generic conditional-completion pass. Uses only the existing
        (expr, values, type) representation -- no domain vocabulary.

        Key insight: two same-typed candidates whose match-against-target
        masks are COMPLEMENTARY (together cover every training example,
        overlap on none) are exactly the behavioral signature of a
        piecewise function's two branches, regardless of what the
        underlying formula, domain, or field names are. If a boolean
        candidate already in the bank has a value tuple identical to one
        candidate's own match mask, that boolean IS the discriminating
        predicate, and if_else(predicate, A, B) is testable immediately
        -- without waiting for the ordinary size-by-size combinatorial
        sweep to reach whatever depth that if_else naturally falls at.

        Yields (predicate, then, otherwise) triples with DISTINCT
        training-behavior signatures (bounded): finding is separated from
        deciding. Each triple is stashed for the level-end
        identifiability gate; nothing is returned directly, because
        fitting the training data does not identify the conditional.

        Bounded deliberately: only candidates with partial (neither zero
        nor total) agreement with the target participate, since a
        candidate matching nothing or everything cannot be half of a
        complementary pair; each type group and the boolean pool are each
        capped so this pass's own cost stays small relative to the
        ordinary sweep it runs alongside.
        """
        n = len(target)
        if n < 2:
            return
        # Incremental classification: the bank only grows within a
        # search, so each entry is classified once and the (bools,
        # partial-mask) pools persist across the many calls this pass
        # gets (pre-level plus every 500 sweep candidates). Rebuilding
        # them from scratch on every call re-scanned thousands of
        # entries to rediscover the same handful of booleans -- measured
        # at ~40% of a non-conditional search's time while yielding
        # nothing new. The pairing below only depends on these two
        # pools, so when no new relevant entry arrived the yield set is
        # provably identical to the previous call's (already stashed,
        # deduped by _stash_conditional) and the scan is skipped.
        new_relevant = False
        for size, entries in bank_by_size.items():
            seen = self._cc_seen.get(size, 0)
            if seen >= len(entries):
                continue
            for expr, values, typ in entries[seen:]:
                if typ is None:
                    continue
                if len(values) != n:
                    continue
                if all(isinstance(v, bool) for v in values):
                    if len(self._cc_bools) < 400:
                        self._cc_bools.append((expr, tuple(values)))
                        new_relevant = True
                    continue
                # v40: {0,1}-valued numeric vectors are parity/bit patterns
                # that conditional completion needs as predicates. Waiting
                # for an explicit equals(expr, 0|1) wrapper often loses to
                # the size-1 flood; promote them generically via equals so
                # modulo-parity (and similar) can complete if_else branches
                # without a task-specific rule.
                if (
                    len(self._cc_bools) < 400
                    and values
                    and all(
                        isinstance(v, (int, float))
                        and not isinstance(v, bool)
                        and float(v) in (0.0, 1.0)
                        for v in values
                    )
                    and self.reg.get("equals") is not None
                ):
                    from swarm_engine.cognition.representations import Expr as _E
                    for lit, as_true in ((1, True), (0, False)):
                        pred = _E(
                            op="equals",
                            children=(
                                ("a", expr),
                                ("b", _E(literal=lit, is_literal=True)),
                            ),
                        )
                        if as_true:
                            bvals = tuple(bool(float(v) == 1.0) for v in values)
                        else:
                            bvals = tuple(bool(float(v) == 0.0) for v in values)
                        # Skip degenerate (all True / all False) — not a split.
                        if not (any(bvals) and not all(bvals)):
                            continue
                        if len(self._cc_bools) < 400:
                            self._cc_bools.append((pred, bvals))
                            new_relevant = True
                # Numeric-tolerant agreement so int branch values match
                # float targets (and vice versa) when building masks.
                def _agree(v, t):
                    if v == t:
                        return True
                    if isinstance(v, (int, float)) and isinstance(t, (int, float)):
                        return abs(float(v) - float(t)) <= 1e-9 * max(1.0, abs(float(v)), abs(float(t)))
                    return False
                mask = tuple(_agree(v, t) for v, t in zip(values, target))
                mc = sum(mask)
                if 0 < mc < n:
                    # Group int/float together so complementary branches
                    # are not split solely by numeric width (e.g. literal
                    # 100.0 vs multiply(x,2) producing ints).
                    try:
                        from swarm_engine.primitives.core import Kind
                        _k = getattr(typ, "kind", None)
                        if _k in (Kind.INT, Kind.FLOAT, Kind.NUM):
                            type_key = "num"
                        else:
                            type_key = str(typ)
                    except Exception:
                        type_key = str(typ)
                    lst = self._cc_by_type.setdefault(type_key, [])
                    entry = (expr, values, mask)
                    if len(lst) < 200:
                        lst.append(entry)
                        new_relevant = True
                    else:
                        # Prefer higher match-count partials over single-
                        # point constant hits that otherwise fill the cap
                        # before real branch candidates arrive.
                        worst_i, worst_mc = None, n
                        for _i, (_e, _v, _m) in enumerate(lst):
                            _mc = sum(_m)
                            if _mc < worst_mc:
                                worst_mc, worst_i = _mc, _i
                        if worst_i is not None and mc > worst_mc:
                            lst[worst_i] = entry
                            new_relevant = True
            self._cc_seen[size] = len(entries)
        by_type = self._cc_by_type if new_relevant else {}
        bools = self._cc_bools
        if not bools:
            return
        prim = self.reg.get("if_else")
        if prim is None:
            return
        yielded = 0
        for candidates in by_type.values():
            for i in range(len(candidates)):
                expr_a, values_a, mask_a = candidates[i]
                for j in range(i + 1, len(candidates)):
                    expr_b, values_b, mask_b = candidates[j]
                    if not all(ma != mb for ma, mb in zip(mask_a, mask_b)):
                        continue
                    for cond_expr, cond_values in bools:
                        if cond_values == mask_a:
                            combined = tuple(
                                values_a[k] if cond_values[k] else values_b[k]
                                for k in range(n))
                            trace.candidates_tried += 1
                            if combined == target:
                                sig = self._conditional_signature(
                                    cond_values, values_a, values_b)
                                if sig in self._poisoned_signatures:
                                    continue
                                yielded += 1
                                yield cond_expr, expr_a, expr_b
                                if yielded >= 6:
                                    break

        # Residual nested completion with PARTITION FAIRNESS.
        # Group outer predicates by effective truth-mask over the training
        # examples. Allocate a per-partition yield quota so no single broad
        # partition can consume the residual budget before other viable
        # partitions are explored. Within each partition, prefer smaller
        # region-correct branch expressions. Generic -- no mask special cases.
        if bools:
            n_total = n
            # --- bank: always keep size-0/1 first ---
            all_exprs = []
            for size, entries in bank_by_size.items():
                for expr, values, typ in entries:
                    if values is None or len(values) != n_total:
                        continue
                    all_exprs.append((expr, values))
            seen_c = set()
            size01, rest = [], []
            for expr, values in all_exprs:
                c = expr.canonical()
                if c in seen_c:
                    continue
                seen_c.add(c)
                if expr.size() <= 1:
                    size01.append((expr, values))
                else:
                    rest.append((expr, values))
            uniq = size01 + rest[:80]

            # --- group outer bools by truth mask ---
            # Include observationally-equivalent structural rivals kept
            # in _alt_exprs (dedup retains one bank representative but
            # stores alternate thresholds/predicates that agree on train).
            from collections import OrderedDict
            partitions = OrderedDict()  # mask -> list of (pred, values)
            bool_expanded = list(bools)
            for key, alts in (getattr(self, "_alt_exprs", None) or {}).items():
                for alt in alts:
                    try:
                        av = self._eval_all(alt, [dict(zip(
                            [p for p in (getattr(self, "_search_args_list", []) or [[]])[0].keys()]
                            if False else [], []))])
                    except Exception:
                        av = None
                    # Prefer reusing stored structure; evaluate against
                    # current args via search state if available.
            # Simpler expansion: re-evaluate alts on training args from bank
            args_for_eval = None
            for size, entries in bank_by_size.items():
                if entries:
                    # recover args_list from synthesizer search state
                    break
            args_list_ref = getattr(self, "_search_args_list", None)
            if args_list_ref and getattr(self, "_alt_exprs", None):
                for key, alts in self._alt_exprs.items():
                    for alt in alts:
                        if getattr(alt, "op", None) is None:
                            continue
                        try:
                            av = self._eval_all(alt, args_list_ref)
                        except Exception:
                            continue
                        if av is None or not all(isinstance(x, bool) for x in av):
                            continue
                        bool_expanded.append((alt, tuple(av)))
            for pred, pvals in bool_expanded:
                if len(pvals) != n_total:
                    continue
                mask = tuple(bool(x) for x in pvals)
                if not any(mask) or all(mask):
                    continue
                # Dedup by structure within partition
                plist = partitions.setdefault(mask, [])
                can = pred.canonical()
                if any(p.canonical() == can for p, _ in plist):
                    continue
                plist.append((pred, pvals))

            if not partitions:
                return

            # Per-partition quota: share residual slots fairly.
            # residual_budget total yields from residual path.
            residual_budget = 24 if self.max_size > 2 else 0
            if residual_budget <= 0:
                return
            n_parts = len(partitions)
            base_quota = max(1, residual_budget // n_parts)
            # First pass: up to base_quota per partition (round-robin order)
            # Second pass: fill remaining slots from any partition.
            part_items = list(partitions.items())
            # Stable order by mask cardinality then mask bits (deterministic,
            # not answer-directed)
            part_items.sort(key=lambda kv: (sum(kv[0]), kv[0]))

            def _region_match(values, idxs):
                return all(self._values_close(values[k], target[k]) for k in idxs)

            def _assemble_for_partition(mask, pred_list, quota, already):
                """Yield up to quota nested triples for this outer mask."""
                true_idx = [k for k in range(n_total) if mask[k]]
                false_idx = [k for k in range(n_total) if not mask[k]]
                thens = [(e, v) for e, v in uniq if _region_match(v, true_idx)]
                if not thens:
                    return
                thens_s = sorted(thens, key=lambda ev: ev[0].size())[:12]
                got = 0
                # Inner predicates: any bool with both residual regions non-empty
                for ipred, ivalues in bools:
                    if len(ivalues) != n_total:
                        continue
                    it = [k for k in false_idx if ivalues[k]]
                    iff = [k for k in false_idx if not ivalues[k]]
                    if not it or not iff:
                        continue
                    i_then = [(e, v) for e, v in uniq if _region_match(v, it)]
                    i_else = [(e, v) for e, v in uniq if _region_match(v, iff)]
                    if not i_then or not i_else:
                        continue
                    i_then_s = sorted(i_then, key=lambda ev: ev[0].size())[:8]
                    i_else_s = sorted(i_else, key=lambda ev: ev[0].size())[:8]
                    for outer_pred, _ in pred_list:
                        # skip if outer == inner structurally
                        for expr_a, values_a in thens_s:
                            for e1, v1 in i_then_s:
                                for e2, v2 in i_else_s:
                                    if e1.canonical() == e2.canonical():
                                        continue
                                    if outer_pred.canonical() == ipred.canonical():
                                        continue
                                    inner = Expr(
                                        op="if_else",
                                        children=(
                                            ("condition", ipred),
                                            ("then", e1),
                                            ("otherwise", e2),
                                        ),
                                    )
                                    inner_vals = tuple(
                                        v1[k] if ivalues[k] else v2[k]
                                        for k in range(n_total)
                                    )
                                    if not all(self._values_close(inner_vals[k], target[k])
                                               for k in false_idx):
                                        continue
                                    combined = tuple(
                                        values_a[k] if mask[k] else inner_vals[k]
                                        for k in range(n_total)
                                    )
                                    trace.candidates_tried += 1
                                    if not all(self._values_close(combined[k], target[k])
                                               for k in range(n_total)):
                                        continue
                                    sig = self._conditional_signature(
                                        mask, values_a, inner_vals)
                                    if sig in self._poisoned_signatures:
                                        continue
                                    yield outer_pred, expr_a, inner
                                    got += 1
                                    if got >= quota:
                                        return

            # Pass 1: fair quota per partition
            residual_yielded = 0
            part_yields = {m: 0 for m, _ in part_items}
            for mask, pred_list in part_items:
                if residual_yielded >= residual_budget:
                    break
                quota = min(base_quota, residual_budget - residual_yielded)
                for triple in _assemble_for_partition(mask, pred_list, quota, part_yields[mask]):
                    residual_yielded += 1
                    part_yields[mask] += 1
                    yielded += 1
                    yield triple
                    if residual_yielded >= residual_budget:
                        break
            # Pass 2: fill remaining budget from any partition (still bounded)
            if residual_yielded < residual_budget:
                for mask, pred_list in part_items:
                    if residual_yielded >= residual_budget:
                        break
                    extra = residual_budget - residual_yielded
                    # only if partition hasn't been fully explored relative to quota
                    for triple in _assemble_for_partition(
                            mask, pred_list, extra, part_yields[mask]):
                        residual_yielded += 1
                        part_yields[mask] += 1
                        yielded += 1
                        yield triple
                        if residual_yielded >= residual_budget:
                            break
            # Record fairness instrumentation on the trace
            try:
                covered = sum(1 for m, c in part_yields.items() if c > 0)
                trace.rejected.append(
                    "residual partition fairness: %d/%d partitions yielded, "
                    "counts=%s" % (
                        covered, n_parts,
                        sorted(((sum(m), c) for m, c in part_yields.items() if c > 0))[:12]))
            except Exception:
                pass
        return


    def _substitute_literal(self, expr: Expr, path: Tuple[int, ...],
                            new_value: float) -> Optional[Expr]:
        """Rebuild expr with the literal at `path` (child indices from the
        root) replaced by new_value. Generic tree surgery -- no domain
        knowledge."""
        if not path:
            if expr.is_literal:
                return Expr(literal=new_value, is_literal=True)
            return None
        if expr.is_leaf():
            return None
        idx = path[0]
        if idx < 0 or idx >= len(expr.children):
            return None
        new_children = []
        for j, (cname, child) in enumerate(expr.children):
            if j == idx:
                sub = self._substitute_literal(child, path[1:], new_value)
                if sub is None:
                    return None
                new_children.append((cname, sub))
            else:
                new_children.append((cname, child))
        return Expr(op=expr.op, children=tuple(new_children))

    def _predicate_variants(self, cond: Expr, cond_vals: Tuple[bool, ...],
                            args_list: List[Dict[str, Any]],
                            param_names: Sequence[str],
                            literals: List[float]):
        """Perturbed variants of a candidate predicate: every numeric
        literal in its own structure, substituted by every other literal
        in the pool's vocabulary and every midpoint between consecutive
        training values of the parameters the predicate mentions. Only
        variants agreeing with the candidate on EVERY training example
        are yielded ("same predicate mask"): they are rival explanations
        the evidence cannot distinguish. If any of them changes the
        conditional's output on a discriminating probe ("different value
        tuples on unconstrained positions"), the predicate's literals
        are underdetermined -- observational ambiguity. Generic: no
        domain vocabulary, only the evidence's and the pool's own values.

        Gap rivals: a threshold literal is behaviorally pinned only to
        the interval between consecutive training values -- ANY point
        strictly inside the same gap fits the training data identically,
        so the pool's discrete vocabulary can miss the rival entirely
        (e.g. threshold 50 with training values 40 and 60: no pool
        midpoint but 55 in (40, 60) is an equally simple rival). For
        each literal occurrence and each mentioned parameter, in-gap
        points bracketed by that parameter's own training values are
        proposed as substitutions; the same-mask check keeps only
        genuinely training-indistinguishable ones. Each kept gap rival
        also yields discriminating probes strictly inside the gap (where
        no training example can lie), so the probe check that follows
        is guaranteed a fair chance to tell candidate and rival apart.
        Yields (variant, mask, extra_probes) triples.
        """
        occurrences: List[Tuple[Tuple[int, ...], float]] = []
        mentioned: set = set()

        def walk(e: Expr, path: Tuple[int, ...]) -> None:
            if e.is_leaf():
                if e.is_literal and isinstance(e.literal, (int, float)) \
                        and not isinstance(e.literal, bool):
                    occurrences.append((path, float(e.literal)))
                elif not e.is_literal and e.param:
                    mentioned.add(e.param)
                return
            for j, (_, child) in enumerate(e.children):
                walk(child, path + (j,))

        walk(cond, ())
        if not occurrences:
            return
        subs = set(literals)
        param_vals: Dict[str, List[float]] = {}
        for p in mentioned:
            if p not in param_names:
                continue
            vals = sorted({float(a[p]) for a in args_list
                           if isinstance(a.get(p), (int, float))
                           and not isinstance(a.get(p), bool)})
            param_vals[p] = vals
            for x, y in zip(vals, vals[1:]):
                if y > x:
                    subs.add((x + y) / 2.0)
        # In-gap substitution candidates per literal value: (rival_value
        # -> [(literal_value, param)]). Bracketing uses the parameter's
        # own training values; open rays default to unit width. The
        # same-mask check below is the arbiter -- a proposal that does
        # not preserve training behavior is discarded, so this stays
        # sound for literals that are not thresholds too.
        gap_subs: Dict[float, List[Tuple[float, str]]] = {}
        for _path, old_v in occurrences:
            for p, vals in param_vals.items():
                lo = None
                hi = None
                for v in vals:
                    if v < old_v:
                        lo = v
                    elif v > old_v and hi is None:
                        hi = v
                if lo is None:
                    lo = old_v - 1.0
                if hi is None:
                    hi = old_v + 1.0
                for s in ((old_v + lo) / 2.0, (old_v + hi) / 2.0):
                    if s != old_v:
                        gap_subs.setdefault(s, []).append((old_v, p))
        cond_key = tuple(bool(v) for v in cond_vals)
        seen = set()
        count = 0
        base = args_list[0] if args_list else {}
        for path, old_v in occurrences:
            cands = set(subs)
            gap_src: Dict[float, List[Tuple[float, str]]] = {}
            for s, srcs in gap_subs.items():
                for (L, p) in srcs:
                    if L == old_v:
                        cands.add(s)
                        gap_src.setdefault(s, []).append((L, p))
            for s in sorted(cands):
                if s == old_v:
                    continue
                var = self._substitute_literal(cond, path, s)
                if var is None:
                    continue
                vcanon = var.canonical()
                if vcanon in seen:
                    continue
                seen.add(vcanon)
                mv = self._eval_all(var, args_list)
                if mv is None or len(mv) != len(cond_key):
                    continue
                if not all(isinstance(v, bool) for v in mv):
                    continue
                if tuple(bool(v) for v in mv) != cond_key:
                    continue
                count += 1
                # Discriminating probes strictly inside the gap between
                # the candidate literal and the rival: no training
                # example can lie there, and the candidate and rival
                # must disagree somewhere in it whenever the branches
                # differ. Quartiles make missing an isolated
                # branch-agreement point unlikely.
                xprobes: List[Dict[str, Any]] = []
                seen_xk = set()
                for (L, p) in gap_src.get(s, []):
                    for frac in (0.25, 0.5, 0.75):
                        xv = L + (s - L) * frac
                        d = dict(base)
                        d[p] = xv
                        try:
                            xk = repr(sorted((k, repr(v))
                                             for k, v in d.items()))
                        except Exception:
                            continue
                        if xk not in seen_xk:
                            seen_xk.add(xk)
                            xprobes.append(d)
                yield var, tuple(mv), xprobes
                if count >= 64:
                    return

    @staticmethod
    @staticmethod
    def _gate_consider_branch(expr: Expr, values, target: Tuple[Any, ...],
                              t0: Any, n: int,
                              all_branches: list, branch_seen: set) -> None:
        """Add one branch candidate to the gate's branch pool (with its
        target-match bitmask), unless incompatible or already present."""
        if len(values) != n:
            return
        for v in values:
            if isinstance(t0, bool) or isinstance(v, bool):
                if not (isinstance(v, bool) and isinstance(t0, bool)):
                    return
            elif isinstance(t0, (int, float)) and isinstance(v, (int, float)):
                continue
            elif type(v) is not type(t0):
                return
        c = expr.canonical()
        if c in branch_seen:
            return
        branch_seen.add(c)
        bits = 0
        for i, (v, t) in enumerate(zip(values, target)):
            if v == t:
                bits |= (1 << i)
        all_branches.append((expr, tuple(values), bits, c))

    def _conditional_gate_view(self, bank_by_size, target: Tuple[Any, ...],
                               args_list: List[Dict[str, Any]],
                               param_names: Sequence[str],
                               trace) -> Dict[str, Any]:
        """Per-level-end shared work for the conditional identifiability
        gate. The bank does not change while one level's stashed
        conditionals are gated, so the rival-predicate pool, the branch
        pool, the base probe set, and the probe-evaluation cache are
        built once here and shared by every _gate_conditional call at
        this level end. (Measured: 9 stashed conditionals each
        re-scanned a 5810-entry bank and re-evaluated every
        predicate/branch on every probe -- ~9s of a fail-closed search
        -- sharing makes the repeated work ~1x. The gate's decisions
        are unchanged: the shared structures are exactly what each
        call built for itself.)
        """
        n = len(target)
        # ---- rival predicates from the search's own bank ----
        # One representative per behaviorally distinct boolean tuple
        # (every training-distinguishable predicate behavior is
        # covered), plus structurally distinct same-tuple alternatives.
        pred_pool: List[Tuple[Expr, Tuple[bool, ...]]] = []
        pred_seen_masks = set()
        pred_seen_canon = set()
        entries = [e for v in bank_by_size.values() for e in v]
        for expr, values, _typ in entries:
            if len(values) != n or not all(isinstance(v, bool) for v in values):
                continue
            t = tuple(values)
            canon = expr.canonical()
            if canon in pred_seen_canon:
                continue
            pred_seen_canon.add(canon)
            key = self._value_key(t)
            bucket = self._alt_exprs.get(key, [])
            alts = [e for e in bucket if e.canonical() not in pred_seen_canon]
            for e in alts:
                pred_seen_canon.add(e.canonical())
            if key not in pred_seen_masks:
                pred_seen_masks.add(key)
                pred_pool.append((expr, t))
                pred_pool.extend((e, t) for e in alts[:5])
            else:
                # Same mask as an earlier predicate, but structurally
                # distinct spellings are still rival explanations worth
                # probing (they may diverge off the training inputs).
                pred_pool.extend((e, t) for e in alts[:2])
            if len(pred_pool) >= 64:
                break

        # ---- branches: EVERY type-compatible bank entry ----
        # (plus the observational-dedup-hidden alternatives), with its
        # target-match mask as a bitmask. Deliberately no positional
        # cap here: a cap that drops the candidate's own branches makes
        # the gate vacuous (nothing fits, everything looks
        # identifiable). Per-predicate fitting sets are computed lazily
        # with bit operations instead, and diversity caps apply per
        # predicate.
        t0 = target[0]
        all_branches: List[Tuple[Expr, Tuple[Any, ...], int, str]] = []
        branch_seen = set()
        for expr, values, _typ in entries:
            self._gate_consider_branch(expr, values, target, t0, n,
                                       all_branches, branch_seen)
            if len(values) == n:
                key = self._value_key(tuple(values))
                for e in self._alt_exprs.get(key, []):
                    ev = self._eval_all(e, args_list)
                    if ev is not None:
                        self._gate_consider_branch(e, ev, target, t0, n,
                                                   all_branches, branch_seen)

        # ---- discriminating probes from the evidence's own geometry ----
        literals = self._numeric_literals(
            [p for p, _ in pred_pool] + [b for b, _, _, _ in all_branches])
        probes = self._discrimination_probes(args_list, param_names, literals)
        # Precomputed value-tuple reprs, parallel to all_branches: the
        # per-predicate diversity-capped branch selection sorts by these,
        # and recomputing repr per sort was the gate's hottest cost.
        branch_rv = [repr(b[1]) for b in all_branches]
        return {"n": n, "pred_pool": pred_pool, "all_branches": all_branches,
                "branch_seen": branch_seen, "literals": literals,
                "probes": probes, "probe_cache": {}, "t0": t0,
                "branch_rv": branch_rv}

    def _gate_conditional(self, cand: Expr, target: Tuple[Any, ...],
                          args_list: List[Dict[str, Any]],
                          param_names: Sequence[str],
                          view: Dict[str, Any], trace) -> Tuple[bool, Dict[str, Any]]:
        """Identifiability gate: is this training-fitting conditional the
        only explanation the explored evidence supports, or do rival
        training-consistent explanations disagree on discriminating
        probe inputs?

        Rival explanations are built from the search's OWN bank --
        every behaviorally distinct boolean tuple in it as a candidate
        predicate (plus structurally distinct same-tuple alternatives
        the observational dedup hid), every type-compatible candidate
        as a branch -- combined freely and kept only if the combination
        fits every training example. Each fitting rival is evaluated on
        the bounded probe set; any probe-tuple disagreement with the
        candidate means the evidence does not identify the conditional,
        and the gate fails closed (suppress + poison the shape).

        Returns (identifiable, detail). Generic: no vocabulary, no
        thresholds, no domain predicates anywhere in this logic.
        """
        detail: Dict[str, Any] = {"conditional": cand.canonical()}
        parts = self._decompose_conditional(cand)
        if parts is None:
            detail["reason"] = "undecomposable conditional shape"
            self._suppress_ambiguous(cand, None, None, None, detail, trace)
            return False, detail
        cond, a_expr, b_expr = parts
        n = len(target)

        cond_vals = self._eval_all(cond, args_list)
        a_vals = self._eval_all(a_expr, args_list)
        b_vals = self._eval_all(b_expr, args_list)
        if cond_vals is None or a_vals is None or b_vals is None:
            detail["reason"] = "conditional does not evaluate on training"
            self._suppress_ambiguous(cand, None, None, None, detail, trace)
            return False, detail
        if not all(isinstance(v, bool) for v in cond_vals):
            detail["reason"] = "predicate is not boolean-valued"
            self._suppress_ambiguous(cand, None, None, None, detail, trace)
            return False, detail
        if not all((a_vals[i] if cond_vals[i] else b_vals[i]) == target[i]
                   for i in range(n)):
            detail["reason"] = "conditional does not fit training data"
            self._suppress_ambiguous(cand, None, None, None, detail, trace)
            return False, detail

        # ---- rival pools: shared per-level-end view ----
        # pred_pool / all_branches / base probes were built once for this
        # level's whole stashed set (the bank is unchanged across these
        # calls). Only the candidate-first ordering and the candidate's
        # own branches are per-candidate work.
        n = view["n"]
        t0 = view["t0"]
        pred_pool = sorted(
            view["pred_pool"],
            key=lambda pt: 0 if pt[0].canonical() == cond.canonical() else 1)
        # The candidate's own branches are always present: without them
        # even self-consistency cannot be checked. Copied per candidate
        # so the shared view is never mutated.
        all_branches = list(view["all_branches"])
        branch_seen = set(view["branch_seen"])
        branch_rv = list(view["branch_rv"])
        for e in (a_expr, b_expr):
            ev = self._eval_all(e, args_list)
            if ev is not None:
                _before = len(all_branches)
                self._gate_consider_branch(e, ev, target, t0, n,
                                           all_branches, branch_seen)
                for _b in all_branches[_before:]:
                    branch_rv.append(repr(_b[1]))

        # ---- discriminating probes from the evidence's own geometry ----
        # Base probes are shared from the view; this candidate's gap
        # rivals may extend them (deduped). Probe evaluations are shared
        # too: view["probe_cache"] maps canonical -> tuple over the base
        # probes, filled lazily across this level's gate calls, so the
        # same predicate/branch is never re-evaluated on the same probe
        # twice. A candidate with gap probes extends cached base tuples
        # rather than recomputing them.
        literals = view["literals"]
        base_probes = view["probes"]
        probe_cache = view["probe_cache"]
        variants = list(self._predicate_variants(
            cond, tuple(cond_vals), args_list, param_names, literals))
        _pkeys = set()
        for _d in base_probes:
            try:
                _pkeys.add(repr(sorted((k, repr(_v))
                                       for k, _v in _d.items())))
            except Exception:
                pass
        gap_probes: List[Dict[str, Any]] = []
        for _var, _mask, _xprobes in variants:
            for _d in _xprobes:
                try:
                    _xk = repr(sorted((k, repr(_v))
                                       for k, _v in _d.items()))
                except Exception:
                    continue
                if _xk not in _pkeys:
                    _pkeys.add(_xk)
                    gap_probes.append(_d)
        probes = base_probes + gap_probes
        n_base = len(base_probes)

        def probe_tuple(expr: Expr) -> Tuple[Any, ...]:
            c = expr.canonical()
            t = probe_cache.get(c)
            if t is not None:
                if not gap_probes:
                    return t
                ext = t + tuple(self._safe_eval(expr, pr)
                                for pr in gap_probes)
                trace.ambiguity_probe_evals += len(gap_probes)
                return ext
            full = tuple(self._safe_eval(expr, pr) for pr in probes)
            trace.ambiguity_probe_evals += len(probes)
            probe_cache[c] = full[:n_base]
            return full

        cond_p = probe_tuple(cond)
        a_p = probe_tuple(a_expr)
        b_p = probe_tuple(b_expr)
        cand_probe = tuple(a_p[k] if cond_p[k] else b_p[k]
                           for k in range(len(probes)))

        def probe_disagrees(p_p, ap_p, bp_p) -> Optional[int]:
            for k in range(len(probes)):
                rv = ap_p[k] if p_p[k] else bp_p[k]
                cv = cand_probe[k]
                if not self._values_close(rv, cv):
                    return k
            return None

        # ---- Mechanism 1 loop: fitting bank-rival combinations ----
        a_canon = a_expr.canonical()
        b_canon = b_expr.canonical()
        # Branch ordering is sorted once per side per candidate (by
        # preferred-spelling-last, value-tuple, spelling) instead of
        # re-sorting the fitting subset per predicate: the old
        # per-predicate sort was the gate's hottest cost (a predicate
        # selecting nothing fits EVERY branch, sorting thousands).
        # The linear diversity-capped scan below visits fitting
        # branches in exactly the same order the old sort produced.
        def _side_order(pref_canon: str):
            return sorted(
                range(len(all_branches)),
                key=lambda i: (0 if all_branches[i][3] != pref_canon else 1,
                               branch_rv[i], all_branches[i][3]))

        def _fit_capped(order, need: int):
            out = []
            seen_counts: Dict[str, int] = {}
            for i in order:
                if (all_branches[i][2] & need) != need:
                    continue
                tk = branch_rv[i]
                c = seen_counts.get(tk, 0)
                if c >= 2:
                    continue
                seen_counts[tk] = c + 1
                out.append(i)
                if len(out) >= 12:
                    break
            return out

        order_a = _side_order(a_canon)
        order_b = _side_order(b_canon)
        for pi, (p_expr, p_vals) in enumerate(pred_pool):
            p_p = probe_tuple(p_expr)
            need_a = 0
            need_b = 0
            for i, pv in enumerate(p_vals):
                if pv:
                    need_a |= (1 << i)
                else:
                    need_b |= (1 << i)
            # Branch candidates that can serve this predicate: must match
            # the target everywhere the predicate selects them.
            fit_a = _fit_capped(order_a, need_a)
            fit_b = _fit_capped(order_b, need_b)
            if not fit_a or not fit_b:
                continue
            for bi in fit_a:
                b_a_expr, _, _, b_a_canon = all_branches[bi]
                ap_p = probe_tuple(b_a_expr)
                for bj in fit_b:
                    b_b_expr, _, _, b_b_canon = all_branches[bj]
                    if pi == 0 and b_a_canon == a_canon \
                            and b_b_canon == b_canon:
                        continue  # the candidate itself
                    bp_p = probe_tuple(b_b_expr)
                    k = probe_disagrees(p_p, ap_p, bp_p)
                    if k is not None:
                        # Occam's razor: fail closed only on ambiguity
                        # between equally-simple (or simpler) explanations.
                        # A disagreeing rival that is strictly more complex
                        # than the candidate does not make the candidate
                        # unidentifiable -- the candidate is the unique
                        # simplest fitting explanation, so this rival is
                        # not a legitimate tie. (Without this, a rich
                        # enough bank always yields a contrived
                        # more-complex rival, and the gate would fail
                        # always instead of fail closed.)
                        rival_size = (1 + p_expr.size()
                                      + b_a_expr.size() + b_b_expr.size())
                        if rival_size > cand.size():
                            continue
                        detail["reason"] = (
                            "rival bank explanation fits training but "
                            "disagrees on a discriminating probe")
                        self._suppress_ambiguous(
                            cand, p_expr, (b_a_expr, b_b_expr), probes[k],
                            detail, trace,
                            rival_probe=(ap_p[k] if p_p[k] else bp_p[k]),
                            cand_probe_val=cand_probe[k])
                        return False, detail

        # ---- Mechanism 2: predicate literal perturbation ----
        # Same predicate mask on training + same branches (hence same
        # target agreement), but the predicate's literals may be
        # underdetermined by the evidence: a perturbed variant that
        # still fits every training example, yet changes the
        # conditional's output on a discriminating probe, proves the
        # evidence does not identify the conditional.
        for var, _var_mask, _xprobes in variants:
            var_p = probe_tuple(var)
            k = probe_disagrees(var_p, a_p, b_p)
            if k is not None:
                detail["reason"] = (
                    "predicate literal underdetermined: %s agrees with "
                    "the candidate predicate on all %d training examples "
                    "but the conditional disagrees on a discriminating probe"
                    % (var.canonical()[:120], n))
                self._suppress_ambiguous(
                    cand, var, (a_expr, b_expr), probes[k],
                    detail, trace,
                    rival_probe=(a_p[k] if var_p[k] else b_p[k]),
                    cand_probe_val=cand_probe[k])
                return False, detail

        detail["reason"] = ("no rival training-consistent explanation "
                            "disagrees on %d discriminating probes "
                            "(%d bank predicates + predicate-literal "
                            "perturbations examined)"
                            % (len(probes), len(pred_pool)))
        trace.rejected.append(
            "conditional identifiable: %s" % detail["reason"])
        return True, detail

    def _stable_tiebreak(self, op: str) -> int:
        """A deterministic, process-independent ordering key — Python's
        built-in hash() is randomized per-process for strings (PYTHONHASHSEED)
        specifically to prevent it being relied on this way, so using it here
        would have made search order (and therefore which composition gets
        found and persisted) vary across restarts, which is incompatible
        with everything in this architecture that assumes a stored plan
        reflects a reproducible search outcome."""
        import hashlib
        return int(hashlib.md5(op.encode()).hexdigest(), 16) % 100000

    def _relevance_tiebreak(self, expr: Expr) -> float:
        """Middle tiebreak for pool ordering: input-dependency relevance.

        Param leaves sort most-relevant-first (rank 0 = most relevant, from
        the dependency analyzer's value-based ranking). Literals and
        composite expressions are neutral (they are not inputs) and fall
        through to the hash tiebreak. This only reorders among equal
        _entry_priority -- pool MEMBERSHIP is unchanged, so completeness
        is preserved; it just tries relevant inputs' combinations first.
        """
        if expr.is_leaf() and not expr.is_literal and expr.param is not None:
            rank = getattr(self, "_relevance_rank", None) or {}
            if expr.param in rank:
                return float(rank[expr.param])
        return 1e9

    def _expr_matches_tree(self, expr: Expr, tree: Any) -> bool:
        """Structural match of an Expr against a hypothesis tree.

        ``tree`` is an input name (str -- matches a param leaf) or a
        StructuralHypothesis (matches op + children). Compares operator
        and leaf parameter names only -- never values, positions, or
        test vocabulary. For commutative ops the child order is ignored
        (the probe canonicalized it); for non-commutative ops the order
        must match exactly, because subtract(a, b) and subtract(b, a)
        are different computations.
        """
        import itertools
        if isinstance(tree, str):
            return (expr.is_leaf() and not expr.is_literal
                    and expr.param == tree)
        if expr.is_leaf() or expr.op != tree.op:
            return False
        kids = [c for _, c in expr.children]
        tcs = tree.children
        if len(kids) != len(tcs):
            return False
        # tree is a StructuralHypothesis here (non-str); commutative
        # check is by op name.
        if tree.op in self._COMMUTATIVE_OPS:
            for perm in itertools.permutations(range(len(kids))):
                if all(self._expr_matches_tree(kids[p], tc)
                       for p, tc in zip(perm, tcs)):
                    return True
            return False
        return all(self._expr_matches_tree(k, tc)
                   for k, tc in zip(kids, tcs))

    def _leaf_level_nodes(self, op: str) -> List[Any]:
        """Hypothesis subtrees rooted at ``op`` whose children are all
        leaves (input names). These are the size-1 building blocks the
        probe claims are useful; at the size-1 level their leaf wirings
        sort first in the slot pools."""
        found: List[Any] = []
        seen_ids = set()

        def _walk(t: Any) -> None:
            if not isinstance(t, str) and id(t) not in seen_ids:
                seen_ids.add(id(t))
                if t.op == op and all(isinstance(c, str)
                                      for c in t.children):
                    found.append(t)
                for c in t.children:
                    if not isinstance(c, str):
                        _walk(c)

        for h in (getattr(self, "_structural_hyps", None) or []):
            _walk(h)
        return found

    def _hyp_pool_rank(self, op: str, size_partition: Tuple[int, ...],
                       slot_idx: int, expr: Expr) -> int:
        """0 when expr matches a structural-hypothesis pattern for this
        (op, partition, slot), else 1.

        Prepended to the slot-pool sort key so hypothesized structure
        sorts first. Pool MEMBERSHIP is untouched: every entry is still
        present and reachable, so completeness is preserved -- this is
        ordering, not pruning.

        Two pattern shapes, both derived from the value-based structural
        probe, never from test vocabulary:
        - firing level (target_size == hypothesis size): the root op's
          slots prioritize bank entries structurally matching the
          hypothesis's child subtrees;
        - size-1 level: a leaf-level hypothesis node's op sorts its
          hypothesized leaf parameters first in each slot.
        """
        hyps = getattr(self, "_structural_hyps", None) or []
        if not hyps:
            return 1
        # Firing level: root-op slots match child subtrees by SIZE. For
        # a non-commutative root the slot POSITION must also align with
        # the hypothesized wiring; for a commutative root the sweep
        # canonicalizes partitions (mirror skipped), so any child of
        # the right size matches any slot of that size.
        for h in hyps:
            if h.size() != sum(size_partition) + 1:
                continue
            if h.op != op:
                continue
            if op in self._COMMUTATIVE_OPS:
                for child in h.children:
                    csize = (child.size() if not isinstance(child, str)
                             else 0)
                    if csize == size_partition[slot_idx]:
                        if self._expr_matches_tree(expr, child):
                            return 0
            elif tuple(h.child_sizes()) == tuple(size_partition):
                child = h.children[slot_idx]
                if self._expr_matches_tree(expr, child):
                    return 0
        # Size-1 level: leaf-level nodes sort their leaves first.
        if all(s == 0 for s in size_partition):
            for node in self._leaf_level_nodes(op):
                leaves = [c for c in node.children]  # all str
                if slot_idx < len(leaves):
                    want = leaves[slot_idx]
                    # For commutative leaf-level ops the slot order is
                    # meaningless; accept any of the node's leaves.
                    if op in self._COMMUTATIVE_OPS:
                        if (expr.is_leaf() and not expr.is_literal
                                and expr.param in leaves):
                            return 0
                    elif (expr.is_leaf() and not expr.is_literal
                            and expr.param == want):
                        return 0
        return 1

    def _entry_priority(self, expr: Expr, values: Optional[Tuple[Any, ...]] = None,
                        target: Optional[Tuple[Any, ...]] = None) -> float:
        """Priority used to decide which bank entries survive pool_per_type
        truncation. A leaf (parameter or literal) is always maximally
        available -- it costs nothing to keep every leaf, there are only a
        handful.

        A composite expression's priority combines two signals: how many
        training examples its own already-computed value tuple exactly
        matches the target output for (found directly, from the
        threshold-multiplier investigation: every sub-expression sharing
        one operator, e.g. every multiply(signal, K) for every candidate
        constant K, previously scored identically by op-bias alone, so the
        one specific K that actually agreed with the data had no way to
        survive pool_per_type truncation ahead of the others), and its
        root op's current bias score as a secondary tiebreak within the
        same match count -- the same signal that already orders which ops
        get tried, now also deciding which of their PAST results remain
        reachable as arguments to later, larger expressions.
        """
        if expr.is_leaf():
            return 2.0
        op_score = self.bias.score_op(expr.op)
        _prim = self.reg.get(expr.op) if expr.op else None
        if _prim is not None and getattr(_prim, "family", "") in ("acquired", "promoted"):
            _param_bound = any(
                (not c.is_leaf()) or (c.param is not None)
                for _, c in (expr.children or ()))
            return (500000.0 if _param_bound else 100000.0) + op_score
        if values is None or target is None or len(values) != len(target):
            return op_score
        match_count = sum(1 for v, t in zip(values, target) if v == t)
        return match_count * 1000.0 + op_score

    def _size_partitions(self, total: int, slots: int):
        """Every way to write `total` as an ordered sum of `slots`
        non-negative integers — small and cheap for the arities and sizes
        this operates at (at most a handful of required arguments, at most
        a few levels of size)."""
        if slots == 0:
            if total == 0:
                yield ()
            return
        if slots == 1:
            yield (total,)
            return
        for first in range(total + 1):
            for rest in self._size_partitions(total - first, slots - 1):
                yield (first,) + rest

    def _bounded_product(self, pools: List[List[Any]]):
        import itertools
        if not pools:
            return
        sizes = [len(p) for p in pools]
        if any(s == 0 for s in sizes):
            return
        total = 1
        for s in sizes:
            total *= s
            if total > self.max_arg_combinations * 20:
                total = None
                break
        if total is None or len(pools) == 1:
            # Too large to materialize and sort safely, or nothing to
            # reorder for a single slot -- fall back to the original,
            # unchanged lexicographic behavior.
            count = 0
            for combo in itertools.product(*pools):
                if count >= self.max_arg_combinations:
                    return
                count += 1
                yield combo
            return
        # Diagonal (balanced) order: sort by how deep into any single
        # slot's pool a combination reaches (max index used), so the
        # first max_arg_combinations combinations sample across ALL
        # slots rather than exhausting the last slot's full range while
        # earlier slots stay pinned to their single highest-priority
        # entry. Ties broken by sum-of-indices for a smooth diagonal
        # sweep rather than an arbitrary one.
        index_combos = list(itertools.product(*(range(s) for s in sizes)))
        index_combos.sort(key=lambda idxs: (max(idxs), sum(idxs)))
        count = 0
        for idxs in index_combos:
            if count >= self.max_arg_combinations:
                return
            count += 1
            yield tuple(pools[k][idxs[k]] for k in range(len(pools)))

    def _eval(self, expr: Expr, args: Dict[str, Any]) -> Any:
        from swarm_engine.cognition.representations import evaluate_expr
        return evaluate_expr(expr, args, self.reg)

    def _eval_all(self, expr: Expr, args_list: List[Dict[str, Any]]
                 ) -> Optional[Tuple[Any, ...]]:
        out = []
        for args in args_list:
            try:
                out.append(self._eval(expr, args))
            except Exception:
                return None
        return tuple(out)

    def _apply_across_examples(self, prim, required: List[str],
                               kwargs_values: Dict[str, Tuple[Any, ...]],
                               n: int) -> Optional[Tuple[Any, ...]]:
        out = []
        for i in range(n):
            try:
                out.append(prim.fn(**{name: kwargs_values[name][i] for name in required}))
            except Exception:
                return None
        return tuple(out)

    def _value_key(self, values: Tuple[Any, ...]) -> str:
        try:
            return repr(values)
        except Exception:
            return str(id(values))

    def _plan_typechecks(self, plan: Dict[str, Any]) -> bool:
        """True iff Composer static analysis accepts the plan."""
        try:
            from swarm_engine.synthesis.composer import Composer
            analysis = Composer(self.reg).analyze(plan)
            if isinstance(analysis, dict):
                return bool(analysis.get("ok", False))
            return bool(getattr(analysis, "ok", False))
        except Exception:
            return False

    def _finish(self, expr: Expr, param_names: Sequence[str],
               trace: SynthesisTrace) -> Hypothesis:
        plan = self._build_general_plan(expr, param_names)
        ops = expr.ops_used()
        derivation = (f"constructed {expr.size()} primitive application(s), "
                     f"arity-general search: {' -> '.join(ops) if ops else '(identity)'}")
        return Hypothesis(plan=plan, op_sequence=ops, origin="synthesis",
                          derivation=derivation, expr=expr)

    def _build_general_plan(self, expr: Expr, param_names: Sequence[str]
                            ) -> Dict[str, Any]:
        steps: List[Dict[str, Any]] = []
        counter = [0]

        def compile_node(node: Expr) -> Dict[str, Any]:
            if node.is_leaf():
                return node.literal if node.is_literal else {"$param": node.param}
            child_refs = {name: compile_node(child) for name, child in node.children}
            counter[0] += 1
            step_id = f"s{counter[0]}"
            steps.append({"id": step_id, "op": node.op, "args": child_refs})
            return {"$step": step_id}

        output_ref = compile_node(expr)
        return {"name": "synthesized_general",
                "params": {p: "any" for p in param_names},
                "steps": steps, "output": output_ref}


class ExpressivenessAnalyzer:
    """Distinguishes "the search ran out of budget" from "the current
    primitive vocabulary cannot express this at all" (item 2).

    The mechanism is a type-reachability closure: starting from the types of
    the problem's input parameters, repeatedly ask which primitives' full
    argument lists can be filled from types already reached, and add their
    output types to the reached set. This costs nothing to compute (no
    values are evaluated, only types) and is exact for a well-defined
    question: is the goal's output type reachable from its input types under
    the primitive vocabulary at all? If the target type never enters the
    closure, no amount of additional search budget, search order, or luck
    would ever find a matching expression — the vocabulary itself is
    insufficient, and that is worth recording as a structurally different
    kind of failure than "the search timed out."

    Honest scope: this checks type reachability, not value reachability. A
    goal can be type-reachable and still have no expression producing the
    exact right VALUES (the actual search question) — the closure only rules
    out the strictly worse case where not even the TYPE is achievable.
    """

    def __init__(self, registry):
        self.reg = registry
        self._ops = [n for n in registry.names()
                     if not registry.get(n).variadic
                     and registry.get(n).effects == (Effect.PURE,)]

    def reachable_types(self, param_types: Dict[str, Any], max_rounds: int = 6) -> List[Any]:
        reached = list(dict.fromkeys(param_types.values()))  # de-duplicated, order-preserving
        for _ in range(max_rounds):
            added_this_round = False
            for name in self._ops:
                prim = self.reg.get(name)
                required = [k for k, v in prim.inputs.items() if not v.optional]
                if not required:
                    continue
                if all(any(t.accepts(rt) for rt in reached)
                      for t in (prim.inputs[a] for a in required)):
                    if not any(rt.kind == prim.output.kind and rt.args == prim.output.args
                              for rt in reached):
                        reached.append(prim.output)
                        added_this_round = True
            if not added_this_round:
                break
        return reached

    def is_expressible(self, param_types: Dict[str, Any], target_type: Any) -> bool:
        reached = self.reachable_types(param_types)
        return any(t.kind == target_type.kind and t.args == target_type.args
                   for t in reached)

    def diagnose(self, examples: Sequence[Tuple[Dict[str, Any], Any]],
                param_names: Sequence[str],
                unsatisfiable_args: Optional[Sequence[Dict[str, Any]]] = None
                ) -> Dict[str, Any]:
        """A ready-to-use verdict for a specific (examples, params) problem,
        inferring types from the first example's values.

        When search reports unsatisfiable_args (M+4), distinguish I/O type
        reachability from intermediate constructibility failures.
        """
        if not examples:
            return {"checked": False, "reason": "no examples supplied"}
        first_args, first_output = examples[0]
        param_types = {p: infer(first_args[p]) for p in param_names if p in first_args}
        target_type = infer(first_output)
        expressible = self.is_expressible(param_types, target_type)
        exclusions = list(unsatisfiable_args or [])
        # Only treat as structural constructibility failure when a required
        # type has zero producers in the registry (e.g. CALLABLE), not when
        # the bank is temporarily empty for a producible type (str/dict/…).
        hard = []
        for ex in exclusions:
            rt = str(ex.get("required_type", "")).lower()
            if "callable" in rt:
                # No primitive currently outputs CALLABLE.
                hard.append(ex)
        structural = bool(hard)
        constructible = not structural
        if structural and expressible:
            reason = (
                "target I/O types are reachable under the primitive vocabulary, "
                "but search encountered required argument types with no "
                "constructible producer in the current expression grammar "
                f"(unsatisfiable_args={hard[:5]})"
            )
        elif expressible:
            reason = (
                "the target type is reachable from the input types "
                "under the current primitive vocabulary; a search "
                "failure here means the search needs more budget, "
                "different structure, or the exact VALUES are not "
                "reachable even though the type is"
            )
        else:
            reason = (
                "the target type is NOT reachable from the input "
                "types under any composition of the current "
                "primitive vocabulary; this is a vocabulary gap, "
                "not a search-budget problem — no amount of "
                "additional search would find an answer"
            )
        return {
            "checked": True,
            "expressible_by_type": expressible,
            "constructible_by_current_search": constructible,
            "structural_exclusion": structural,
            "unsatisfiable_args": exclusions,
            "param_types": {k: str(v) for k, v in param_types.items()},
            "target_type": str(target_type),
            "reason": reason,
        }
