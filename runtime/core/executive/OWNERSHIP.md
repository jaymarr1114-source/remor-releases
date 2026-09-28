# Executive ownership map + Q1 loop_driver settlement (RUN-EXEC-1)

## The six loops and their boundary classes

The executive routes a validated `BoundaryPresentation` to its owning loop
per this declared map (`BOUNDARY_OWNERSHIP` in `executive.py`). The map is
a declared architectural fact -- James's six-loop hierarchy: each loop
manages convergence for its boundary class -- not a learned guess. The
*recognition* of what boundary exists is performed by the observing
machinery that emitted the validated structured record; the executive only
answers "which loop owns this boundary class".

| boundary kind         | owning loop   | loop entry (real machinery)                        |
|-----------------------|---------------|----------------------------------------------------|
| `run_wake`            | run           | `RunController.tick()` (V10-P2)                    |
| `acquisition_gap`     | acquisition   | `GapRegistry.dispatch(gap_id)` (M7)                |
| `execution_failure`   | execution     | `diagnose_quarantine(engine, capability_id)` (M5)  |
| `completion_candidate`| acceptance    | `AcceptanceLoop.present(...)` (Q8)                 |
| `technique_delta`     | distillation  | `DistillationLoop.distill(delta)` (M2/V10-P4)      |
| `novel_task`          | generalization| `generalize_for_task(...)` (V10-P5)                |

A loop with no real machinery registers ABSENT; the executive names the
absence (`RoutingDecision.status == "absent"`) and never enters a stub. A
boundary whose evidence fails validation, or whose kind no loop owns, is
refused fail-closed as UNOWNED -- the executive never guesses.

Selection honesty classification (standing): the executive performs
**evidence-driven routing**. Open-ended comprehension of novel boundary
kinds the machinery has never emitted a record for is UNPROVEN -- that is
the brain's future work, not the executive's.

## Q1 `loop_driver.py` ownership settlement -- EXPLICITLY SUBORDINATE

**Decision:** Q1's `CognitionLoop` (`runtime/acquisition/loop_driver.py`)
is **explicitly subordinated** to the Acquisition loop, via the executive's
Acquisition registration. It is NOT retired and NOT left ambiguous.

**Rationale:**
- The driver is real machinery (continuous acquisition: ingest scan,
  distill scan, quarantine sweep, observe) and dormant: nothing
  instantiates `CognitionLoop` today.
- The RunController deliberately does NOT drive `CognitionLoop.cycle()`
  (V10-P2's no-double-drive discipline: Q1's cycle targets pre-V10
  M1-schema deltas; the V10 generation is driven by V10-P4's sweep, which
  the RunController clocks). Driving both would double-drive
  distillation -- the duplication James's order forbids.
- Under the three-level hierarchy, continuous cognition belongs to the
  **Acquisition loop**, hosted through the microcontroller substrate
  (`loop="acquisition"`), not to the run controller's cadence.

**Implementation:**
- `AcquisitionLoopInlet.drive_cognition(cognition_loop, budget_s)` is the
  ONLY authorized drive path for `CognitionLoop.cycle()` in the runtime.
  The cycle is hosted in a microcontroller (`spawn(loop="acquisition",
  purpose="cognition_cycle", budget_s=...)`) and retired afterwards; the
  substrate ledger records the hosting.
- The Acquisition registration records `cognition_driver =
  "swarm_engine.acquisition.loop_driver.CognitionLoop"` and the drive
  contract (`Q1_DRIVE_CONTRACT` in `loops.py`).
- `runtime/acquisition/loop_driver.py` itself is UNCHANGED (Q1 owns it);
  `runtime/core/run_controller.py` is UNCHANGED (still never drives it).
- Until the full Acquisition loop controller exists (ACQ-CTRL-1's
  mission), nothing drives the cycle on a schedule. The executive records
  that state honestly -- there is no phantom driving.

**Proof obligation (RUN-EXEC-1 battery):** one real `CognitionLoop.cycle()`
driven through `drive_cognition` against a real engine (scratch DB),
returning a real `CycleReport` with the substrate ledger showing the
hosting; plus a structural check that no other production call path to
`CognitionLoop.cycle()` exists.
