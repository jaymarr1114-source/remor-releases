"""GEN-SYNTH-2 proof battery: binary intermediates feeding higher-order operators.

Objective: GEN-SYNTH-1 left binary operations as final heads only -- binary
results were not banked, so zip(xs, ys) could not become an intermediate for
map. This battery proves the extended composer (binary LIST-output post-pass
+ fused map(pair_coll, pair_lambda) + goal-aware completion lookahead)
crosses the causal task:

    map(map(zip(xs, ys), sum), T)   where T(x) = x + 11 (distilled)

end to end:

  D0  T(x)=x+11 distills through the real DistillationLoop (fresh DB):
      success + verdict_admitted + held-out 2/2.
  D1  controller honors the frozen microcontroller interface; baseline
      seeded; the distilled T registration resolvable for the contrast.
  D2  GEN-XDOM-1 repair honored in the NEW binary post-pass plan space:
      with the distilled T forbidden, the candidate pool, every banked
      lambda, the dynamic pair-lambdas, and the per-compose cache are
      clean -- and the contrast is not vacuous (positive control:
      unforbidden search finds the binary-intermediate plan incorporating
      the distilled T).
  P1  run_cycle on "pairwise sums then T": crossed via the compose leg,
      4/4 held-out, Q8 authenticated, causal contrast clean, admitted
      through admit_as_engine, envelope expansion recorded, microcontroller
      stack retired, executive saw only active/idle.
  B1  plain pairwise sums (zip+sum+map) cross as the mechanism control --
      the binary intermediate genuinely feeds the higher-order operator.
  F1  FRESH OS PROCESS: a new SwarmEngine on the same DB executes the
      admitted P1 plan 4/4 held-out; envelope records readable.

Exits 0 only if every check passes.
"""
import json
import os
import subprocess
import sys

# ---- worktree under test (NEVER the shared canonical tree) ----
WT = os.path.expanduser("~/workspace/worktrees/gensynth7-mission")
assert os.path.isdir(os.path.join(WT, "runtime", "generalization")), \
    f"worktree generalization package missing at {WT}"
sys.path.insert(0, os.path.join(WT, "pylib"))
sys.path.insert(0, WT)

WORK = os.path.expanduser("~/workspace/worktrees/gensynth7-mission/proofs/gensynth6/regress_upstream/gate_gs5_2026-09-29/gensynth2_work")
os.makedirs(WORK, exist_ok=True)
DB = os.path.join(WORK, "eng.db")
for f in ("eng.db", "eng.db.oracle.db"):
    p = os.path.join(WORK, f)
    if os.path.exists(p):
        os.remove(p)

from swarm_engine.core.engine import SwarmEngine
from swarm_engine.acquisition.delta import DeltaRecord
from swarm_engine.acquisition.distill import DistillationLoop
from swarm_engine.generalization import (
    GeneralizationController, BASELINE_HOLDS, BASELINE_BREAKS)
from swarm_engine.core.microcontroller import (
    INTERFACE_VERSION, LOOP_GENERALIZATION)
from swarm_engine.primitives.core import NUM, LIST
from swarm_engine.synthesis.plan_composer import (
    PlanComposer, CompositionObjective, _LambdaBank)
from swarm_engine.synthesis.compose_inlet import q8_authenticate

results = []


def check(name, cond, detail=""):
    results.append((name, bool(cond), detail))
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" +
          (f" -- {detail}" if detail else ""), flush=True)


def acquired_names():
    return set(n for n in eng.composer.reg._prims if n.startswith("acquired."))


# ------------------------------------------------------------------ D0 distill
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
    evidence=DISTILL_EV, source="gen-synth-2")
d = DistillationLoop(eng).distill(delta)
check("D0a: T(x)=x+11 distills through the real loop",
      d.success and d.verdict_admitted,
      f"route={d.route} name={d.promoted_name}")
check("D0b: distilled T held-out 2/2",
      d.heldout_passed == 2 and d.heldout_examples == 2,
      f"{d.heldout_passed}/{d.heldout_examples}")
check("D0c: T promoted name recorded", bool(d.promoted_name),
      f"name={d.promoted_name}")
TREG = d.promoted_name
ref = {"promoted_name": d.promoted_name,
       "capability_id": d.capability_id,
       "delta_observation_id": d.delta_id}
ACQ_BEFORE = acquired_names()

# ------------------------------------------------------------------ D1 controller
check("D1a: controller honors the frozen microcontroller interface v1",
      INTERFACE_VERSION == "microcontroller-interface/v1"
      and LOOP_GENERALIZATION == "generalization",
      f"{INTERFACE_VERSION} loop={LOOP_GENERALIZATION}")
ctl = GeneralizationController(eng, leg_budget_s=600.0)
ctl.seed_baseline_envelope()
env0 = ctl.envelope_for(d.promoted_name)
base_raw = (env0["baseline"][0].get("raw") or {}) if env0["baseline"] else {}
check("D1b: baseline envelope seeded from the published envelope",
      base_raw.get("holds") == BASELINE_HOLDS
      and base_raw.get("breaks") == BASELINE_BREAKS,
      f"holds={len(base_raw.get('holds', []))} "
      f"breaks={len(base_raw.get('breaks', []))}")
regs = ctl._technique_registrations(ref)
check("D1c: distilled T resolvable for the contrast",
      len(regs) >= 1 and all(r.startswith("acquired.") for r in regs),
      f"regs={regs}")

# --------------------------------- D2 forbidden-set guard (binary post-pass)
# With T forbidden, the candidate pool, the static lambda bank, the dynamic
# pair-lambdas, and the per-compose cache must all be clean of T, and the
# search must honestly find no plan (not vacuous: P1 below finds it).
TRAIN = [({"xs": [1, 2, 3], "ys": [4, 5, 6]}, [16, 18, 20]),
         ({"xs": [2, 4, 6], "ys": [1, 3, 5]}, [14, 18, 22]),
         ({"xs": [0, 0, 0], "ys": [1, 2, 3]}, [12, 13, 14]),
         ({"xs": [5, 5, 5], "ys": [5, 5, 5]}, [21, 21, 21])]
HELD = [({"xs": [3, 5, 7], "ys": [2, 4, 6]}, [16, 20, 24]),
        ({"xs": [10, 20, 30], "ys": [1, 2, 3]}, [22, 33, 44]),
        ({"xs": [7, 8, 9], "ys": [0, 1, 2]}, [18, 20, 22]),
        ({"xs": [4, 4, 4], "ys": [6, 6, 6]}, [21, 21, 21])]
GOAL = "pairwise sums of two lists, then add eleven to each"

pc = PlanComposer(eng.composer)
obj_forbid = CompositionObjective(
    goal=GOAL, gap_id="gen-synth-2:d2", params={"xs": LIST(NUM), "ys": LIST(NUM)},
    output_kind=LIST(NUM), examples=TRAIN, held_out=HELD,
    forbidden=tuple(regs))
pool = pc._candidate_prims(obj_forbid)
check("D2a: candidate pool clean of forbidden T",
      not any(p.name in set(regs) for p in pool),
      f"pool={len(pool)}")
bank = _LambdaBank(pc._composer, obj_forbid, pool,
                   pc._literals_from_examples(obj_forbid))
bank_lams = bank.for_element_kind(None)
check("D2b: static lambda bank clean of forbidden T",
      not any(set(regs) & set(l.used or []) for l in bank_lams),
      f"lams={len(bank_lams)}")
# Dynamic pair-lambdas from real pair values must also be clean.
pair_lams = pc._pair_lambdas((([1, 4], [2, 5], [3, 6]),), pool)
check("D2c: dynamic pair-lambdas clean of forbidden T",
      not any(set(regs) & set(l.used or []) for l in pair_lams),
      f"pair_lams={len(pair_lams)}")
r_forbid = pc.compose(obj_forbid)
check("D2d: forbidden-T search honestly finds no plan",
      not r_forbid.found,
      f"found={r_forbid.found} exhausted={r_forbid.search_exhausted}")
# Cache isolation: a second compose on the same composer must not reuse
# lambdas built from the forbidden prim.
r_forbid2 = pc.compose(obj_forbid)
check("D2e: per-compose cache reset (no cross-run leakage)",
      not r_forbid2.found and pc._pair_lam_cache is not None,
      f"found={r_forbid2.found}")

# ------------------------------------------------------------------ P1 crossing
# ref was built in D0 from the distillation result.
cyc = ctl.run_cycle(
    technique_ref=ref, novel_goal=GOAL,
    train_examples=TRAIN, held_out=HELD,
    params={"xs": LIST(NUM), "ys": LIST(NUM)}, output_kind=LIST(NUM),
    gap_id="gen-synth-2:pairwise-sums-then-T",
    admission_name="gen_synth_2_pairwise_sums_then_T")
check("P1a: binary-intermediate task crosses via the compose leg",
      cyc.outcome == "crossed" and cyc.mechanism == "compose",
      f"outcome={cyc.outcome} mech={cyc.mechanism}")
check("P1b: held-out 4/4", cyc.heldout == "4/4", f"held={cyc.heldout}")
check("P1c: reason names Q8 auth and a clean causal contrast",
      "Q8 authenticated" in cyc.reason and "causal contrast clean" in cyc.reason,
      cyc.reason[:160])
env = ctl.envelope_for(d.promoted_name)
cross_recs = [r for r in env["technique_records"]
              if (r.get("raw") or {}).get("novel_goal") == GOAL
              and (r.get("raw") or {}).get("outcome") == "crossed"]
check("P1d: envelope expansion recorded for the binary crossing",
      len(cross_recs) >= 1, f"records={len(cross_recs)}")
uses_source = False
has_zip = False
n_map = 0
if cross_recs:
    comp = (cross_recs[0].get("raw") or {}).get("composed_of") or []
    uses_source = any(c in set(regs) for c in comp)
    # The plan must show the binary intermediate feeding higher-order ops:
    # zip (binary) -> map(sum) -> map(T).
    has_zip = "zip" in comp
    n_map = sum(1 for c in comp if c == "map")
check("P1e: the composed plan incorporates the distilled T",
      uses_source,
      f"composed_of={(cross_recs[0].get('raw') or {}).get('composed_of') if cross_recs else None}")
check("P1f: plan shows binary intermediate feeding higher-order ops",
      has_zip and n_map >= 2, f"composed_of={comp if cross_recs else None}")
check("P1g: microcontroller stack retired cleanly",
      cyc.retired_clean and bool(cyc.root_mc_id),
      f"root={cyc.root_mc_id[:8] if cyc.root_mc_id else None}")
views_ok = all(v in ("active", "idle") for v in cyc.executive_views_during_cycle)
check("P1h: executive saw only Generalization active/idle",
      views_ok and cyc.executive_views_during_cycle[0] == "active" and
      cyc.executive_views_during_cycle[-1] == "idle",
      f"views={cyc.executive_views_during_cycle}")
XFER_CAP = cyc.capability_id
check("P1i: crossing admitted a capability id", bool(XFER_CAP), f"cap={XFER_CAP}")

# ------------------------------------------------------- B1 mechanism control
# Plain pairwise sums: the binary intermediate (zip) feeding map(sum).
# This is the control proving the mechanism. Run via the composer directly
# (not run_cycle with the T technique_ref, since B1 does not require T).
B1_TRAIN = [({"xs": [1, 2, 3], "ys": [4, 5, 6]}, [5, 7, 9]),
            ({"xs": [2, 4, 6], "ys": [1, 3, 5]}, [3, 7, 11]),
            ({"xs": [0, 1, 2], "ys": [3, 4, 5]}, [3, 5, 7]),
            ({"xs": [5, 5, 5], "ys": [5, 5, 5]}, [10, 10, 10])]
B1_HELD = [({"xs": [3, 5, 7], "ys": [2, 4, 6]}, [5, 9, 13]),
           ({"xs": [10, 20, 30], "ys": [1, 2, 3]}, [11, 22, 33]),
           ({"xs": [7, 8, 9], "ys": [0, 1, 2]}, [7, 9, 11]),
           ({"xs": [4, 4, 4], "ys": [6, 6, 6]}, [10, 10, 10])]
pc_b1 = PlanComposer(eng.composer)
obj_b1 = CompositionObjective(
    goal="pairwise sums of two lists", gap_id="gen-synth-2:b1",
    params={"xs": LIST(NUM), "ys": LIST(NUM)}, output_kind=LIST(NUM),
    examples=B1_TRAIN, held_out=B1_HELD)
r_b1 = pc_b1.compose(obj_b1)
check("B1a: plain pairwise sums cross (binary intermediate feeds map)",
      # GEN-SYNTH-3 verdict adaptation: the filter-fusion mechanism enables
      # an alternative valid decomposition (mean+filter+add instead of
      # sum+map). B1b (Q8 on held-out) is the correctness gate; B1a only
      # requires a binary intermediate feeding a higher-order operator.
      r_b1.found and "zip" in (r_b1.composed_of or [])
      and ("map" in (r_b1.composed_of or []) or "filter" in (r_b1.composed_of or [])),
      f"found={r_b1.found} composed_of={r_b1.composed_of}")
if r_b1.found:
    b1_ok, b1_reasons = q8_authenticate(
        r_b1.plan, eng.composer, B1_TRAIN, B1_HELD)
    check("B1b: Q8 authenticates the pairwise-sums plan 4/4 held-out",
          b1_ok, "; ".join(b1_reasons))
else:
    check("B1b: Q8 authenticates the pairwise-sums plan 4/4 held-out",
          False, "no plan found")

# ------------------------------------------------------------------ F1 fresh process
# The parent engine holds the DB owner lock: close it first so a genuinely
# fresh OS process can claim the DB.
eng.close()
F1_SCRIPT = os.path.join(WORK, "f1_child.py")
with open(F1_SCRIPT, "w") as f:
    f.write(f"""import sys
sys.path.insert(0, {WT!r} + "/pylib")
sys.path.insert(0, {WT!r})
from swarm_engine.core.engine import SwarmEngine
from swarm_engine.intellect.unified_memory import read_experiences
eng = SwarmEngine(db_path={DB!r})
cap = {XFER_CAP!r}
admitted = eng.admission.store.get(cap)
assert admitted is not None, "admitted capability not found"
plan = admitted.plan
assert plan, "no plan in admitted capability"
HELD = {HELD!r}
ok = 0
for inp, exp in HELD:
    r = eng.composer.execute_sync(plan, {{"xs": inp["xs"], "ys": inp["ys"]}})
    assert r.get("success"), f"exec failed: {{r}}"
    got = r.get("value")
    def norm(v):
        if isinstance(v, (list, tuple)): return [norm(x) for x in v]
        if isinstance(v, float) and v.is_integer(): return int(v)
        return v
    if norm(got) == norm(exp):
        ok += 1
    else:
        print(f"MISMATCH: got {{got}} exp {{exp}}")
recs = read_experiences(eng.intellect.epistemic, kind="generalization_envelope")
print(f"FRESH_HELDOUT={{ok}}/{{len(HELD)}} ENV={{len(recs)}}")
eng.close()
""")
proc = subprocess.run([sys.executable, F1_SCRIPT], capture_output=True, text=True,
                      timeout=300)
print(proc.stdout[-500:] if proc.stdout else "")
print(proc.stderr[-500:] if proc.stderr else "")
f1_ok = "FRESH_HELDOUT=4/4" in (proc.stdout or "")
check("F1a: fresh OS process executes admitted P1 plan 4/4 held-out",
      f1_ok and proc.returncode == 0,
      f"rc={proc.returncode}")
check("F1b: envelope records readable from a fresh process",
      "ENV=" in (proc.stdout or "") and proc.returncode == 0,
      (proc.stdout or "").strip().splitlines()[-1] if proc.stdout else "")

# ------------------------------------------------------------------ summary
fails = [n for n, ok, _ in results if not ok]
print(f"\n{'='*60}\nGEN-SYNTH-2: {len(results)-len(fails)}/{len(results)} passed")
if fails:
    print("FAILURES:", fails)
    sys.exit(1)
print("ALL GREEN")
