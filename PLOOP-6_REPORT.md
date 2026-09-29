# PLOOP-6 REPORT — resource arbitration for the Primary executive's unified loop

**Date:** 2026-09-28 ~21:05 EDT
**Mission:** PLOOP-6 (audit item #7: resource arbitration; EXEC2 prerequisite #2 lane)
**Branch:** `ploop-6-arbitration` (worktree `~/workspace/ploop-6-work`), commit `9d97caf`
**Base:** canonical master `6d412ce`

## What was built

**`runtime/core/resource_arbitrator.py`** (new, ~330 lines): a `ResourceArbitrator`
that decides resource grants between loop controllers under contention.

- **Charter C-4.4 honored by construction:** ARBITRATION = how much a loop is
  GRANTED (executive-level); CAPS = how much a single activation may CONSUME.
  The arbitrator sets each loop's pool size (the grant). Per-activation caps
  stay enforced by the substrate's `LoopAdmission` pools — `spawn()` refuses
  against the pool, so no activation can silently exceed its grant.
- **Policy (fixed, auditable):** (1) every loop with demand gets its
  anti-starvation minimum first (clamped to demand); if minimums alone exceed
  capacity they are scaled down proportionally and the scaling is RECORDED
  (`minimums_scaled=True` + note) — never silent; (2) remaining capacity is
  distributed by priority weight over unmet demand; (3) no grant exceeds stated
  demand; (4) integer concurrency slots use largest-remainder distribution
  with deterministic tie-break.
- **Axes:** compute budget seconds (hard-enforced via pool), concurrency slots
  (hard-enforced via pool), epoch wall-clock (cooperative: `expired_loops()`
  signals the owner to re-arbitrate; nothing is revoked unilaterally).
- **Breach handling is refusal-as-a-value:** the substrate's existing
  `R_ADMISSION_EXHAUSTED` / `R_CONCURRENCY_CAP` paths. Shrinking a grant
  below already-reserved spend never kills running work — `available_s`
  clamps at zero and new spawns are refused (fail-closed), matching the
  substrate's cooperative philosophy.
- **Inspectability:** every round produces an `ArbitrationDecision` (grants,
  headroom, notes); a bounded ledger keeps history for gate audit.
- **Loop names not validated against the six Primary loops** — the loop set
  is provisional (charter C-6.1); a future Curiosity forest uses the same
  mechanism.

**`runtime/core/microcontroller/substrate.py`** (one method added):
`set_grant(loop, budget_s, max_concurrent)` writes an arbitration grant into
a loop's `LoopAdmission` pool. KeyError for unregistered loops (loud, never
silent). No existing code path was modified — smoke-tested that
register/spawn/retire behavior is unchanged.

**Legacy-router resolution (no-duplication):** `runtime/core/arbitration.py`
(`LocalArbitrator`: DIRECT/SYNTHESIZE/DECOMPOSE) is a *task-tier router*
("what must the engine do to handle this task"), live in `engine.py`. It was
left untouched: it routes tasks, the new module grants resources — different
decisions, different names, exactly one decider per decision. The distinction
is documented in the new module's docstring.

## Proof (real runs, this machine)

- `proofs/ploop6_resource_arbitration.py` — contention harness against the
  REAL `MicrocontrollerSubstrate` (fake clock injected, a supported API):
  three loops demand 170s/9 slots against 100s/4 slots of capacity; real
  `spawn()` calls run until refusal. **30/30 checks passed.**
  Log: `proofs/ploop6_proof_2026-09-28.log`.
  - Grants within capacity on both axes; minimums honored; no grant exceeds
    demand; higher weight wins the remainder.
  - Budget axis: 5×10s spawns against a 50s grant, 6th+ refused with
    `R_ADMISSION_EXHAUSTED`; refusals counted; reserved ≤ grant.
  - Concurrency axis: loser of slot contention (grant 1 slot) has its 2nd
    spawn refused with `R_CONCURRENCY_CAP` — fails closed, never over-grant.
  - Caps≠grants: `budget_s=0` refused `R_INVALID_BUDGET` independently of
    arbitration; a 1000s activation against a 10s grant refused vs the pool.
  - Re-arbitration follows changed demands; epoch expiry observable;
    unregistered-loop/negative-demand/empty-name/unknown-substrate-loop all
    raise explicitly; over-capacity minimums scaled AND recorded.
- `tests/contracts/test_resource_arbitration.py` — 7 contract tests pinning
  grant semantics for regression. **7/7 passed.**
  Log: `proofs/ploop6_unittest_2026-09-28.log`.
- One self-caught test bug during development: the first harness draft
  conflated the budget and concurrency axes (expected 4 budget-spawns where
  the concurrency grant allowed 2). The mechanism was correct; the test was
  fixed to isolate axes. Recorded here, not hidden.

## Exact next boundary

**Adoption:** the mechanism is proven standalone against the real substrate,
but nothing in the tree calls it yet. The executive still registers loops
with static 600s/16 grants (`executive.py: _LOOP_BUDGET_S`), and the Run
Controller (which owns resource+concurrency admission per James's
architecture) has no arbitrator inlet. Wiring `register_loop → set_demand →
arbitrate → apply` into the executive/run-controller cadence is the next
boundary — a deliberate integration mission, not part of this one.

## What remains unproven

- Dynamic behavior across many epochs under a real run (the harness proves
  single-epoch arbitration + one re-arbitration, not long-horizon stability
  of grant oscillations).
- Interaction with PLOOP-1's boundary detection once it lands (detection
  will drive demand; the arbitrator has not yet seen detector-shaped
  demand patterns).
- The cross-executive grant question (charter OQ-5) is untouched by design:
  this builds the Primary-side mechanism only.
- Heavy-load timing characteristics (grant computation is O(loops); not
  profiled under hundreds of loops).
- The independent gate has not re-run this battery (this report is the
  coordinator's claim; crossing requires Felix's gate).

## Stopping condition

Report complete. Mechanism built, proven 30/30 + 7/7 on real runs,
committed cleanly with no verification claims in the message.
