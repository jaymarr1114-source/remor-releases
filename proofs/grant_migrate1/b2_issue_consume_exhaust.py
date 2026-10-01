"""b2: issue -> record -> get -> allocate -> double-record refused."""
import os
import tempfile
from common import check, make_grant, summary

from runtime.curiosity.attribution.grants import (
    AllocationLedger, AllocationRefused)
from runtime.curiosity.attribution.testdoubles import EnforcementStub

TMP = tempfile.mkdtemp(prefix="gm_b2_")


@check("b2_issue_record_get")
def _():
    led = AllocationLedger(os.path.join(TMP, "b2.db"))
    g = make_grant()
    led.record_grant(g)
    g2 = led.get_grant(g.grant_id)
    assert g2 is not None
    assert type(g2).__name__ == "FrmGrant", type(g2)
    assert g2.epoch_id == 1 and isinstance(g2.epoch_id, int)
    assert g2.budget_s == 60.0 and g2.max_concurrent == 4
    assert g2.domain == "curiosity" and g2.lent is False
    assert g2.as_dict().keys() == {"grant_id", "epoch_id", "epoch_s",
                                   "dimensions", "primary_minimum", "lent"}
    led.close()
    return f"round-trip ok grant={g.grant_id[:8]}"


@check("b2_allocate_binds_controller")
def _():
    led = AllocationLedger(os.path.join(TMP, "b2a.db"))
    g = make_grant()
    led.record_grant(g)
    a = led.allocate(g.grant_id, "ctrl-1", "exec-1", EnforcementStub("RUNNING"))
    assert a.grant_id == g.grant_id
    assert a.epoch_id == 1 and isinstance(a.epoch_id, int)
    assert a.controller_id == "ctrl-1" and a.budget_s == 60.0
    assert len(led.allocations_for_grant(g.grant_id)) == 1
    led.close()
    return f"allocation ok {a.allocation_id[:8]}"


@check("b2_double_record_refused")
def _():
    led = AllocationLedger(os.path.join(TMP, "b2b.db"))
    g = make_grant()
    led.record_grant(g)
    try:
        led.record_grant(g)
    except Exception as e:
        led.close()
        return f"second record refused: {type(e).__name__}"
    led.close()
    raise AssertionError("double record accepted")


@check("b2_unknown_grant_refused")
def _():
    led = AllocationLedger(os.path.join(TMP, "b2c.db"))
    try:
        led.allocate("no-such-grant", "ctrl-1", "exec-1",
                     EnforcementStub("RUNNING"))
    except AllocationRefused as e:
        led.close()
        return f"refused: {e}"
    led.close()
    raise AssertionError("unknown grant allocated")


summary("b2")
