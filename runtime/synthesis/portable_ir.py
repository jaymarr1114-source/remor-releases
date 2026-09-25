"""M+29.25 -- Portable Implementation Representation (IR) substrate.

Extracts a genuine, structured, JSON-serializable intermediate
representation from a primitive's real Python source (via ast.parse,
never eval/exec, never pickle), for the naturally representable class
of primitives: single-expression functions (lambdas or simple
`def name(...): return <expr>` functions) built from a small, explicit
whitelist of arithmetic/comparison/boolean/ternary/call operations.

This IR is itself the persisted, portable artifact -- not the lambda
object, not reconstructed Python source text, not a pickle. A separate,
small, deterministic tree-walking interpreter (interpret_portable_ir)
executes it, dispatching each node to real Python operators/builtins
via a fixed dictionary -- never via eval()/exec() of any string.

Deliberately out of scope for this vertical slice (a genuine, honest
boundary, not an oversight): multi-statement function bodies (if/raise/
multiple statements), loops, closures over external state, and calls to
anything outside the explicit whitelist below. extract_portable_ir
returns None for any of these rather than silently producing a partial
or incorrect IR.
"""
from __future__ import annotations

import ast
import inspect
import math
import textwrap
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Set

# ---------------------------------------------------------------------------
# The whitelist. This is the ENTIRE set of operations the portable IR can
# ever reference -- extraction rejects anything outside it, and the
# interpreter below has no other dispatch path. Adding to this list is a
# deliberate, auditable act, not something extraction can do implicitly.
# ---------------------------------------------------------------------------

_BINOP_DISPATCH: Dict[type, Callable[[Any, Any], Any]] = {
    ast.Add: lambda a, b: a + b,
    ast.Sub: lambda a, b: a - b,
    ast.Mult: lambda a, b: a * b,
    ast.Div: lambda a, b: a / b,
    ast.Mod: lambda a, b: a % b,
    ast.Pow: lambda a, b: a ** b,
    ast.FloorDiv: lambda a, b: a // b,
}
_BINOP_NAMES: Dict[type, str] = {
    ast.Add: "add", ast.Sub: "sub", ast.Mult: "mult", ast.Div: "div",
    ast.Mod: "mod", ast.Pow: "pow", ast.FloorDiv: "floordiv",
}
_NAME_TO_BINOP = {v: k for k, v in _BINOP_NAMES.items()}

_UNARYOP_DISPATCH: Dict[type, Callable[[Any], Any]] = {
    ast.USub: lambda x: -x,
    ast.UAdd: lambda x: +x,
    ast.Not: lambda x: not x,
}
_UNARYOP_NAMES: Dict[type, str] = {
    ast.USub: "neg", ast.UAdd: "pos", ast.Not: "not",
}
_NAME_TO_UNARYOP = {v: k for k, v in _UNARYOP_NAMES.items()}

_CMP_DISPATCH: Dict[type, Callable[[Any, Any], bool]] = {
    ast.Lt: lambda a, b: a < b, ast.Gt: lambda a, b: a > b,
    ast.LtE: lambda a, b: a <= b, ast.GtE: lambda a, b: a >= b,
    ast.Eq: lambda a, b: a == b, ast.NotEq: lambda a, b: a != b,
    ast.In: lambda a, b: a in b, ast.NotIn: lambda a, b: a not in b,
}
_CMP_NAMES: Dict[type, str] = {
    ast.Lt: "lt", ast.Gt: "gt", ast.LtE: "le", ast.GtE: "ge",
    ast.Eq: "eq", ast.NotEq: "ne", ast.In: "in", ast.NotIn: "not_in",
}
_NAME_TO_CMP = {v: k for k, v in _CMP_NAMES.items()}

_BOOLOP_DISPATCH: Dict[type, Callable[[list], Any]] = {
    ast.And: lambda vals: all(vals) and vals[-1] if all(vals) else next(
        (v for v in vals if not v)),
    ast.Or: lambda vals: next((v for v in vals if v), vals[-1] if vals else None),
}

# Whitelisted CALL targets: bare-name builtins, and math.<name> attribute
# calls. Each maps a stable IR name to the real Python callable used only
# at interpretation time, never at extraction time and never via eval.
_CALL_WHITELIST: Dict[str, Callable[..., Any]] = {
    "max": max, "min": min, "abs": abs, "round": round,
    "bool": bool, "int": int, "float": float, "str": str, "len": len,
    "all": all, "any": any, "sum": sum,
    "math.sqrt": math.sqrt, "math.log": math.log, "math.exp": math.exp,
    "math.sin": math.sin, "math.cos": math.cos, "math.tan": math.tan,
    "math.atan2": math.atan2, "math.hypot": math.hypot,
    "math.floor": math.floor, "math.ceil": math.ceil,
}


@dataclass
class ExtractionResult:
    ir: Optional[Dict[str, Any]]
    param_names: List[str]
    dependencies: Set[str] = field(default_factory=set)
    error: str = ""


def _call_target_name(node: ast.expr) -> Optional[str]:
    """Resolve a Call's func expression to a whitelist key, or None."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
        return f"{node.value.id}.{node.attr}"
    return None


def _walk(node: ast.expr, param_names: Set[str],
           known_primitive_names: Set[str],
           dependencies: Set[str]) -> Optional[Dict[str, Any]]:
    """Recursively convert one AST expression node into an IR dict, or
    return None the moment anything outside the whitelist is found --
    extraction fails closed, it never guesses or degrades gracefully
    into an incorrect partial IR."""
    if isinstance(node, ast.BinOp) and type(node.op) in _BINOP_NAMES:
        left = _walk(node.left, param_names, known_primitive_names, dependencies)
        right = _walk(node.right, param_names, known_primitive_names, dependencies)
        if left is None or right is None:
            return None
        return {"kind": "binop", "op": _BINOP_NAMES[type(node.op)],
                "left": left, "right": right}

    if isinstance(node, ast.UnaryOp) and type(node.op) in _UNARYOP_NAMES:
        operand = _walk(node.operand, param_names, known_primitive_names, dependencies)
        if operand is None:
            return None
        return {"kind": "unaryop", "op": _UNARYOP_NAMES[type(node.op)],
                "operand": operand}

    if isinstance(node, ast.Compare) and len(node.ops) == 1 and len(node.comparators) == 1:
        if type(node.ops[0]) not in _CMP_NAMES:
            return None
        left = _walk(node.left, param_names, known_primitive_names, dependencies)
        right = _walk(node.comparators[0], param_names, known_primitive_names, dependencies)
        if left is None or right is None:
            return None
        return {"kind": "compare", "op": _CMP_NAMES[type(node.ops[0])],
                "left": left, "right": right}

    if isinstance(node, ast.BoolOp) and type(node.op) in (ast.And, ast.Or):
        vals = [_walk(v, param_names, known_primitive_names, dependencies)
                for v in node.values]
        if any(v is None for v in vals):
            return None
        return {"kind": "boolop",
                "op": "and" if isinstance(node.op, ast.And) else "or",
                "values": vals}

    if isinstance(node, ast.IfExp):
        test = _walk(node.test, param_names, known_primitive_names, dependencies)
        body = _walk(node.body, param_names, known_primitive_names, dependencies)
        orelse = _walk(node.orelse, param_names, known_primitive_names, dependencies)
        if test is None or body is None or orelse is None:
            return None
        return {"kind": "ifexp", "test": test, "body": body, "orelse": orelse}

    if isinstance(node, ast.Call) and not node.keywords:
        target = _call_target_name(node.func)
        args = [_walk(a, param_names, known_primitive_names, dependencies)
                for a in node.args]
        if any(a is None for a in args):
            return None
        if target in _CALL_WHITELIST:
            return {"kind": "call", "func": target, "args": args}
        if target in known_primitive_names:
            # A genuine dependency on another registered primitive --
            # recorded, not inlined or guessed at. The caller is
            # responsible for recursively extracting/persisting it.
            dependencies.add(target)
            return {"kind": "prim_call", "func": target, "args": args}
        return None

    if isinstance(node, ast.Name):
        if node.id in param_names:
            return {"kind": "param", "name": node.id}
        return None

    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float, str, bool, type(None))):
        return {"kind": "const", "value": node.value}

    return None


def classify_portability(fn: Callable[..., Any], effects: tuple,
                           known_primitive_names: Optional[Set[str]] = None) -> Dict[str, Any]:
    """M+29.25 governance classification. Never claims PORTABLE merely
    because the expression shape happens to be extractable -- a
    non-pure primitive (any effect other than PURE) is always
    NON_PORTABLE regardless of its body's shape, since portable
    execution must be safe to run in an isolated environment with no
    REMOR governance/effect-authorization context available at all.
    """
    is_pure = bool(effects) and all(getattr(e, "name", None) == "PURE" for e in effects)
    if not is_pure:
        return {"status": "NON_PORTABLE", "reason": "primitive has non-PURE effects"}
    result = extract_portable_ir(fn, known_primitive_names)
    if result.ir is None:
        return {"status": "UNSUPPORTED", "reason": result.error}
    if result.dependencies:
        return {"status": "UNRESOLVED_DEPENDENCY",
                "reason": f"depends on: {sorted(result.dependencies)}",
                "ir": result.ir, "param_names": result.param_names,
                "dependencies": sorted(result.dependencies)}
    return {"status": "PORTABLE", "ir": result.ir, "param_names": result.param_names}


def verify_artifact_integrity(artifact: Dict[str, Any]) -> bool:
    """A minimal, explicit structural check -- rejects a corrupted or
    malformed artifact before interpretation is ever attempted, rather
    than letting interpret_portable_ir fail partway through or, worse,
    silently produce a wrong result. Recursively validates every IR
    node kind against the same whitelist extraction itself enforces, so
    a corrupted artifact (unknown kind, unknown op name, unknown call
    target) is REJECTED_UNSAFE rather than executed.
    """
    def check(node: Any) -> bool:
        if not isinstance(node, dict) or "kind" not in node:
            return False
        kind = node["kind"]
        if kind == "param":
            return isinstance(node.get("name"), str)
        if kind == "const":
            return "value" in node
        if kind == "binop":
            return (node.get("op") in _NAME_TO_BINOP
                    and check(node.get("left")) and check(node.get("right")))
        if kind == "unaryop":
            return node.get("op") in _NAME_TO_UNARYOP and check(node.get("operand"))
        if kind == "compare":
            return (node.get("op") in _NAME_TO_CMP
                    and check(node.get("left")) and check(node.get("right")))
        if kind == "boolop":
            return (node.get("op") in ("and", "or")
                    and isinstance(node.get("values"), list)
                    and all(check(v) for v in node["values"]))
        if kind == "ifexp":
            return (check(node.get("test")) and check(node.get("body"))
                    and check(node.get("orelse")))
        if kind == "guard_raise":
            return (check(node.get("test")) and check(node.get("then"))
                    and node.get("exception_type") in _EXCEPTION_WHITELIST
                    and isinstance(node.get("message"), str))
        if kind == "call":
            return (node.get("func") in _CALL_WHITELIST
                    and isinstance(node.get("args"), list)
                    and all(check(a) for a in node["args"]))
        if kind == "prim_call":
            return (isinstance(node.get("func"), str)
                    and isinstance(node.get("args"), list)
                    and all(check(a) for a in node["args"]))
        return False

    if not isinstance(artifact, dict):
        return False
    if "sub_plan" in artifact:
        return verify_full_artifact_integrity(artifact["sub_plan"])
    if "ir" not in artifact or "param_names" not in artifact:
        return False
    if not isinstance(artifact["param_names"], list):
        return False
    return check(artifact["ir"])


def verify_full_artifact_integrity(full_artifact: Dict[str, Any]) -> bool:
    """Validate a complete exported capability artifact (its plan plus
    every primitive_irs entry, including nested sub-capability plans),
    not just a single leaf IR node.
    """
    if not isinstance(full_artifact, dict):
        return False
    if "plan" not in full_artifact or "primitive_irs" not in full_artifact:
        return False
    if not isinstance(full_artifact["primitive_irs"], dict):
        return False
    return all(verify_artifact_integrity(dep)
               for dep in full_artifact["primitive_irs"].values())


_EXCEPTION_WHITELIST = {
    "ZeroDivisionError": ZeroDivisionError, "ValueError": ValueError,
    "TypeError": TypeError, "ArithmeticError": ArithmeticError,
    "OverflowError": OverflowError,
}


def _walk_guard_body(body: List[ast.stmt], param_names: Set[str],
                       known_primitive_names: Set[str],
                       dependencies: Set[str]) -> Optional[Dict[str, Any]]:
    """Recognize exactly two genuine, naturally-occurring multi-statement
    shapes (confirmed against real primitives: `divide`, `sqrt`) and
    convert each to a structured IR node -- nothing else. Returns None
    (extraction fails closed) for any other shape, including anything
    with more than 2 statements, loops, additional raises, or
    non-whitelisted exception types.
    """
    if len(body) == 2 and isinstance(body[1], ast.Return) and body[1].value is not None:
        guard, fallthrough = body[0], body[1]
        if (isinstance(guard, ast.If) and not guard.orelse and len(guard.body) == 1):
            inner = guard.body[0]
            # Shape 1: if <test>: raise Exc("message")  \n  return <expr>
            if (isinstance(inner, ast.Raise) and isinstance(inner.exc, ast.Call)
                    and isinstance(inner.exc.func, ast.Name)
                    and inner.exc.func.id in _EXCEPTION_WHITELIST
                    and len(inner.exc.args) == 1
                    and isinstance(inner.exc.args[0], ast.Constant)
                    and isinstance(inner.exc.args[0].value, str)):
                test_ir = _walk(guard.test, param_names, known_primitive_names, dependencies)
                then_ir = _walk(fallthrough.value, param_names, known_primitive_names, dependencies)
                if test_ir is None or then_ir is None:
                    return None
                return {"kind": "guard_raise", "test": test_ir,
                        "exception_type": inner.exc.func.id,
                        "message": inner.exc.args[0].value, "then": then_ir}
            # Shape 2: if <test>: return A   \n   return B  (fallthrough form)
            if isinstance(inner, ast.Return) and inner.value is not None:
                test_ir = _walk(guard.test, param_names, known_primitive_names, dependencies)
                a_ir = _walk(inner.value, param_names, known_primitive_names, dependencies)
                b_ir = _walk(fallthrough.value, param_names, known_primitive_names, dependencies)
                if test_ir is None or a_ir is None or b_ir is None:
                    return None
                return {"kind": "ifexp", "test": test_ir, "body": a_ir, "orelse": b_ir}

    # Shape 3: if <test>: return A  else: return B  (single-statement, proper if/else)
    if (len(body) == 1 and isinstance(body[0], ast.If)
            and len(body[0].body) == 1 and len(body[0].orelse) == 1
            and isinstance(body[0].body[0], ast.Return) and body[0].body[0].value is not None
            and isinstance(body[0].orelse[0], ast.Return) and body[0].orelse[0].value is not None):
        node = body[0]
        test_ir = _walk(node.test, param_names, known_primitive_names, dependencies)
        a_ir = _walk(node.body[0].value, param_names, known_primitive_names, dependencies)
        b_ir = _walk(node.orelse[0].value, param_names, known_primitive_names, dependencies)
        if test_ir is None or a_ir is None or b_ir is None:
            return None
        return {"kind": "ifexp", "test": test_ir, "body": a_ir, "orelse": b_ir}

    return None


def extract_portable_ir(fn: Callable[..., Any],
                          known_primitive_names: Optional[Set[str]] = None) -> ExtractionResult:
    """Extract a portable IR from a real, already-registered primitive
    function. Supports exactly two shapes: a bare lambda expression, or
    a `def name(...): return <expr>` single-statement function -- both
    read via ast.parse(inspect.getsource(fn)), never eval/exec. Anything
    else (multi-statement bodies, loops, closures, unsupported calls)
    returns ExtractionResult(ir=None, error=...) rather than a partial
    or best-effort IR.
    """
    known_primitive_names = known_primitive_names or set()
    try:
        src = inspect.getsource(fn)
    except (OSError, TypeError) as e:
        return ExtractionResult(ir=None, param_names=[], error=f"no source: {e}")

    try:
        tree = ast.parse(textwrap.dedent(src.strip().rstrip(",")))
    except SyntaxError as e:
        return ExtractionResult(ir=None, param_names=[], error=f"unparseable: {e}")

    lambda_node = None
    funcdef_node = None
    for n in ast.walk(tree):
        if isinstance(n, ast.Lambda) and lambda_node is None:
            lambda_node = n
        if isinstance(n, ast.FunctionDef) and funcdef_node is None:
            funcdef_node = n

    if funcdef_node is not None:
        body = funcdef_node.body
        params = [a.arg for a in funcdef_node.args.args]
        dependencies: Set[str] = set()
        if len(body) == 1 and isinstance(body[0], ast.Return) and body[0].value is not None:
            expr_node = body[0].value
        else:
            control_ir = _walk_guard_body(body, set(params), known_primitive_names, dependencies)
            if control_ir is None:
                return ExtractionResult(ir=None, param_names=params,
                                         error="unsupported: multi-statement function body")
            return ExtractionResult(ir=control_ir, param_names=params, dependencies=dependencies)
    elif lambda_node is not None:
        params = [a.arg for a in lambda_node.args.args]
        expr_node = lambda_node.body
        dependencies = set()
    else:
        return ExtractionResult(ir=None, param_names=[],
                                 error="no lambda or single-return function found")

    ir = _walk(expr_node, set(params), known_primitive_names, dependencies)
    if ir is None:
        return ExtractionResult(ir=None, param_names=params,
                                 error="expression contains unsupported construct")
    return ExtractionResult(ir=ir, param_names=params, dependencies=dependencies)


def interpret_portable_ir(ir: Dict[str, Any], args: Dict[str, Any],
                            dependency_irs: Optional[Dict[str, Dict[str, Any]]] = None) -> Any:
    """Deterministic tree-walking interpreter. No eval(), no exec(), no
    lambda reconstruction -- every node kind dispatches to a fixed,
    pre-existing Python callable from the module-level dispatch tables
    above. `dependency_irs` supplies the IR (not the original registry)
    for any "prim_call" node this IR references, enabling recursive,
    registry-free execution of a dependency closure.
    """
    dependency_irs = dependency_irs or {}
    kind = ir["kind"]
    if kind == "param":
        return args[ir["name"]]
    if kind == "const":
        return ir["value"]
    if kind == "binop":
        left = interpret_portable_ir(ir["left"], args, dependency_irs)
        right = interpret_portable_ir(ir["right"], args, dependency_irs)
        return _BINOP_DISPATCH[_NAME_TO_BINOP[ir["op"]]](left, right)
    if kind == "unaryop":
        operand = interpret_portable_ir(ir["operand"], args, dependency_irs)
        return _UNARYOP_DISPATCH[_NAME_TO_UNARYOP[ir["op"]]](operand)
    if kind == "compare":
        left = interpret_portable_ir(ir["left"], args, dependency_irs)
        right = interpret_portable_ir(ir["right"], args, dependency_irs)
        return _CMP_DISPATCH[_NAME_TO_CMP[ir["op"]]](left, right)
    if kind == "boolop":
        vals = [interpret_portable_ir(v, args, dependency_irs) for v in ir["values"]]
        return _BOOLOP_DISPATCH[ast.And if ir["op"] == "and" else ast.Or](vals)
    if kind == "ifexp":
        test = interpret_portable_ir(ir["test"], args, dependency_irs)
        branch = ir["body"] if test else ir["orelse"]
        return interpret_portable_ir(branch, args, dependency_irs)
    if kind == "guard_raise":
        test = interpret_portable_ir(ir["test"], args, dependency_irs)
        if test:
            exc_cls = _EXCEPTION_WHITELIST[ir["exception_type"]]
            raise exc_cls(ir["message"])
        return interpret_portable_ir(ir["then"], args, dependency_irs)
    if kind == "call":
        fn = _CALL_WHITELIST[ir["func"]]
        call_args = [interpret_portable_ir(a, args, dependency_irs) for a in ir["args"]]
        return fn(*call_args)
    if kind == "prim_call":
        dep_ir = dependency_irs.get(ir["func"])
        if dep_ir is None:
            raise ValueError(
                f"unresolved dependency {ir['func']!r}: no dependency IR "
                f"supplied -- this is exactly the failure mode the "
                f"dependency-closure mechanism exists to prevent silently")
        dep_param_names = dep_ir["param_names"]
        call_args = [interpret_portable_ir(a, args, dependency_irs) for a in ir["args"]]
        sub_args = dict(zip(dep_param_names, call_args))
        return interpret_portable_ir(dep_ir["ir"], sub_args, dependency_irs)
    raise ValueError(f"unknown IR node kind: {kind!r}")


def export_portable_capability(plan: Dict[str, Any], ops: List[str],
                                 primitives_registry: Any,
                                 capability_store: Any = None) -> Dict[str, Any]:
    """M+29.25b: export a full CapabilityRecord (its plan plus every
    primitive its steps reference) into one self-contained, JSON-
    serializable artifact. Walks record.ops (not a hardcoded name list)
    and calls classify_portability generically for each -- no
    capability-specific or primitive-specific branch anywhere in this
    function. Raises ValueError naming the exact non-portable primitive
    rather than silently producing an artifact that will fail later.

    M+29.29: an op referencing a previously-admitted, wrapped
    capability (recognized via the registry's own
    _acquired_capability_ids tag, set at registration time -- not by
    name pattern matching) is exported by recursively fetching ITS OWN
    already-persisted portable artifact and embedding it as a
    sub-plan, rather than attempting AST extraction on the wrapper
    closure (which would always fail, since the wrapper's body calls
    Composer.execute_sync, not a representable expression).
    """
    primitive_irs: Dict[str, Dict[str, Any]] = {}
    acquired_map = getattr(primitives_registry, "_acquired_capability_ids", {})
    for op_name in ops:
        if op_name in acquired_map:
            if capability_store is None:
                raise ValueError(
                    f"op {op_name!r} references acquired capability "
                    f"{acquired_map[op_name]!r} but no capability_store "
                    f"was supplied to resolve its own portable sub-plan")
            sub_portability = get_persisted_portability(capability_store, acquired_map[op_name])
            if sub_portability is None or sub_portability["status"] != "PORTABLE":
                raise ValueError(
                    f"op {op_name!r} depends on capability "
                    f"{acquired_map[op_name]!r} which is not itself portable")
            primitive_irs[op_name] = {"sub_plan": sub_portability["artifact"]}
            continue
        prim = primitives_registry.get(op_name)
        if prim is None:
            raise ValueError(f"primitive {op_name!r} not found in registry")
        classification = classify_portability(prim.fn, prim.effects, set(ops))
        if classification["status"] != "PORTABLE":
            raise ValueError(
                f"primitive {op_name!r} is not portable: "
                f"{classification['status']} ({classification.get('reason')})")
        primitive_irs[op_name] = {
            "ir": classification["ir"], "param_names": classification["param_names"],
        }
    return {"plan": plan, "primitive_irs": primitive_irs}


def execute_portable_capability(artifact: Dict[str, Any], args: Dict[str, Any]) -> Any:
    """Registry-free plan execution: resolves the SAME $param/$step
    reference shapes Composer.execute_sync already uses (confirmed by
    reading composer.py's own _resolve method), but dispatches each
    step's op via the embedded primitive_irs through
    interpret_portable_ir -- never consulting a live primitive
    registry, never importing swarm_engine.primitives, never calling
    the original Python callable.
    """
    plan = artifact["plan"]
    primitive_irs = artifact["primitive_irs"]
    env: Dict[str, Any] = {"__params__": args}

    def resolve(ref: Any) -> Any:
        if isinstance(ref, dict) and len(ref) == 1:
            key, val = next(iter(ref.items()))
            if key == "$step":
                if val not in env:
                    raise ValueError(f"reference to undefined step {val!r}")
                return env[val]
            if key == "$param":
                if val not in env["__params__"]:
                    raise ValueError(f"missing plan parameter {val!r}")
                return env["__params__"][val]
        if isinstance(ref, list):
            return [resolve(v) for v in ref]
        return ref

    for step in plan.get("steps", []):
        op_name = step["op"]
        if op_name not in primitive_irs:
            raise ValueError(f"step references non-portable/unexported op {op_name!r}")
        resolved_args = {k: resolve(v) for k, v in step.get("args", {}).items()}
        dep = primitive_irs[op_name]
        if "sub_plan" in dep:
            env[step["id"]] = execute_portable_capability(dep["sub_plan"], resolved_args)
        else:
            env[step["id"]] = interpret_portable_ir(dep["ir"], resolved_args)

    return resolve(plan.get("output"))


def get_persisted_portability(store: Any, capability_id: str) -> Optional[Dict[str, Any]]:
    """Retrieve the portability classification/artifact an admitted
    capability was automatically given during admission (see
    AdmissionController.admit()'s M+29.26 addition) via the existing,
    unmodified CapabilityStore.events() log -- no new storage mechanism.
    """
    import json as _json
    for ev in store.events(capability_id, limit=200):
        if ev["event"] == "portability":
            return _json.loads(ev["detail"])
    return None
