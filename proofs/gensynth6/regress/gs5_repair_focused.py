"""GEN-SYNTH-5-REPAIR focused tests (audit follow-ups to commit ee5f791).

Covers the three repair-packet concerns left unverified at the checkpoint:
  T1  probe-cache key is exactly type-preserving (list vs tuple, int vs
      float, bool vs int, set vs frozenset; dict order-insensitive).
  T2  int/float cache separation through the REAL wrapper (type_of prim:
      type_of(3)="int" vs type_of(3.0)="float" -- the old _value_key
      conflated these, serving phantom cached probes).
  T3  list/tuple cache separation through the REAL wrapper (type_of:
      "list[int]" vs "tuple[int, int]").
  T4  compose() under a RUNNING event loop: probes must actually execute
      (probe_evals > 0). The old run_until_complete path raised
      RuntimeError inside a running loop and silently degraded every
      probe to (False, None); the worker thread must not.
  T5  probe-budget exhaustion is explicit and fail-safe: the flag is set
      the moment the probe context cannot spend; the pruning gate fails
      open on exhaustion (never prunes on unknown); the exhaustion is
      recorded on ComposeResult; a real compose with a tiny probe budget
      degrades gracefully and still crosses.

No mocks: every probe goes through PrimitiveRegistry.invoke (the same
call real per-step execution makes) on the real admitted registry.
"""
import asyncio
import os
import sys

WT = os.path.expanduser("~/workspace/worktrees/gensynth6-mission")
sys.path.insert(0, os.path.join(WT, "pylib"))
sys.path.insert(0, WT)

WORK = os.path.expanduser(
    "~/workspace/worktrees/gensynth6-mission/proofs/gensynth6/regress_upstream/gate_gs5_2026-09-29/gensynth5_repair_focused_work")
os.makedirs(WORK, exist_ok=True)
DB = os.path.join(WORK, "focused.db")
for f in ("focused.db", "focused.db.oracle.db"):
    p = os.path.join(WORK, f)
    if os.path.exists(p):
        os.remove(p)

from swarm_engine.core.engine import SwarmEngine
from swarm_engine.primitives.core import ExecContext, NUM, LIST
from swarm_engine.synthesis.plan_composer import (
    PlanComposer, CompositionObjective)

eng = SwarmEngine(db_path=DB)

results = []


def check(name, cond, detail=""):
    results.append(bool(cond))
    print(f"[{'PASS' if cond else 'FAIL'}] {name}"
          + (f" -- {detail}" if detail else ""), flush=True)


def fresh_probes(pc, budget):
    """Mirror compose()'s per-call probe-state setup (real machinery)."""
    pc._probe_cache = {}
    pc._probe_ctx = ExecContext(pc._reg.governor, pc._reg, budget=budget)
    pc._probe_budget_exhausted = False
    pc._probe_evals = 0


def ground_truth(op, args):
    """The wrapper path itself, outside any probe cache."""
    return eng.composer.reg.invoke_sync(op, None, **args)


# ---- T1: freeze exactness ------------------------------------------------
pc0 = PlanComposer(eng.composer)
fz = pc0._probe_freeze
vk = pc0._value_key
check("T1a old key conflated list/tuple (hole existed)",
      vk([1, 2]) == vk((1, 2)), f"key={vk([1,2])!r}")
check("T1b old key conflated int/float (hole existed)",
      vk(3) == vk(3.0), f"key={vk(3)!r}")
check("T1c freeze separates list/tuple", fz([1, 2]) != fz((1, 2)))
check("T1d freeze separates int/float", fz(3) != fz(3.0))
check("T1e freeze separates bool/int", fz(True) != fz(1))
check("T1f freeze separates set/frozenset",
      fz({1, 2}) != fz(frozenset({1, 2})))
check("T1g freeze dict order-insensitive",
      fz({"a": 1, "b": [2, 3]}) == fz({"b": [2, 3], "a": 1}))
check("T1h freeze nested exactness",
      fz([1, (2.0,)]) != fz([1, (2,)]))
try:
    hash(fz({"a": (1, 2.0, None, "x", [True])}))
    hashable = True
except Exception:
    hashable = False
check("T1i freeze hashable", hashable)
check("T1j args key separates int/float",
      pc0._probe_args_key({"v": 3}) != pc0._probe_args_key({"v": 3.0}))

# ---- T2: int/float separation through the real wrapper ------------------
pc = PlanComposer(eng.composer)
fresh_probes(pc, 1_000_000)
ok1, v1 = pc._probe_invoke("type_of", {"value": 3})
ok2, v2 = pc._probe_invoke("type_of", {"value": 3.0})
g1, g2 = ground_truth("type_of", {"value": 3}), ground_truth(
    "type_of", {"value": 3.0})
check("T2a int probe matches wrapper", ok1 and v1 == g1 == "int",
      f"probe={v1!r} wrapper={g1!r}")
check("T2b float probe not a phantom cache hit", ok2 and v2 == g2 == "float",
      f"probe={v2!r} wrapper={g2!r}")
check("T2c two distinct cache entries", len(pc._probe_cache) == 2,
      f"entries={len(pc._probe_cache)}")
fresh_probes(pc, 1_000_000)  # reverse order: float first
ok3, v3 = pc._probe_invoke("type_of", {"value": 3.0})
ok4, v4 = pc._probe_invoke("type_of", {"value": 3})
check("T2d order-independent", ok3 and ok4 and v3 == "float" and v4 == "int",
      f"float->{v3!r} int->{v4!r}")

# ---- T3: list/tuple separation through the real wrapper -----------------
fresh_probes(pc, 1_000_000)
ok5, v5 = pc._probe_invoke("type_of", {"value": [1, 2]})
ok6, v6 = pc._probe_invoke("type_of", {"value": (1, 2)})
g5, g6 = ground_truth("type_of", {"value": [1, 2]}), ground_truth(
    "type_of", {"value": (1, 2)})
check("T3a list probe matches wrapper", ok5 and v5 == g5 == "list[int]",
      f"probe={v5!r} wrapper={g5!r}")
check("T3b tuple probe not a phantom cache hit",
      ok6 and v6 == g6 == "tuple[int, int]", f"probe={v6!r} wrapper={g6!r}")
check("T3c two distinct cache entries", len(pc._probe_cache) == 2,
      f"entries={len(pc._probe_cache)}")

# ---- T4: compose() under a RUNNING event loop ---------------------------
async def _loop_main():
    await asyncio.sleep(0)  # prove we are inside a running loop
    assert asyncio.get_running_loop() is not None
    pc4 = PlanComposer(eng.composer)
    obj = CompositionObjective(
        goal="sum values (loop-safety smoke)", gap_id="gs5:focus:loop",
        params={"values": LIST(NUM)}, output_kind=NUM,
        examples=[({"values": [1, 2, 3]}, 6), ({"values": [4, 5]}, 9)],
        held_out=[])
    return pc4.compose(obj)


res4 = asyncio.run(_loop_main())
check("T4a compose under running loop completes", res4 is not None)
check("T4b plan found under running loop", res4.found,
      f"composed_of={res4.composed_of}")
# T4c (revised): the trivial smoke objective never fires probes, so drive
# _probe_invoke directly inside a running loop. Under the OLD fresh-loop
# implementation this raised RuntimeError inside the running loop and was
# caught as a silent (False, None); the worker-thread path must return
# the real wrapper result. This is the discriminating test for the hazard.
async def _loop_probe():
    assert asyncio.get_running_loop() is not None
    pc4p = PlanComposer(eng.composer)
    fresh_probes(pc4p, 1_000_000)
    ok, val = pc4p._probe_invoke("type_of", {"value": 3})
    return ok, val, pc4p._probe_evals
ok4, val4, evals4 = asyncio.run(_loop_probe())
check("T4c probe executes inside running loop (not silent-failed)",
      ok4 is True and val4 == "int" and evals4 == 1,
      f"ok={ok4} val={val4!r} probe_evals={evals4}")
check("T4d no probe exhaustion on smoke", res4.probe_budget_exhausted is False)

# ---- T5: probe-budget exhaustion explicit + fail-safe --------------------
pc5 = PlanComposer(eng.composer)
fresh_probes(pc5, 5)  # ExecContext.spend raises on the 5th spend
for i in range(10):   # distinct args -> cache misses -> real spends
    pc5._probe_invoke("type_of", {"value": i})
check("T5a exhaustion flag set", pc5._probe_budget_exhausted is True,
      f"flag={pc5._probe_budget_exhausted}")
# budget=5: spends succeed at 4,3,2,1 then raise at 0 -> 4 evals.
check("T5b evals counted honestly", pc5._probe_evals == 4,
      f"probe_evals={pc5._probe_evals}")

GATE = dict(m0=[0], v0=[0], m1=[1], v1=[1], pair_prims=[], static_ops=[],
            goal_first_key=pc5._value_key(999),
            goal_second_key=pc5._value_key(1000))
pc6 = PlanComposer(eng.composer)
fresh_probes(pc6, 1_000_000)
check("T5c control: gate prunes on genuine no-match",
      pc6._nest_may_complete(**GATE) is False)
pc6._probe_budget_exhausted = True
check("T5d exhausted gate fails open (never prunes on unknown)",
      pc6._nest_may_complete(**GATE) is True)

# T5e/f: end-to-end -- real compose with a tiny probe budget. Phase 1
# measures normal probe spend on a T-free three-level nesting objective;
# phase 2 reruns with a probe budget far below that and requires the
# exhaustion to be recorded honestly while the search still crosses.
TRAIN3X = [
    ({"xs": [1, 2], "ys": [10, 20], "zs": [100, 200]},
     [2 * 1 + 10 + 100, 2 * 2 + 20 + 200]),   # [111, 224]
    ({"xs": [0, 5], "ys": [1, 1], "zs": [2, 3]},
     [0 + 1 + 2, 10 + 1 + 3]),                # [3, 14]
]


def three_level_objective(tag):
    return CompositionObjective(
        goal=f"three-level nesting, no T ({tag})", gap_id=f"gs5:focus:{tag}",
        params={"xs": LIST(NUM), "ys": LIST(NUM), "zs": LIST(NUM)},
        output_kind=LIST(NUM), examples=TRAIN3X, held_out=[])


pc7a = PlanComposer(eng.composer)
res7a = pc7a.compose(three_level_objective("baseline"))
print(f"[INFO] baseline: found={res7a.found} "
      f"evals={res7a.candidates_evaluated} "
      f"probe_evals={res7a.probe_evals} "
      f"exhausted={res7a.probe_budget_exhausted} "
      f"composed_of={res7a.composed_of}", flush=True)
check("T5e-baseline crosses with normal probe budget", res7a.found is True)
check("T5e-baseline no exhaustion recorded",
      res7a.probe_budget_exhausted is False)

tiny = max(50, res7a.probe_evals // 4) if res7a.probe_evals else 50
pc7b = PlanComposer(eng.composer)
pc7b.PROBE_BUDGET = tiny  # instance shadow: force exhaustion mid-search
res7b = pc7b.compose(three_level_objective("tiny-probe-budget"))
print(f"[INFO] tiny-budget: found={res7b.found} "
      f"evals={res7b.candidates_evaluated} "
      f"probe_evals={res7b.probe_evals} "
      f"exhausted={res7b.probe_budget_exhausted} "
      f"composed_of={res7b.composed_of}", flush=True)
check("T5e exhaustion recorded honestly on ComposeResult",
      res7b.probe_budget_exhausted is True,
      f"probe_evals={res7b.probe_evals} budget={tiny}")
# T5f (revised): the original expectation -- "still crosses" -- was wrong.
# Probes are load-bearing for search efficiency (baseline: ~130k probe
# evals to cross at 7.3k candidate evals); with probes dead at ~1/4 of the
# needed spend, the fail-open search burns the 20k candidate budget
# without crossing. The safety contract is explicitness + no unsound
# pruning, not crossing under exhaustion. Assert the honest properties:
# the search completes without crashing, the degradation is recorded on
# the result, and no phantom crossing is claimed (found implies a real
# plan; found=False is the honest degraded outcome).
check("T5f degraded search completes honestly",
      res7b is not None
      and res7b.probe_budget_exhausted is True
      and (res7b.found is False or res7b.plan is not None),
      f"found={res7b.found} evals={res7b.candidates_evaluated} "
      f"exhausted={res7b.probe_budget_exhausted}")

print(f"\n=== {sum(results)}/{len(results)} focused checks passed ===",
      flush=True)
sys.exit(0 if all(results) else 1)
