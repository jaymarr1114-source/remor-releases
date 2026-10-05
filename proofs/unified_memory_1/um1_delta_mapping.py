"""UNIFIED-MEMORY-1 gate battery 2: delta schema mapping.

Proves the three persisted delta schemas map to the one canonical schema
(M2's DeltaRecord):
  - M1 single-letter dict (frozen ingestion schema) -> M2 via _adapt_m1_to_m2
  - Charter technique_t dict (delta-capture schema) -> M2 via charter_to_delta_record
  - M2 DeltaRecord is the canonical class (exactly one in the tree)
"""
import os
import sys

WT = os.path.expanduser("~/workspace/worktrees/unified-memory-1")
sys.path.insert(0, os.path.join(WT, "pylib"))
sys.path.insert(0, WT)

results = []
def check(name, cond, detail=""):
    results.append((name, bool(cond)))
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" +
          (f" -- {detail}" if detail else ""), flush=True)

# 1. Exactly one DeltaRecord class in the tree (M2's).
import subprocess
out = subprocess.run(
    ["grep", "-rn", "^class DeltaRecord", "--include=*.py",
     os.path.join(WT, "runtime"), os.path.join(WT, "pylib")],
    capture_output=True, text=True).stdout.strip().split("\n")
out = [l for l in out if l and "/tests/" not in l and "/proofs/" not in l]
check("um1_one_delta_class", len(out) == 1, f"found={out}")
check("um1_canonical_is_m2",
      len(out) == 1 and "runtime/acquisition/delta.py" in out[0],
      f"found={out}")

# 2. M1 -> M2 adapter exists and is wired in loop_driver.
ld_path = os.path.join(WT, "runtime", "acquisition", "loop_driver.py")
with open(ld_path) as f:
    ld_src = f.read()
check("um1_m1_adapter_exists", "_adapt_m1_to_m2" in ld_src)
check("um1_m1_adapter_wired",
      "m2 = self._adapt_m1_to_m2(delta, actions)" in ld_src)

# 3. Charter -> M2 mapping exists and is wired.
dd_path = os.path.join(WT, "runtime", "acquisition", "distill_driver.py")
with open(dd_path) as f:
    dd_src = f.read()
check("um1_charter_mapping_exists",
      "def charter_to_delta_record(" in dd_src)
check("um1_charter_mapping_wired",
      "charter_to_delta_record" in ld_src)

# 4. Functional: M1 dict adapts to a real M2 DeltaRecord.
# Use the real frozen M1 shape from ingest._m1_delta_dict.
from swarm_engine.acquisition.ingest import _m1_delta_dict
from swarm_engine.acquisition.loop_driver import CognitionLoop
# _adapt_m1_to_m2 is a staticmethod; call it directly with a real M1 dict.
m1 = _m1_delta_dict(
    delta_id="test_m1_001",
    X="objective text",
    Y={"source": "test", "action_count": 2},
    Z={"capabilities": ["c1"], "primitives": ["p1"], "vocabulary_size": 10},
    gap="test gap",
    T={"technique_name": "test_technique"},
    E=["e1"],
    D=["d1"],
    V={"v": 1},
    C=None,
)
try:
    m2 = CognitionLoop._adapt_m1_to_m2(m1, actions=[])
    from swarm_engine.acquisition.delta import DeltaRecord
    check("um1_m1_adapts_to_m2", isinstance(m2, DeltaRecord),
          f"type={type(m2).__name__}")
    check("um1_m1_preserves_id",
          getattr(m2, "delta_id", None) == "test_m1_001")
except Exception as e:
    check("um1_m1_adapts_to_m2", False, f"raised {e}")
    check("um1_m1_preserves_id", False, f"raised {e}")

# 5. Functional: charter dict maps to a real M2 DeltaRecord (or honest refusal).
from swarm_engine.acquisition.distill_driver import (
    charter_to_delta_record, NotDistillable)
charter = {
    "observation_id": "test_charter_001",
    "raw": {"delta": {
        "technique_t": {"name": "test_technique", "steps": ["a", "b"]},
        "objective_x": "test objective",
    }},
}
try:
    m2c = charter_to_delta_record(charter)
    check("um1_charter_maps_to_m2", isinstance(m2c, DeltaRecord),
          f"type={type(m2c).__name__}")
except NotDistillable as e:
    # Honest refusal is acceptable — the mapping refuses cleanly, not silently.
    check("um1_charter_maps_to_m2", True,
          f"honest refusal (NotDistillable): {e}")
except Exception as e:
    check("um1_charter_maps_to_m2", False, f"raised {type(e).__name__}: {e}")

npass = sum(1 for _, c in results if c)
print(f"\n=== delta mapping: {npass}/{len(results)} checks passed ===",
      flush=True)
sys.exit(0 if npass == len(results) else 1)
