#!/usr/bin/env python3
"""DELTA-NAME-1 b1: prove _m1_delta_dict() output is byte-identical to the
old ingest.DeltaRecord(...).as_dict() for representative inputs.

Loads the OLD ingest.py from git HEAD into a scratch module, constructs the
old dataclass, and compares its as_dict() JSON against the new builder.
"""
import hashlib
import json
import subprocess
import sys
import types

WT = "/home/hatch/workspace/worktrees/delta-name-1"

# --- load OLD ingest.py from git HEAD -------------------------------------
old_src = subprocess.run(
    ["git", "-C", WT, "show", "HEAD~1:runtime/acquisition/ingest.py"],
    capture_output=True, text=True, check=True).stdout
old_mod = types.ModuleType("old_ingest")
old_mod.__name__ = "old_ingest"
# stub the heavy lazy imports: the module only needs dataclasses at load
sys.modules["old_ingest"] = old_mod
exec(compile(old_src, "old_ingest.py", "exec"), old_mod.__dict__)
OldDeltaRecord = old_mod.DeltaRecord
from dataclasses import asdict as _asdict

# --- load NEW builder ------------------------------------------------------
sys.path.insert(0, WT)
from runtime.acquisition.ingest import _m1_delta_dict  # noqa: E402

CASES = [
    dict(delta_id="delta_abc123", X="sort a list",
         Y={"y_id": "y_1", "action_count": 3, "source": "subagent_trace"},
         Z={"capabilities": ["cap_a"], "primitives": ["p1"],
            "vocabulary_size": 2, "at": 1234.5},
         gap="novel composition", T={"technique_name": "sort a list",
                                     "required_tools": ["sort"]},
         E=["ev_1"], D=[], V={"status": "unverified", "method": None},
         C=None),
    dict(delta_id="delta_empty", X="",
         Y={"y_id": "y_2", "action_count": 0, "source": "chat_turn"},
         Z={"capabilities": [], "primitives": [],
            "vocabulary_size": 0, "at": 0.0},
         gap="", T={"technique_name": "", "required_tools": []},
         E=[], D=["dep1"], V={"status": "unverified", "method": None},
         C=None),
    dict(delta_id="delta_unicode", X="trier par ordre — sorting with ünïcode",
         Y={"y_id": "y_3", "action_count": 7, "source": "plugin_bot"},
         Z={"capabilities": ["c1", "c2"], "primitives": [],
            "vocabulary_size": 2, "at": 99999.99},
         gap="gap with \"quotes\" and\nnewline",
         T={"technique_name": "t", "required_tools": ["b", "a"]},
         E=["ev_1", "ev_2"], D=[],
         V={"status": "unverified", "method": None}, C=None),
]

ok = True
for i, kw in enumerate(CASES):
    old_out = _asdict(OldDeltaRecord(**kw))
    new_out = _m1_delta_dict(**kw)
    old_json = json.dumps(old_out, sort_keys=True, ensure_ascii=False)
    new_json = json.dumps(new_out, sort_keys=True, ensure_ascii=False)
    old_h = hashlib.sha256(old_json.encode()).hexdigest()
    new_h = hashlib.sha256(new_json.encode()).hexdigest()
    same = old_h == new_h
    print(f"case {i}: {'IDENTICAL' if same else 'DIFFER'} "
          f"(sha256 {old_h[:12]} vs {new_h[:12]})")
    if not same:
        ok = False
        print("  OLD:", old_json[:300])
        print("  NEW:", new_json[:300])

# key order must also match (asdict preserves field declaration order)
kw = CASES[0]
old_keys = list(_asdict(OldDeltaRecord(**kw)).keys())
new_keys = list(_m1_delta_dict(**kw).keys())
print(f"key order: {'IDENTICAL' if old_keys == new_keys else 'DIFFER'} "
      f"{old_keys == new_keys}")
ok = ok and (old_keys == new_keys)

print("B1 " + ("PASS: byte-identical" if ok else "FAIL"))
sys.exit(0 if ok else 1)
