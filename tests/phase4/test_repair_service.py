"""Phase 4, item 1: RepairService battery.

Real mechanisms only:
  * real RemorOrganization (anchored via the repair_loop anchor_shim),
  * real agent credentials (AgentDirectory token auth, real grants),
  * the REAL IndependentValidator in a subprocess for every submit
    (no mocks, no hard-coded verdicts),
  * every attack actually executed: unauthenticated / unauthorized /
    forged-token / confused-deputy submits, double admission, admission
    without a verdict, unauthenticated reads.

Run: python3 tests/phase4/test_repair_service.py
"""
import json
import os
import sys
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
os.environ.setdefault(
    "REMOR_TEST_WORK_ROOT",
    os.path.join(os.sep, "tmp", "remor_phase4_test"))
sys.path.insert(0, os.path.abspath(
    os.path.join(_HERE, "..", "..", "pylib")))          # swarm_engine -> runtime
sys.path.insert(0, os.path.abspath(
    os.path.join(_HERE, "..", "repair_loop")))          # anchor_shim

import anchor_shim  # noqa: E402

from swarm_engine.services.repair import (  # noqa: E402
    RepairService, _case_from_json)
from swarm_engine.acquisition.repair_synthesis_cardinality import (  # noqa: E402
    synthesize_cardinality_guard, SynthesisRefused,
    D3_SRC, D3_CONTRACT, D3_HELDOUT_SRC, D3_HELDOUT_CONTRACT)
from swarm_engine.governance.caller_authorization import (  # noqa: E402
    AgentDirectory, AuthorizationError)
from swarm_engine.governance.oracle_binding import (  # noqa: E402
    DECISION_REPAIR_SUBMIT, DECISION_REPAIR_APPLY)
from swarm_engine.agent_org.store import digest  # noqa: E402
from swarm_engine.agent_org.exceptions import LifecycleError  # noqa: E402

A_ID = "p4_agent_repairer"
B_ID = "p4_agent_observer"

DIAGNOSIS = (
    "dict(zip(<9 label literals>, traits)) in ApexProfile.__init__ silently "
    "truncates a short traits vector: a 5-element input constructs a 5-key "
    "dims dict instead of being rejected. Repair: synthesized fail-fast "
    "contract-arity guard (len(traits) != 9 -> ValueError) inserted before "
    "the truncation site; labels from the source AST, arity 9 from the "
    "independent APEX-9 contract."
)


def _defect_signature():
    return {
        "family": "cardinality-truncation",
        "file": "apex_profile.py",
        "function": "ApexProfile.__init__",
        "observed": "5-element traits vector silently constructs a 5-key "
                    "dims dict (zip truncation)",
        "entrypoint": D3_CONTRACT["entrypoint"],
        "input_names": ["traits"],
        "pre_digest": digest(D3_SRC),
    }


def _d3_cases():
    return [
        {"args": {"traits": [0.5] * 9}, "expect": 9,
         "label": "9-dim vector constructs 9 dims", "kind": "positive",
         "must_fail": False},
        {"args": {"traits": [0.1] * 5}, "expect": None, "must_fail": True,
         "label": "5-dim vector must raise", "kind": "negative"},
        {"args": {"traits": [0.9] * 9}, "expect": 9,
         "label": "second 9-dim vector constructs 9 dims", "kind": "positive",
         "must_fail": False},
    ]


def _register(org, agent_id, *decision_classes):
    agents = AgentDirectory(org.oregistry)
    cred = agents.register_agent(
        org.oregistry.engine_handle(), source="phase4 battery",
        agent_id=agent_id, decision_classes=decision_classes)
    return cred.token


class SynthesizerTests(unittest.TestCase):
    """Third family: synthesizer behaviour, no org involved."""

    @classmethod
    def setUpClass(cls):
        cls.repaired = synthesize_cardinality_guard(
            D3_SRC, D3_CONTRACT["constructor"], D3_CONTRACT["arity"],
            D3_CONTRACT["citation"])

    def _exec(self, source):
        ns = {}
        exec(compile(source, "<d3>", "exec"), ns)
        return ns

    def test_defect_is_real_before_repair(self):
        # The pre-repair module silently truncates: no exception, 5 keys.
        ns = self._exec(D3_SRC)
        self.assertEqual(ns["check_traits"]([0.5] * 5), 5)

    def test_guard_present_and_behavioral(self):
        self.assertIn("if len(traits) != 9:", self.repaired)
        self.assertIn("raise ValueError", self.repaired)
        # Labels came from the source, not the synthesizer: spot-check.
        self.assertIn("'drive'", self.repaired)
        self.assertIn("'tempo'", self.repaired)
        ns = self._exec(self.repaired)
        with self.assertRaises(ValueError):
            ns["check_traits"]([0.5] * 5)
        self.assertEqual(ns["check_traits"]([0.5] * 9), 9)

    def test_refuse_no_zip_site(self):
        src = ("class A:\n"
               "    def __init__(self, x):\n"
               "        self.d = {'a': 1}\n")
        with self.assertRaises(SynthesisRefused):
            synthesize_cardinality_guard(src, "A.__init__", 3, "c")

    def test_refuse_missing_constructor(self):
        with self.assertRaises(SynthesisRefused):
            synthesize_cardinality_guard(D3_SRC, "Nope.__init__", 9, "c")

    def test_refuse_nonliteral_labels(self):
        for src in (
            "class A:\n    def __init__(self, x, n):\n"
            "        self.d = dict(zip([n, 'b', 'c'], x))\n",
            "class A:\n    def __init__(self, x):\n"
            "        self.d = dict(zip([1, 2, 3], x))\n",
        ):
            with self.assertRaises(SynthesisRefused) as ctx:
                synthesize_cardinality_guard(src, "A.__init__", 3, "c")
            self.assertIn("not a literal list of strings",
                          str(ctx.exception))

    def test_refuse_bad_arity(self):
        for bad in (0, -1, -9, "9", 9.0, True, False, None):
            with self.assertRaises(SynthesisRefused):
                synthesize_cardinality_guard(
                    D3_SRC, D3_CONTRACT["constructor"], bad, "c")

    def test_heldout_different_labels_and_arity(self):
        # Same synthesizer, different labels, different arity, different
        # constructor/entrypoint names: generality is structural.
        repaired = synthesize_cardinality_guard(
            D3_HELDOUT_SRC, D3_HELDOUT_CONTRACT["constructor"],
            D3_HELDOUT_CONTRACT["arity"], D3_HELDOUT_CONTRACT["citation"])
        self.assertIn("if len(levels) != 4:", repaired)
        self.assertIn("'north'", repaired)
        self.assertIn("'west'", repaired)
        ns = self._exec(repaired)
        with self.assertRaises(ValueError):
            ns["check_levels"]([1, 2, 3])
        self.assertEqual(ns["check_levels"]([1, 2, 3, 4]), 4)


class ServiceAuthzTests(unittest.TestCase):
    """RepairService: auth, admission, governed reads. Real org."""

    @classmethod
    def setUpClass(cls):
        workdir = anchor_shim.fresh_workdir("p4repair_")
        cls.org = anchor_shim.deploy_org(workdir)
        cls.agents = AgentDirectory(cls.org.oregistry)
        cls.a_token = _register(cls.org, A_ID,
                                DECISION_REPAIR_SUBMIT, DECISION_REPAIR_APPLY)
        cls.b_token = _register(cls.org, B_ID)  # live agent, no repair grants
        cls.svc = RepairService(cls.org, cls.agents)
        cls.post = synthesize_cardinality_guard(
            D3_SRC, D3_CONTRACT["constructor"], D3_CONTRACT["arity"],
            D3_CONTRACT["citation"])
        cls.post_digest = digest(cls.post)

    def _submit(self, repair_id, agent_id=A_ID, token=None, cases=None):
        return self.svc.submit_repair(
            repair_id=repair_id, agent_id=agent_id,
            defect_signature=_defect_signature(), diagnosis=DIAGNOSIS,
            pre_code=D3_SRC, post_code=self.post,
            entrypoint=D3_CONTRACT["entrypoint"],
            cases=cases if cases is not None else _d3_cases(),
            caller=(agent_id, self.a_token if token is None else token))

    # -- attacks on submit ------------------------------------------------

    def test_submit_unauthenticated_refused(self):
        with self.assertRaises(AuthorizationError):
            self.svc.submit_repair(
                repair_id="rep_nope1", agent_id=A_ID,
                defect_signature=_defect_signature(), diagnosis=DIAGNOSIS,
                pre_code=D3_SRC, post_code=self.post,
                entrypoint=D3_CONTRACT["entrypoint"], cases=_d3_cases(),
                caller=None)
        with self.assertRaises(AuthorizationError):
            self.svc.submit_repair(
                repair_id="rep_nope2", agent_id="ghost_agent",
                defect_signature=_defect_signature(), diagnosis=DIAGNOSIS,
                pre_code=D3_SRC, post_code=self.post,
                entrypoint=D3_CONTRACT["entrypoint"], cases=_d3_cases(),
                caller=("ghost_agent", "bogus"))

    def test_submit_unauthorized_agent_refused(self):
        # B is a live, authenticated agent but holds no repair grants.
        with self.assertRaises(AuthorizationError):
            self.svc.submit_repair(
                repair_id="rep_nope3", agent_id=B_ID,
                defect_signature=_defect_signature(), diagnosis=DIAGNOSIS,
                pre_code=D3_SRC, post_code=self.post,
                entrypoint=D3_CONTRACT["entrypoint"], cases=_d3_cases(),
                caller=(B_ID, self.b_token))

    def test_submit_forged_token_refused(self):
        with self.assertRaises(AuthorizationError):
            self.svc.submit_repair(
                repair_id="rep_nope4", agent_id=A_ID,
                defect_signature=_defect_signature(), diagnosis=DIAGNOSIS,
                pre_code=D3_SRC, post_code=self.post,
                entrypoint=D3_CONTRACT["entrypoint"], cases=_d3_cases(),
                caller=(A_ID, "f" * 64))

    def test_submit_confused_deputy_refused(self):
        # Claimed agent_id disagrees with the authenticated caller.
        with self.assertRaises(AuthorizationError):
            self.svc.submit_repair(
                repair_id="rep_nope5", agent_id="p4_agent_impostor",
                defect_signature=_defect_signature(), diagnosis=DIAGNOSIS,
                pre_code=D3_SRC, post_code=self.post,
                entrypoint=D3_CONTRACT["entrypoint"], cases=_d3_cases(),
                caller=(A_ID, self.a_token))

    # -- JSON service-boundary -------------------------------------------

    def test_predicate_case_refused_at_boundary(self):
        # Predicates are in-process callables: the boundary refuses them
        # outright instead of silently weakening the verification.
        with self.assertRaises(ValueError) as ctx:
            _case_from_json({"args": {"traits": [0.5] * 9},
                             "predicate": "lambda v: True"})
        self.assertIn("in-process-only", str(ctx.exception))

    def test_nonserializable_case_refused_at_boundary(self):
        with self.assertRaises(ValueError):
            _case_from_json({"args": {"traits": {1, 2, 3}}})

    def test_must_fail_case_builds(self):
        case = _case_from_json({"args": {"traits": [0.1] * 5},
                                "must_fail": True, "label": "short",
                                "kind": "negative"})
        self.assertTrue(case.must_fail)
        self.assertIsNone(case.predicate)

    # -- full cycle through the service ----------------------------------

    def test_submit_admit_full_cycle_d3(self):
        rid = "rep_d3_" + digest(self.post_digest + A_ID)[:12]
        res = self._submit(rid)
        self.assertEqual(res["repair_id"], rid)
        # Independent verdict from the REAL subprocess validator: admitted.
        self.assertTrue(res["admitted"], f"verdict refused: {res['reasons']}")
        # The verdict binds the exact post bytes: the execution id on the
        # service result matches the persisted repair record.
        rec = self.org.review.get_repair_record(rid)
        self.assertEqual(res["execution_id"], rec["verdict_execution_id"])
        self.assertEqual(rec["repair_id"], rid)
        self.assertEqual(rec["agent_id"], A_ID)
        self.assertEqual(rec["pre_digest"], digest(D3_SRC))
        self.assertEqual(rec["post_digest"], self.post_digest)
        # Trust derivation: the stored verdict binds the post digest.
        vrow = self.org.review.require_admitted_verdict(
            self.post_digest, "repair")
        self.assertEqual(vrow["code_digest"], self.post_digest)
        self.assertEqual(vrow["artifact_ref"], rid)
        self.assertEqual(vrow["execution_id"], res["execution_id"])

        decision = self.svc.admit_repair(rid, caller=(A_ID, self.a_token))
        self.assertEqual(decision["repair_id"], rid)
        self.assertTrue(decision["decision_id"].startswith("dec_"))
        rec2 = self.org.review.get_repair_record(rid)
        self.assertEqual(rec2["admission_decision_id"],
                         decision["decision_id"])

        # Double admission refused.
        with self.assertRaises(LifecycleError):
            self.svc.admit_repair(rid, caller=(A_ID, self.a_token))

        # Governed read by an agent with NO repair grants: allowed (any
        # live agent), and standing reflects the admitted verdict.
        got = self.svc.get_repair(rid, caller=(B_ID, self.b_token))
        self.assertEqual(got["repair_id"], rid)
        self.assertTrue(got["standing"]["admitted"])
        self.assertEqual(got["standing"]["execution_id"],
                         res["execution_id"])

    def test_admit_no_verdict_refused(self):
        with self.assertRaises(KeyError):
            self.svc.admit_repair("rep_never_submitted_xyz",
                                  caller=(A_ID, self.a_token))

    # -- governed reads ---------------------------------------------------

    def test_get_repair_unauthenticated_refused(self):
        with self.assertRaises(AuthorizationError):
            self.svc.get_repair("rep_whatever", caller=None)
        with self.assertRaises(AuthorizationError):
            self.svc.get_repair("rep_whatever",
                                caller=("ghost_agent", "bogus"))

    def test_list_repairs_unauthenticated_refused(self):
        with self.assertRaises(AuthorizationError):
            self.svc.list_repairs(caller=None)

    def test_list_repairs_latest_and_family_filter(self):
        rid = "rep_d3_list_" + digest(self.post_digest + "list")[:12]
        self._submit(rid)
        rows = self.svc.list_repairs(caller=(B_ID, self.b_token))
        by_id = {r["repair_id"]: r for r in rows}
        self.assertIn(rid, by_id)
        # Latest-row semantics: exactly one row per repair_id.
        self.assertEqual(len([r for r in rows if r["repair_id"] == rid]), 1)
        self.assertEqual(by_id[rid]["post_digest"], self.post_digest)
        self.assertTrue(by_id[rid]["standing"]["admitted"])
        # Family filter.
        fam = self.svc.list_repairs(caller=(B_ID, self.b_token),
                                    defect_family="cardinality-truncation")
        self.assertIn(rid, {r["repair_id"] for r in fam})
        none = self.svc.list_repairs(caller=(B_ID, self.b_token),
                                     defect_family="no-such-family")
        self.assertEqual(none, [])
        # The engine handle also authenticates for reads.
        eng_rows = self.svc.list_repairs(
            caller=self.org.oregistry.engine_handle())
        self.assertGreaterEqual(len(eng_rows), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
