"""
GEN-XDOM-1 proof battery: cross-domain transfer (scalar NUM->NUM acquired
technique into LIST(NUM)->LIST(NUM)) through the real GeneralizationController.

Claims under test:
  D0  add-seven distills through the real DistillationLoop (fresh DB):
      success, admitted, held-out 2/2.
  D1  the controller constructs against the frozen RUN-MICRO-1 v1 interface;
      baseline seeded from the published envelope; both source registrations
      resolvable for the contrast.
  D2  repair evidence (GEN-XDOM-1 incident): the causal-contrast forbidden set
      is honored by _candidate_prims AND the lambda bank -- no forbidden
      (acquired) primitive can appear as a head, in a banked unary fragment,
      or inside a generated lambda.
  C1  causal control (direct, through PlanComposer, PRE-admission): composing
      the transfer objective with BOTH technique registrations forbidden finds
      nothing and the search exhausts -- without the source technique the
      target task is unreachable in this plan space.
  P1  run_cycle on the transfer target "add 7 to each element of the list":
      outcome crossed via compose; the composed plan incorporates the
      distilled technique (adversarial: no transfer claim for a plan that
      never used the source technique); held-out 4/4; the reason names Q8
      authentication and a clean causal contrast; the envelope expansion is
      recorded; the microcontroller stack retired cleanly; the executive saw
      only "Generalization is active."
  P1x mechanism evidence: the admission registers the transfer capability as
      a searchable acquired primitive; post-admission, the contrast objective
      (source regs forbidden) IS solvable -- through the admitted transfer
      capability. This is the causal explanation of A1.
  B1  honest-bound control: pairwise adjacent sums in the collection domain
      (needs binary combination -- outside the composer's plan space) marks
      the bound with the obstruction named, never admitted. The admitted
      transfer primitive does not cause a spurious crossing here.
  A1  compose-leg idempotence: re-probing the transfer after admission does
      NOT claim a second crossing -- outcome bound_marked, "causal contrast
      failed" (the task is now solvable through the admitted transfer
      capability, so the source technique is no longer necessary), and no
      duplicate capability is admitted. The envelope keeps both the crossing
      record and this refusal; the refusal is lossily encoded as
      bound_marked (no explicit already-crossed verdict exists -- named
      residual, not a defect in the transfer claim).
  F1  FRESH OS PROCESS: a new SwarmEngine on the same DB executes the
      admitted transfer capability 4/4 held-out; envelope records readable.

Training examples deliberately avoid the literals 1 and 7 (the composer's
bank harvests training literals): with 7 prime and 1 absent, no one- or
two-layer composition of base primitives can express +7, so the clean
causal contrast is adversarial, not vacuous.

Exits 0 only if every check passes.
"""
import json
import os
import subprocess
import sys

# ---- worktree under test (NEVER the shared canonical tree) ----
WT = os.path.expanduser("/home/hatch/workspace/worktrees/warm-generalization")
assert os.path.isdir(os.path.join(WT, "runtime", "generalization")), \
    f"worktree generalization package missing at {WT}"
sys.path.insert(0, os.path.join(WT, "pylib"))
sys.path.insert(0, WT)

WORK = os.path.expanduser("/home/hatch/workspace/worktrees/warm-generalization/proofs/gensynth6/regress_upstream/gate_gs5_2026-09-29/genxdom1_work")
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

results = []


def check(name, cond, detail=""):
    results.append((name, bool(cond), detail))
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" +
          (f" -- {detail}" if detail else ""), flush=True)


def acquired_names():
    return set(n for n in eng.composer.reg._prims if n.startswith("acquired."))


# ------------------------------------------------------------------ D0 distill
eng = SwarmEngine(db_path=DB)
DISTILL_EV = [{"input": {"x": x}, "output": x + 7}
              for x in (2, 4, 6, 8, 10, 12, 14, 16)]
delta = DeltaRecord(
    objective="add seven to the input",
    external_actions="add the constant 7 to the input",
    prior_capability="no such capability admitted in the engine",
    capability_gap="REMOR could not perform this",
    technique="add the constant 7 to the input",
    evidence=DISTILL_EV, source="gen-xdom-1")
d = DistillationLoop(eng).distill(delta)
check("D0a: add-seven distills through the real loop",
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

# ------------------------------------------------- D2 repair evidence (white-box)
# The forbidden set must be honored by the candidate pool itself (the bank,
# the forward loop, and the head loop all draw from it).
TRAIN = [({"xs": [2, 4]}, [9, 11]), ({"xs": [6, 8]}, [13, 15])]
HELD = [({"xs": [10, 12]}, [17, 19]), ({"xs": [20, 22]}, [27, 29]),
        ({"xs": [30, 32]}, [37, 39]), ({"xs": [40, 50]}, [47, 57])]
GOAL = "add 7 to each element of the list"
pc = PlanComposer(eng.composer)
obj_forbid = CompositionObjective(
    goal=GOAL, gap_id="gen-xdom-1:d2", params={"xs": LIST(NUM)},
    output_kind=LIST(NUM), examples=TRAIN, held_out=HELD,
    forbidden=tuple(regs))
pool = pc._candidate_prims(obj_forbid)
pool_leak = [p.name for p in pool if p.name in set(regs)]
# Replicate the literal flow compose() uses internally (compose sets
# self._literals at its head; the bank draws from it).
lits = pc._literals_from_examples(obj_forbid)
bank = _LambdaBank(pc._composer, obj_forbid, pool, lits)
bank_leak = [e.used for e in bank.entries
             if any(u in set(regs) for u in e.used)]
check("D2a: forbidden registrations absent from the candidate pool",
      not pool_leak, f"leaked={pool_leak}")
check("D2b: forbidden primitives absent from every banked lambda",
      not bank_leak, f"bank_entries={len(bank.entries)} leaked={bank_leak}")
check("D2c: the bank is non-empty (the repair did not gut it)",
      len(bank.entries) > 0, f"entries={len(bank.entries)}")

# ------------------------------------------- C1 causal control (PRE-admission)
# Must run before P1's admission registers the transfer capability as a
# searchable primitive -- afterwards this objective IS solvable (see P1x).
obj_contrast = CompositionObjective(
    goal=GOAL, gap_id="gen-xdom-1:c1", params={"xs": LIST(NUM)},
    output_kind=LIST(NUM), examples=TRAIN, held_out=HELD,
    forbidden=tuple(regs))
cres = pc.compose(obj_contrast)
check("C1: without the source technique the search exhausts honestly",
      not cres.found and cres.search_exhausted,
      f"found={cres.found} exhausted={cres.search_exhausted} "
      f"evaluated={cres.candidates_evaluated}")

# ------------------------------------------------------------------ P1 transfer
cyc = ctl.run_cycle(
    technique_ref=ref, novel_goal=GOAL,
    train_examples=TRAIN, held_out=HELD,
    params={"xs": LIST(NUM)}, output_kind=LIST(NUM),
    gap_id="gen-xdom-1:xfer-add-seven",
    admission_name="gen_xdom_1_xfer_add_seven")
check("P1a: transfer crosses via the compose leg",
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
check("P1d: envelope expansion recorded for the transfer",
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
    goal=GOAL, gap_id="gen-xdom-1:p1x", params={"xs": LIST(NUM)},
    output_kind=LIST(NUM), examples=TRAIN, held_out=HELD,
    forbidden=tuple(regs))
rpx = pc2.compose(obj_px)
via_transfer = [c for c in (rpx.composed_of or [])
                if c in set(new_regs)] if rpx.found else []
check("P1x-b: post-admission the contrast objective is solvable via the "
      "admitted transfer capability",
      rpx.found and bool(via_transfer),
      f"found={rpx.found} via_transfer={via_transfer}")

# ------------------------------------------------------- B1 honest-bound control
B1_TRAIN = [({"xs": [1, 2, 3]}, [3, 5]), ({"xs": [4, 5, 6]}, [9, 11])]
B1_HELD = [({"xs": [7, 8, 9]}, [15, 17]), ({"xs": [10, 20, 30]}, [30, 50])]
b1 = ctl.run_cycle(
    technique_ref=ref, novel_goal="replace each adjacent pair by its sum",
    train_examples=B1_TRAIN, held_out=B1_HELD,
    params={"xs": LIST(NUM)}, output_kind=LIST(NUM),
    gap_id="gen-xdom-1:bound-binary-combine",
    admission_name="gen_xdom_1_bound_binary_combine")
check("B1a: binary combination in the collection domain marks a bound",
      b1.outcome == "bound_marked" and b1.mechanism == "compose",
      f"outcome={b1.outcome} mech={b1.mechanism}")
check("B1b: the bound names the honest obstruction",
      "composer honestly found no plan" in b1.reason,
      b1.reason[:160])
check("B1c: nothing admitted on the bound path",
      not b1.capability_id, f"cap={b1.capability_id}")

# ------------------------------------------------------- A1 re-probe idempotence
# The transfer capability is now an admitted primitive: the source technique
# is no longer necessary, so the re-probe must NOT claim a second crossing.
cyc2 = ctl.run_cycle(
    technique_ref=ref, novel_goal=GOAL,
    train_examples=TRAIN, held_out=HELD,
    params={"xs": LIST(NUM)}, output_kind=LIST(NUM),
    gap_id="gen-xdom-1:xfer-add-seven:reprobe",
    admission_name="gen_xdom_1_xfer_add_seven_reprobe")
check("A1a: re-probe does not claim a second crossing",
      cyc2.outcome == "bound_marked" and cyc2.mechanism == "compose",
      f"outcome={cyc2.outcome} mech={cyc2.mechanism}")
check("A1b: the refusal names the causal contrast",
      "causal contrast failed" in cyc2.reason, cyc2.reason[:160])
check("A1c: no duplicate capability admitted on the re-probe",
      not cyc2.capability_id, f"cap={cyc2.capability_id}")

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
    '    vals.append(r.get("success") and r.get("value") == want)\n'
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
    check("F1a: fresh OS process executes the admitted transfer 4/4 held-out",
          f1.get("held4") == [True] * 4, f"held4={f1.get('held4')}")
    check("F1b: envelope records readable from a fresh process",
          f1.get("env_recs", 0) >= 2, f"env={f1.get('env_recs')}")

# ------------------------------------------------------------------ summary
fails = [n for n, ok, _ in results if not ok]
print(f"\n{len(results) - len(fails)}/{len(results)} checks passed")
if fails:
    print("FAILURES:", fails)
    sys.exit(1)
print("GEN-XDOM-1 proof battery GREEN")
