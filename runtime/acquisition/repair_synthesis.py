"""M+29.03 — Bounded example-driven repair synthesis for defective source files.

Given a failing process result (stderr/test path) and upstream source paths,
extract assert-based behavioral examples from the test artifact and search a
small, generic expression space for a function body that satisfies them.

This is NOT general-purpose programming and does not hardcode solutions for
named benchmarks. It only:
  1. Parses assert forms already present in project test files
  2. Tries generic operator templates against those examples
  3. Returns candidate source text when all extracted examples pass

Provenance is explicit so recovery can distinguish synthesized vs registered.
"""
from __future__ import annotations

import ast
import operator
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple


# Generic binary ops — not task-specific; ordered by commonality for search.
_BINARY_OPS: List[Tuple[str, Callable[[Any, Any], Any]]] = [
    ("+", operator.add),
    ("-", operator.sub),
    ("*", operator.mul),
    ("//", operator.floordiv),
    ("%", operator.mod),
    ("**", operator.pow),
]

_ASSERT_RE = re.compile(
    r"assert\s+(\w+)\s*\(\s*([^)]+)\s*\)\s*==\s*([^\n;#]+)",
    re.IGNORECASE,
)
_DEF_RE = re.compile(
    r"def\s+(\w+)\s*\(([^)]*)\)\s*:",
)


@dataclass
class Example:
    func: str
    args: List[Any]
    expected: Any


@dataclass
class RepairCandidate:
    path: str
    content: str
    func_name: str
    expression: str
    examples_satisfied: int
    provenance: Dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "path": self.path,
            "content": self.content,
            "func_name": self.func_name,
            "expression": self.expression,
            "examples_satisfied": self.examples_satisfied,
            "provenance": dict(self.provenance),
        }


def _literal(tok: str) -> Any:
    tok = tok.strip()
    try:
        return ast.literal_eval(tok)
    except Exception:
        return tok


def extract_examples_from_test(test_text: str) -> List[Example]:
    """Pull behavioral examples from assert statements in a test file."""
    examples: List[Example] = []
    for m in _ASSERT_RE.finditer(test_text or ""):
        func, args_s, exp_s = m.group(1), m.group(2), m.group(3)
        args = [_literal(a) for a in args_s.split(",")]
        expected = _literal(exp_s)
        examples.append(Example(func=func, args=args, expected=expected))
    return examples


def _parse_def(source: str) -> Optional[Tuple[str, List[str], str]]:
    """Return (name, param_names, full_def_prefix_indent) from source."""
    m = _DEF_RE.search(source or "")
    if not m:
        return None
    name = m.group(1)
    params = [p.strip().split("=")[0].strip() for p in m.group(2).split(",") if p.strip()]
    return name, params, m.group(0)


def _eval_examples(fn: Callable, examples: List[Example], func_name: str) -> bool:
    for ex in examples:
        if ex.func != func_name:
            continue
        try:
            got = fn(*ex.args)
        except Exception:
            return False
        if got != ex.expected:
            return False
    return True


def synthesize_binary_body(
    source_text: str,
    examples: List[Example],
) -> Optional[Tuple[str, str, str]]:
    """Search generic binary operator templates against extracted examples.

    Returns (func_name, expression, full_file_content) or None.
    """
    parsed = _parse_def(source_text)
    if not parsed:
        return None
    name, params, _ = parsed
    relevant = [e for e in examples if e.func == name]
    if not relevant:
        return None
    if len(params) != 2:
        # Only binary synthesis in this bounded substrate
        return None
    a_name, b_name = params[0], params[1]
    for op_sym, op_fn in _BINARY_OPS:
        def make_fn(op=op_fn):
            return lambda x, y: op(x, y)
        if _eval_examples(make_fn(), relevant, name):
            expr = f"{a_name} {op_sym} {b_name}"
            body = f"def {name}({a_name}, {b_name}): return {expr}\n"
            return name, expr, body
    return None


def synthesize_repair_for_paths(
    source_path: str,
    test_paths: List[str],
    examples_batch_id: Optional[str] = None,
) -> Optional[RepairCandidate]:
    """Synthesize a repair for source_path using asserts from test_paths.

    O19: when the caller knows the driver example batch these test asserts
    were recorded from, pass it and it is cited in the candidate's
    provenance; otherwise provenance records the test-assert source
    honestly (no driver batch is invented).
    """
    sp = Path(source_path)
    if not sp.exists():
        return None
    source_text = sp.read_text()
    examples: List[Example] = []
    for tp in test_paths:
        p = Path(tp)
        if p.exists():
            examples.extend(extract_examples_from_test(p.read_text()))
    if not examples:
        return None
    result = synthesize_binary_body(source_text, examples)
    if result is None:
        return None
    name, expr, content = result
    provenance = {
        "origin": "example_driven_repair_synthesis",
        "source": "test_assert_examples",
        "ops_searched": [op for op, _ in _BINARY_OPS],
        "expression": expr,
        "examples": [
            {"func": e.func, "args": e.args, "expected": e.expected}
            for e in examples if e.func == name
        ],
    }
    # O19 citation (only when the caller actually supplies one).
    if examples_batch_id is not None:
        from swarm_engine.governance.examples_provenance import cite_batch
        provenance["driver_examples_citation"] = cite_batch(
            examples_batch_id)
    return RepairCandidate(
        path=str(sp),
        content=content,
        func_name=name,
        expression=expr,
        examples_satisfied=len([e for e in examples if e.func == name]),
        provenance=provenance,
    )
