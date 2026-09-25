"""
swarm_engine/cognition/nested_decomposition.py

NestedDecomposer: recursive structural decomposition for nested composite
outputs.

The novel decomposition (not one of the four hand-recognized regex shapes,
not single-level tuple/list/dict): when a goal's worked examples uniformly
produce a NESTED composite output (dict containing dict, list containing
dict, etc.), the requirement splits RECURSIVELY into one sub-requirement
per LEAF element, mirroring the output structure.

Example:
  Output: {"shouted": "AB", "details": {"quiet": "cd"}}
  Splits into:
    - Leaf 1: path ("shouted",) -> upper(name)
    - Leaf 2: path ("details", "quiet") -> lower(code)
  The parent re-assembles by placing leaf outputs at their paths.

Why this is novel:
  - The four _COMPOUND_SHAPES are regex on goal text. This is structural
    (example outputs), goal-text-blind.
  - _match_composite_output handles SINGLE-LEVEL tuple/list/dict and
    explicitly rejects nested ("Single-level only" -- a stated bound).
    This handles ARBITRARY NESTING recursively -- a general mechanism,
    not a new shape.
  - No per-objective branch, no keyword, no domain knowledge.

Why this drives acquisition:
  Without the split, synthesis must find a program producing the entire
  nested structure (hard: requires dict construction + nesting). With the
  split, each leaf is a SIMPLE synthesis problem (e.g. upper(name)).
  The decomposition makes the unacquirable acquirable -- it causally
  enables sub-capability acquisition.

Fail-closed (returns None):
  - Fewer than 2 examples, or non-uniform output structure.
  - Atomic outputs (nothing to split).
  - Mixed types at the same path, or ragged structures.
  - Non-dict inputs, or inconsistent input keys.

The output is a NestedSplit tree. Leaf nodes contain the path and the
projected examples for that leaf. Internal nodes mirror the structure.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple, Union


@dataclass
class LeafSubgoal:
    """A leaf sub-requirement: synthesize a program for this output path."""
    path: Tuple[Union[str, int], ...]  # e.g. ("details", "quiet")
    examples: List[Tuple[Dict[str, Any], Any]]  # projected (inputs, leaf_output)
    param_names: Tuple[str, ...]

    def path_str(self) -> str:
        return ".".join(str(p) for p in self.path)


@dataclass
class NestedSplit:
    """The decomposition tree. Leaves are LeafSubgoals; internal structure
    mirrors the output nesting for re-assembly."""
    leaves: List[LeafSubgoal]
    # Structure template: nested dict/list/tuple with LeafSubgoal refs at leaves.
    # For re-assembly, we store the output skeleton.
    skeleton: Any  # nested structure with None at leaves, for shape reference

    def leaf_count(self) -> int:
        return len(self.leaves)


def _output_shape(val: Any) -> Any:
    """Structural shape of a value: type + keys/length, values replaced by shape."""
    if isinstance(val, dict):
        return ("dict", tuple(sorted(val.keys())),
                tuple(_output_shape(val[k]) for k in sorted(val.keys())))
    if isinstance(val, (list, tuple)):
        kind = "list" if isinstance(val, list) else "tuple"
        return (kind, len(val), tuple(_output_shape(v) for v in val))
    return ("atom",)


def _uniform_shape(examples: List[Tuple[Dict[str, Any], Any]]) -> Optional[Any]:
    """Check all outputs have the same nested shape. Return the shape or None."""
    if len(examples) < 2:
        return None
    shapes = [_output_shape(out) for _, out in examples]
    if len(set(map(str, shapes))) != 1:
        return None
    # Must be composite and nested (contain at least one composite child)
    shape = shapes[0]
    if shape[0] == "atom":
        return None
    # Check for nesting: a composite with a composite direct child is nested
    # (depth >= 2). Single-level composites (all children atoms) return False
    # -- the existing _match_composite_output handles those; this is the
    # novel recursive case.
    def _has_nested(s):
        if s[0] == "atom":
            return False
        # s is dict/list/tuple; child shapes are in s[2]
        for c in s[2]:
            if c[0] != "atom":
                return True
        return False
    if not _has_nested(shape):
        return None  # single-level; existing code handles it
    return shape


def _collect_leaves(output: Any, path: Tuple,
                    inputs: Dict[str, Any],
                    leaves: Dict[Tuple, List[Tuple[Dict, Any]]]) -> None:
    """Recursively collect (inputs, leaf_value) by path."""
    if isinstance(output, dict):
        for k in sorted(output.keys()):
            _collect_leaves(output[k], path + (k,), inputs, leaves)
    elif isinstance(output, (list, tuple)):
        for i, v in enumerate(output):
            _collect_leaves(v, path + (i,), inputs, leaves)
    else:
        leaves.setdefault(path, []).append((inputs, output))


def decompose_nested(examples: List[Tuple[Dict[str, Any], Any]],
                     param_names: Tuple[str, ...]) -> Optional[NestedSplit]:
    """Decompose examples with nested composite outputs into leaf subgoals.

    Returns None (fail-closed) if the outputs are not uniformly nested.
    """
    if not examples or len(examples) < 2:
        return None
    # Check inputs are dicts with consistent keys
    input_keys = [tuple(sorted(args.keys())) for args, _ in examples]
    if len(set(input_keys)) != 1:
        return None
    shape = _uniform_shape(examples)
    if shape is None:
        return None

    # Collect leaves by path
    leaves_by_path: Dict[Tuple, List[Tuple[Dict, Any]]] = {}
    for args, out in examples:
        _collect_leaves(out, (), dict(args), leaves_by_path)

    # Build LeafSubgoals
    leaves = []
    for path in sorted(leaves_by_path.keys()):
        proj_examples = leaves_by_path[path]
        leaves.append(LeafSubgoal(path=path, examples=proj_examples,
                                  param_names=param_names))
    # Build skeleton from the first output (structure with None at leaves)
    def _skeleton(val):
        if isinstance(val, dict):
            return {k: _skeleton(val[k]) for k in sorted(val.keys())}
        if isinstance(val, list):
            return [_skeleton(v) for v in val]
        if isinstance(val, tuple):
            return tuple(_skeleton(v) for v in val)
        return None
    skeleton = _skeleton(examples[0][1])
    return NestedSplit(leaves=leaves, skeleton=skeleton)


def reassemble(split: NestedSplit,
               leaf_outputs: Dict[Tuple, Any]) -> Any:
    """Re-assemble the nested output from leaf outputs by path.

    leaf_outputs: path -> computed leaf value.
    Returns the nested structure with leaves filled in.
    """
    def _fill(skel, path):
        if isinstance(skel, dict):
            return {k: _fill(skel[k], path + (k,)) for k in skel}
        if isinstance(skel, list):
            return [_fill(v, path + (i,)) for i, v in enumerate(skel)]
        if isinstance(skel, tuple):
            return tuple(_fill(v, path + (i,)) for i, v in enumerate(skel))
        # Leaf: look up by path
        if path not in leaf_outputs:
            raise KeyError(f"missing leaf output for path {path}")
        return leaf_outputs[path]
    return _fill(split.skeleton, ())
