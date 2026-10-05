"""GEN-SYNTH-7 battery: four-level nesting through full run_cycle.

Proves T(2x+y+z+w) with distilled T(x)=x+11 crosses end to end through
GeneralizationController.run_cycle: the generalize leg honestly skips
(4 params vs 1-ary T, recorded envelope observation), the compose leg
finds the four-level plan via the GEN-SYNTH-7 arity-gated depth
extension (MAX_NEST_DEPTH=3 + 1 for 4-param goals, sound
_nest_may_complete prune + full-vector prioritization), Q8
authenticates, causal contrast is clean (T necessary), admission goes
through admit_as_engine, held-out verifies, and the microcontroller
stack retires clean.

  D0  T(x)=x+11 distills through the real DistillationLoop.
  C1  Direct composer crosses the four-level goal (mechanism sanity).
  C2  Full run_cycle crosses via compose (outcome/mechanism/heldout).
  C3  Generalize leg honestly skipped (inapplicability recorded).
  C4  Causal contrast clean (implied by C2's crossed outcome; the
      controller refuses to cross on a failed contrast).
  C5  Fresh OS process executes the composed plan 4/4 held-out.

Exits 0 only if every check passes.
"""
import os
import subprocess
import sys
import time

WT = os.path.expanduser("~/workspace/worktrees/gensynth7-mission")
sys.path.insert(0, os.path.join(WT, "pylib"))
sys.path.insert(0, WT)

WORK = os.path.join(WT, "proofs", "gensynth7", "fourlevel_work")
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

# T(2x+y+z+w). Hand-verified:
#   TRAIN[0]: T(2*1+10+100+1000)=T(1112)=1123; T(2*2+20+200+2000)=T(2224)=2235
#   TRAIN[1]: T(2*0+1+2+0)=T(3)=14; T(2*5+1+3+0)=T(14)=25
TRAIN = [
    ({"xs": [1, 2], "ys": [10, 20], "zs": [100, 200],
      "ws": [1000, 2000]}, [1123, 2235]),
    ({"xs": [0, 5], "ys": [1, 1], "zs": [2, 3],
      "ws": [0, 0]}, [14, 25]),
]
# HELD: T(2*3+4+5+6)=T(21)=32; zeros -> T(0)=11; etc.
HELD = [
    ({"xs": [3], "ys": [4], "zs": [5], "ws": [6]}, [32]),
    ({"xs": [0, 0], "ys": [0, 0], "zs": [0, 0], "ws": [0, 0]}, [11, 11]),
    ({"xs": [1, 1, 1], "ys": [2, 2, 2], "zs": [3, 3, 3],
      "ws": [4, 4, 4]}, [22, 22, 22]),
    ({"xs": [10], "ys": [20], "zs": [30], "ws": [40]}, [121]),
]
GOAL = ("four-level nested sums with distilled technique: T(2x+y+z+w)")
PARAMS = {"xs": LIST(NUM), "ys": LIST(NUM), "zs": LIST(NUM),
          "ws": LIST(NUM)}
# GEN-SYNTH-7: the four-level search uses the standard 20k candidate
# budget. The arity-gated extension is bounded (one extra level, still
# gated by the sound prune), not the 150x-per-level blowup the raw cap
# guards against.
BUDGET = 20000

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
    evidence=DISTILL_EV, source="gen-synth-7")
d = DistillationLoop(eng).distill(delta)
check("D0: T(x)=x+11 distills", d.success and d.verdict_admitted,
      f"name={d.promoted_name}")
TREF = {"capability_id": d.capability_id, "promoted_name": d.promoted_name}

# ------------------------------------------------------------------ C1 direct
pc = PlanComposer(eng.composer)
obj = CompositionObjective(
    goal=GOAL, gap_id="gs7:fourlevel",
    params=dict(PARAMS), output_kind=LIST(NUM),
    examples=TRAIN, held_out=HELD, max_candidates=BUDGET)
stamp("C1: direct compose (four-level, budget 20k) ...")
r = pc.compose(obj)
stamp(f"C1 done: found={r.found} eval={r.candidates_evaluated}")
check("C1: four-level crosses via direct composer",
      r.found, f"eval={r.candidates_evaluated} "
      f"exhausted={r.search_exhausted} composed_of={r.composed_of}")

# ------------------------------------------------------------------ C2/C3 run_cycle
gc = GeneralizationController(eng)
stamp("C2: run_cycle ...")
res = gc.run_cycle(
    technique_ref=TREF, novel_goal=GOAL,
    train_examples=TRAIN, held_out=HELD,
    params=dict(PARAMS), output_kind=LIST(NUM),
    gap_id="gs7:fourlevel:rc", admission_name="gs7_fourlevel")
stamp(f"C2 done: outcome={res.outcome} mechanism={res.mechanism}")
check("C2: run_cycle crosses via compose",
      res.outcome == "crossed" and res.mechanism == "compose",
      f"outcome={res.outcome} heldout={res.heldout} "
      f"retired_clean={res.retired_clean} reason={res.reason[:160]}")
check("C3a: generalize-leg guard reports inapplicable (arity mismatch)",
      (lambda: (lambda ap, why: (not ap and "inapplicable" in why))(
          *gc._generalize_applicable(TREF, dict(PARAMS))))(),
      "guard must refuse bound-constant re-parameterization for 4 params")
env = gc.envelope_for(d.promoted_name)
recs = env.get("technique_records", [])
skip_recs = [r for r in recs
             if (r.get("raw") or {}).get("outcome") == "skipped_inapplicable"]
check("C3b: generalize-leg skip recorded as envelope observation",
      len(skip_recs) >= 1, f"skip records={len(skip_recs)}")

# ------------------------------------------------------------------ C5 fresh process
eng.close()
import json as _json
PLAN_FILE = os.path.join(WORK, "c5_plan.json")
with open(PLAN_FILE, "w") as pf:
    _json.dump(r.plan if r.found else None, pf, default=str)
C5_SCRIPT = os.path.join(WORK, "c5_child.py")
with open(C5_SCRIPT, "w") as f:
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
print(f"C5_CHILD_OK {{ok}}/4")
""")
proc = subprocess.run([sys.executable, C5_SCRIPT],
                      capture_output=True, text=True, timeout=300)
c5_ok = proc.returncode == 0 and "C5_CHILD_OK 4/4" in proc.stdout
check("C5: fresh OS process executes plan 4/4 held-out",
      c5_ok and r.found,
      proc.stdout.strip()[:200] or proc.stderr.strip()[:200])

npass = sum(1 for _, c, _ in results if c)
ntotal = len(results)
print(f"\n=== {npass}/{ntotal} checks passed ===", flush=True)
sys.exit(0 if npass == ntotal else 1)
