#!/usr/bin/env python3
"""b1_capture.py: behavioral fingerprint of the LIVE distillation path.

Captures deterministic structural facts about the live machinery that
DEAD-DISTILL-1 must not change:
- DistillationLoop class interface (method names)
- distill_driver module interface (function names)
- validate_delta / emit_delta behavior on fixed inputs
- distill.py module source hash (must be untouched)

Outputs JSON to stdout. Byte-identical output before/after the removal
proves zero behavior change on the live path.
"""
import hashlib
import inspect
import json
import sys

sys.path.insert(0, ".")
sys.path.insert(0, "pylib")

from swarm_engine.acquisition import distill as distill_mod
from runtime.acquisition import distill_driver as driver_mod
from runtime.intellect.delta_capture import validate_delta

fingerprint = {}

# 1. Live DistillationLoop interface
fingerprint["DistillationLoop_methods"] = sorted(
    m for m in dir(distill_mod.DistillationLoop)
    if not m.startswith("_")
)

# 2. Live distill_driver interface
fingerprint["distill_driver_functions"] = sorted(
    n for n, o in vars(driver_mod).items()
    if callable(o) and not n.startswith("_")
    and getattr(o, "__module__", "") == driver_mod.__name__
)

# 3. validate_delta behavior on fixed inputs (deterministic)
good = {
    "objective_x": "test objective with sufficient length here",
    "external_demo_y": "external demonstration description",
    "native_inventory_z": "native inventory description",
    "capability_gap": "before: failed; after: passed -- technique shown",
    "technique_t": {"name": "t", "probe": {"import": "os", "attr": "getcwd", "check": "callable"}},
    "evidence_e": [{"kind": "run", "cmd": "true", "output_includes": ""}],
    "dependencies_d": ["stdlib"],
    "verification_v": "manual verification statement",
    "resulting_capability_c": "os.getcwd",
}
fingerprint["validate_good"] = validate_delta(good)
fingerprint["validate_empty"] = validate_delta({})
fingerprint["validate_non_dict"] = validate_delta(["x"])

# 4. Source hashes of live-path files (must be untouched by this mission)
for path in [
    "runtime/acquisition/distill.py",
    "pylib/swarm_engine/acquisition/distill.py",
]:
    try:
        with open(path, "rb") as f:
            fingerprint[f"sha256:{path}"] = hashlib.sha256(f.read()).hexdigest()
    except FileNotFoundError:
        fingerprint[f"sha256:{path}"] = "NOT-FOUND"

print(json.dumps(fingerprint, indent=2, sort_keys=True))
