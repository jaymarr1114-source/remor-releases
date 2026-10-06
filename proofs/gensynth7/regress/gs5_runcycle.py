"""GEN-SYNTH-5: run_cycle end-to-end with mixed nested-with-T goal (faster).

Uses the mixed v4 goal (2xy+11) which crosses in ~1 min vs ~10 min for three-level.
Proves: guard skips generalize, compose crosses via Q8 + admission + held-out.
"""
import os, sys
WT = os.path.expanduser("~/workspace/worktrees/gensynth7-mission")
sys.path.insert(0, os.path.join(WT, "pylib"))
sys.path.insert(0, WT)
WORK = os.path.expanduser("~/workspace/worktrees/gensynth7-mission/proofs/gensynth6/regress_upstream/gensynth5_work_rc3")
os.makedirs(WORK, exist_ok=True)
DB = os.path.join(WORK, "rc3.db")
for f in ("rc3.db", "rc2.db.oracle.db"):
    p = os.path.join(WORK, f)
    if os.path.exists(p):
        os.remove(p)

from swarm_engine.core.engine import SwarmEngine
from swarm_engine.acquisition.delta import DeltaRecord
from swarm_engine.acquisition.distill import DistillationLoop
from swarm_engine.primitives.core import NUM, LIST
from swarm_engine.generalization.controller import GeneralizationController

eng = SwarmEngine(db_path=DB)
DISTILL_EV = [{"input": {"x": x}, "output": x + 11} for x in (1,2,3,4,5,6,7,8)]
delta = DeltaRecord(objective="add eleven", external_actions="demo",
    prior_capability="none", capability_gap="gap", technique="add 11",
    evidence=DISTILL_EV, source="rc3")
d = DistillationLoop(eng).distill(delta)
print(f"D0: success={d.success} name={d.promoted_name}", flush=True)
assert d.success

technique_ref = {
    "capability_id": d.capability_id,
    "promoted_name": d.promoted_name,
}

# Mixed v4 goal: 2xy+11 (nested with T, crosses fast)
def goal_fn(xs, ys):
    return [2*x*y + 11 for x, y in zip(xs, ys)]

TRAIN = [
    ({"xs": [10, 20], "ys": [3, 4]}, goal_fn([10, 20], [3, 4])),
    ({"xs": [1, 2], "ys": [5, 6]}, goal_fn([1, 2], [5, 6])),
]
HELD = [
    ({"xs": [7, 8], "ys": [2, 3]}, goal_fn([7, 8], [2, 3])),
]
print(f"TRAIN: {[t[1] for t in TRAIN]}", flush=True)
print(f"HELD: {[h[1] for h in HELD]}", flush=True)

ctl = GeneralizationController(eng, leg_budget_s=1200.0)
print("Running run_cycle...", flush=True)
c = ctl.run_cycle(
    technique_ref=technique_ref,
    novel_goal="mixed nested 2xy+11",
    train_examples=TRAIN, held_out=HELD,
    params={"xs": LIST(NUM), "ys": LIST(NUM)},
    output_kind=LIST(NUM),
    gap_id="gensynth5:run_cycle_mixed",
    admission_name="gensynth5_mixed")
print(f"run_cycle: outcome={c.outcome} mechanism={c.mechanism}", flush=True)
print(f"  reason={c.reason}", flush=True)
print(f"  heldout={c.heldout}", flush=True)
print(f"  capability_id={c.capability_id}", flush=True)
print(f"  retired_clean={c.retired_clean}", flush=True)
if c.outcome == "crossed" and c.mechanism == "compose":
    print("RUN_CYCLE END-TO-END CROSSED", flush=True)
else:
    print("RUN_CYCLE DID NOT CROSS", flush=True)
