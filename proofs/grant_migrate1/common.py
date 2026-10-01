"""Shared harness for the GRANT-MIGRATE-1 battery."""
import os
import sys
import time

TREE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, TREE)
sys.path.insert(0, os.path.join(TREE, "pylib"))

from swarm_engine.curiosity.frm.grant import FrmGrant, LendingRecord  # noqa: E402


def make_grant(**kw):
    args = dict(domain="curiosity", epoch_id=1, epoch_s=600.0,
                budget_s=60.0, max_concurrent=4,
                primary_minimum_budget_s=1.0, primary_minimum_concurrent=1,
                lent=False, lending=LendingRecord(0.0, 0),
                enforcement_state_at_issue="RUNNING",
                issued_at=time.time())
    args.update(kw)
    return FrmGrant.issue(**args)


RESULTS = []


def check(name):
    def deco(fn):
        try:
            detail = fn()
            RESULTS.append(True)
            print(f"PASS {name}: {detail}")
        except Exception as e:  # noqa: BLE001
            RESULTS.append(False)
            print(f"FAIL {name}: {e}")
    return deco


def summary(battery):
    n = sum(RESULTS)
    total = len(RESULTS)
    print(f"BATTERY {battery} {n}/{total} {'PASS' if n == total else 'FAIL'}")
    sys.exit(0 if n == total else 1)
