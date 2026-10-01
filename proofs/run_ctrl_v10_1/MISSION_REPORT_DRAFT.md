# RUN-CTRL-V10-1 — Mission Report (DRAFT)

## Mandate

Make the REMOR Core Run Controller's ownership of cadence/execution
control real. Report-don't-land: only Felix in main chat gates and lands.

## What was done

### 1. Fresh structural re-map (mandate 1) — DONE

Mapped every `cycle()`/`tick()`/scheduler entry point and caller on the
worktree as it was:
- `cycle()` defs: only `runtime/acquisition/loop_driver.py:136`.
- `tick()` defs: `runtime/core/run_controller.py` (V10-P2 Controller) and
  `runtime/curiosity/run_controller/controller.py` (Curiosity executive's
  own — different track, not touched).
- `.cycle()` call sites: only `AcquisitionLoopInlet.drive_cognition`
  (documented "ONLY authorized caller") — which has ZERO production
  callers. Q1's cycle was UNDRIVEN, an orphaned cadence path.
- `.tick()` call sites: the Controller's own `run()` loop;
  `RunLoopInlet.enter` (executive inlet, not a cadence driver).
- `DumbScheduler`: no `fire()`/`cancel()` — just `__init__(interval_s)`
  and `wait_until_next_tick(stop_event)`. Pure blocking sleep.

### 2. Inventory before building (mandate 2) — DONE

- Double-drive mechanism proven: `ingest.py` writes ONE record with both
  `kind="technique_delta"` (V10-P4's marker) and `source="technique_delta"`
  (Q1's marker); each driver's consumption marker invisible to the other.
- Q4's `run_quarantine_sweep`: bounded, per-capability isolation, audit
  trail; subprocess mode fail-closes on live engines; `caller=None` ->
  engine oracle (authorized).
- V10-P3 `delta_capture` writes `kind="technique_delta"` but
  `source="delta-capture"` — invisible to Q1's old scan (orphaned).
- Retry layers: Controller-owned gap backoff (`rc_gap_backoff`,
  exponential 60s->1800s, checkpoint-persisted); "bound-aware retry" is a
  GapRegistry dispatch route, not a retry loop; no retry loops in sweeps,
  Q7, acceptance.

### 3. One cadence (mandate 3) — PROVEN

Decision on evidence: `tick()` remains the ONE cadence; Q1's
`CognitionLoop.cycle()` is the acquisition leg the Controller drives
inside its tick (the seed, per James's directive). The V10-P4 sweep step
is retired from the tick (its charter adapter absorbed into Q1's distill
leg; its consumption marker unified). Runners-up: retiring `tick()` for
Q1's `cycle()` (would reimplement arbitration/gap/quarantine/acceptance
inside Q1's file, violating the RUN-EXEC-1 settlement for no gain);
leaving Q1 undriven (leaves the seed dormant).

Adversarial: drove the loop outside the Controller (separate
CognitionLoop.cycle on a seeded delta), then ran the Controller's leg —
the leg refused to re-drive it (12/12 causal_01). The unified consumption
record IS the refusal mechanism; no second cadence can double-drive.

### 4. Scheduler subordinate (mandate 4) — PROVEN

Causal-02 (8/8): scheduler `__dict__` is only `{"_interval"}`; blocks
without self-firing; reports stop promptly; adversarial
`cadence_interval_s=0.0` + `run_budget_s=3.0` -> `budget_exhausted=True`
in 3.0s, 41 cycles. No cognition route passes through the scheduler.

### 5. Quarantine sweep repair (mandate 5) — PROVEN

`_quarantine_sweep` now routes through Q4's `run_quarantine_sweep`
(engine, `worker_mode="in_process"`, `caller=_engine_caller_context`,
per-capability and total bounds from the remaining cycle budget,
`reason="run_controller cadence sweep"`, `audit=True`). Causal-03 (3/3):
the real `restore_everywhere` gate refuses bare `"run_controller"`
(`AuthorizationError`) and authorizes the engine's caller context. The
audit row lands in `sweep_runs` with the Controller's reason. No new
`repair_controller` under `pylib/` was needed — Q4's sweep is the one
owner, no duplication.

### 6. Live control (mandate 6) — PROVEN

functional_light (18/18):
- tick() end-to-end: all three legs, zero step errors.
- Repaired quarantine sweep: Q4 report shape, audit row recorded.
- Pause/resume/stop on a running Controller (checkpoint cycle counts).
- Kill -9 across genuine cross-process death: `run_resumed`
  observation, prior_cycles carried forward (7 -> 15).
- Budget exhaustion: `run_budget_s=0` refuses before any cycle.
- Per-cycle acceptance (`_accept_cycle`) recorded on every run cycle.
- Q8 `present()` as inlet: synchronous CANDIDATE transition, no thread
  started, duplicate refused loudly — it is an inlet, not a loop.

### 7. Sustained unprompted run (mandate 7) — PROVEN

≥1 hour under the Controller processing a SEEDED gap queue (3 gaps: one
user gap for user-gaps-first ordering, two unrouted gaps that stay
honestly open), within budgets, every action an observation.

Result (`sustained/sustained_report.json`, pin 5cbe778, clean tree):
- elapsed 3743s (62.4 min), 148 cycles, 0 cycles with errors
- per-cycle acceptance on 148/148 cycles
- 18 gap dispatches (seeded queue processed, gaps honestly open)
- ended cleanly on `budget_exhausted=True` (no crash, no stop)
- `success: true`

Mechanism evidence only — never a product claim.

### 8. No retry-layer shadowing (mandate 8) — PROVEN (by inventory)

Map (mandate 2) holds post-change: the only retry is the Controller's
gap backoff; no retry loops were added in the distill leg, quarantine
sweep, Q7 trigger, or acceptance. Distinct layers stay distinct.

### 9. Gate-ready battery (mandate 9) — SHIPPED AND RUN CLEAN

`proofs/run_ctrl_v10_1/gate_run.sh`: full battery in fresh sequential
processes (causal_01, causal_02, causal_03, functional_light,
tests/backend/test_run_control.py). One command, exit 0 on green,
pass/fail visible in stdout.

Clean end-to-end run 2026-10-01 ~17:00 UTC: **GATE: PASS (5/5)** —
causal_01 12/12, causal_02 8/8, causal_03 3/3, functional_light 18/18,
regression 13/13 (174s).

### 10. Report, do not land (mandate 10) — DONE

Implementation committed on `run-ctrl-v10-1-work` (a457e60 + proof
commit); nothing landed to canonical/main. This report is the handoff;
Felix gates in main chat.

## Exact next boundary

Felix's independent gate re-run of `gate_run.sh` in main chat, then
James's landing call. After landing: James's hardware confirmation —
nothing is described as working until he confirms it on his hardware.

## What remains unproven

- The Controller driving real gaps/deltas over days (sustained run is
  unprompted but the gaps are seeded test shapes).
- Device-acceptance path on James's phone.
- Coordination with ACQ-CTRL-1 when the full Acquisition loop controller
  exists (it will need to coordinate with the Controller's ownership of
  the Q1 cycle).
- The `drive_cognition` executive inlet path (retained, not the scheduled
  path; unexercised in this mission).

## Incidents

None. One test-authoring correction (not a product defect): the
checkpoint's `run_id` meta is re-minted each boot by design (tracks the
live owner); the resume signal is the `run_resumed` observation + carried
prior_cycles, which the test now asserts.
