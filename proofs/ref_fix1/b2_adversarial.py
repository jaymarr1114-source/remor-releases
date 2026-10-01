"""REF-FIX-1 battery b2: adversarial + caller-contract evidence.

1. Adversarial: inputs that would have reached the removed dead branch
   (graphs containing lookup_value nodes with hostile/edge-case params)
   must not raise NameError or any other exception — they fall through
   honestly to the live paths.
2. Caller contract: synthesize_from_operator_graph writes the returned
   string as tests/test_app.py — prove the file the caller would write is
   a valid, importable-structure test module.
"""
import ast
import sys
import tempfile
from pathlib import Path

TREE = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(TREE / "pylib"))
sys.path.insert(0, str(Path(__file__).parent))

from swarm_engine.acquisition.atomic_operators import (  # noqa: E402
    OperatorGraph,
    OperatorNode,
    tests_for_graph,
)
from b1_capture import FakeIR  # noqa: E402


def parse(fields):
    return OperatorNode("n0", "parse_fields", {"fields": fields})


def check(name, graph, ir=None):
    src = tests_for_graph(graph, ir=ir)  # must not raise
    assert isinstance(src, str), f"{name}: not a str"
    ast.parse(src)  # must compile
    # The removed branch emitted pytest-style tests calling bare run();
    # none of the honest outputs may contain its distinctive marker.
    assert "def test_lookup_value():" not in src, f"{name}: dead-branch output leaked"
    assert "def test_lookup_unknown():" not in src, f"{name}: dead-branch output leaked"
    print(f"PASS {name}: no exception, compiles, honest fall-through ({len(src)} bytes)")
    return src


def main():
    # A1: lookup_value with a full table — the exact former-NameError input.
    check(
        "a1_lookup_value_full_table",
        OperatorGraph(
            nodes=[
                parse(["item", "qty"]),
                OperatorNode(
                    "n1", "lookup_value",
                    {"table": {"apple|red": 1.5}, "key_fields": ["item", "color"],
                     "out_field": "price", "strict": True}, "n0"),
            ]
        ),
    )
    # A2: lookup_value with EMPTY table and missing key_fields.
    check(
        "a2_lookup_value_empty_table",
        OperatorGraph(
            nodes=[parse(["item"]),
                   OperatorNode("n1", "lookup_value",
                                {"table": {}, "out_field": "price"}, "n0")]
        ),
    )
    # A3: lookup_value node present AND lookup_mul present (selector used to
    # pick lookup_mul; dead branch checked lu.op == "lookup_value").
    check(
        "a3_lookup_value_plus_lookup_mul",
        OperatorGraph(
            nodes=[
                parse(["sku", "qty"]),
                OperatorNode("n1", "lookup_mul", {"field": "qty", "factor": 2.0}, "n0"),
                OperatorNode("n2", "lookup_value",
                             {"table": {"x": 9.0}, "key_fields": ["sku"],
                              "out_field": "v"}, "n1"),
            ]
        ),
    )
    # A4: lookup_value with bizarre params (non-string keys, None table).
    check(
        "a4_lookup_value_hostile_params",
        OperatorGraph(
            nodes=[parse(["a", "b"]),
                   OperatorNode("n1", "lookup_value",
                                {"table": None, "key_fields": None,
                                 "out_field": "v", "strict": "yes"}, "n0")]
        ),
    )
    # A5: lookup_value node with worked-examples IR — the M+29.20 live path
    # must still be the one that serves lookup_value graphs when examples exist.
    src = check(
        "a5_lookup_value_with_worked_examples",
        OperatorGraph(
            nodes=[
                parse(["item", "qty"]),
                OperatorNode("n1", "lookup_value",
                             {"table": {"apple": 1.5}, "key_fields": ["item"],
                              "out_field": "price"}, "n0"),
            ]
        ),
        ir=FakeIR("item apple qty 2 -> price 3.00"),
    )

    # Caller contract: the caller writes this string as tests/test_app.py.
    # Prove the written file is a structurally valid test module.
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "tests" / "test_app.py"
        p.parent.mkdir(parents=True)
        p.write_text(src)
        tree = ast.parse(p.read_text())
        fn_names = [n.name for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)]
        assert any(n.startswith("test_") for n in fn_names), "no test functions in caller-written file"
        print(f"PASS caller_contract: tests/test_app.py written, test fns={sorted(fn_names)}")

    print("BATTERY b2_adversarial PASS")


if __name__ == "__main__":
    main()
