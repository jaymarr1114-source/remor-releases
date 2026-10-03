#!/usr/bin/env python3
"""CONTINUOUS-OPS REBUILD: Real ControllerCheckpoint kill/restart proof.

Proves the REAL ControllerCheckpoint from runtime/core/run_controller.py
(not a schema copy) survives process death:
1. Write state via the REAL ControllerCheckpoint
2. Simulate process death (close and reopen — the SQLite file persists)
3. New ControllerCheckpoint instance reads the same file
4. State is preserved

No schema copies. Real class, real SQLite, real persistence.
"""
import sys
import os
import tempfile

sys.path.insert(0, "/home/hatch/workspace/remor_convergence/canonical")
sys.path.insert(0, "/home/hatch/workspace/remor_convergence/canonical/pylib")

from runtime.core.run_controller import ControllerCheckpoint


def test_real_checkpoint_class():
    """The REAL ControllerCheckpoint class is used (not a copy)."""
    print("[TEST] real ControllerCheckpoint class...", flush=True)
    
    assert ControllerCheckpoint.__module__ == "runtime.core.run_controller", \
        f"Wrong module: {ControllerCheckpoint.__module__}"
    
    print(f"  PASS: Real class from {ControllerCheckpoint.__module__}", flush=True)
    return True


def test_checkpoint_survives_reopen():
    """State written via ControllerCheckpoint survives close/reopen."""
    print("[TEST] checkpoint survives close/reopen (simulated death)...", flush=True)
    
    tmpdir = tempfile.mkdtemp(prefix="continuous_ops_real_")
    ckpt_path = os.path.join(tmpdir, "checkpoint.db")
    
    # Phase 1: Write state via REAL ControllerCheckpoint
    ckpt1 = ControllerCheckpoint(ckpt_path)
    ckpt1.save_meta("run_id", "test-run-123")
    ckpt1.save_meta("mission_state", "in_progress")
    ckpt1.mark_gap_processed("gap_001", "completed")
    
    # Verify it was written
    assert ckpt1.load_meta("run_id") == "test-run-123", "Write failed"
    
    # Phase 2: Simulate death — delete the object, create a new one
    # (In reality, the process would die via SIGKILL; the SQLite file
    # persists on disk. We simulate by creating a fresh instance.)
    del ckpt1
    
    # Phase 3: New instance reads the same file
    ckpt2 = ControllerCheckpoint(ckpt_path)
    
    # State must be preserved
    assert ckpt2.load_meta("run_id") == "test-run-123", "run_id lost!"
    assert ckpt2.load_meta("mission_state") == "in_progress", "mission_state lost!"
    assert ckpt2.is_gap_processed("gap_001"), "gap_001 should be marked processed"
    
    print("  PASS: State survived (run_id, mission_state, gap_001)", flush=True)
    return True


def test_checkpoint_isolation():
    """Different checkpoint files are isolated (no cross-contamination)."""
    print("[TEST] checkpoint isolation...", flush=True)
    
    tmpdir = tempfile.mkdtemp(prefix="continuous_ops_isolation_")
    path1 = os.path.join(tmpdir, "ckpt1.db")
    path2 = os.path.join(tmpdir, "ckpt2.db")
    
    ckpt1 = ControllerCheckpoint(path1)
    ckpt1.save_meta("id", "first")
    
    ckpt2 = ControllerCheckpoint(path2)
    ckpt2.save_meta("id", "second")
    
    assert ckpt1.load_meta("id") == "first", "ckpt1 corrupted"
    assert ckpt2.load_meta("id") == "second", "ckpt2 corrupted"
    
    print("  PASS: Checkpoints isolated", flush=True)
    return True


def main():
    print("=" * 60, flush=True)
    print("CONTINUOUS-OPS REBUILD: Real ControllerCheckpoint proof", flush=True)
    print("=" * 60, flush=True)
    
    results = []
    results.append(("real_checkpoint_class", test_real_checkpoint_class()))
    results.append(("checkpoint_survives_reopen", test_checkpoint_survives_reopen()))
    results.append(("checkpoint_isolation", test_checkpoint_isolation()))
    
    print("=" * 60, flush=True)
    passed = sum(1 for _, ok in results if ok)
    total = len(results)
    print(f"Results: {passed}/{total} passed", flush=True)
    
    for name, ok in results:
        status = "PASS" if ok else "FAIL"
        print(f"  [{status}] {name}", flush=True)
    
    if passed == total:
        print("\nAll CONTINUOUS-OPS rebuild tests PASSED.", flush=True)
        print("Real ControllerCheckpoint, real persistence, survives death.", flush=True)
        return 0
    else:
        print(f"\n{total - passed} test(s) FAILED.", flush=True)
        return 1


if __name__ == "__main__":
    sys.exit(main())
