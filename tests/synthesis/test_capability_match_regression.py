"""Regression tests for the qualified-op repair in
synthesis/capability_match.py (capability-aware planning mission, 2026-09-27).

Failing-before code: pre-repair capability_match assumed bare primitive
names (`op == "map"`, polarity tables keyed on "multiply"/"divide"), while
real admitted plans store qualified names ("data.map",
"computation.multiply"). Consequence: qualified map plans lost the
map-shape score, and the polarity guard missed divide-vs-multiply,
misrouting "divide each value by 2 and sum them" to a x2 multiplier.

Repair: `_bare_op()` normalization in `plan_has_map` and
`polarity_conflict` (capability_match.py).

Causal chain tested: structural unit checks -> router-level refusal checks
-> adaptation proposal contract checks, all against the real admission path.
"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "pylib")))

from swarm_engine.core.engine import SwarmEngine  # noqa: E402
from swarm_engine.synthesis import capability_match as cm  # noqa: E402
from swarm_engine.synthesis.admission import (  # noqa: E402
    SmokeTest, Verdict)
from swarm_engine.synthesis.intent_router import IntentRouter  # noqa: E402
from swarm_engine.synthesis.capability_store import (  # noqa: E402
    plan_fingerprint)


def _plan(op_name, bound_b):
    return {
        "name": "scale_and_sum", "params": {"values": "list"},
        "steps": [
            {"id": "m", "op": op_name,
             "args": {"items": {"$param": "values"},
                      "fn": {"$partial": {"op": "computation.multiply",
                                           "bound": {"b": bound_b},
                                           "free": ["a"]}}}},
            {"id": "s", "op": "computation.sum",
             "args": {"values": {"$step": "m"}}},
        ],
        "output": {"$step": "s"},
    }


class TestQualifiedOpRegression(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._td = tempfile.TemporaryDirectory()
        db = os.path.join(cls._td.name, "eng.db")
        cls.eng = SwarmEngine(db_path=db)
        cls.reg = cls.eng.primitives
        # Admit once; the plan fingerprint is deterministic, so a second
        # identical admission would be refused as a duplicate.
        plan = _plan("data.map", 2)
        res = cls.eng.admit_as_engine(
            "double each value and sum them", plan,
            smoke=SmokeTest(args={"values": [1, 2, 3]}, expect=12),
            name="double_and_sum")
        assert res.ok and res.verdict == Verdict.ADMITTED, res.reasons
        cls.capability_id = res.capability_id

    @classmethod
    def tearDownClass(cls):
        cls.eng.close()
        cls._td.cleanup()

    def test_plan_has_map_bare(self):
        self.assertTrue(
            cm.plan_has_map({"steps": [{"op": "map", "args": {}}]}))

    def test_plan_has_map_qualified(self):
        self.assertTrue(
            cm.plan_has_map({"steps": [{"op": "data.map", "args": {}}]}))

    def test_plan_has_map_reduce_is_not_map(self):
        self.assertFalse(
            cm.plan_has_map({"steps": [{"op": "data.reduce", "args": {}}]}))

    def test_polarity_bare_multiply_conflicts(self):
        c, _ = cm.polarity_conflict(
            "divide each value by 2 and sum them",
            {"steps": [{"op": "multiply", "args": {}}]}, self.reg)
        self.assertTrue(c)

    def test_polarity_qualified_multiply_conflicts(self):
        c, _ = cm.polarity_conflict(
            "divide each value by 2 and sum them",
            {"steps": [{"op": "computation.multiply", "args": {}}]}, self.reg)
        self.assertTrue(c)

    def test_polarity_qualified_add_does_not_conflict(self):
        c, _ = cm.polarity_conflict(
            "divide each value by 2 and sum them",
            {"steps": [{"op": "computation.add", "args": {}}]}, self.reg)
        self.assertFalse(c)

    def test_router_divide_goal_not_routed_to_multiplier(self):
        router = IntentRouter(self.eng)
        rt = router.route("divide each value by 2 and sum them")
        self.assertFalse(rt.ok)
        self.assertIn(rt.refusal, ("unknown_intent", "ambiguous_intent"))

    def test_router_matching_goal_still_routes(self):
        router = IntentRouter(self.eng)
        rt = router.route("double each value and sum them")
        self.assertTrue(rt.ok)
        self.assertEqual(rt.capability_id, self.capability_id)

    def test_adapt_proposal_contract(self):
        recs = self.eng.capabilities.list(status="active", limit=100)
        ex5 = [({"values": [1, 2, 3]}, 30), ({"values": [2, 2]}, 20)]
        props = cm.adapt_compatible(
            recs, "scale every value by 5 and sum them", ex5,
            self.eng.composer, registry=self.eng.primitives)
        self.assertGreaterEqual(len(props), 1)
        p = props[0]
        pid = plan_fingerprint(p.plan)
        self.assertIsNone(self.eng.capabilities.get(pid))
        self.assertIsNone(self.eng.primitives.get("acquired." + pid))
        self.assertEqual(p.behavioral, 1.0)

    def test_adapt_ambiguous_goal_refused(self):
        recs = self.eng.capabilities.list(status="active", limit=100)
        ex5 = [({"values": [1, 2, 3]}, 30), ({"values": [2, 2]}, 20)]
        self.assertEqual(
            cm.adapt_compatible(recs, "scale by 5 or 6 and sum", ex5,
                                self.eng.composer), [])

    def test_adapt_no_examples_refused(self):
        recs = self.eng.capabilities.list(status="active", limit=100)
        self.assertEqual(
            cm.adapt_compatible(recs, "scale every value by 5 and sum them",
                                [], self.eng.composer), [])


if __name__ == "__main__":
    unittest.main()
