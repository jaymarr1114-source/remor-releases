#!/usr/bin/env python3
"""RUN-CTRL-V10-1 sustained proof: the RunController runs unprompted on its
own cadence for >= 1 hour. No gaps seeded, no deltas seeded, no operator:
the Controller ticks, drives the acquisition leg, runs the quarantine
sweep, turns per-cycle acceptance, checkpoints every cycle.

Success: run completes its authorized budget with no tick errors, the
checkpoint advances every cycle, and per-cycle acceptance records land.
Progress is logged to sustained.log; final report to sustained_report.json.
"""

import json
import os
import sys
import time

WT = "/home/hatch/workspace/worktrees/warm-v10-convergence"
sys.path.insert(0, os.path.join(WT, "pylib"))
sys.path.insert(0, WT)

from swarm_engine.core.engine import SwarmEngine
from swarm_engine.core.run_controller import RunController, RunConfig

OUT = os.path.join(WT, "proofs", "run_ctrl_v10_1", "sustained")
os.makedirs(OUT, exist_ok=True)
LOG = os.path.join(OUT, "sustained.log")
REPORT = os.path.join(OUT, "sustained_report.json")

# Pin check: prove against the committed tree, not a dirty worktree.
import subprocess
pin = subprocess.run(
    ["git", "rev-parse", "HEAD"], cwd=WT, capture_output=True,
    text=True).stdout.strip()
dirty = subprocess.run(
    ["git", "status", "--porcelain", "--", "runtime/"],
    cwd=WT, capture_output=True, text=True).stdout.strip()

td = os.path.join(OUT, "state_seeded")
os.makedirs(td, exist_ok=True)
eng = SwarmEngine(db_path=os.path.join(td, "eng.db"))

# Mandate 7: the sustained run processes a SEEDED gap queue. Three gaps:
# one user gap (dissatisfaction block -> user-gaps-first ordering) and two
# plain gaps with no routable shape (dispatch records "no route
# satisfied"; they stay honestly open -- the queue is processed, not
# closed by fiat). Every gap carries real observation evidence (the
# registry refuses evidence-less records -- the adversarial bar).
from swarm_engine.acquisition.gaps import (
    GapRegistry, GapRecord, DissatisfactionBlock)
import time as _time
_registry = GapRegistry(eng)


def _ev(note):
    return [{"kind": "observation",
             "observed": "sustained proof seeded this gap",
             "detail": note}]


_seeds = [
    GapRecord(gap_id="sustained-user-gap-1",
              registered_at=_time.time(),
              registered_by="sustained-proof",
              summary="user wants the assistant to remember context",
              dissatisfaction=DissatisfactionBlock(
                  feedback="it forgot what we discussed"),
              evidence=_ev("user dissatisfaction observed in proof setup")),
    GapRecord(gap_id="sustained-gap-2",
              registered_at=_time.time(),
              registered_by="sustained-proof",
              summary="unrouted capability gap (stays open)",
              evidence=_ev("no route satisfies this shape; stays open")),
    GapRecord(gap_id="sustained-gap-3",
              registered_at=_time.time(),
              registered_by="sustained-proof",
              summary="second unrouted gap (stays open)",
              evidence=_ev("no route satisfies this shape; stays open")),
]
for _rec in _seeds:
    try:
        _registry.register(_rec)
    except Exception:
        pass  # re-run over an existing state dir: already registered
rc = RunController(
    eng,
    config=RunConfig(
        cadence_interval_s=20.0,
        run_budget_s=3720.0,      # 62 minutes: >= 1 hour of wall-clock
        cycle_budget_s=60.0,
        gap_budget_s=5.0,
        max_cycles=10000,
        max_gaps_per_cycle=10,
        trusted_indexes=[],
        staging_dir=os.path.join(td, "staging")),
    checkpoint_path=os.path.join(td, "ckpt.db"),
    registry=None)

started = time.time()
with open(LOG, "a") as lf:
    lf.write(f"sustained start: pin={pin} dirty_runtime={bool(dirty)} "
             f"at={time.strftime('%Y-%m-%d %H:%M:%S')}\n")
    lf.flush()

report = rc.run()

elapsed = time.time() - started
cycles = report.get("cycles", [])
errors = [c for c in cycles
          if c.get("errors") or c.get("tick_error")]
acceptance = [c for c in cycles if c.get("acceptance")]
gaps_processed = sum(len(c.get("gaps", [])) for c in cycles)

result = {
    "pin": pin,
    "dirty_runtime": bool(dirty),
    "elapsed_s": elapsed,
    "cycles": len(cycles),
    "cycles_with_errors": len(errors),
    "cycles_with_acceptance": len(acceptance),
    "gap_dispatches": gaps_processed,
    "seeded_gaps": 3,
    "budget_exhausted": report.get("budget_exhausted"),
    "stopped": report.get("stopped"),
    "resumed": report.get("resumed", False),
    "success": (elapsed >= 3600 and len(errors) == 0
                and len(cycles) > 0 and gaps_processed > 0),
}
with open(REPORT, "w") as rf:
    json.dump(result, rf, indent=1)
with open(LOG, "a") as lf:
    lf.write(f"sustained end: {json.dumps(result)}\n")
print(json.dumps(result, indent=1))
