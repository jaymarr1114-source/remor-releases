"""b1: exactly ONE grant contract exists in the tree (U-7)."""
import ast
import os
from common import check, summary

TREE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
GRANTS = os.path.join(TREE, "runtime", "curiosity", "attribution", "grants.py")


@check("b1_no_attribution_grant_class")
def _():
    tree = ast.parse(open(GRANTS).read())
    classes = [n.name for n in ast.walk(tree) if isinstance(n, ast.ClassDef)]
    assert "Grant" not in classes, f"mutable Grant still defined: {classes}"
    assert "AllocationLedger" in classes
    return f"classes={classes}"


@check("b1_no_false_frozen_claim")
def _():
    src = open(GRANTS).read()
    # The old false docstring claimed a MUTABLE class was "the frozen FRM
    # grant shape". No such claim may attach to anything but FrmGrant now.
    assert '"""The frozen FRM grant shape.' not in src, "false claim survives"
    return "false docstring gone"


@check("b1_frmgrant_is_the_contract")
def _():
    from swarm_engine.curiosity.frm.grant import FrmGrant
    import dataclasses
    assert dataclasses.is_dataclass(FrmGrant)
    assert FrmGrant.__dataclass_params__.frozen, "FrmGrant must be frozen"
    assert FrmGrant.__dataclass_fields__["epoch_id"].type == "int" or True
    g_fields = FrmGrant.__dataclass_fields__
    # epoch_id annotation is int (string annotation under __future__ import)
    ann = g_fields["epoch_id"].type
    assert ann == "int", f"epoch_id annotation: {ann!r}"
    return "FrmGrant frozen, epoch_id: int"


@check("b1_primitive_grant_documented_distinct")
def _():
    # PrimitiveGrant is a different concept (effect permission), not a
    # competing FRM contract — it must remain, and remain distinct.
    from swarm_engine.primitives.core import Grant as PrimitiveGrant
    from swarm_engine.curiosity.frm.grant import FrmGrant
    assert PrimitiveGrant is not FrmGrant
    src = open(GRANTS).read()
    assert "PrimitiveGrant" in src, "distinction not documented in grants.py"
    return "PrimitiveGrant distinct and documented"


summary("b1")
