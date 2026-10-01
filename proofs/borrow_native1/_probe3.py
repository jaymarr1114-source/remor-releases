"""Probe 3: add-only verification. Can the search find add+equals?"""
import sys, os
sys.path.insert(0, "/home/hatch/workspace/worktrees/borrow-native-1/pylib")
sys.path.insert(0, "/home/hatch/workspace/worktrees/borrow-native-1")

os.environ["REMOR_TEST_MODE"] = "1"
from runtime.core.engine import SwarmEngine

db = "/home/hatch/workspace/worktrees/borrow-native-1/proofs/borrow_native1/_probe3.db"
for f in (db, db + ".oracle.db"):
    if os.path.exists(f):
        os.remove(f)

eng = SwarmEngine(db_path=db, _test_allow_shared=True)
print("engine built", flush=True)

examples = [
    ({"a": 3, "b": 4, "claimed": 7}, True),
    ({"a": 3, "b": 4, "claimed": 8}, False),
    ({"a": 10, "b": 15, "claimed": 25}, True),
    ({"a": 10, "b": 15, "claimed": 24}, False),
    ({"a": 7, "b": 8, "claimed": 15}, True),
    ({"a": 7, "b": 8, "claimed": 14}, False),
    ({"a": 12, "b": 9, "claimed": 21}, True),
    ({"a": 12, "b": 9, "claimed": 20}, False),
    ({"a": 5, "b": 11, "claimed": 16}, True),
    ({"a": 5, "b": 11, "claimed": 17}, False),
    ({"a": 20, "b": 30, "claimed": 50}, True),
    ({"a": 20, "b": 30, "claimed": 49}, False),
]
res = eng.cognition.propose_multi(
    "verify whether the claimed sum of two numbers is correct",
    examples, ("a", "b", "claimed"))
print("solved:", res.solved, flush=True)
print("candidates:", res.candidates_tried, flush=True)
if res.solved:
    import json
    print("plan:", json.dumps(res.plan)[:800], flush=True)
    # verify the plan actually recomputes (uses a and b), not a spurious fit
    plan_s = json.dumps(res.plan)
    uses_ab = '"a"' in plan_s or "'a'" in plan_s or "a" in plan_s
    print("mentions_a_b:", ("$param" in plan_s), flush=True)
else:
    print("expressiveness:", str(res.expressiveness)[:300], flush=True)
