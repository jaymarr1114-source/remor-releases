"""
swarm_engine/primitives

The capability substrate. `build_registry()` assembles the whole vocabulary;
everything else in the engine draws from the object it returns.
"""
from swarm_engine.primitives.core import (
    ANY, BOOL, BYTES, CALLABLE, DICT, FLOAT, INT, LIST, NONE, NUM, OPT, STR,
    TUPLE, UNION, AuditEntry, Effect, ExecContext, Governor, Grant,
    PermissionError_, Primitive, PrimitiveRegistry, TypeSpec, coerce, infer,
    unify,
)
from swarm_engine.primitives.families_pure import register_all_pure
from swarm_engine.primitives.families_effect import register_all_effect
from swarm_engine.primitives.families_meta import register_all_meta


def build_registry(governor: Governor = None) -> PrimitiveRegistry:
    """Construct a registry with every family registered."""
    reg = PrimitiveRegistry(governor=governor or Governor())
    register_all_pure(reg)
    register_all_effect(reg)
    register_all_meta(reg)
    return reg


__all__ = [
    "build_registry", "PrimitiveRegistry", "Primitive", "Governor", "Grant",
    "Effect", "ExecContext", "TypeSpec", "PermissionError_", "AuditEntry",
    "infer", "coerce", "unify",
    "ANY", "INT", "FLOAT", "NUM", "BOOL", "STR", "BYTES", "NONE",
    "LIST", "DICT", "TUPLE", "UNION", "OPT", "CALLABLE",
]
