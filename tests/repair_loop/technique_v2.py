"""T2: diagnosis-driven targeted binary-operator repair.

Repair technique discovered by Agent B (Track 1, 2026-09-25) as an
ADAPTATION of T1 to a materially different defect shape.

T1 assumes a single-function module and replaces the whole file. That
fails for multi-function modules: it would delete every other
function. T2 instead:
  1. executes each function defined in the source against its own
     examples to identify the failing function(s) (diagnosis-driven
     targeting -- the defective function is found, not assumed);
  2. searches the binary-operator grammar for each failing function;
  3. surgically replaces ONLY the failing function(s) in the source by
     AST line span, preserving every other function byte-for-byte.

Contract: same as T1 --
  input:  source_text (str), test_text (str)
  output: JSON string {"repaired_source": str, "target": str,
                       "targets": [str], "repairs": [...]}
          or {"error": str}.

Self-contained (stdlib only) for subprocess-isolated verification.
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


def _exec_source(source_text, tag):
    ns = {}
    exec(compile(source_text, tag, "exec"), {"__builtins__": {}}, ns)
    return ns


def _satisfies_fn(fn, examples):
    for args, expected in examples:
        try:
            got = fn(*args)
        except Exception:
            return False
        if got != expected:
            return False
    return True


def _candidate_source(fn_name, param_names, op_sym):
    params = ", ".join(param_names)
    return ("def %s(%s):\n    return %s %s %s\n"
            % (fn_name, params, param_names[0], op_sym, param_names[1]))


def repair_procedure(source_text, test_text):
    try:
        examples_by_func = _extract_examples(test_text)
        if not examples_by_func:
            return json.dumps({"error": "no examples found in test text"})
        tree = ast.parse(source_text)
        funcs = [n for n in tree.body if isinstance(n, ast.FunctionDef)]
        if not funcs:
            return json.dumps({"error": "no functions found in source"})
        try:
            ns = _exec_source(source_text, "<t2>")
        except Exception as exc:
            return json.dumps({"error": "source does not execute: %s" % exc})
        # --- diagnosis: find the failing function(s) ---
        failing = []
        for fn_node in funcs:
            fn_name = fn_node.name
            examples = examples_by_func.get(fn_name, [])
            if not examples:
                continue
            fn = ns.get(fn_name)
            if fn is None or not _satisfies_fn(fn, examples):
                failing.append(fn_node)
        if not failing:
            return json.dumps(
                {"error": "no failing function identified"})
        # --- repair each failing function, replacing by line span ---
        lines = source_text.splitlines(keepends=True)
        repairs = []
        for fn_node in sorted(failing, key=lambda n: n.lineno, reverse=True):
            fn_name = fn_node.name
            param_names = [a.arg for a in fn_node.args.args]
            if len(param_names) != 2:
                return json.dumps(
                    {"error": "T2 handles two-parameter functions (%s)"
                     % fn_name})
            examples = examples_by_func[fn_name]
            found = None
            for op_sym in _OPS:
                body_src = _candidate_source(fn_name, param_names, op_sym)
                tns = _exec_source(body_src, "<t2c>")
                if _satisfies_fn(tns[fn_name], examples):
                    found = (op_sym, body_src)
                    break
            if found is None:
                return json.dumps(
                    {"error": "no operator satisfies %s" % fn_name})
            op_sym, body_src = found
            start = fn_node.lineno - 1      # 1-based -> 0-based
            end = fn_node.end_lineno        # slice end is exclusive
            lines[start:end] = [body_src]
            repairs.append({"target": fn_name, "operator": op_sym,
                            "expression": "%s %s %s"
                            % (param_names[0], op_sym, param_names[1])})
        repaired_source = "".join(lines)
        ast.parse(repaired_source)  # sanity: must still parse
        targets = [r["target"] for r in repairs]
        return json.dumps({"repaired_source": repaired_source,
                           "target": targets[0],
                           "targets": targets,
                           "repairs": repairs})
    except Exception as exc:
        return json.dumps({"error": "%s: %s" % (type(exc).__name__, exc)})
