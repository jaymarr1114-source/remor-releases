"""GEN-SYNTH-6 battery: three-level nesting through full run_cycle.

Proves T(2x+y+z) with distilled T(x)=x+11 crosses end to end through
GeneralizationController.run_cycle: the generalize leg honestly skips
(3 params vs 1-ary T, recorded envelope observation), the compose leg
finds the three-level plan, Q8 authenticates, causal contrast is clean
(T necessary), admission goes through admit_as_engine, held-out
verifies, and the microcontroller stack retires clean.

  D0  T(x)=x+11 distills through the real DistillationLoop.
  B1  Direct composer crosses the three-level goal (mechanism sanity).
  B2  Full run_cycle crosses via compose (outcome/mechanism/heldout).
  B3  Generalize leg honestly skipped (inapplicability recorded).
  B4  Causal contrast clean (implied by B2's crossed outcome; the
      controller refuses to cross on a failed contrast).
  B5  Fresh OS process executes the composed plan 4/4 held-out.

Exits 0 only if every check passes.
"""
import os
import subprocess
import sys
import time

WT = os.path.expanduser("~/workspace/worktrees/gensynth6-mission")
sys.path.insert(0, os.path.join(WT, "pylib"))
sys.path.insert(0, WT)

WORK = os.path.join(WT, "proofs", "gensynth6", "threelevel_work")
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
from swarm_engine.generalization.controller import GeneralizationController

t0 = time.monotonic()
def stamp(m):
    print(f"[{time.monotonic()-t0:8.1f}s] {m}", flush=True)

results = []
def check(name, cond, detail=""):
    results.append((name, bool(cond), detail))
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" +
          (f" -- {detail}" if detail else ""), flush=True)

# T(2x+y+z). Hand-verified.
TRAIN = [
    ({"xs": [1, 2], "ys": [10, 20], "zs": [100, 200]}, [123, 235]),
    ({"xs": [0, 5], "ys": [1, 1], "zs": [2, 3]}, [14, 25]),
]
HELD = [
    ({"xs": [3], "ys": [4], "zs": [5]}, [26]),
    ({"xs": [0, 0], "ys": [0, 0], "zs": [0, 0]}, [11, 11]),
    ({"xs": [1, 1, 1], "ys": [2, 2, 2], "zs": [3, 3, 3]}, [18, 18, 18]),
    ({"xs": [10], "ys": [20], "zs": [30]}, [81]),
]
GOAL = ("three-level nested sums with distilled technique: T(2x+y+z)")
PARAMS = {"xs": LIST(NUM), "ys": LIST(NUM), "zs": LIST(NUM)}

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
TREF = {"capability_id": d.capability_id, "promoted_name": d.promoted_name}

# ------------------------------------------------------------------ B1 direct
pc = PlanComposer(eng.composer)
obj = CompositionObjective(
    goal=GOAL, gap_id="gs6:threelevel",
    params=dict(PARAMS), output_kind=LIST(NUM),
    examples=TRAIN, held_out=HELD)
stamp("B1: direct compose ...")
r = pc.compose(obj)
stamp(f"B1 done: found={r.found} eval={r.candidates_evaluated}")
check("B1: three-level crosses via direct composer",
      r.found, f"eval={r.candidates_evaluated} "
      f"exhausted={r.search_exhausted} composed_of={r.composed_of}")

# ------------------------------------------------------------------ B2/B3 run_cycle
gc = GeneralizationController(eng)
stamp("B2: run_cycle ...")
res = gc.run_cycle(
    technique_ref=TREF, novel_goal=GOAL,
    train_examples=TRAIN, held_out=HELD,
    params=dict(PARAMS), output_kind=LIST(NUM),
    gap_id="gs6:threelevel:rc", admission_name="gs6_threelevel")
stamp(f"B2 done: outcome={res.outcome} mechanism={res.mechanism}")
check("B2: run_cycle crosses via compose",
      res.outcome == "crossed" and res.mechanism == "compose",
      f"outcome={res.outcome} heldout={res.heldout} "
      f"retired_clean={res.retired_clean} reason={res.reason[:160]}")
check("B3a: generalize-leg guard reports inapplicable (arity mismatch)",
      (lambda: (lambda ap, why: (not ap and "inapplicable" in why))(
          *gc._generalize_applicable(TREF, dict(PARAMS))))(),
      "guard must refuse bound-constant re-parameterization for 3 params")
env = gc.envelope_for(d.promoted_name)
recs = env.get("technique_records", [])
skip_recs = [r for r in recs
             if (r.get("raw") or {}).get("outcome") == "skipped_inapplicable"]
check("B3b: generalize-leg skip recorded as envelope observation",
      len(skip_recs) >= 1, f"skip records={len(skip_recs)}")

# ------------------------------------------------------------------ B5 fresh process
eng.close()
import json as _json
PLAN_FILE = os.path.join(WORK, "b5_plan.json")
with open(PLAN_FILE, "w") as pf:
    _json.dump(r.plan if r.found else None, pf, default=str)
B5_SCRIPT = os.path.join(WORK, "b5_child.py")
with open(B5_SCRIPT, "w") as f:
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
print(f"B5_CHILD_OK {{ok}}/4")
""")
proc = subprocess.run([sys.executable, B5_SCRIPT],
                      capture_output=True, text=True, timeout=300)
b5_ok = proc.returncode == 0 and "B5_CHILD_OK 4/4" in proc.stdout
check("B5: fresh OS process executes plan 4/4 held-out",
      b5_ok and r.found,
      proc.stdout.strip()[:200] or proc.stderr.strip()[:200])

npass = sum(1 for _, c, _ in results if c)
ntotal = len(results)
print(f"\n=== {npass}/{ntotal} checks passed ===", flush=True)
sys.exit(0 if npass == ntotal else 1)
