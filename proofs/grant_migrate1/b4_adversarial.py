"""b4: adversarial — the migrated contract must fail closed everywhere
the old mutable Grant was attackable."""
import dataclasses
import os
import tempfile
import time
from common import check, make_grant, summary

from swarm_engine.curiosity.frm.grant import FrmGrant
from swarm_engine.primitives.core import Grant as PrimitiveGrant, Effect
from runtime.curiosity.attribution.grants import (
    AllocationLedger, AllocationRefused, validate_grant, grant_expired)
from runtime.curiosity.attribution.testdoubles import EnforcementStub

TMP = tempfile.mkdtemp(prefix="gm_b4_")


@check("b4_frozen_post_issue_mutation")
def _():
    g = make_grant()
    for field, val in (("budget_s", 9999.0), ("epoch_id", 2), ("lent", True)):
        try:
            setattr(g, field, val)
            raise AssertionError(f"mutation of {field} allowed")
        except dataclasses.FrozenInstanceError:
            pass
    return "all post-issue mutations refused"


@check("b4_non_frmgrant_never_coerced")
def _():
    led = AllocationLedger(os.path.join(TMP, "b4.db"))
    impostors = [
        {"grant_id": "x", "epoch_id": 1},                    # legacy-shaped dict
        "grant-id-string",                                    # plain string
        PrimitiveGrant(effect=Effect.READ_FS, pattern="*"),   # other Grant
        None,
    ]
    for imp in impostors:
        try:
            led.record_grant(imp)
            led.close()
            raise AssertionError(f"impostor accepted: {imp!r:.40}")
        except AllocationRefused:
            pass
        except Exception as e:
            led.close()
            raise AssertionError(f"wrong failure mode for {imp!r:.40}: {e!r}")
    led.close()
    return f"{len(impostors)} impostors refused via AllocationRefused"


@check("b4_str_epoch_id_refused")
def _():
    # The old contract allowed epoch_id="epoch-1" (str). Smuggle one in via
    # replace() — the ledger must refuse it, not coerce it.
    g = make_grant()
    sneaky = dataclasses.replace(g, epoch_id="epoch-1")
    led = AllocationLedger(os.path.join(TMP, "b4b.db"))
    try:
        led.record_grant(sneaky)
    except AllocationRefused as e:
        led.close()
        assert "int" in str(e), e
        return f"refused: {e}"
    led.close()
    raise AssertionError("str epoch_id accepted")


@check("b4_negative_budget_refused")
def _():
    g = make_grant(budget_s=-5.0)
    try:
        validate_grant(g)
    except AllocationRefused as e:
        return f"refused: {e}"
    raise AssertionError("negative budget accepted")


@check("b4_zero_epoch_s_refused")
def _():
    g = make_grant(epoch_s=0.0)
    try:
        validate_grant(g)
    except AllocationRefused as e:
        return f"refused: {e}"
    raise AssertionError("zero epoch_s accepted")


@check("b4_expired_allocate_refused")
def _():
    led = AllocationLedger(os.path.join(TMP, "b4c.db"))
    old = make_grant(epoch_s=600.0, issued_at=time.time() - 3600.0)
    assert grant_expired(old)
    led.record_grant(old)
    try:
        led.allocate(old.grant_id, "ctrl-1", "exec-1", EnforcementStub("RUNNING"))
    except AllocationRefused as e:
        led.close()
        assert "epoch" in str(e)
        return f"refused: {e}"
    led.close()
    raise AssertionError("expired grant allocated")


@check("b4_enforcement_blocks_allocation")
def _():
    led = AllocationLedger(os.path.join(TMP, "b4d.db"))
    g = make_grant()
    led.record_grant(g)
    for state in ("SUSPENDED_SAFETY", "HARD_SHUTDOWN_RESOURCE", "BANNED_6M"):
        try:
            led.allocate(g.grant_id, "ctrl-1", "exec-1", EnforcementStub(state))
            led.close()
            raise AssertionError(f"allocated under {state}")
        except AllocationRefused:
            pass
    led.close()
    return "zero allocation under 3 enforcement states"


@check("b4_shape_drift_assertion")
def _():
    # as_dict() asserts exactly the six frozen keys — shape drift fails loud.
    g = make_grant()
    assert set(g.as_dict().keys()) == {"grant_id", "epoch_id", "epoch_s",
                                       "dimensions", "primary_minimum", "lent"}
    return "frozen six-key shape holds"


summary("b4")
