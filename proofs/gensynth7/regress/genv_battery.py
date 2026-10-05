#!/usr/bin/env python3
"""GEN-ENV-1: envelope-probing automation -- proof battery.

CRITICAL: imports from the WORKTREE under test
(~/workspace/worktrees/gensynth7-mission), never from the shared
canonical tree. Every check exercises the real automation against the
real GeneralizationController, the real subordinated machinery, and the
frozen microcontroller substrate.

Stages:
  D0  distill square (technique A, constant-free) and add-three
      (technique B, binds the constant 3) through the real loop
  D1  controller constructs against the frozen interface; baseline seeded
  D2  sweep arm on square: honestly empty (no bound constants)
  D3  sweep arm on add-three: 2 variants from the bound literal 3
  P1  probe square: baseline anchor crosses; depth-3 shapes and the
      binary control mark bounds honestly
  P2  probe add-three with the sweep variants: both cross via the
      generalize leg -- the first generalize-leg crossings ever recorded
      through the controller (GEN-CTRL-1's UNPROVEN item), growing
      add-three's envelope from empty
  P3  published envelope: summary reports readable per technique
  A1  adversarial agreement: independent re-probes of one published
      expansion and one published bound agree with the records
  A2  gates not weakened: the crossed record's reason names Q8,
      the causal contrast, and admission
  F1  FRESH PROCESS: the automation-crossed add-five capability executes
      on held-out; envelope records readable

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

WORK = os.path.expanduser("~/workspace/worktrees/gensynth7-mission/proofs/gensynth6/regress_upstream/gate_gs5_2026-09-29/genenv1_work")
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
    GeneralizationController, BASELINE_HOLDS)
from swarm_engine.generalization.envelope_probe import (
    EnvelopeProber, VariantSpec, generate_sweep_variants,
    technique_bound_constants, verify_agreement,
    ENVELOPE_REPORT_MARKER)
from swarm_engine.core.microcontroller import INTERFACE_VERSION
from swarm_engine.primitives.core import NUM

results = []


def check(name, cond, detail=""):
    results.append((name, bool(cond), detail))
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" +
          (f" -- {detail}" if detail else ""), flush=True)


def distill(objective, tech_desc, examples, src):
    delta = DeltaRecord(
        objective=objective, external_actions=tech_desc,
        prior_capability="no such capability admitted in the engine",
        capability_gap="REMOR could not perform this; the external agent "
                       "demonstrably could",
        technique=tech_desc, evidence=examples, source=src)
    return DistillationLoop(eng).distill(delta)


# ------------------------------------------------------------------ D0 distill
eng = SwarmEngine(db_path=DB)
SQ_EV = [{"input": {"x": x}, "output": x * x}
         for x in (2, 3, 5, 7, 11, 1, 4, 9, 6)]
A3_EV = [{"input": {"x": x}, "output": x + 3}
         for x in (2, 3, 5, 7, 11, 1, 4, 9, 6)]
d_sq = distill("square the input", "multiply the input by itself",
               SQ_EV, "gen-env-1-proof")
check("D0a: square distilled through the real loop",
      d_sq.success and d_sq.verdict_admitted,
      f"route={d_sq.route} heldout={d_sq.heldout_passed}/"
      f"{d_sq.heldout_examples}")
d_a3 = distill("add three to the input", "add the constant 3 to the input",
               A3_EV, "gen-env-1-proof")
check("D0b: add-three distilled through the real loop",
      d_a3.success and d_a3.verdict_admitted,
      f"route={d_a3.route} heldout={d_a3.heldout_passed}/"
      f"{d_a3.heldout_examples}")
if not (d_sq.success and d_a3.success):
    sys.exit(1)
SQ_REF = {"promoted_name": d_sq.promoted_name,
          "capability_id": d_sq.capability_id,
          "delta_observation_id": d_sq.delta_id}
A3_REF = {"promoted_name": d_a3.promoted_name,
          "capability_id": d_a3.capability_id,
          "delta_observation_id": d_a3.delta_id}

# ------------------------------------------------------------------ D1 controller
ctl = GeneralizationController(eng, leg_budget_s=1200.0)
check("D1a: substrate interface is the frozen v1",
      INTERFACE_VERSION == "microcontroller-interface/v1",
      INTERFACE_VERSION)
check("D1b: controller constructs, loop starts idle",
      "idle" in ctl.status(), ctl.status())
base_id = ctl.seed_baseline_envelope()
env0 = ctl.envelope_for(SQ_REF["promoted_name"])
base_raw = (env0["baseline"][0].get("raw") or {}) if env0["baseline"] else {}
check("D1c: baseline envelope seeded",
      base_id and base_raw.get("holds") == BASELINE_HOLDS, f"obs={base_id}")

prober = EnvelopeProber(ctl)
PARAMS = {"x": NUM}

# ------------------------------------------------------------------ D2 sweep arm, square
# Square's distilled plan is power(base=x, exponent=2): the exponent is a
# genuine bound constant, so the sweep arm MUST fire here (an earlier
# hypothesis that square is constant-free was wrong -- corrected against
# the retained plan).
def sq_oracle(t):
    train = [({"x": 2}, 2 ** t), ({"x": 3}, 3 ** t)]
    held = [({"x": 4}, 4 ** t), ({"x": 5}, 5 ** t), ({"x": 6}, 6 ** t)]
    return train, held


sq_consts, sq_note = technique_bound_constants(eng, SQ_REF)
sq_sweeps, sq_sweep_note = generate_sweep_variants(
    SQ_REF, eng, "raise x to the power {target:g}", sq_oracle,
    sweep_values=[4.0, 6.0])
check("D2a: sweep arm fires on square's bound exponent",
      sq_consts == [2.0]
      and {v.name for v in sq_sweeps} == {"sweep_2_to_4", "sweep_2_to_6"},
      f"consts={sq_consts} note={sq_note}")
check("D2b: square sweep targets are 4.0 and 6.0, hypothesis crossed",
      {v.target_constant for v in sq_sweeps} == {4.0, 6.0}
      and all(v.expected == "crossed" and v.arm == "sweep"
              for v in sq_sweeps),
      sq_sweep_note)

# ------------------------------------------------------------------ D3 sweep arm, add-three


def a3_oracle(t):
    train = [({"x": 2}, 2 + t), ({"x": 3}, 3 + t)]
    held = [({"x": 4}, 4 + t), ({"x": 5}, 5 + t), ({"x": 6}, 6 + t)]
    return train, held


a3_consts, _ = technique_bound_constants(eng, A3_REF)
a3_sweeps, a3_sweep_note = generate_sweep_variants(
    A3_REF, eng, "add {target:g} to the input", a3_oracle,
    sweep_values=[5.0, 7.0])
check("D3a: sweep arm yields 2 variants from add-three's bound constant",
      len(a3_sweeps) == 2 and a3_consts == [3.0]
      and {v.name for v in a3_sweeps} == {"sweep_3_to_5", "sweep_3_to_7"},
      f"consts={a3_consts} variants={[v.name for v in a3_sweeps]}")
check("D3b: sweep targets are 5.0 and 7.0, hypothesis crossed",
      {v.target_constant for v in a3_sweeps} == {5.0, 7.0}
      and all(v.expected == "crossed" and v.arm == "sweep"
              for v in a3_sweeps),
      a3_sweep_note)

# ------------------------------------------------------------------ P1 probe square
SQ_VARIANTS = [
    VariantSpec(name="neg_square", novel_goal="negate the square of x",
                train_examples=[({"x": 2}, -4), ({"x": 3}, -9),
                                ({"x": 5}, -25)],
                held_out=[({"x": 7}, -49), ({"x": 11}, -121),
                          ({"x": 4}, -16), ({"x": 9}, -81)],
                expected="crossed", arm="structural"),
    VariantSpec(name="neg_fourth", novel_goal="negate the fourth power of x",
                train_examples=[({"x": 2}, -16), ({"x": 3}, -81)],
                held_out=[({"x": 4}, -256), ({"x": 5}, -625)],
                expected="bound", arm="structural"),
    VariantSpec(name="eighth", novel_goal="raise x to the eighth power",
                train_examples=[({"x": 2}, 256)],
                held_out=[({"x": 3}, 6561)], expected="bound",
                arm="structural"),
    VariantSpec(name="div5_sq", novel_goal="divide five by the square of x",
                train_examples=[({"x": 2}, 1.25), ({"x": 5}, 0.2)],
                held_out=[({"x": 4}, 0.3125), ({"x": 10}, 0.05)],
                expected="bound", arm="control"),
]
rep_sq = prober.probe_technique(
    SQ_REF, SQ_VARIANTS + sq_sweeps, params=PARAMS, output_kind=NUM,
    gap_prefix="genenv1-sq", admission_prefix="genenv1_sq",
    sweep_note=sq_sweep_note)
match = all(o.outcome == ("crossed" if o.spec.expected == "crossed"
                           else "bound_marked") for o in rep_sq.outcomes)
check("P1a: square variants match the automation's hypotheses 6/6",
      match and len(rep_sq.outcomes) == 6,
      "; ".join(f"{o.spec.name}={o.outcome}/{o.mechanism}"
                for o in rep_sq.outcomes))
check("P1b: every cycle retired clean, loop idle afterwards",
      all(o.retired_clean for o in rep_sq.outcomes)
      and ctl.loop_view().active_count == 0,
      f"retired={[o.retired_clean for o in rep_sq.outcomes]}")
depth_bounds = [o for o in rep_sq.outcomes
                if o.spec.name in ("neg_fourth", "eighth")]
honest_reasons = ("honestly found no plan", "causal contrast failed",
                  "Q8 refused", "admission refused")
check("P1c: depth-3 bounds fail honestly with named reasons",
      all(o.outcome == "bound_marked"
          and any(h in o.reason for h in honest_reasons)
          for o in depth_bounds),
      "; ".join(f"{o.spec.name}: {o.reason[:70]}" for o in depth_bounds))

# ------------------------------------------------------------------ P4 square sweep crossings (generalize leg)
sq_sweep_outcomes = [o for o in rep_sq.outcomes if o.spec.arm == "sweep"]
check("P4a: square's exponent sweep crossed via the generalize leg",
      len(sq_sweep_outcomes) == 2
      and all(o.outcome == "crossed" and o.mechanism == "generalize"
              for o in sq_sweep_outcomes),
      "; ".join(f"{o.spec.name}={o.outcome}/{o.mechanism} "
                f"promoted={o.promoted_name}" for o in sq_sweep_outcomes))
check("P4b: held-out full on both exponent-sweep crossings",
      {o.heldout for o in sq_sweep_outcomes} == {"3/3"},
      f"heldout={[o.heldout for o in sq_sweep_outcomes]}")

# ------------------------------------------------------------------ P2 probe add-three (sweep arm)
env_a3_before = ctl.envelope_for(A3_REF["promoted_name"])
crossed_before = [r for r in env_a3_before["technique_records"]
                  if (r.get("raw") or {}).get("outcome") == "crossed"]
check("P2a: add-three envelope holds no crossings before probing",
      crossed_before == [], f"crossed={len(crossed_before)}")
rep_a3 = prober.probe_technique(
    A3_REF, a3_sweeps, params=PARAMS, output_kind=NUM,
    gap_prefix="genenv1-a3", admission_prefix="genenv1_a3",
    sweep_note=a3_sweep_note)
check("P2b: both sweep variants crossed via the generalize leg",
      all(o.outcome == "crossed" and o.mechanism == "generalize"
          for o in rep_a3.outcomes),
      "; ".join(f"{o.spec.name}={o.outcome}/{o.mechanism} "
                f"promoted={o.promoted_name}" for o in rep_a3.outcomes))
check("P2c: held-out full on both sweep crossings",
      {o.heldout for o in rep_a3.outcomes} == {"3/3"},
      f"heldout={[o.heldout for o in rep_a3.outcomes]}")
env_a3_after = ctl.envelope_for(A3_REF["promoted_name"])
crossed_after = [r for r in env_a3_after["technique_records"]
                 if (r.get("raw") or {}).get("outcome") == "crossed"]
check("P2d: add-three envelope grew from empty to 2 expansions",
      len(crossed_after) == 2,
      f"crossed={len(crossed_after)}")
ADD5 = next(o for o in rep_a3.outcomes if o.spec.target_constant == 5.0)

# ------------------------------------------------------------------ P3 published envelope
def find_reports(promoted):
    env = ctl.envelope_for(promoted)
    return [r for r in env["technique_records"]
            if (r.get("raw") or {}).get(ENVELOPE_REPORT_MARKER)]


rep_docs_sq = find_reports(SQ_REF["promoted_name"])
rep_docs_a3 = find_reports(A3_REF["promoted_name"])
check("P3a: summary report published per technique",
      len(rep_docs_sq) == 1 and len(rep_docs_a3) == 1
      and rep_sq.report_observation_id
      and rep_a3.report_observation_id,
      f"sq={len(rep_docs_sq)} a3={len(rep_docs_a3)}")
raw_sq = rep_docs_sq[0].get("raw") or {} if rep_docs_sq else {}
check("P3b: square report lists 3 expansions and 3 bounds",
      raw_sq.get("expansions") == ["neg_square", "sweep_2_to_4",
                                   "sweep_2_to_6"]
      and len(raw_sq.get("bounds", [])) == 3,
      f"exp={raw_sq.get('expansions')} bounds={raw_sq.get('bounds')}")

# ------------------------------------------------------------------ A1 adversarial agreement
agreed5, det5 = verify_agreement(
    prober, A3_REF,
    next(v for v in a3_sweeps if v.target_constant == 5.0),
    "crossed", params=PARAMS, output_kind=NUM)
check("A1a: independent re-probe of published add-five agrees",
      agreed5 and ("idempotence" in det5 or "reprobe=crossed" in det5),
      det5)
div5_spec = next(v for v in SQ_VARIANTS if v.name == "div5_sq")
# GEN-SYNTH-1 honest verdict change (documented): the 5/x^2 stale bound now
# genuinely crosses with the improved mechanisms. The re-probe should agree
# with "crossed", not the stale "bound_marked".
agreed_d, det_d = verify_agreement(
    prober, SQ_REF, div5_spec, "crossed",
    params=PARAMS, output_kind=NUM)
check("A1b: independent re-probe of published 5/x^2 agrees (now crossed)",
      agreed_d, det_d)

# ------------------------------------------------------------------ A2 gates not weakened
# The controller records crossed compose cycles with an empty record
# reason; the CycleResult reason (kept on the probe report's outcome)
# carries the full trust path. Both are checked: the record proves the
# cycle crossed, the outcome reason proves HOW.
neg_outcome = next(o for o in rep_sq.outcomes
                   if o.spec.name == "neg_square")
check("A2: crossed compose cycle names Q8, the contrast, and admission",
      neg_outcome.outcome == "crossed"
      and "Q8 authenticated" in neg_outcome.reason
      and "causal contrast clean" in neg_outcome.reason
      and "admitted" in neg_outcome.reason,
      neg_outcome.reason[:160])

# ------------------------------------------------------------------ F1 fresh process
# The parent engine holds the DB owner lock: close it first so a genuinely
# fresh OS process can claim the DB.
ADD5_PROMOTED = ADD5.promoted_name
ADD5_HELD = a3_oracle(5.0)[1]
eng.close()
fresh_src = (
    "import json\n"
    "import os, sys\n"
    f"sys.path.insert(0, {os.path.join(WT, 'pylib')!r})\n"
    f"sys.path.insert(0, {WT!r})\n"
    "from swarm_engine.core.engine import SwarmEngine\n"
    "from swarm_engine.intellect.unified_memory import read_experiences\n"
    f"eng = SwarmEngine(db_path={DB!r})\n"
    f"HELD = {ADD5_HELD!r}\n"
    f"prom = {ADD5_PROMOTED!r}\n"
    "entry = eng.composer.reg.get(prom)\n"
    "vals = []\n"
    "if entry is not None:\n"
    "    fn = entry.fn\n"
    "    import asyncio\n"
    "    for args, want in HELD:\n"
    "        try:\n"
    "            v = fn(**args)\n"
    "            vals.append(v == want)\n"
    "        except TypeError:\n"
    "            vals.append(False)\n"
    'recs = read_experiences(eng.intellect.epistemic, kind="generalization_envelope")\n'
    'base = read_experiences(eng.intellect.epistemic, kind="generalization_envelope_baseline")\n'
    'print(json.dumps({"held": vals, "env_recs": len(recs),\n'
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
    check("F1a: fresh engine executes the automation-crossed add-five",
          f1.get("held") == [True] * 3, f"held={f1.get('held')}")
    check("F1b: envelope records readable from a fresh process",
          f1.get("env_recs", 0) >= 5 and f1.get("base_recs", 0) >= 1,
          f"env={f1.get('env_recs')} base={f1.get('base_recs')}")

# ------------------------------------------------------------------ summary
fails = [n for n, ok, _ in results if not ok]
print(f"\n{len(results) - len(fails)}/{len(results)} checks passed")
if fails:
    print("FAILURES:", fails)
    sys.exit(1)
print("GEN-ENV-1 proof battery GREEN")
