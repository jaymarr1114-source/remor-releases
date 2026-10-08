"""GEN-SYNTH-5: mixed nesting v4 - different ops at different levels.

Goal: map(zip(map(zip(xs,xs),sum),ys),product),T)
Inner: M1 = map(zip(xs,xs),sum) = [2x]
Outer: S = map(zip(M1,ys),product) = [2x*y]
Goal: map(S,T) = [2xy+11]
Mixed: sum at inner level, product at outer level (different higher-order ops).
"""
import os, sys
WT = os.path.expanduser("/home/hatch/workspace/worktrees/warm-generalization")
sys.path.insert(0, os.path.join(WT, "pylib"))
sys.path.insert(0, WT)
WORK = os.path.expanduser("/home/hatch/workspace/worktrees/warm-generalization/proofs/gensynth6/regress_upstream/gensynth5_work")
os.makedirs(WORK, exist_ok=True)
DB = os.path.join(WORK, "diag_mixed_v4.db")
for f in ("diag_mixed_v4.db", "diag_mixed_v4.db.oracle.db"):
    p = os.path.join(WORK, f)
    if os.path.exists(p):
        os.remove(p)
from swarm_engine.core.engine import SwarmEngine
from swarm_engine.acquisition.delta import DeltaRecord
from swarm_engine.acquisition.distill import DistillationLoop
from swarm_engine.primitives.core import NUM, LIST
from swarm_engine.synthesis.plan_composer import PlanComposer, CompositionObjective

eng = SwarmEngine(db_path=DB)
DISTILL_EV = [{"input": {"x": x}, "output": x + 11} for x in (1,2,3,4,5,6,7,8)]
delta = DeltaRecord(objective="add eleven", external_actions="demo",
    prior_capability="none", capability_gap="gap", technique="add 11",
    evidence=DISTILL_EV, source="mixed_v4")
d = DistillationLoop(eng).distill(delta)
print(f"D0: {d.success}", flush=True)
assert d.success

def goal_fn(xs, ys):
    return [2*x*y + 11 for x, y in zip(xs, ys)]

train = [
    ({"xs": [10, 20], "ys": [3, 4]},
     goal_fn([10, 20], [3, 4])),
    ({"xs": [1, 2], "ys": [5, 6]},
     goal_fn([1, 2], [5, 6])),
]
print(f"expected: {[t[1] for t in train]}", flush=True)

pc = PlanComposer(eng.composer)
obj = CompositionObjective(goal="mixed_v4", gap_id="g5m4",
    params={"xs": LIST(NUM), "ys": LIST(NUM)}, output_kind=LIST(NUM),
    examples=train, held_out=[], max_candidates=20000)
r = pc.compose(obj)
print(f"mixed_v4: found={r.found} eval={r.candidates_evaluated} exhausted={r.search_exhausted}", flush=True)
if r.found:
    print(f"composed_of={r.composed_of}", flush=True)
