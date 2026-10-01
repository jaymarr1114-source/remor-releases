"""Probe 4: minimal 4-example add-only. Does the search EVER find add+equals?"""
import sys, os
sys.path.insert(0, "/home/hatch/workspace/worktrees/borrow-native-1/pylib")
sys.path.insert(0, "/home/hatch/workspace/worktrees/borrow-native-1")

os.environ["REMOR_TEST_MODE"] = "1"
from runtime.core.engine import SwarmEngine

db = "/home/hatch/workspace/worktrees/borrow-native-1/proofs/borrow_native1/_probe4.db"
for f in (db, db + ".oracle.db"):
    if os.path.exists(f):
        os.remove(f)

eng = SwarmEngine(db_path=db, _test_allow_shared=True)
print("engine built", flush=True)

examples = [
    ({"a": 3, "b": 4, "claimed": 7}, True),
    ({"a": 3, "b": 4, "claimed": 8}, False),
    ({"a": 10, "b": 15, "claimed": 25}, True),
    ({"a": 10, "b": 15, "claimed": 26}, False),
]
res = eng.cognition.propose_multi(
    "verify whether the claimed sum is correct",
    examples, ("a", "b", "claimed"))
print("solved:", res.solved, flush=True)
print("candidates:", res.candidates_tried, flush=True)
if res.solved:
    import json
    print("plan:", json.dumps(res.plan), flush=True)
