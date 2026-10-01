#!/usr/bin/env python3
"""MEMORY-UNIFY-1 b4: no regressions in existing test suites."""
import subprocess
import sys

WT = "/home/hatch/workspace/worktrees/memory-unify-1"

suites = [
    "tests/intellect/test_unified_memory.py",
    "tests/intellect/test_delta_capture.py",
]

passed, failed = 0, 0
for suite in suites:
    result = subprocess.run(
        [sys.executable, "-m", "pytest", suite, "-q"],
        cwd=WT, capture_output=True, text=True)
    # Parse the summary line
    for line in result.stdout.split("\n"):
        if "passed" in line and ("failed" in line or "passed" in line):
            print(f"{suite}: {line.strip()}")
            break
    if result.returncode == 0:
        print(f"PASS: {suite}")
        passed += 1
    else:
        print(f"FAIL: {suite}\n{result.stdout[-500:]}\n{result.stderr[-500:]}")
        failed += 1

print(f"\n=== b4: {passed} passed, {failed} failed ===")
sys.exit(0 if failed == 0 else 1)
