"""GEN-SYNTH-6 boundary diagnostics on UNMODIFIED ce2745b.

Diag A: filter fusion over a nested scalar list via the direct composer.
        Goal: map(filter([2x+y], x > 20), T), T(x) = x + 11 (distilled).
        The threshold 20 is pinned: max dropped S value is 20, min kept
        is 21, and 20 is the only integer literal in the examples that
        fits every example.
Diag B: three-level binary->binary->binary nesting via FULL run_cycle.
        Goal: T(2x+y+z), T(x) = x + 11 (distilled). The generalize leg
        must honestly skip (3 params vs 1-ary T); the compose leg must
        cross with Q8 + clean causal contrast + admission + held-out.

Exits 0 only if the diagnostics complete (cross or honest fail -- both
are informative at the diagnostic stage).
"""
import os
import sys
import time

WT = os.path.expanduser("~/workspace/worktrees/gensynth6-mission")
sys.path.insert(0, os.path.join(WT, "pylib"))
sys.path.insert(0, WT)

WORK = os.path.join(WT, "proofs", "gensynth6", "diag_work")
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
from swarm_engine.generalization.controller import GeneralizationController

t0 = time.monotonic()
def stamp(msg):
    print(f"[{time.monotonic()-t0:8.1f}s] {msg}", flush=True)

# ------------------------------------------------------------------ distill T
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
    evidence=DISTILL_EV, source="gen-synth-6-diag")
d = DistillationLoop(eng).distill(delta)
stamp(f"D0 distill: success={d.success} admitted={d.verdict_admitted} "
      f"name={d.promoted_name} cap={d.capability_id}")
assert d.success and d.verdict_admitted, "distillation failed"
TREF = {"capability_id": d.capability_id, "promoted_name": d.promoted_name}

# ------------------------------------------------------------------ Diag A
# S = [2x+y]; filter x>20; then T. Verified by hand below.
TRAIN_A = [
    ({"xs": [1, 2, 3], "ys": [10, 20, 30]}, [35, 47]),   # S=[12,24,36]
    ({"xs": [5, 0, 0], "ys": [5, 5, 25]}, [36]),         # S=[15,5,25]
    ({"xs": [2, 2, 2], "ys": [3, 4, 20]}, [35]),         # S=[7,8,24]
    ({"xs": [9, 9, 9], "ys": [3, 3, 3]}, [32, 32, 32]),  # S=[21,21,21]
    ({"xs": [10, 10], "ys": [0, 0]}, []),               # S=[20,20] pins t>=20
]
HELD_A = [
    ({"xs": [4, 4, 4], "ys": [8, 8, 8]}, []),            # S=[16,16,16]
    ({"xs": [0, 5, 10], "ys": [5, 5, 5]}, [36]),        # S=[5,15,25]
    ({"xs": [6, 1, 1], "ys": [6, 6, 6]}, []),           # S=[18,8,8]
    ({"xs": [11, 1], "ys": [0, 0]}, [33]),              # S=[22,2]
]
pc = PlanComposer(eng.composer)
obj_a = CompositionObjective(
    goal="nested filter fusion: keep 2x+y values greater than twenty, "
         "then add eleven to each",
    gap_id="gs6:diagA",
    params={"xs": LIST(NUM), "ys": LIST(NUM)}, output_kind=LIST(NUM),
    examples=TRAIN_A, held_out=HELD_A)
stamp("Diag A: composing filter-fusion-over-nested ...")
r_a = pc.compose(obj_a)
stamp(f"Diag A result: found={r_a.found} eval={r_a.candidates_evaluated} "
      f"exhausted={r_a.search_exhausted} "
      f"composed_of={r_a.composed_of} "
      f"probe_exhausted={r_a.probe_budget_exhausted}")

# ------------------------------------------------------------------ Diag B
TRAIN_B = [
    ({"xs": [1, 2], "ys": [10, 20], "zs": [100, 200]}, [123, 235]),
    ({"xs": [0, 5], "ys": [1, 1], "zs": [2, 3]}, [14, 25]),
]
HELD_B = [
    ({"xs": [3], "ys": [4], "zs": [5]}, [26]),
    ({"xs": [0, 0], "ys": [0, 0], "zs": [0, 0]}, [11, 11]),
    ({"xs": [1, 1, 1], "ys": [2, 2, 2], "zs": [3, 3, 3]}, [18, 18, 18]),
    ({"xs": [10], "ys": [20], "zs": [30]}, [81]),
]
gc = GeneralizationController(eng)
stamp("Diag B: run_cycle three-level T(2x+y+z) ...")
res_b = gc.run_cycle(
    technique_ref=TREF,
    novel_goal="three-level nested sums with distilled technique: "
               "T(2x+y+z)",
    train_examples=TRAIN_B, held_out=HELD_B,
    params={"xs": LIST(NUM), "ys": LIST(NUM), "zs": LIST(NUM)},
    output_kind=LIST(NUM),
    gap_id="gs6:diagB", admission_name="gs6_diagB_composed")
stamp(f"Diag B result: outcome={res_b.outcome} mechanism={res_b.mechanism} "
      f"heldout={res_b.heldout} retired_clean={res_b.retired_clean}")
stamp(f"Diag B reason: {res_b.reason[:300]}")

stamp("diagnostics complete")
