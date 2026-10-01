#!/usr/bin/env python3
"""CAUSAL-02: the DumbScheduler is subordinate -- it cannot drive the
Controller, fire on its own, or push the Controller past its budget.

Adversarial claims under test:
  A. STRUCTURAL: the scheduler holds no reference to the controller
     (its __dict__ is just the interval). It has no path to invoke
     tick() -- there is nothing to call.
  B. NO SELF-FIRE: wait_until_next_tick is a blocking sleep on a
     stop_event. With the event unset and a positive interval, it blocks
     (does not return promptly); with the event set, it returns False
     (stop reported, not decided -- the decision to stop lives in the
     Controller's run loop).
  C. ADVERSARIAL INTERVAL 0: a scheduler that "fires constantly"
     (interval_s=0) cannot push the Controller past its authorized
     run budget. run() still terminates with budget_exhausted=True and
     a bounded cycle count -- every stop condition (stop event,
     max_cycles, run_deadline) is evaluated by the Controller, never
     by the scheduler.

Exit 0 only if all three hold on the real machinery.
"""

import os
import sys
import tempfile
import threading
import time

WT = "/home/hatch/workspace/worktrees/warm-v10-convergence"
sys.path.insert(0, os.path.join(WT, "pylib"))
sys.path.insert(0, WT)

from swarm_engine.core.engine import SwarmEngine
from swarm_engine.core.run_controller import (
    RunController, RunConfig, DumbScheduler)

PASSED = 0


def check(cond, msg):
    global PASSED
    if not cond:
        print(f"FAIL: {msg}")
        sys.exit(1)
    PASSED += 1
    print(f"ok [{PASSED}]: {msg}")


# --- A: structural -- the scheduler cannot reach the controller ---------
sched = DumbScheduler(interval_s=60.0)
check(set(sched.__dict__.keys()) == {"_interval"},
      f"scheduler state is only the interval {sched.__dict__}: "
      "no controller reference, no callback, no path to tick()")
check(not hasattr(sched, "tick") and not hasattr(sched, "fire"),
      "scheduler exposes no tick/fire/callback entry point at all")

# --- B: no self-fire ------------------------------------------------------
ev = threading.Event()
t0 = time.monotonic()
# interval 60s, event unset: must block (not return promptly).
# We probe with a 0.3s join on a thread to avoid hanging the test.
probe = threading.Thread(
    target=lambda: ev_unset_result.append(sched.wait_until_next_tick(ev)))
ev_unset_result = []
probe.daemon = True
probe.start()
probe.join(timeout=1.0)
check(probe.is_alive(),
      "scheduler with unset stop_event BLOCKS (does not self-fire); "
      "still blocked after 1.0s of a 60s interval")
ev.set()  # now release the probe thread
probe.join(timeout=5.0)

ev2 = threading.Event()
ev2.set()
check(sched.wait_until_next_tick(ev2) is False,
      "scheduler with set stop_event returns False promptly "
      "(reports stop; the stop DECISION lives in the Controller)")

# --- C: adversarial interval 0 vs the Controller's budget -----------------
td = tempfile.mkdtemp(prefix="rcv10_causal02_")
eng = SwarmEngine(db_path=os.path.join(td, "eng.db"))
cfg = RunConfig(cadence_interval_s=0.0,   # ADVERSARIAL: fire constantly
                run_budget_s=3.0,         # tiny authorized budget
                cycle_budget_s=1.0, gap_budget_s=0.2,
                max_cycles=1000000,       # no cycle-count backstop
                max_gaps_per_cycle=5, trusted_indexes=[],
                staging_dir=os.path.join(td, "staging"))
rc = RunController(eng, config=cfg,
                   checkpoint_path=os.path.join(td, "ckpt.db"),
                   registry=None)
# The controller builds its scheduler from the authorized cadence interval.
check(abs(rc._scheduler._interval - 0.0) < 1e-9,
      "controller's scheduler carries the adversarial 0s interval")
t0 = time.monotonic()
report = rc.run()
elapsed = time.monotonic() - t0
check(report.get("budget_exhausted") is True,
      "run() with a constantly-firing scheduler still stopped at "
      "budget_exhausted=True (the Controller's deadline, not the "
      "scheduler's firing, governs)")
check(elapsed < 30.0,
      f"run() terminated promptly ({elapsed:.1f}s): the 0-interval "
      "scheduler could not spin the Controller past its budget")
check(len(report.get("cycles", [])) < 1000000,
      f"cycle count bounded ({len(report.get('cycles', []))}): "
      "no runaway under adversarial firing")

print(f"\nCAUSAL-02 PROVEN: scheduler subordinate on all three adversarial "
      f"claims ({PASSED}/7 checks).")
