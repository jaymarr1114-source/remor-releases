"""UNIFIED-MEMORY-1 gate battery 1: bypass-writer census.

Proves zero loop-owned direct writers bypass the unified facade for the
four record types (experience/evidence/hypothesis/experiment).

The facade (runtime/intellect/unified_memory.py) is the ONLY production
caller of the EpistemicStore's raw write methods. The store's own internal
convenience methods are the only other callers.
"""
import os
import re
import sys

WT = os.path.expanduser("~/workspace/worktrees/unified-memory-1")

results = []
def check(name, cond, detail=""):
    results.append((name, bool(cond)))
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" +
          (f" -- {detail}" if detail else ""), flush=True)

# The raw store write methods that the facade wraps.
RAW_METHODS = [
    "save_observation", "record_observation",
    "save_evidence",
    "save_hypothesis", "record_hypothesis",
    "save_experiment",
]

# Files allowed to call raw methods: the store itself (internal) and the facade.
ALLOWED_FILES = {
    "runtime/intellect/epistemic.py",      # store internals
    "pylib/swarm_engine/intellect/epistemic.py",
    "runtime/intellect/unified_memory.py", # the facade
    "pylib/swarm_engine/intellect/unified_memory.py",
}

# Scan runtime/ and pylib/ for direct calls to raw methods.
# Exclude tests, proofs, and the allowed files.
pattern = re.compile(
    r"\.(%s)\s*\(" % "|".join(RAW_METHODS))

bypass_found = []
for root_dir in ("runtime", "pylib"):
    base = os.path.join(WT, root_dir)
    for dirpath, dirnames, filenames in os.walk(base):
        # Skip test/proof dirs
        if "/tests/" in dirpath or "/proofs/" in dirpath:
            continue
        # Skip __pycache__
        dirnames[:] = [d for d in dirnames if d != "__pycache__"]
        for fn in filenames:
            if not fn.endswith(".py"):
                continue
            fpath = os.path.join(dirpath, fn)
            rel = os.path.relpath(fpath, WT)
            if rel in ALLOWED_FILES:
                continue
            try:
                with open(fpath) as f:
                    src = f.read()
            except Exception:
                continue
            for m in pattern.finditer(src):
                # Exclude method definitions and comments
                line_start = src.rfind("\n", 0, m.start()) + 1
                line = src[line_start:src.find("\n", m.start())]
                stripped = line.strip()
                if stripped.startswith("def ") or stripped.startswith("#"):
                    continue
                bypass_found.append(f"{rel}: {stripped[:80]}")

check("um1_zero_bypass_writers", len(bypass_found) == 0,
      f"bypass={bypass_found[:3]}" if bypass_found else "clean")

# Verify the facade functions exist and are the documented four.
facade_path = os.path.join(WT, "runtime", "intellect", "unified_memory.py")
with open(facade_path) as f:
    facade_src = f.read()
for func in ("record_experience", "record_evidence",
             "record_hypothesis", "record_experiment",
             "read_experiences"):
    check(f"um1_facade_has_{func}",
          f"def {func}(" in facade_src)

# Verify the four facade functions actually call the store (not stubs).
check("um1_facade_writes_store",
      "save_observation" in facade_src and
      "save_evidence" in facade_src and
      "save_hypothesis" in facade_src and
      "save_experiment" in facade_src)

npass = sum(1 for _, c in results if c)
print(f"\n=== census: {npass}/{len(results)} checks passed ===", flush=True)
sys.exit(0 if npass == len(results) else 1)
