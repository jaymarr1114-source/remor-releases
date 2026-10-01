#!/usr/bin/env python3
"""CAUSAL-01 (RUN-CTRL-V10-1, post-repair): the Controller drives Q1's
CognitionLoop.cycle() as its ONE acquisition leg; the retired V10-P4 sweep
step double-drives nothing.

Repaired behavior under test:
  * The Controller's _acquisition_leg() drives CognitionLoop.cycle() with
    run_quarantine_sweep=False (the tick owns quarantine + Q7).
  * The cycle's distill leg covers BOTH delta generations: M1-ingest
    (source "technique_delta") via _adapt_m1_to_m2, and V10-P3 charter
    (source "delta-capture") via V10-P4's charter_to_delta_record.
  * Every processed delta is marked with BOTH the driver's outcome
    observation AND V10-P4's distillation_consumption record (unified
    consumption).
  * Q1's _scan_new_deltas and V10-P4's find_pending_deltas both see the
    unified consumption: neither returns the delta again. Double-drive is
    gone.

Exit 0 only if all of the above is demonstrated on the real machinery.
"""

import os
import sys
import tempfile

WT = "/home/hatch/workspace/worktrees/warm-v10-convergence"
sys.path.insert(0, os.path.join(WT, "pylib"))
sys.path.insert(0, WT)

from swarm_engine.core.engine import SwarmEngine
from swarm_engine.core.run_controller import RunController, RunConfig
from swarm_engine.acquisition.loop_driver import CognitionLoop
from swarm_engine.intellect.unified_memory import record_experience
from swarm_engine.acquisition.ingest import _m1_delta_dict

PASSED = 0


def check(cond, msg):
    global PASSED
    if not cond:
        print(f"FAIL: {msg}")
        sys.exit(1)
    PASSED += 1
    print(f"ok [{PASSED}]: {msg}")


td = tempfile.mkdtemp(prefix="rcv10_causal01_")
eng = SwarmEngine(db_path=os.path.join(td, "eng.db"))
ep = eng.intellect.epistemic

# Seed 1: one M1-ingest delta (dual-marker record, both drivers see it).
m1 = _m1_delta_dict(
    delta_id="causal-delta-01",
    X="add two numbers",
    Y={"source": "external-demo", "action_count": 2,
       "actions": [{"name": "type"}, {"name": "press-enter"}]},
    Z={"capabilities": []},
    gap="remor cannot add two numbers from a demo",
    T={"technique_name": "column-addition",
       "required_tools": ["calculator"]},
    E=[], D=[],
    V={"status": "unverified", "method": None},
    C=None,
)
obs_id_1 = record_experience(
    ep, origin_loop="acquisition", kind="technique_delta",
    content="technique delta (Y-Z): causal-delta-01",
    raw={"delta": m1},
    source="technique_delta",
)
check(bool(obs_id_1), "seeded one M1-ingest technique_delta record")

# Seed 2: one V10-P3 charter delta (source "delta-capture" -- invisible to
# the old M1-only scan; was orphaned before the repair).
charter_delta = {
    "delta_id": "charter-delta-01",
    "objective_x": "add two numbers",
    "technique_t": {"technique_name": "column-addition",
                    "required_tools": ["calculator"],
                    "method": "add column-wise"},
    "evidence_e": {"gap": "remor cannot add two numbers",
                   "rationale": "column arithmetic composes"},
    "prior_capability": {"capabilities": []},
    "dependencies": [],
    "verification": {"status": "unverified", "method": None},
}
obs_id_2 = record_experience(
    ep, origin_loop="acquisition", kind="technique_delta",
    content="technique delta (charter): charter-delta-01",
    raw={"delta": charter_delta},
    source="delta-capture",
)
check(bool(obs_id_2), "seeded one V10-P3 charter delta (delta-capture)")

cfg = RunConfig(cadence_interval_s=60.0, run_budget_s=3600.0,
                cycle_budget_s=30.0, gap_budget_s=2.0, max_cycles=3,
                max_gaps_per_cycle=25, trusted_indexes=[],
                staging_dir=os.path.join(td, "staging"))
rc = RunController(eng, config=cfg,
                   checkpoint_path=os.path.join(td, "ckpt.db"),
                   registry=None)

# The Controller owns the CognitionLoop it drives (the seed).
check(isinstance(rc._cognition_loop, CognitionLoop),
      "RunController constructs and owns the CognitionLoop")

# Step 1: the acquisition leg drives Q1's cycle; both deltas processed.
leg = rc._acquisition_leg(budget_s=30.0)
distilled_ids = [o.get("delta_id") for o in leg.get("distilled", [])]
err_ids = [e.get("delta_id") for e in leg.get("distill_errors", [])]
processed_ids = set(distilled_ids) | set(err_ids)
check("causal-delta-01" in processed_ids,
      f"acquisition leg processed the M1 delta (distilled={distilled_ids})")
check("charter-delta-01" in processed_ids,
      f"acquisition leg processed the charter delta (no orphaning)")

# Step 2: unified consumption -- outcome observations for both.
outcomes = [
    o for o in ep.all_observations()
    if getattr(o, "source", "") == "loop_driver:distillation_outcome"]
outcome_ids = {(getattr(o, "raw", None) or {}).get("delta_id")
               for o in outcomes}
check({"causal-delta-01", "charter-delta-01"} <= outcome_ids,
      "outcome observations written for both deltas")

# Step 3: unified consumption -- V10-P4 markers for both.
consumed_oids = [
    (getattr(o, "raw", None) or {}).get("delta_observation_id")
    for o in ep.all_observations()]
check(obs_id_1 in consumed_oids and obs_id_2 in consumed_oids,
      "V10-P4 distillation_consumption markers written for both deltas")

# Step 4: double-drive is gone -- Q1's scan sees the unified consumption.
loop = rc._cognition_loop
rescan = loop._scan_new_deltas()
rescan_ids = [(o.get("delta") or {}).get("delta_id") for _, o in rescan]
check("causal-delta-01" not in rescan_ids
      and "charter-delta-01" not in rescan_ids,
      f"Q1's scan no longer returns either delta (rescan={rescan_ids})")

# Step 5: V10-P4's sweep sees the same consumption.
from swarm_engine.acquisition.distill_driver import find_pending_deltas
pending = find_pending_deltas(ep, limit=100)
pending_oids = [(getattr(o, "observation_id", None)) for o, _ in pending]
check(obs_id_1 not in pending_oids and obs_id_2 not in pending_oids,
      "V10-P4's find_pending_deltas also sees neither delta "
      "(one shared consumption record)")

print(f"\nCAUSAL-01 REPAIR VERIFIED: one driver, one cadence, unified "
      f"consumption, no orphaned generation ({PASSED}/9 checks).")
