# DEAD-DISTILL-1 Report

**Status:** Complete. REPORT, not landed.
**Worktree:** `~/workspace/worktrees/dead-distill-1`, branch `dead-distill-1-work`
**Commit:** (see handoff)

## Objective
Remove the dead distillation machinery: the dead `DistillationController`,
the test-only `DeltaSession`, and the false "only delta source" docstring
claim — with zero behavior change proven by byte-identical outputs on the
live paths.

## What was removed

1. **`runtime/core/distillation_controller.py` (490 lines, deleted entirely)**
   - The `DistillationController` class had ZERO imports anywhere in the
     tree. Completely orphaned. No live code instantiated it.
   - Its docstring (line 28) contained the false architectural claim:
     "DeltaSession / delta_capture.py (V10-P3): the only delta source."
     The claim dies with the file.

2. **`DeltaSession` class (`runtime/intellect/delta_capture.py`, ~90 lines)**
   - Instantiated ONLY in `tests/intellect/test_delta_capture.py`.
     No production code used it. Test-only, as the audit found.
   - Also removed the orphaned `read_session_deltas` helper (only used by
     DeltaSession tests) and the now-unused `SessionError` exception
     (only raised by DeltaSession; the remote_dispatch `SessionError` is
     a different class, untouched).

3. **`SessionLifecycleTests` (`tests/intellect/test_delta_capture.py`, 7 tests)**
   - The test class exercising DeltaSession. Removed with the class.

4. **Docstring updates**
   - `runtime/acquisition/distill_driver.py`: replaced the "DeltaSession
     capture... the only source of technique_delta records" line with the
     actual live capture path (`acquisition/ingest.py`).
   - `runtime/intellect/delta_capture.py` module docstring: removed the
     DeltaSession bullet and Coverage section; documents the live
     ingest.py path.

## What was kept

- `validate_delta`, `emit_delta`, `DeltaRefused`, and the causal
  adjudication machinery in `delta_capture.py` — these are the validation
  boundary the live ingest path uses. Not in scope.
- The live `DistillationLoop` (`runtime/acquisition/distill.py`) —
  untouched, as required.

## Gate evidence (`proofs/dead_distill1/gate_run.sh` — 7/7 PASS)

- **b0:** zero `DistillationController` references, zero `DeltaSession`
  references, zero "only delta source" claims, file confirmed deleted.
- **b1:** behavioral fingerprint of the live path (DistillationLoop
  interface, distill_driver interface, validate_delta on fixed inputs,
  source hashes) — **byte-identical (sha256) before/after removal**.
- **b2:** 13/13 surviving delta_capture tests pass.
- **b3:** all live imports intact.

## Incidents

None. No live code touched the removed names; the deletion was clean.

## Exact next boundary

The `validate_delta`/`emit_delta` functions in `delta_capture.py` have no
live importers either (only tests use them). They were kept because the
packet scoped to `DeltaSession`, and they are the validation boundary the
audit describes as live machinery. If a future mission confirms the live
ingest path does not call them, they are the next dead-code candidates.

## What remains unproven

- No full end-to-end `DistillationLoop` distill() run was performed
  (would require 8B inference; host was running the BORROW-NATIVE-1
  gate). The byte-identical fingerprint + import checks + unit tests are
  the operative proof for a pure deletion.
- Felix's independent gate re-run (this report is the coordinator's claim).
