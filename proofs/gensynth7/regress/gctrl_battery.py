#!/usr/bin/env python3
"""GEN-CTRL-1: Generalization Controller as a runtime entity -- proof battery.

CRITICAL: imports from the WORKTREE under test
(~/workspace/worktrees/gensynth7-mission), never from the shared
canonical tree. Every check below exercises the real controller against
the real subordinated machinery and the frozen microcontroller substrate.

Stages:
  D0  square distilled through the real DistillationLoop (held-out,
      verdict admitted); technique_ref built
  D1  controller constructs against the frozen interface; loop registered;
      executive status starts idle
  D2  baseline envelope seeded from GEN-STRUCT-1's published envelope
  P1  cycle on -(x^2): crossed via compose, Q8-authenticated, admitted;
      envelope expansion recorded; microcontroller stack fully unwound;
      executive saw only "Generalization is active" while live
  P2  cycle on x^4: crossed via compose (autonomous second-order reuse)
  B1  cycle on 5/x^2: bound_marked, NEVER admitted; envelope records the
      bound; stack unwound
  A1  substrate honesty: re-retire -> already_retired; unknown id ->
      unknown_microcontroller; wrong-loop retire -> cross_loop
  A2  budget honesty: leg_budget_s=0.001 -> budget_exhausted; the ledger
      shows the exhausted leg honestly (no fabricated resolve)
  F1  FRESH PROCESS: new SwarmEngine on the same DB executes the admitted
      -(x^2) capability 4/4 held-out; envelope records readable

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

WORK = os.path.expanduser("~/workspace/worktrees/gensynth7-mission/proofs/gensynth6/regress_upstream/gate_gs5_2026-09-29/gensynth1_work_ctrl1")
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
    INTERFACE_VERSION, LOOP_GENERALIZATION, MC_RESOLVED)
from swarm_engine.synthesis.plan_composer import (
    PlanComposer, CompositionObjective)
from swarm_engine.primitives.core import NUM

results = []


def check(name, cond, detail=""):
    results.append((name, bool(cond), detail))
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" +
          (f" -- {detail}" if detail else ""), flush=True)


# ------------------------------------------------------------------ D0 distill
eng = SwarmEngine(db_path=DB)
EVIDENCE = [{"input": {"x": x}, "output": x * x}
            for x in (2, 3, 5, 7, 11, 1, 4, 9, 6)]
delta = DeltaRecord(
    objective="square the input",
    external_actions=("the external agent demonstrated squaring: for each "
                      "input x it produced x*x"),
    prior_capability="no squaring capability admitted in the engine",
    capability_gap=("REMOR could not square an input; the external agent "
                    "demonstrably could"),
    technique="multiply the input by itself",
    evidence=EVIDENCE,
    source="gen-ctrl-1-proof",
)
dres = DistillationLoop(eng).distill(delta)
check("D0: square distilled through the real loop",
      dres.success and dres.verdict_admitted,
      f"route={dres.route} heldout={dres.heldout_passed}/{dres.heldout_examples}")
if not dres.success:
    sys.exit(1)

technique_ref = {
    "promoted_name": dres.promoted_name,
    "capability_id": dres.capability_id,
    "delta_observation_id": dres.delta_id,
}

# ------------------------------------------------------------------ D1 construct
ctl = GeneralizationController(eng, leg_budget_s=1200.0)
check("D1a: substrate interface is the frozen v1",
      INTERFACE_VERSION == "microcontroller-interface/v1",
      INTERFACE_VERSION)
check("D1b: loop registered, status starts idle",
      ctl.loop_view().loop == LOOP_GENERALIZATION
      and "idle" in ctl.status(), ctl.status())

# ------------------------------------------------------------------ D2 baseline
base_id = ctl.seed_baseline_envelope()
env = ctl.envelope_for(technique_ref["promoted_name"])
base_raw = (env["baseline"][0].get("raw") or {}) if env["baseline"] else {}
check("D2: baseline envelope seeded from GEN-STRUCT-1's published envelope",
      base_id and base_raw.get("holds") == BASELINE_HOLDS
      and base_raw.get("breaks") == BASELINE_BREAKS,
      f"obs={base_id}")

PARAMS = {"x": NUM}
# D3: the contrast's forbidden list must name BOTH registrations of the
# technique (dual registration: acquired.{cap_id} + acquired.{promoted}).
# A silently-empty or half-empty list makes the causal contrast vacuous.
regs = ctl._technique_registrations(technique_ref)
check("D3: technique registrations resolved without prefix doubling",
      len(regs) == 2 and all(r.startswith("acquired.") for r in regs)
      and not any(r.startswith("acquired.acquired.") for r in regs),
      f"{regs}")
TRAIN_NEG = [({"x": 2}, -4), ({"x": 3}, -9), ({"x": 5}, -25)]
HELD_NEG = [({"x": 7}, -49), ({"x": 11}, -121), ({"x": 4}, -16), ({"x": 9}, -81)]

# ------------------------------------------------------------------ P1 -(x^2)
c1 = ctl.run_cycle(
    technique_ref=technique_ref,
    novel_goal="negate the square of x",
    train_examples=TRAIN_NEG, held_out=HELD_NEG,
    params=PARAMS, output_kind=NUM,
    gap_id="gen-ctrl-1:negate-square",
    admission_name="genctrl1_negate_square")
check("P1a: controller crossed -(x^2) via compose",
      c1.outcome == "crossed" and c1.mechanism == "compose"
      and c1.capability_id,
      f"outcome={c1.outcome} mech={c1.mechanism} cap={c1.capability_id} "
      f"reason={c1.reason}")
check("P1b: held-out 4/4 claimed on the cycle", c1.heldout == "4/4", c1.heldout)
check("P1c: stack unwound to the controller",
      c1.retired_clean and ctl.loop_view().active_count == 0
      and len(c1.leg_mc_ids) == 2,
      f"retired_clean={c1.retired_clean} legs={len(c1.leg_mc_ids)}")
mid_views = c1.executive_views_during_cycle[:-1]
check("P1d: executive saw only 'Generalization is active' while live",
      all(v == "active" for v in mid_views)
      and c1.executive_views_during_cycle[-1] == "idle",
      f"views={c1.executive_views_during_cycle}")
env1 = ctl.envelope_for(technique_ref["promoted_name"])
exp1 = [r for r in env1["technique_records"]
        if (r.get("raw") or {}).get("outcome") == "crossed"]
check("P1e: envelope expansion recorded",
      c1.envelope_observation_id
      and any((r.get("raw") or {}).get("capability_id") == c1.capability_id
              for r in exp1),
      f"env_obs={c1.envelope_observation_id}")
NEG_CAP = c1.capability_id

# ------------------------------------------------------------------ P2 x^4
# GEN-SYNTH-1 NOTE (2026-09-28): this expectation was updated from the
# original GEN-CTRL-1 battery. The binary-head extension completed the
# composer's plan space: the causal contrast now (correctly) finds
# power(abs(x), sqrt(16)) = |x|^4 = x^4 without the distilled square,
# so the technique is not causally necessary for x^4 and the controller
# honestly marks a bound instead of admitting a vacuous transfer. The
# original "crossed" was an artifact of the incomplete (unary-only) plan
# space. The second-order reuse MECHANISM is intact: the open search
# still finds square(square(x)) first (P2b). Original battery file
# (proofs/genctrl1_controller_2026-09-28.py) is UNCHANGED -- this update
# applies only to this GEN-SYNTH-1 regression copy.
TRAIN_P4 = [({"x": 2}, 16), ({"x": 3}, 81)]
HELD_P4 = [({"x": 4}, 256), ({"x": 5}, 625), ({"x": 6}, 1296), ({"x": 7}, 2401)]
c2 = ctl.run_cycle(
    technique_ref=technique_ref,
    novel_goal="raise x to the fourth power",
    train_examples=TRAIN_P4, held_out=HELD_P4,
    params=PARAMS, output_kind=NUM,
    gap_id="gen-ctrl-1:fourth-power",
    admission_name="genctrl1_fourth_power")
check("P2a: x^4 honestly marks a bound -- the completed plan space reveals "
      "the distilled square is not causally necessary (contrast finds "
      "power(abs(x),4))",
      c2.outcome == "bound_marked" and c2.mechanism == "compose"
      and "causal contrast failed" in c2.reason
      and not c2.capability_id and c2.retired_clean
      and ctl.loop_view().active_count == 0,
      f"outcome={c2.outcome} mech={c2.mechanism} cap={c2.capability_id} "
      f"reason={c2.reason[:120]}")
pc_p2 = PlanComposer(eng.composer)
obj_p2 = CompositionObjective(
    goal="x^4 open", gap_id="gen-synth-1:p2b", params=PARAMS,
    output_kind=NUM, examples=TRAIN_P4, held_out=[])
rp2 = pc_p2.compose(obj_p2)
sq_regs = set(ctl._technique_registrations(technique_ref))
check("P2b: the second-order reuse mechanism is intact -- the open search "
      "still finds square(square(x))",
      rp2.found and rp2.composed_of
      and all(c in sq_regs for c in rp2.composed_of),
      f"found={rp2.found} composed_of={rp2.composed_of}")

# ------------------------------------------------------------------ B1 5/x^2 (must fail)
TRAIN_DIV = [({"x": 2}, 1.25), ({"x": 5}, 0.2)]
HELD_DIV = [({"x": 4}, 0.3125), ({"x": 10}, 0.05), ({"x": 1}, 5.0),
            ({"x": 8}, 0.078125)]
c3 = ctl.run_cycle(
    technique_ref=technique_ref,
    novel_goal="divide five by the square of x",
    train_examples=TRAIN_DIV, held_out=HELD_DIV,
    params=PARAMS, output_kind=NUM,
    gap_id="gen-ctrl-1:div5-square",
    admission_name="genctrl1_div5_square")
check("B1a: 5/x^2 recorded as a marked bound, never admitted",
      c3.outcome == "bound_marked" and c3.mechanism == "compose"
      and not c3.capability_id,
      f"outcome={c3.outcome} mech={c3.mechanism} reason={c3.reason}")
check("B1b: stack unwound on the bound path too",
      c3.retired_clean and ctl.loop_view().active_count == 0,
      f"retired_clean={c3.retired_clean}")
env3 = ctl.envelope_for(technique_ref["promoted_name"])
bmarks = [r for r in env3["technique_records"]
          if (r.get("raw") or {}).get("outcome") == "bound_marked"]
check("B1c: bound record names the obstruction",
      bmarks and any((r.get("raw") or {}).get("breaks") for r in bmarks),
      f"bound_records={len(bmarks)}")

# ------------------------------------------------------------------ A1 substrate honesty
sub = ctl.substrate
r = sub.spawn(LOOP_GENERALIZATION, purpose="a1-probe", budget_s=60.0)
assert r.ok, f"a1 spawn failed: {r.refusal}"
mid = r.mc.mc_id
first = sub.retire(mid, MC_RESOLVED, loop=LOOP_GENERALIZATION)
again = sub.retire(mid, MC_RESOLVED, loop=LOOP_GENERALIZATION)
check("A1a: re-retire refused already_retired",
      getattr(again, "reason", "") == "already_retired",
      f"reason={getattr(again, 'reason', '')}")
unknown = sub.retire("mc-deadbeef1234", MC_RESOLVED,
                     loop=LOOP_GENERALIZATION)
check("A1b: unknown id refused unknown_microcontroller",
      getattr(unknown, "reason", "") == "unknown_microcontroller",
      f"reason={getattr(unknown, 'reason', '')}")
r2 = sub.spawn(LOOP_GENERALIZATION, purpose="a1-cross", budget_s=60.0)
assert r2.ok
cross = sub.retire(r2.mc.mc_id, MC_RESOLVED, loop="acquisition")
check("A1c: cross-loop retire refused cross_loop",
      getattr(cross, "reason", "") == "cross_loop",
      f"reason={getattr(cross, 'reason', '')}")
sub.retire(r2.mc.mc_id, MC_RESOLVED, loop=LOOP_GENERALIZATION)

# ------------------------------------------------------------------ A2 budget honesty
tiny = GeneralizationController(eng, leg_budget_s=0.001)
c4 = tiny.run_cycle(
    technique_ref=technique_ref,
    novel_goal="negate the square of x",
    train_examples=TRAIN_NEG, held_out=HELD_NEG,
    params=PARAMS, output_kind=NUM,
    gap_id="gen-ctrl-1:budget-probe",
    admission_name="genctrl1_budget_probe")
ledger = tiny.substrate.retired_ledger()
leg_recs = [m for m in ledger if m.mc_id in c4.leg_mc_ids]
check("A2a: tiny budget -> budget_exhausted, honestly",
      c4.outcome == "budget_exhausted" and c4.mechanism == "generalize"
      and not c4.capability_id,
      f"outcome={c4.outcome} mech={c4.mechanism}")
check("A2b: exhausted leg ledgered as exhausted, never fabricated-resolve",
      leg_recs and all(m.outcome == "exhausted" for m in leg_recs)
      and not any(m.fabrication_flags for m in leg_recs),
      f"leg_outcomes={[m.outcome for m in leg_recs]} "
      f"flags={[m.fabrication_flags for m in leg_recs]}")
check("A2c: root retired clean after exhaustion",
      c4.retired_clean and tiny.loop_view().active_count == 0,
      f"retired_clean={c4.retired_clean}")

# ------------------------------------------------------------------ F1 fresh process
# The parent engine holds the DB owner lock: close it first so a genuinely
# fresh OS process can claim the DB (true process freshness, stronger than
# GEN-STRUCT-1's in-process re-open).
eng.close()
fresh_src = (
    "import json\n"
    "import os, sys\n"
    f"sys.path.insert(0, {os.path.join(WT, 'pylib')!r})\n"
    f"sys.path.insert(0, {WT!r})\n"
    "from swarm_engine.core.engine import SwarmEngine\n"
    "from swarm_engine.intellect.unified_memory import read_experiences\n"
    f"eng = SwarmEngine(db_path={DB!r})\n"
    f"HELD = {HELD_NEG!r}\n"
    f"cap = {NEG_CAP!r}\n"
    "res = eng.composer.execute_sync(\n"
    "    eng.admission.store.get(cap).plan,\n"
    '    {"x": 7})\n'
    'ok = res.get("success") and res.get("value") == -49\n'
    "vals = []\n"
    "for args, want in HELD:\n"
    "    r = eng.composer.execute_sync(eng.admission.store.get(cap).plan, args)\n"
    '    vals.append(r.get("success") and abs(r.get("value") - want) < 1e-9)\n'
    'recs = read_experiences(eng.intellect.epistemic, kind="generalization_envelope")\n'
    'base = read_experiences(eng.intellect.epistemic, kind="generalization_envelope_baseline")\n'
    'print(json.dumps({"single": ok, "held4": vals, "env_recs": len(recs),\n'
    '                   "base_recs": len(base)}))\n'
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
    check("F1a: fresh engine executes the controller-admitted -(x^2)",
          f1.get("single") and f1.get("held4") == [True] * 4,
          f"single={f1.get('single')} held4={f1.get('held4')}")
    check("F1b: envelope records readable from a fresh process",
          f1.get("env_recs", 0) >= 3 and f1.get("base_recs", 0) >= 1,
          f"env={f1.get('env_recs')} base={f1.get('base_recs')}")

# ------------------------------------------------------------------ summary
fails = [n for n, ok, _ in results if not ok]
print(f"\n{len(results) - len(fails)}/{len(results)} checks passed")
if fails:
    print("FAILURES:", fails)
    sys.exit(1)
print("GEN-CTRL-1 proof battery GREEN")
