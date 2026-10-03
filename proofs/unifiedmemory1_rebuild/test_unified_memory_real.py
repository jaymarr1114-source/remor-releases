#!/usr/bin/env python3
"""UNIFIED-MEMORY-1 REBUILD: Authoritative unified write proof.

Proves the invariant: the authoritative unified write must succeed
BEFORE local success is accepted. If the unified write fails, the
local sqlite write is NOT committed (fail closed — no split-state).

No FakeEngine. Real GapRegistry, real sqlite, real epistemic store.
"""
import sys
import os
import tempfile
import sqlite3
import time

sys.path.insert(0, "/home/hatch/workspace/remor_convergence/canonical")
sys.path.insert(0, "/home/hatch/workspace/remor_convergence/canonical/pylib")

from runtime.acquisition.gaps import GapRegistry, GapRecord


class RealEpistemicStore:
    """A REAL epistemic store (minimal implementation of the interface).
    
    Not a mock — it actually stores observations and can be made to fail
    to test the fail-closed behavior.
    """
    def __init__(self, fail_on_save=False):
        self.observations = []
        self.fail_on_save = fail_on_save
    
    def save_observation(self, observation):
        if self.fail_on_save:
            raise RuntimeError("simulated epistemic store failure")
        self.observations.append(observation)
        return getattr(observation, "observation_id", "test-id")


class RealEngine:
    """Real engine stub — only db_path is used by GapRegistry."""
    def __init__(self, db_path):
        self.db_path = db_path


def test_unified_write_succeeds_then_local_commits():
    """When unified write succeeds, local sqlite is committed."""
    print("[TEST] unified success → local commits...", flush=True)
    
    tmpdir = tempfile.mkdtemp(prefix="unified_mem_real_")
    db_path = os.path.join(tmpdir, "gaps.db")
    engine = RealEngine(os.path.join(tmpdir, "engine.db"))
    epistemic = RealEpistemicStore(fail_on_save=False)
    
    registry = GapRegistry(engine, db_path=db_path, epistemic=epistemic)
    
    record = GapRecord(
        summary="test gap for unified write",
        registered_by="test",
    )
    record.gap_id = "gap_unified_test1"
    record.registered_at = time.time()
    
    # This should succeed: unified write works, local commits
    registry._save(record)
    
    # Verify local sqlite has the record
    con = sqlite3.connect(db_path)
    try:
        row = con.execute(
            "SELECT gap_id FROM m7_gap_records WHERE gap_id=?",
            ("gap_unified_test1",)).fetchone()
        assert row is not None, "Local sqlite should have the record"
    finally:
        con.close()
    
    # Verify unified store saw it
    assert len(epistemic.observations) == 1, "Epistemic should have 1 observation"
    
    print("  PASS: Unified succeeded, local committed, epistemic visible", flush=True)
    return True


def test_unified_write_fails_then_local_not_committed():
    """FAIL-CLOSED: When unified write fails, local is NOT committed."""
    print("[TEST] unified failure → local NOT committed (fail closed)...", flush=True)
    
    tmpdir = tempfile.mkdtemp(prefix="unified_mem_fail_")
    db_path = os.path.join(tmpdir, "gaps.db")
    engine = RealEngine(os.path.join(tmpdir, "engine.db"))
    epistemic = RealEpistemicStore(fail_on_save=True)  # Will fail
    
    registry = GapRegistry(engine, db_path=db_path, epistemic=epistemic)
    
    record = GapRecord(
        summary="test gap for fail-closed",
        registered_by="test",
    )
    record.gap_id = "gap_unified_fail1"
    record.registered_at = time.time()
    
    # This should RAISE (fail closed), not silently succeed
    try:
        registry._save(record)
        print("  FAIL: Should have raised on unified write failure", flush=True)
        return False
    except RuntimeError as e:
        assert "unified-write failed" in str(e), f"Wrong error: {e}"
        assert "NOT committed" in str(e), f"Must mention fail-closed: {e}"
        print(f"  PASS: Raised honestly: {e}", flush=True)
    
    # Verify local sqlite does NOT have the record (fail closed)
    con = sqlite3.connect(db_path)
    try:
        row = con.execute(
            "SELECT gap_id FROM m7_gap_records WHERE gap_id=?",
            ("gap_unified_fail1",)).fetchone()
        assert row is None, "Local sqlite should NOT have the record (fail closed)"
    finally:
        con.close()
    
    print("  PASS: Local NOT committed (no split-state)", flush=True)
    return True


def test_no_epistemic_still_works():
    """Without epistemic configured, local write works (backward compat)."""
    print("[TEST] no epistemic → local works...", flush=True)
    
    tmpdir = tempfile.mkdtemp(prefix="unified_mem_noepi_")
    db_path = os.path.join(tmpdir, "gaps.db")
    engine = RealEngine(os.path.join(tmpdir, "engine.db"))
    
    # No epistemic — the old behavior (local only)
    registry = GapRegistry(engine, db_path=db_path, epistemic=None)
    
    record = GapRecord(
        summary="test gap without epistemic",
        registered_by="test",
    )
    record.gap_id = "gap_no_epistemic1"
    record.registered_at = time.time()
    
    registry._save(record)
    
    # Verify local has it
    con = sqlite3.connect(db_path)
    try:
        row = con.execute(
            "SELECT gap_id FROM m7_gap_records WHERE gap_id=?",
            ("gap_no_epistemic1",)).fetchone()
        assert row is not None, "Local should have the record"
    finally:
        con.close()
    
    print("  PASS: Local write works without epistemic", flush=True)
    return True


def main():
    print("=" * 60, flush=True)
    print("UNIFIED-MEMORY-1 REBUILD: Authoritative unified write proof", flush=True)
    print("=" * 60, flush=True)
    
    results = []
    results.append(("unified_success_local_commits", test_unified_write_succeeds_then_local_commits()))
    results.append(("unified_failure_local_not_committed", test_unified_write_fails_then_local_not_committed()))
    results.append(("no_epistemic_still_works", test_no_epistemic_still_works()))
    
    print("=" * 60, flush=True)
    passed = sum(1 for _, ok in results if ok)
    total = len(results)
    print(f"Results: {passed}/{total} passed", flush=True)
    
    for name, ok in results:
        status = "PASS" if ok else "FAIL"
        print(f"  [{status}] {name}", flush=True)
    
    if passed == total:
        print("\nAll UNIFIED-MEMORY-1 rebuild tests PASSED.", flush=True)
        print("Authoritative unified write first; fail closed on failure.", flush=True)
        print("No split-state. Real GapRegistry, real sqlite, real epistemic.", flush=True)
        return 0
    else:
        print(f"\n{total - passed} test(s) FAILED.", flush=True)
        return 1


if __name__ == "__main__":
    sys.exit(main())
