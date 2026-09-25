"""
swarm_engine/agents/capabilities/math_ops.py

A real (small) capability: safely evaluates arithmetic expressions.
Uses ast parsing with a whitelist of node types instead of eval()/exec(),
so it's both functional and safe to run on untrusted task input.
"""
import ast
import operator

_ALLOWED_BINOPS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.Pow: operator.pow,
    ast.Mod: operator.mod,
    ast.FloorDiv: operator.floordiv,
}
_ALLOWED_UNARYOPS = {
    ast.UAdd: operator.pos,
    ast.USub: operator.neg,
}


class UnsafeExpressionError(Exception):
    pass


def _eval_node(node):
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
        return node.value
    if isinstance(node, ast.BinOp) and type(node.op) in _ALLOWED_BINOPS:
        return _ALLOWED_BINOPS[type(node.op)](_eval_node(node.left), _eval_node(node.right))
    if isinstance(node, ast.UnaryOp) and type(node.op) in _ALLOWED_UNARYOPS:
        return _ALLOWED_UNARYOPS[type(node.op)](_eval_node(node.operand))
    raise UnsafeExpressionError(f"Disallowed expression node: {type(node).__name__}")


def safe_eval(expression: str):
    """Evaluates a pure arithmetic expression. Raises UnsafeExpressionError
    for anything outside +,-,*,/,**,%,// on numeric literals."""
    tree = ast.parse(expression, mode="eval")
    return _eval_node(tree.body)
