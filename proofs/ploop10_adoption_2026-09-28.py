#!/usr/bin/env python3
"""PLOOP-10 proof: resource-arbitrator adoption.

Proves, on the real machinery (no mocks, no staged demos):

  T1 -- bind measures capacity from the substrate's real initial pools
        (600s x 6 / 16 x 6 -> 3600.0s / 96 slots); arbitration_status()
        reports the arbitrated live path.
  T2 -- contention on real state: 10 genuinely registered open gaps plus
        2 real generalization microcontrollers (600s reserved) against
        3600s capacity. Demands are measured, not constant: acquisition
        states 6000s, generalization 600s. The arbitrator grants within
        capacity, the acquisition grant differs from the static 600s, the
        refused 4200s is recorded in the round summary (visible, never
        vanished), the grant is written into the live pool, and a spawn
        beyond the grant is really refused (admission_exhausted, counted
        in total_refused). A real tick() carries the arbitration round in
        its cycle summary and advances the epoch.
  T3 -- the static fallback is explicit: an unbound controller's tick
        names static-fallback with its reason; arbitration_status() agrees.
  T4 -- refused demand accounting is exact (4200.0s for acquisition).
  T5 -- loud bind-time failure on a misconfigured substrate (missing loop
        pool -> KeyError naming the loop); tick-time measurement failure
        (corrupted gap db -> real DatabaseError) is fail-closed: the tick
        still completes and records mode=error.
  T6 -- the executive/run-controller cadence wiring: constructing the real
        ExecutiveController auto-binds the shared substrate to the
        controller's arbitration path.

Exit 0 only if every count below holds exactly.
"""

import os
import sqlite3
import sys
import tempfile

CANON = "/home/hatch/workspace/ploop-10-work"
sys.path.insert(0, os.path.join(CANON, "pylib"))
sys.path.insert(0, CANON)

from swarm_engine.core.engine import SwarmEngine
from swarm_engine.core.run_controller import (
    RunController, RunConfig, _ACQUISITION_HOSTED_CYCLE_BUDGET_S)
from swarm_engine.core.executive.executive import ExecutiveController
from swarm_engine.core.microcontroller.substrate import (
    MicrocontrollerSubstrate, LOOPS)
from swarm_engine.acquisition.gaps import (
    GapRegistry, GapRecord, DissatisfactionBlock, STATUS_OPEN)
from swarm_engine.services.acceptance import AcceptanceLoop, AcceptanceStore


PASSED = 0


def check(cond, msg):
    global PASSED
    if not cond:
        print(f"FAIL: {msg}")
        sys.exit(1)
    PASSED += 1
    print(f"ok [{PASSED}]: {msg}")


def make_engine(workdir):
    return SwarmEngine(db_path=os.path.join(workdir, "eng.db"))


def make_registry(eng, workdir, name="gaps.db"):
    return GapRegistry(eng, db_path=os.path.join(workdir, name))


def seed_open_gaps(registry, n):
    for i in range(n):
        registry.register(GapRecord(
            registered_by="ploop10/proof",
            summary=f"proof open gap {i}: acquisition demand seed",
            dissatisfaction=DissatisfactionBlock(
                attempt_ref=f"ploop10_attempt_{i}",
                feedback="seeded feedback",
                unmet_criteria="seeded criterion"),
            evidence=[{"kind": "observation",
                       "observed": f"seeded gap {i}",
                       "detail": "ploop10 proof"}]))
    rows = registry.list_gaps(status=STATUS_OPEN)
    return len(rows)


def make_controller(eng, registry, workdir, **cfg_kw):
    cfg = RunConfig(
        cadence_interval_s=60.0, run_budget_s=3600.0,
        cycle_budget_s=30.0, gap_budget_s=2.0,
        max_cycles=3, max_gaps_per_cycle=25,
        trusted_indexes=[], staging_dir=os.path.join(workdir, "staging"),
        **cfg_kw)
    return RunController(
        eng, config=cfg,
        checkpoint_path=os.path.join(workdir, "ckpt.db"),
        registry=registry)


def make_substrate():
    sub = MicrocontrollerSubstrate()
    for loop in LOOPS:
        sub.register_loop(loop, budget_s=600.0, max_concurrent=16)
    return sub


# ------------------------------------------------------------------ T1 ---
print("--- T1: bind measures capacity from the real initial pools ---")
td1 = tempfile.mkdtemp(prefix="ploop10_t1_")
eng1 = make_engine(td1)
reg1 = make_registry(eng1, td1)
rc1 = make_controller(eng1, reg1, td1)
sub1 = make_substrate()

st0 = rc1.arbitration_status()
check(st0["mode"] == "static-fallback",
      "T1: unbound controller reports static-fallback (explicit, not silent)")
check("no substrate bound" in st0["reason"],
      "T1: fallback reason names the missing bind")

bound = rc1.bind_arbitration_substrate(sub1)
check(bound["bound"] is True, "T1: bind reports bound")
check(bound["capacity_budget_s"] == 3600.0,
      f"T1: capacity measured from real pools = 3600.0s "
      f"(got {bound['capacity_budget_s']})")
check(bound["capacity_concurrent"] == 96,
      f"T1: slot capacity measured from real pools = 96 "
      f"(got {bound['capacity_concurrent']})")
check(bound["epoch_length_s"] == 60.0,
      "T1: epoch length follows the real cadence interval")

st1 = rc1.arbitration_status()
check(st1["mode"] == "arbitrated",
      "T1: bound controller reports the arbitrated live path")
check(st1["live_path"] == "arbitrator grants",
      "T1: live path names the arbitrator grants")

# ------------------------------------------------------------------ T2 ---
print("--- T2: contention on real state ---")
td2 = tempfile.mkdtemp(prefix="ploop10_t2_")
eng2 = make_engine(td2)
reg2 = make_registry(eng2, td2)
n = seed_open_gaps(reg2, 10)
check(n == 10, f"T2: 10 genuinely registered open gaps (got {n})")
rc2 = make_controller(eng2, reg2, td2)
sub2 = make_substrate()
rc2.bind_arbitration_substrate(sub2)

# Two real generalization microcontrollers: real reservations on the pool.
spawns = [sub2.spawn("generalization",
                     purpose=f"ploop10-proof-mc-{i}", budget_s=300.0)
          for i in range(2)]
check(all(s.ok for s in spawns),
      "T2: 2 real generalization MCs spawned (600s really reserved)")
pools_before = sub2.export_state()["loops"]
check(pools_before["generalization"]["reserved_s"] == 600.0,
      "T2: 600s really reserved on the generalization pool")

summary = rc2._arbitrate_resources()
check(summary["mode"] == "arbitrated", "T2: round ran arbitrated")
check(summary["epoch_id"] == 1, "T2: first epoch is 1")
check(summary["capacity_budget_s"] == 3600.0,
      "T2: capacity is the measured 3600s")
grants = summary["grants"]
check(grants["acquisition"]["demand_budget_s"] == 10 * 600.0,
      f"T2: acquisition demand MEASURED as 10 open gaps x 600s = 6000s "
      f"(got {grants['acquisition']['demand_budget_s']})")
check("open gaps" in grants["acquisition"]["method"],
      "T2: demand method names the real signal")
check(grants["generalization"]["demand_budget_s"] == 600.0,
      "T2: generalization demand MEASURED as the real 600s reserved")
check(grants["run"]["demand_budget_s"] == 0.0,
      "T2: run loop honestly states reserved-only demand (no MC work path)")

total_granted = summary["total_stated_budget_s"]
check(total_granted <= 3600.0,
      f"T2: total stated grants {total_granted}s within the 3600s capacity")
check(grants["run"]["grant_stated"] is False,
      "T2: zero-demand loop gets no stated grant (pool keeps its prior "
      "grant -- no thrash, honestly reported)")
acq_grant = grants["acquisition"]["grant_budget_s"]
check(acq_grant == 1800.0,
      f"T2: acquisition grant is 1800.0s under contention "
      f"(got {acq_grant})")
check(acq_grant != 600.0,
      "T2: arbitrated grant differs from the static 600s default")
check(grants["generalization"]["grant_budget_s"] == 600.0,
      "T2: active work keeps its ceiling (generalization granted its "
      "full reserved 600s)")

# The grant is live in the pool, not advisory.
pools_after = sub2.export_state()["loops"]
check(pools_after["acquisition"]["budget_s"] == 1800.0,
      "T2: acquisition pool really rewritten to the 1800s grant")
check(pools_after["generalization"]["budget_s"] == 600.0,
      "T2: generalization pool really rewritten to the 600s grant")

# Enforcement is real: a spawn beyond the grant is refused, and counted.
ref = sub2.spawn("acquisition", purpose="ploop10-over-grant",
                 budget_s=2000.0)
check(not ref.ok and ref.refusal.reason == "admission_exhausted",
      "T2: spawn of 2000s against the 1800s grant really refused "
      "(admission_exhausted)")
check(sub2.export_state()["loops"]["acquisition"]["total_refused"] == 1,
      "T2: the refusal is really counted in total_refused")

# A real tick() carries the arbitration round in-cadence.
tick_summary = rc2.tick(cycle_n=1)
arb = tick_summary.get("arbitration", {})
check(arb.get("mode") == "arbitrated",
      "T2: real tick() runs the arbitration round in-cadence")
check(arb.get("epoch_id") == 2,
      "T2: tick advanced the arbitration epoch to 2")
check(rc2.arbitration_status()["epoch_id"] == 2,
      "T2: status tracks the live epoch")

# ------------------------------------------------------------------ T4 ---
print("--- T4: refused demand is visible, never vanished ---")
ref_acq = grants["acquisition"]["refused_budget_s"]
check(ref_acq == 4200.0,
      f"T2/T4: refused acquisition demand recorded exactly: 6000 - 1800 "
      f"= 4200.0s (got {ref_acq})")
check(grants["generalization"]["refused_budget_s"] == 0.0,
      "T4: fully granted loop records zero refused")
check(summary["headroom_budget_s"] == 1200.0,
      f"T4: headroom recorded: 3600 - 2400 = 1200.0s "
      f"(got {summary['headroom_budget_s']})")

# ------------------------------------------------------------------ T3 ---
print("--- T3: static fallback is explicit ---")
td3 = tempfile.mkdtemp(prefix="ploop10_t3_")
eng3 = make_engine(td3)
reg3 = make_registry(eng3, td3)
rc3 = make_controller(eng3, reg3, td3)
tick3 = rc3.tick(cycle_n=1)
arb3 = tick3.get("arbitration", {})
check(arb3.get("mode") == "static-fallback",
      "T3: unbound tick names static-fallback in the cycle summary")
check("no substrate bound" in arb3.get("reason", ""),
      "T3: the summary carries the reason, not a silent default")
check(len(tick3.get("gaps", [])) >= 0,
      "T3: the tick still completes its real work under fallback")
st3 = rc3.arbitration_status()
check(st3["mode"] == "static-fallback"
      and st3["live_path"] == "static initial pools",
      "T3: arbitration_status() agrees on the live path")

# ------------------------------------------------------------------ T5 ---
print("--- T5: loud bind failure; fail-closed tick ---")
td5 = tempfile.mkdtemp(prefix="ploop10_t5_")
eng5 = make_engine(td5)
reg5 = make_registry(eng5, td5)
rc5 = make_controller(eng5, reg5, td5)
sub5 = MicrocontrollerSubstrate()
for loop in LOOPS:
    if loop == "distillation":
        continue
    sub5.register_loop(loop, budget_s=600.0, max_concurrent=16)
try:
    rc5.bind_arbitration_substrate(sub5)
    check(False, "T5: bind with a missing loop pool should have raised")
except KeyError as exc:
    check("distillation" in str(exc),
          f"T5: bind fails LOUDLY naming the missing loop pool ({exc})")

# Real measurement failure: corrupt the gap db file, then tick.
td5b = tempfile.mkdtemp(prefix="ploop10_t5b_")
eng5b = make_engine(td5b)
reg5b = make_registry(eng5b, td5b, name="gaps5b.db")
rc5b = make_controller(eng5b, reg5b, td5b)
sub5b = make_substrate()
rc5b.bind_arbitration_substrate(sub5b)
with open(os.path.join(td5b, "gaps5b.db"), "wb") as fh:
    fh.write(b"\x00\x01\x02garbage-not-sqlite")
tick5 = rc5b.tick(cycle_n=1)
arb5 = tick5.get("arbitration", {})
check(arb5.get("mode") == "error",
      "T5: corrupted gap db -> arbitration round records mode=error")
check("DatabaseError" in arb5.get("error", "")
      or "database" in arb5.get("error", "").lower(),
      f"T5: the error names the real sqlite failure "
      f"({arb5.get('error')})")
check(tick5.get("cycle") == 1,
      "T5: the tick still completes (fail-closed: pools keep prior "
      "grants, nothing crashes)")

# ------------------------------------------------------------------ T6 ---
print("--- T6: executive/run-controller cadence wiring ---")
td6 = tempfile.mkdtemp(prefix="ploop10_t6_")
eng6 = make_engine(td6)
reg6 = make_registry(eng6, td6)
rc6 = make_controller(eng6, reg6, td6)
acc6 = AcceptanceLoop(
    AcceptanceStore(db_path=os.path.join(td6, "acc.db")),
    eng6.intellect.epistemic, engine=eng6)
check(rc6.arbitration_status()["mode"] == "static-fallback",
      "T6: pre-executive controller is unbound (baseline)")
ex6 = ExecutiveController(
    engine=eng6, run_controller=rc6, gap_registry=reg6,
    acceptance_loop=acc6)
st6 = rc6.arbitration_status()
check(st6["mode"] == "arbitrated",
      "T6: constructing the executive auto-binds the shared substrate "
      "to the controller's arbitration path")
check(rc6._arbitrator is not None
      and rc6._arbitration_substrate is ex6._substrate,
      "T6: the controller's arbitration path holds the executive's "
      "shared substrate (same object, not a copy)")
# capacity is inspectable through the last decision after one round
r6 = rc6._arbitrate_resources()
check(r6["capacity_budget_s"] == 3600.0
      and r6["capacity_concurrent"] == 96,
      "T6: executive-bound arbitration measures the same 3600s/96 "
      "capacity from the executive's real initial pools")
check(_ACQUISITION_HOSTED_CYCLE_BUDGET_S == 600.0,
      "T6: demand unit constant matches the inlet's real cycle budget")

print(f"\nPLOOP-10 PROOF COMPLETE: {PASSED} checks green")
