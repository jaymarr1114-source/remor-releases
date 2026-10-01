"""Probe 2: 12 op-diverse examples. Can the true recompute-and-compare be found?"""
import sys, os
sys.path.insert(0, "/home/hatch/workspace/worktrees/borrow-native-1/pylib")
sys.path.insert(0, "/home/hatch/workspace/worktrees/borrow-native-1")

os.environ["REMOR_TEST_MODE"] = "1"
from runtime.core.engine import SwarmEngine

db = "/home/hatch/workspace/worktrees/borrow-native-1/proofs/borrow_native1/_probe2.db"
for f in (db, db + ".oracle.db"):
    if os.path.exists(f):
        os.remove(f)

eng = SwarmEngine(db_path=db, _test_allow_shared=True)
print("engine built", flush=True)

# Cross-op examples: same (a,b), the add-answer is WRONG for sub/mul, etc.
examples = [
    ({"op": "add", "a": 6, "b": 4, "claimed": 10}, True),
    ({"op": "add", "a": 6, "b": 4, "claimed": 11}, False),
    ({"op": "sub", "a": 6, "b": 4, "claimed": 2}, True),
    ({"op": "sub", "a": 6, "b": 4, "claimed": 10}, False),   # add's answer
    ({"op": "mul", "a": 6, "b": 4, "claimed": 24}, True),
    ({"op": "mul", "a": 6, "b": 4, "claimed": 10}, False),   # add's answer
    ({"op": "mul", "a": 6, "b": 4, "claimed": 2}, False),    # sub's answer
    ({"op": "add", "a": 9, "b": 7, "claimed": 16}, True),
    ({"op": "add", "a": 9, "b": 7, "claimed": 15}, False),
    ({"op": "sub", "a": 9, "b": 7, "claimed": 2}, True),
    ({"op": "sub", "a": 9, "b": 7, "claimed": 16}, False),   # add's answer
    ({"op": "mul", "a": 9, "b": 7, "claimed": 63}, True),
]
res = eng.cognition.propose_multi(
    "verify whether the claimed result of an arithmetic step is correct",
    examples, ("op", "a", "b", "claimed"))
print("solved:", res.solved, flush=True)
print("stages:", res.stages_tried, flush=True)
print("candidates:", res.candidates_tried, flush=True)
if res.solved:
    import json
    print("plan:", json.dumps(res.plan)[:1200], flush=True)
else:
    print("expressiveness:", str(res.expressiveness)[:500], flush=True)
