# Run-loop integration seam (declared, not wired)

The Run loop's convergence process (Run Controller track charter):
**wake → dispatch gaps → checkpoint → sleep**, with the gap-iteration loop
inside dispatch (user gaps first).

## Where the microcontroller substrate plugs in

1. **Dispatch wake.** When the RunController wakes a gap for dispatch, it may
   host that dispatch as a loop-rooted microcontroller:
   `spawn(loop="run", purpose=f"dispatch:{gap_id}", budget_s=<gap_budget_s>)`.
   The gap's existing wall-clock guard (`RunConfig.gap_budget_s`) becomes the
   microcontroller's budget — one discipline, not two.
2. **During dispatch.** The dispatch microcontroller may spawn children for
   sub-tasks (diagnose → repair → verify legs), each with its own declared
   purpose and budget slice, bounded by `max_depth` and the loop admission
   pool. A runaway leg is capped by the substrate, not by new controller code.
3. **Checkpoint.** At the gap's checkpoint the controller calls
   `retire(mc_id, outcome)` and may persist `export_state()` through its
   existing checkpoint/recovery path; `import_state()` is version-checked
   against `microcontroller-interface/v1`.
4. **Sleep.** `resolve_loop("run")` at run end cascade-retires stragglers;
   `loop_view("run")` is what any executive-facing status reports.

## What this mission does NOT do

- It does not edit `runtime/core/run_controller.py`, the scheduler, or
  checkpoint code (RUN-P2-1's ownership).
- It does not change run-controller cadence behavior.
- It does not wire the seam. Wiring is a future mission's work, against the
  frozen interface.

## Q1 loop_driver relation (recorded for RUN-EXEC-1)

Q1's `CognitionLoop` (`runtime/acquisition/loop_driver.py`) is the continuous
acquisition driver. When the Acquisition loop controller exists, it will host
its cognition work through this substrate (`loop="acquisition"`). Whether
`loop_driver.py` is retained, retired, or subordinated is RUN-EXEC-1's
settlement — this substrate is agnostic: any driver code can call
`spawn()`/`retire()` against a registered loop.
