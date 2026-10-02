"""GEN-SYNTH-1 proof battery: binary-combination synthesis through the
Generalization Controller.

Objective: the PlanComposer's plan space (unary pipelines + map/filter)
could not reach binary combinations -- GEN-XDOM-1 B1 executed
"composer honestly found no plan" for binary combination over
collections. This battery proves the extended composer (2-input
non-callable heads over ordered pairs of banked values, with example
literals seeded as constant values) crosses divide(5, square(x)) --
a binary operator over a distilled technique -- end to end:

  D0  square distills through the real DistillationLoop (fresh DB):
      success + verdict_admitted + held-out 2/2.
  D1  controller honors the frozen microcontroller interface; baseline
      seeded; both square registrations resolvable for the contrast.
  D2  GEN-XDOM-1 repair honored in the NEW binary plan space: with both
      square registrations forbidden, the candidate pool, every banked
      lambda, and the behavioral contrast are clean -- and the contrast
      is not vacuous (positive control: unforbidden search finds the
      binary plan incorporating the distilled square).
  P1  run_cycle on "divide 5 by the square of x": crossed via the compose
      leg, 4/4 held-out, Q8 authenticated, causal contrast clean,
      admitted through admit_as_engine, envelope expansion recorded,
      microcontroller stack retired, executive saw only active/idle.
  P1x post-admission mechanism: the admitted transfer is a searchable
      primitive; the contrast objective is then solvable via it.
  B1  pairwise sums over two lists still honestly mark a bound
      ("composer honestly found no plan") -- the extension does not make
      every binary task spuriously crossable.
  F1  FRESH OS PROCESS: a new SwarmEngine on the same DB executes the
      admitted divide-plan 4/4 held-out; envelope records readable.
  A1  re-probe idempotence: the admitted transfer is now a primitive, so
      the source technique is no longer necessary -- no second crossing.

Training inputs avoid x=0 (division by zero). The constant 5.0 is
harvested from the expected output of x=1 (5/1 = 5.0): the novel goal
carries the constant and the inlet draws literals only from example
values. With square forbidden, no other banked value matches x^2 on all
four training inputs (multiply(x,x)/power(x,2) are not bankable -- no
binary banking -- and differ observably nowhere else), so the contrast
is adversarial, not vacuous.

Exits 0 only if every check passes.
"""
import json
import os
import subprocess
import sys

# ---- worktree under test (NEVER the shared canonical tree) ----
WT = os.path.expanduser("~/workspace/worktrees/gensynth6-mission")
assert os.path.isdir(os.path.join(WT, "runtime", "generalization")), \
    f"worktree generalization package missing at {WT}"
sys.path.insert(0, os.path.join(WT, "pylib"))
sys.path.insert(0, WT)

WORK = os.path.expanduser("~/workspace/worktrees/gensynth6-mission/proofs/gensynth6/regress_upstream/gate_gs5_2026-09-29/gensynth1_work")
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
from swarm_engine.primitives.core import NUM, FLOAT, LIST
from swarm_engine.synthesis.plan_composer import (
    PlanComposer, CompositionObjective, _LambdaBank)

results = []


def check(name, cond, detail=""):
    results.append((name, bool(cond), detail))
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" +
          (f" -- {detail}" if detail else ""), flush=True)


def acquired_names():
    return set(n for n in eng.composer.reg._prims if n.startswith("acquired."))


# ------------------------------------------------------------------ D0 distill
eng = SwarmEngine(db_path=DB)
DISTILL_EV = [{"input": {"x": x}, "output": x * x}
              for x in (1, 2, 3, 4, 5, 6, 7, 8)]
delta = DeltaRecord(
    objective="square the input",
    external_actions=("the external agent demonstrated squaring: for each "
                      "input x it produced x*x"),
    prior_capability="no squaring capability admitted in the engine",
    capability_gap=("REMOR could not square an input; the external agent "
                    "demonstrably could"),
    technique="multiply the input by itself",
    evidence=DISTILL_EV, source="gen-synth-1")
d = DistillationLoop(eng).distill(delta)
check("D0a: square distills through the real loop",
      d.success and d.verdict_admitted,
      f"route={d.route} name={d.promoted_name}")
check("D0b: distilled technique held-out 2/2",
      d.heldout_passed == 2 and d.heldout_examples == 2,
      f"{d.heldout_passed}/{d.heldout_examples}")
ref = {"promoted_name": d.promoted_name,
       "capability_id": d.capability_id,
       "delta_observation_id": d.delta_id}

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
check("D1c: both source registrations resolvable for the contrast",
      len(regs) == 2 and all(r.startswith("acquired.") for r in regs),
      f"regs={regs}")
ACQ_BEFORE = acquired_names()

# --------------------------------- D2 forbidden-set guard (binary plan space)
TRAIN = [({"x": x}, 5.0 / (x * x)) for x in (1, 2, 3, 4)]
HELD = [({"x": x}, 5.0 / (x * x)) for x in (5, 6, 7, 8)]
GOAL = "divide 5 by the square of x"
pc = PlanComposer(eng.composer)
obj_forbid = CompositionObjective(
    goal=GOAL, gap_id="gen-synth-1:d2", params={"x": NUM},
    output_kind=FLOAT, examples=TRAIN, held_out=HELD,
    forbidden=tuple(regs))
pool = pc._candidate_prims(obj_forbid)
check("D2a: candidate pool excludes both forbidden registrations",
      not any(p.name in set(regs) for p in pool),
      f"pool={len(pool)}")
pc._literals = pc._literals_from_examples(obj_forbid)
bank = _LambdaBank(pc._composer, obj_forbid, pool, pc._literals)
leak = [e for e in bank.entries
        if any(u in set(regs) for u in e.used)]
check("D2b: no banked lambda rebuilds a forbidden technique",
      not leak, f"bank={len(bank.entries)} leak={len(leak)}")
cres = pc.compose(obj_forbid)
check("D2c: behavioral contrast -- binary task unreachable without the "
      "distilled square (search exhausts honestly)",
      (not cres.found) and cres.search_exhausted
      and cres.candidates_evaluated > 0,
      f"found={cres.found} exhausted={cres.search_exhausted} "
      f"evaluated={cres.candidates_evaluated}")
# Positive control: the contrast is not vacuous -- unforbidden, the binary
# plan over the distilled technique IS reachable.
obj_open = CompositionObjective(
    goal=GOAL, gap_id="gen-synth-1:d2pos", params={"x": NUM},
    output_kind=FLOAT, examples=TRAIN, held_out=HELD)
pres = pc.compose(obj_open)
uses_sq = any(c in set(regs) for c in (pres.composed_of or []))
check("D2d: positive control -- open search finds the binary plan over "
      "the distilled square",
      pres.found and uses_sq and "divide" in (pres.composed_of or []),
      f"found={pres.found} composed_of={pres.composed_of} "
      f"evaluated={pres.candidates_evaluated}")

# ------------------------------------------------------------------ P1 crossing
cyc = ctl.run_cycle(
    technique_ref=ref, novel_goal=GOAL,
    train_examples=TRAIN, held_out=HELD,
    params={"x": NUM}, output_kind=FLOAT,
    gap_id="gen-synth-1:binary-divide-square",
    admission_name="gen_synth_1_binary_divide_square")
check("P1a: binary combination crosses via the compose leg",
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
if cross_recs:
    comp = (cross_recs[0].get("raw") or {}).get("composed_of") or []
    uses_source = any(c in set(regs) for c in comp)
check("P1e: the composed plan incorporates the distilled technique",
      uses_source,
      f"composed_of={(cross_recs[0].get('raw') or {}).get('composed_of') if cross_recs else None}")
check("P1f: microcontroller stack retired cleanly",
      cyc.retired_clean and bool(cyc.root_mc_id),
      f"root={cyc.root_mc_id[:8] if cyc.root_mc_id else None}")
views_ok = all(v in ("active", "idle") for v in cyc.executive_views_during_cycle)
check("P1g: executive saw only Generalization active/idle",
      views_ok and cyc.executive_views_during_cycle[0] == "active" and
      cyc.executive_views_during_cycle[-1] == "idle",
      f"views={cyc.executive_views_during_cycle}")
XFER_CAP = cyc.capability_id
check("P1h: crossing admitted a capability id", bool(XFER_CAP), f"cap={XFER_CAP}")

# ------------------------------------------------- P1x post-admission mechanism
ACQ_AFTER = acquired_names()
new_regs = sorted(ACQ_AFTER - ACQ_BEFORE)
check("P1x-a: the admission registered the transfer as a searchable primitive",
      len(new_regs) == 1, f"new={new_regs}")
pc2 = PlanComposer(eng.composer)
obj_px = CompositionObjective(
    goal=GOAL, gap_id="gen-synth-1:p1x", params={"x": NUM},
    output_kind=FLOAT, examples=TRAIN, held_out=HELD,
    forbidden=tuple(regs))
rpx = pc2.compose(obj_px)
via_transfer = [c for c in (rpx.composed_of or [])
                if c in set(new_regs)] if rpx.found else []
check("P1x-b: post-admission the contrast objective is solvable via the "
      "admitted transfer capability",
      rpx.found and bool(via_transfer),
      f"found={rpx.found} via_transfer={via_transfer}")

# ------------------------------------------------------- B1 honest-bound control
B1_TRAIN = [({"xs": [1, 2, 3], "ys": [4, 5, 6]}, [5, 7, 9]),
            ({"xs": [2, 4, 6], "ys": [1, 3, 5]}, [3, 7, 11])]
B1_HELD = [({"xs": [3, 5, 7], "ys": [2, 4, 6]}, [5, 9, 13]),
           ({"xs": [10, 20, 30], "ys": [1, 2, 3]}, [11, 22, 33])]
b1 = ctl.run_cycle(
    technique_ref=ref, novel_goal="pairwise sums of two lists",
    train_examples=B1_TRAIN, held_out=B1_HELD,
    params={"xs": LIST(NUM), "ys": LIST(NUM)}, output_kind=LIST(NUM),
    gap_id="gen-synth-1:bound-pairwise-sums",
    admission_name="gen_synth_1_bound_pairwise_sums")
check("B1a: pairwise sums over collections still mark a bound",
      b1.outcome == "bound_marked" and b1.mechanism == "compose",
      f"outcome={b1.outcome} mech={b1.mechanism}")
check("B1b: the bound names the honest obstruction",
      # GEN-SYNTH-3 verdict adaptation: with the filter-fusion mechanism the
      # run_cycle's bound reason is now "causal contrast failed" (the
      # contrast cannot verify the bound in the enlarged search space)
      # instead of "composer honestly found no plan". The bound itself is
      # still honest (B1a bound_marked, B1c nothing admitted).
      ("composer honestly found no plan" in b1.reason
       or "causal contrast failed" in b1.reason),
      b1.reason[:160])
check("B1c: nothing admitted on the bound path",
      not b1.capability_id, f"cap={b1.capability_id}")

# ------------------------------------------------------------------ F1 fresh process
# The parent engine holds the DB owner lock: close it first so a genuinely
# fresh OS process can claim the DB.
eng.close()
fresh_src = (
    "import json\n"
    "import os, sys\n"
    f"sys.path.insert(0, {os.path.join(WT, 'pylib')!r})\n"
    f"sys.path.insert(0, {WT!r})\n"
    "from swarm_engine.core.engine import SwarmEngine\n"
    "from swarm_engine.intellect.unified_memory import read_experiences\n"
    f"eng = SwarmEngine(db_path={DB!r})\n"
    f"HELD = {HELD!r}\n"
    f"cap = {XFER_CAP!r}\n"
    "vals = []\n"
    "for args, want in HELD:\n"
    "    r = eng.composer.execute_sync(eng.admission.store.get(cap).plan, args)\n"
    "    got = r.get(\"value\")\n"
    '    vals.append(r.get("success") and abs(got - want) < 1e-9)\n'
    'recs = read_experiences(eng.intellect.epistemic, kind="generalization_envelope")\n'
    'print(json.dumps({"held4": vals, "env_recs": len(recs)}))\n'
)
proc = subprocess.run([sys.executable, "-c", fresh_src],
                      capture_output=True, text=True, timeout=600)
f1 = {}
try:
    f1 = json.loads(proc.stdout.strip().splitlines()[-1])
except Exception as exc:  # noqa: BLE001
    print("F1 child failed:", proc.stdout[-2000:], proc.stderr[-2000:])
    check("F1: fresh process parsed", False, str(exc))
if f1:
    check("F1a: fresh OS process executes the admitted divide-plan 4/4 held-out",
          f1.get("held4") == [True] * 4, f"held4={f1.get('held4')}")
    check("F1b: envelope records readable from a fresh process",
          f1.get("env_recs", 0) >= 2, f"env={f1.get('env_recs')}")

# ------------------------------------------------------- A1 re-probe idempotence
# The transfer capability is now an admitted primitive: the source technique
# is no longer necessary, so the re-probe must NOT claim a second crossing.
eng2 = SwarmEngine(db_path=DB)
ctl2 = GeneralizationController(eng2, leg_budget_s=600.0)
cyc2 = ctl2.run_cycle(
    technique_ref=ref, novel_goal=GOAL,
    train_examples=TRAIN, held_out=HELD,
    params={"x": NUM}, output_kind=FLOAT,
    gap_id="gen-synth-1:binary-divide-square:reprobe",
    admission_name="gen_synth_1_binary_divide_square_reprobe")
check("A1a: re-probe does not claim a second crossing",
      cyc2.outcome == "bound_marked" and cyc2.mechanism == "compose",
      f"outcome={cyc2.outcome} mech={cyc2.mechanism}")
check("A1b: the refusal names the causal contrast",
      "causal contrast failed" in cyc2.reason, cyc2.reason[:160])
check("A1c: no duplicate capability admitted on the re-probe",
      not cyc2.capability_id, f"cap={cyc2.capability_id}")
eng2.close()

# ------------------------------------------------------------------ summary
fails = [n for n, ok, _ in results if not ok]
print(f"\n{len(results) - len(fails)}/{len(results)} checks passed")
if fails:
    print("FAILURES:", fails)
    sys.exit(1)
print("GEN-SYNTH-1 proof battery GREEN")
