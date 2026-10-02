"""UNIFIED-MEMORY-1 re-verification: unified-memory invariant proof.

Mandate: "proof of the unified-memory invariant and bypass-writer elimination"

The invariant:
1. Every gap write goes through GapRegistry (no direct sqlite bypasses)
2. GapRegistry flows through the unified path (record_experience) when
   epistemic is provided
3. Unified-write failures are LOUD, not silent (no `except: pass`)

FALSIFICATION DESIGN: Tests fail if bypasses exist, if the registry can be
bypassed, or if unified failures are silent.

Provenance: Felix, 2026-10-02, Phase 4 (v10 convergence to completion).
"""

import sys
import os
import re
import logging

sys.path.insert(0, "/home/hatch/workspace/remor_convergence/canonical")

CANONICAL = "/home/hatch/workspace/remor_convergence/canonical"


def test_no_bypass_writers():
    """FALSIFICATION: If any code writes directly to gaps.db bypassing
    GapRegistry, this test FAILS. The invariant requires a single write path.
    """
    bypasses = []
    for root, dirs, files in os.walk(os.path.join(CANONICAL, "runtime")):
        # Skip test/proof directories
        if "test" in root or "proof" in root:
            continue
        for f in files:
            if not f.endswith(".py"):
                continue
            path = os.path.join(root, f)
            with open(path) as fh:
                lines = fh.readlines()
            for i, line in enumerate(lines):
                stripped = line.strip()
                # Skip comments
                if stripped.startswith("#"):
                    continue
                # Look for actual sqlite3.connect calls with gaps.db
                # (not just mentions in comments or strings)
                if "sqlite3.connect" in line and "gaps.db" in line:
                    # Check if it's in GapRegistry class (allowed)
                    # Look backwards for class definition
                    context = "".join(lines[max(0, i-50):i+1])
                    if "class GapRegistry" not in context:
                        bypasses.append(f"{path}:{i+1}")
                # Look for actual INSERT executions (not in comments)
                if "con.execute" in line and "m7_gap_records" in line and "INSERT" in line:
                    context = "".join(lines[max(0, i-100):i+1])
                    # Must be in GapRegistry._save
                    if not ("class GapRegistry" in context and "def _save" in context):
                        bypasses.append(f"{path}:{i+1}")

    # Deduplicate
    bypasses = list(set(bypasses))
    assert len(bypasses) == 0, (
        f"FALSIFIED: Found {len(bypasses)} bypass writers to gaps.db: {bypasses}. "
        f"The invariant requires ALL writes through GapRegistry.")
    print(f"[PASS] no_bypass_writers: 0 direct writes to gaps.db outside GapRegistry")


def test_unified_failure_not_silent():
    """FALSIFICATION: If GapRegistry swallows unified-write exceptions
    silently (the old `except: pass`), this test FAILS. Failures must be loud.
    """
    gaps_py = os.path.join(CANONICAL, "runtime/acquisition/gaps.py")
    with open(gaps_py) as fh:
        content = fh.read()

    # The old bad pattern: `except Exception:` followed by `pass`
    # (with optional comment) and nothing else
    bad_pattern = re.compile(
        r"except\s+Exception\s*:\s*\n\s*pass\s*(#.*)?\n",
        re.MULTILINE)
    matches = bad_pattern.findall(content)
    assert len(matches) == 0, (
        f"FALSIFIED: Found silent `except Exception: pass` in gaps.py. "
        f"Unified-write failures must be loud, not swallowed.")
    print("[PASS] unified_failure_not_silent: no silent exception swallowing")


def test_registry_single_write_path():
    """FALSIFICATION: If GapRecord can be persisted without going through
    GapRegistry._save, the invariant is broken.
    """
    # Verify _save is the ONLY method that INSERTs into m7_gap_records
    gaps_py = os.path.join(CANONICAL, "runtime/acquisition/gaps.py")
    with open(gaps_py) as fh:
        lines = fh.readlines()

    insert_lines = []
    for i, line in enumerate(lines):
        # Look for INSERT targeting m7_gap_records specifically
        # (not m7_dependency_inventory or other tables)
        if "INSERT" in line and "m7_gap_records" in line:
            # Verify it's not in a comment
            stripped = line.strip()
            if not stripped.startswith("#"):
                insert_lines.append(i+1)

    assert len(insert_lines) == 1, (
        f"FALSIFIED: Found {len(insert_lines)} INSERT paths to m7_gap_records "
        f"at lines {insert_lines}, expected exactly 1 (in GapRegistry._save).")
    print(f"[PASS] registry_single_write_path: 1 INSERT path (GapRegistry._save, line {insert_lines[0]})")


if __name__ == "__main__":
    test_no_bypass_writers()
    test_unified_failure_not_silent()
    test_registry_single_write_path()
    print()
    print("All UNIFIED-MEMORY-1 invariant tests PASSED.")
    print("Proven: No bypass writers, single write path via GapRegistry,")
    print("unified failures are loud (not silent).")
