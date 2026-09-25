"""
swarm_engine/cognition/reification.py

ReificationObserver: autonomous representation expansion from experience.

The missing link the architecture already had both halves of:

  1. CaseMemory persists every verified (goal, expr, examples, param_names)
     record (cognition/representations.py).
  2. PrimitivePromoter crystallizes a verified Expr into a genuinely new,
     hash-named, registered Primitive -- re-verified, persisted, rehydrated
     (capability/primitive_promotion.py).

but nothing ever connected them: PrimitivePromoter.promote() had no live
callers. This observer closes that wiring gap, and goes one step further
than whole-expression promotion: it detects RECURRING SUB-COMPUTATIONS --
sub-expression trees that independently appear in the verified solutions of
two or more DISTINCT goals -- and promotes those.

Detection is purely structural and goal-agnostic:

  * sub-expressions are compared under ALPHA-EQUIVALENCE (parameters renamed
    by order of first appearance), so `upper(concat(first, last))` and
    `upper(concat(a, b))` are recognised as the same computation even though
    the goals, vocabularies, and parameter names differ completely;
  * literals stay part of the fingerprint (`add(x, 1)` != `add(x, 2)`), so
    no over-generalisation;
  * support counts DISTINCT goal strings, never re-solves of one goal;
  * only non-trivial sub-expressions (size >= 2 primitive applications)
    qualify -- a single application is already atomic in the registry and
    promoting it would buy nothing.

What "verification" means here, stated honestly (Defined-Bounded): the
promoted primitive is verified for FIDELITY -- the new primitive's own
calling convention is re-checked against examples derived by executing the
sub-expression on the parent case's live inputs (PrimitivePromoter.promote
does this re-check itself and refuses on any mismatch). The SEMANTICS of
the sub-computation is inherited, definitionally, from the end-to-end
verified parent solutions it was extracted from: a sub-tree of a verified
tree. Ground-truth verification of an intermediate value in isolation is
not available in general, and this module does not pretend otherwise.

Failure modes are fail-closed: any exception during observation is
reported, never raised into the success path that called it.
"""
from __future__ import annotations

import time
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

from swarm_engine.cognition.representations import Expr, evaluate_expr


# A sub-expression must collapse at least this many primitive applications
# to be worth reifying. Size 1 is a single primitive application -- already
# atomic in the registry; promoting it changes nothing about reachability.
MIN_SUBEXPR_SIZE = 2
# A computation must recur across this many DISTINCT goals before it earns
# reification. Recurrence across independent successes is the evidence that
# the computation is reusable rather than incidental to one goal.
MIN_SUPPORT_GOALS = 2


def sub_exprs(expr: Expr) -> List[Expr]:
    """All sub-expressions of expr, including expr itself (pre-order)."""
    out: List[Expr] = []

    def rec(e: Expr) -> None:
        out.append(e)
        if not e.is_leaf():
            for _, child in e.children:
                rec(child)

    rec(expr)
    return out


def alpha_rename(expr: Expr) -> Tuple[Expr, Tuple[str, ...], Dict[str, str]]:
    """Rename parameters by order of first appearance: p0, p1, ...

    Returns (renamed_expr, param_order, old_to_new). Literals are untouched,
    so they remain part of the identity of the computation.
    """
    order: List[str] = []

    def rec(e: Expr) -> Expr:
        if e.is_leaf():
            if e.is_literal:
                return Expr(literal=e.literal, is_literal=True)
            if e.param not in order:
                order.append(e.param)
            return Expr(param=f"p{order.index(e.param)}")
        return Expr(op=e.op,
                    children=tuple((name, rec(child))
                                   for name, child in e.children))

    renamed = rec(expr)
    param_order = tuple(f"p{i}" for i in range(len(order)))
    mapping = {old: f"p{i}" for i, old in enumerate(order)}
    return renamed, param_order, mapping


def alpha_fingerprint(expr: Expr) -> str:
    """Structural identity of a computation, modulo parameter naming."""
    renamed, _, _ = alpha_rename(expr)
    return renamed.canonical()


class ReificationObserver:
    """Watches verified syntheses; reifies recurring sub-computations."""

    def __init__(self, promoter, cases,
                 min_size: int = MIN_SUBEXPR_SIZE,
                 min_support: int = MIN_SUPPORT_GOALS):
        self.promoter = promoter
        self.cases = cases
        self.min_size = min_size
        self.min_support = min_support
        # fingerprints already promoted (this session); the promoter itself
        # is idempotent across sessions via its deterministic hash names.
        self._done: Set[str] = set()
        self.history: List[Dict[str, Any]] = []

    # -- main path ------------------------------------------------------
    def observe(self, expr: Optional[Expr], goal: str,
                examples: Optional[Sequence[Tuple[Dict[str, Any], Any]]],
                param_names: Optional[Sequence[str]] = None) -> List[str]:
        """Inspect one verified success; promote newly-supported recurrences.

        Returns the names of primitives promoted by THIS call (possibly
        empty). Never raises: failures are recorded in self.history.
        """
        promoted: List[str] = []
        if expr is None or not examples:
            return promoted
        try:
            support = self._support_index()
            law_by_goal = self._law_index()
            seen: Set[str] = set()
            for se in sub_exprs(expr):
                if se.size() < self.min_size:
                    continue
                fp = alpha_fingerprint(se)
                if fp in seen:
                    continue
                seen.add(fp)
                supporters = support.get(fp, set())
                if len(supporters) < self.min_support:
                    continue
                laws: Set[Tuple[Optional[str], Optional[str]]] = set()
                for g in supporters:
                    laws |= law_by_goal.get(g, set())
                # NOTE: _promote_one is called even when fp is already in
                # _done. The merge branch inside must run unconditionally:
                # a second law's cases for an already-promoted structure
                # arrive with fp in _done, and skipping the call would drop
                # their justification link.
                name = self._promote_one(se, fp, goal, examples, laws)
                if name is not None:
                    self._done.add(fp)
                    promoted.append(name)
        except Exception as exc:  # fail-closed: never break the success path
            self.history.append({"at": time.time(), "goal": goal,
                                 "error": f"{type(exc).__name__}: {exc}"})
        return promoted

    # -- internals ------------------------------------------------------
    def _support_index(self) -> Dict[str, Set[str]]:
        """fingerprint -> distinct goals whose verified expr contains it."""
        index: Dict[str, Set[str]] = {}
        for _cid, entry in self.cases.all():
            if entry.expr is None or not entry.goal:
                continue
            for se in sub_exprs(entry.expr):
                if se.size() < self.min_size:
                    continue
                fp = alpha_fingerprint(se)
                index.setdefault(fp, set()).add(entry.goal)
        return index

    def _law_index(self) -> Dict[str, Set[Tuple[Optional[str], Optional[str]]]]:
        """goal -> governing laws behind the cases remembered under it.

        Only cases routed from grounding carry a law; cognition syntheses
        contribute support but no law attribution. Identity of a law is
        (predicate, semantic_id) -- never fixture vocabulary.
        """
        index: Dict[str, Set[Tuple[Optional[str], Optional[str]]]] = {}
        for _cid, entry in self.cases.all():
            law = getattr(entry, "law", None)
            if not law:
                continue
            index.setdefault(entry.goal, set()).add(
                (law.get("predicate"), law.get("semantic_id")))
        return index

    def _promote_one(self, se: Expr, fp: str, goal: str,
                     examples: Sequence[Tuple[Dict[str, Any], Any]],
                     laws: Set[Tuple[Optional[str], Optional[str]]]
                     ) -> Optional[str]:
        renamed, param_order, mapping = alpha_rename(se)
        # Idempotency across sessions: the promoter's deterministic hash
        # name lets us recognise an already-promoted computation without
        # re-registering it. The promotion still gains epistemic
        # justification: every law whose verified program contains this
        # structure is folded into the record's law links.  This branch
        # runs even when fp is already in _done (see observe()), so a
        # later law's cases always record their justification.
        name = self.promoter._deterministic_name(renamed, param_order)
        link_dicts = [{"predicate": p, "semantic_id": s} for (p, s) in laws]
        if name in self.promoter.reg:
            self.promoter.merge_law_links(name, link_dicts)
            self._done.add(fp)
            self.history.append({"at": time.time(), "goal": goal,
                                 "fingerprint": fp[:32],
                                 "note": f"already registered as {name}; "
                                         f"merged {len(link_dicts)} law link(s)"})
            return None
        # NOTE: no `if fp in self._done: return None` here.  fp in _done
        # with the name absent from the registry means a previously
        # promoted structure was later unregistered (evidence-driven
        # quarantine).  Fresh cases under a new justification must be
        # allowed to re-promote it: promote() re-verifies against the new
        # examples and the new record carries only the new law links, so
        # the old (invalid) justification cannot silently grant validity.
        reg = self.promoter.reg
        derived: List[Tuple[Dict[str, Any], Any]] = []
        for args, _ in examples:
            try:
                old_bindings = {old: args[old] for old in mapping}
            except KeyError:
                return None  # parent example lacks a param: fail closed
            try:
                expected = evaluate_expr(se, old_bindings, reg)
            except Exception:
                return None  # sub-expression not evaluable here: fail closed
            derived.append(
                ({new: args[old] for old, new in mapping.items()}, expected))
        if not derived:
            return None
        # PrimitivePromoter.promote re-verifies the new primitive's own
        # calling convention against these fidelity examples and refuses on
        # any mismatch; it persists, registers, and logs provenance itself.
        # law_links records which governed laws justified this promotion so
        # evidence-driven revocation can assess its standing later.
        result = self.promoter.promote(renamed, param_order, derived,
                                      source_goal=goal, law_links=link_dicts)
        self.history.append({"at": time.time(), "goal": goal,
                             "fingerprint": fp[:32], "promoted": result,
                             "support_goals": sorted(
                                 self._support_index().get(fp, set()))})
        return result
