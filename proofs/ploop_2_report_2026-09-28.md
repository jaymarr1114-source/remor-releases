# PLOOP-2 Mission Report — Loop transition/handoff contracts

Date: 2026-09-28/29 (UTC). Worktree: `~/workspace/ploop-2-work`, branch `ploop-2-handoff`, base `6d412ce`.
Status: COMPLETE — full battery 49/49 green, EXIT:0, committed.

## Objective (from dispatch)
Define and implement contractual loop-to-loop handoffs: every handoff carries state, evidence,
termination/`LoopOutcome`, and resource accounting; producing and receiving sides honor explicit
interface shapes; transitions are never ad hoc. Proof: drive ≥2 real loop controllers through the
contract (handoff produced → accepted → receiving loop continues), provoke a contract violation and
show it failing loudly, cover all six Primary loop controllers.

## What was built
New module `runtime/core/executive/handoff.py` (~600 lines) + additive exports in
`runtime/core/executive/__init__.py`. No existing file's behavior changed; no loop machinery touched.

1. **Terminal-state taxonomy** (`classify_terminal`): `converged | exhausted | refused | failed |
   open | absent | candidate`, classified FROM each loop's real result type —
   tick-summary dict (run), `DispatchResult` (acquisition), `QuarantineDiagnosis` (execution),
   acceptance record state (acceptance: COMPLETED is a candidate, never terminal — Q8's rule
   enforced), `DistillationResult` (distillation/generalization). Unknown loops and
   unrecognizable shapes raise `HandoffRefused`, never guessed.
2. **`LoopHandoff` record**: `handoff_id, from_loop, to_loop, terminal_state, boundary_kind,
   triggering_boundary_id, outcome_detail (≤500 chars), evidence (real records), evidence_refs
   (inspectable id map), resource_delta {spawned, retired, refused} (measured from substrate
   LoopViews, required), chain_depth, produced_at, produced_by (contract version)`.
   `validate()` fails loud on: anonymous handoffs, self-handoffs, unknown terminal states,
   missing lineage, empty evidence, unmeasured resources, chain depth > MAX_HANDOFF_DEPTH (8),
   version skew.
3. **Declared route table `HANDOFF_ROUTES`**: every (loop, terminal_state) pair — 6×7=42 — has a
   declared answer. Three non-terminal routes, each with an honest evidence builder:
   - (run, converged) → `execution_failure`: tick's quarantine sweep surfaced a STILL-quarantined
     capability (reads the real `attempt_outcome`; readmitted capabilities do not resurface).
   - (run, converged) → `acquisition_gap`: tick's gap queue surfaced an open gap; the REAL
     `GapRecord` is fetched via context `gap_fetcher`, never reconstructed.
   - (distillation, converged) → `novel_task`: requires explicit `novel_goal` + `novel_examples`
     in context; without them the builder raises LOUDLY (novelty never auto-generated).
   - Everything else is a DECLARED terminal (`_terminal(reason)` with the policy reason in the
     comment): budget-exhausted ticks (run controller's own backoff), error ticks, open/refused
     gaps (no auto-retry, no spin), execution outcomes (repaired capabilities re-enter via the
     normal completion path with real attempt evidence — manufacturing Attempt+AuthReport would
     be fabrication), acceptance candidates (verdict is James's; never auto-advanced),
     distillation named failures, generalization outcomes (trust path lives outside the six loops).
   - Deliberately NOT declared (review findings, not oversights): no run→distillation (the tick's
     sweep IS distillation work, already consumed — resurfacing would re-distill); no
     acquisition→distillation (no real route carries a live DeltaRecord in closing_evidence —
     they are plain dicts; the technique route distills inline while closing).
4. **`produce_handoff`**: classify → table lookup → ordered builders (None = route doesn't apply,
   try next; HandoffRefused = route applies but evidence can't honestly be built → LOUD) →
   measured resource accounting (views required) → validated handoff, or None for declared
   terminals (a declared answer, never a silent drop).
5. **`accept_handoff`**: validates the handoff, verifies `boundary_kind` names a route declared
   for (from_loop, terminal_state), verifies `to_loop` owns that boundary kind per the
   executive's `BOUNDARY_OWNERSHIP`, enforces chain depth, then constructs the
   `BoundaryPresentation` — which passes the EXISTING anti-fabrication `validate()` gate
   (defense in depth: a tampered handoff dies at the contract level with `HandoffRefused`;
   fabricated evidence would die again at the boundary gate).
6. **`transition`**: produce → accept → `executive.enter` — the full contractual path in one call.

## Proof
`proofs/ploop_2_handoff_proof_2026-09-28.py` — fresh process, temp dirs, real SwarmEngine,
RunController, GapRegistry, AcceptanceLoop, ExecutiveController.
- Light battery (`ploop_2_handoff_light_2026-09-28.log`, SKIP_T2=1, run while a sibling heavy
  battery held the lane): **42/42 green, EXIT:0**.
- Full battery (`ploop_2_handoff_full_2026-09-28.log`, clean uncontended lane after the sibling
  finished): **49/49 green, EXIT:0**.
- H1: classification of real result types for all six loops (14 checks) + unknown-loop refusal.
- H2: all 42 (loop, terminal) pairs declared; six loops covered.
- T1: run→acquisition end-to-end on real machinery — real tick surfaces a real open gap →
  handoff with real GapRecord evidence, measured resource accounting, chain depth 1, lineage →
  accepted → acquisition really entered via executive.enter → gap still OPEN (nothing faked).
  Plus `transition()` one-call path on a fresh gap.
- T2: distillation→generalization end-to-end — real DeltaRecord distilled (real success,
  heldout 2/2) → handoff with explicit novel spec (+ real capability_id) → novel_task boundary
  validated → generalization really entered → real DistillationResult out: success=False with the
  NAMED refusal "generalize refused: new goal names no single target constant" — the novel task
  (running products) is genuinely outside the bound-constant re-parameterization envelope, and
  the loop said so instead of faking success. The contract's job (produce → accept → receiving
  loop runs) is proven either way; the verdict is the loop's business.
- T3: acquisition OPEN → produce returns None; acceptance CANDIDATE → produce returns None.
- V1: tampered to_loop → HandoffRefused naming the ownership mismatch.
- V2: empty evidence / unmeasured resources / self-handoff → HandoffRefused.
- V3: chain depth 9 → refused at produce AND at validate.
- V4: distillation converged without novel spec → HandoffRefused (not silently skipped).

## Review findings fixed during the run (run→observe→diagnose→repair→rerun)
1. V1 exposed that a to_loop-tampered handoff matched a different declared route and died only at
   the downstream boundary gate — the contract now records `boundary_kind` on the handoff and
   verifies (declared route) + (to_loop ownership) at accept time, failing loud at contract level.
2. `_build_run_execution` read a nonexistent `resolved` key — now reads the real `attempt_outcome`
   (only still-quarantined capabilities surface; readmitted ones don't).
3. Removed the speculative run→distillation route (wrong sweep key anyway; the sweep IS
   distillation work, already consumed) and the acquisition→distillation route (no real route
   carries a live DeltaRecord in closing_evidence).
4. The full battery's first run exposed that the generalize inlet needs `capability_id` in the
   distilled ref — the builder now carries the real `capability_id` (+ `delta_observation_id`)
   from the distill result, and refuses loudly if it's missing.

## Exact next boundary
Production wiring: the run tick still dispatches gaps inline (V10-P2 behavior, unchanged) — the
future seam (SEAM_EXECUTIVE.md) has the tick SURFACE boundaries instead of inline-dispatching, at
which point the run→acquisition/execution follow-ons become the live path instead of a
redundant-but-real one. That wiring is a separate mission's explicit boundary; this mission
delivered the contract it will run on.

## What remains unproven
- The execution_failure follow-on path (run→execution) is declared and unit-shaped but was not
  driven end-to-end (no still-quarantined capability with a parseable missing-substrate reason
  was staged — staging one would need a real quarantined capability, available when M5's path
  produces one naturally).
- Cross-transition checkpoint/recovery (the audit's separate open item) is untouched — not
  claimed.
- Heavy-battery behavior under the landing HEAD is re-proven at gate time, not here.

## Residuals / notes for the gate
- The light battery's V4 used a constructed (real-typed) DistillationResult for the refusal path;
  the full battery uses a real distill outcome. Both test the contract, not the loop.
- Production-wiring residual (named, not hidden): surfaced gaps may be re-dispatched by the
  acquisition inlet until the future seam lands — redundant but real.
