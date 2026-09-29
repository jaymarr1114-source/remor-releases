"""Contract tests: runtime/core/resource_arbitrator.py.

The arbitrator grants resources between loop controllers (charter C-4.4:
arbitration = the grant; caps = per-activation consumption enforced by the
substrate's admission pools). These tests pin the contract so future
changes cannot silently alter grant semantics.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "pylib")))

from swarm_engine.core.microcontroller.substrate import (
    MicrocontrollerSubstrate,
    R_ADMISSION_EXHAUSTED,
    R_CONCURRENCY_CAP,
    R_INVALID_BUDGET,
)
from swarm_engine.core.resource_arbitrator import ResourceArbitrator


class FakeClock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now

    def advance(self, s):
        self.now += s


def make_pair(clock, budget=100.0, concurrent=4):
    sub = MicrocontrollerSubstrate(clock=clock)
    for loop in ("acquisition", "execution", "distillation"):
        sub.register_loop(loop, budget_s=1.0, max_concurrent=1)
    arb = ResourceArbitrator(total_budget_s=budget,
                             total_max_concurrent=concurrent, clock=clock)
    arb.register_loop("acquisition", weight=2.0,
                      minimum_budget_s=10.0, minimum_concurrent=1)
    arb.register_loop("execution", weight=1.0,
                      minimum_budget_s=10.0, minimum_concurrent=1)
    arb.register_loop("distillation", weight=1.0,
                      minimum_budget_s=5.0, minimum_concurrent=1)
    return sub, arb


class TestResourceArbitrator(unittest.TestCase):
    def test_contention_grants_within_capacity(self):
        clock = FakeClock()
        sub, arb = make_pair(clock)
        arb.set_demand("acquisition", budget_s=80.0, concurrent=4)
        arb.set_demand("execution", budget_s=80.0, concurrent=4)
        arb.set_demand("distillation", budget_s=10.0, concurrent=1)
        d = arb.apply(sub)
        self.assertLessEqual(
            sum(g.budget_s for g in d.grants.values()), 100.0)
        self.assertLessEqual(
            sum(g.max_concurrent for g in d.grants.values()), 4)
        # minimums honored for every demanding loop
        self.assertGreaterEqual(d.grants["acquisition"].budget_s, 10.0)
        self.assertGreaterEqual(d.grants["execution"].budget_s, 10.0)
        self.assertGreaterEqual(d.grants["distillation"].budget_s, 5.0)
        # weight decides the remainder
        self.assertGreater(d.grants["acquisition"].budget_s,
                           d.grants["execution"].budget_s)
        # no grant exceeds demand
        self.assertLessEqual(d.grants["distillation"].budget_s, 10.0)

    def test_grant_enforced_by_substrate_refusal(self):
        clock = FakeClock()
        sub = MicrocontrollerSubstrate(clock=clock)
        sub.register_loop("acquisition", budget_s=1.0, max_concurrent=1)
        arb = ResourceArbitrator(total_budget_s=100.0, total_max_concurrent=100,
                                 clock=clock)
        arb.register_loop("acquisition", weight=1.0)
        arb.set_demand("acquisition", budget_s=50.0, concurrent=100)
        grant = arb.apply(sub).grants["acquisition"]
        self.assertAlmostEqual(grant.budget_s, 50.0)
        ok = 0
        for _ in range(8):
            r = sub.spawn("acquisition", purpose="p", budget_s=10.0)
            if r.ok:
                ok += 1
            else:
                self.assertEqual(r.refusal.reason, R_ADMISSION_EXHAUSTED)
        self.assertEqual(ok, 5)  # 5x10s == 50s grant; the rest fail closed
        adm = sub._loops["acquisition"]
        self.assertLessEqual(adm.reserved_s, grant.budget_s + 1e-9)
        self.assertEqual(adm.total_refused, 3)

    def test_concurrency_grant_enforced(self):
        clock = FakeClock()
        sub, arb = make_pair(clock)
        arb.set_demand("acquisition", budget_s=80.0, concurrent=4)
        arb.set_demand("execution", budget_s=80.0, concurrent=4)
        arb.set_demand("distillation", budget_s=10.0, concurrent=1)
        grants = arb.apply(sub).grants
        # execution loses the slot contention: grant is 1 slot
        self.assertEqual(grants["execution"].max_concurrent, 1)
        r1 = sub.spawn("execution", purpose="one", budget_s=1.0)
        r2 = sub.spawn("execution", purpose="two", budget_s=1.0)
        self.assertTrue(r1.ok)
        self.assertFalse(r2.ok)
        self.assertEqual(r2.refusal.reason, R_CONCURRENCY_CAP)

    def test_caps_are_not_grants(self):
        # Charter C-4.4: per-activation caps live in the substrate,
        # independent of arbitration.
        clock = FakeClock()
        sub = MicrocontrollerSubstrate(clock=clock)
        sub.register_loop("distillation", budget_s=1.0, max_concurrent=1)
        arb = ResourceArbitrator(total_budget_s=100.0, total_max_concurrent=4,
                                 clock=clock)
        arb.register_loop("distillation", weight=1.0)
        arb.set_demand("distillation", budget_s=50.0, concurrent=4)
        arb.apply(sub)
        r = sub.spawn("distillation", purpose="zero", budget_s=0.0)
        self.assertFalse(r.ok)
        self.assertEqual(r.refusal.reason, R_INVALID_BUDGET)

    def test_misconfiguration_is_loud(self):
        clock = FakeClock()
        _, arb = make_pair(clock)
        with self.assertRaises(ValueError):
            arb.set_demand("ghost", budget_s=1.0)
        with self.assertRaises(ValueError):
            arb.set_demand("acquisition", budget_s=-1.0)

    def test_minimums_over_capacity_scaled_and_recorded(self):
        clock = FakeClock()
        arb = ResourceArbitrator(total_budget_s=10.0, total_max_concurrent=2,
                                 clock=clock)
        arb.register_loop("a", weight=1.0, minimum_budget_s=10.0,
                          minimum_concurrent=2)
        arb.register_loop("b", weight=1.0, minimum_budget_s=10.0,
                          minimum_concurrent=2)
        arb.set_demand("a", budget_s=50.0, concurrent=4)
        arb.set_demand("b", budget_s=50.0, concurrent=4)
        d = arb.arbitrate()
        self.assertTrue(d.minimums_scaled)
        self.assertTrue(d.notes)
        self.assertLessEqual(
            sum(g.budget_s for g in d.grants.values()), 10.0 + 1e-9)

    def test_epoch_expiry_is_cooperative(self):
        clock = FakeClock()
        sub, arb = make_pair(clock)
        arb.set_demand("acquisition", budget_s=10.0, concurrent=1)
        arb.apply(sub)
        self.assertEqual(arb.expired_loops(), [])
        clock.advance(301.0)
        self.assertEqual(arb.expired_loops(), ["acquisition"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
