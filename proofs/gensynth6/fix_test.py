"""Quick test: does the pre-gate fix let filter-fusion-over-nested cross?"""
import os
import sys
import time

WT = os.path.expanduser("~/workspace/worktrees/gensynth6-mission")
sys.path.insert(0, os.path.join(WT, "pylib"))
sys.path.insert(0, WT)

WORK = os.path.join(WT, "proofs", "gensynth6", "fix_test_work")
os.makedirs(WORK, exist_ok=True)
DB = os.path.join(WORK, "test.db")
for f in ("test.db", "test.db.oracle.db"):
    p = os.path.join(WORK, f)
    if os.path.exists(p):
        os.remove(p)

from swarm_engine.core.engine import SwarmEngine
from swarm_engine.acquisition.delta import DeltaRecord
from swarm_engine.acquisition.distill import DistillationLoop
from swarm_engine.primitives.core import NUM, LIST
from swarm_engine.synthesis.plan_composer import (
    PlanComposer, CompositionObjective)

t0 = time.monotonic()
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
    evidence=DISTILL_EV, source="gen-synth-6-fixtest")
d = DistillationLoop(eng).distill(delta)
print(f"D0: {d.success and d.verdict_admitted}", flush=True)

TRAIN = [
    ({"xs": [1, 2, 3], "ys": [10, 20, 30]}, [35, 47]),
    ({"xs": [5, 0, 0], "ys": [5, 5, 25]}, [36]),
    ({"xs": [2, 2, 2], "ys": [3, 4, 20]}, [35]),
    ({"xs": [9, 9, 9], "ys": [3, 3, 3]}, [32, 32, 32]),
    ({"xs": [10, 10], "ys": [0, 0]}, []),
]
pc = PlanComposer(eng.composer)
obj = CompositionObjective(
    goal="nested filter fusion test",
    gap_id="gs6:fixtest",
    params={"xs": LIST(NUM), "ys": LIST(NUM)}, output_kind=LIST(NUM),
    examples=TRAIN, held_out=[])
r = pc.compose(obj)
print(f"RESULT: found={r.found} eval={r.candidates_evaluated} "
      f"exhausted={r.search_exhausted} composed_of={r.composed_of}",
      flush=True)
print(f"TIME: {time.monotonic()-t0:.1f}s", flush=True)
