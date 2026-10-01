"""Q2 unified memory tests: planner-attempt Z-check + unified query layer.

Real sqlite in tmp dirs, real planner, real fresh PURE subprocesses.
No mocks. Stores start empty; every record in them was written by the
test itself through the real store APIs.
"""
from __future__ import annotations

import os
import sys
import tempfile
import time
import unittest

_CANONICAL = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, _CANONICAL)
sys.path.insert(0, os.path.join(_CANONICAL, "pylib"))

from runtime.intellect.unified_memory import (  # noqa: E402
    ZCheckResult,
    attempt_z,
    evidence_from_demo_actions,
    prior_attempts,
    query_memory,
    similar_experiences,
    trace_capability,
    trace_delta,
    z_check_with_experience,
)
from runtime.intellect.epistemic import EpistemicStore  # noqa: E402
from runtime.synthesis.capability_store import (  # noqa: E402
    CapabilityRecord,
    CapabilityStore,
    plan_fingerprint,
)
from runtime.primitives import build_registry  # noqa: E402
from runtime.synthesis.planner import Planner  # noqa: E402
from runtime.acquisition.ingest import (  # noqa: E402
    ExternalAction,
    ExternalDemonstration,
    InventorySnapshot,
    _objective_overlap,
    ingest_external_demonstration,
)


def _stores():
    tmp = tempfile.mkdtemp(prefix="q2test_")
    return (EpistemicStore(db_path=os.path.join(tmp, "ep.db")),
            CapabilityStore(db_path=os.path.join(tmp, "cap.db")))


REV_OBJECTIVE = "generate a quarterly revenue summary"
REV_EVIDENCE = [({"input": '{"q1": 100, "q2": 150, "q3": 120, "q4": 180}'},
                 "Total revenue 550 across 4 quarters, strongest in Q4.")]
DBL_EVIDENCE = [({"values": [21, 7]}, [42, 14]), ({"values": [3]}, [6])]


class ZCheckTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.registry = build_registry()
        cls.planner = Planner(cls.registry)

    def test_false_overlap_fixed(self):
        """Q5's demonstrated failure: lexical overlap misses a real gap."""
        self.assertTrue(_objective_overlap(REV_OBJECTIVE, ["image_generate"]))
        z = attempt_z(REV_OBJECTIVE, evidence=REV_EVIDENCE,
                      planner=self.planner, registry=self.registry,
                      epistemic=None)
        self.assertFalse(z.reached)
        self.assertEqual(z.classification, "output_mismatch")
        # The planner really did attempt: a backward-search plan existed.
        self.assertIn("deserialize", z.ops_used)

    def test_verified_reach_no_false_gap(self):
        z = attempt_z("double each number in the list", evidence=DBL_EVIDENCE,
                      planner=self.planner, registry=self.registry,
                      epistemic=None)
        self.assertTrue(z.reached)
        self.assertEqual(z.classification, "verified_reach")
        self.assertEqual(z.detail.get("examples_verified"), 2)

    def test_proposal_without_evidence_is_unverified(self):
        z = attempt_z(REV_OBJECTIVE, evidence=None,
                      planner=self.planner, registry=self.registry,
                      epistemic=None)
        self.assertFalse(z.reached)
        self.assertEqual(z.classification, "proposal_unverified")

    def test_nonsense_objective_not_reachable(self):
        z = attempt_z("asdf qwerty zxcv nonsense",
                      evidence=[({"input": "x"}, "nonsense-result-zzz")],
                      planner=self.planner, registry=self.registry,
                      epistemic=None)
        self.assertFalse(z.reached)

    def test_effectful_plan_stays_sandboxed(self):
        """An effectful planner proposal must not execute its effect:
        the PURE sandbox denies it, the file survives, and the Z-check
        honestly reports a gap instead of a reach."""
        import os as _os
        target = os.path.join(tempfile.mkdtemp(prefix="q2fx_"),
                              "victim.txt")
        with open(target, "w") as fh:
            fh.write("x")
        z = attempt_z("delete the temp files",
                      evidence=[({"path": target}, "deleted")],
                      planner=self.planner, registry=self.registry,
                      epistemic=None)
        self.assertIn("delete", z.ops_used)  # the effectful plan was proposed
        self.assertFalse(z.reached)
        self.assertTrue(_os.path.exists(target))  # ...but never executed

    def test_never_raises(self):
        z = attempt_z("", evidence=None, planner=self.planner,
                      registry=self.registry, epistemic=None)
        self.assertIsInstance(z, ZCheckResult)
        self.assertFalse(z.reached)

    def test_evidence_from_demo_actions(self):
        actions = [ExternalAction(kind="tool_call", name="t",
                                  inputs={"a": 1}, outputs={"r": 2}),
                   ExternalAction(kind="tool_call", name="u",
                                  inputs={}, outputs={"r": 2})]
        pairs = evidence_from_demo_actions(actions)
        self.assertEqual(pairs, [({"a": 1}, 2)])


class QueryHelperTests(unittest.TestCase):
    """Tests for the cross-store query helpers (MEMORY-UNIFY-1).

    These were methods on the un-adopted UnifiedMemory class; they are now
    plain module functions. The tests exercise them through real stores.
    """
    @classmethod
    def setUpClass(cls):
        cls.registry = build_registry()
        cls.planner = Planner(cls.registry)

    def _seeded(self):
        ep, caps = _stores()
        demo = ExternalDemonstration(
            source="subagent_trace", objective="summarize quarterly revenue",
            actions=[ExternalAction(
                kind="tool_call", name="summarize_quarters",
                inputs={"input": '{"q1": 10}'},
                outputs={"summary": "Total revenue 10."})],
            required_tools=["revenue_summarizer"],
            provenance={"simulated": True, "note": "Q2 test fixture"})
        inv = InventorySnapshot(capabilities=["image_generate"],
                                primitives=["deserialize"], at=time.time())
        res = ingest_external_demonstration(demo, inv, ep)
        self.assertTrue(res.recorded)
        plan = {"steps": [{"id": "s1", "op": "text.summarize",
                           "args": {"text": {"$param": "input"}}}],
                "params": {"input": "any"}, "output": {"$step": "s1"}}
        rec = CapabilityRecord(capability_id=plan_fingerprint(plan),
                               name="revenue_summary",
                               goal="summarize quarterly revenue figures",
                               plan=plan, ops=["text.summarize"],
                               effects=["pure"])
        caps.store(rec)
        return ep, caps, res.delta_id, rec.capability_id

    def test_query_reaches_all_three_stores(self):
        ep, caps, _delta_id, _cap_id = self._seeded()
        ans = query_memory(ep, caps, self.registry, "quarterly revenue summary")
        kinds = {h["kind"] for h in (ans.epistemic_hits
                                     + ans.capability_hits
                                     + ans.primitive_hits)}
        self.assertEqual(kinds, {"observation", "capability", "primitive"})
        self.assertGreaterEqual(len(ans.epistemic_hits), 1)
        self.assertGreaterEqual(len(ans.capability_hits), 1)
        self.assertGreaterEqual(len(ans.primitive_hits), 1)

    def test_trace_delta_finds_ingested_delta(self):
        ep, caps, delta_id, _cap_id = self._seeded()
        tr = trace_delta(ep, delta_id)
        self.assertIsNotNone(tr["delta"])
        self.assertEqual(tr["delta"]["source"], "technique_delta")

    def test_trace_capability_finds_record(self):
        ep, caps, _delta_id, cap_id = self._seeded()
        tr = trace_capability(ep, caps, cap_id)
        self.assertIsNotNone(tr["record"])
        self.assertEqual(tr["record"]["name"], "revenue_summary")

    def test_attempt_history_informs_next_attempt(self):
        ep, caps = _stores()
        first = z_check_with_experience("double each number in the list",
                                        evidence=DBL_EVIDENCE,
                                        epistemic=ep,
                                        registry=self.registry,
                                        planner=self.planner)
        self.assertEqual(len(first.detail.get("prior_attempts", [])), 0)
        second = z_check_with_experience("double each number in the list",
                                         evidence=DBL_EVIDENCE,
                                         epistemic=ep,
                                         registry=self.registry,
                                         planner=self.planner)
        priors = second.detail.get("prior_attempts", [])
        self.assertGreaterEqual(len(priors), 1)
        self.assertGreaterEqual(len(prior_attempts(
            ep, "double each number in the list")), 2)


    def test_distillation_experience_influences_later_decision(self):
        """An M2-style distilled experience, retrieved through the unified
        layer, informs a later Z-check on a neighboring objective."""
        from runtime.intellect.unified_memory import (
            record_distillation_experience,
        )
        ep, caps = _stores()
        record_distillation_experience(
            ep, objective="double each number in the list",
            delta_id="delta_dbl_001", Y={"action": "map x -> 2*x"},
            Z={"planner_ops": ["data.map", "computation.multiply"]},
            T="map with multiply-by-2 lambda",
            E=[{"inputs": {"values": [21]}, "outputs": [42]}],
            D=[], V={"status": "verified", "held_out": True},
            C="cap_doubler_001")
        # A neighboring objective -- not the exact same string.
        res = z_check_with_experience("double the numbers in the collection",
                                      evidence=[({"values": [5]}, [10])],
                                      epistemic=ep,
                                      registry=self.registry,
                                      planner=self.planner)
        exps = res.detail.get("prior_experiences", [])
        self.assertGreaterEqual(len(exps), 1)
        self.assertEqual(exps[0]["delta_id"], "delta_dbl_001")
        self.assertTrue(res.reached)  # planner still genuinely covers it

    def test_no_experience_no_influence(self):
        ep, caps = _stores()
        res = z_check_with_experience("double each number in the list",
                                      evidence=DBL_EVIDENCE,
                                      epistemic=ep,
                                      registry=self.registry,
                                      planner=self.planner)
        self.assertEqual(res.detail.get("prior_experiences", []), [])

class ZCheckIngestionIntegrationTests(unittest.TestCase):
    """The lexical heuristic is REPLACED by the planner-attempt Z-check
    when a z_check callable is injected into the real ingestion gate.
    Without it, Q5 behavior is byte-identical (backward compatibility)."""

    @classmethod
    def setUpClass(cls):
        cls.registry = build_registry()
        cls.planner = Planner(cls.registry)

    def _z(self, objective, evidence):
        return attempt_z(objective, evidence=evidence,
                         planner=self.planner, registry=self.registry,
                         epistemic=None)

    def _demo(self, objective, inp, out):
        return ExternalDemonstration(
            source="subagent_trace", objective=objective,
            actions=[ExternalAction(kind="tool_call", name="demo_op",
                                    inputs=inp, outputs=out)],
            required_tools=[],
            provenance={"simulated": True, "note": "Q2 integration fixture"})

    def test_behavioral_gap_records_delta_where_lexical_missed_it(self):
        ep, _caps = _stores()
        inv = InventorySnapshot(capabilities=["image_generate"],
                                primitives=["deserialize"], at=time.time())
        demo = self._demo(REV_OBJECTIVE,
                          {"input": '{"q1": 100, "q2": 150, "q3": 120, "q4": 180}'},
                          {"summary": "Total revenue 550 across 4 quarters, strongest in Q4."})
        # Lexical path (Q5): misses the gap.
        lex = ingest_external_demonstration(demo, inv, ep)
        self.assertFalse(lex.recorded)
        self.assertEqual(lex.reason, "no_observable_gap")
        # Behavioral path: the planner attempt exposes the real gap.
        beh = ingest_external_demonstration(demo, inv, ep, z_check=self._z)
        self.assertTrue(beh.recorded)
        self.assertIsNotNone(beh.delta_id)
        self.assertEqual(beh.detail["z_check"]["classification"],
                         "output_mismatch")

    def test_genuinely_covered_objective_still_not_recorded(self):
        ep, _caps = _stores()
        inv = InventorySnapshot(capabilities=["image_generate"],
                                primitives=[], at=time.time())
        demo = self._demo("double each number in the list",
                          {"values": [21, 7]}, {"doubled": [42, 14]})
        beh = ingest_external_demonstration(demo, inv, ep, z_check=self._z)
        self.assertFalse(beh.recorded)
        self.assertEqual(beh.reason, "no_observable_gap")
        self.assertTrue(beh.detail["z_check"]["reached"])


if __name__ == "__main__":
    unittest.main()
