"""
Causal tests for swarm_engine.services.scheduler.RunScheduler.

Anti-simulation: every test runs the real SwarmEngine on the real
UniversalTaskInterface pipeline with real goals against scratch sqlite DBs
(tempfile). Nothing is mocked except in the causality spot-check, where the
control-installation mechanism itself is the thing under test.

Slow goal used for preemption tests: "compute the nth prime number" with
(input, output) tuple examples. On a fresh engine this drives the real
example-driven acquisition path, which runs ~85s through the scheduler
before failing honestly (nth-prime is not polynomial-synthesizable; the
candidates fail independent validation) — a genuinely slow real path we
interrupt early via stop/pause/cancel.

Note (2026-09-27): the previous slow goal, "compute the triple of n", is
now genuinely acquirable via the verdict-bound promotion path (it succeeds
in ~68s through the scheduler), so it no longer serves as the
"slow-and-honestly-failing" vehicle. The test premise went stale because the
system got more capable, not less.

Quick goal: "knowledge_stats" — real pipeline, ~0.02s, success=True, no
payload or examples needed.
"""
import sys
import tempfile
import threading
import time
import unittest

import os as _os, sys as _sys
_sys.path.insert(0, _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "..", "..", "pylib"))

from swarm_engine.services import scheduler as sched_mod
from swarm_engine.services.scheduler import RunScheduler

QUICK_GOAL = "knowledge_stats"


def _nth_prime(n):
    """The nth prime (1-indexed). Used to build SLOW_EXAMPLES."""
    count, c = 0, 2
    while True:
        if all(c % i for i in range(2, int(c ** 0.5) + 1)):
            count += 1
            if count == n:
                return c
        c += 1


SLOW_GOAL = "compute the nth prime number"
SLOW_EXAMPLES = [({"n": i}, _nth_prime(i)) for i in range(1, 11)]

TERMINAL = ("completed", "failed", "stopped", "cancelled", "error")


def _wait_for(sched, run_id, want, timeout=60.0):
    """Poll get_run until status in want (or timeout). Returns the record."""
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        last = sched.get_run(run_id)
        if last is not None and last["status"] in want:
            return last
        time.sleep(0.05)
    raise AssertionError(
        f"run {run_id} never reached {want}; last status="
        f"{last['status'] if last else None}")


class SchedulerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        import os
        self.sched = RunScheduler(
            os.path.join(self.tmp.name, "sched.db"),
            os.path.join(self.tmp.name, "rt.db"),
        )

    def tearDown(self):
        try:
            self.sched.close()
        finally:
            self.tmp.cleanup()

    # -- FIFO serialization ------------------------------------------------
    def test_fifo_order_and_no_overlap(self):
        rids = []
        for _ in range(3):
            r = self.sched.submit(QUICK_GOAL)
            self.assertTrue(r["ok"], r)
            rids.append(r["run_id"])
        recs = [_wait_for(self.sched, rid, TERMINAL) for rid in rids]

        for rec in recs:
            self.assertEqual(rec["status"], "completed", rec)

        # Completion order matches submission order (single FIFO worker).
        by_start = sorted(recs, key=lambda r: r["started_at"])
        self.assertEqual([r["id"] for r in by_start], rids)

        # No two runs ever overlap: each ended before the next started.
        for first, second in zip(by_start, by_start[1:]):
            self.assertIsNotNone(first["ended_at"])
            self.assertIsNotNone(second["started_at"])
            self.assertLessEqual(first["ended_at"], second["started_at"],
                                 f"overlap: {first['id']} vs {second['id']}")

        # Queue positions were honest while queued (all drained by now).
        self.assertEqual(self.sched.queue_depth(), 0)

    # -- stop an active run; queue advances --------------------------------
    def test_stop_active_run_advances_queue(self):
        slow = self.sched.submit(SLOW_GOAL, examples=SLOW_EXAMPLES)
        self.assertTrue(slow["ok"], slow)
        slow_id = slow["run_id"]
        _wait_for(self.sched, slow_id, ("running",))
        time.sleep(0.5)  # let it get inside the pipeline

        quick = self.sched.submit(QUICK_GOAL)
        self.assertTrue(quick["ok"], quick)
        quick_id = quick["run_id"]
        self.assertEqual(self.sched.get_run(quick_id)["status"], "queued")
        self.assertEqual(self.sched.queue_depth(), 1)

        stop_r = self.sched.stop_run(slow_id)
        self.assertTrue(stop_r["ok"], stop_r)
        self.assertEqual(stop_r["status"], "stopping")
        self.assertEqual(self.sched.get_run(slow_id)["status"], "stopping")

        slow_rec = _wait_for(self.sched, slow_id, ("stopped",), timeout=90.0)
        self.assertEqual(slow_rec["status"], "stopped")
        # The STOPPED marker from handle() survived into the persisted outcome.
        trace = (slow_rec["outcome"] or {}).get("trace") or []
        stages = [t.get("stage") for t in trace]
        self.assertIn("stopped", stages,
                      f"no STOPPED stage in trace: {stages}")

        # The queue advanced: the quick run executed only after the slow one
        # ended, and completed on the still-healthy engine.
        quick_rec = _wait_for(self.sched, quick_id, TERMINAL)
        self.assertEqual(quick_rec["status"], "completed", quick_rec)
        self.assertGreaterEqual(quick_rec["started_at"], slow_rec["ended_at"])

    # -- cancel a queued run: never executes --------------------------------
    def test_cancel_queued_never_runs(self):
        slow = self.sched.submit(SLOW_GOAL, examples=SLOW_EXAMPLES)
        slow_id = slow["run_id"]
        _wait_for(self.sched, slow_id, ("running",))

        quick = self.sched.submit(QUICK_GOAL)
        quick_id = quick["run_id"]
        self.assertEqual(self.sched.get_run(quick_id)["status"], "queued")

        c = self.sched.cancel_run(quick_id)
        self.assertTrue(c["ok"], c)
        self.assertEqual(c["status"], "cancelled")
        self.assertEqual(self.sched.queue_depth(), 0)

        # Cancelling anything but a queued run is an honest error.
        self.assertFalse(self.sched.cancel_run(slow_id)["ok"])
        self.assertFalse(self.sched.cancel_run(quick_id)["ok"])  # twice

        # Stop the slow run so the worker drains; the cancelled run must
        # never have executed: no started_at, no stage events.
        self.assertTrue(self.sched.stop_run(slow_id)["ok"])
        _wait_for(self.sched, slow_id, ("stopped",), timeout=90.0)
        time.sleep(0.5)  # give the worker a chance to (not) pick it up
        rec = self.sched.get_run(quick_id)
        self.assertEqual(rec["status"], "cancelled")
        self.assertIsNone(rec["started_at"])
        evs = self.sched.events(quick_id) or []
        kinds = [(e.get("type"), e.get("stage") or e.get("status"))
                 for e in evs]
        self.assertTrue(all(k[0] == "status" for k in kinds),
                        f"cancelled run has non-status events: {kinds}")

    # -- pause / resume an active run ---------------------------------------
    def test_pause_resume_active_run(self):
        slow = self.sched.submit(SLOW_GOAL, examples=SLOW_EXAMPLES)
        slow_id = slow["run_id"]
        _wait_for(self.sched, slow_id, ("running",))
        time.sleep(0.5)

        p = self.sched.pause_run(slow_id)
        self.assertTrue(p["ok"], p)
        self.assertEqual(p["status"], "paused")
        self.assertEqual(self.sched.get_run(slow_id)["status"], "paused")
        # Pausing a paused run is an honest error, not a silent no-op.
        self.assertFalse(self.sched.pause_run(slow_id)["ok"])

        # The run stays parked (not racing to terminal) while paused.
        time.sleep(2.0)
        self.assertEqual(self.sched.get_run(slow_id)["status"], "paused")

        r = self.sched.resume_run(slow_id)
        self.assertTrue(r["ok"], r)
        self.assertEqual(self.sched.get_run(slow_id)["status"], "running")
        # Resuming a running (not paused) run is an honest error.
        self.assertFalse(self.sched.resume_run(slow_id)["ok"])

        # Stop overrides the pause machinery: the worker was parked inside a
        # checkpoint, so the stop must land promptly (proves the pause had
        # genuinely engaged the worker, not just relabeled the status).
        t0 = time.time()
        self.assertTrue(self.sched.stop_run(slow_id)["ok"])
        rec = _wait_for(self.sched, slow_id, ("stopped",), timeout=90.0)
        dt = time.time() - t0
        self.assertEqual(rec["status"], "stopped")
        self.assertLess(dt, 30.0, f"stop-after-pause took {dt:.1f}s")

    # -- concurrent submitters: all serialized, all terminal -----------------
    def test_concurrent_submits_serialized(self):
        n = 8
        results = [None] * n

        def worker(i):
            results[i] = self.sched.submit(QUICK_GOAL)

        threads = [threading.Thread(target=worker, args=(i,))
                    for i in range(n)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        rids = []
        for r in results:
            self.assertTrue(r["ok"], r)
            rids.append(r["run_id"])
        self.assertEqual(len(set(rids)), n, "duplicate run ids")

        recs = [_wait_for(self.sched, rid, TERMINAL) for rid in rids]
        for rec in recs:
            self.assertEqual(rec["status"], "completed", rec)

        by_start = sorted(recs, key=lambda r: r["started_at"])
        for first, second in zip(by_start, by_start[1:]):
            self.assertLessEqual(first["ended_at"], second["started_at"],
                                 "concurrent runs overlapped")

    # -- causality: stop flows through the control mechanism ------------------
    def test_causality_stop_needs_control_installation(self):
        """With control installation disabled, stop has no effect.

        handle() (feature-1 wiring) installs the metadata-passed control via
        run_control.set_current, and the scheduler worker installs it too;
        every checkpoint() in the pipeline reads that contextvar. Neutralize
        installation at both points -> the stop request cannot reach the run.
        """
        import swarm_engine.services.run_control as rc_mod
        import swarm_engine.core.task_interface as ti_mod

        real_rc_set = rc_mod.set_current
        real_ti_set = ti_mod._set_current_run_control
        rc_mod.set_current = lambda control: None
        ti_mod._set_current_run_control = lambda control: None
        try:
            slow = self.sched.submit(SLOW_GOAL, examples=SLOW_EXAMPLES)
            slow_id = slow["run_id"]
            _wait_for(self.sched, slow_id, ("running",))
            time.sleep(0.5)

            stop_r = self.sched.stop_run(slow_id)
            self.assertTrue(stop_r["ok"], stop_r)
            # The request is recorded (status 'stopping') but the run does
            # not observe it: no checkpoint can fire without an installed
            # control.
            time.sleep(5.0)
            mid = self.sched.get_run(slow_id)
            self.assertEqual(mid["status"], "stopping",
                             f"stop took effect without a control: {mid}")
        finally:
            rc_mod.set_current = real_rc_set
            ti_mod._set_current_run_control = real_ti_set

        # Mechanism restored: the still-pending stop request now lands at the
        # next checkpoint and the run ends 'stopped' (also drains the worker).
        # Timeout 150s: the slow goal runs ~85s before failing honestly; the
        # stop then resolves via _classify (stop_requested and not success).
        rec = _wait_for(self.sched, slow_id, ("stopped",), timeout=150.0)
        self.assertEqual(rec["status"], "stopped")

    # -- honest errors for misuse ----------------------------------------------
    def test_misuse_returns_ok_false(self):
        self.assertIsNone(self.sched.get_run("no-such-run"))
        self.assertIsNone(self.sched.events("no-such-run"))
        for fn in (self.sched.pause_run, self.sched.resume_run,
                   self.sched.stop_run, self.sched.cancel_run):
            r = fn("no-such-run")
            self.assertFalse(r["ok"], fn)
            self.assertIn("error", r)

        r = self.sched.submit("   ")
        self.assertFalse(r["ok"])

        # stop on a terminal run is an honest error.
        q = self.sched.submit(QUICK_GOAL)
        qid = q["run_id"]
        _wait_for(self.sched, qid, TERMINAL)
        self.assertFalse(self.sched.stop_run(qid)["ok"])
        self.assertFalse(self.sched.pause_run(qid)["ok"])

        # stop on a queued run suggests cancel instead of silently working.
        slow = self.sched.submit(SLOW_GOAL, examples=SLOW_EXAMPLES)
        slow_id = slow["run_id"]
        _wait_for(self.sched, slow_id, ("running",))
        q2 = self.sched.submit(QUICK_GOAL)
        q2id = q2["run_id"]
        sr = self.sched.stop_run(q2id)
        self.assertFalse(sr["ok"])
        self.assertIn("cancel", sr["error"])
        self.assertTrue(self.sched.cancel_run(q2id)["ok"])
        self.assertTrue(self.sched.stop_run(slow_id)["ok"])
        _wait_for(self.sched, slow_id, ("stopped",), timeout=90.0)

    # -- query surface ------------------------------------------------------------
    def test_list_runs_and_events(self):
        r1 = self.sched.submit(QUICK_GOAL, conversation_id="conv-1")
        r2 = self.sched.submit(QUICK_GOAL)
        _wait_for(self.sched, r1["run_id"], TERMINAL)
        _wait_for(self.sched, r2["run_id"], TERMINAL)

        rows = self.sched.list_runs(limit=10)
        self.assertGreaterEqual(len(rows), 2)
        by_id = {row["id"]: row for row in rows}
        self.assertEqual(by_id[r1["run_id"]]["conversation_id"], "conv-1")
        self.assertEqual(by_id[r1["run_id"]]["status"], "completed")
        for row in rows:
            for key in ("id", "goal", "status", "queue_position",
                        "queued_at", "started_at", "ended_at"):
                self.assertIn(key, row)

        full = self.sched.get_run(r1["run_id"])
        self.assertIn("outcome", full)
        self.assertTrue(full["outcome"]["success"])
        stages = [t["stage"] for t in full["outcome"]["trace"]]
        self.assertIn("understand", stages)
        self.assertIn("persist", stages)

        evs = self.sched.events(r1["run_id"])
        self.assertIsInstance(evs, list)
        stage_evs = [e for e in evs if e.get("type") == "stage"]
        self.assertGreater(len(stage_evs), 5,
                           "expected real _trace stage events buffered")
        self.assertTrue(all(e["run_id"] == r1["run_id"] for e in evs))


if __name__ == "__main__":
    unittest.main(verbosity=2)
