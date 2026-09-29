# PLOOP-3 — Continuous turning + acceptance evidence

**Mission:** close Primary-loop audit item #3 independently (no gating on sibling PLOOP missions).
**Date:** 2026-09-28/29. **Worktree:** `~/workspace/worktrees/ploop-3`, branch `ploop-3-continuous-acceptance`.
**Status:** mechanism built, proof green, report written. Commit pending final clean rerun.

## What was missing (the real gap)

1. `RunController.tick()` executed gap-queue → distillation sweep → quarantine sweep, but **never invoked acceptance per cycle**. `invoke_acceptance()` existed and delegated to the V10-P6 driver, yet nothing in the runtime called it — it was dead machinery in the cycle path.
2. `AcceptanceLoop.present()` raises on authentication failure and `AcceptanceStore` keys by `run_id` — so there was **no honest path for a failed acceptance to be persisted**; a failed cycle would either crash the run or be silently dropped.

## What was built (reused, not duplicated)

- **`runtime/services/acceptance_driver.py`** — added `AcceptanceDriver.evaluate_cycle()` (deterministic, evidence-derived verdict criteria, single source of truth) and `AcceptanceDriver.accept_cycle()`.
  - Operational authentication: re-reads the **persisted** `rc_cycles` row for the cycle and verifies the cycle's claims (errors, budget flags, cycle number) against what's on disk — never trusts the in-memory summary.
  - Named checks: `row_exists`, `cycle_number_matches`, `claims_match_persisted`, `no_tick_error`, `no_step_errors`, `budget_honored`. A cycle passes only when all pass.
  - PASS → `loop.present()` (the real inlet) → `ACCEPTED` with an honest `close_reason` naming the autonomous evidence-derived verdict.
  - FAIL → `AcceptanceRecord(state=REJECTED)` recorded directly through `AcceptanceStore`, failed checks named, near-miss characterized and persisted to the epistemic store. **Never skipped, never auto-passed.**
  - Deliberately does NOT route through `present_result()`: that inlet's auth gate re-executes synthesis plans, and a cycle summary is not a plan — forcing it through would be gaming the mechanism.
- **`runtime/core/run_controller.py`** — `run()` now calls `self._accept_cycle(cycle_n, summary)` after every cycle's checkpoint write, re-records the checkpoint row with the acceptance evidence attached, and records any acceptance-infrastructure failure as `acceptance_error` on the summary (loud, never silent; acceptance never kills the run).
- **Proof:** `proofs/ploop3_continuous_acceptance_2026-09-28.py`.

## Proof results (2026-09-29 ~01:32 UTC, final clean run)

- **RUN A** — real `RunController`, one `run()` call, seeded with one genuine user gap (real dispatch → honestly "open", backed off): **10/10 consecutive cycles**, each carrying an `accepted` acceptance record; cycles numbered 1..10 back-to-back, no manual reset, no resume.
- **RUN B** — same machinery with a zero cycle budget (genuinely trips `budget_exceeded` through the real `tick()` path, no mocks): **2/2 cycles recorded `rejected`**, failed check names `budget_honored`, near-miss observations persisted to the epistemic store (2 found).
- **RECHECK** — independent pass with fresh sqlite connections (no controller objects), re-deriving every verdict from the persisted rows via the driver's own `evaluate_cycle` (imported, not copied): **12/12 records verified from disk** (10 accepted + 2 rejected), check-level agreement on all 6 named checks per record.
- **Regression:** `tests/contracts/test_acceptance_loop.py` + `tests/backend/test_run_control.py` — **21/21 passed**.
- The first green run overlapped PLOOP-2's handoff battery (37/37, EXIT 0) and the gensynth5 e2e; the run above is a **clean uncontended rerun** after both finished (gensynth5 log: EXIT 0). No heavy proof battery was running during it.

## Exact next boundary

- The per-cycle accept stage is proven for the controller's own cadence. The next boundary is **cross-loop acceptance**: other loops (Acquisition, Execution+Repair, Distillation, Generalization) do not yet route their per-cycle/per-result claims through an equivalent per-turn acceptance — item #3's sibling audit items (loop-transition/handoff contracts, Graph Controller symmetry) are where that wiring will be tested. PLOOP-3 proves the pattern, not the coverage.
- `acceptance_error` path (acceptance infrastructure itself failing mid-run) is instrumented and loud but was **not exercised** in this proof — it has no test. An adversarial test (e.g., checkpoint DB locked/unwritable during acceptance) would close that.

## What remains unproven

- Long-horizon turning (hundreds of cycles, pause/resume/stop interplay with acceptance records) — the proof ran 10 cycles in ~4s.
- Acceptance under real Q7 quarantine pressure (proof used `trusted_indexes=[]`; no network).
- Whether the 6-check criteria are the *right* bar for a cycle (they are the honest-mechanism bar: no crashes, no errors, budget honored, claims match disk) — a stronger bar (e.g., requiring productive work per cycle) is a product decision, not taken here.
- Sibling-overlap: resolved — the committed evidence is the clean uncontended rerun (see Proof results).