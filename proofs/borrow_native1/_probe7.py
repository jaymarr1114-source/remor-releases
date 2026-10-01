"""Probe 7: can the distill loop distill simple addition (a,b) -> a+b?

If yes, we have a working (simpler) cycle. If no, the synthesis is
fundamentally broken for arithmetic.
"""
import sys, os
sys.path.insert(0, "/home/hatch/workspace/worktrees/borrow-native-1/pylib")
sys.path.insert(0, "/home/hatch/workspace/worktrees/borrow-native-1")

os.environ["REMOR_TEST_MODE"] = "1"
from runtime.core.engine import SwarmEngine
from swarm_engine.acquisition.delta import DeltaRecord
from swarm_engine.acquisition.distill import DistillationLoop

db = "/home/hatch/workspace/worktrees/borrow-native-1/proofs/borrow_native1/_probe7.db"
for f in (db, db + ".oracle.db"):
    if os.path.exists(f):
        os.remove(f)

eng = SwarmEngine(db_path=db, _test_allow_shared=True)
print("engine built", flush=True)

def ex(a, b):
    return {"input": {"a": a, "b": b}, "output": a + b}

evidence = [
    ex(2, 3), ex(5, 7), ex(10, 20), ex(1, 1), ex(15, 25),
    ex(3, 8), ex(12, 4), ex(9, 6),
    # held-out
    ex(100, 200), ex(7, 13), ex(50, 50),
]
print(f"{len(evidence)} examples", flush=True)

delta = DeltaRecord(
    objective="compute the sum of two integers",
    external_actions="demonstrated addition",
    prior_capability="none",
    capability_gap="cannot add",
    technique="addition",
    evidence=evidence,
    source="probe7",
).validate()

loop = DistillationLoop(eng, epistemic=None)
result = loop.distill(delta)
print(f"success: {result.success}", flush=True)
print(f"route: {result.route}", flush=True)
print(f"reason: {result.reason}", flush=True)
print(f"promoted: {result.promoted_name}", flush=True)
