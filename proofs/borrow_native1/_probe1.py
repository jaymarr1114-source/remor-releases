"""Probe: can propose_multi synthesize single-step arithmetic verification?"""
import sys, os
sys.path.insert(0, "/home/hatch/workspace/worktrees/borrow-native-1/pylib")
sys.path.insert(0, "/home/hatch/workspace/worktrees/borrow-native-1")

os.environ["REMOR_TEST_MODE"] = "1"
from runtime.core.engine import SwarmEngine

db = "/home/hatch/workspace/worktrees/borrow-native-1/proofs/borrow_native1/_probe.db"
for f in (db, db + ".oracle.db"):
    if os.path.exists(f):
        os.remove(f)

eng = SwarmEngine(db_path=db, _test_allow_shared=True)
print("engine built", flush=True)

# Single-step verification: is the claimed result correct?
examples = [
    ({"op": "add", "a": 3, "b": 4, "claimed": 7}, True),
    ({"op": "add", "a": 3, "b": 4, "claimed": 8}, False),
    ({"op": "sub", "a": 10, "b": 4, "claimed": 6}, True),
    ({"op": "sub", "a": 10, "b": 4, "claimed": 5}, False),
    ({"op": "mul", "a": 6, "b": 7, "claimed": 42}, True),
    ({"op": "mul", "a": 6, "b": 7, "claimed": 41}, False),
]
res = eng.cognition.propose_multi(
    "verify whether the claimed result of an arithmetic step is correct",
    examples, ("op", "a", "b", "claimed"))
print("solved:", res.solved, flush=True)
print("stages:", res.stages_tried, flush=True)
print("candidates:", res.candidates_tried, flush=True)
if res.solved:
    print("plan:", str(res.plan)[:500], flush=True)
else:
    print("expressiveness:", str(res.expressiveness)[:500], flush=True)
