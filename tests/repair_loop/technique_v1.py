"""T1: example-driven binary-operator repair for single-function modules.

Repair technique discovered by Agent A (Track 1, 2026-09-25).

Contract:
  input:  source_text (str) -- Python module source (single function)
          test_text (str)   -- test file text with `assert f(args) == expected`
  output: JSON string:
            {"repaired_source": str, "target": str, "expression": str,
             "operator": str}
          or {"error": str} when no repair is found.

Method: parse assert examples from the test text, take the single
function defined in the source, and search the binary-operator grammar
for one whose body satisfies every example. Self-contained (stdlib
only) so it executes under the IndependentValidator's subprocess
isolation.
"""
import ast
import json
import re

_OPS = ["+", "-", "*", "//", "%", "**"]

_ASSERT_RE = re.compile(
    r"^\s*assert\s+([A-Za-z_]\w*)\s*\(([^)]*)\)\s*==\s*([^\n#;]+)",
    re.MULTILINE)


def _parse_literal(tok):
    tok = tok.strip()
    try:
        return ast.literal_eval(tok)
    except Exception:
        raise ValueError("non-literal example token: %r" % (tok,))


def _extract_examples(test_text):
    by_func = {}
    for m in _ASSERT_RE.finditer(test_text or ""):
        name, argstr, expstr = m.group(1), m.group(2), m.group(3)
        args = [_parse_literal(t) for t in argstr.split(",") if t.strip()]
        expected = _parse_literal(expstr)
        by_func.setdefault(name, []).append((args, expected))
    return by_func


def _single_function(source_text):
    tree = ast.parse(source_text)
    funcs = [n for n in tree.body if isinstance(n, ast.FunctionDef)]
    if len(funcs) != 1:
        raise ValueError(
            "T1 handles single-function modules, found %d" % len(funcs))
    return funcs[0]


def _candidate_source(fn_name, param_names, op_sym):
    params = ", ".join(param_names)
    return ("def %s(%s):\n    return %s %s %s\n"
            % (fn_name, params, param_names[0], op_sym, param_names[1]))


def _satisfies(body_src, fn_name, examples):
    ns = {}
    exec(compile(body_src, "<t1>", "exec"), {"__builtins__": {}}, ns)
    fn = ns[fn_name]
    for args, expected in examples:
        try:
            got = fn(*args)
        except Exception:
            return False
        if got != expected:
            return False
    return True


def repair_procedure(source_text, test_text):
    try:
        examples_by_func = _extract_examples(test_text)
        if not examples_by_func:
            return json.dumps({"error": "no examples found in test text"})
        fn_node = _single_function(source_text)
        fn_name = fn_node.name
        examples = examples_by_func.get(fn_name)
        if not examples:
            return json.dumps(
                {"error": "no examples for function %s" % fn_name})
        param_names = [a.arg for a in fn_node.args.args]
        if len(param_names) != 2:
            return json.dumps(
                {"error": "T1 handles two-parameter functions"})
        for op_sym in _OPS:
            body = _candidate_source(fn_name, param_names, op_sym)
            if _satisfies(body, fn_name, examples):
                return json.dumps({
                    "repaired_source": body,
                    "target": fn_name,
                    "expression": "%s %s %s"
                    % (param_names[0], op_sym, param_names[1]),
                    "operator": op_sym,
                })
        return json.dumps({"error": "no operator satisfies all examples"})
    except Exception as exc:
        return json.dumps({"error": "%s: %s" % (type(exc).__name__, exc)})
