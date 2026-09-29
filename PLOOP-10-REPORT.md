# PLOOP-10 Mission Report — Resource-arbitrator adoption

**Status: COMPLETE.** Branch `ploop-10-adoption` in `~/workspace/ploop-10-work`
(base `cfe7915`), committed as `9b9cbe4` — 2 files changed, additive only,
no existing behavior altered.

## What was built

The ResourceArbitrator (PLOOP-6, landed) now actually grants resources on the
live path instead of sitting beside the static defaults.

**`runtime/core/run_controller.py`** (additive):
- `bind_arbitration_substrate(substrate)` — binds the shared substrate.
  Capacity is MEASURED as the sum of the substrate's initial per-loop pools
  (3600.0s / 96 slots from the 600s×6 / 16×6 static regime), never a
  hard-coded constant. All six loops register with equal weight (1.0, no loop
  privileged — the charter peer relationship) and zero minimums: demand rules,
  and starvation shows up as recorded refusals instead of hiding in minimums.
  Missing loop pools raise `KeyError` naming the loop — loud at bind time.
- `_measure_demands()` — per-loop substrate demand from real state:
  `reserved_s` (currently-reserved spend, from the substrate's public
  `export_state()` snapshot — the grant must keep covering active work) +
  pending new work × that work's real per-unit cost; concurrent = active MCs
  + pending units. Acquisition: open gaps (real `GapRegistry.list_gaps`)
  × 600s (the inlet's real hosted cognition-cycle budget, cited constant).
  The other five loops honestly state reserved-only demand: the run loop
  dispatches inline, execution repair is inline, acceptance verification is
  driver-driven, distillation's `spawn_microcontroller` has no callers yet,
  and generalization's `run_cycle` is mission-driven with no steady backlog.
  New demand appears as reserved/active the moment a loop spawns.
- `_arbitrate_resources()` — measure → `set_demand` → `arbitrate` →
  `apply(substrate)` every tick, before the tick's own work. Grants are
  written into the live pools where the substrate's own admission enforces
  them (`admission_exhausted` / `concurrency_cap`, counted in
  `total_refused`) — enforced, not advisory. The round summary records the
  refused portion per loop (demand − grant): visible, never vanished.
  Zero-demand loops get no stated grant and keep their prior pool (no
  thrash), reported honestly. A failed round never breaks the tick:
  pools keep previous grants (fail-closed) and the failure is observed.
- `arbitration_status()` — explicit live-path report: `arbitrated` or
  `static-fallback` with the reason. The static path is never silent.
- `tick()` runs the arbitration round as step 0 of every cycle.

**`runtime/core/executive/executive.py`** (additive, 4 lines): after the static
initial pool registration, the executive hands its substrate to the
controller's bind inlet (guarded `getattr` for duck-typed controllers).
The static 600s/16 registrations remain the initial pools; each tick
overwrites them with arbitrated grants. PLOOP-7's findings inlet untouched;
PLOOP-1/PLOOP-3's run-controller additions preserved (verified by marker
grep + full suites).

## Proof (real counts, on disk)

`proofs/ploop10_adoption_2026-09-28.py` + `.log` — fresh processes, real
`SwarmEngine`, real `GapRegistry` rows, real substrate, real
`ExecutiveController`. **46/46 green, EXIT 0.**

- T1 (8): bind measures 3600.0s/96 from the real pools; epoch length follows
  the real cadence interval; unbound status names static-fallback explicitly.
- T2 (17): contention on real state — 10 genuinely registered open gaps +
  2 real generalization MCs (600s reserved). Acquisition demand measured
  6000s (not constant); grant 1800.0s (≠ static 600); total stated grants
  2400s ≤ 3600s capacity; generalization keeps its full reserved 600s;
  pools really rewritten; a 2000s spawn against the 1800s grant really
  refused (`admission_exhausted`, `total_refused` = 1); a real `tick()`
  carries the round in-cadence and advances the epoch 1 → 2.
- T4 (3): refused acquisition demand recorded exactly 4200.0s; headroom
  1200.0s; zero-refused for the fully granted loop.
- T3 (4): unbound tick names static-fallback with its reason in the cycle
  summary; the tick still completes its real work; status agrees.
- T5 (5): bind with a missing loop pool raises `KeyError` naming
  `distillation`; a really corrupted gap db (`DatabaseError` at measurement)
  is fail-closed — the tick completes and records `mode: error`.
- T6 (5): constructing the real `ExecutiveController` auto-binds the shared
  substrate (same object) to the controller's arbitration path; capacity
  re-measured 3600s/96 from the executive's real initial pools.

Regression: `tests/contracts/test_resource_arbitration.py` 7/7 green;
`tests.contracts.test_acceptance_loop` + `tests.backend.test_run_control`
21/21 green (the PLOOP-3 suites — the tick hook disturbs nothing).

## Mid-run incidents / repairs

1. Proof math error (mine, not the tree): seeded two 600s MCs against a 600s
   pool — the second spawn was really refused. Corrected to 300s × 2 and
   re-derived expectations; the refusal itself confirmed enforcement works.
2. `_arbitrate_resources` crashed with `KeyError: 'run'`: the arbitrator
   omits zero-demand loops from its grants dict (landed PLOOP-6 behavior —
   not redesigned). Repaired by reporting stated-grant vs carried-pool
   separately and honestly: zero demand → no stated grant, pool unchanged,
   refused = 0. This also surfaced the design's fail-safe property: a loop
   that will need to spawn keeps its last grant instead of being zeroed.

## Exact next boundary

**Per-loop demand fidelity.** Five loops state reserved-only demand because
they have no measurable pending microcontroller-work signal *today*. The
moment distillation's `spawn_microcontroller` gains callers, or
generalization gains a steady backlog queue, or execution grows an MC repair
path, each needs its own measured pending signal like acquisition's open
gaps — otherwise their bursts are grant-throttled to whatever the last
epoch left them. The measurement points are marked in `_measure_demands`;
adding a signal is a small, honest extension, not a redesign.

## What remains unproven

- Multi-epoch behavior under a real long-running cadence (grants
  shrinking/growing across ticks as gaps open and close) — proven for
  single rounds and two consecutive ticks, not for hours of turning.
- The distillation controller's defensive `_ensure_loop_registered`
  (3600s/16) still exists on its own path (out of this mission's file
  ownership); it never resets an existing pool, so on the shared substrate
  the arbitrator's grants win — but a foreign substrate would start from
  those constants.
- Minimums are 0 by policy choice (demand-driven, starvation visible via
  refusals). Whether James wants anti-starvation minimums > 0 is his call.
- Capacity is measured from the initial pools (the old static envelope);
  making it independently tunable (e.g. RunConfig fields) is a follow-up,
  not a defect.
