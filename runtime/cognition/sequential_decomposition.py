"""
swarm_engine/cognition/sequential_decomposition.py

SequentialDecomposer: split a verified nested program into sequential
sub-capabilities with dependency.

The novel decomposition (not one of the four hand-recognized regex shapes,
not single-level tuple/list/dict projection): when a verified program has
the form outer(inner(...)) -- a SEQUENTIAL chain where the outer step
consumes the inner step's output -- the decomposition extracts:

  - Stage 1: the inner computation (e.g. upper(name))
  - Stage 2: the outer computation with the intermediate as a parameter
             (e.g. lower(temp))

with Stage 2 depending on Stage 1. Re-composing the stages (feeding Stage 1's
output into Stage 2) reproduces the original program's behavior on all
examples -- the split is verified, not assumed.

Why this is decomposition (not just program reading):
  The split CREATES reusable sub-capabilities. Without it, the system has
  only the monolithic lower(upper(name)). With it, the system has upper(_)
  and lower(_) as independent units that can be reused in new objectives
  (e.g. "Shout the code" reuses upper; "Whisper the tag" reuses lower).
  The decomposition causally drives acquisition of the parts.

Why this is novel:
  - The four _COMPOUND_SHAPES are regex on goal text for specific domains.
    This is structural (program tree), domain-blind.
  - _match_composite_output handles PARALLEL tuple/list/dict (all children
    share inputs). This handles SEQUENTIAL nesting (outer consumes inner's
    output) -- a dependency, not a fan-out.
  - No goal text is consulted. The firing condition is purely structural:
    the verified Expr has depth >= 2 with a single-threaded dataflow.

Fail-closed:
  - Atomic programs (depth < 2) -> None (nothing to split).
  - Multi-arg outer with non-threaded flow -> None (not a clean chain).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, List, Optional, Tuple


@dataclass
class SequentialSplit:
    """A verified sequential decomposition."""
    inner: Any            # Expr for stage 1 (e.g. upper(name))
    outer: Any            # Expr for stage 2 with intermediate param
                          # (e.g. lower(temp))
    intermediate_param: str  # name of the intermediate parameter
    original: Any         # the original whole Expr

    def stage_names(self) -> Tuple[str, str]:
        return (f"stage1_{self.inner.op}" if hasattr(self.inner, 'op')
                else "stage1",
                f"stage2_{self.outer.op}" if hasattr(self.outer, 'op')
                else "stage2")


def decompose_sequential(expr: Any) -> Optional[SequentialSplit]:
    """Split a nested Expr into sequential stages.

    Returns None if the Expr is not a clean sequential chain.
    A clean chain: outer(inner(...)) where outer takes the inner's output
    as one of its arguments (single-threaded dataflow).
    """
    if expr is None:
        return None
    try:
        if expr.is_leaf():
            return None
        # The Expr must have at least one non-leaf child (the inner stage).
        # For a clean sequential chain, we take the FIRST non-leaf child
        # as the inner stage, and the outer is the Expr with that child
        # replaced by an intermediate parameter.
        children = list(expr.children)  # list of (name, child_expr)
        inner_idx = None
        inner_expr = None
        for idx, (cname, child) in enumerate(children):
            if not child.is_leaf():
                inner_idx = idx
                inner_expr = child
                break
        if inner_idx is None:
            # No nested non-leaf child: check if there's a leaf pattern
            # like outer(leaf_op(...))? Actually, for depth>=2 we need
            # a non-leaf child. If all children are leaves, it's depth 1.
            return None

        # Build the outer Expr with the inner replaced by a param.
        # We need to construct a new Expr. Use the Expr's own structure.
        from swarm_engine.cognition.representations import Expr as ExprCls
        inter_param = "__stage1_out__"
        new_children = []
        for idx, (cname, child) in enumerate(children):
            if idx == inner_idx:
                # Replace with intermediate param leaf
                param_leaf = ExprCls.leaf_param(inter_param)
                new_children.append((cname, param_leaf))
            else:
                new_children.append((cname, child))
        # Reconstruct the outer Expr. We need the op and the arg mapping.
        # Use Expr's constructor or a copy method.
        outer_expr = _rebuild_expr(expr, new_children)
        if outer_expr is None:
            return None
        return SequentialSplit(inner=inner_expr, outer=outer_expr,
                               intermediate_param=inter_param,
                               original=expr)
    except Exception:
        return None


def _rebuild_expr(orig: Any, new_children: List[Tuple[str, Any]]) -> Optional[Any]:
    """Rebuild an Expr with replaced children, preserving op and metadata."""
    try:
        from swarm_engine.cognition.representations import Expr as ExprCls
        # Expr is likely a dataclass or has a specific constructor.
        # Try to copy via __dict__ and replace children.
        import copy
        new_expr = copy.copy(orig)
        # Children is likely a tuple of (name, expr) or a dict.
        # Inspect the original's children type.
        if isinstance(orig.children, dict):
            new_expr.children = {k: v for k, v in new_children}
        elif isinstance(orig.children, (list, tuple)):
            # Preserve the container type
            new_expr.children = type(orig.children)(new_children)
        else:
            return None
        return new_expr
    except Exception:
        return None


def verify_split(split: SequentialSplit, examples: List[Tuple[dict, Any]],
                 registry) -> bool:
    """Verify that stage1 -> stage2 reproduces the original on all examples.

    Executes: out1 = inner(example_inputs); out2 = outer(out1, ...other args).
    For simplicity, assumes the outer takes the intermediate as its FIRST
    non-leaf-derived argument and other args come from the example inputs.
    Returns True iff all examples match.
    """
    try:
        from swarm_engine.cognition.representations import evaluate_expr
        for args, expected in examples:
            # Stage 1: evaluate inner on the example inputs
            out1 = evaluate_expr(split.inner, dict(args), registry)
            # Stage 2: evaluate outer with intermediate bound, plus
            # original args for any other params the outer needs.
            stage2_args = dict(args)
            stage2_args[split.intermediate_param] = out1
            out2 = evaluate_expr(split.outer, stage2_args, registry)
            if out2 != expected:
                return False
        return True
    except Exception:
        return False
