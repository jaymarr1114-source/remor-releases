# Executive integration seam (declared, not wired)

The ExecutiveController is a real runtime entity: constructed with the
live engine and the existing machinery, it routes validated boundaries to
their owning loops and enters them through the real entry points. What is
NOT yet wired is who calls the executive in production.

## Construction site (declared)

The executive is constructed alongside the RunController, in the process
that boots REMOR's runtime, with the live engine injected:

```python
from swarm_engine.core.executive import ExecutiveController
from swarm_engine.acquisition.gaps import GapRegistry
from swarm_engine.services.acceptance import AcceptanceLoop, AcceptanceStore

executive = ExecutiveController(
    engine=engine,                       # live engine, injected (single-owner)
    run_controller=run_controller,       # the V10-P2 RunController
    gap_registry=GapRegistry(engine, db_path=...),   # M7
    acceptance_loop=AcceptanceLoop(       # Q8
        AcceptanceStore(db_path=...), engine.intellect.epistemic,
        engine=engine),
)
```

The executive never constructs an engine (the single-owner lock forbids a
second live engine per process) and never owns cadence (the RunController
does -- James's 2026-09-27 decision, unchanged).

## The wake -> executive seam (future wiring)

Today the RunController's tick drives the gap queue, distill sweep, and
quarantine sweep directly (V10-P2 behavior, unchanged). The future seam,
for the mission that wires it:

1. **Boundary surfacing.** When run-level machinery observes a boundary
   that belongs to another loop (e.g. the quarantine sweep surfaces a
   quarantined capability, the distill sweep surfaces a distillable
   delta), it presents a `BoundaryPresentation` with the real record as
   evidence, instead of handling it inline.
2. **Selection.** `executive.route(boundary)` selects the owning loop.
3. **Entry.** `executive.enter(boundary)` enters the loop's real entry
   point. The LoopView returned is the executive-facing status
   ("Acquisition is active").

Nothing in this seam changes run-controller cadence ownership, the
scheduler's subordinate role, or checkpoint/recovery.

## What this mission does NOT do

- It does not edit `runtime/core/run_controller.py`, the scheduler,
  checkpoint code, or any loop machinery (all consumed, never modified).
- It does not touch `runtime/core/microcontroller/` (frozen
  `microcontroller-interface/v1`; additive consumption only).
- It does not schedule Q1's `CognitionLoop` on a cadence -- the drive
  contract is implemented and proven once; scheduled driving awaits
  ACQ-CTRL-1.
- It does not build the full loop controllers (their tracks' missions);
  the inlets are the executive's entries, not the controllers.
