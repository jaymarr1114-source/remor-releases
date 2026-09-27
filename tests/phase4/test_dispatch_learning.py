"""Phase 4 battery: dispatch learning native in the product (worker 2).

Items 2, 3, 4: native dispatch-evidence capture in NLToolDispatcher,
the DispatchLearningService governed read contract + dispatch-completion
policy, and acquired-op re-execution with dependency closure.

Real mechanisms only: a real SwarmEngine, real admitted capabilities
(base + a REAL composite whose plan calls an acquired child op), a real
RemorOrganization with an anchored chain, real subprocess re-execution
through the real IndependentValidator, and real attacks (quarantine,
closure tampering, empty closure, dependency cycle) that must be
refused.

Run: python3 tests/phase4/test_dispatch_learning.py
"""
import json
import os
import sqlite3
import sys
import tempfile
import unittest

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_CANONICAL = os.path.join(_THIS_DIR, "..", "..")
sys.path.insert(0, os.path.join(_CANONICAL, "pylib"))  # swarm_engine -> runtime
sys.path.insert(0, os.path.join(_CANONICAL, "tests", "phase4"))
sys.path.insert(0, os.path.join(_CANONICAL, "tests", "agent_org"))

_PYL = os.path.join(_CANONICAL, "pylib")
os.environ["PYTHONPATH"] = _PYL + os.pathsep + os.environ.get("PYTHONPATH", "")

import common_phase4  # noqa: E402
from swarm_engine.core.engine import SwarmEngine  # noqa: E402
from swarm_engine.synthesis.nl_dispatch import NLToolDispatcher  # noqa: E402
from swarm_engine.services.dispatch_learning import (  # noqa: E402
    DispatchLearningService,
)
from swarm_engine.agent_org.dispatch_learning import (  # noqa: E402
    get_evidence,
    DISPATCH_EVIDENCE_KIND,
)
from swarm_engine.agent_org.store import digest, canonical  # noqa: E402
from swarm_engine.agent_org.exceptions import VerificationFailed  # noqa: E402
from swarm_engine.acquisition.semantic import Case  # noqa: E402
from swarm_engine.governance.caller_authorization import (  # noqa: E402
    AgentDirectory, AuthorizationError, _engine_caller_context,
)
from swarm_engine.governance.oracle_binding import ENGINE_PRODUCER_ID  # noqa: E402
from swarm_engine.synthesis.integrity import (  # noqa: E402
    effective_status, quarantine_everywhere, restore_everywhere,
)
from swarm_engine.synthesis.capability_store import plan_fingerprint  # noqa: E402
from swarm_engine.cognition.revocation import plan_acquired_refs  # noqa: E402
from swarm_engine.governance.anchor import collect_anchor_heads  # noqa: E402

SORT_GOAL = "sort numbers"
SORT_PLAN = {
    "name": "sort_numbers",
    "params": {"items": "list"},
    "steps": [{"id": "s1", "op": "sort",
               "args": {"items": {"$param": "items"}}}],
    "output": {"$step": "s1"},
}
PARENT_GOAL = "sort then reverse"

MAPPER_TEMPLATE = '''"""Dispatch technique distilled from verified dispatch {evidence_id}."""
import re

CAPABILITY_ID = "{capability_id}"
CAPABILITY_VERSION = {capability_version}
CANONICAL_DISPATCH_TEXT = "sort numbers"

_PATTERNS = (r"\\bsort\\b", r"\\border\\b", r"\\barrange\\b",
             r"\\bascend(?:ing)?\\b")


def map_request(user_text):
    """Map a sort request to dispatch parameters, or refuse as a value."""
    if not isinstance(user_text, str):
        return {{"ok": False, "reason": "request must be text"}}
    low = user_text.lower()
    if not any(re.search(p, low) for p in _PATTERNS):
        return {{"ok": False, "reason": "not a sort request"}}
    nums = [int(x) for x in re.findall(r"-?\\d+", user_text)]
    if not nums:
        return {{"ok": False, "reason": "no integers found"}}
    return {{"ok": True, "dispatch_text": CANONICAL_DISPATCH_TEXT,
             "capability_id": CAPABILITY_ID,
             "capability_version": CAPABILITY_VERSION,
             "args": {{"items": nums}}}}
'''


def _generality_cases(cap_id, version):
    def _ok(items):
        return {"ok": True, "dispatch_text": SORT_GOAL,
                "capability_id": cap_id, "capability_version": version,
                "args": {"items": items}}

    raw = [
        ("please sort 9 2 7", _ok([9, 2, 7])),
        ("arrange 4 1 9 in ascending order", _ok([4, 1, 9])),
        ("order these digits: 3 8 1", _ok([3, 8, 1])),
        ("what is the weather today",
         {"ok": False, "reason": "not a sort request"}),
        ("compute the average of 1 2 3",
         {"ok": False, "reason": "not a sort request"}),
    ]
    return [Case(args={"user_text": t}, expect=e, label=f"p4gen{i}")
            for i, (t, e) in enumerate(raw)]


class DispatchLearningBattery(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        base = tempfile.mkdtemp(prefix="p4_dispatch_learning_")
        cls.workdir = os.path.join(base, "work")
        cls.org = common_phase4.deploy_org(cls.workdir)
        cls.eng = SwarmEngine(
            db_path=os.path.join(cls.workdir, "engine.db"))
        # -- admit the base sort capability (engine-attributed) ----------
        adm = cls.eng.admit_as_engine(
            goal=SORT_GOAL, plan=dict(SORT_PLAN), name="sort_numbers")
        assert adm.ok, f"child admit failed: {adm.reasons}"
        cls.child_id = adm.capability_id
        cls.child_version = cls.eng.capabilities.get(cls.child_id).version
        # -- admit a REAL composite: parent plan calls acquired.<child> --
        parent_plan = {
            "name": "sort_then_reverse",
            "params": {"items": "list"},
            "steps": [
                {"id": "s1", "op": "acquired." + cls.child_id,
                 "args": {"items": {"$param": "items"}}},
                {"id": "s2", "op": "reverse",
                 "args": {"items": {"$step": "s1"}}},
            ],
            "output": {"$step": "s2"},
        }
        adm2 = cls.eng.admit_as_engine(
            goal=PARENT_GOAL, plan=parent_plan, name="sort_then_reverse")
        assert adm2.ok, f"parent admit failed: {adm2.reasons}"
        cls.parent_id = adm2.capability_id
        # sanity: the composite really composes the child
        refs = plan_acquired_refs(
            cls.eng.capabilities.get(cls.parent_id).plan)
        assert refs == {cls.child_id}, f"parent refs: {refs}"
        # -- agent + assignment ------------------------------------------
        cls.A = cls.org.factory.create("tpl_callable_coder_v1")
        asg = cls.org.assignments.create(
            cls.A.agent_id,
            objective={"goal": "dispatch sort capabilities"},
            constraints={},
            authority_scope={"allowed_capability_patterns": ["dispatch:*"],
                             "workspace": cls.A.workspace_path,
                             "max_steps": 20},
            expected_outputs={}, validation_requirements={},
            originating_decision="phase4:dispatch-learning")
        cls.asg = cls.org.assignments.activate(asg.assignment_id)
        cls.a_token = cls.org.agents._custody_tokens[cls.A.agent_id]
        # -- service + dispatcher ----------------------------------------
        cls.agents = AgentDirectory(cls.org.oregistry)
        cls.service = DispatchLearningService(
            cls.org, cls.agents, engine=cls.eng)
        assert cls.org.review.dispatch_engine is cls.eng
        cls.dispatcher = NLToolDispatcher(
            cls.eng, learning=cls.service)
        cls.dispatcher.bind_agent(cls.A.agent_id, cls.asg.assignment_id)

    # -- helpers ----------------------------------------------------------
    def _row_snapshot(self, evidence_id):
        con = sqlite3.connect(self.org.store.db_path)
        con.row_factory = sqlite3.Row
        try:
            row = con.execute(
                "SELECT * FROM ao_dispatch_evidence WHERE evidence_id=?",
                (evidence_id,)).fetchone()
            self.assertIsNotNone(row, f"no row for {evidence_id}")
            return dict(row)
        finally:
            con.close()

    def _restore_row(self, snapshot):
        """Restore exact original row bytes (chain intact) + re-anchor."""
        cols = [c for c in snapshot if c != "seq"]
        con = sqlite3.connect(self.org.store.db_path)
        try:
            sets = ", ".join(f"{c}=?" for c in cols)
            con.execute(
                f"UPDATE ao_dispatch_evidence SET {sets} WHERE seq=?",
                [snapshot[c] for c in cols] + [snapshot["seq"]])
            con.commit()
        finally:
            con.close()
        ok, msg = self.org.store.audit("ao_dispatch_evidence")
        self.assertTrue(ok, f"chain not restored: {msg}")
        self.org.anchor.anchor(
            collect_anchor_heads(self.org.store, self.org.oregistry),
            reason="recovery", authority=ENGINE_PRODUCER_ID,
            caller=self.org.oregistry.engine_handle())

    def _update_evidence_json(self, evidence_id, doc):
        con = sqlite3.connect(self.org.store.db_path)
        try:
            con.execute(
                "UPDATE ao_dispatch_evidence SET evidence_json=? "
                "WHERE evidence_id=?",
                (canonical(doc), evidence_id))
            con.commit()
        finally:
            con.close()

    def _dispatch_sort(self, items, text=SORT_GOAL):
        return self.dispatcher.dispatch(
            text, {"items": list(items)},
            producer=f"agent:{self.A.agent_id}")

    # -- item 2: native capture -------------------------------------------
    def test_01_auto_capture_native(self):
        res = self._dispatch_sort([5, 3, 8, 1])
        self.assertTrue(res.ok, res.refusal)
        self.assertEqual(res.result, [1, 3, 5, 8])
        self.assertIsNone(res.learning_error, res.learning_error)
        ev_id = "dsp_ev_" + digest(res.dispatch_id + self.A.agent_id)[:16]
        read = self.service.get_evidence(
            ev_id, caller=self.service.engine_caller)
        doc = read["evidence"]
        self.assertEqual(doc["evidence_id"], ev_id)
        self.assertEqual(doc["agent_id"], self.A.agent_id)
        self.assertEqual(doc["assignment_id"], self.asg.assignment_id)
        self.assertEqual(doc["dispatch_id"], res.dispatch_id)
        self.assertTrue(read["chain"]["audit_ok"],
                        read["chain"]["audit_msg"])
        # base plan: closure recorded, empty (no acquired refs)
        ev = get_evidence(self.org.store, ev_id)
        doc_parsed = json.loads(ev.evidence_json)
        self.assertEqual(doc_parsed["dependency_closure"], [])

    def test_02_no_agent_context_no_capture(self):
        n_before = len(self.service.list_evidence(
            caller=self.service.engine_caller))
        self.dispatcher.unbind_agent()
        try:
            res = self._dispatch_sort([3, 1, 2])
        finally:
            self.dispatcher.bind_agent(
                self.A.agent_id, self.asg.assignment_id)
        self.assertTrue(res.ok, res.refusal)
        self.assertEqual(res.result, [1, 2, 3])
        self.assertIsNone(res.learning_error)
        n_after = len(self.service.list_evidence(
            caller=self.service.engine_caller))
        self.assertEqual(n_before, n_after,
                         "dispatch without agent context captured evidence")

    def test_03_capture_failure_learning_error(self):
        # bind a bogus agent: capture must fail, dispatch must succeed
        self.dispatcher.bind_agent("agt_nonexistent_123",
                                   self.asg.assignment_id)
        try:
            res = self._dispatch_sort([4, 1])
        finally:
            self.dispatcher.bind_agent(
                self.A.agent_id, self.asg.assignment_id)
        self.assertTrue(res.ok, res.refusal)
        self.assertEqual(res.result, [1, 4])
        self.assertIsNotNone(res.learning_error, "learning_error not set")
        self.assertIn("unknown agent", res.learning_error,
                      res.learning_error)

    # -- item 2: governed reads --------------------------------------------
    def test_04_governed_reads(self):
        res = self._dispatch_sort([9, 2])
        ev_id = "dsp_ev_" + digest(res.dispatch_id + self.A.agent_id)[:16]
        # unauthenticated: no caller at all
        with self.assertRaises(AuthorizationError):
            self.service.get_evidence(ev_id, caller=None)
        with self.assertRaises(AuthorizationError):
            self.service.list_evidence(caller=None)
        # wrong token
        with self.assertRaises(AuthorizationError):
            self.service.get_evidence(
                ev_id, caller=(self.A.agent_id, "wrong-token"))
        # destroyed agent's credential fails authentication
        B = self.org.factory.create("tpl_callable_coder_v1")
        b_token = self.org.agents._custody_tokens[B.agent_id]
        self.org.agents.destroy(B.agent_id, actor="remor:engine",
                                reason="phase4: read-auth test")
        with self.assertRaises(AuthorizationError):
            self.service.get_evidence(ev_id, caller=(B.agent_id, b_token))
        # the attributed agent itself can read with its own credential
        read = self.service.get_evidence(
            ev_id, caller=(self.A.agent_id, self.a_token))
        self.assertEqual(read["evidence"]["evidence_id"], ev_id)
        self.assertTrue(read["chain"]["audit_ok"])
        self.assertTrue(read["chain"]["anchor_ok"],
                        read["chain"]["anchor_msg"])
        # unknown evidence id
        with self.assertRaises(KeyError):
            self.service.get_evidence(
                "dsp_ev_doesnotexist", caller=self.service.engine_caller)
        # service-level capture auth: wrong agent != attributed agent
        C = self.org.factory.create("tpl_callable_coder_v1")
        c_token = self.org.agents._custody_tokens[C.agent_id]
        try:
            with self.assertRaises(AuthorizationError):
                self.service.capture_evidence(
                    res.dispatch_id, self.A.agent_id,
                    self.asg.assignment_id, {"items": [9, 2]}, [2, 9],
                    caller=(C.agent_id, c_token))
        finally:
            self.org.agents.destroy(C.agent_id, actor="remor:engine",
                                    reason="phase4: capture-auth test")

    # -- item 3: completion policy ------------------------------------------
    def test_05_completion_policy(self):
        # fresh dispatch WITHOUT native capture (agent unbound), so the
        # policy's own capture path is exercised
        self.dispatcher.unbind_agent()
        try:
            res = self._dispatch_sort([5, 3, 8, 1])
        finally:
            self.dispatcher.bind_agent(
                self.A.agent_id, self.asg.assignment_id)
        self.assertTrue(res.ok, res.refusal)
        mapper = MAPPER_TEMPLATE.format(
            evidence_id="pending", agent_id=self.A.agent_id,
            capability_id=self.child_id,
            capability_version=self.child_version)
        ok, exp_or = self.service.complete_dispatch_knowledge(
            dispatch_id=res.dispatch_id, agent_id=self.A.agent_id,
            assignment_id=self.asg.assignment_id,
            args={"items": [5, 3, 8, 1]}, result_value=[1, 3, 5, 8],
            technique_name="sort_request_mapper_p4",
            code=mapper, entrypoint="map_request",
            problem_class="dispatch.arg_mapping.sort",
            tags=["dispatch", "arg-mapping", "sort"],
            io_contract={"input": "user_text: str",
                         "output": "{ok, dispatch_text, capability_id, args}"},
            params={"capability_id": self.child_id,
                    "capability_version": self.child_version,
                    "dispatch_text": SORT_GOAL},
            generality_cases=_generality_cases(
                self.child_id, self.child_version),
            caller=self.service.engine_caller)
        self.assertTrue(ok, f"completion failed: {exp_or}")
        exp = self.org.experience.get_experience(exp_or)
        self.assertEqual(exp.level, "L2")
        self.assertEqual(exp.origin, "dispatch_discovery", exp.origin)
        self.assertEqual(exp.discovered_by, self.A.agent_id)
        # replay of the SAME dispatch is refused (idempotent capture +
        # admission duplicate guard)
        with self.assertRaises(VerificationFailed) as cm:
            self.service.complete_dispatch_knowledge(
                dispatch_id=res.dispatch_id, agent_id=self.A.agent_id,
                assignment_id=self.asg.assignment_id,
                args={"items": [5, 3, 8, 1]}, result_value=[1, 3, 5, 8],
                technique_name="sort_request_mapper_p4",
                code=mapper, entrypoint="map_request",
                problem_class="dispatch.arg_mapping.sort",
                tags=["dispatch"], io_contract={}, params={},
                generality_cases=_generality_cases(
                    self.child_id, self.child_version),
                caller=self.service.engine_caller)
        self.assertIn("already admitted", str(cm.exception),
                      str(cm.exception)[:200])
        # exactly one evidence row for this dispatch (no duplicate on
        # the idempotent second capture)
        rows = [r for r in self.service.list_evidence(
            caller=self.service.engine_caller)
            if r["evidence"]["dispatch_id"] == res.dispatch_id]
        self.assertEqual(len(rows), 1, f"duplicate evidence rows: {len(rows)}")

    # -- item 4: acquired-op re-execution ------------------------------------
    def test_06_acquired_reexecution_pass(self):
        res = self.dispatcher.dispatch(
            PARENT_GOAL, {"items": [5, 3, 8, 1]},
            producer=f"agent:{self.A.agent_id}")
        self.assertTrue(res.ok, f"{res.refusal} {res.reasons}")
        self.assertEqual(res.result, [8, 5, 3, 1], res.result)
        self.assertIsNone(res.learning_error, res.learning_error)
        ev_id = "dsp_ev_" + digest(res.dispatch_id + self.A.agent_id)[:16]
        ev = get_evidence(self.org.store, ev_id)
        doc = json.loads(ev.evidence_json)
        closure = doc["dependency_closure"]
        self.assertEqual(len(closure), 1, f"closure: {closure}")
        entry = closure[0]
        self.assertEqual(entry["capability_id"], self.child_id)
        self.assertEqual(entry["version"], str(self.child_version))
        self.assertEqual(entry["plan_fingerprint"],
                         plan_fingerprint(
                             self.eng.capabilities.get(self.child_id).plan))
        # review passes: sandbox recomputation byte-identical
        verdict = self.org.review.verify_dispatch_evidence(ev_id)
        self.assertTrue(verdict.admitted, "; ".join(verdict.reasons)[:300])
        vrow = self.org.review.require_admitted_verdict(
            digest(ev.evidence_json), DISPATCH_EVIDENCE_KIND)
        self.assertEqual(vrow["artifact_ref"], ev_id)
        self.__class__.parent_ev_id = ev_id
        self.__class__.parent_dispatch = res

    def test_07_quarantine_child_refuses(self):
        ev_id = self.__class__.parent_ev_id
        self.eng.quarantine_as_engine(self.child_id, "phase4: battery")
        eff = effective_status(self.eng, self.child_id)
        self.assertEqual(eff["effective"], "quarantined", str(eff))
        try:
            with self.assertRaises(VerificationFailed) as cm:
                self.org.review.verify_dispatch_evidence(ev_id)
        finally:
            # governed restore of the child + dependency recovery of the
            # parent (derived quarantine), so later tests see live state
            restore_everywhere(
                self.eng, self.child_id,
                caller=_engine_caller_context(self.eng),
                reason="phase4: battery restore")
            reg = getattr(self.eng.admission,
                          "_register_capability_as_primitive", None)
            self.eng.capabilities.audit_all(
                self.eng.primitives,
                register_fn=(lambda rec: reg(rec.capability_id, rec)))
        self.assertIn("quarantined", str(cm.exception).lower(),
                      str(cm.exception)[:300])
        eff2 = effective_status(self.eng, self.child_id)
        self.assertEqual(eff2["effective"], "active", str(eff2))
        effp = effective_status(self.eng, self.parent_id)
        self.assertEqual(effp["effective"], "active", str(effp))
        # and the same evidence verifies again once deps are live
        verdict = self.org.review.verify_dispatch_evidence(ev_id)
        self.assertTrue(verdict.admitted, "; ".join(verdict.reasons)[:200])

    def test_08_tampered_closure_refuses(self):
        res = self.dispatcher.dispatch(
            PARENT_GOAL, {"items": [2, 7, 1]},
            producer=f"agent:{self.A.agent_id}")
        self.assertTrue(res.ok, res.refusal)
        ev_id = "dsp_ev_" + digest(res.dispatch_id + self.A.agent_id)[:16]
        snap = self._row_snapshot(ev_id)
        try:
            # attack: swap the recorded child plan for a different plan
            # (fingerprint kept) inside the evidence document
            doc = json.loads(snap["evidence_json"])
            evil_plan = {"name": "evil", "params": {"items": "list"},
                         "steps": [{"id": "s1", "op": "reverse",
                                    "args": {"items": {"$param": "items"}}}],
                         "output": {"$step": "s1"}}
            doc["dependency_closure"][0]["plan_json"] = canonical(evil_plan)
            self._update_evidence_json(ev_id, doc)
            with self.assertRaises(VerificationFailed) as cm:
                self.org.review.verify_dispatch_evidence(ev_id)
            self.assertIn("fingerprint", str(cm.exception).lower(),
                          str(cm.exception)[:300])
            # the chain audit also detects the raw-DB tamper
            ok, msg = self.org.store.audit("ao_dispatch_evidence")
            self.assertFalse(ok, "chain audit missed the tamper")
        finally:
            self._restore_row(snap)

    def test_09_empty_closure_refuses(self):
        res = self.dispatcher.dispatch(
            PARENT_GOAL, {"items": [6, 1, 4]},
            producer=f"agent:{self.A.agent_id}")
        self.assertTrue(res.ok, res.refusal)
        ev_id = "dsp_ev_" + digest(res.dispatch_id + self.A.agent_id)[:16]
        snap = self._row_snapshot(ev_id)
        try:
            # attack/legacy shape: acquired refs but an EMPTY closure
            doc = json.loads(snap["evidence_json"])
            doc["dependency_closure"] = []
            self._update_evidence_json(ev_id, doc)
            with self.assertRaises(VerificationFailed) as cm:
                self.org.review.verify_dispatch_evidence(ev_id)
            self.assertIn("no dependency closure", str(cm.exception).lower(),
                          str(cm.exception)[:300])
        finally:
            self._restore_row(snap)

    def test_10_legacy_row_base_plan_verifies(self):
        # backward compat: a row WITHOUT the dependency_closure key for a
        # base-primitive-only plan still verifies (treated as empty
        # closure, no acquired refs -> the old path)
        res = self._dispatch_sort([8, 3])
        ev_id = "dsp_ev_" + digest(res.dispatch_id + self.A.agent_id)[:16]
        snap = self._row_snapshot(ev_id)
        try:
            doc = json.loads(snap["evidence_json"])
            del doc["dependency_closure"]
            self._update_evidence_json(ev_id, doc)
            verdict = self.org.review.verify_dispatch_evidence(ev_id)
            self.assertTrue(verdict.admitted,
                            "; ".join(verdict.reasons)[:200])
        finally:
            self._restore_row(snap)

    def test_11_cyclic_dependency_capture_refused(self):
        # build a real cycle: dispatch the parent (valid), then surgically
        # make the child's stored plan reference the parent
        res = self.dispatcher.dispatch(
            PARENT_GOAL, {"items": [3, 1]},
            producer=f"agent:{self.A.agent_id}")
        self.assertTrue(res.ok, res.refusal)
        con = sqlite3.connect(self.eng.db_path)
        try:
            row = con.execute(
                "SELECT plan_json FROM plan_capabilities WHERE "
                "capability_id=?", (self.child_id,)).fetchone()
            orig_child_plan = row[0]
            cyclic = canonical({
                "name": "sort_numbers", "params": {"items": "list"},
                "steps": [{"id": "s1",
                           "op": "acquired." + self.parent_id,
                           "args": {"items": {"$param": "items"}}}],
                "output": {"$step": "s1"}})
            con.execute(
                "UPDATE plan_capabilities SET plan_json=? WHERE "
                "capability_id=?", (cyclic, self.child_id))
            con.commit()
        finally:
            con.close()
        try:
            # use a FRESH agent+assignment so the replay guard cannot
            # interfere; native capture already wrote this dispatch's row
            # under agent A, so this direct call must fail on the cycle,
            # not on duplication
            D = self.org.factory.create("tpl_callable_coder_v1")
            dasg = self.org.assignments.create(
                D.agent_id, objective={"goal": "cycle test"},
                constraints={},
                authority_scope={
                    "allowed_capability_patterns": ["dispatch:*"]},
                expected_outputs={}, validation_requirements={},
                originating_decision="phase4: cycle test")
            dasg = self.org.assignments.activate(dasg.assignment_id)
            from swarm_engine.agent_org.dispatch_learning import (
                capture_dispatch_evidence)
            with self.assertRaises(ValueError) as cm:
                capture_dispatch_evidence(
                    self.eng, self.org.store, res.dispatch_id,
                    D.agent_id, dasg.assignment_id,
                    {"items": [3, 1]}, [3, 1])
            self.assertIn("cycle", str(cm.exception).lower(),
                          str(cm.exception)[:200])
            self.org.agents.destroy(D.agent_id, actor="remor:engine",
                                    reason="phase4: cycle test artifact")
        finally:
            con = sqlite3.connect(self.eng.db_path)
            try:
                con.execute(
                    "UPDATE plan_capabilities SET plan_json=? WHERE "
                    "capability_id=?", (orig_child_plan, self.child_id))
                con.commit()
            finally:
                con.close()
        # child plan restored: dispatch works again
        r2 = self._dispatch_sort([2, 1])
        self.assertTrue(r2.ok and r2.result == [1, 2], str(r2.result))


if __name__ == "__main__":
    unittest.main(verbosity=2)
