"""Probe 6: can the DISTILL LOOP (not just propose_multi) distill add-verify?

Uses synthetic examples (computed locally, no teacher). Build set has
diverse (a,b); held-out has NOVEL (a,b) pairs. Tests whether Route B can
find a generalizing program.
"""
import sys, os, json
sys.path.insert(0, "/home/hatch/workspace/worktrees/borrow-native-1/pylib")
sys.path.insert(0, "/home/hatch/workspace/worktrees/borrow-native-1")

os.environ["REMOR_TEST_MODE"] = "1"
from runtime.core.engine import SwarmEngine
from swarm_engine.acquisition.delta import DeltaRecord
from swarm_engine.acquisition.distill import DistillationLoop

db = "/home/hatch/workspace/worktrees/borrow-native-1/proofs/borrow_native1/_probe6.db"
for f in (db, db + ".oracle.db"):
    if os.path.exists(f):
        os.remove(f)

eng = SwarmEngine(db_path=db, _test_allow_shared=True)
print("engine built", flush=True)

# 16 examples: first 11 build, last 5 held-out (novel a,b pairs).
def ex(a, b, claimed):
    return {"input": {"a": a, "b": b, "claimed": claimed},
            "output": (a + b) == claimed}

evidence = [
    ex(2, 4, 6), ex(2, 4, 7), ex(3, 4, 7), ex(3, 4, 8),
    ex(10, 15, 25), ex(10, 15, 24), ex(10, 14, 24), ex(10, 14, 25),
    ex(7, 3, 10), ex(7, 3, 11), ex(12, 8, 20),
    # held-out: novel (a,b) pairs
    ex(50, 25, 75), ex(50, 25, 74), ex(9, 9, 18), ex(9, 9, 19),
    ex(100, 1, 101),
]
print(f"{len(evidence)} examples", flush=True)

delta = DeltaRecord(
    objective="verify whether the claimed sum is correct",
    external_actions="demonstrated recompute-and-compare",
    prior_capability="none",
    capability_gap="cannot verify sums",
    technique="recompute-and-compare",
    evidence=evidence,
    source="probe6",
).validate()
print("delta valid", flush=True)

loop = DistillationLoop(eng, epistemic=None)
result = loop.distill(delta)
print(f"success: {result.success}", flush=True)
print(f"route: {result.route}", flush=True)
print(f"reason: {result.reason}", flush=True)
print(f"promoted: {result.promoted_name}", flush=True)
print(f"heldout: {result.heldout_passed}/{result.heldout_examples}", flush=True)
