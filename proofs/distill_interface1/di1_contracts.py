"""DISTILL-INTERFACE-FREEZE-1 gate: interface spec exists, contracts hold."""
import os
import sys

WT = os.path.expanduser("~/workspace/worktrees/distill-interface-1")
P = os.path.join(WT, "proofs", "distill_interface1")

results = []
def check(name, cond, detail=""):
    results.append((name, bool(cond)))
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f" -- {detail}" if detail else ""), flush=True)

spec = os.path.join(P, "DISTILL_INTERFACE_v1.md")
check("di1_spec_exists", os.path.exists(spec))
with open(spec) as f:
    s = f.read()

check("di1_frozen", "FROZEN" in s)
check("di1_registry_contract", "acquired." in s and "registry" in s.lower())
check("di1_routing_contract", "borrow" in s.lower() and "defer" in s.lower() and "native" in s.lower())
check("di1_reviewboard", "ReviewBoard" in s and "ADMIT" in s)
check("di1_no_silent_promotion", "No silent promotion" in s)
check("di1_versioned", "v1 (2026-10-02)" in s)

# Verify the contracts match reality: acquired. prefix is used.
distill_py = os.path.join(WT, "runtime", "acquisition", "distill.py")
with open(distill_py) as f:
    d = f.read()
check("di1_prefix_in_code", 'startswith("acquired.")' in d)
check("di1_reviewboard_in_code", "ReviewBoard" in d)

npass = sum(1 for _, c in results if c)
print(f"\n=== {npass}/{len(results)} checks passed ===", flush=True)
sys.exit(0 if npass == len(results) else 1)
