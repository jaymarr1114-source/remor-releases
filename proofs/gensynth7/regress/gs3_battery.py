"""GEN-SYNTH-3 proof battery: filter fusion over binary intermediates.

Objective: GEN-SYNTH-2 banked one level of binary intermediate and completed
it via map. Chains where the binary intermediate feeds FILTER (rather than
map) were untested -- the post-pass only completed via map(M, L). This
battery proves the extended composer (filter-fusion completion in the
binary post-pass: filter(M, pred) for banked BOOL predicates with a sound
multiset/length pre-filter, then the map(F, L) lookahead) crosses the
causal task:

    map(filter(map(zip(xs, ys), sum), x > 10), T)   T(x) = x + 11 (distilled)

end to end:

  D0  T(x)=x+11 distills through the real DistillationLoop (fresh DB):
      success + verdict_admitted + held-out 2/2.
  D1  controller honors the frozen microcontroller interface; baseline
      seeded; the distilled T registration resolvable for the contrast.
  D2  GEN-XDOM-1 repair honored in the NEW filter-fusion plan space:
      with the distilled T forbidden, the candidate pool, every banked
      lambda (including BOOL predicate lambdas used by the filter path),
      the dynamic pair-lambdas, and the per-compose cache are clean --
      and the contrast is not vacuous (positive control: unforbidden
      search finds the filter-fusion plan incorporating the distilled T).
  P1  run_cycle on "pairwise sums, keep those greater than ten, then add
      eleven to each": crossed via the compose leg, 4/4 held-out, Q8
      authenticated, causal contrast clean, admitted through
      admit_as_engine, envelope expansion recorded, microcontroller stack
      retired, executive saw only active/idle.
  B1  plain filter fusion (bankable predicate, no distilled T) crosses as
      the mechanism control -- the binary intermediate genuinely feeds
      filter. Training examples disambiguate the intended
      sum+greater_than(x,10) decomposition from the observationally
      similar mean-based alternative on the earlier probe.
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

WORK = os.path.expanduser("~/workspace/worktrees/gensynth7-mission/proofs/gensynth6/regress_upstream/gate_gs5_2026-09-29/gensynth3_work")
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
    evidence=DISTILL_EV, source="gen-synth-3")
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
ctl = GeneralizationController(eng, leg_budget_s=900.0)
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

# --------------------------------- D2 forbidden-set guard (filter-fusion path)
# With T forbidden, the candidate pool, the static lambda bank (including
# the BOOL predicate lambdas the filter-fusion path draws on), the dynamic
# pair-lambdas, and the per-compose cache must all be clean of T, and the
# search must honestly find no plan (not vacuous: P1 below finds it).
#
# Examples are 11-free (no 11 in any input or output) so x+11 is not
# rebuildable from banked literals; the predicate threshold 10 IS an
# example literal so the filter step itself is bankable and the contrast
# isolates exactly the distilled T. ex2 pins the threshold: the pairwise
# sum 10 must be DROPPED, disambiguating greater_than(x,10) from
# greater_or_equal(x,10) (the latter fits the other examples but fails
# held-out).
TRAIN = [({"xs": [2, 3, 4], "ys": [10, 10, 10]}, [23, 24, 25]),
         ({"xs": [5, 0, 0], "ys": [5, 5, 25]}, [36]),
         ({"xs": [2, 2, 2], "ys": [3, 4, 20]}, [33]),
         ({"xs": [9, 9, 9], "ys": [3, 3, 3]}, [23, 23, 23])]
HELD = [({"xs": [4, 4, 4], "ys": [8, 8, 8]}, [23, 23, 23]),
        ({"xs": [0, 5, 10], "ys": [5, 5, 5]}, [26]),
        ({"xs": [6, 1, 1], "ys": [6, 6, 6]}, [23]),
        ({"xs": [2, 8, 1], "ys": [9, 3, 12]}, [22, 22, 24])]
GOAL = ("pairwise sums of two lists, keeping only those greater than ten, "
        "then add eleven to each")

pc = PlanComposer(eng.composer)
obj_forbid = CompositionObjective(
    goal=GOAL, gap_id="gen-synth-3:d2", params={"xs": LIST(NUM), "ys": LIST(NUM)},
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
# The filter-fusion path draws BOOL predicate lambdas from the bank:
# they must be clean too.
bool_lams = [l for l in bank.entries
             if getattr(l.output_kind, "kind", None)
             and l.output_kind.kind.name == "BOOL"]
check("D2c: BOOL predicate lambdas clean of forbidden T",
      bool_lams and not any(set(regs) & set(l.used or [])
                            for l in bool_lams),
      f"bool_lams={len(bool_lams)}")
# Dynamic pair-lambdas from real pair values must also be clean.
pair_lams = pc._pair_lambdas((([2, 10], [3, 10], [4, 10]),), pool)
check("D2d: dynamic pair-lambdas clean of forbidden T",
      not any(set(regs) & set(l.used or []) for l in pair_lams),
      f"pair_lams={len(pair_lams)}")
r_forbid = pc.compose(obj_forbid)
check("D2e: forbidden-T search honestly finds no plan",
      not r_forbid.found,
      f"found={r_forbid.found} exhausted={r_forbid.search_exhausted} "
      f"eval={r_forbid.candidates_evaluated}")
# Cache isolation: a second compose on the same composer must not reuse
# lambdas built from the forbidden prim.
r_forbid2 = pc.compose(obj_forbid)
check("D2f: per-compose cache reset (no cross-run leakage)",
      not r_forbid2.found and pc._pair_lam_cache is not None,
      f"found={r_forbid2.found}")

# ------------------------------------------------------------------ P1 crossing
cyc = ctl.run_cycle(
    technique_ref=ref, novel_goal=GOAL,
    train_examples=TRAIN, held_out=HELD,
    params={"xs": LIST(NUM), "ys": LIST(NUM)}, output_kind=LIST(NUM),
    gap_id="gen-synth-3:filter-fusion-then-T",
    admission_name="gen_synth_3_filter_fusion_then_T")
check("P1a: filter-fusion task crosses via the compose leg",
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
check("P1d: envelope expansion recorded for the filter-fusion crossing",
      len(cross_recs) >= 1, f"records={len(cross_recs)}")
uses_source = False
has_zip = False
has_filter = False
if cross_recs:
    comp = (cross_recs[0].get("raw") or {}).get("composed_of") or []
    uses_source = any(c in set(regs) for c in comp)
    # The plan must show the binary intermediate feeding filter then map:
    # zip (binary) -> map(sum) -> filter(pred) -> map(T).
    has_zip = "zip" in comp
    has_filter = "filter" in comp
check("P1e: the composed plan incorporates the distilled T",
      uses_source,
      f"composed_of={(cross_recs[0].get('raw') or {}).get('composed_of') if cross_recs else None}")
check("P1f: plan shows binary intermediate feeding filter",
      has_zip and has_filter, f"composed_of={comp if cross_recs else None}")
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
# ex1 pins the threshold from above (pairwise sum 11 is KEPT, ruling out
# greater_or_equal(x,12)); ex2 pins it from the middle (sum 10 is DROPPED:
# greater_than, not greater_or_equal); ex3 pins it from below (sum 9 is
# dropped: the threshold is 10, not 8).
B1_TRAIN = [({"xs": [2, 3, 1], "ys": [9, 10, 10]}, [11, 13, 11]),
            ({"xs": [5, 0, 0], "ys": [5, 5, 25]}, [25]),
            ({"xs": [1, 1, 1], "ys": [8, 8, 8]}, []),
            ({"xs": [9, 9, 9], "ys": [3, 3, 3]}, [12, 12, 12])]
B1_HELD = [({"xs": [4, 4, 4], "ys": [8, 8, 8]}, [12, 12, 12]),
           ({"xs": [0, 5, 10], "ys": [5, 5, 5]}, [15]),
           ({"xs": [6, 1, 1], "ys": [6, 6, 6]}, [12]),
           ({"xs": [2, 8, 1], "ys": [9, 3, 12]}, [11, 11, 13])]
pc_b1 = PlanComposer(eng.composer)
obj_b1 = CompositionObjective(
    goal="pairwise sums of two lists, keeping only those greater than ten",
    gap_id="gen-synth-3:b1",
    params={"xs": LIST(NUM), "ys": LIST(NUM)}, output_kind=LIST(NUM),
    examples=B1_TRAIN, held_out=B1_HELD)
r_b1 = pc_b1.compose(obj_b1)
check("B1a: plain filter fusion crosses (binary intermediate feeds filter)",
      r_b1.found and "filter" in (r_b1.composed_of or [])
      and "zip" in (r_b1.composed_of or []),
      f"found={r_b1.found} composed_of={r_b1.composed_of}")
if r_b1.found:
    b1_ok, b1_reasons = q8_authenticate(
        r_b1.plan, eng.composer, B1_TRAIN, B1_HELD)
    check("B1b: Q8 authenticates the filter-fusion plan on train+held-out",
          b1_ok, "; ".join(b1_reasons))
else:
    check("B1b: Q8 authenticates the filter-fusion plan on train+held-out",
          False, "no plan found")

# ------------------------------------------------------------------ F1 fresh process
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
print(f"\n{'='*60}\nGEN-SYNTH-3: {len(results)-len(fails)}/{len(results)} passed")
if fails:
    print("FAILURES:", fails)
    sys.exit(1)
print("ALL GREEN")
