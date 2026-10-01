#!/usr/bin/env python3
"""Input-dependency analysis: which inputs actually matter to an output.

Given behavioral examples (input dicts -> output), infers a relevance
ranking over the inputs WITHOUT knowing the target program and WITHOUT
using input names or positions -- every signal is computed from the
examples' VALUES only.

Two evidence signals:

1. Exact relational probe (strong): for each ordered input pair and a
   small set of GENERIC binary templates (add/subtract/multiply -- the
   same class of domain-free arithmetic the synthesizer itself searches),
   the fraction of examples where the template applied to the pair EXACTLY
   equals the output. A 1.0 fit is strong evidence that both inputs
   participate (and suggests the operator). This is inference from the
   caller's own examples, not an answer supplied by the test: the
   templates are fixed and generic, the test never names inputs or ops.

2. Statistical dependence (general): absolute Pearson correlation between
   each input's value vector and the output vector. Catches monotone-ish
   relationships the template set misses (deeper expressions, non-template
   ops). Weak with few examples, but strictly better than random order.

Also computed: a SOUND lower bound -- inputs PROVEN relevant by the
singleton-difference test (two examples differing in exactly one input
with different outputs). Rarely fires on random examples, but when it
does the input is pinned to the top of the ranking by proof, not by
heuristic.

Output is SEARCH GUIDANCE, not a decision: relevance scores, a ranking,
and high-confidence (op, inputs) hypotheses from exact fits. The
synthesizer still constructs, validates, and admits through its normal
path; when the guidance is wrong the complete search continues
unchanged. Completeness and minimality are preserved by construction --
this only changes the ORDER in which candidates are tried.

A sound OVER-approximation (a set guaranteed to contain every relevant
input) is impossible from finite examples alone: for any input, a law
that ignores it and a law that uses it outside the observed range agree
on all examples but differ in relevance. Hence heuristic ranking +
validation backstop + complete fallback, not hard pruning.
"""

from __future__ import annotations

import hashlib
import math
import random
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple, Union


# Generic binary templates for the exact relational probe. Each is a pure
# function of two values; the NAME is the registry primitive it suggests.
# Deliberately small and fixed: these are the domain-free arithmetic
# operators, not task-specific vocabulary.
_PROBE_TEMPLATES: Tuple[Tuple[str, Any], ...] = (
    ("add", lambda x, y: x + y),
    ("subtract", lambda x, y: x - y),
    ("multiply", lambda x, y: x * y),
)


def _is_number(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


@dataclass
class OpHypothesis:
    """A (primitive, inputs) combination that exactly fits the examples.

    Produced by the exact relational probe. The synthesizer may try these
    first as search guidance; they are not decisions.
    """
    op: str
    inputs: Tuple[str, ...]
    fit_fraction: float = 1.0


@dataclass
class StructuralHypothesis:
    """A tree-structured exact-fit hypothesis from the value probe.

    Discovered by the structural relational probe from the examples'
    VALUES only -- never names, positions, or any target-expression
    knowledge. The probe templates are the same small fixed set of
    generic binary operators the synthesizer itself searches
    (add/subtract/multiply), so this is a structural pre-filter over
    the synthesizer's own space, not an answer supplied from outside.

    A tree generalizes the old two-level chain: ``op`` is a registry op
    name, and each child is either an input name (str -- a size-0 leaf)
    or a nested StructuralHypothesis (a computed intermediate). A
    depth-2 chain ``outer(inner(x, y), z)`` is the tree
    ``StructuralHypothesis(outer, (StructuralHypothesis(inner, (x, y)),
    z))``; a balanced depth-3 structure ``(a+b)*(c+d)`` is
    ``StructuralHypothesis('*', (StructuralHypothesis('add', ('a','b')),
    StructuralHypothesis('add', ('c','d'))))``.

    Search guidance only: the synthesizer tries these structures first
    but still constructs, validates, and admits every candidate through
    its normal path (dedup, safety, identifiability gate). A wrong
    hypothesis costs a few wasted candidates, never a wrong admission.
    """
    op: str
    children: Tuple[Union[str, 'StructuralHypothesis'], ...]
    fit_fraction: float = 1.0
    # Structural rivals: distinct trees with the same training value
    # vector that disagree on synthetic probes (evidence-bounded
    # underdetermination). Populated by _find_structural_rivals after the
    # probe returns hypotheses; empty in the well-determined case.
    rivals: List['StructuralHypothesis'] = field(default_factory=list)
    # R2'' provenance: for dependency-expansion rivals, rival_kind is
    # "expansion" and expands_leaf names the functionally-determined
    # leaf that was replaced. For dependent-subtree alternatives
    # (gap (c)) expands_leaf is None and expands_subtree carries the
    # replaced subexpression as a compact s-expression. None for
    # ordinary hypotheses/rivals. Synthetic disagreement proves
    # behavioral DISTINCTION only, never which law is true.
    rival_kind: Optional[str] = None
    expands_leaf: Optional[str] = None
    expands_subtree: Optional[str] = None

    def size(self) -> int:
        """Number of op applications in the tree (matches Expr.size)."""
        return 1 + sum(c.size() if isinstance(c, StructuralHypothesis)
                       else 0 for c in self.children)

    def all_ops(self) -> List[str]:
        """All op names, root first then depth-first."""
        ops = [self.op]
        for c in self.children:
            if isinstance(c, StructuralHypothesis):
                ops.extend(c.all_ops())
        return ops

    def leaf_names(self) -> List[str]:
        """Input names at the leaves, left to right."""
        names: List[str] = []
        for c in self.children:
            if isinstance(c, str):
                names.append(c)
            else:
                names.extend(c.leaf_names())
        return names

    def child_sizes(self) -> Tuple[int, ...]:
        """Size of each child (0 for leaves)."""
        return tuple(c.size() if isinstance(c, StructuralHypothesis)
                     else 0 for c in self.children)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "op": self.op,
            "children": [c.to_dict() if isinstance(c, StructuralHypothesis)
                         else c for c in self.children],
            "size": self.size(),
            "fit_fraction": self.fit_fraction,
            "rivals": [r.to_dict() for r in self.rivals],
            "rival_kind": self.rival_kind,
            "expands_leaf": self.expands_leaf,
            "expands_subtree": self.expands_subtree,
        }


@dataclass
class InputDependencyReport:
    scores: Dict[str, float] = field(default_factory=dict)
    ranking: List[str] = field(default_factory=list)
    hypotheses: List[OpHypothesis] = field(default_factory=list)
    composite_hypotheses: List[StructuralHypothesis] = field(
        default_factory=list)
    proven_relevant: Set[str] = field(default_factory=set)
    n_examples: int = 0
    probe_stats: Dict[str, Any] = field(default_factory=dict)


def _stable_tiebreak(values: Tuple[Any, ...]) -> str:
    """Deterministic tiebreak from VALUES only -- never names/positions."""
    h = hashlib.sha256()
    for v in values:
        h.update(repr(v).encode("utf-8", "replace"))
        h.update(b"\x00")
    return h.hexdigest()


def _pearson(xs: Sequence[float], ys: Sequence[float]) -> float:
    n = len(xs)
    if n < 2:
        return 0.0
    mx = sum(xs) / n
    my = sum(ys) / n
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    sxx = sum((x - mx) ** 2 for x in xs)
    syy = sum((y - my) ** 2 for y in ys)
    if sxx <= 0.0 or syy <= 0.0:
        return 0.0
    return sxy / math.sqrt(sxx * syy)


# Time budget for the size-9 (4,4) add/subtract search: the probe
# pass streams the FULL inner-pair set (no hit cap -- hits are
# deduplicated by semantic key and only the final list is
# relevance-ordered) and aborts on budget rather than hanging on the
# pathological negative case (a complete negative result costs
# ~8-11 min at 10 inputs). Recorded honestly as addsub_44_timeout;
# the synthesizer's complete fallback covers a truncated search.
# Follows the _search_45 precedent. Module-level so tests can
# monkeypatch it.
_SEARCH_44_ADDSUB_BUDGET_S = 900.0
# Combo-count threshold for the (4,4) add/sub mate index: at or below
# it the complete B_4 set is materialized as a Python set (exact,
# fastest lookups); above it the Bloom-filter index is used (compact,
# no false negatives, false positives resolved by the complete
# witness). Measured ~544 bytes per distinct vector: the 8-input case
# (~5.3M combos, ~1.0M distinct) retains ~552MB and the 9-input case
# (~9.5M combos, ~1.8M distinct) ~992MB, so the threshold sits below
# both -- they use the Bloom index instead of a ~0.5-1GB set.
# Module-level so tests can monkeypatch it (e.g. force the Bloom
# path on small inputs).
_ADDSUB_44_SET_THRESHOLD = 4000000


def analyze(examples: Sequence[Tuple[Dict[str, Any], Any]],
            input_names: Sequence[str],
            probe_max_size: int = 4,
            _probe_k_min: int = 2) -> InputDependencyReport:
    """Infer input relevance from behavioral examples.

    Pure function of the examples' values. Never inspects input names or
    positions for scoring (names are carried through only as labels).
    ``probe_max_size`` bounds the structural probe's tree-size search
    (the synthesizer passes its own max_size, capped); the default keeps
    the common case cheap. ``_probe_k_min`` is a diagnostics hook: start
    the size sweep at a higher k to measure one level in isolation
    (default 2 = the full sweep; production callers never pass it).
    """
    report = InputDependencyReport(n_examples=len(examples))
    names = list(input_names)
    if not examples or not names:
        report.ranking = list(names)
        return report

    args_list = [dict(a) for a, _ in examples]
    outputs = [o for _, o in examples]
    n = len(examples)

    # This analyzer is NUMERIC search guidance. For symbolic /
    # non-numeric domains (entity names, predicates, structured outputs)
    # the value-hash and correlation signals are meaningless; reordering
    # inputs there can only mislead synthesis. Fail closed: return the
    # original order with no hypotheses.
    if not all(_is_number(o) for o in outputs):
        report.ranking = list(names)
        return report
    if not all(_is_number(a.get(nm)) for a in args_list for nm in names):
        report.ranking = list(names)
        return report

    # Numeric matrix for the statistical signal; non-numeric inputs get a
    # zero statistical score (the exact probe may still catch them, but
    # these templates are numeric).
    numeric_out = all(_is_number(o) for o in outputs)
    out_vals = [float(o) for o in outputs] if numeric_out else None

    col_vals: Dict[str, List[Any]] = {}
    for nm in names:
        col_vals[nm] = [a.get(nm) for a in args_list]

    # Honesty threshold: with a single example, exact-fit probes
    # (both the OpHypothesis relational probe and the structural probe)
    # are meaningless -- any op/structure can be made to fit. Return
    # no hypotheses; the synthesizer falls back to unguided search
    # with its normal ambiguity detection. The ranking is still
    # computed (it's ordering-only, not a correctness claim).
    _too_few = n < 2

    # --- Signal 1: exact relational probe --------------------------------
    exact_score: Dict[str, float] = {nm: 0.0 for nm in names}
    hypotheses: List[OpHypothesis] = []
    if numeric_out and not _too_few:
        for i, ni in enumerate(names):
            xi_all = col_vals[ni]
            if not all(_is_number(v) for v in xi_all):
                continue
            for j, nj in enumerate(names):
                if i == j:
                    continue
                xj_all = col_vals[nj]
                if not all(_is_number(v) for v in xj_all):
                    continue
                for op_name, fn in _PROBE_TEMPLATES:
                    hits = 0
                    for xi, xj, yo in zip(xi_all, xj_all, outputs):
                        try:
                            if fn(xi, xj) == yo:
                                hits += 1
                        except Exception:
                            break
                    frac = hits / n
                    if frac > exact_score[ni]:
                        exact_score[ni] = frac
                    if frac > exact_score[nj]:
                        exact_score[nj] = frac
                    if frac >= 1.0:
                        hypotheses.append(OpHypothesis(
                            op=op_name, inputs=(ni, nj), fit_fraction=1.0))

    # --- Signal 2: absolute correlation -----------------------------------
    corr_score: Dict[str, float] = {}
    for nm in names:
        xs = col_vals[nm]
        if out_vals is not None and all(_is_number(v) for v in xs):
            corr_score[nm] = abs(_pearson([float(v) for v in xs], out_vals))
        else:
            corr_score[nm] = 0.0

    # --- Sound lower bound: singleton-difference proof --------------------
    proven: Set[str] = set()
    for a in range(n):
        for b in range(a + 1, n):
            if outputs[a] == outputs[b]:
                continue
            diff = [nm for nm in names
                    if col_vals[nm][a] != col_vals[nm][b]]
            if len(diff) == 1:
                proven.add(diff[0])

    # --- Combine ----------------------------------------------------------
    # Exact relational evidence dominates (deductive); correlation breaks
    # ties and covers what templates miss. Proven-relevant inputs pin to
    # the top. Tiebreaks use value hashes, never names.
    def sort_key(nm: str):
        return (-(1.0 if nm in proven else 0.0),
                -max(exact_score[nm], corr_score[nm]),
                -exact_score[nm],
                -corr_score[nm],
                _stable_tiebreak(tuple(col_vals[nm])))

    ranking = sorted(names, key=sort_key)
    scores = {nm: max(exact_score[nm], corr_score[nm]) for nm in names}

    # Deduplicate hypotheses (same op+inputs found via both orders is fine
    # to keep once; keep deterministic order by value hash).
    seen = set()
    uniq_hyp = []
    for h in hypotheses:
        key = (h.op, h.inputs)
        if key not in seen:
            seen.add(key)
            uniq_hyp.append(h)
    uniq_hyp.sort(key=lambda h: _stable_tiebreak(
        tuple(col_vals[i] for i in h.inputs)))

    # Order-awareness for non-commutative hypotheses: the exact probe
    # found an ORDERED pair (e.g. subtract(e, f), not subtract(f, e)).
    # A pure relevance ranking loses that order; if the ranking puts the
    # pair backwards, a positional consumer (skeleton a0/a1 slots) would
    # try f-e instead of e-f. For commutative ops order is irrelevant.
    # Adjust minimally: for each non-commutative exact hypothesis, ensure
    # the first element precedes the second, preserving relative order
    # otherwise. Deterministic (hypotheses already in stable order).
    _NON_COMMUTATIVE = {"subtract"}
    rank_pos = {nm: i for i, nm in enumerate(ranking)}
    for h in uniq_hyp:
        if h.op in _NON_COMMUTATIVE and len(h.inputs) == 2:
            i_nm, j_nm = h.inputs[0], h.inputs[1]
            if i_nm in rank_pos and j_nm in rank_pos:
                if rank_pos[i_nm] > rank_pos[j_nm]:
                    # move i_nm just before j_nm
                    ranking.remove(i_nm)
                    ranking.insert(rank_pos[j_nm], i_nm)
                    rank_pos = {nm: i for i, nm in enumerate(ranking)}

    report.scores = scores
    report.ranking = ranking
    report.hypotheses = uniq_hyp
    report.composite_hypotheses, report.probe_stats = _structural_probe(
        col_vals, outputs, names, n, max_size=probe_max_size,
        _k_min=_probe_k_min)
    report.proven_relevant = proven
    return report


# Commutative ops among the probe templates. The mirror wiring of a
# commutative outer op is behaviorally identical, so only one is emitted.
_PROBE_COMMUTATIVE = frozenset({"add", "multiply"})

_PROBE_TEMPLATE_FUNCS: Dict[str, Any] = dict(_PROBE_TEMPLATES)


# ---------------------------------------------------------------------------
# Structural-rival detection (evidence-bounded honesty).
#
# The probe's banks dedup by VALUE VECTOR, keeping one tree per vector.
# When the training evidence underdetermines structure -- e.g. two inputs
# with identical training columns, or a dependence like a == 2*b -- MULTIPLE
# structurally distinct trees share a value vector and the probe silently
# keeps one. Committing to it would be a guess, not an inference.
#
# _find_structural_rivals(h, ...) searches for alternative trees with the
# same training value vector as h that DISAGREE with h on synthetic probes
# (independently sampled input assignments, so inputs that coincide on
# training are varied apart). A disagreeing alternative is a genuine rival:
# the training evidence cannot distinguish the pairings. Rivals are attached
# to the hypothesis; the synthesizer's identifiability gate suppresses any
# training-fitting candidate shadowed by such a rival (fail closed).
#
# Search channels (bounded; stops after 3 rivals):
#   (a) duplicate-column leaf substitution: a leaf input x replaced by
#       another input y with an identical training column;
#   (b) size-1/size-2 subtree replacement: on-demand re-enumeration of
#       template trees with the same value vector as the subtree.
# Name-based (not value-based) canonical keys distinguish alternatives;
# commutative mirrors are NOT rivals (behaviorally identical everywhere),
# and candidates that agree with h on every synthetic probe are not
# rivals either (e.g. associativity variants).
# ---------------------------------------------------------------------------

def _name_canon_key(tree: Union[str, "StructuralHypothesis"]) -> Any:
    """Canonical structure key by INPUT NAME (not value vector).

    Two trees sharing a value vector but with different name keys are
    structurally distinct realizations of one behavior -- exactly the
    underdetermination the rival search looks for.
    """
    if isinstance(tree, str):
        return ("leaf", tree)
    kids = [_name_canon_key(c) for c in tree.children]
    if tree.op in _PROBE_COMMUTATIVE:
        kids = sorted(kids, key=repr)
    return (tree.op, tuple(kids))


def eval_structural_tree(tree: Union[str, "StructuralHypothesis"],
                         assignment: Dict[str, Any]) -> Any:
    """Evaluate a StructuralHypothesis on one input assignment."""
    if isinstance(tree, str):
        return assignment[tree]
    t = _PROBE_TEMPLATE_FUNCS[tree.op]
    vals = [eval_structural_tree(c, assignment) for c in tree.children]
    return t(vals[0], vals[1])


def _synthetic_probe_assignments(
        names: Sequence[str],
        leaf_vecs: Dict[str, Tuple[Any, ...]],
        n: int = 160) -> List[Dict[str, Any]]:
    """Independently-sampled input assignments for rival disagreement.

    Each input is sampled independently from its training values plus
    range-extended integers and midpoints, so inputs that coincide on
    training (duplicate columns) are varied apart. Fixed seed: the probes
    are a deterministic instrument, not tuned per target.
    """
    pools: Dict[str, List[Any]] = {}
    for nm in names:
        vs = [v for v in leaf_vecs[nm]
              if isinstance(v, (int, float)) and not isinstance(v, bool)]
        if not vs:
            return []  # non-numeric domain: no synthetic probes
        pool = set(vs)
        lo, hi = min(vs), max(vs)
        sv = sorted(set(vs))
        for x, y in zip(sv, sv[1:]):
            pool.add((x + y) / 2.0)
        pools[nm] = list(pool)
    rng = random.Random(0x5BD1E995)
    probes: List[Dict[str, Any]] = []
    for _ in range(n):
        asg: Dict[str, Any] = {}
        for nm in names:
            vs = [v for v in leaf_vecs[nm]
                  if isinstance(v, (int, float)) and not isinstance(v, bool)]
            lo, hi = min(vs), max(vs)
            if rng.random() < 0.5:
                asg[nm] = rng.choice(pools[nm])
            else:
                asg[nm] = rng.randint(math.floor(lo) - 3,
                                     math.ceil(hi) + 3)
        probes.append(asg)
    return probes


def _tree_training_vector(tree: Union[str, "StructuralHypothesis"],
                          leaf_vecs: Dict[str, Tuple[Any, ...]],
                          names: Sequence[str]) -> Tuple[Any, ...]:
    """Value vector of a tree on the training rows."""
    n = len(leaf_vecs[names[0]])
    out = []
    for i in range(n):
        asg = {nm: leaf_vecs[nm][i] for nm in names}
        out.append(eval_structural_tree(tree, asg))
    return tuple(out)


def _find_structural_rivals(
        h: "StructuralHypothesis",
        leaf_vecs: Dict[str, Tuple[Any, ...]],
        names: Sequence[str],
        probes: List[Dict[str, Any]],
        max_rivals: int = 3) -> List["StructuralHypothesis"]:
    """Distinct trees with h's training value vector that disagree with h
    on a synthetic probe. Empty list == structurally identified (on the
    evidence); non-empty == evidence-bounded underdetermination."""
    rivals: List[StructuralHypothesis] = []
    if not probes:
        return rivals
    seen = {_name_canon_key(h)}
    h_vec = _tree_training_vector(h, leaf_vecs, names)

    def _disagrees(candidate: "StructuralHypothesis") -> bool:
        # Same training vector (checked by caller); a rival must also
        # differ behaviorally on some probe.
        if _tree_training_vector(candidate, leaf_vecs, names) != h_vec:
            return False
        for pr in probes:
            try:
                hv = eval_structural_tree(h, pr)
                cv = eval_structural_tree(candidate, pr)
            except Exception:
                continue
            if isinstance(hv, bool) or isinstance(cv, bool):
                if hv != cv:
                    return True
            elif isinstance(hv, (int, float)) and \
                    isinstance(cv, (int, float)):
                if abs(hv - cv) > 1e-9:
                    return True
            elif hv != cv:
                return True
        return False

    def _consider(candidate: "StructuralHypothesis") -> bool:
        # True when the rival budget is exhausted (stop searching).
        key = _name_canon_key(candidate)
        if key in seen:
            return False
        seen.add(key)
        if _disagrees(candidate):
            rivals.append(candidate)
        return len(rivals) >= max_rivals

    def _leaf_paths(tree: Union[str, "StructuralHypothesis"],
                    path: Tuple[int, ...] = ()) -> List[Any]:
        if isinstance(tree, str):
            return [(path, tree)]
        out: List[Any] = []
        for i, c in enumerate(tree.children):
            out.extend(_leaf_paths(c, path + (i,)))
        return out

    def _replace_at(tree: "StructuralHypothesis", path: Tuple[int, ...],
                    new_sub: Union[str, "StructuralHypothesis"]
                    ) -> "StructuralHypothesis":
        if not path:
            assert isinstance(new_sub, StructuralHypothesis)
            return new_sub
        kids = list(tree.children)
        kids[path[0]] = _replace_at(kids[path[0]], path[1:], new_sub) \
            if isinstance(kids[path[0]], StructuralHypothesis) \
            else new_sub
        r = StructuralHypothesis(tree.op, tuple(kids))
        r.fit_fraction = tree.fit_fraction
        return r

    # Channel (a): duplicate-column leaf substitution.
    dup: Dict[Tuple[Any, ...], List[str]] = {}
    for nm in names:
        dup.setdefault(leaf_vecs[nm], []).append(nm)
    for path, x in _leaf_paths(h):
        for y in dup.get(leaf_vecs[x], ()):
            if y == x:
                continue
            if _consider(_replace_at(h, path, y)):
                break
        if len(rivals) >= max_rivals:
            break

    # Channel (b): size-1/size-2 subtree alternatives via on-demand
    # re-enumeration of template trees with the same value vector.
    def _alt_size1(svec: Tuple[Any, ...],
                   exclude_key: Any) -> List["StructuralHypothesis"]:
        alts: List[StructuralHypothesis] = []
        seen_a = {exclude_key}
        for t1n, t1 in _PROBE_TEMPLATES:
            for x in names:
                xv = leaf_vecs[x]
                for y in names:
                    try:
                        s = tuple(t1(a, b)
                                  for a, b in zip(xv, leaf_vecs[y]))
                    except Exception:
                        continue
                    if s != svec:
                        continue
                    cand = StructuralHypothesis(t1n, (x, y))
                    key = _name_canon_key(cand)
                    if key in seen_a:
                        continue
                    seen_a.add(key)
                    alts.append(cand)
                    if len(alts) >= 6:
                        return alts
        return alts

    def _alt_size2(svec: Tuple[Any, ...],
                   exclude_key: Any) -> List["StructuralHypothesis"]:
        s1_all: List[Any] = []
        for t1n, t1 in _PROBE_TEMPLATES:
            for x in names:
                xv = leaf_vecs[x]
                for y in names:
                    try:
                        s = tuple(t1(a, b)
                                  for a, b in zip(xv, leaf_vecs[y]))
                    except Exception:
                        continue
                    s1_all.append((s, StructuralHypothesis(t1n, (x, y))))
        alts: List[StructuralHypothesis] = []
        seen_a = {exclude_key}
        for tname, t in _PROBE_TEMPLATES:
            for svec_inner, stree in s1_all:
                for z in names:
                    zv = leaf_vecs[z]
                    wirings: Tuple[bool, ...] = (True,) \
                        if tname in _PROBE_COMMUTATIVE else (True, False)
                    for s_first in wirings:
                        try:
                            if s_first:
                                vec = tuple(t(s, x) for s, x in
                                            zip(svec_inner, zv))
                            else:
                                vec = tuple(t(x, s) for x, s in
                                            zip(zv, svec_inner))
                        except Exception:
                            continue
                        if vec != svec:
                            continue
                        kids = (stree, z) if s_first else (z, stree)
                        cand = StructuralHypothesis(tname, kids)
                        key = _name_canon_key(cand)
                        if key in seen_a:
                            continue
                        seen_a.add(key)
                        alts.append(cand)
                        if len(alts) >= 6:
                            return alts
        return alts

    if len(rivals) < max_rivals:
        sub_occs: List[Any] = []

        def _walk(t: Union[str, "StructuralHypothesis"],
                  path: Tuple[int, ...] = ()) -> None:
            if isinstance(t, str):
                return
            if t.size() in (1, 2):
                sub_occs.append((path, t))
            for i, c in enumerate(t.children):
                _walk(c, path + (i,))

        _walk(h)
        for path, sub in sub_occs:
            if len(rivals) >= max_rivals:
                break
            svec = _tree_training_vector(sub, leaf_vecs, names)
            ex_key = _name_canon_key(sub)
            alts = _alt_size1(svec, ex_key) if sub.size() == 1 \
                else _alt_size2(svec, ex_key)
            for alt in alts:
                if _consider(_replace_at(h, path, alt)):
                    break
    return rivals


def find_dependency_expansions(
        h: "StructuralHypothesis",
        leaf_vecs: Dict[str, Tuple[Any, ...]],
        names: Sequence[str],
        b1_dict: Dict[Tuple[Any, ...], "StructuralHypothesis"],
        b2_dict: Dict[Tuple[Any, ...], "StructuralHypothesis"],
        b3_set: Optional[set],
        b3_tree_for,
        max_expansions: int = 6,
        _ff_cache: Optional[Dict[Any, Any]] = None) -> List[Tuple[str, "StructuralHypothesis"]]:
    """Dependency-expansion search (R2''): for each leaf x used by the
    structural hypothesis h, find banked B_1-B_4 trees over inputs OTHER
    than x whose training value vector equals leaf_vecs[x] -- i.e. x is
    functionally determined by other inputs on the training evidence
    (the silent wrong-commit gap: the probe spells the law with the
    leaf while the world may use the expansion). Returns
    [(x, expansion_tree)]; at most max_expansions pairs. B_3 trees are
    reconstructed on demand via b3_tree_for(vec) (O(1) set membership
    first, so no full B_3 tree enumeration). B_4 (size-4) expansions
    use the target-directed F-free witness search (_witness_ff_size4
    over explicitly enumerated F-free size-1/size-2 banks, F = {x}):
    the full B_4 bank is infeasible to materialize (~16.1M vectors for
    10 inputs, measured), while inversion is complete for F-free
    size-4 trees and cheap. Trees that themselves use x are excluded
    (circular). Pure value matching -- never names."""
    out: List[Tuple[str, "StructuralHypothesis"]] = []
    seen = set()
    for x in h.leaf_names():
        if len(out) >= max_expansions:
            break
        xv = leaf_vecs[x]
        for bdict in (b1_dict, b2_dict):
            if len(out) >= max_expansions:
                break
            tree = bdict.get(xv)
            if tree is None:
                continue
            if x in tree.leaf_names():
                continue
            key = _name_canon_key(tree)
            if key in seen:
                continue
            seen.add(key)
            out.append((x, tree))
        if len(out) >= max_expansions:
            break
        if b3_set is not None and xv in b3_set:
            tree = b3_tree_for(xv)
            if tree is not None and x not in tree.leaf_names():
                key = _name_canon_key(tree)
                if key not in seen:
                    seen.add(key)
                    out.append((x, tree))
        if len(out) >= max_expansions:
            break
        # B_4: F-free size-4 witness (F = {x}). Smaller expansions are
        # reported first (more parsimonious rival first); a leaf may
        # still carry both a small and a size-4 expansion (independent
        # evidence), bounded by max_expansions.
        s1f, s2f, freef = _ff_banks(names, leaf_vecs, {x}, _ff_cache)
        t4 = _witness_ff_size4(xv, s1f, s2f, freef)
        if t4 is not None:
            key = _name_canon_key(t4)
            if key not in seen:
                seen.add(key)
                out.append((x, t4))
    return out


def _replace_all(tree: Union[str, "StructuralHypothesis"],
                 x: str,
                 new_sub: Union[str, "StructuralHypothesis"]
                 ) -> Union[str, "StructuralHypothesis"]:
    """Replace EVERY occurrence of leaf x in tree with new_sub (the full
    dependency expansion, not a single-occurrence variant)."""
    if isinstance(tree, str):
        return new_sub if tree == x else tree
    kids = tuple(_replace_all(c, x, new_sub) for c in tree.children)
    r = StructuralHypothesis(tree.op, kids)
    r.fit_fraction = tree.fit_fraction
    return r


def attach_expansion_rivals(
        h: "StructuralHypothesis",
        leaf_vecs: Dict[str, Tuple[Any, ...]],
        names: Sequence[str],
        probes: List[Dict[str, Any]],
        b1_dict: Dict[Tuple[Any, ...], "StructuralHypothesis"],
        b2_dict: Dict[Tuple[Any, ...], "StructuralHypothesis"],
        b3_set: Optional[set],
        b3_tree_for,
        max_rivals: int = 3) -> List["StructuralHypothesis"]:
    """Attach full dependency expansions as structural rivals of h.

    Two independent channels (R2''):
    - leaf expansions (gaps (a)/(b)): for each leaf x with a banked
      B_1-B_4 expansion, the full expansion (_replace_all).
    - dependent-subtree alternatives (gap (c)): for each non-leaf
      subtree S with an F-free alternative spelling, the full graft
      (_replace_subtree_all).
    A graft is attached as a rival (rival_kind="expansion") iff it is
    training-identical to h and disagrees with h on a synthetic probe.
    Synthetic disagreement proves behavioral DISTINCTION only, never
    which law is true -- provenance (expands_leaf / expands_subtree)
    is preserved on the rival for the synthesizer's R2'' gate. Each
    channel gets its own budget of max_rivals (they are independent
    evidence; a busy leaf channel must not starve the subtree
    channel); subtree rivals sort first (the deeper gap). Returns the
    attached expansion rivals.

    Evidence-sufficiency guard: with very few training examples,
    coincidental value-vector matches are likely (a random B_2 tree
    matches a leaf's 4-example column with non-trivial probability),
    so expansion-rival detection is unreliable and produces false
    positives that suppress genuine hypotheses. Only attach rivals
    when n >= 8; the R2'' validation (silent wrong-commit gap) used
    n=12, and n=4 cases (e.g. grounding compounds) are known to
    false-positive. This is a sound necessary condition for the
    machinery's trustworthiness, not a target-specific exemption.

    Cost note (2026-09-14, demonstrated): the F-free B_4 enumeration
    below is also the probe's dominant cost driver (measured ~240s for
    ~137 hypotheses over 12 inputs), and the probe runs inside the
    synthesis wall-clock budget -- without this guard that cost trips
    the budget before the search's first level runs, returning None
    for a solvable problem (grounding I.4: 12/12 fail pre-guard with
    ZERO rivals attached, 6/6 pass with a cost-only shim, 33/33 pass
    post-guard). The guard therefore doubles as a cost guard for the
    small-n regime; the general cost/budget separation is enforced in
    GeneralSynthesizer.search, whose wall-clock starts after the
    guidance probe."""
    rivals: List["StructuralHypothesis"] = []
    if not probes:
        return rivals
    # Evidence-sufficiency: see docstring.
    _n = len(next(iter(leaf_vecs.values()))) if leaf_vecs else 0
    if _n < 8:
        return rivals
    h_vec = _tree_training_vector(h, leaf_vecs, names)
    seen = {_name_canon_key(h)}
    seen.update(_name_canon_key(r) for r in h.rivals)

    def _disagrees(candidate: "StructuralHypothesis") -> bool:
        if _tree_training_vector(candidate, leaf_vecs, names) != h_vec:
            return False
        for pr in probes:
            try:
                hv = eval_structural_tree(h, pr)
                cv = eval_structural_tree(candidate, pr)
            except Exception:
                continue
            if isinstance(hv, bool) or isinstance(cv, bool):
                if hv != cv:
                    return True
            elif isinstance(hv, (int, float)) and \
                    isinstance(cv, (int, float)):
                if abs(hv - cv) > 1e-9:
                    return True
            elif hv != cv:
                return True
        return False

    def _try_attach(cand: "StructuralHypothesis",
                    bucket: List["StructuralHypothesis"]) -> None:
        if len(bucket) >= max_rivals:
            return
        key = _name_canon_key(cand)
        if key in seen:
            return
        seen.add(key)
        if _disagrees(cand):
            bucket.append(cand)

    _ff_cache: Dict[Any, Any] = {}
    leaf_cands: List["StructuralHypothesis"] = []
    for x, exp_tree in find_dependency_expansions(
            h, leaf_vecs, names, b1_dict, b2_dict, b3_set, b3_tree_for,
            max_expansions=max_rivals * 2, _ff_cache=_ff_cache):
        cand = _replace_all(h, x, exp_tree)
        cand.rival_kind = "expansion"
        cand.expands_leaf = x
        _try_attach(cand, leaf_cands)

    # Dependent-subtree alternatives (R2'' gap (c)): a non-leaf
    # subexpression S of h may itself be functionally determined by
    # other inputs (e.g. (a+b) == (d+e)*f+g on every training row).
    # One O(1)-ish lookup per distinct subtree occurrence (sizes
    # 1..3); the graft must still disagree on a synthetic probe.
    sub_cands: List["StructuralHypothesis"] = []
    for s_key, s_tree, alt_tree in find_subtree_alternatives(
            h, leaf_vecs, names,
            max_alternatives=max_rivals * 2, _ff_cache=_ff_cache):
        cand = _replace_subtree_all(h, s_key, alt_tree)
        cand.rival_kind = "expansion"
        cand.expands_leaf = None
        cand.expands_subtree = _sexpr(s_tree)
        _try_attach(cand, sub_cands)

    rivals.extend(sub_cands)
    rivals.extend(leaf_cands)
    return rivals


# ---------------------------------------------------------------------------
# Forbidden-input (F-free) witness search: the B_4 expansion-rival
# channel and the dependent-subtree channel.
#
# Behavioral evidence principle (same as R2''): "subexpression S's
# training column equals a template tree T over OTHER inputs on every
# training row" is observable functional-dependency evidence. Two gaps
# in the B_1-B_3 leaf channel:
#   (b) T of size 4 (B_4): the full B_4 bank is infeasible (~16.1M
#       vectors for 10 inputs, measured), so instead of materializing
#       it we search target-directedly: for S = leaf x, invert the root
#       template over explicitly enumerated F-free size-1/size-2 banks
#       (F = {x}). All four B_4 root shapes -- (1,2), (2,1), (0,3),
#       (3,0) -- are covered; the (0,3)/(3,0) solved side is witnessed
#       by the same inversion one level down.
#   (c) S a non-leaf subtree (size 1..3): one O(1)-ish lookup per
#       distinct subtree occurrence for T of size 0..3 over inputs
#       disjoint from S's leaves (smallest T first).
# Both reduce to "first F-free template tree of size m with a given
# value vector", found by exact template inversion (_probe_inverse,
# the same sound primitive the probe uses) over explicitly enumerated
# F-free component banks. The F-free banks are enumerated fresh per
# forbidden set -- complete for F-free trees by construction, with no
# bank-dedup representative problem (a deduped bank may keep an
# F-using tree for a vector that also has an F-free realization).
# A candidate T becomes a rival only if the grafted tree disagrees
# with h on a fixed-seed synthetic probe (behavioral distinction
# only -- never which law is true). Suppression goes to AMBIGUITY,
# never promotion.
# ---------------------------------------------------------------------------

def _ff_banks(names: Sequence[str],
              leaf_vecs: Dict[str, Tuple[Any, ...]],
              forbidden: Any,
              _cache: Optional[Dict[Any, Any]] = None):
    """Explicit F-free size-1/size-2 template banks.

    Returns (s1, s2, free_items): s1 maps value vector -> tree for
    every (template, x, y) with x, y not in forbidden; s2 maps value
    vector -> tree for every T2(s1, z) / T2(z, s1) with s1 drawn from
    s1 (by vector -- complete for s2 vectors) and z not in forbidden;
    free_items is [(name, vec)] for names not in forbidden. Complete
    for F-free size-1/size-2 trees by construction. Results are cached
    per forbidden set in _cache (a dict supplied by the caller;
    leaf_vecs must be fixed for the cache's lifetime) because several
    leaves/subtrees of one hypothesis may share a forbidden set.
    Template applications that raise are skipped, mirroring bank
    building (so symbolic domains degrade to empty banks, never an
    error).
    """
    key = frozenset(forbidden)
    if _cache is not None and key in _cache:
        return _cache[key]
    free_items = [(nm, leaf_vecs[nm]) for nm in names
                  if nm not in forbidden]
    s1: Dict[Tuple[Any, ...], StructuralHypothesis] = {}
    for t1n, t1 in _PROBE_TEMPLATES:
        for xn, xv in free_items:
            for yn, yv in free_items:
                try:
                    v = tuple(t1(a, b) for a, b in zip(xv, yv))
                except Exception:
                    continue
                if v not in s1:
                    s1[v] = StructuralHypothesis(t1n, (xn, yn))
    s2: Dict[Tuple[Any, ...], StructuralHypothesis] = {}
    s1_items = list(s1.items())
    for t2n, t2 in _PROBE_TEMPLATES:
        wirings = (True,) if t2n in _PROBE_COMMUTATIVE else (True, False)
        for s1v, s1t in s1_items:
            for zn, zv in free_items:
                for first in wirings:
                    try:
                        if first:
                            v = tuple(t2(a, b)
                                      for a, b in zip(s1v, zv))
                        else:
                            v = tuple(t2(b, a)
                                      for a, b in zip(s1v, zv))
                    except Exception:
                        continue
                    if v not in s2:
                        kids = (s1t, zn) if first else (zn, s1t)
                        s2[v] = StructuralHypothesis(t2n, kids)
    out = (s1, s2, free_items)
    if _cache is not None:
        _cache[key] = out
    return out


def _ff_lookup(bank: Dict[Tuple[Any, ...], Any],
               pattern: Tuple[Any, ...]) -> List[Any]:
    """Trees in a vec->tree bank matching a (possibly wildcard)
    pattern. Exact dict hit fast path; wildcard patterns (rare: only
    multiply zero-divisor inversions produce them) scan."""
    if _WC not in pattern:
        t = bank.get(pattern)
        return [t] if t is not None else []
    return [t for v, t in bank.items() if _vec_matches(pattern, v)]


def _ff_leaf_for(free_items: List[Tuple[str, Tuple[Any, ...]]],
                 pattern: Tuple[Any, ...]) -> Optional[str]:
    """First free leaf name whose vector matches the pattern."""
    for nm, zv in free_items:
        if _vec_matches(pattern, zv):
            return nm
    return None


def _witness_ff_size3(target: Tuple[Any, ...],
                      s1: Dict[Tuple[Any, ...],
                               "StructuralHypothesis"],
                      s2: Dict[Tuple[Any, ...],
                               "StructuralHypothesis"],
                      free_items: List[Tuple[str, Tuple[Any, ...]]
                                       ]) -> Optional["StructuralHypothesis"]:
    """First F-free size-3 template tree with value vector == target.

    Root splits (1,1), (0,2), (2,0): enumerate the smaller side (an s1
    tree / a free leaf), invert the template exactly, verify the
    solved side by F-free bank/leaf membership. Complete for F-free
    size-3 trees: every such tree has one of these root splits with
    F-free sides. Enumerating every s1 vector as the left child covers
    both subtract wirings (ordered pairs). `target` may carry
    wildcards (multiply zero-divisor inversion); lookups are
    wildcard-tolerant. Returns None when no F-free size-3 tree
    matches.
    """
    for t3n, _t3 in _PROBE_TEMPLATES:
        # (1,1): size-1 side left, solve right (size 1).
        for s1v, s1t in s1.items():
            yv = _probe_inverse(t3n, target, s1v, known_is_left=True)
            if yv is None:
                continue
            for m in _ff_lookup(s1, yv):
                return StructuralHypothesis(t3n, (s1t, m))
    for t3n, _t3 in _PROBE_TEMPLATES:
        # (0,2): leaf left, solve right (size 2).
        for zn, zv in free_items:
            yv = _probe_inverse(t3n, target, zv, known_is_left=True)
            if yv is None:
                continue
            for m in _ff_lookup(s2, yv):
                return StructuralHypothesis(t3n, (zn, m))
        if t3n in _PROBE_COMMUTATIVE:
            continue  # mirror wiring behaviorally identical
        # (2,0): leaf right, solve left (size 2).
        for zn, zv in free_items:
            yv = _probe_inverse(t3n, target, zv, known_is_left=False)
            if yv is None:
                continue
            for m in _ff_lookup(s2, yv):
                return StructuralHypothesis(t3n, (m, zn))
    return None


def _witness_ff_size4(target: Tuple[Any, ...],
                      s1: Dict[Tuple[Any, ...],
                               "StructuralHypothesis"],
                      s2: Dict[Tuple[Any, ...],
                               "StructuralHypothesis"],
                      free_items: List[Tuple[str, Tuple[Any, ...]]
                                       ]) -> Optional["StructuralHypothesis"]:
    """First F-free size-4 template tree with value vector == target.

    Root splits (1,2), (2,1): enumerate the size-1 side (s1 -- the
    smaller side, as in the probe's banked split search), invert the
    template exactly, verify the size-2 side by F-free bank
    membership. Root splits (0,3), (3,0): enumerate a free leaf,
    invert, witness the size-3 side with _witness_ff_size3. Complete
    for F-free size-4 trees. Returns None when no F-free size-4 tree
    matches.
    """
    for t4n, _t4 in _PROBE_TEMPLATES:
        for s1v, s1t in s1.items():
            # (1,2): size-1 left, solve right (size 2).
            yv = _probe_inverse(t4n, target, s1v, known_is_left=True)
            if yv is not None:
                for m in _ff_lookup(s2, yv):
                    return StructuralHypothesis(t4n, (s1t, m))
            # (2,1): size-1 right, solve left (size 2).
            yv = _probe_inverse(t4n, target, s1v, known_is_left=False)
            if yv is not None:
                for m in _ff_lookup(s1, yv):
                    return StructuralHypothesis(t4n, (m, s1t))
    for t4n, _t4 in _PROBE_TEMPLATES:
        for zn, zv in free_items:
            # (0,3): leaf left, solve right (size 3).
            yv = _probe_inverse(t4n, target, zv, known_is_left=True)
            if yv is not None:
                w = _witness_ff_size3(yv, s1, s2, free_items)
                if w is not None:
                    return StructuralHypothesis(t4n, (zn, w))
            if t4n in _PROBE_COMMUTATIVE:
                continue  # mirror wiring behaviorally identical
            # (3,0): leaf right, solve left (size 3).
            yv = _probe_inverse(t4n, target, zv, known_is_left=False)
            if yv is not None:
                w = _witness_ff_size3(yv, s1, s2, free_items)
                if w is not None:
                    return StructuralHypothesis(t4n, (w, zn))
    return None


def _sexpr(tree: Union[str, "StructuralHypothesis"]) -> str:
    """Compact s-expression for a tree (ambiguity records)."""
    if isinstance(tree, str):
        return tree
    return "(%s %s)" % (tree.op, " ".join(_sexpr(c)
                                         for c in tree.children))


def _replace_subtree_all(
        tree: Union[str, "StructuralHypothesis"],
        s_key: Any,
        new_sub: Union[str, "StructuralHypothesis"]
        ) -> Union[str, "StructuralHypothesis"]:
    """Replace EVERY subtree occurrence with name-key == s_key by
    new_sub (the full alternative spelling, not a single-occurrence
    variant)."""
    if _name_canon_key(tree) == s_key:
        return new_sub
    if isinstance(tree, str):
        return tree
    kids = tuple(_replace_subtree_all(c, s_key, new_sub)
                 for c in tree.children)
    r = StructuralHypothesis(tree.op, kids)
    r.fit_fraction = tree.fit_fraction
    return r


def find_subtree_alternatives(
        h: "StructuralHypothesis",
        leaf_vecs: Dict[str, Tuple[Any, ...]],
        names: Sequence[str],
        max_alternatives: int = 6,
        _ff_cache: Optional[Dict[Any, Any]] = None
        ) -> List[Tuple[Any, "StructuralHypothesis", Any]]:
    """Dependent-subtree alternatives (R2'' gap (c)).

    For each distinct non-leaf subtree occurrence S of h with
    1 <= S.size() <= 3, find template trees T over inputs DISJOINT
    from S's leaves with S's training value vector, smallest first
    (T sizes 0..3: a free leaf with S's column, the F-free size-1 /
    size-2 banks, the F-free size-3 inversion). Returns
    [(s_key, s_tree, alt_tree)] with at most max_alternatives
    entries. Disjoint leaves guarantee T is a genuinely different
    spelling (T cannot contain S; name keys differ). The graft must
    still disagree with h on a synthetic probe before it becomes a
    rival (checked in attach_expansion_rivals). Pure behavioral
    evidence -- never names.
    """
    out: List[Tuple[Any, "StructuralHypothesis", Any]] = []
    seen_alt = set()
    sub_occs: List[Tuple[int, Any, "StructuralHypothesis"]] = []
    seen_sub = set()

    def _walk(t: Union[str, "StructuralHypothesis"]) -> None:
        if isinstance(t, str):
            return
        key = _name_canon_key(t)
        if key not in seen_sub:
            seen_sub.add(key)
            if 1 <= t.size() <= 3:
                sub_occs.append((t.size(), key, t))
        for c in t.children:
            _walk(c)

    _walk(h)
    sub_occs.sort(key=lambda e: e[0])
    for _ssize, s_key, s_tree in sub_occs:
        if len(out) >= max_alternatives:
            break
        forbidden = frozenset(s_tree.leaf_names())
        s1f, s2f, free_items = _ff_banks(names, leaf_vecs, forbidden,
                                         _ff_cache)
        svec = _tree_training_vector(s_tree, leaf_vecs, names)
        alt: Any = None
        # T size 0: a free leaf with S's column.
        for nm, zv in free_items:
            if zv == svec:
                alt = nm
                break
        # T sizes 1-2: F-free bank lookup.
        if alt is None:
            alt = s1f.get(svec)
        if alt is None:
            alt = s2f.get(svec)
        # T size 3: F-free inversion.
        if alt is None:
            alt = _witness_ff_size3(svec, s1f, s2f, free_items)
        if alt is None:
            continue
        akey = _name_canon_key(alt)
        if akey in seen_alt:
            continue
        seen_alt.add(akey)
        out.append((s_key, s_tree, alt))
    return out


# Wildcard sentinel for probe inversion: a position where the equation
# holds for ANY value (multiply with a zero known-side and zero target).
# A dedicated object (not None) so it can never collide with a value.
class _Wildcard:
    _inst = None
    def __new__(cls):
        if cls._inst is None:
            cls._inst = super().__new__(cls)
        return cls._inst
    def __repr__(self):
        return "<WC>"

_WC: Any = _Wildcard()


def _probe_inverse(op: str, target: Tuple[Any, ...], known: Tuple[Any, ...],
                   known_is_left: bool) -> Optional[Tuple[Any, ...]]:
    """Solve op(a, b) == target for the unknown side, elementwise.

    known_is_left=True: known is ``a``, solve for ``b``.
    known_is_left=False: known is ``b``, solve for ``a``.
    Returns None when any element has no solution (e.g. indivisible for
    multiply, or a zero divisor with nonzero target). A zero divisor
    with zero target yields a WILDCARD (``_WC``) at that position: the
    equation holds for any value there. Wildcards in the target
    propagate to the solution. Pure value computation over the template
    vocabulary -- the same generic operators the probe already uses.
    """
    out = []
    for t, k in zip(target, known):
        try:
            if t is _WC:
                # Target unconstrained here: solution unconstrained.
                out.append(_WC)
            elif op == "add":
                # a + b == t
                out.append(t - k)
            elif op == "subtract":
                # a - b == t
                out.append(t + k if not known_is_left else k - t)
            elif op == "multiply":
                # a * b == t
                if k == 0:
                    if t == 0:
                        out.append(_WC)  # 0 == a*0 for any a
                    else:
                        return None
                elif t % k != 0:
                    return None
                else:
                    out.append(t // k)
            else:
                return None
        except Exception:
            return None
    return tuple(out)


def _vec_matches(pattern: Tuple[Any, ...],
                 vec: Tuple[Any, ...]) -> bool:
    """Wildcard agreement: every non-wildcard position agrees."""
    return all(p is _WC or p == v for p, v in zip(pattern, vec))


def _bank_lookup(bank: Dict[Tuple[Any, ...], Any],
                 pattern: Tuple[Any, ...]) -> List[Any]:
    """All bank trees whose vector matches the (possibly wildcard)
    pattern. Exact dict hit fast path when the pattern is concrete."""
    if _WC not in pattern:
        hit = bank.get(pattern)
        return [hit] if hit is not None else []
    return [tree for vec, tree in bank.items()
            if _vec_matches(pattern, vec)]


class _BloomFilter:
    """Compact set-membership index with NO false negatives.

    Used as the mate-side index for the size-9 (4,4) add/subtract join:
    the complete B_4 value set is too large to materialize as a Python
    set (measured ~1.6GB at 10 inputs), while a Bloom filter over it
    costs tens of MB. False positives are resolved by the complete
    _rec_witness confirmation, so the join stays exactly complete: no
    false negatives (Bloom filters have none by construction) and no
    invented hits (every hit is confirmed by a real tree). Pure Python;
    double hashing from two independent tuple hashes.
    """

    def __init__(self, n_items: int, fpr: float = 3e-6):
        # bits = -n ln(p) / (ln2)^2 ; k = (bits/n) ln2 (optimal).
        bits = int(-n_items * math.log(fpr) / (math.log(2) ** 2)) + 64
        self.nbits = bits
        self.buf = bytearray((bits + 7) // 8)
        self.k = max(1, int(round(bits / n_items * math.log(2))))

    def _positions(self, vec: Tuple[Any, ...]):
        h1 = hash(vec)
        h2 = hash(vec[::-1]) | 1  # odd, nonzero: independent-ish
        m = self.nbits
        for i in range(self.k):
            yield (h1 + i * h2) % m

    def add(self, vec: Tuple[Any, ...]) -> None:
        buf = self.buf
        for idx in self._positions(vec):
            buf[idx >> 3] |= 1 << (idx & 7)

    def __contains__(self, vec: Tuple[Any, ...]) -> bool:
        buf = self.buf
        for idx in self._positions(vec):
            if not (buf[idx >> 3] >> (idx & 7)) & 1:
                return False
        return True


def _structural_probe(col_vals: Dict[str, List[Any]],
                      outputs: List[Any],
                      names: List[str],
                      n: int,
                      max_size: int = 4,
                      _k_min: int = 2) -> List[StructuralHypothesis]:
    """Hierarchical exact relational probe over the generic templates.

    ``_k_min`` is a diagnostics hook (see ``analyze``): start the
    size sweep at a higher k to measure one level in isolation.

    Discovers tree-structured hypotheses -- output == T(...intermediates
    ...) -- from the examples' VALUES only. Level 1 builds every
    (template, ordered input pair) intermediate value vector. Higher
    levels use BANKED SPLIT SEARCH: for a size-k target, every root
    split (i, j) is tried by enumerating the smaller side exhaustively
    from its value bank (B_0 leaves, B_1 size-1 templates, B_2 size-2
    template trees -- all complete by construction), inverting the
    template to solve for the larger side's value vector, and verifying
    it by bank membership or complete recursion. Splits are ordered
    cheapest-first; the search is deterministic.

    The probe is COMPLETE over the template vocabulary through size 7
    (every size-k <= 7 tree has a root split with a side of size <= 3,
    covered by B_0/B_1/B_2 banks or the B_3 value set with on-demand
    tree reconstruction). Size 8 splits (3,4)/(4,3) are covered for
    multiply roots via the divisibility-filtered flat size-4 witness,
    and for add/subtract roots via the exact join reordering
    (_search_34_addsub: complete size-4 inner decompositions evaluated
    forward, the add/subtract root inverted once per pair, the size-3
    side verified by B_3 membership -- hit-set-equal to the naive
    formulation by construction).
    Size-9 (4,4) splits are covered for multiply roots via the lazily
    generated divisibility-filtered B_4 candidate set (see
    _get_b4_filtered_set): complete for all-integer targets (the filter
    is a sound necessary condition), budget-skipped for non-integer
    targets (no sound filter exists), and skipped for add/subtract
    roots (no sound prefilter). Size-9+ splits with a side larger than
    4 remain the documented incompleteness boundary. The B_3 set is
    built under a hard entry budget; if the budget is exceeded the
    (3,3) split is skipped and probe_stats reports b3_capped=True.

    Multiple hits are normal and harmless -- algebraically equivalent
    structures agree on ALL inputs, not just the training set, so any
    of them is a correct answer. They become search ORDERING (and staged
    construction), and the synthesizer still verifies through its normal
    path.

    A negative result at some size is EVIDENCE (not proof) against a
    template-vocabulary structure of that size: the probe does not cover
    non-template operators. The synthesizer records this boundary
    explicitly when it defers blind levels.

    Returns (hypotheses, stats).
    """
    # Honesty threshold: with a single example, exact-fit is
    # meaningless (any structure can be made to fit) and the probe's
    # "discoveries" are spurious. Return no hypotheses; the synthesizer
    # falls back to unguided search with its normal ambiguity detection.
    # This preserves the fail-closed behavior on sparse evidence.
    if n < 2:
        return [], {"max_size": max_size, "n_too_small": True,
                    "n_hypotheses": 0}
    target = tuple(outputs)
    leaf_vecs = {nm: tuple(col_vals[nm]) for nm in names}
    leaf_items = list(leaf_vecs.items())

    # Level 1: every (template, ordered pair) intermediate, deduped by
    # value. Canonicalize commutative pairs by value-hash (never names).
    s1: Dict[Tuple[Any, ...], Tuple[str, str, str]] = {}
    for t1n, t1 in _PROBE_TEMPLATES:
        for x in names:
            xv = leaf_vecs[x]
            for y in names:
                yv = leaf_vecs[y]
                try:
                    s = tuple(t1(a, b) for a, b in zip(xv, yv))
                except Exception:
                    continue
                ix, iy = x, y
                if t1n in _PROBE_COMMUTATIVE and \
                        _stable_tiebreak(yv) < _stable_tiebreak(xv):
                    ix, iy = iy, ix
                if s not in s1:
                    s1[s] = (t1n, ix, iy)
    s1_items = list(s1.items())

    def _tree_for_s1(vec: Tuple[Any, ...]) -> StructuralHypothesis:
        t1n, x, y = s1[vec]
        return StructuralHypothesis(t1n, (x, y))

    # Size-0/1 banks as value->tree dicts (B_1 trees via _tree_for_s1).
    b0: Dict[Tuple[Any, ...], Any] = {zv: zn for zn, zv in leaf_items}
    b1: Dict[Tuple[Any, ...], StructuralHypothesis] = {}
    for _sv, _ in s1_items:
        if _sv not in b1:
            b1[_sv] = _tree_for_s1(_sv)
    # Size-2 intermediate bank: every template tree with exactly 2 op
    # applications, deduped by value vector. B_2 = { T(X, Y) } over
    # (size(X), size(Y)) in {(1,0), (0,1)}. Complete by construction:
    # every size-2 template tree has a size-1 child and a leaf child.
    # Built lazily -- only needed for k >= 4 split search.
    _banks: List[Dict[Tuple[Any, ...], Any]] = [b0, b1]
    _b2: Optional[Dict[Tuple[Any, ...], StructuralHypothesis]] = None

    def _get_b2() -> Dict[Tuple[Any, ...], StructuralHypothesis]:
        nonlocal _b2
        if _b2 is not None:
            return _b2
        b2: Dict[Tuple[Any, ...], StructuralHypothesis] = {}
        for tname, t in _PROBE_TEMPLATES:
            for svec, _ in s1_items:
                stree = _tree_for_s1(svec)
                for z, zv in leaf_items:
                    # Commutative: one wiring suffices (same values, and
                    # commutative children are canonicalized later).
                    wirings: Tuple[bool, ...] = (True,) \
                        if tname in _PROBE_COMMUTATIVE else (True, False)
                    for s_first in wirings:
                        try:
                            if s_first:
                                vec = tuple(t(s, x)
                                            for s, x in zip(svec, zv))
                            else:
                                vec = tuple(t(x, s)
                                            for x, s in zip(zv, svec))
                        except Exception:
                            continue
                        if vec not in b2:
                            kids = (stree, z) if s_first else (z, stree)
                            b2[vec] = StructuralHypothesis(tname, kids)
        _b2 = b2
        _banks.append(b2)
        return b2

    # Size-3 value bank (SET of vectors, no trees): T(S2, leaf),
    # T(leaf, S2), T(S1a, S1b). Built lazily with a hard entry budget --
    # beyond the budget the (3,3) split is skipped with honest stats.
    # Trees are reconstructed on demand via _rec_witness (size 3).
    _B3_BUDGET = 200000
    _b3_set: Optional[set] = None
    _b3_capped: bool = False

    def _get_b3_set() -> Optional[set]:
        nonlocal _b3_set, _b3_capped
        if _b3_set is not None or _b3_capped:
            return _b3_set
        b2 = _get_b2()
        b3: set = set()
        capped = False
        # T(S2, leaf) and T(leaf, S2).
        for tname, t in _PROBE_TEMPLATES:
            if capped:
                break
            for svec in b2:
                if capped:
                    break
                for _, zv in leaf_items:
                    wirings: Tuple[bool, ...] = (True,) \
                        if tname in _PROBE_COMMUTATIVE else (True, False)
                    for s_first in wirings:
                        try:
                            if s_first:
                                vec = tuple(t(s, x)
                                            for s, x in zip(svec, zv))
                            else:
                                vec = tuple(t(x, s)
                                            for x, s in zip(zv, svec))
                        except Exception:
                            continue
                        b3.add(vec)
                        if len(b3) >= _B3_BUDGET:
                            capped = True
                            break
                    if capped:
                        break
        # T(S1a, S1b).
        if not capped:
            b1_vecs = list(b1.keys())
            for tname, t in _PROBE_TEMPLATES:
                if capped:
                    break
                for avec in b1_vecs:
                    if capped:
                        break
                    for bvec in b1_vecs:
                        try:
                            vec = tuple(t(a, b)
                                        for a, b in zip(avec, bvec))
                        except Exception:
                            continue
                        b3.add(vec)
                        if len(b3) >= _B3_BUDGET:
                            capped = True
                            break
        if capped:
            _b3_capped = True
            _b3_set = None
            return None
        _b3_set = b3
        return b3

    def _match_b3_set(b3: set,
                      pattern: Tuple[Any, ...]) -> List[Tuple[Any, ...]]:
        """Vectors in the B_3 set matching a (possibly wildcard) pattern."""
        if _WC not in pattern:
            return [pattern] if pattern in b3 else []
        return [v for v in b3 if _vec_matches(pattern, v)]

    def _divides_vec(tname: str, target: Tuple[Any, ...],
                     xvec: Tuple[Any, ...]) -> bool:
        """Sound pre-filter for a multiply root: the known side's values
        must divide the target's elementwise. For integer vectors this
        eliminates the vast majority of B_3 candidates before the
        expensive size-4 witness. Non-integer/boolean domains: no
        filtering (returns True) -- soundness over speed."""
        if tname != "multiply":
            return True
        for t, x in zip(target, xvec):
            if isinstance(x, bool) or isinstance(t, bool):
                return True
            if not isinstance(x, int) or not isinstance(t, int):
                return True
            if x == 0:
                if t != 0:
                    return False
            elif t % x != 0:
                return False
        return True

    def _witness_size4_flat(yv: Tuple[Any, ...]
                            ) -> Optional[StructuralHypothesis]:
        """Flat (non-recursive) size-4 witness via bank membership.

        Tries (1,2)/(2,1) via the B_1/B_2 banks and (0,3)/(3,0) via
        B_0 leaf enumeration + B_3 set membership. No nested
        enumeration: each check is a single bank scan plus O(1)
        membership tests. Size-3 sides are reconstructed on demand via
        the (cheap, fully banked) size-3 witness.
        """
        b3s = _get_b3_set()
        if b3s is None:
            return None
        b0 = _banks[0]
        b1 = _banks[1]
        # (1,2) and (2,1): both sides in B_1/B_2 banks.
        for i, j in ((1, 2), (2, 1)):
            got = _try_split_sides(yv, i, j, True, None, {})
            if got:
                return got[0]
        # (0,3): enumerate leaves, solve for the size-3 side, verify
        # by B_3 set membership.
        for tname, _t in _PROBE_TEMPLATES:
            for zvec, zname in b0.items():
                yv2 = _probe_inverse(tname, yv, zvec,
                                     known_is_left=False)
                if yv2 is not None:
                    for m in _match_b3_set(b3s, yv2):
                        xt = _rec_witness(m, 3)
                        if xt is not None:
                            return StructuralHypothesis(
                                tname, (xt, zname))
                if tname not in _PROBE_COMMUTATIVE:
                    yv2 = _probe_inverse(tname, yv, zvec,
                                         known_is_left=True)
                    if yv2 is not None:
                        for m in _match_b3_set(b3s, yv2):
                            xt = _rec_witness(m, 3)
                            if xt is not None:
                                return StructuralHypothesis(
                                    tname, (zname, xt))
        # (3,0): enumerate the B_3 set for the size-3 side
        # (divisibility pre-filtered for multiply roots), solve for
        # the leaf side, verify by B_0 membership.
        for tname, _t in _PROBE_TEMPLATES:
            cands = b3s
            if tname == "multiply":
                cands = [x for x in b3s
                         if _divides_vec(tname, yv, x)]
            for xvec in cands:
                yv2 = _probe_inverse(tname, yv, xvec,
                                     known_is_left=True)
                if yv2 is not None:
                    hits = _bank_lookup(b0, yv2)
                    if hits:
                        xt = _rec_witness(xvec, 3)
                        if xt is not None:
                            return StructuralHypothesis(
                                tname, (xt, hits[0]))
                if tname not in _PROBE_COMMUTATIVE:
                    yv2 = _probe_inverse(tname, yv, xvec,
                                         known_is_left=False)
                    if yv2 is not None:
                        hits = _bank_lookup(b0, yv2)
                        if hits:
                            xt = _rec_witness(xvec, 3)
                            if xt is not None:
                                return StructuralHypothesis(
                                    tname, (hits[0], xt))
        return None

    def _search_34(v: Tuple[Any, ...],
                   seen: set,
                   leaf_vecs: Dict[str, Tuple[Any, ...]],
                   three_on_left: bool = True
                   ) -> List[StructuralHypothesis]:
        """Size-8 (3,4)/(4,3) splits: enumerate the size-3 side from the
        B_3 value set (divisibility pre-filtered for multiply roots),
        invert to solve for the size-4 side, verify via the flat
        bank-membership witness. Returns hits (bounded)."""
        found: List[StructuralHypothesis] = []
        b3 = _get_b3_set()
        if b3 is None:
            return found
        for tname, _t in _PROBE_TEMPLATES:
            cands = b3
            if tname == "multiply":
                pre = len(b3)
                cands = [x for x in b3
                         if _divides_vec(tname, v, x)]
                probe_stats["b3_div_pre"] = pre
                probe_stats["b3_div_post"] = len(cands)
            else:
                # Add/subtract roots have no divisibility pre-filter;
                # the B_3 enumeration x flat size-4 witness is
                # prohibitively expensive. Documented boundary: size-8
                # (3,4)/(4,3) is supported for multiply roots; add/sub
                # roots remain resource-constrained.
                probe_stats["skipped_34_op"] = tname
                continue
            for xvec in cands:
                if len(found) >= _BANK_MAX_HITS:
                    break
                yv = _probe_inverse(tname, v, xvec,
                                    known_is_left=three_on_left)
                if yv is None:
                    continue
                yw = _witness_size4_flat(yv)
                if yw is None:
                    continue
                xt = _rec_witness(xvec, 3)
                if xt is None:
                    continue
                kids = (xt, yw) if three_on_left else (yw, xt)
                h = StructuralHypothesis(tname, kids)
                key = _canon_bank_key(h, leaf_vecs)
                if key in seen:
                    continue
                seen.add(key)
                found.append(h)
                if len(found) >= _BANK_MAX_HITS:
                    break
        return found

    def _search_34_addsub(v: Tuple[Any, ...],
                          seen: set,
                          leaf_vecs: Dict[str, Tuple[Any, ...]]
                          ) -> List[StructuralHypothesis]:
        """Size-8 (3,4)/(4,3) splits for ADD/SUBTRACT roots: exact join
        reordering.

        No sound necessary-condition prefilter exists for add/subtract
        roots (divisibility is specific to multiply; a magnitude-based
        filter would be unsound), so the _search_34 strategy (enumerate
        the size-3 side, verify the size-4 side by witness) cannot be
        reused -- the witness side is the expensive one. Instead the
        enumeration order is inverted:

        * enumerate COMPLETE size-4 inner decompositions: the inner
          pair (p, q) over B_3 x B_0 and B_1 x B_2 -- every size-4
          template tree has a root split with children of sizes
          (3,0), (0,3), (1,2) or (2,1), so these pair sets (plus
          wirings) cover every size-4 tree exactly;
        * evaluate the inner root forward (all three templates);
        * invert the add/subtract root ONCE per inner pair;
        * O(1) B_3-set lookup for the outer size-3 side.

        All three root wirings are covered in the one call: add
        (v = s4 + S3; the mirror v = S3 + s4 is skipped as provably
        key-identical -- _canon_bank_key sorts commutative children),
        sub-x-left (v = s4 - S3) and sub-x-right (v = S3 - s4).
        Commutative inner mirrors are likewise skipped only as
        provably key-identical; noncommutative (subtract) inner
        mirrors are enumerated via both wirings on each pair set.

        Hit-set equality with the naive formulation (enumerate every
        size-4 tree from its complete inner decomposition, invert the
        outer root, verify the outer side in B_3) holds by construction:
        the join computes the same inner value vectors in a different
        order and applies the same outer inversion and membership test.
        Trees are reconstructed on demand via the complete
        _rec_witness(v, 3); inner-pair trees come from the bank dicts
        (B_0/B_1/B_2) or _rec_witness(v, 3) for B_3 vectors. Returns
        hits bounded by _BANK_MAX_HITS, canonically deduped against
        `seen` (shared with the multiply path).

        Pure Python: add/subtract inversion is exact elementwise
        arithmetic (a unique solution always exists), so no precision
        or overflow concerns arise. Telemetry: addsub_34_pairs (inner
        pairs examined), addsub_34_hits, addsub_34_time_s,
        addsub_34_searched.
        """
        found: List[StructuralHypothesis] = []
        b3 = _get_b3_set()
        if b3 is None:
            return found
        # _search_34 records skipped_34_op for the add/subtract roots it
        # defers; they are covered here instead, so the stale skip record
        # is superseded (the multiply path inside _search_34 is
        # untouched).
        probe_stats.pop("skipped_34_op", None)
        probe_stats["addsub_34_searched"] = True
        _t0 = time.time()
        _n_pairs = 0
        b0_items = list(_banks[0].items())      # (vec, name)
        b1_items = list(_banks[1].items())      # (vec, tree)
        b2_items = list(_get_b2().items())      # (vec, tree)
        b3_vecs = list(b3)                       # vectors; trees on demand

        def _inner_tree(kind: str, vec: Tuple[Any, ...], idx: int):
            if kind == "b0":
                # Find leaf name by vector (index alignment between
                # leaf_items and _banks[0] is not guaranteed).
                for nm, zv in leaf_items:
                    if zv == vec:
                        return nm
                return None
            if kind == "b1":
                return b1_items[idx][1]
            if kind == "b2":
                return b2_items[idx][1]
            return _rec_witness(vec, 3)

        # (A_kind, A_vecs, B_kind, B_vecs): inner pair sets. The naive
        # uses all four [(b3,b0),(b0,b3),(b1,b2),(b2,b1)]; we match it
        # exactly for hit-set equality (duplicates are deduped by the
        # canonical key).
        _b0_vecs = [zv for _, zv in leaf_items]
        _b1_vecs = [vv for vv, _ in b1_items]
        _b2_vecs = [vv for vv, _ in b2_items]
        _pair_sets = (("b3", b3_vecs, "b0", _b0_vecs),
                      ("b0", _b0_vecs, "b3", b3_vecs),
                      ("b1", _b1_vecs, "b2", _b2_vecs),
                      ("b2", _b2_vecs, "b1", _b1_vecs))

        def _note_hit(s4t, s3t, outer: str) -> None:
            if outer == "add":
                h = StructuralHypothesis("add", (s4t, s3t))
            elif outer == "sub-left":
                h = StructuralHypothesis("subtract", (s4t, s3t))
            else:  # sub-right
                h = StructuralHypothesis("subtract", (s3t, s4t))
            key = _canon_bank_key(h, leaf_vecs)
            if key in seen:
                return
            seen.add(key)
            found.append(h)

        for akind, avecs, bkind, bvecs in _pair_sets:
            if len(found) >= _BANK_MAX_HITS:
                break
            na, nb = len(avecs), len(bvecs)
            for tname, t in _PROBE_TEMPLATES:
                if len(found) >= _BANK_MAX_HITS:
                    break
                wirings: Tuple[bool, ...] = (True,) \
                    if tname in _PROBE_COMMUTATIVE else (True, False)
                for ai in range(na):
                    if len(found) >= _BANK_MAX_HITS:
                        break
                    avec = avecs[ai]
                    for bi in range(nb):
                        if len(found) >= _BANK_MAX_HITS:
                            break
                        bvec = bvecs[bi]
                        _n_pairs += 1
                        for s_first in wirings:
                            try:
                                if s_first:
                                    s4 = tuple(t(aa, bb) for aa, bb in
                                               zip(avec, bvec))
                                else:
                                    s4 = tuple(t(bb, aa) for aa, bb in
                                               zip(avec, bvec))
                            except Exception:
                                continue
                            # Invert the outer root once per inner pair;
                            # the outer side is verified by O(1) B_3
                            # membership. Add/subtract inversion is
                            # exact (unique solution, no wildcards: s4
                            # is forward-computed, v is concrete).
                            try:
                                o_add = tuple(vv - s for vv, s in
                                              zip(v, s4))
                                o_subl = tuple(s - vv for s, vv in
                                               zip(s4, v))
                                o_subr = tuple(vv + s for vv, s in
                                               zip(v, s4))
                            except Exception:
                                continue
                            for outer, ovec in (
                                    ("add", o_add),
                                    ("sub-left", o_subl),
                                    ("sub-right", o_subr)):
                                if ovec not in b3:
                                    continue
                                s3t = _rec_witness(ovec, 3)
                                if s3t is None:
                                    continue
                                pt = _inner_tree(akind, avec, ai)
                                qt = _inner_tree(bkind, bvec, bi)
                                if pt is None or qt is None:
                                    continue
                                kids = (pt, qt) if s_first else (qt, pt)
                                s4t = StructuralHypothesis(tname, kids)
                                _note_hit(s4t, s3t, outer)
                                if len(found) >= _BANK_MAX_HITS:
                                    break
                            if len(found) >= _BANK_MAX_HITS:
                                break
        probe_stats["addsub_34_pairs"] = _n_pairs
        probe_stats["addsub_34_hits"] = len(found)
        probe_stats["addsub_34_time_s"] = round(time.time() - _t0, 3)
        return found

    def _search_33(v: Tuple[Any, ...],
                   seen: set,
                   leaf_vecs: Dict[str, Tuple[Any, ...]]
                   ) -> List[StructuralHypothesis]:
        """The (3,3) split: enumerate x from the B_3 value set, invert to
        solve for y, verify y in the B_3 set, reconstruct both trees via
        complete size-3 witness search. Returns hits (bounded)."""
        return _search_3g(v, seen, leaf_vecs, g=3)

    def _search_3g(v: Tuple[Any, ...],
                   seen: set,
                   leaf_vecs: Dict[str, Tuple[Any, ...]],
                   g: int,
                   three_on_left: bool = True) -> List[StructuralHypothesis]:
        """The (3,3) split (g=3): enumerate the size-3 side from the B_3
        value set, invert to solve for the other size-3 side, verify by
        B_3 membership, reconstruct trees. Returns hits (bounded).
        The g=4 case (size-8 (3,4) splits) is handled by the dedicated
        _search_34 (B_3 enumeration for the size-3 side, flat banked
        witness for the size-4 side): a complete recursive size-4
        witness over the B_3 enumeration would be prohibitively
        expensive. Documented resource boundary."""
        found: List[StructuralHypothesis] = []
        if g != 3:
            return found
        b3 = _get_b3_set()
        if b3 is None:
            return found
        for three_vec in b3:
            if len(found) >= _BANK_MAX_HITS:
                break
            for tname, _t in _PROBE_TEMPLATES:
                if len(found) >= _BANK_MAX_HITS:
                    break
                # For g=3, three_on_left doesn't matter (symmetric).
                # x (size 3) is left, solve for right (size 3).
                g_pat = _probe_inverse(tname, v, three_vec,
                                       known_is_left=True)
                if g_pat is None:
                    continue
                g_candidates = _match_b3_set(b3, g_pat)
                for gvec in g_candidates:
                    three_t = _rec_witness(three_vec, 3)
                    g_t = _rec_witness(gvec, 3)
                    if three_t is None or g_t is None:
                        continue
                    h = StructuralHypothesis(tname, (three_t, g_t))
                    key = _canon_bank_key(h, leaf_vecs)
                    if key in seen:
                        continue
                    seen.add(key)
                    found.append(h)
                    if len(found) >= _BANK_MAX_HITS:
                        break
        return found

    def _search_45(v: Tuple[Any, ...],
                   seen: set,
                   leaf_vecs: Dict[str, Tuple[Any, ...]]
                   ) -> List[StructuralHypothesis]:
        """Size-10 (4,5)/(5,4) splits: ONE boundary attempt (multiply
        roots only). Enumerate the size-4 side from the lazily generated
        divisibility-filtered B_4 candidate set, invert the multiply
        root to solve for the size-5 side, verify by the complete
        _rec_witness(v, 5), reconstruct the size-4 side via the
        complete _rec_witness(v, 4). Returns hits (bounded).

        Completeness argument (verified): _rec_witness(v, 5) tries all
        root splits (0,4)..(4,0); every one has a side <= 2, enumerated
        from the complete B_0/B_1/B_2 banks, with the larger side (<=4)
        verified by complete recursion. The B_4 filtered set contains
        every size-4 template tree that can be a multiply-root child
        (sound divisibility filter). Wildcard inversions (zero divisor
        with zero target) are skipped + recorded: the size-5 witness
        needs concrete vectors.

        Time-boxed (_SEARCH_45_BUDGET_S): aborts with
        search_45_timeout=True rather than hanging. This is an
        experimental boundary probe; add/subtract roots have no sound
        prefilter and remain resource-constrained.
        """
        found: List[StructuralHypothesis] = []
        b4 = _get_b4_filtered_set(v)
        if b4 is None:
            return found
        _t0 = time.time()
        for xvec in sorted(b4):
            if len(found) >= _BANK_MAX_HITS:
                break
            if time.time() - _t0 > _SEARCH_45_BUDGET_S:
                probe_stats["search_45_timeout"] = True
                break
            # Multiply is commutative; the size-5 side can be left or
            # right. known_is_left selects which child xvec is.
            for known_is_left in (True, False):
                if len(found) >= _BANK_MAX_HITS:
                    break
                yv = _probe_inverse("multiply", v, xvec,
                                    known_is_left=known_is_left)
                if yv is None or _WC in yv:
                    if yv is not None:
                        probe_stats["search_45_wildcard_skipped"] = \
                            probe_stats.get(
                                "search_45_wildcard_skipped", 0) + 1
                    continue
                yt = _rec_witness(yv, 5)
                if yt is None:
                    continue
                xt = _rec_witness(xvec, 4)
                if xt is None:
                    continue
                kids = (xt, yt) if known_is_left else (yt, xt)
                h = StructuralHypothesis("multiply", kids)
                key = _canon_bank_key(h, leaf_vecs)
                if key in seen:
                    continue
                seen.add(key)
                found.append(h)
        probe_stats["search_45_time_s"] = round(time.time() - _t0, 3)
        probe_stats["search_45_hits"] = len(found)
        probe_stats["search_45_b4_size"] = len(b4)
        return found

    # Size-4 candidate value set for (4,4) splits, divisibility-filtered
    # for multiply roots. A complete B_4 = T(B_3,B_0) + T(B_1,B_2) +
    # T(B_2,B_1) + T(B_0,B_3) over the three generic templates; for 10
    # inputs that is ~16.1M vectors (measured: 16,123,808 combos, ~152s
    # to generate) -- never materialized naively. The representation
    # already expresses B_4 (templates + bank recursion); only the
    # enumeration is infeasible. What makes it tractable is intermediate
    # structural information: for a MULTIPLY root, componentwise
    # divisibility of the target is a NECESSARY condition for any
    # size-4 multiplied subexpression, whatever its own root operator
    # (_divides_vec encodes the soundness conditions: integers,
    # nonzero; non-integers/bools are never filtered). So the B_4
    # candidates are generated lazily -- combos examined one at a time,
    # only divisibility survivors stored (typically dozens) -- and the
    # other size-4 side is verified by membership in that filtered set,
    # with trees reconstructed on demand by the complete _rec_witness.
    # Same structural abstraction as the (3,4) search (banked split
    # search + necessary-condition prefilter + on-demand
    # reconstruction), one level deeper -- not a special B_4 code path:
    # the bank being enumerated is still "the complete bank for the
    # smaller side", it is just too large to materialize unfiltered.
    #
    # Sound generation pruning: for multiply-TEMPLATE combos
    # vec = x*y, vec|v implies x|v and y|v (integers), so the B_3/B_2/
    # B_1/B_0 components are prefiltered by divisibility BEFORE
    # combining -- the prefilter can only drop components no completion
    # could use (if _divides_vec(v,x) is False then _divides_vec(v,x*y)
    # is False for every y; on non-integers/bools _divides_vec is the
    # identity, so the prefilter is vacuous there). For add/subtract-
    # template combos no sound component prefilter exists, so those
    # combos are generated in full and the combined vector is filtered.
    # _B4_BUDGET bounds the stored survivor set as pathological
    # insurance (e.g. an all-zero target, which everything divides);
    # past it the (4,4) search is skipped with b4_capped=True -- an
    # honest resource skip, not a hang. Non-integer targets skip
    # outright (b4_no_sound_filter): no sound necessary condition
    # exists there, and the unfiltered enumeration is infeasible.
    # Survivor-set budgets for the divisibility-filtered B_k candidate
    # sets, keyed by k: pathological insurance (e.g. an all-zero
    # target, which everything divides); past the budget the (k,k)
    # search is skipped with bk_capped=True -- an honest resource skip,
    # not a hang. k=4 keeps the proven value verbatim.
    _BK_BUDGETS: Dict[int, int] = {4: 200000, 5: 500000}
    _B4_BUDGET = _BK_BUDGETS[4]
    # Time budget for the size-10 (4,5) boundary attempt: abort rather
    # than hang.
    _SEARCH_45_BUDGET_S = 600.0
    # Time budget for the add/subtract-template banked enumeration
    # inside the B_5 filtered set. No sound component prefilter exists
    # for add/subtract, so those combos are generated in full (tens of
    # millions at 12 inputs, minutes measured). Past the budget the
    # enumeration stops with b5_addsub_timeout=True, keeping the
    # survivors found so far (sound but partial, splits recorded); the
    # multiply-template enumeration is complete regardless.
    _B5_ADDSUB_BUDGET_S = 480.0

    # Memo for the divisibility-filtered B_k candidate sets, keyed by
    # target vector: _search_44 (k=9), _search_45 (k=10) and _search_55
    # (k=11) would otherwise repeat the same lazy enumerations within
    # one probe (the B_5 enumeration itself recurses into the B_4
    # one). The enumeration is pure in (v, banks) and the banks are
    # fixed once built within a probe, so the cache is exact.
    _b4_cache: Dict[Any, Optional[set]] = {}
    _b5_cache: Dict[Any, Optional[set]] = {}

    def _get_bk_filtered_set(v: Tuple[Any, ...], k: int
                             ) -> Optional[set]:
        """Divisibility-filtered B_k candidate set (k = 4 or 5).

        One generic mechanism: the same lazy component-prefiltered
        enumeration, parameterized by k. Thin per-k cached entry
        points below preserve the proven k=4 call sites verbatim.
        """
        cache = _b4_cache if k == 4 else _b5_cache
        tag = f"b{k}"
        if v in cache:
            probe_stats[f"{tag}_cache_hit"] = \
                probe_stats.get(f"{tag}_cache_hit", 0) + 1
            return cache[v]
        res = _get_bk_filtered_set_uncached(v, k)
        cache[v] = res
        return res

    def _get_b4_filtered_set(v: Tuple[Any, ...]) -> Optional[set]:
        return _get_bk_filtered_set(v, 4)

    def _get_b5_filtered_set(v: Tuple[Any, ...]) -> Optional[set]:
        return _get_bk_filtered_set(v, 5)

    def _get_bk_filtered_set_uncached(v: Tuple[Any, ...], k: int
                                      ) -> Optional[set]:
        # Generic divisibility-filtered B_k enumeration (k = 4 or 5):
        # the complete B_k = sum over root splits (i, k-1-i) of
        # T(B_i, B_j) over the three generic templates is never
        # materialized naively; candidates are generated lazily, only
        # divisibility survivors stored. For a MULTIPLY root,
        # componentwise divisibility of the target is a NECESSARY
        # condition for any size-k multiplied subexpression, whatever
        # its own root operator (_divides_vec encodes the soundness
        # conditions: integers, nonzero; non-integers/bools are never
        # filtered) -- the same structural abstraction as the (3,4)
        # search, one level deeper: the bank being enumerated is still
        # "the complete bank for the smaller side", just too large to
        # materialize unfiltered.
        #
        # Sound generation pruning: for multiply-TEMPLATE combos
        # vec = x*y, vec|v implies x|v and y|v (integers), so the
        # components are prefiltered by divisibility BEFORE combining
        # -- the prefilter can only drop components no completion could
        # use (if _divides_vec(v,x) is False then _divides_vec(v,x*y)
        # is False for every y; on non-integers/bools _divides_vec is
        # the identity, so the prefilter is vacuous there). The
        # (k-1,0)/(0,k-1) multiply-template components recurse into
        # the filtered set of size k-1 (already divisor-filtered,
        # reused as-is). For add/subtract-template combos no sound
        # component prefilter exists, so those combos are generated in
        # full and the combined vector is filtered; the (k-1,0)/(0,k-1)
        # add/subtract splits have no feasible unfiltered enumeration
        # and are skipped + recorded, and the remaining banked
        # add/subtract splits are time-boxed (kept survivors stay
        # sound; the skip/timeout is recorded honestly).
        tag = f"b{k}"
        b3 = _get_b3_set()
        if b3 is None:
            probe_stats[f"{tag}_no_b3"] = True
            return None
        b2 = _get_b2()
        b1 = _banks[1]
        comp: Dict[int, List[Tuple[Any, ...]]] = {
            0: [zv for _, zv in leaf_items],
            1: list(b1.keys()),
            2: list(b2.keys()),
            3: list(b3),
        }
        if k == 5:
            b4 = _get_b4_filtered_set(v)
            if b4 is None:
                probe_stats["b5_no_b4"] = True
                return None
            comp[4] = list(b4)
        # The filter is sound only for all-integer targets; otherwise
        # _divides_vec never filters (soundness over speed) and the
        # unfiltered B_k enumeration is infeasible, so the (k,k) search
        # is skipped outright -- an honest resource skip, recorded, not
        # a hang.
        filter_active = all(isinstance(t, int) and not isinstance(t, bool)
                            for t in v)
        probe_stats[f"{tag}_filtered"] = filter_active
        if not filter_active:
            probe_stats[f"{tag}_no_sound_filter"] = True
            return None
        _t0 = time.time()
        _gen = 0
        kept: set = set()
        capped = False
        addsub_timed_out = False
        _addsub_done: List[Tuple[int, int]] = []
        # Divisor-component lists for the multiply template (sound:
        # vec = x*y dividing v implies x and y divide v). Size-4
        # components (k=5) come from the filtered B_4 set: already
        # divisor-filtered, reused as-is (exact, not recomputed).
        compd: Dict[int, List[Tuple[Any, ...]]] = {
            s: [x for x in vecs if _divides_vec("multiply", v, x)]
            for s, vecs in comp.items() if s <= 3
        }
        if k == 5:
            compd[4] = comp[4]
        # (size-A, size-B) per root split of a size-k tree. k=4 keeps
        # the legacy order verbatim; the combo SET examined (and hence
        # the survivor set and the _gen total when uncapped) is
        # order-independent in any case.
        if k == 4:
            _split_pairs = ((3, 0), (1, 2), (2, 1), (0, 3))
        else:
            _split_pairs = tuple((i, k - 1 - i) for i in range(k))
        # Template order: for k=5 the multiply template runs FIRST.
        # Its enumeration is COMPLETE (sound divisor prefilter, no
        # time-box) and must not be starved by the add/subtract
        # time-box: an add/subtract timeout breaks the template loop,
        # so multiply has to precede it. k=4 keeps the legacy order
        # verbatim (its add/subtract enumeration is complete, there
        # is no time-box to starve multiply). The combo SET examined
        # (and hence the survivor set and the _gen total when uncapped)
        # is order-independent in any case.
        _tmpl_order = _PROBE_TEMPLATES
        if k == 5:
            _tmpl_order = tuple(
                sorted(_PROBE_TEMPLATES,
                       key=lambda tm: 0 if tm[0] == "multiply" else 1))
        for tname, t in _tmpl_order:
            if capped or addsub_timed_out:
                break
            wirings: Tuple[bool, ...] = (True,) \
                if tname in _PROBE_COMMUTATIVE else (True, False)
            for i, j in _split_pairs:
                if capped or addsub_timed_out:
                    break
                if tname != "multiply" and k - 1 > 3 \
                        and max(i, j) == k - 1:
                    # Add/subtract (k-1,0)/(0,k-1): no sound component
                    # prefilter exists and the unfiltered size-(k-1)
                    # side is infeasible; recorded skip (same posture
                    # as skipped_44_op). The multiply template covers
                    # these splits via the filtered B_{k-1} components.
                    probe_stats[f"{tag}_skipped_op_{k - 1}0"] = tname
                    continue
                if tname == "multiply":
                    avecs, bvecs = compd[i], compd[j]
                else:
                    avecs, bvecs = comp[i], comp[j]
                for avec in avecs:
                    if capped or addsub_timed_out:
                        break
                    if tname != "multiply" and k == 5 and \
                            time.time() - _t0 > _B5_ADDSUB_BUDGET_S:
                        probe_stats["b5_addsub_timeout"] = True
                        addsub_timed_out = True
                        break
                    for bvec in bvecs:
                        for s_first in wirings:
                            _gen += 1
                            try:
                                if s_first:
                                    vec = tuple(
                                        t(aa, bb)
                                        for aa, bb in zip(avec, bvec))
                                else:
                                    vec = tuple(
                                        t(bb, aa)
                                        for aa, bb in zip(avec, bvec))
                            except Exception:
                                continue
                            if _divides_vec("multiply", v, vec):
                                kept.add(vec)
                                if len(kept) >= _BK_BUDGETS[k]:
                                    capped = True
                                    break
                            if capped:
                                break
                        if capped:
                            break
                if tname != "multiply" and k == 5 \
                        and not addsub_timed_out:
                    _addsub_done.append((i, j))
        probe_stats[f"{tag}_enum_generated"] = _gen
        probe_stats[f"{tag}_div_post"] = len(kept)
        probe_stats[f"{tag}_enum_time_s"] = round(time.time() - _t0, 3)
        probe_stats[f"{tag}_capped"] = capped
        if k == 5:
            probe_stats["b5_addsub_splits_done"] = _addsub_done
        if capped:
            return None
        return kept

    def _match_bk_set(bk: set,
                      pattern: Tuple[Any, ...]) -> List[Tuple[Any, ...]]:
        """Vectors in a filtered B_k set matching a (possibly
        wildcard) pattern."""
        if _WC not in pattern:
            return [pattern] if pattern in bk else []
        return [v for v in bk if _vec_matches(pattern, v)]

    def _match_b4_set(b4: set,
                      pattern: Tuple[Any, ...]) -> List[Tuple[Any, ...]]:
        return _match_bk_set(b4, pattern)

    def _search_kk(v: Tuple[Any, ...],
                   k: int,
                   seen: set,
                   leaf_vecs: Dict[str, Tuple[Any, ...]]
                   ) -> List[StructuralHypothesis]:
        """Size-(2k+1) (k,k) splits, k = 4 or 5: enumerate one size-k
        side from the lazily generated divisibility-filtered B_k
        candidate set (multiply roots only), invert to solve for the
        other size-k side, verify by membership in the filtered set,
        reconstruct both trees via the complete _rec_witness(v, k).
        Returns hits (bounded). One generic mechanism -- k=4 is the
        proven size-9 (4,4) search, k=5 the size-11 (5,5) search.

        Complete for all-integer targets (modulo the pathological
        bk_capped budget and, for k=5, the recorded add/subtract
        (4,0)/(0,4) template skip and addsub time-box inside the B_5
        enumeration): the filtered set contains every size-k
        MULTIPLY-template tree whose value vector can be a child of a
        multiply root (the filter is a sound necessary condition; the
        multiply template runs FIRST in the k=5 enumeration and is
        excluded from the add/subtract time-box, so its completeness
        cannot be starved by an add/subtract timeout), and both sides
        are reconstructed by the complete size-k witness.
        Add/subtract-TEMPLATE-rooted size-k children are best-effort
        for k=5 (time-boxed/skipped as recorded); add/subtract ROOT
        (k,k) searches have no sound prefilter: skipped + recorded
        (resource boundary). Non-integer targets skip outright (no
        sound filter exists): recorded, not hung on."""
        found: List[StructuralHypothesis] = []
        bk = _get_bk_filtered_set(v, k)
        if bk is None:
            return found
        for tname, _t in _PROBE_TEMPLATES:
            if tname != "multiply":
                # Add/subtract roots have no divisibility pre-filter;
                # the full B_k enumeration is infeasible. Documented
                # boundary: size-(2k+1) (k,k) is supported for multiply
                # roots; add/sub roots remain resource-constrained.
                # (k=4: the add/subtract roots _search_44 skips are
                # covered by _search_44_addsub instead, which pops this
                # stale record.)
                probe_stats[f"skipped_{k}{k}_op"] = tname
                continue
            # Multiply is commutative: one wiring (known side left).
            for xvec in sorted(bk):
                if len(found) >= _BANK_MAX_HITS:
                    break
                yv = _probe_inverse(tname, v, xvec, known_is_left=True)
                if yv is None:
                    continue
                mates = _match_bk_set(bk, yv)
                if not mates:
                    continue
                xt = _rec_witness(xvec, k)
                if xt is None:
                    continue
                for yvec in mates:
                    yt = _rec_witness(yvec, k)
                    if yt is None:
                        continue
                    h = StructuralHypothesis(tname, (xt, yt))
                    key = _canon_bank_key(h, leaf_vecs)
                    if key in seen:
                        continue
                    seen.add(key)
                    found.append(h)
                    if len(found) >= _BANK_MAX_HITS:
                        break
        return found

    def _search_44(v: Tuple[Any, ...],
                   seen: set,
                   leaf_vecs: Dict[str, Tuple[Any, ...]]
                   ) -> List[StructuralHypothesis]:
        return _search_kk(v, 4, seen, leaf_vecs)

    def _search_55(v: Tuple[Any, ...],
                   seen: set,
                   leaf_vecs: Dict[str, Tuple[Any, ...]]
                   ) -> List[StructuralHypothesis]:
        """Size-11 (5,5) splits: the k=5 instance of _search_kk."""
        return _search_kk(v, 5, seen, leaf_vecs)

    def _search_44_addsub(v: Tuple[Any, ...],
                          seen: set,
                          leaf_vecs: Dict[str, Tuple[Any, ...]]
                          ) -> List[StructuralHypothesis]:
        """Size-9 (4,4) splits for ADD/SUBTRACT roots: exact join
        reordering with a compact complete mate index.

        The two-sided structural question: no sound value-only
        ONE-side necessary condition exists for add/subtract roots (a
        summand's vector is unconstrained: any x admits y = v - x), so
        no prefilter can shrink the mate side. What remains sound is
        the JOIN reordering itself -- enumerate complete size-4 inner
        decompositions on ONE side, invert the add/subtract root ONCE
        per pair (exact: _probe_inverse is total on concrete
        vectors), verify the mate by O(1) membership in the complete
        B_4 set -- exactly analogous to _search_34_addsub, one level
        deeper. Hit-set equality with the naive formulation (enumerate
        every size-4 tree from its complete inner decomposition,
        invert the outer root, verify the mate in B_4) holds by
        construction: the same inner value vectors are computed in a
        different order with the same outer inversion and membership
        test.

        The B_4 "set" cannot be materialized naively (measured ~1.6GB
        at 10 inputs): below _ADDSUB_44_SET_THRESHOLD combos it is a
        Python set; above it a _BloomFilter (no false negatives, so
        completeness is preserved; false positives are resolved by the
        complete _rec_witness(v, 4) confirmation, so no invented
        hits). Commutative inner mirrors are enumerated once --
        behaviorally identical vectors dedupe by canonical key, so no
        canonical hit is lost.

        COMPLETENESS: the probe pass streams the FULL inner-pair
        stream -- no hit cap, no early stop. Every pair is visited
        within the _SEARCH_44_ADDSUB_BUDGET_S budget, hits are
        deduplicated by canonical semantic key (one confirmed witness
        retained per semantic hit), and only the final deduplicated
        hit list is sorted by behavioral relevance (|Pearson| of the
        hit's size-4 side vs the target -- values only) for advisory
        ordering. The hit SET is independent of that order: relevance
        is ordering-only, never pruning. On budget exhaustion the
        timeout is recorded honestly (addsub_44_timeout) and the
        partial hit list is returned; the synthesizer's complete
        fallback covers a truncated search.

        All three root wirings are covered: add (v = s4 + S4; the
        mirror is key-identical by commutative sorting), sub-left
        (v = s4 - S4), sub-right (v = S4 - s4). Trees are reconstructed
        on demand: the inner pair from the bank dicts (B_0/B_1/B_2) or
        _rec_witness(v, 3) for B_3 vectors; the mate via the complete
        _rec_witness(v, 4). Works for non-integer targets too (no
        divisibility filter is involved; float rounding can only cause
        misses, never false hits).

        Telemetry: addsub_44_searched, addsub_44_pairs (inner pairs),
        addsub_44_combos (index inserts), addsub_44_index
        ("set"/"bloom"), addsub_44_build_s, addsub_44_fp_confirms
        (bloom hits rejected by the witness), addsub_44_hits,
        addsub_44_time_s, addsub_44_timeout.
        """
        found: List[StructuralHypothesis] = []
        b3 = _get_b3_set()
        if b3 is None:
            return found
        # The add/subtract roots _search_44 skips are covered here
        # instead; the stale skip record is superseded.
        probe_stats.pop("skipped_44_op", None)
        probe_stats["addsub_44_searched"] = True
        _t0 = time.time()
        _n_combos = 0
        _n_fp = 0
        b0_items = list(_banks[0].items())      # (vec, name)
        b1_items = list(_banks[1].items())      # (vec, tree)
        b2_items = list(_get_b2().items())      # (vec, tree)
        b3_vecs = list(b3)                       # vectors; trees on demand
        _b0_vecs = [zv for zv, _ in b0_items]
        _b1_vecs = [vv for vv, _ in b1_items]
        _b2_vecs = [vv for vv, _ in b2_items]

        # Behavioral relevance of one value vector vs the target
        # (values only). Used ONLY to order the final deduplicated hit
        # list -- ordering-only, never pruning.
        _tgt_f = [float(t) for t in v]

        def _vec_rel(_vec: Tuple[Any, ...]) -> float:
            try:
                return abs(_pearson([float(_x) for _x in _vec],
                                    _tgt_f))
            except Exception:
                return 0.0

        # (a_kind, a_vecs, b_kind, b_vecs): the complete size-4 inner
        # decompositions. Every size-4 template tree has a root split
        # with children of sizes (3,0), (0,3), (1,2) or (2,1); the two
        # pair-sets below cover all four -- for commutative inner
        # templates t(b,a)==t(a,b), and for subtract both wirings are
        # enumerated per pair-set, so mirror pair-sets would only
        # regenerate behaviorally identical vectors (the E1 exactness
        # test checks this against an independent naive enumerating
        # all four).
        _pair_sets = (
            ("b3", b3_vecs, "b0", _b0_vecs),
            ("b1", _b1_vecs, "b2", _b2_vecs),
        )

        def _iter_pairs():
            """Yield (akind, avec, aidx, bkind, bvec, bidx, tname, t,
            s_first) over the complete inner decompositions, in a
            fixed deterministic order."""
            for _tname, _t in _PROBE_TEMPLATES:
                _wirings: Tuple[bool, ...] = (True,) \
                    if _tname in _PROBE_COMMUTATIVE else (True, False)
                for (_ak, _av, _bk, _bv) in _pair_sets:
                    for _ai, _avec in enumerate(_av):
                        for _bi, _bvec in enumerate(_bv):
                            for _s_first in _wirings:
                                yield (_ak, _avec, _ai, _bk, _bvec,
                                       _bi, _tname, _t, _s_first)

        def _fwd(_t: Any, _avec: Tuple[Any, ...], _bvec: Tuple[Any, ...],
                 _s_first: bool) -> Optional[Tuple[Any, ...]]:
            try:
                if _s_first:
                    return tuple(_t(_aa, _bb)
                                 for _aa, _bb in zip(_avec, _bvec))
                return tuple(_t(_bb, _aa)
                             for _aa, _bb in zip(_avec, _bvec))
            except Exception:
                return None

        def _over_budget() -> bool:
            return time.time() - _t0 > _SEARCH_44_ADDSUB_BUDGET_S

        # ---- pass 1: build the complete B_4 mate index ------------
        # Streamed with O(1) memory beyond the index itself: every
        # inner pair's s4 vector is inserted, nothing is retained.
        # The probe pass then verifies mates against this complete
        # index. (The old design scored every pair and kept the full
        # score arrays + a Python sort index -- ~1.2GB at 10 inputs;
        # relevance now only orders the final deduplicated hit list.)
        _p30 = len(b3_vecs) * len(_b0_vecs)
        _p12 = len(_b1_vecs) * len(_b2_vecs)
        # Two pair-sets: add/multiply contribute (P30+P12) each (one
        # wiring), subtract contributes 2*(P30+P12) (both wirings).
        _est_combos = 4 * (_p30 + _p12)
        _use_set = _est_combos <= _ADDSUB_44_SET_THRESHOLD
        probe_stats["addsub_44_index"] = "set" if _use_set else "bloom"
        _b4_set: Optional[set] = set() if _use_set else None
        _bloom: Optional[_BloomFilter] = None
        if not _use_set:
            _bloom = _BloomFilter(_est_combos)
        _t_build = time.time()
        _n_index_pairs = 0
        _timed_out = False
        for (_ak, _avec, _aidx, _bk, _bvec, _bidx, _tn, _tt,
             _sf) in _iter_pairs():
            _n_index_pairs += 1
            if (_n_index_pairs & 0x3FFFF) == 0 and _over_budget():
                _timed_out = True
                break
            _s4 = _fwd(_tt, _avec, _bvec, _sf)
            if _s4 is None:
                continue
            _n_combos += 1
            if _use_set:
                assert _b4_set is not None
                _b4_set.add(_s4)
            else:
                assert _bloom is not None
                _bloom.add(_s4)
        probe_stats["addsub_44_pairs"] = _n_index_pairs
        probe_stats["addsub_44_combos"] = _n_combos
        probe_stats["addsub_44_build_s"] = round(time.time() - _t_build,
                                                3)

        def _inner_tree(_kind: str, _vec: Tuple[Any, ...], _idx: int):
            if _kind == "b0":
                for _zv, _zn in b0_items:
                    if _zv == _vec:
                        return _zn
                return None
            if _kind == "b1":
                return b1_items[_idx][1]
            if _kind == "b2":
                return b2_items[_idx][1]
            return _rec_witness(_vec, 3)

        # Per-hit relevance scores, parallel to `found` (hits only --
        # O(#hits) memory). Only the final list is sorted by these.
        _hit_scores: List[float] = []

        def _note_hit(_s4t: Any, _yt: Any, _outer: str,
                      _score: float) -> None:
            if _outer == "add":
                _h = StructuralHypothesis("add", (_s4t, _yt))
            elif _outer == "sub-left":
                _h = StructuralHypothesis("subtract", (_s4t, _yt))
            else:  # sub-right
                _h = StructuralHypothesis("subtract", (_yt, _s4t))
            _key = _canon_bank_key(_h, leaf_vecs)
            if _key in seen:
                return
            seen.add(_key)
            found.append(_h)
            _hit_scores.append(_score)

        def _mate_ok(_ovec: Tuple[Any, ...]) -> Optional[Any]:
            """O(1) mate check + tree. Set path: exact. Bloom path:
            filter gate, then the complete witness confirms (false
            positives rejected -- counted, never emitted)."""
            nonlocal _n_fp
            if _use_set:
                assert _b4_set is not None
                if _ovec not in _b4_set:
                    return None
            else:
                assert _bloom is not None
                if _ovec not in _bloom:
                    return None
            _yt = _rec_witness(_ovec, 4)
            if _yt is None:
                _n_fp += 1
                return None
            return _yt

        # ---- pass 2: probe the join over the FULL pair stream -----
        # No hit cap and no early stop: every inner pair is visited
        # within the budget. Hits dedupe by canonical semantic key
        # (one confirmed witness per semantic hit). The scan order is
        # the fixed template order -- deterministic and
        # ordering-independent.
        _n_probe = 0
        for (_ak, _avec, _aidx, _bk, _bvec, _bidx, _tn, _tt,
             _sf) in _iter_pairs():
            if _timed_out:
                break
            _n_probe += 1
            if (_n_probe & 0x3FFFF) == 0 and _over_budget():
                _timed_out = True
                break
            _s4 = _fwd(_tt, _avec, _bvec, _sf)
            if _s4 is None:
                continue
            # Invert the outer add/subtract root once per pair
            # (exact on concrete vectors); the mate is verified by
            # O(1) index membership.
            for _outer, _known_left in (("add", True),
                                       ("sub-left", True),
                                       ("sub-right", False)):
                _op = "add" if _outer == "add" else "subtract"
                _ovec = _probe_inverse(_op, v, _s4,
                                       known_is_left=_known_left)
                if _ovec is None or _WC in _ovec:
                    continue
                _yt = _mate_ok(_ovec)
                if _yt is None:
                    continue
                _pt = _inner_tree(_ak, _avec, _aidx)
                _qt = _inner_tree(_bk, _bvec, _bidx)
                if _pt is None or _qt is None:
                    continue
                _kids = (_pt, _qt) if _sf else (_qt, _pt)
                _s4t = StructuralHypothesis(_tn, _kids)
                _note_hit(_s4t, _yt, _outer, _vec_rel(_s4))
        # Ordering-only: the complete deduplicated hit list, most
        # relevant first. The hit SET does not depend on this order.
        if found:
            _ord = sorted(range(len(found)),
                          key=_hit_scores.__getitem__, reverse=True)
            found = [found[_i] for _i in _ord]
        probe_stats["addsub_44_fp_confirms"] = _n_fp
        probe_stats["addsub_44_hits"] = len(found)
        probe_stats["addsub_44_timeout"] = _timed_out
        probe_stats["addsub_44_time_s"] = round(time.time() - _t0, 3)
        return found

    # Bound on distinct hypotheses collected per probe (keeps the
    # advisory output small and deterministic).
    _BANK_MAX_HITS = 8
    # Memo for the recursive witness search: (vector, size) -> tree/None.
    _memo: Dict[Any, Optional[Any]] = {}

    def _split_order(kk: int) -> List[Tuple[int, int]]:
        splits = [(i, kk - 1 - i) for i in range(kk)]
        splits.sort(key=lambda s: (0 if max(s) <= 2 else 1, min(s)))
        return splits

    def _canon_bank_key(tree: StructuralHypothesis,
                        leaf_vecs: Dict[str, Tuple[Any, ...]]) -> Any:
        kids = []
        for c in tree.children:
            if isinstance(c, StructuralHypothesis):
                kids.append(_canon_bank_key(c, leaf_vecs))
            else:
                kids.append(("leaf", _stable_tiebreak(leaf_vecs[c])))
        if tree.op in _PROBE_COMMUTATIVE:
            kids = sorted(kids, key=repr)
        return (tree.op, tuple(kids))

    def _try_split_sides(v: Tuple[Any, ...], i: int, j: int,
                         first_only: bool,
                         seen: Optional[set],
                         leaf_vecs: Dict[str, Tuple[Any, ...]]
                         ) -> List[StructuralHypothesis]:
        # Enumerate the smaller side from its bank, invert to solve the
        # larger side, verify by bank membership (size <= 2) or complete
        # recursion. Returns hits: first only, or up to _BANK_MAX_HITS
        # canonically distinct.
        found: List[StructuralHypothesis] = []
        e = min(i, j)
        g = max(i, j)
        E = _banks[e]
        G = _banks[g] if g <= 2 else None
        for evec, etree in E.items():
            if found and (first_only or len(found) >= _BANK_MAX_HITS):
                break
            for tname, _t in _PROBE_TEMPLATES:
                if found and (first_only or len(found) >= _BANK_MAX_HITS):
                    break
                # etree is the size-e side: left child when i <= j
                # (solve right), right child when i > j (solve left).
                yv = _probe_inverse(tname, v, evec,
                                   known_is_left=(i <= j))
                if yv is None:
                    continue
                if G is not None:
                    # Solved side has size <= 2: verify by (wildcard)
                    # bank membership. Take all matches when collecting,
                    # first match for first-only.
                    ytrees = _bank_lookup(G, yv)
                    if not ytrees:
                        continue
                    ytree_iter = ytrees if not first_only else ytrees[:1]
                else:
                    yw = _rec_witness(yv, g)
                    if yw is None:
                        continue
                    ytree_iter = (yw,)
                for ytree in ytree_iter:
                    kids = (etree, ytree) if i <= j else (ytree, etree)
                    h = StructuralHypothesis(tname, kids)
                    if seen is not None:
                        key = _canon_bank_key(h, leaf_vecs)
                        if key in seen:
                            continue
                        seen.add(key)
                    found.append(h)
                    if first_only or len(found) >= _BANK_MAX_HITS:
                        break
                if found and (first_only or len(found) >= _BANK_MAX_HITS):
                    break
        return found

    def _rec_witness(v: Tuple[Any, ...], kk: int
                     ) -> Optional[StructuralHypothesis]:
        # First-witness recursion for the solved (larger) side, kk >= 3.
        key = (v, kk)
        if key in _memo:
            return _memo[key]
        res: Optional[StructuralHypothesis] = None
        for i, j in _split_order(kk):
            if min(i, j) > 2:
                continue
            got = _try_split_sides(v, i, j, True, None, {})
            if got:
                res = got[0]
                break
        _memo[key] = res
        return res

    # Find witnesses for the target at each size up to max_size.
    # For k=2,3 use explicit all-hits enumeration (cheap). For k>=4 use
    # the banked split search, but ONLY if no smaller witness was found
    # (minimality: a smaller exact-fit makes larger ones redundant, and
    # the deeper search is the expensive part).
    probe_stats: Dict[str, Any] = {"max_size": max_size}
    _leaf_vecs = {nm: tuple(col_vals[nm]) for nm in names}
    raw: List[StructuralHypothesis] = []
    # Telemetry counters.
    _probe_t0 = time.time()
    _n_splits_examined = 0
    _n_inversions = 0
    # Per-k phase timing (additive telemetry for cost decomposition).
    _k_times: Dict[int, float] = {}
    for k in range(max(2, _k_min), max_size + 1):
        if raw:
            break
        _k_t0 = time.time()
        if k <= 3:
            _collect_all_k(k, raw, target, s1, s1_items, leaf_items,
                           _tree_for_s1)
        else:
            _get_b2()
            probe_stats["b2_size"] = len(_banks[2])
            seen: set = set()
            # For k=8, try the balanced (3,4)/(4,3) splits FIRST via the
            # divisibility-filtered flat witness: they are cheap for
            # multiply roots and the (2,5) nested enumeration can blow
            # up before we reach them.
            _splits = _split_order(k)
            if k == 8:
                _balanced = [(i, j) for i, j in _splits
                             if (i, j) in ((3, 4), (4, 3))]
                _rest = [(i, j) for i, j in _splits
                         if (i, j) not in ((3, 4), (4, 3))]
                _splits = _balanced + _rest
            if k == 11:
                # Try the balanced (5,5) split first: every other
                # split at k=11 is guard-skipped (recorded) below, so
                # this only fixes the examination order, mirroring
                # the k==8 precedent.
                _balanced = [(i, j) for i, j in _splits
                             if (i, j) == (5, 5)]
                _rest = [(i, j) for i, j in _splits
                         if (i, j) != (5, 5)]
                _splits = _balanced + _rest
            for i, j in _splits:
                _n_splits_examined += 1
                if len(raw) >= _BANK_MAX_HITS:
                    break
                if k == 8 and (i, j) not in ((3, 4), (4, 3)):
                    # Size-8 unbalanced splits (2,5)/(1,6)/... require
                    # nested witness enumeration that blows up
                    # (B_2 x recursive size-5 witness). Documented
                    # resource boundary: size-8 covers balanced splits.
                    probe_stats["skipped_split"] = (i, j)
                    continue
                if k >= 9 and max(i, j) > 4 and \
                        (i, j) not in ((4, 5), (5, 4), (5, 5)):
                    # Size-9+: any split with a side larger than 4
                    # needs a B_4+ enumerated side (resource/enumeration
                    # boundary -- the representation already expresses
                    # B_4+ via templates + bank recursion; only the
                    # enumeration is infeasible) or a nested recursive
                    # witness over a large bank (resource boundary: the
                    # (2,6) nested enumeration exceeds a 600s budget).
                    # Record the skip honestly instead of hanging. The
                    # size-10 (4,5)/(5,4) multiply-root attempt is
                    # exempted (handled by _search_45, time-boxed),
                    # as is the size-11 (5,5) multiply-root attempt
                    # (handled by _search_55 via the B_5 filtered set).
                    probe_stats["skipped_split"] = (i, j)
                    continue
                if (i, j) == (3, 3):
                    # Balanced size-7 split: enumerate the B_3 value
                    # set, reconstruct trees on demand.
                    b3s = _get_b3_set()
                    probe_stats["b3_size"] = len(b3s) if b3s else 0
                    probe_stats["b3_capped"] = _b3_capped
                    raw.extend(_search_33(target, seen, _leaf_vecs))
                elif (i, j) == (3, 4) or (i, j) == (4, 3):
                    # Size-8 (3,4)/(4,3): B_3 enumeration for the
                    # size-3 side (divisibility pre-filtered for
                    # multiply), flat bank-membership witness for the
                    # size-4 side. No nested recursion. Add/subtract
                    # roots are covered by the exact join reordering
                    # (dispatched once -- it enumerates all three root
                    # wirings internally); the multiply path is
                    # untouched.
                    b3s = _get_b3_set()
                    probe_stats["b3_size"] = len(b3s) if b3s else 0
                    raw.extend(_search_34(target, seen, _leaf_vecs,
                                          three_on_left=(i == 3)))
                    if (i, j) == (3, 4):
                        raw.extend(_search_34_addsub(target, seen,
                                                     _leaf_vecs))
                elif (i, j) == (4, 4):
                    # Size-9 (4,4): lazily generated divisibility-filtered
                    # B_4 candidate set for multiply roots (both sides
                    # reconstructed via the complete size-4 witness).
                    # The add/subtract join (_search_44_addsub: complete
                    # size-4 inner decompositions enumerated on one side,
                    # the add/subtract root inverted once per pair, the
                    # mate verified by O(1) membership in the complete B_4
                    # set -- hit-set-equal to the naive formulation by
                    # construction) runs ONLY when the multiply search
                    # found no (4,4) hypotheses. Rationale: the join burns
                    # up to 900s proving a negative on multiply-rooted
                    # targets (B1 probe went 300s -> 1239s, starving
                    # synthesis); the probe is advisory, synthesis keeps
                    # its complete fallback, and the proven B4 baseline
                    # skipped add/sub (4,4) entirely -- conditional dispatch
                    # is strictly more informative than baseline with no
                    # correctness cost. Deferred dispatch is recorded
                    # honestly as addsub_44_deferred.
                    b3s = _get_b3_set()
                    probe_stats["b3_size"] = len(b3s) if b3s else 0
                    _mul_44 = _search_44(target, seen, _leaf_vecs)
                    raw.extend(_mul_44)
                    if _mul_44:
                        probe_stats["addsub_44_deferred"] = True
                    else:
                        raw.extend(_search_44_addsub(
                            target, seen, _leaf_vecs))
                elif (i, j) in ((4, 5), (5, 4)):
                    # Size-10 (4,5)/(5,4): ONE boundary attempt --
                    # _search_45 covers both wirings internally
                    # (known_is_left in (True, False)), so dispatch once
                    # for the pair. A second call would redo the B_4
                    # enumeration and the join while every hypothesis is
                    # already in `seen` (pure recomputation); dispatching
                    # once also keeps the search_45_* telemetry from
                    # being overwritten by the redundant call.
                    # Multiply roots only (divisibility-filtered B_4
                    # side, complete size-5 witness for the other).
                    # Time-boxed; add/sub roots have no sound prefilter.
                    if (i, j) == (4, 5):
                        probe_stats["search_45_attempted"] = True
                        raw.extend(_search_45(target, seen, _leaf_vecs))
                    else:
                        probe_stats["search_45_pair_covered"] = True
                elif (i, j) == (5, 5):
                    # Size-11 (5,5): boundary attempt -- _search_55 is
                    # the k=5 instance of the generic _search_kk
                    # (divisibility-filtered B_5 side, multiply roots
                    # only, both sides reconstructed via the complete
                    # _rec_witness(v, 5)). The B_5 enumeration's
                    # add/subtract-template banked splits are
                    # time-boxed; (4,0)/(0,4) add/subtract splits are
                    # skipped + recorded inside the enumeration.
                    b3s = _get_b3_set()
                    probe_stats["b3_size"] = len(b3s) if b3s else 0
                    probe_stats["search_55_attempted"] = True
                    raw.extend(_search_55(target, seen, _leaf_vecs))
                elif min(i, j) > 2:
                    # Needs a B_4+ enumerated side: documented boundary.
                    probe_stats["skipped_split"] = (i, j)
                    continue
                else:
                    raw.extend(_try_split_sides(target, i, j, False, seen,
                                                _leaf_vecs))
        _k_times[k] = round(time.time() - _k_t0, 3)
    probe_stats["k_time_s"] = _k_times
    # Canonicalize + dedupe (as before).
    def _canon_key(tree: StructuralHypothesis) -> Any:
        kids = []
        for c in tree.children:
            if isinstance(c, StructuralHypothesis):
                kids.append(_canon_key(c))
            else:
                kids.append(("leaf", _stable_tiebreak(leaf_vecs[c])))
        if tree.op in _PROBE_COMMUTATIVE:
            kids = sorted(kids, key=repr)
        return (tree.op, tuple(kids))

    seen = set()
    out: List[StructuralHypothesis] = []
    for tree in raw:
        key = _canon_key(tree)
        if key in seen:
            continue
        seen.add(key)
        out.append(tree)
    # Structural-rival detection (evidence-bounded honesty): for each
    # hypothesis, find distinct trees with the same training value vector
    # that disagree on synthetic probes. A hypothesis with rivals is
    # underdetermined by the training evidence; the synthesizer's
    # identifiability gate will fail closed on it rather than guess.
    _rival_probes = _synthetic_probe_assignments(names, _leaf_vecs)
    for _h in out:
        _h.rivals = _find_structural_rivals(_h, _leaf_vecs, names,
                                            _rival_probes)
        # R2'': dependency-expansion rivals. A leaf the hypothesis
        # spells directly may be functionally determined by other
        # inputs on the training evidence; the full expansion is a
        # behaviorally-distinct rival that the training evidence
        # cannot rule out (silent wrong-commit gap). Banks are
        # complete by construction (B_1/B_2) or value-complete (B_3);
        # the lookup is O(1) per leaf plus on-demand B_3 tree
        # reconstruction only for matching vectors.
        _h.rivals.extend(attach_expansion_rivals(
            _h, _leaf_vecs, names, _rival_probes,
            _banks[1], _get_b2(), _get_b3_set(),
            lambda _v: _rec_witness(_v, 3)))
    probe_stats["n_underdetermined"] = sum(1 for _h in out if _h.rivals)
    probe_stats["n_rivals"] = sum(len(_h.rivals) for _h in out)
    out.sort(key=lambda h: (h.size(), _stable_tiebreak(
        tuple(leaf_vecs[nm] for nm in h.leaf_names()))))
    probe_stats["n_hypotheses"] = len(out)
    probe_stats["splits_examined"] = _n_splits_examined
    probe_stats["probe_time_s"] = round(time.time() - _probe_t0, 3)
    return out, probe_stats


def _collect_all_k(k: int, raw: List[StructuralHypothesis],
                   target: Tuple[Any, ...],
                   s1: Dict[Tuple[Any, ...], Tuple[str, str, str]],
                   s1_items: List[Any], leaf_items: List[Any],
                   tree_for_s1) -> None:
    """Explicit all-hits enumeration for k=2,3 (cheap, preserves the
    multiple-equivalent-hits behavior the synthesizer relies on)."""
    if k == 2:
        # Chain: T2(S1, z) / T2(z, S1).
        for svec, _ in s1_items:
            inner = tree_for_s1(svec)
            for t2n, t2 in _PROBE_TEMPLATES:
                for z, zv in leaf_items:
                    wirings: Tuple[bool, ...] = (True,) \
                        if t2n in _PROBE_COMMUTATIVE else (True, False)
                    for inner_first in wirings:
                        try:
                            if inner_first:
                                ok = all(t2(si, zi) == o for si, zi, o
                                         in zip(svec, zv, target))
                            else:
                                ok = all(t2(zi, si) == o for si, zi, o
                                         in zip(svec, zv, target))
                        except Exception:
                            ok = False
                        if ok:
                            kids = (inner, z) if inner_first else (z, inner)
                            raw.append(StructuralHypothesis(t2n, kids))
    elif k == 3:
        # Balanced: T3(S1, S1) via inversion (wildcard-aware: a zero
        # divisor with zero target leaves the solved side free on those
        # positions, matched by bank agreement).
        s1_bank = {vec: tree_for_s1(vec) for vec in s1}
        for t3n, _t3 in _PROBE_TEMPLATES:
            for s1v, _ in s1_items:
                s2v = _probe_inverse(t3n, target, s1v, known_is_left=True)
                if s2v is not None:
                    for m in _bank_lookup(s1_bank, s2v):
                        raw.append(StructuralHypothesis(
                            t3n, (tree_for_s1(s1v), m)))
                if t3n not in _PROBE_COMMUTATIVE:
                    s2v = _probe_inverse(t3n, target, s1v,
                                         known_is_left=False)
                    if s2v is not None:
                        for m in _bank_lookup(s1_bank, s2v):
                            raw.append(StructuralHypothesis(
                                t3n, (m, tree_for_s1(s1v))))
        # Chain: T3(S2, z) with S2 verified by inversion + S1.
        for t3n, _t3 in _PROBE_TEMPLATES:
            for z, zv in leaf_items:
                for inner_first in ((True,) if t3n in _PROBE_COMMUTATIVE
                                    else (True, False)):
                    s2v = _probe_inverse(
                        t3n, target, zv,
                        known_is_left=not inner_first)
                    if s2v is None:
                        continue
                    w = _witness_size2(s2v, t3n, s1_bank,
                                       leaf_items)
                    if w is not None:
                        kids = (w, z) if inner_first else (z, w)
                        raw.append(StructuralHypothesis(t3n, kids))


def _witness_size2(vec: Tuple[Any, ...], _t3n: str,
                   s1_bank: Dict[Tuple[Any, ...], StructuralHypothesis],
                   leaf_items: List[Any]
                   ) -> Optional[StructuralHypothesis]:
    """Size-2 witness for vec: T2(S1, leaf) / T2(leaf, S1).

    Wildcard-aware: vec may contain _WC (free positions); bank lookup
    matches on the constrained positions.
    """
    for t2n, _t2 in _PROBE_TEMPLATES:
        for z2, z2v in leaf_items:
            av = _probe_inverse(t2n, vec, z2v, known_is_left=False)
            if av is not None:
                hits = _bank_lookup(s1_bank, av)
                if hits:
                    return StructuralHypothesis(t2n, (hits[0], z2))
            if t2n not in _PROBE_COMMUTATIVE:
                bv = _probe_inverse(t2n, vec, z2v, known_is_left=True)
                if bv is not None:
                    hits = _bank_lookup(s1_bank, bv)
                    if hits:
                        return StructuralHypothesis(t2n, (z2, hits[0]))
    return None


