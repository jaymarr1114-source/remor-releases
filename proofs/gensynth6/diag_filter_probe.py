"""GEN-SYNTH-6 instrumented diagnostic: WHERE does the filter-fusion
budget go on unmodified ce2745b?

Monkeypatches _complete_nested and _complete_filtered to log:
- every _complete_nested entry (depth, #pair_lambdas)
- every _complete_filtered call (S shapes, strict_removal, #preds,
  direct_possible/mappend_after_possible, result, eval delta)
- whether greater_than(x,20) is in the predicate pool

Does NOT modify runtime/ files. Read-only instrumentation.
"""
import os
import sys
import time

WT = os.path.expanduser("~/workspace/worktrees/gensynth6-mission")
sys.path.insert(0, os.path.join(WT, "pylib"))
sys.path.insert(0, WT)

WORK = os.path.join(WT, "proofs", "gensynth6", "diag_probe_work")
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

t0 = time.monotonic()
def stamp(m):
    print(f"[{time.monotonic()-t0:8.1f}s] {m}", flush=True)

# ---- instrumentation ----
_orig_nested = PlanComposer._complete_nested
_orig_filtered = PlanComposer._complete_filtered

def logged_nested(self, objective, res, banked, prims, bank,
                  map_prim, cname, cspec, fn_name,
                  nfrag, out_vals, vkind, exp_key, depth):
    # count pair_lambdas the same way the method does (approx: probe pool)
    stamp(f"NESTED enter depth={depth} eval={res.candidates_evaluated}")
    out = _orig_nested(self, objective, res, banked, prims, bank,
                       map_prim, cname, cspec, fn_name,
                       nfrag, out_vals, vkind, exp_key, depth)
    stamp(f"NESTED exit depth={depth} -> {out} "
          f"eval={res.candidates_evaluated}")
    return out

def logged_filtered(self, objective, res, banked, prims, bank,
                    map_prim, map_inputs, mfrag, map_vals,
                    exp_key, first_goal_key):
    e0 = res.candidates_evaluated
    shapes = []
    s0summary = "?"
    try:
        for s_ex in map_vals:
            shapes.append(len(s_ex) if isinstance(s_ex, (list, tuple))
                          else type(s_ex).__name__)
        if map_vals and isinstance(map_vals[0], (list, tuple)):
            s0summary = tuple(map_vals[0][:6])
    except Exception:
        shapes = ["?"]
    sr = self._strict_removal(map_vals, objective.examples)
    # Only log in detail for the shape-matching S; otherwise count.
    if shapes == [3, 3, 3, 3, 2]:
        stamp(f"FILTERED enter S0={s0summary} strict_removal={sr} "
              f"eval={e0}")
    out = _orig_filtered(self, objective, res, banked, prims, bank,
                         map_prim, map_inputs, mfrag, map_vals,
                         exp_key, first_goal_key)
    if shapes == [3, 3, 3, 3, 2]:
        stamp(f"FILTERED exit S0={s0summary} -> {out} eval_delta="
              f"{res.candidates_evaluated - e0}")
    return out

PlanComposer._complete_nested = logged_nested
PlanComposer._complete_filtered = logged_filtered

# ---- setup ----
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
    evidence=DISTILL_EV, source="gen-synth-6-probe")
d = DistillationLoop(eng).distill(delta)
stamp(f"D0 distill ok: {d.success and d.verdict_admitted}")

TRAIN = [
    ({"xs": [1, 2, 3], "ys": [10, 20, 30]}, [35, 47]),
    ({"xs": [5, 0, 0], "ys": [5, 5, 25]}, [36]),
    ({"xs": [2, 2, 2], "ys": [3, 4, 20]}, [35]),
    ({"xs": [9, 9, 9], "ys": [3, 3, 3]}, [32, 32, 32]),
    ({"xs": [10, 10], "ys": [0, 0]}, []),
]
pc = PlanComposer(eng.composer)

# Probe 2: the real objective with logging.
obj = CompositionObjective(
    goal="nested filter fusion: keep 2x+y values greater than twenty, "
         "then add eleven to each",
    gap_id="gs6:diagprobe",
    params={"xs": LIST(NUM), "ys": LIST(NUM)}, output_kind=LIST(NUM),
    examples=TRAIN, held_out=[])
stamp("main compose with instrumentation ...")
r = pc.compose(obj)
stamp(f"main result: found={r.found} eval={r.candidates_evaluated} "
      f"exhausted={r.search_exhausted} composed_of={r.composed_of}")
stamp("instrumented diagnostic complete")
