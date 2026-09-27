"""swarm_engine/acquisition/repair_synthesis_cardinality.py

Third defect-family synthesizer: silent cardinality truncation at a
constructor boundary.

Defect mechanism
----------------
A constructor builds state with ``dict(zip(<label_list_literal>, seq))``.
When ``seq`` is shorter than the label list, ``zip`` silently truncates:
the object is constructed with fewer dimensions than the contract
declares, and the corruption surfaces late (or never) downstream.

Repair mechanism
----------------
CONTRACT-PRECONDITION GUARD SYNTHESIS. Given

  (a) a construction site located by AST in the named constructor, and
  (b) an independently-established contract arity plus a citation string,

the synthesizer emits a fail-fast precondition guard inserted BEFORE the
truncation site::

    if len(<seq>) != <arity>:
        raise ValueError(
            f"<seq> must have exactly <arity> dimensions "
            f"(<label tuple>) "
            f"per <citation>; got {len(<seq>)}"
        )

Generality: the mechanism is structural (zip-truncation site + contract
arity). Labels are extracted FROM THE SOURCE AST -- never hard-coded in
the synthesizer -- and the arity comes from the contract parameter. The
same function repairs a module with different labels and a different
arity (see D3_HELDOUT_SRC / D3_HELDOUT_CONTRACT).

Refusal (raises SynthesisRefused):
  1. no ``dict(zip(<list>, <name>))`` construction site in the named
     constructor (constructor missing counts as this);
  2. the label list is not a literal list of strings (labels not
     derivable from the source);
  3. the contract arity is not a positive int.

The synthesizer never invents semantics: it does not guess labels, does
not guess arity, and does not touch any other statement.
"""
from __future__ import annotations

import ast
import re
from typing import List, Optional, Tuple


class SynthesisRefused(Exception):
    """The synthesizer declines: structural preconditions not met."""


# ---------------------------------------------------------------------------
# site location
# ---------------------------------------------------------------------------

def _find_constructor(tree: ast.Module, constructor_qualname: str
                       ) -> Optional[ast.FunctionDef]:
    """constructor_qualname looks like 'ApexProfile.__init__'."""
    if "." not in constructor_qualname:
        return None
    cls_name, meth_name = constructor_qualname.rsplit(".", 1)
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == cls_name:
            for item in node.body:
                if (isinstance(item, ast.FunctionDef)
                        and item.name == meth_name):
                    return item
    return None


def _zip_call_shape(node: ast.AST) -> Optional[Tuple[ast.AST, ast.AST]]:
    """If node is dict(zip(X, Y)), return (X, Y); else None."""
    if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
            and node.func.id == "dict" and len(node.args) == 1
            and not node.keywords):
        return None
    inner = node.args[0]
    if not (isinstance(inner, ast.Call) and isinstance(inner.func, ast.Name)
            and inner.func.id == "zip" and len(inner.args) == 2
            and not inner.keywords):
        return None
    return inner.args[0], inner.args[1]


def _find_zip_site(ctor: ast.FunctionDef
                   ) -> Tuple[Optional[ast.Assign],
                              Optional[List[str]],
                              Optional[str],
                              bool]:
    """Locate the truncation site inside the constructor.

    Returns (assign_node, labels_or_None, seq_name_or_None,
    nonliteral_labels_seen).

    A structural candidate is ``<name> = dict(zip(<list>, <name>))``.
    If the first structural candidate's label list is not all string
    literals, nonliteral_labels_seen is True so the caller can refuse
    with the labels-specific reason instead of the no-site reason.
    """
    for node in ast.walk(ctor):
        if not isinstance(node, ast.Assign):
            continue
        shape = _zip_call_shape(node.value)
        if shape is None:
            continue
        labels_node, seq_node = shape
        if not isinstance(labels_node, ast.List):
            continue
        if not isinstance(seq_node, ast.Name):
            continue
        labels: List[str] = []
        nonliteral = False
        for elt in labels_node.elts:
            if isinstance(elt, ast.Constant) and isinstance(elt.value, str):
                labels.append(elt.value)
            else:
                nonliteral = True
                break
        if nonliteral:
            return node, None, seq_node.id, True
        if not labels:
            # An empty label list is degenerate: refusing is fail-closed.
            return node, None, seq_node.id, True
        return node, labels, seq_node.id, False
    return None, None, None, False


# ---------------------------------------------------------------------------
# synthesis
# ---------------------------------------------------------------------------

def _sanitize_citation(citation: object) -> str:
    text = re.sub(r"\s+", " ", str(citation)).strip().replace('"', "'")
    return text or "contract (citation unavailable)"


def synthesize_cardinality_guard(source_text: str,
                                 constructor_qualname: str,
                                 arity: object,
                                 citation: object) -> str:
    """Insert a fail-fast contract-arity guard before the truncation site.

    Returns the repaired source text. Raises SynthesisRefused when:
      - no dict(zip(<label list>, <seq>)) site exists in the named
        constructor,
      - the label list is not a literal list of strings,
      - arity is not a positive int.
    """
    if (isinstance(arity, bool) or not isinstance(arity, int)
            or arity <= 0):
        raise SynthesisRefused(
            f"contract arity must be a positive int, got {arity!r}")
    try:
        tree = ast.parse(source_text)
    except SyntaxError as exc:
        raise SynthesisRefused(f"source does not parse: {exc}")
    ctor = _find_constructor(tree, constructor_qualname)
    if ctor is None:
        raise SynthesisRefused(
            f"constructor {constructor_qualname!r} not found in source")
    assign, labels, seq_name, nonliteral = _find_zip_site(ctor)
    if assign is None:
        raise SynthesisRefused(
            f"no dict(zip(<label list>, <seq>)) construction site in "
            f"{constructor_qualname}")
    if nonliteral or labels is None or seq_name is None:
        raise SynthesisRefused(
            f"truncation site in {constructor_qualname} has a label list "
            f"that is not a literal list of strings: labels cannot be "
            f"derived from the source -- refused")
    cite = _sanitize_citation(citation)
    labels_repr = "(" + ", ".join(repr(l) for l in labels) + ")"
    lines = source_text.splitlines(keepends=True)
    # Insert BEFORE the assignment statement, at its own indentation.
    first = lines[assign.lineno - 1]
    indent = first[:len(first) - len(first.lstrip())]
    guard = [
        f"{indent}# REMOR semantic repair (cardinality): "
        f"contract-precondition guard.\n",
        f"{indent}# Defect mechanism: dict(zip(<label list>, {seq_name})) "
        f"silently truncates a short {seq_name};\n",
        f"{indent}# malformed input is rejected explicitly (fail-closed) "
        f"instead of constructing corrupt state.\n",
        f"{indent}# Citation: {cite}\n",
        f"{indent}if len({seq_name}) != {arity}:\n",
        f"{indent}    raise ValueError(\n",
        f"{indent}        f\"{seq_name} must have exactly {arity} dimensions \"\n",
        f"{indent}        f\"{labels_repr} \"\n",
        f"{indent}        f\"per {cite}; got {{len({seq_name})}}\"\n",
        f"{indent}    )\n",
    ]
    return "".join(lines[:assign.lineno - 1] + guard
                   + lines[assign.lineno - 1:])


# ---------------------------------------------------------------------------
# defect corpus: third family (silent cardinality truncation)
# ---------------------------------------------------------------------------

D3_SRC = '''"""Apex behavioral profile module (defective revision).

Contract (independent of this implementation): the Apex profile declares
NINE behavioral dimensions. A 5-element input vector must be rejected,
not silently truncated.
"""

CONTRACT_ARITY = 9


class ApexProfile:
    """Behavioral profile over nine declared dimensions."""

    def __init__(self, tag, traits):
        self.tag = tag
        self.dims = dict(zip(
            ['drive', 'focus', 'calm', 'verve', 'grit',
             'wit', 'poise', 'zeal', 'tempo'],
            traits))
        self.ready = len(self.dims) == CONTRACT_ARITY

    def summary(self):
        return {"tag": self.tag, "dims": dict(self.dims),
                "ready": self.ready}


def check_traits(traits):
    """Review entrypoint: construct and report the constructed arity."""
    prof = ApexProfile("probe", traits)
    return len(prof.dims)
'''

D3_CONTRACT = {
    "arity": 9,
    "citation": "APEX-9 profile contract: 9 declared dimensions "
                "(module docstring; CONTRACT_ARITY)",
    "constructor": "ApexProfile.__init__",
    "entrypoint": "check_traits",
    "family": "cardinality-truncation",
}

# Held-out module: different labels, different arity (4), different
# constructor and entrypoint names. The SAME synthesizer must repair it.
D3_HELDOUT_SRC = '''"""Quad-state navigation module (defective revision).

Contract (independent of this implementation): a quad state declares
FOUR cardinal levels. A 3-element input vector must be rejected,
not silently truncated.
"""


class QuadState:
    """Navigation state over four declared cardinal levels."""

    def __init__(self, name, levels):
        self.name = name
        self.levels = dict(zip(['north', 'east', 'south', 'west'], levels))

    def report(self):
        return {"name": self.name, "levels": dict(self.levels)}


def check_levels(levels):
    """Review entrypoint: construct and report the constructed arity."""
    st = QuadState("probe", levels)
    return len(st.levels)
'''

D3_HELDOUT_CONTRACT = {
    "arity": 4,
    "citation": "QUAD-4 navigation contract: 4 declared cardinal levels "
                "(module docstring)",
    "constructor": "QuadState.__init__",
    "entrypoint": "check_levels",
    "family": "cardinality-truncation",
}
