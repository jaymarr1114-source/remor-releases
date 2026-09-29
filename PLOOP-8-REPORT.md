# PLOOP-8 Mission Report — Checkpoint/recovery across loop transitions

**Status: COMPLETE.** Branch `ploop-8-checkpoint` in `~/workspace/ploop-8-work`
(base `cfe7915`), committed as `f0d3ccd` — 3 files changed
in runtime, 3 proof files, no existing behavior changed.

## What was built

**New module `runtime/core/executive/checkpoint.py`** (PLOOP-8 owns it):
- `TransitionCheckpointStore`: sqlite-backed durable store. Every operation
  opens a fresh connection — a row written by one process is readable by
  another with no shared in-memory state. Row lifecycle
  `saved -> verified -> consumed`, single-use: `mark_consumed` on a
  consumed row raises loudly (replay refused). `save` on a duplicate
  handoff_id raises (one checkpoint per handoff, never silently
  overwritten).
- `extract_from_state(from_loop, outcome, context)`: per-loop resumable-state
  snapshot read from REAL records — run reads the persisted `rc_cycles`
  row on a fresh connection (deep) or honestly marks itself "shallow"
  when no controller is in context; acquisition/distillation/execution/
  acceptance read their real result types. Unknown loops raise (never
  guessed); non-JSON-serializable content raises (the store holds plain
  data, never live objects).
- `verify_checkpoint_integrity`: sha256 tamper-evidence over the row's
  content fields, recomputed on every read.
- `verify_against_live`: the anti-stale core. For `acquisition_gap` the gap
  is re-fetched through the real registry and must still be open; the
  deep run extraction re-reads the `rc_cycles` row and requires it
  byte-identical (history rewritten -> refuse). A missing, tampered,
  drifted, or consumed checkpoint raises `CheckpointError` naming the
  exact violation.

**Checkpoint hooks in `runtime/core/executive/handoff.py`** (additive only;
the PLOOP-2 contract is untouched when no store is engaged):
- `LoopHandoff` gains optional `checkpoint_id` (None = not engaged) and
  in-memory `verified_checkpoint`; both validated additively.
- `produce_handoff`: when context carries `checkpoint_store` (a real
  `TransitionCheckpointStore`) or `checkpoint_db_path`, the from-loop's
  state is checkpointed at produce time and the id attached. Without
  either, behavior is byte-for-byte the PLOOP-2 contract.
- `accept_handoff`: when the handoff carries a checkpoint, the row is
  re-read on a fresh connection, field-matched against the handoff,
  integrity-checked, verified against the live world, and marked
  verified. Any failure -> `HandoffRefused` loudly. Never a silent
  cold start.
- `transition()`: after the receiving loop is entered, the checkpoint is
  marked consumed (single-use; a second transition on the same handoff
  is replay and is refused).
- `rehydrate_handoff(checkpoint_row, evidence)`: cross-process resume —
  the checkpoint row is the durable source of truth; the caller
  re-fetches the REAL records the row's `evidence_refs` name and the
  rebuilt handoff is validated like any other.
- `runtime/core/executive/__init__.py`: additive exports only.

## Proof (real counts, on disk)
- `proofs/ploop8_checkpoint_proof_2026-09-28.py` — fresh process, real
  SwarmEngine/RunController/GapRegistry/AcceptanceLoop/ExecutiveController.
- **21/21 checks green**, including:
  - C1: no store -> contract behaves exactly as PLOOP-2 (49/49 proof
    re-run green on this branch, unchanged).
  - C2: produce with store -> durable row, deep run from_state from the
    real `rc_cycles` row, tamper hash verifies, status saved.
  - C3 (T): **cross-process resume** — process A ticks + produces and
    exits; process B (fresh) loads the checkpoint from disk, rehydrates
    the handoff from the row + honestly re-fetched GapRecord (B never
    calls produce), accepts it verified against the live world, enters
    acquisition through the executive: the SAME gap_id dispatched with
    unbroken `triggering_boundary_id` lineage, checkpoint consumed.
  - C4: in-process full `transition()` consumes via the real code path.
  - C5: replay of a consumed checkpoint -> `HandoffRefused` ("already
    consumed ... replay is refused").
  - C6: gap closed out-of-band between produce and accept ->
    `HandoffRefused` naming the stale checkpoint (never silently
    resumed).
  - C7: checkpoint_id with no store at accept -> `HandoffRefused`.
  - C8: tampered row (from_state rewritten via raw SQL) ->
    `HandoffRefused` naming the integrity failure.

## Mid-run incident (repaired)
First proof run failed with `AttributeError: 'RunController' object has
no attribute 'record_cycle'` — `tick()` does not persist its cycle row;
`run()` does via `rc._checkpoint.record_cycle`. The proof persists
through the controller's real `ControllerCheckpoint.record_cycle`
(the same method `run()` calls per cycle). Fixed in the proof drivers;
21/21 green after. No runtime code was wrong — the proof assumed a
method that never existed.

## Exact next boundary
**Production wiring.** The checkpoint hooks engage only when a store is
in context. No production caller passes one yet: `transition()` without
a store behaves as PLOOP-2 (durable, correct, but un-checkpointed).
The next boundary is threading a `TransitionCheckpointStore` through
the production transition path — the SEAM_EXECUTIVE.md future seam,
where the tick surfaces boundaries instead of inline-dispatching and
the executive drives `transition()` with a store. Until that seam
lands, checkpoints are proven in the harness, not in the live cadence.

## What remains unproven
- The live cadence does not yet engage checkpointing (see above) —
  proven in the proof harness only.
- `run -> execution_failure` and `distillation -> novel_task` save
  checkpoints (extractors exist) but their live re-verification is
  id/evidence-ref matching only; the full live re-read exists for the
  `acquisition_gap` route.
- Cross-process resume proven for `run -> acquisition_gap` only.
- No retention/expiry policy on `transition_checkpoints` rows —
  consumed rows accumulate. Operational, not correctness.
- The shallow run extraction path (no controller in context) is
  implemented but has no dedicated proof check.

## Notes for the gate
- No verification claims in the commit message (standing rule); proof
  counts live in the proof output and this report.
- Reuse check: built on the run controller's real `ControllerCheckpoint`
  / `rc_cycles` persistence and the registry's real `get`/`_save` — no
  second checkpoint store, no new persistence machinery invented.
- PLOOP-9 (terminal_routing.py) and PLOOP-10 (run_controller/executive
  cadence) file ownership respected: this mission touched
  `checkpoint.py` (new), `handoff.py` (hooks only), `__init__.py`
  (exports only).
