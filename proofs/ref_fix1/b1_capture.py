"""REF-FIX-1 battery b1: behavioral-equivalence capture for tests_for_graph.

Builds a matrix of OperatorGraphs and records the exact output of
tests_for_graph(graph, ir) for each. Run BEFORE the fix (captures the
'before' snapshot) and AFTER (must be byte-identical, since the removed
branch was unreachable).

Includes the adversarial input: a graph WITH a real lookup_value node —
the input that would have hit the dead :3010 branch had the selector been
fixed. It must behave honestly (no exception, honest fall-through output).

Usage: python3 b1_capture.py <out_json>
"""
import ast
import hashlib
import json
import sys
from pathlib import Path

TREE = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(TREE / "pylib"))

from swarm_engine.acquisition.atomic_operators import (  # noqa: E402
    OperatorGraph,
    OperatorNode,
    tests_for_graph,
)


class FakeIR:
    def __init__(self, source_text=None):
        self.provenance = {"source_text": source_text or ""}


def g_parse(fields):
    return OperatorNode("n0", "parse_fields", {"fields": fields})


def g_lookup_value():
    return OperatorGraph(
        nodes=[
            g_parse(["item", "qty"]),
            OperatorNode(
                "n1",
                "lookup_value",
                {
                    "table": {"apple|red": 1.5, "banana|yellow": 0.75},
                    "key_fields": ["item", "color"],
                    "out_field": "price",
                    "strict": True,
                },
                "n0",
            ),
        ]
    )


def g_lookup_pct_apply():
    return OperatorGraph(
        nodes=[
            g_parse(["sku", "amount"]),
            OperatorNode(
                "n1",
                "lookup_pct_apply",
                {
                    "table": {"a": 10.0, "b": 20.0},
                    "key_field": "sku",
                    "base_field": "amount",
                    "out_field": "total",
                    "strict": True,
                },
                "n0",
            ),
        ]
    )


def g_lookup_mul():
    return OperatorGraph(
        nodes=[
            g_parse(["sku", "qty"]),
            OperatorNode("n1", "lookup_mul", {"field": "qty", "factor": 2.0}, "n0"),
        ]
    )


def g_plain():
    return OperatorGraph(
        nodes=[
            g_parse(["name", "price"]),
            OperatorNode("n1", "map_mul", {"field": "price", "factor": 1.1}, "n0"),
        ]
    )


def g_format_scalar():
    return OperatorGraph(
        nodes=[
            g_parse(["total"]),
            OperatorNode("n1", "format_scalar_field", {"field": "total"}, "n0"),
        ]
    )


def g_both_lookups():
    g = g_lookup_pct_apply()
    g.nodes.append(
        OperatorNode(
            "n2",
            "lookup_value",
            {"table": {"x": 1.0}, "key_fields": ["sku"], "out_field": "v"},
            "n1",
        )
    )
    return g


def g_empty():
    return OperatorGraph(nodes=[g_parse(["a"])])


CASES = [
    ("lookup_value_node", g_lookup_value(), None),
    ("lookup_value_node_with_ir", g_lookup_value(), FakeIR("item apple red qty 3 -> 4.50")),
    (
        "lookup_value_node_worked_examples",
        g_lookup_value(),
        FakeIR("item apple qty 2 -> price 3.00\nitem banana qty 1 -> price 0.75"),
    ),
    ("lookup_pct_apply_node", g_lookup_pct_apply(), None),
    ("lookup_mul_node", g_lookup_mul(), None),
    ("plain_graph", g_plain(), None),
    ("format_scalar_field", g_format_scalar(), None),
    ("both_lookups", g_both_lookups(), None),
    ("empty_graph", g_empty(), None),
]


def main():
    out_path = Path(sys.argv[1])
    results = {}
    failures = []
    for name, graph, ir in CASES:
        try:
            src = tests_for_graph(graph, ir=ir)
        except Exception as e:  # noqa: BLE001
            failures.append(f"{name}: raised {type(e).__name__}: {e}")
            continue
        if not isinstance(src, str):
            failures.append(f"{name}: returned {type(src).__name__}, not str")
            continue
        try:
            ast.parse(src)
            compiles = True
        except SyntaxError as e:
            compiles = False
            failures.append(f"{name}: output does not compile: {e}")
        digest = hashlib.sha256(src.encode()).hexdigest()
        results[name] = {"sha256": digest, "compiles": compiles, "len": len(src)}
    out_path.write_text(json.dumps(results, indent=2, sort_keys=True))
    print(f"captured {len(results)} cases -> {out_path}")
    if failures:
        print("FAILURES:")
        for f in failures:
            print("  " + f)
        sys.exit(1)
    print("BATTERY b1_capture PASS")


if __name__ == "__main__":
    main()
