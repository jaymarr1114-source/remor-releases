"""M6 — user-acceptance loop regression tests.

Every mechanism below runs on REAL machinery: the real planner plans, the
real composer executes, the authentication gate executes real held-out /
negative-control / counterfactual checks, the pool expansion re-runs the
real gap reasoner and enumerates the planner's own pool, near-misses persist
in the real epistemic store.

The ONLY simulated element is the USER VERDICT at the acceptance step —
every such call is marked SIMULATED_USER in the test name/docstring. The
mechanism is what's proven, not the verdict.
"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "pylib")))

from swarm_engine.services.acceptance import (  # noqa: E402
    AcceptanceLoop, AcceptanceState, AcceptanceStore, Attempt, AuthReport,
    NearMiss, PoolVerdict)
from swarm_engine.services.chat_handler import answer  # noqa: E402
from swarm_engine.services.http_adapter import (  # noqa: E402
    build_services, close_services)
from swarm_engine.intellect.epistemic import EpistemicStore  # noqa: E402
from swarm_engine.core.engine import SwarmEngine  # noqa: E402

GOAL = "compute the sum of the numbers"
ARGS = {"values": [3, 4, 5]}
HELD_OUT_ARGS = {"values": [10, 20, 30]}


def _auth_gate(engine, plan, args, held_out_args, held_out_expected,
               alt_plan=None):
    """A REAL authentication gate: held-out inputs the attempt was not built
    against, a negative control that must fail closed, and (where an
    alternative approach exists) a counterfactual proving the result is
    causally tied to the approach rather than constant."""
    held = engine.composer.execute_sync(plan, held_out_args)
    held_ok = held.get("success") and held.get("value") == held_out_expected
    neg = engine.composer.execute_sync(plan, {"values": "not-a-list"})
    neg_ok = not neg.get("success")  # must fail closed, never confabulate
    cf = None
    cf_ok = True
    if alt_plan is not None:
        alt = engine.composer.execute_sync(alt_plan, args)
        diverges = (alt.get("success") and alt.get("value")
                    != engine.composer.execute_sync(plan, args).get("value"))
        cf = {"alternative_value": alt.get("value"), "diverges": diverges}
        cf_ok = bool(diverges)
    passed = bool(held_ok and neg_ok and cf_ok)
    return AuthReport(
        held_out={"args": held_out_args, "expected": held_out_expected,
                  "observed": held.get("value"), "passed": held_ok},
        negative_controls={"args": {"values": "not-a-list"},
                           "expected": "fail closed",
                           "observed_success": neg.get("success"),
                           "passed": neg_ok},
        counterfactuals=cf,
        passed=passed)


class AcceptanceLoopTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="acceptance_loop_test_")
        self.engine = SwarmEngine()
        self.epistemic = EpistemicStore(os.path.join(self.tmp, "epi.db"))
        self.loop = AcceptanceLoop(
            AcceptanceStore(os.path.join(self.tmp, "acc.db")),
            self.epistemic, engine=self.engine)

    def tearDown(self):
        try:
            self.engine.close()
        finally:
            import shutil
            shutil.rmtree(self.tmp, ignore_errors=True)

    # -- the full loop, end to end --------------------------------------
    def test_full_loop_reject_then_accept_SIMULATED_USER(self):
        """attempt -> completed(candidate) -> SIMULATED dissatisfaction ->
        near-miss -> steered expansion (pool_limited) -> retry ->
        SIMULATED satisfaction -> accepted. Only the verdicts are simulated;
        every transition is real machinery + real store writes."""

        # ROUND 1: real plan, real execution, real auth gate.
        attempt1 = self.loop.attempt(GOAL, ARGS)
        self.assertIn("computation.sum", attempt1.approach_signature)
        self.assertTrue(attempt1.exec_ok)
        self.assertEqual(attempt1.result_summary, 12)
        auth1 = _auth_gate(self.engine, attempt1.plan, ARGS, HELD_OUT_ARGS, 60)
        self.assertTrue(auth1.passed, auth1.as_dict())

        rec = self.loop.present("demo-run-1", GOAL, attempt1, auth1)
        self.assertEqual(rec.state, AcceptanceState.CANDIDATE)
        self.assertEqual(rec.system_status, "completed")
        # completed is a candidate state: the loop is NOT closed.
        self.assertIsNone(rec.decided_at)
        self.assertIsNone(rec.close_reason)

        # SIMULATED USER VERDICT (dissatisfied): the mechanism records it.
        rec = self.loop.record_verdict(
            "demo-run-1", satisfied=False,
            feedback="close but wrong: I wanted the product, not the sum",
            unmet_criteria=["product"])
        self.assertEqual(rec.state, AcceptanceState.REJECTED)
        self.assertEqual(rec.rounds, 2)
        self.assertEqual(len(rec.near_miss_ids), 1)

        # Near-miss record: shape persisted AND retrievable as evidence.
        nms = self.loop.near_misses_for_goal(GOAL)
        self.assertEqual(len(nms), 1)
        nm = nms[0]
        self.assertEqual(nm.near_miss_id, rec.near_miss_ids[0])
        self.assertIn("computation.sum", nm.steers_away_from)
        self.assertEqual(nm.steers_toward, ["product"])
        self.assertIn("product", nm.feedback_text)
        self.assertTrue(nm.close_but_wrong["matched"])
        self.assertIn("product", nm.close_but_wrong["missed"][0])

        # Pool expansion: interrogates the search itself.
        exp = self.loop.expand_pool(GOAL, nm)
        self.assertEqual(exp.verdict, PoolVerdict.POOL_LIMITED)
        # Alias-aware exclusion: `computation.sum` resolves to `sum`.
        self.assertIn("sum", exp.excluded)
        names = [c["name"] for c in exp.candidates]
        self.assertNotIn("sum", names)
        self.assertNotIn("computation.sum", names)
        self.assertEqual(names[0], "product")  # steered toward unmet criteria
        self.assertTrue(exp.re_decomposed_gaps)  # fresh decomposition ran

        # ROUND 2: retry steered toward the unmet criteria. The near-miss
        # constraint is enforced: re-binding the excluded approach raises.
        with self.assertRaises(RuntimeError):
            self.loop.attempt(GOAL, ARGS, exclude=["computation.sum"])
        attempt2 = self.loop.attempt(
            "compute the product of the numbers", ARGS,
            exclude=["computation.sum"])
        self.assertIn("computation.product", attempt2.approach_signature)
        self.assertEqual(attempt2.result_summary, 60)
        auth2 = _auth_gate(self.engine, attempt2.plan, ARGS, HELD_OUT_ARGS, 6000,
                           alt_plan=attempt1.plan)
        self.assertTrue(auth2.passed, auth2.as_dict())
        self.assertTrue(auth2.counterfactuals["diverges"])

        rec = self.loop.present("demo-run-1", GOAL, attempt2, auth2)
        self.assertEqual(rec.state, AcceptanceState.CANDIDATE)

        # SIMULATED USER VERDICT (satisfied): only this closes the loop.
        rec = self.loop.record_verdict("demo-run-1", satisfied=True)
        self.assertEqual(rec.state, AcceptanceState.ACCEPTED)
        self.assertEqual(rec.close_reason, "user_satisfied")
        self.assertIsNotNone(rec.decided_at)

    # -- completed vs accepted are distinct -------------------------------
    def test_completed_is_not_accepted(self):
        attempt = self.loop.attempt(GOAL, ARGS)
        auth = _auth_gate(self.engine, attempt.plan, ARGS, HELD_OUT_ARGS, 60)
        rec = self.loop.present("run-distinct", GOAL, attempt, auth)
        self.assertEqual(rec.system_status, "completed")
        self.assertEqual(rec.state, AcceptanceState.CANDIDATE)
        self.assertNotEqual(rec.state, AcceptanceState.ACCEPTED)

    def test_present_requires_auth_gate(self):
        attempt = self.loop.attempt(GOAL, ARGS)
        bad = AuthReport(passed=False)
        with self.assertRaises(ValueError):
            self.loop.present("run-noauth", GOAL, attempt, bad)

    # -- dissatisfaction after acceptance reopens ------------------------
    def test_reject_after_accept_reopens_SIMULATED_USER(self):
        attempt = self.loop.attempt(GOAL, ARGS)
        auth = _auth_gate(self.engine, attempt.plan, ARGS, HELD_OUT_ARGS, 60)
        self.loop.present("run-reopen", GOAL, attempt, auth)
        rec = self.loop.record_verdict("run-reopen", satisfied=True)
        self.assertEqual(rec.state, AcceptanceState.ACCEPTED)
        # SIMULATED: user changes their mind — the latest word reopens.
        rec = self.loop.record_verdict(
            "run-reopen", satisfied=False,
            feedback="actually the rounding is wrong on negatives",
            unmet_criteria=["negative rounding"])
        self.assertEqual(rec.state, AcceptanceState.REJECTED)
        self.assertEqual(rec.rounds, 2)
        self.assertEqual(len(rec.near_miss_ids), 1)
        # Terminal stays terminal: no verdict reopens it.
        self.loop.close_terminal("run-reopen", "his decision")
        with self.assertRaises(ValueError):
            self.loop.record_verdict("run-reopen", satisfied=True)

    # -- terminal conditions ----------------------------------------------
    def test_terminal_condition_closes_without_user(self):
        attempt = self.loop.attempt(GOAL, ARGS)
        auth = _auth_gate(self.engine, attempt.plan, ARGS, HELD_OUT_ARGS, 60)
        self.loop.present("run-term", GOAL, attempt, auth)
        rec = self.loop.close_terminal(
            "run-term",
            "genuinely unavailable substrate: no execution substrate for "
            "the required effect exists on this machine")
        self.assertEqual(rec.state, AcceptanceState.CLOSED_TERMINAL)
        self.assertIn("unavailable substrate", rec.close_reason)

    # -- genuinely-absent classification ----------------------------------
    def test_genuinely_absent_when_pool_exhausted(self):
        pool_names = [p.name for p in
                      self.engine.primitives.producing(
                          __import__("swarm_engine.synthesis.planner",
                                     fromlist=["parse_goal"]).parse_goal(GOAL).wants_type)
                      if p.pure]
        nm = NearMiss(
            near_miss_id="nm_exhaust", run_id="r", goal=GOAL,
            attempt={"approach_signature": pool_names}, auth={"passed": True},
            feedback_text="none of these work", unmet_criteria=[],
            close_but_wrong={"matched": [], "missed": ["everything"]},
            steers_away_from=list(pool_names), steers_toward=[])
        exp = self.loop.expand_pool(GOAL, nm)
        self.assertEqual(exp.verdict, PoolVerdict.GENUINELY_ABSENT)
        self.assertEqual(exp.candidates, [])

    # -- chat surface: accept / reject kinds -------------------------------
    def test_chat_accept_reject_kinds(self):
        svc = build_services(self.tmp)
        svc["acceptance"] = self.loop
        try:
            attempt = self.loop.attempt(GOAL, ARGS)
            auth = _auth_gate(self.engine, attempt.plan, ARGS, HELD_OUT_ARGS, 60)
            self.loop.present("chat-run-1", GOAL, attempt, auth)

            out = answer("what is the status of run chat-run-1", svc)
            self.assertIn("candidate", out["text"])

            out = answer("reject run chat-run-1 because I wanted the "
                         "product not the sum", svc)
            self.assertEqual(out["kind"], "reject_run")
            self.assertEqual(out["grounded"]["state"], "rejected")
            self.assertIsNotNone(out["grounded"]["near_miss_id"])
            self.assertIn("product", out["grounded"]["feedback"])

            rec = self.loop.store.get("chat-run-1")
            self.assertEqual(rec.state, AcceptanceState.REJECTED)
            nms = self.loop.near_misses_for_goal(GOAL)
            self.assertTrue(any(n.run_id == "chat-run-1" for n in nms))

            out = answer("accept run chat-run-1", svc)
            self.assertEqual(out["kind"], "accept_run")
            self.assertEqual(out["grounded"]["state"], "accepted")

            out = answer("what is the status of run chat-run-1", svc)
            self.assertIn("accepted", out["text"])

            # Unknown run: honest refusal, no invented verdict.
            out = answer("accept run no-such-run", svc)
            self.assertIn("can't accept", out["text"].lower())
        finally:
            close_services(svc)

    def test_verdict_kinds_write_only_the_overlay(self):
        """Accept/reject must not mutate the capability, evidence, or
        scheduler DBs — the acceptance overlay is the only write target."""
        import sqlite3
        svc = build_services(self.tmp)
        svc["acceptance"] = self.loop

        def content(path):
            if not os.path.exists(path):
                return None
            con = sqlite3.connect(path)
            try:
                tabs = [r[0] for r in con.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'")]
                return {t: sorted(map(repr, con.execute(
                    f'SELECT * FROM "{t}"').fetchall())) for t in tabs}
            finally:
                con.close()

        dbs = [os.path.join(self.tmp, n) for n in
               ("capabilities.db", "evidence.db", "scheduler.db")]
        try:
            attempt = self.loop.attempt(GOAL, ARGS)
            auth = _auth_gate(self.engine, attempt.plan, ARGS, HELD_OUT_ARGS, 60)
            self.loop.present("chat-run-2", GOAL, attempt, auth)
            before = {p: content(p) for p in dbs}
            answer("reject run chat-run-2 because wrong", svc)
            answer("accept run chat-run-2", svc)
            after = {p: content(p) for p in dbs}
            self.assertEqual(before, after)
        finally:
            close_services(svc)


if __name__ == "__main__":
    unittest.main()
