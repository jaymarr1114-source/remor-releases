"""Causal tests for swarm_engine.services.tasks_api.ScheduledTaskService.

Real RunScheduler, real SwarmEngine, real sqlite — scratch DBs under
tempfile, nothing touches production state.

Goals (same as test_scheduler.py):
  QUICK_GOAL "knowledge_stats" — real pipeline, ~0.02s, success=True.
  SLOW_GOAL  "compute the triple of n" + tuple examples — drives the real
  example-driven acquisition path (~60s), interrupted early for
  cancel-of-running tests.

Causality spot-checks (revert->fails / apply->holds):
  * dispatcher disabled  -> a due task never dispatches (revert->fails).
  * dispatcher enabled   -> it does NOT run before its due timestamp and
                            DOES run after it (apply->holds), with the
                            scheduler's own started_at >= run_at.
  * cancel a pending due task -> it never dispatches, even past its due.
  * cancel of queued/running immediate tasks delegates to the
    scheduler's real cancel_run/stop_run (verified verbatim).
  * a fresh service instance over the same DB picks up a pending row
    (sqlite-persisted due timestamps survive process death).
"""
import os
import sys
import tempfile
import time
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.normpath(
    os.path.join(_HERE, "..", "..", "pylib")))

from swarm_engine.services.scheduler import RunScheduler  # noqa: E402
from swarm_engine.services.tasks_api import (  # noqa: E402
    ScheduledTaskService,
    routes_for_tasks_api,
)

QUICK_GOAL = "knowledge_stats"
SLOW_GOAL = "compute the triple of n"
SLOW_EXAMPLES = [({"n": 4}, 12), ({"n": 7}, 21)]

TERMINAL = ("completed", "failed", "stopped", "cancelled", "error")


def _wait_status(sched, run_id, want, timeout=90.0):
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        last = sched.get_run(run_id)
        if last is not None and last["status"] in want:
            return last
        time.sleep(0.05)
    raise AssertionError(
        f"run {run_id} never reached {want}; last="
        f"{last['status'] if last else None}")


def _wait_task(svc, task_id, want_statuses, timeout=30.0):
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        last = svc.get_task(task_id)["task"]
        if last["status"] in want_statuses:
            return last
        time.sleep(0.05)
    raise AssertionError(
        f"task {task_id} never reached {want_statuses}; last="
        f"{last['status'] if last else None}")


def _wait_task_run_id(svc, task_id, timeout=30.0):
    """Wait until the task row carries a scheduler run_id.

    The dispatcher claims due rows as status='submitted', run_id=NULL and
    only populates run_id after the scheduler accepts the submission (see
    tasks_api._dispatch_due: claim inside the lock, submit outside it).
    Under load the gap is observable, so polling for status='submitted'
    alone races a None run_id. This waits for the actual precondition.
    """
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        last = svc.get_task(task_id)["task"]
        if last["run_id"]:
            return last
        time.sleep(0.05)
    raise AssertionError(
        f"task {task_id} never got a run_id; last={last}")


class TasksApiTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.sched = RunScheduler(
            os.path.join(self.tmp.name, "sched.db"),
            os.path.join(self.tmp.name, "rt.db"))
        self.svc = ScheduledTaskService(
            os.path.join(self.tmp.name, "tasks.db"), self.sched)

    def tearDown(self):
        try:
            self.svc.close()
            self.sched.close()
        finally:
            self.tmp.cleanup()

    # -- immediate scheduling -------------------------------------------
    def test_immediate_schedule_completes(self):
        r = self.svc.schedule_task(QUICK_GOAL)
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["status"], "submitted")
        self.assertIsNotNone(r["run_id"])

        rec = _wait_status(self.sched, r["run_id"], TERMINAL)
        self.assertEqual(rec["status"], "completed", rec)

        task = self.svc.get_task(r["task_id"])["task"]
        self.assertEqual(task["run_status"], "completed", task)
        self.assertEqual(task["status"], "submitted")

        listed = self.svc.list_tasks()["tasks"]
        self.assertTrue(any(t["task_id"] == r["task_id"]
                            for t in listed))

    def test_past_run_at_dispatches_immediately(self):
        r = self.svc.schedule_task(QUICK_GOAL, run_at=time.time() - 10)
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["status"], "submitted", r)
        self.assertIsNotNone(r["run_id"])

    def test_run_at_iso_string(self):
        due = time.time() + 3600
        iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(due))
        r = self.svc.schedule_task(QUICK_GOAL, run_at=iso)
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["status"], "scheduled", r)
        self.assertAlmostEqual(r["run_at"], due, delta=2.0)
        # Clean up: cancel so nothing lingers.
        c = self.svc.cancel_task(r["task_id"])
        self.assertTrue(c["ok"], c)

    # -- delayed dispatch: real timing ----------------------------------
    def test_delayed_dispatch_does_not_run_early(self):
        due = time.time() + 2.0
        r = self.svc.schedule_task(QUICK_GOAL, run_at=due,
                                   conversation_id="conv-1")
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["status"], "scheduled")
        self.assertIsNone(r["run_id"])
        task_id = r["task_id"]

        # At t+1s (well before due): still pending, no scheduler run.
        time.sleep(1.0)
        task = self.svc.get_task(task_id)["task"]
        self.assertEqual(task["status"], "pending", task)
        self.assertIsNone(task["run_id"], task)
        qs = self.svc.queue_status()
        self.assertEqual(qs["pending_scheduled"], 1, qs)
        self.assertIsNotNone(qs["next_due_at"])

        # After due: the dispatcher submits it to the real scheduler.
        task = _wait_task(self.svc, task_id, ("submitted",), timeout=15.0)
        self.assertIsNotNone(task["run_id"], task)
        rec = _wait_status(self.sched, task["run_id"], TERMINAL)
        self.assertEqual(rec["status"], "completed", rec)
        # The scheduler only saw it after the due timestamp.
        self.assertGreaterEqual(rec["started_at"], due - 0.05,
                                f"started {rec['started_at']} before due "
                                f"{due}")
        self.assertEqual(rec["conversation_id"], "conv-1", rec)

    def test_dispatcher_disabled_never_dispatches(self):
        """Causal revert->fails: without the dispatcher, a due task
        sits pending forever — dispatch is real machinery, not magic."""
        svc2 = ScheduledTaskService(
            os.path.join(self.tmp.name, "tasks2.db"), self.sched,
            dispatcher_enabled=False)
        try:
            r = svc2.schedule_task(QUICK_GOAL, run_at=time.time() + 1.0)
            self.assertTrue(r["ok"], r)
            time.sleep(2.5)  # past the due timestamp
            task = svc2.get_task(r["task_id"])["task"]
            self.assertEqual(task["status"], "pending", task)
            self.assertIsNone(task["run_id"], task)
        finally:
            svc2.close()

    def test_pending_row_survives_service_restart(self):
        """Due rows are sqlite-persisted: a fresh instance over the same
        DB picks them up (process-death recovery)."""
        db = os.path.join(self.tmp.name, "tasks3.db")
        dead = ScheduledTaskService(db, self.sched,
                                    dispatcher_enabled=False)
        r = dead.schedule_task(QUICK_GOAL, run_at=time.time() + 1.0)
        self.assertTrue(r["ok"], r)
        dead.close()  # "process death": dispatcher never ran

        revived = ScheduledTaskService(db, self.sched)
        try:
            # Wait for the run_id itself, not just status='submitted': the
            # dispatcher claims rows as submitted/NULL before the scheduler
            # accepts the submission (see _wait_task_run_id).
            task = _wait_task_run_id(revived, r["task_id"], timeout=30.0)
            rec = _wait_status(self.sched, task["run_id"], TERMINAL)
            self.assertEqual(rec["status"], "completed", rec)
        finally:
            revived.close()

    def test_cancel_pending_never_dispatches(self):
        due = time.time() + 3.0
        r = self.svc.schedule_task(QUICK_GOAL, run_at=due)
        self.assertTrue(r["ok"], r)
        time.sleep(0.5)
        c = self.svc.cancel_task(r["task_id"])
        self.assertTrue(c["ok"], c)
        self.assertEqual(c["status"], "cancelled", c)

        time.sleep(3.0)  # past the due timestamp
        task = self.svc.get_task(r["task_id"])["task"]
        self.assertEqual(task["status"], "cancelled", task)
        self.assertIsNone(task["run_id"], task)
        self.assertEqual(self.svc.queue_status()["pending_scheduled"], 0)

    # -- cancel delegates to the real scheduler -------------------------
    def test_cancel_queued_and_running_delegates_to_scheduler(self):
        slow = self.svc.schedule_task(SLOW_GOAL, examples=SLOW_EXAMPLES)
        self.assertTrue(slow["ok"], slow)
        _wait_status(self.sched, slow["run_id"], ("running",))

        quick = self.svc.schedule_task(QUICK_GOAL)
        self.assertTrue(quick["ok"], quick)
        time.sleep(0.5)  # let it settle into queued behind the slow run
        self.assertEqual(self.sched.get_run(quick["run_id"])["status"],
                         "queued")

        # Queued -> the scheduler's real cancel_run, verbatim.
        c = self.svc.cancel_task(quick["task_id"])
        self.assertTrue(c["ok"], c)
        self.assertTrue(c["scheduler"]["ok"], c)
        self.assertEqual(c["scheduler"]["status"], "cancelled", c)
        self.assertEqual(
            self.sched.get_run(quick["run_id"])["status"], "cancelled")

        # Running -> the scheduler's real stop_run -> stopped terminal.
        c2 = self.svc.cancel_task(slow["task_id"])
        self.assertTrue(c2["ok"], c2)
        self.assertTrue(c2["scheduler"]["ok"], c2)
        rec = _wait_status(self.sched, slow["run_id"], TERMINAL,
                           timeout=60.0)
        self.assertEqual(rec["status"], "stopped", rec)

    # -- queue status ----------------------------------------------------
    def test_queue_status_counts(self):
        r1 = self.svc.schedule_task(QUICK_GOAL)
        r2 = self.svc.schedule_task(QUICK_GOAL, run_at=time.time() + 600)
        self.assertTrue(r1["ok"] and r2["ok"])
        _wait_status(self.sched, r1["run_id"], TERMINAL)

        qs = self.svc.queue_status()
        self.assertTrue(qs["ok"], qs)
        for key in ("depth", "active", "counts", "pending_scheduled",
                    "next_due_at"):
            self.assertIn(key, qs, qs)
        self.assertEqual(qs["pending_scheduled"], 1, qs)
        self.assertIsNotNone(qs["next_due_at"])
        self.assertGreaterEqual(qs["counts"].get("completed", 0), 1, qs)
        # counts cover every scheduler run on the books
        total = sum(qs["counts"].values())
        self.assertGreaterEqual(total, 1)

    # -- bad input is honest ---------------------------------------------
    def test_bad_inputs(self):
        self.assertFalse(self.svc.schedule_task("")["ok"])
        self.assertFalse(self.svc.schedule_task("   ")["ok"])
        self.assertFalse(
            self.svc.schedule_task(QUICK_GOAL, run_at="not-a-time")["ok"])
        self.assertFalse(self.svc.cancel_task("no-such-task")["ok"])
        self.assertFalse(self.svc.get_task("no-such-task")["ok"])
        # Cancelling twice: second is an honest refusal.
        r = self.svc.schedule_task(QUICK_GOAL, run_at=time.time() + 600)
        self.assertTrue(self.svc.cancel_task(r["task_id"])["ok"])
        self.assertFalse(self.svc.cancel_task(r["task_id"])["ok"])

    # -- recurring: honestly unavailable ----------------------------------
    def test_recurring_is_typed_unavailable(self):
        r = self.svc.schedule_recurring()
        self.assertFalse(r["ok"])
        un = r["unavailable"]
        self.assertEqual(un["code"], "recurring_schedule")
        self.assertEqual(un["gui"], "coming_soon")
        self.assertIn("cron", un["missing_substrate"].lower()
                      or un["reason"].lower())
        self.assertTrue(un["reason"] and un["missing_substrate"])

    # -- route table: real handlers, JSON in/out --------------------------
    def test_routes_for_tasks_api(self):
        routes = routes_for_tasks_api(self.svc)
        self.assertEqual(len(routes), 6)

        created = routes[("POST", "/api/tasks")]({"goal": QUICK_GOAL})
        self.assertTrue(created["ok"], created)
        task_id = created["task_id"]

        listed = routes[("GET", "/api/tasks")]({})
        self.assertTrue(listed["ok"])
        self.assertTrue(any(t["task_id"] == task_id
                            for t in listed["tasks"]))

        got = routes[("GET", "/api/tasks/item")]({"task_id": task_id})
        self.assertTrue(got["ok"], got)
        self.assertEqual(got["task"]["task_id"], task_id)

        qs = routes[("GET", "/api/tasks/queue")]({})
        self.assertTrue(qs["ok"] and "counts" in qs, qs)

        un = routes[("POST", "/api/tasks/recurring")](
            {"goal": QUICK_GOAL, "every": "1h"})
        self.assertFalse(un["ok"])
        self.assertEqual(un["unavailable"]["code"], "recurring_schedule")

        missing = routes[("GET", "/api/tasks/item")]({})
        self.assertFalse(missing["ok"])

        cancelled = routes[("POST", "/api/tasks/cancel")](
            {"task_id": task_id})
        # The quick run may already be terminal or still queued/running;
        # either the cancel succeeds or the refusal names the real state.
        self.assertIn("ok", cancelled)
        if not cancelled["ok"]:
            self.assertIn("error", cancelled)

        import json as _json
        _json.dumps(created); _json.dumps(listed); _json.dumps(qs)


if __name__ == "__main__":
    unittest.main()
