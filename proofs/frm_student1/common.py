"""Shared harness for the FRM-STUDENT-1 gate: grant factory + a real
budget-tracking substrate double (not a mock — it genuinely exhausts)."""
import os
import sys
import time

TREE = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, os.path.join(TREE, "pylib"))
sys.path.insert(0, os.path.join(TREE, "distill1"))

from swarm_engine.curiosity.frm.grant import FrmGrant, LendingRecord  # noqa: E402


class SubstrateDouble:
    """Implements the substrate charge interface
    charge(mc_id, seconds) -> (exhausted, state), with a real budget
    that genuinely depletes. Mirrors the semantics of
    MicrocontrollerSubstrate.charge (cooperative)."""

    def __init__(self, budget_s=1000.0):
        self.remaining_s = float(budget_s)
        self.calls = []

    def charge(self, mc_id, seconds):
        self.calls.append((mc_id, float(seconds)))
        self.remaining_s -= float(seconds)
        if self.remaining_s > 0:
            return False, "ACTIVE"
        return True, "EXHAUSTED"


def make_grant(budget_s=400.0, max_concurrent=1,
               enforcement_state_at_issue="RUNNING", epoch_id=1):
    return FrmGrant.issue(
        domain="curiosity", epoch_id=epoch_id, epoch_s=300.0,
        budget_s=budget_s, max_concurrent=max_concurrent,
        primary_minimum_budget_s=0.0, primary_minimum_concurrent=0,
        lent=False, lending=LendingRecord(0.0, 0),
        enforcement_state_at_issue=enforcement_state_at_issue,
        issued_at=time.time(), note="frm-student-1-gate")
