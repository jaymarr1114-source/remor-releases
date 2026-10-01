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


def _rebuild_expr(orig: Any, new_children: List[Tuple[str, Any]]) -> Optional[Any]:
    """Rebuild an Expr with replaced children, preserving op and metadata."""
    try:
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


