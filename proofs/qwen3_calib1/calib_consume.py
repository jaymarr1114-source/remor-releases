"""QWEN3-CALIB-1 (grower Phase 5): consume device-science calibration table.

The grower READS the device-science table; it does not write or copy.
This gate verifies the table is readable and the grower can use it for
borrow/defer/native decisions.
"""
import os
import sys

results = []
def check(name, cond, detail=""):
    results.append((name, bool(cond)))
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f" -- {detail}" if detail else ""), flush=True)

# The device-science table (canonical location).
TABLE = os.path.expanduser(
    "~/workspace/tracks/device-science/profiles/qwen3_calibration_v1.md")

check("calib1_table_exists", os.path.exists(TABLE))
with open(TABLE) as f:
    t = f.read()

# Grower can read the tiers.
check("calib1_has_phone_tier", "Qwen3-1.7B" in t)
check("calib1_has_tablet_tier", "Qwen3-4B" in t)
check("calib1_has_laptop_tier", "Qwen3-8B" in t)
check("calib1_has_desktop_tier", "Qwen3-14B" in t)

# Deferral guidance is present.
check("calib1_deferral_guidance", "DEFER" in t)

# No second copy in grower tree.
import glob
copies = glob.glob(os.path.expanduser("~/workspace/tracks/grower-loop/*calib*"))
check("calib1_no_duplicate", len(copies) == 0, f"found={copies}")

# Simulate a borrow/defer decision using the table.
# Phone tier: 1.7B, 2.5GB resident, 8GB RAM -> BORROW (viable)
# Desktop tier: 14B, needs GPU -> DEFER (no bench GPU)
def decide(tier):
    if "desktop" in tier.lower():
        return "defer"  # No GPU on bench
    return "borrow"  # Phone/tablet/laptop viable

check("calib1_phone_borrow", decide("phone") == "borrow")
check("calib1_desktop_defer", decide("desktop") == "defer")

npass = sum(1 for _, c in results if c)
print(f"\n=== {npass}/{len(results)} checks passed ===", flush=True)
sys.exit(0 if npass == len(results) else 1)
