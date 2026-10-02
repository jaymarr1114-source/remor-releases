"""Focused probe: is greater_than(x,20) in the bank? Is T in static_lams?

Builds the _LambdaBank directly (same construction as _compose_forward)
and inspects it. Read-only; does not modify runtime/.
"""
import os
import sys

WT = os.path.expanduser("~/workspace/worktrees/gensynth6-mission")
sys.path.insert(0, os.path.join(WT, "pylib"))
sys.path.insert(0, WT)

WORK = os.path.join(WT, "proofs", "gensynth6", "bank_probe_work")
os.makedirs(WORK, exist_ok=True)
DB = os.path.join(WORK, "probe.db")
for f in ("probe.db", "probe.db.oracle.db"):
    p = os.path.join(WORK, f)
    if os.path.exists(p):
        os.remove(p)

from swarm_engine.core.engine import SwarmEngine
from swarm_engine.acquisition.delta import DeltaRecord
from swarm_engine.acquisition.distill import DistillationLoop
from swarm_engine.primitives.core import NUM, LIST
from swarm_engine.synthesis.plan_composer import (
    PlanComposer, CompositionObjective, _LambdaBank)

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
    evidence=DISTILL_EV, source="gen-synth-6-bankprobe")
d = DistillationLoop(eng).distill(delta)
print(f"D0 ok: {d.success and d.verdict_admitted} name={d.promoted_name}",
      flush=True)
TNAME = d.promoted_name

TRAIN = [
    ({"xs": [1, 2, 3], "ys": [10, 20, 30]}, [35, 47]),
    ({"xs": [5, 0, 0], "ys": [5, 5, 25]}, [36]),
    ({"xs": [2, 2, 2], "ys": [3, 4, 20]}, [35]),
    ({"xs": [9, 9, 9], "ys": [3, 3, 3]}, [32, 32, 32]),
    ({"xs": [10, 10], "ys": [0, 0]}, []),
]
pc = PlanComposer(eng.composer)
obj = CompositionObjective(
    goal="probe", gap_id="gs6:bankprobe",
    params={"xs": LIST(NUM), "ys": LIST(NUM)}, output_kind=LIST(NUM),
    examples=TRAIN, held_out=[])

# Replicate _compose_forward's bank construction.
lits = pc._literals_from_examples(obj)
print(f"literals contain 20: {20 in lits}", flush=True)
print(f"literals contain 11: {11 in lits}", flush=True)
prims = pc._candidate_prims(obj)
bank = _LambdaBank(pc._composer, obj, prims, lits)
print(f"bank entries: {len(bank.entries)}", flush=True)

# BOOL lambdas viable on S-like first element (12).
def probe_bool(lam, elem):
    try:
        steps = lam.ref["$lambda"]["steps"]
        if len(steps) != 1:
            return None
        s = steps[0]
        args = {}
        for k, v in s["args"].items():
            args[k] = elem if isinstance(v, dict) and v.get("$var") else v
        ok, val = pc._probe_invoke(s["op"], args)
        return val if ok else None
    except Exception:
        return None

bool_lams = [l for l in bank.entries
             if getattr(l.output_kind.kind, "name", "") == "BOOL"]
print(f"BOOL lambdas: {len(bool_lams)}", flush=True)
# Find ones behaviorally equal to x>20 on probe values.
probe_vals = [12, 24, 36, 15, 5, 25, 20, 21]
gt20 = []
for lam in bool_lams:
    try:
        outs = [probe_bool(lam, v) for v in probe_vals]
        if outs == [v > 20 for v in probe_vals]:
            op = lam.ref["$lambda"]["steps"][0]["op"]
            args = lam.ref["$lambda"]["steps"][0]["args"]
            gt20.append((op, str(args), lam.behavior_key[:40]))
    except Exception:
        pass
print(f"BOOL lambdas behaviorally == (x>20): {len(gt20)}", flush=True)
for op, args, bk in gt20[:5]:
    print(f"  {op} {args}", flush=True)

# Static lambdas for NUM element kind: is T among them?
from swarm_engine.primitives.core import Kind
num_kind = None
for lam in bank.entries:
    try:
        if "acq_distilled" in (lam.used[0] if lam.used else ""):
            print(f"distilled lam: used={lam.used} "
                  f"out={lam.output_kind.kind.name}", flush=True)
            break
    except Exception:
        pass
# for_element_kind with a NUM kind
try:
    nk = LIST(NUM).args[0]
    slams = bank.for_element_kind(nk)
    t_in = [l for l in slams
            if TNAME.split(".")[-1] in str(l.used)]
    print(f"static lams for NUM: {len(slams)}; T among them: {len(t_in)}",
          flush=True)
except Exception as e:
    print(f"for_element_kind failed: {e}", flush=True)
print("bank probe complete", flush=True)
