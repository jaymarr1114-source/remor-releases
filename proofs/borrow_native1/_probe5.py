"""Probe 5: parity-balanced examples. Break the claimed%2 correlation."""
import sys, os
sys.path.insert(0, "/home/hatch/workspace/worktrees/borrow-native-1/pylib")
sys.path.insert(0, "/home/hatch/workspace/worktrees/borrow-native-1")

os.environ["REMOR_TEST_MODE"] = "1"
from runtime.core.engine import SwarmEngine

db = "/home/hatch/workspace/worktrees/borrow-native-1/proofs/borrow_native1/_probe5.db"
for f in (db, db + ".oracle.db"):
    if os.path.exists(f):
        os.remove(f)

eng = SwarmEngine(db_path=db, _test_allow_shared=True)
print("engine built", flush=True)

# Parity balanced: True has odd AND even claimed; False has odd AND even.
examples = [
    ({"a": 2, "b": 4, "claimed": 6}, True),    # even
    ({"a": 2, "b": 4, "claimed": 7}, False),   # odd
    ({"a": 3, "b": 4, "claimed": 7}, True),    # odd
    ({"a": 3, "b": 4, "claimed": 8}, False),   # even
    ({"a": 10, "b": 15, "claimed": 25}, True),  # odd
    ({"a": 10, "b": 15, "claimed": 24}, False), # even
    ({"a": 10, "b": 14, "claimed": 24}, True),  # even
    ({"a": 10, "b": 14, "claimed": 25}, False), # odd
]
res = eng.cognition.propose_multi(
    "verify whether the claimed sum is correct",
    examples, ("a", "b", "claimed"))
print("solved:", res.solved, flush=True)
print("candidates:", res.candidates_tried, flush=True)
print("stages:", res.stages_tried, flush=True)
if res.solved:
    import json
    print("plan:", json.dumps(res.plan), flush=True)
