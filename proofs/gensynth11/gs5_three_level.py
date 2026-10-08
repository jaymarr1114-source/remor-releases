"""GEN-SYNTH-5 diagnosis: three-level nesting vs current (6d412ce) mechanism.

Objective: goal[i] = T(2*xs[i] + ys[i] + zs[i]) with T(x)=x+11 distilled.
Shape: map(zip(map(zip(map(zip(xs,xs),sum),ys),sum),zs),sum),T.
Measures where the 20,000 budget goes: candidate-shape histogram, dominant
expansions, whether the three-level target shape ever becomes reachable.
"""
import os
import sys

WT = os.path.expanduser("/home/hatch/workspace/worktrees/warm-generalization")
sys.path.insert(0, os.path.join(WT, "pylib"))
sys.path.insert(0, WT)

WORK = os.path.expanduser("/home/hatch/workspace/worktrees/warm-generalization/proofs/gensynth6/regress_upstream/gensynth5_work")
os.makedirs(WORK, exist_ok=True)
DB = os.path.join(WORK, "diag.db")
for f in ("diag.db", "diag.db.oracle.db"):
    p = os.path.join(WORK, f)
    if os.path.exists(p):
        os.remove(p)

from swarm_engine.core.engine import SwarmEngine
from swarm_engine.acquisition.delta import DeltaRecord
from swarm_engine.acquisition.distill import DistillationLoop
from swarm_engine.primitives.core import NUM, LIST
from swarm_engine.synthesis.plan_composer import (
    PlanComposer, CompositionObjective)

eng = SwarmEngine(db_path=DB)
DISTILL_EV = [{"input": {"x": x}, "output": x + 11}
              for x in (1, 2, 3, 4, 5, 6, 7, 8)]
delta = DeltaRecord(
    objective="add eleven to the input",
    external_actions=("the external agent demonstrated adding eleven: for "
                      "each input x it produced x+11"),
    prior_capability="no add-eleven capability admitted in the engine",
    capability_gap=("REMOR could not add eleven to an input; the external "
                    "agent demonstrably could"),
    technique="add eleven to the input",
    evidence=DISTILL_EV, source="gen-synth-5-diag")
d = DistillationLoop(eng).distill(delta)
print(f"D0 distill: success={d.success} name={d.promoted_name}", flush=True)
TNAME = d.promoted_name

# goal[i] = T(2*xs[i] + ys[i] + zs[i]) = 2x+y+z+11
TRAIN = [
    ({"xs": [1, 2], "ys": [10, 20], "zs": [100, 200]},
     [2*1+10+100+11, 2*2+20+200+11]),          # [123, 235]
    ({"xs": [0, 5], "ys": [1, 1], "zs": [2, 3]},
     [0+1+2+11, 10+1+3+11]),                     # [14, 25]
]
print("expected train outputs:", [[2*x+y+z+11 for x, y, z in
      zip(i["xs"], i["ys"], i["zs"])] for i, _ in TRAIN], flush=True)

pc = PlanComposer(eng.composer)
obj = CompositionObjective(
    goal="three-level nested then T: T(2x+y+z)", gap_id="gs5:diag3",
    params={"xs": LIST(NUM), "ys": LIST(NUM), "zs": LIST(NUM)},
    output_kind=LIST(NUM), examples=TRAIN, held_out=[])
r = pc.compose(obj)
print(f"DIAG three-level: found={r.found} "
      f"eval={r.candidates_evaluated} exhausted={r.search_exhausted}",
      flush=True)
print(f"  refused_type_incoherent={r.refused_type_incoherent} "
      f"pruned_equivalent={r.pruned_equivalent}", flush=True)
nc = getattr(r, "nested_candidates", None)
print(f"  nested_candidates banked: {len(nc) if nc is not None else 'n/a'}",
      flush=True)
if r.found:
    print(f"  composed_of={r.composed_of}", flush=True)
