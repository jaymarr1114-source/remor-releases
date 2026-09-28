# Microcontroller substrate — FROZEN interface specification

**Version stamp:** `microcontroller-interface/v1`
**Module:** `runtime/core/microcontroller/` (`__init__.py`, `substrate.py`)
**Frozen:** 2026-09-28 (RUN-MICRO-1 accept)
**Change rule:** breaking changes require James's explicit decision. Additive,
backwards-compatible extensions (new refusal codes, new optional kwargs) may
land with a minor version bump recorded here; anything that changes the
meaning of an existing call, field, or refusal is breaking.

The other five loop-controller tracks (Acquisition, Execution+Repair,
Acceptance, Distillation, Generalization) code against this document.

## Concepts

- **Microcontroller**: one bounded unit of loop-internal work. Declares its
  `purpose` (non-empty string), its `parent_id` (or None for loop-rooted),
  and its `loop` at spawn. Ids are opaque (`mc-<12 hex>`).
- **Loop**: one of `run`, `acquisition`, `execution`, `acceptance`,
  `distillation`, `generalization`. Microcontrollers are **local to their
  loop** — cross-loop spawn and cross-loop retire are refused.
- **Recursion**: a microcontroller may spawn children (MC-1 → MC-2 → …),
  bounded by `max_depth` (default 8) and each parent's `max_children`
  (default 8). On resolution the stack unwinds bottom-up; a parent retired
  with active children cascade-retires them first (`retired_cascade`).
- **Budgets**: every spawn reserves `budget_s` from its loop's admission pool.
  `charge(mc_id, seconds)` deducts cooperatively; overrun marks the
  microcontroller EXHAUSTED (children cascade-retired, reservation held until
  the owner retires it or the loop resolves). Budgets never grant fresh time
  on overrun.
- **Admission**: per loop — a budget pool plus a concurrency cap. Spawn is
  refused with `admission_exhausted` when the pool cannot cover the ask.
  Subordinate to the RunController's run-level admission, never a second one.

## Calls

```
register_loop(loop, *, budget_s, max_concurrent=64)
set_cognition_provider(provider)          # CognitionProvider protocol
spawn(loop, purpose, *, parent_id=None, budget_s, max_children=8)
    -> SpawnResult(ok, mc | refusal)
charge(mc_id, seconds)                    # -> (exhausted: bool, state: str)
cognize(mc_id, prompt, context={})         # -> CognitionResult(ok, text | error)
retire(mc_id, outcome="resolved", *, loop=None)
    -> Microcontroller | Refusal
resolve_loop(loop)                        # -> LoopView (cascade-retires stragglers)
loop_view(loop)                           # -> LoopView  (executive-facing)
export_state() / import_state(snapshot)   # JSON-serializable checkpoint seam
retired_ledger()                          # retired records, inspectable
get(mc_id)                                # active record or None
```

## Refusal codes (values, never exceptions)

`depth_cap`, `admission_exhausted`, `concurrency_cap`, `cross_loop`,
`unknown_parent`, `unknown_microcontroller`, `already_retired`,
`invalid_purpose`, `invalid_budget`, `loop_unregistered`, `mc_not_active`,
`cognition_unavailable`.

## Fabricated-retire detection

- Retiring an unknown id → refused (`unknown_microcontroller`); re-retiring
  a ledgered id → refused (`already_retired`).
- Retiring with a `loop=` claim that does not match the record → refused
  (`cross_loop`).
- Claiming `outcome="resolved"` on an EXHAUSTED microcontroller → detected:
  flag `fabricated_resolve_on_exhausted` is written to the record, the
  outcome is downgraded to `exhausted`, and the ledger shows the truth.

## Encapsulation (load-bearing)

`LoopView` is the **only** type that may cross above the loop level. It
carries `loop, state, active_count, total_spawned, total_retired,
total_refused` — no microcontroller ids, purposes, or internals. Enforced by
construction: `loop_view()` builds it from aggregates only.

## Cognition inlet (load-bearing)

"Reasoning models provide cognition wherever a microcontroller requires it"
— external reasoning is a utility, not a layer. `CognitionProvider` is a
protocol: `request_cognition(*, mc_id, prompt, context) -> CognitionResult`.
The default is `NullCognitionProvider`, which refuses honestly with
`cognition_unavailable`. The substrate never constructs or selects a model.

## Run-loop integration seam (declared, not wired)

`RunController.dispatch_gap(gap_id)` may: `spawn(loop="run",
purpose=f"dispatch:{gap_id}", budget_s=<that gap's guard>)` at dispatch wake
and `retire(mc_id, outcome)` at that gap's checkpoint. The controller's
checkpoint path may persist `export_state()` and restore via `import_state()`
(version-checked). Wiring belongs to a future mission — this interface only
declares the seam. See `SEAM_RUN_LOOP.md`.
