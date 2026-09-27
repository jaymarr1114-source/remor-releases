"""Quarantine stickiness: a deliberately-quarantined capability cannot be
silently resurrected by re-admission; only governed restore_everywhere
(with trust:transition authority) brings it back.

Regression coverage for the quarantine-resurrection repair
(2026-09-26, Worker A): AdmissionController.admit step 0c +
integrity.restore_everywhere ported from remor_runtime_next.
"""
import os
import sys
import tempfile
import unittest
from types import SimpleNamespace

sys.path.insert(0, os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "pylib")))

from swarm_engine.primitives import build_registry
from swarm_engine.synthesis.capability_store import CapabilityStore, plan_fingerprint
from swarm_engine.synthesis.admission import AdmissionController, Verdict
from swarm_engine.synthesis.integrity import (
    quarantine_everywhere, restore_everywhere, RestoreRefused, effective_status)
from swarm_engine.governance.lifecycle import CapabilityLifecycle, LifecycleState
from swarm_engine.governance.provenance import ProvenanceStore, TrustLevel
from swarm_engine.governance.oracle_binding import OracleRegistry
from swarm_engine.governance.caller_authorization import AuthorizationError

PLAN = {
    "name": "mean_of_three",
    "steps": [{"id": "s1", "op": "computation.mean",
               "args": {"values": [1, 2, 3]}}],
    "output": {"$step": "s1"},
}
GOAL = "compute the mean of 1, 2, 3"


class QuarantineStickinessTest(unittest.TestCase):
    def setUp(self):
        self.workdir = tempfile.mkdtemp(prefix="stickiness_")
        db = os.path.join(self.workdir, "eng.db")
        self.reg = build_registry()
        self.store = CapabilityStore(db_path=db)
        oreg = OracleRegistry(db)
        self.handle = oreg.engine_handle()
        self.prov = ProvenanceStore(
            db, oracle_registry=oreg, engine_oracle=self.handle)
        self.lc = CapabilityLifecycle(db_path=db)
        self.board = AdmissionController(
            self.reg, self.store,
            oracle_registry=oreg, engine_oracle=self.handle)
        self.engine = SimpleNamespace(
            capabilities=self.store, provenance=self.prov,
            lifecycle=self.lc, admission=self.board,
            primitives=self.reg, oracle_registry=oreg,
            oracle=self.handle)
        self.cap_id = plan_fingerprint(PLAN)

    def admit_and_quarantine(self, reason="deliberate: test"):
        r = self.board.admit(GOAL, PLAN, caller=self.handle)
        self.assertEqual(r.verdict, Verdict.ADMITTED, r.reasons)
        quarantine_everywhere(self.engine, self.cap_id, reason,
                              caller=self.handle)
        self.assertEqual(self.store.get(self.cap_id).status, "quarantined")
        self.assertIsNone(self.reg.get(f"acquired.{self.cap_id}"))

    # -- the 0c guard ------------------------------------------------------
    def test_readmit_of_deliberate_quarantine_is_refused(self):
        self.admit_and_quarantine()
        r = self.board.admit(GOAL, PLAN, caller=self.handle)
        self.assertEqual(r.verdict, Verdict.REJECTED)
        self.assertTrue(any("restore_everywhere" in x for x in r.reasons),
                        r.reasons)
        self.assertEqual(self.store.get(self.cap_id).status, "quarantined")
        self.assertIsNone(self.reg.get(f"acquired.{self.cap_id}"))

    def test_refusal_attempt_is_logged(self):
        self.admit_and_quarantine()
        self.board.admit(GOAL, PLAN, caller=self.handle)
        evs = [e["event"] for e in self.store.events(self.cap_id, limit=60)]
        self.assertIn("resurrection_refused", evs)
        self.assertIn("quarantined_everywhere", evs)  # provenance survived

    def test_derived_quarantine_still_recoverable_by_admit(self):
        # Derived (dependency) quarantines are NOT covered by the 0c guard:
        # audit_all's recovery semantics for them are unchanged.
        r = self.board.admit(GOAL, PLAN, caller=self.handle)
        self.assertEqual(r.verdict, Verdict.ADMITTED, r.reasons)
        self.store.set_status(self.cap_id, "quarantined")
        self.store.log(self.cap_id, "dependency_quarantined",
                       "dependency gone, auto-recoverable")
        r2 = self.board.admit(GOAL, PLAN, caller=self.handle)
        self.assertIn(r2.verdict, (Verdict.ADMITTED, Verdict.REUSED),
                      r2.reasons)

    # -- governed restore --------------------------------------------------
    def test_restore_everywhere_happy_path(self):
        self.admit_and_quarantine()
        actions = restore_everywhere(
            self.engine, self.cap_id, caller=self.handle,
            reason="review cleared")
        self.assertEqual(self.store.get(self.cap_id).status, "active")
        self.assertEqual(self.prov.get(self.cap_id).trust, TrustLevel.TRUSTED)
        self.assertEqual(self.lc.state_of(self.cap_id), LifecycleState.DEPLOYED)
        self.assertEqual(actions["lifecycle_walk"],
                         ["candidate", "constructed", "validating",
                          "verified", "admitted", "registered", "deployed"])
        self.assertEqual(effective_status(self.engine, self.cap_id)["effective"],
                         "active")
        self.assertEqual(
            self.store.goal_bindings().get(GOAL.strip().lower()), self.cap_id)
        prim = self.reg.get(f"acquired.{self.cap_id}")
        self.assertIsNotNone(prim)
        self.assertEqual(prim.fn(), 2.0)

    def test_restore_refusals_fail_closed(self):
        self.admit_and_quarantine()
        # Missing/forged caller: the merged default-deny contract refuses
        # with AuthorizationError (not RestoreRefused).
        with self.assertRaises(AuthorizationError):
            restore_everywhere(self.engine, self.cap_id,
                               caller=None, reason="x")
        with self.assertRaises(RestoreRefused):
            restore_everywhere(self.engine, self.cap_id,
                               caller=self.handle, reason="")
        attacker = SimpleNamespace(producer_id="attacker:eve")
        with self.assertRaises(AuthorizationError):
            restore_everywhere(self.engine, self.cap_id,
                               caller=attacker, reason="let me in")
        self.assertEqual(self.store.get(self.cap_id).status, "quarantined")

    def test_restore_refuses_epistemic_revocation(self):
        r = self.board.admit(GOAL, PLAN, caller=self.handle)
        self.assertEqual(r.verdict, Verdict.ADMITTED, r.reasons)
        self.store.set_status(self.cap_id, "quarantined")
        self.store.log(self.cap_id, "epistemic_revocation", "law contradicted")
        with self.assertRaises(RestoreRefused):
            restore_everywhere(self.engine, self.cap_id,
                               caller=self.handle, reason="nope")
        self.assertEqual(self.store.get(self.cap_id).status, "quarantined")

    def test_stickiness_survives_a_restore(self):
        self.admit_and_quarantine()
        restore_everywhere(self.engine, self.cap_id, caller=self.handle,
                           reason="review cleared")
        self.assertEqual(self.store.get(self.cap_id).status, "active")
        quarantine_everywhere(self.engine, self.cap_id, "deliberate: hold",
                              caller=self.handle)
        r = self.board.admit(GOAL, PLAN, caller=self.handle)
        self.assertEqual(r.verdict, Verdict.REJECTED)
        self.assertEqual(self.store.get(self.cap_id).status, "quarantined")


if __name__ == "__main__":
    unittest.main()
