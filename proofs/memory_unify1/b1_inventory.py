#!/usr/bin/env python3
"""MEMORY-UNIFY-1 b1: component inventory with classification evidence.

Verifies every UnifiedMemory component is classified ADOPT / KEEP-AS-HELPER /
REMOVE with live usage evidence from the tree.
"""
import os
import subprocess
import sys

WT = "/home/hatch/workspace/worktrees/memory-unify-1"

def count_importers(name):
    """Count files (outside unified_memory.py) that import name from unified_memory."""
    out = subprocess.run(
        ["grep", "-rl", name, "--include=*.py", "runtime/", "pylib/"],
        cwd=WT, capture_output=True, text=True).stdout
    files = [f for f in out.strip().split("\n") if f
             and "unified_memory.py" not in f]
    return len(files), files

# ADOPT: live module functions with production importers
ADOPT = [
    "record_experience",
    "read_experiences",
    "record_evidence",
    "record_hypothesis",
    "record_experiment",
    "attempt_z",
    "evidence_from_demo_actions",
    "record_distillation_experience",
]

# KEEP-AS-HELPER: extracted from class, now module functions
HELPERS = [
    "query_memory",
    "trace_delta",
    "trace_capability",
    "prior_attempts",
    "similar_experiences",
    "z_check_with_experience",
    "UnifiedAnswer",
]

# REMOVE: the class itself (should have zero references)
REMOVED = ["class UnifiedMemory"]

passed, failed = 0, 0

print("=== ADOPT: live functions with production importers ===")
for name in ADOPT:
    count, files = count_importers(name)
    if count > 0:
        print(f"PASS: {name} ADOPT ({count} importers)")
        passed += 1
    else:
        print(f"FAIL: {name} has no importers but classified ADOPT")
        failed += 1

print("\n=== KEEP-AS-HELPER: module functions (no class) ===")
sys.path.insert(0, WT)
sys.path.insert(0, os.path.join(WT, "pylib"))
from runtime.intellect import unified_memory as um
for name in HELPERS:
    if hasattr(um, name):
        print(f"PASS: {name} exists as module function")
        passed += 1
    else:
        print(f"FAIL: {name} missing from module")
        failed += 1

print("\n=== REMOVE: class is gone ===")
# Check no "class UnifiedMemory" definition (exclude proofs/ — this script
# itself mentions the class name in comments)
result = subprocess.run(
    ["grep", "-rn", "class UnifiedMemory", "--include=*.py",
     "runtime/", "pylib/", "tests/"],
    cwd=WT, capture_output=True, text=True).stdout.strip()
if not result:
    print("PASS: no 'class UnifiedMemory' definition anywhere")
    passed += 1
else:
    print(f"FAIL: class definition still exists:\n{result}")
    failed += 1

# Check no instantiation (UnifiedMemory() calls)
result = subprocess.run(
    ["grep", "-rn", "UnifiedMemory(", "--include=*.py",
     "runtime/", "pylib/", "tests/"],
    cwd=WT, capture_output=True, text=True).stdout.strip()
# Filter out the test class name which contains it
lines = [l for l in result.split("\n") if l and "QueryHelperTests" not in l
         and "UnifiedMemoryTests" not in l]
if not lines:
    print("PASS: no UnifiedMemory() instantiations")
    passed += 1
else:
    print(f"FAIL: instantiations remain:\n" + "\n".join(lines))
    failed += 1

print(f"\n=== b1: {passed} passed, {failed} failed ===")
sys.exit(0 if failed == 0 else 1)
