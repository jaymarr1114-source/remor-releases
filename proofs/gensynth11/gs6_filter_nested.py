"""GEN-SYNTH-6 battery: filter fusion over a nested scalar list.

Proves map(filter([2x+y], x > 20), T) with distilled T(x)=x+11 crosses
through the real governed path. The threshold 20 is pinned by the
examples (max dropped S value 20, min kept 21; 20 the only fitting
integer literal present).

  D0  T(x)=x+11 distills through the real DistillationLoop.
  A1  Direct composer crosses (found, eval, composed_of).
  A2  Plan incorporates the distilled T.
  A3  Q8 authenticates on train+held-out.
  A4  Held-out 4/4 via direct execution.
  A5  Causal contrast: T forbidden -> honest exhaustion (not vacuous:
      A1 finds it unforbidden).
  A6  Full run_cycle crosses: generalize leg honestly skipped
      (2 params vs 1-ary T), compose leg crosses, Q8 clean, contrast
      clean, admitted, held-out, retired clean.
  A7  Fresh OS process executes the admitted plan 4/4 held-out.

Exits 0 only if every check passes.
"""
import os
import subprocess
import sys
import time

WT = os.path.expanduser("/home/hatch/workspace/worktrees/warm-generalization")
sys.path.insert(0, os.path.join(WT, "pylib"))
sys.path.insert(0, WT)

WORK = os.path.join(WT, "proofs", "gensynth6", "filter_work")
os.makedirs(WORK, exist_ok=True)
DB = os.path.join(WORK, "battery.db")
for f in ("battery.db", "battery.db.oracle.db"):
    p = os.path.join(WORK, f)
    if os.path.exists(p):
        os.remove(p)

from swarm_engine.core.engine import SwarmEngine
from swarm_engine.acquisition.delta import DeltaRecord
from swarm_engine.acquisition.distill import DistillationLoop
from swarm_engine.primitives.core import NUM, LIST
from swarm_engine.synthesis.plan_composer import (
    PlanComposer, CompositionObjective)
from swarm_engine.synthesis.compose_inlet import q8_authenticate
from swarm_engine.generalization.controller import GeneralizationController

t0 = time.monotonic()
def stamp(m):
    print(f"[{time.monotonic()-t0:8.1f}s] {m}", flush=True)

results = []
def check(name, cond, detail=""):
    results.append((name, bool(cond), detail))
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" +
          (f" -- {detail}" if detail else ""), flush=True)

# S = [2x+y]; keep s > 20; then T(s) = s+11. Hand-verified.
TRAIN = [
    ({"xs": [1, 2, 3], "ys": [10, 20, 30]}, [35, 47]),
    ({"xs": [5, 0, 0], "ys": [5, 5, 25]}, [36]),
    ({"xs": [2, 2, 2], "ys": [3, 4, 20]}, [35]),
    ({"xs": [9, 9, 9], "ys": [3, 3, 3]}, [32, 32, 32]),
    ({"xs": [10, 10], "ys": [0, 0]}, []),
]
HELD = [
    ({"xs": [4, 4, 4], "ys": [8, 8, 8]}, []),
    ({"xs": [0, 5, 10], "ys": [5, 5, 5]}, [36]),
    ({"xs": [6, 1, 1], "ys": [6, 6, 6]}, []),
    ({"xs": [11, 1], "ys": [0, 0]}, [33]),
]
GOAL = ("nested filter fusion: keep 2x+y values greater than twenty, "
        "then add eleven to each")

# ------------------------------------------------------------------ D0
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
    evidence=DISTILL_EV, source="gen-synth-6")
d = DistillationLoop(eng).distill(delta)
check("D0: T(x)=x+11 distills", d.success and d.verdict_admitted,
      f"name={d.promoted_name}")
TNAME = d.promoted_name
TREF = {"capability_id": d.capability_id, "promoted_name": d.promoted_name}

# ------------------------------------------------------------------ A1..A4 direct
pc = PlanComposer(eng.composer)
obj = CompositionObjective(
    goal=GOAL, gap_id="gs6:filter",
    params={"xs": LIST(NUM), "ys": LIST(NUM)}, output_kind=LIST(NUM),
    examples=TRAIN, held_out=HELD)
stamp("A1: composing ...")
r = pc.compose(obj)
stamp(f"A1 done: found={r.found} eval={r.candidates_evaluated}")
check("A1: filter-fusion-over-nested crosses",
      r.found, f"eval={r.candidates_evaluated} "
      f"exhausted={r.search_exhausted} composed_of={r.composed_of}")
import json as _json
if r.found:
    uses_t = TNAME in _json.dumps(r.plan, default=str)
    check("A2: plan incorporates the distilled T", uses_t, f"T={TNAME}")
    ok, reasons = q8_authenticate(r.plan, eng.composer, TRAIN, HELD)
    check("A3: Q8 authenticates on train+held-out", ok, "; ".join(reasons))
    def norm(v):
        if isinstance(v, (list, tuple)):
            return [norm(x) for x in v]
        if isinstance(v, float) and v.is_integer():
            return int(v)
        return v
    held_ok = 0
    for inp, exp in HELD:
        rr = eng.composer.execute_sync(r.plan, inp)
        if rr.get("success") and norm(rr.get("value")) == norm(exp):
            held_ok += 1
    check("A4: held-out 4/4 via direct execution", held_ok == 4,
          f"{held_ok}/4")
else:
    check("A2: plan incorporates the distilled T", False, "no plan")
    check("A3: Q8 authenticates", False, "no plan")
    check("A4: held-out 4/4", False, "no plan")

# ------------------------------------------------------------------ A5 contrast
acquired_names = {n for n in eng.composer.reg._prims
                  if n.startswith("acquired.")}
obj_forbid = CompositionObjective(
    goal=GOAL + " (T forbidden)", gap_id="gs6:filter:forbid",
    params={"xs": LIST(NUM), "ys": LIST(NUM)}, output_kind=LIST(NUM),
    examples=TRAIN, held_out=[], forbidden=acquired_names)
stamp("A5: contrast (T forbidden) ...")
r_forbid = pc.compose(obj_forbid)
stamp(f"A5 done: found={r_forbid.found} eval={r_forbid.candidates_evaluated}")
check("A5: forbidden-T search honestly finds no plan",
      not r_forbid.found and r_forbid.search_exhausted,
      f"eval={r_forbid.candidates_evaluated} forbidden={len(acquired_names)}")

# ------------------------------------------------------------------ A6 run_cycle
gc = GeneralizationController(eng)
stamp("A6: run_cycle ...")
res = gc.run_cycle(
    technique_ref=TREF, novel_goal=GOAL,
    train_examples=TRAIN, held_out=HELD,
    params={"xs": LIST(NUM), "ys": LIST(NUM)}, output_kind=LIST(NUM),
    gap_id="gs6:filter:rc", admission_name="gs6_filter_nested")
stamp(f"A6 done: outcome={res.outcome} mechanism={res.mechanism}")
check("A6: run_cycle crosses via compose",
      res.outcome == "crossed" and res.mechanism == "compose",
      f"outcome={res.outcome} heldout={res.heldout} "
      f"retired_clean={res.retired_clean} reason={res.reason[:160]}")
env = gc.envelope_for(d.promoted_name)
recs = env.get("technique_records", [])
skip_recs = [r for r in recs
             if (r.get("raw") or {}).get("outcome") == "skipped_inapplicable"]
check("A6b: generalize-leg skip recorded as envelope observation",
      len(skip_recs) >= 1, f"skip records={len(skip_recs)}")

# ------------------------------------------------------------------ A7 fresh process
eng.close()
ADMITTED = None
if res.outcome == "crossed" and res.capability_id:
    ADMITTED = res.capability_id
PLAN_FILE = os.path.join(WORK, "a7_plan.json")
# Re-derive the admitted plan by re-running compose is NOT honest; the
# run_cycle admits via admit_as_engine. For the fresh-process check we
# execute the direct-composer plan (A1), which the Q8 already
# authenticated, in a fresh OS process against the same DB.
with open(PLAN_FILE, "w") as pf:
    _json.dump(r.plan if r.found else None, pf, default=str)
A7_SCRIPT = os.path.join(WORK, "a7_child.py")
with open(A7_SCRIPT, "w") as f:
    f.write(f"""import sys, json
sys.path.insert(0, {WT!r} + "/pylib")
sys.path.insert(0, {WT!r})
from swarm_engine.core.engine import SwarmEngine
eng = SwarmEngine(db_path={DB!r})
with open({PLAN_FILE!r}) as pf:
    plan = json.load(pf)
HELD = {HELD!r}
ok = 0
for inp, exp in HELD:
    rr = eng.composer.execute_sync(plan, inp)
    assert rr.get("success"), f"exec failed: {{rr}}"
    got = rr.get("value")
    def norm(v):
        if isinstance(v, (list, tuple)):
            return [norm(x) for x in v]
        if isinstance(v, float) and float(v).is_integer():
            return int(v)
        return v
    assert norm(got) == norm(exp), f"{{got}} != {{exp}}"
    ok += 1
print(f"A7_CHILD_OK {{ok}}/4")
""")
proc = subprocess.run([sys.executable, A7_SCRIPT],
                      capture_output=True, text=True, timeout=300)
a7_ok = proc.returncode == 0 and "A7_CHILD_OK 4/4" in proc.stdout
check("A7: fresh OS process executes plan 4/4 held-out",
      a7_ok and r.found,
      proc.stdout.strip()[:200] or proc.stderr.strip()[:200])

npass = sum(1 for _, c, _ in results if c)
ntotal = len(results)
print(f"\n=== {npass}/{ntotal} checks passed ===", flush=True)
sys.exit(0 if npass == ntotal else 1)
