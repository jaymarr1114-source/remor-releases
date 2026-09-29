# PLOOP-12 REPORT — acceptance through the live-path seam

Date: 2026-09-29. Mission coordinator: PLOOP-12 (Backend Runtime track).
HEAD at execution: `7494721` (PLOOP-11 merge, Felix-gated).
Worktree: `~/workspace/remor_convergence/worktrees/ploop12` (branch
`ploop-12-acceptance-seam`). No canonical landing. No verification claim
in the commit. No delivery ZIP.

Objective (dispatch): present a real completion candidate (Attempt +
passing AuthReport through Q8's `present()` gate) through
`LivePath.surface()`/`drive_cycle()`, routing its CANDIDATE terminal to
the real verdict consumer — the only loop inlet never driven through the
integrated seam (PLOOP-11 §4 exact next boundary).

## 1. Structural re-map (fresh, at 7494721, before any wiring)

- Seam: `runtime/core/executive/live_path.py` — `LivePath.surface()`
  (produce → accept → enter, declared terminals → router),
  `drive_cycle()`, `build_live_path()`. Unchanged by this mission.
- Acceptance inlet: `runtime/core/executive/loops.py`,
  `AcceptanceLoopInlet.enter()` — calls the real
  `AcceptanceLoop.present(run_id, goal, attempt, auth)` (Q8's gate).
- Boundary: `BOUNDARY_COMPLETION_CANDIDATE = "completion_candidate"`,
  owned by `LOOP_ACCEPTANCE`; `BoundaryPresentation.validate()` demands a
  real `Attempt`, a real `AuthReport`, `auth.passed=True`, `run_id`, `goal`
  — a failing auth is refused at the boundary, before any inlet is entered.
- Classifier: `_classify_acceptance` → `TERMINAL_CANDIDATE` for a record
  whose state is CANDIDATE; anything else is refused, never guessed.
- Router: `(LOOP_ACCEPTANCE, TERMINAL_CANDIDATE)` →
  `_route_acceptance_candidate` → consumer
  `acceptance_loop.verdict_awaiting`; re-reads the record from the real
  `AcceptanceStore`, requires state CANDIDATE, never calls
  `record_verdict`.
- Real auth gate: `AcceptanceDriver._auth_gate` (V10-P6) — re-executes the
  claim and held-out args via `eng.composer.execute_sync`, negative
  control must fail closed, counterfactual alternative must diverge.
- Real work source: `induce_arithmetic_from_examples`
  (`runtime/acquisition/atomic_operators.py`) — genuine synthesis from
  training examples (47 candidates tried here), compiled to a real
  composer plan and genuinely executed.

## 2. What was built / repaired (exact files, hunks, why)

**One runtime repair: `runtime/services/acceptance.py`,
`AcceptanceLoop.present()` (+21 lines).** The mission's sole owned runtime
change. No other runtime file touched.

- *The hole (found by the mandated adversarial contrast):* the
  `AcceptanceStore.save()` is `INSERT OR REPLACE`, and `present()` had no
  duplicate guard. Probes proved: (a) a second `present()` for the same
  `run_id` while a CANDIDATE awaited verdict silently overwrote the
  record; (b) worse, a `present()` AFTER `record_verdict(ACCEPTED)`
  silently clobbered the accepted record back to CANDIDATE — the user's
  verdict, ground truth, erasable by a duplicate delivery.
- *The repair:* `present()` now reads the store first and refuses loudly
  (`ValueError`) when a record already exists for the `run_id`, with a
  state-specific reason: CANDIDATE → duplicate refused (verdict awaited);
  ACCEPTED → verdict stands, cannot be clobbered; REJECTED → re-present
  via `present_retry` so the evidence chain stays intact; terminal →
  cannot re-present. This is the correct layer: the Q8 gate is where
  "completed is a candidate state, never terminal" is enforced, and the
  guard is what forces crash-resume through the durable store instead of
  through duplicate presentation (see §3, kill/resume).
- *Blast radius checked:* exactly three production callers of `present()`
  exist (`AcceptanceLoopInlet.enter`, `AcceptanceDriver.present_result`,
  the driver's cycle evaluation) — none legitimately double-presents the
  same `run_id`. Existing proofs present each `run_id` once.

**Proof files (all under `proofs/`):**

- `ploop12_work.py` — shared real-work helper (induce → compile →
  execute → authenticate). Used by the battery and proc A; no duplication.
- `ploop12_acceptance_seam_proof.py` — 20 checks, sections A/B/D.
- `ploop12_proc_a.py` / `ploop12_proc_b.py` — fresh-process kill/resume
  halves (A: real work → auth → enter/present → dies before routing; B:
  fresh process, same db files, re-fetches the durable record, surfaces,
  routes, verifies lineage).
- `ploop12_kill_resume_driver.py` — runs A then B as sequential
  subprocesses, plus guard-precision checks. 7 checks.
- `proofs/logs/ploop12_acceptance_seam_2026-09-29.log` (20/20, exit 0),
  `proofs/logs/ploop12_kill_resume_2026-09-29.log` (7/7, exit 0).

## 3. Per-boundary results (exact vocabulary)

- Real completion candidate produced (genuine synthesis + execution +
  authentication) — **PROVEN**. A1–A6: `induce_arithmetic_from_examples`
  genuinely induced `('add', ('var','x'), ('var','y'))` from 5 training
  examples (47 candidates tried); the compiled composer plan genuinely
  executed (claim 12.0); 3 held-out examples the induction never saw all
  genuinely passed; the negative control (missing arg) genuinely failed
  closed; the counterfactual wrong plan (`add(x,x)`) genuinely diverged
  (10.0 ≠ 12.0); and the gate is not vacuous (corrupted held-out
  expectations → `passed=False`).
- Acceptance through the live-path seam — **PROVEN**. B1–B7: the
  `completion_candidate` boundary validated through the seam's boundary
  contract; entered through `executive.enter()` into the real acceptance
  inlet; the real `present()` ran → CANDIDATE record; `surface()`
  classified `candidate` and took the declared-terminal path; the seam's
  own `TerminalRouter` routed to the real consumer
  `acceptance_loop.verdict_awaiting`; the terminal ledger holds the route
  with evidence refs (`run_id`, `goal`, `state=CANDIDATE`); the stored
  record is still CANDIDATE afterwards (`decided_at=None` — never
  auto-advanced, `record_verdict` never called).
- Kill/resume across the acceptance path — **PROVEN**. KR1–KR4: proc A did
  the real work, authenticated, entered/presented (CANDIDATE persisted to
  the real AcceptanceStore on disk), and died before routing; fresh proc B
  on the same db files honestly re-fetched the durable record (never
  re-presented — the PLOOP-12 guard rightly forbids it), surfaced it
  through the seam, routed to the verdict-awaiting consumer, with lineage
  intact (`presented_at` and `goal` identical across the process
  boundary). KR5–KR7: the guard is precise — a rerun presents a distinct
  new candidate without clobbering; the store holds both; the ledger
  holds the routes.
- Adversarial refusals — **PROVEN**. D1: failing auth refused at the
  boundary gate (`BoundaryRefused`); D2: failing auth refused at
  `present()` (`ValueError`); D3: tampered store state (CANDIDATE→accepted
  row rewrite) refused by the router (`HandoffRefused`); D4:
  never-persisted candidate refused; D5: double-present while CANDIDATE
  refused (the repair); D6: re-present after ACCEPTED refused, verdict
  intact (the repair).
- Acceptance inlet through `drive_cycle()` — **UNPROVEN** (and not the
  objective). `drive_cycle()` starts from `drive_tick()`; the run loop's
  tick does not produce acceptance outcomes (PLOOP-11 §5: the tick still
  dispatches gaps inline), so there is no honest acceptance hop for
  `drive_cycle()` to surface. The seam path exercised here —
  wake → `executive.enter` → inlet → `surface()` → router — is the
  production path for this hop class.

## 4. Exact next boundary

**The tick's inline gap dispatch (PLOOP-11 §5 residual).** The seam drives
every contractual hop class through the production contracts, but the run
loop's tick still dispatches gaps inline (V10-P2 behavior) alongside the
seam — redundant but real. The future seam has the tick *surface*
boundaries through produce → accept *instead of* inline dispatch, which
is also what would give `drive_cycle()` an honest acceptance hop (a
completed run presenting its candidate through the cycle rather than
through a separately-driven boundary). That change touches the Run
Controller's owned driver — cross-loop ownership, needs care.

## 5. What remains unproven

- The tick surfacing instead of inline dispatch (§4 — the exact next
  boundary).
- Trust-path promotion downstream of generalization (ReviewBoard +
  verdict-bound promotion): declared outside the six loops; unchanged.
- Continuous operation (v10 scope): unchanged.
- A user verdict actually arriving for a seam-presented candidate
  (production verdicts come from James via chat_handler; the bench proves
  the mechanism, not the verdict).
- Device proof: bench/runtime integration only (standing rule).

## 6. Regression (sequential; heavy batteries deferred around GEN-SYNTH-5)

The mission touched one runtime file (`acceptance.py`, additive guard in
`present()`). Re-run green against the worktree:

- PLOOP-12 acceptance-seam battery: 20/20 (this mission).
- PLOOP-12 kill-resume: 7/7 (this mission).
- PLOOP-11 live-path: 33/33 green.
- PLOOP-11 kill-resume: 7/7 green.
- PLOOP-2: 49/49 green.
- PLOOP-8: 21/21 green.
- PLOOP-10: 46/46 green (via /tmp copy pointed at the worktree; the
  checked-in driver hard-codes its old worktree path).
- PLOOP-9: 152/152 green (via `PLOOP9_WT=$PWD`; the driver's fallback
  path is its stale worktree).
- PLOOP-3 (affected area — AcceptanceDriver): 10/10 accepted, 2/2
  rejected, 12/12 re-verified from disk (via a /tmp copy pointed at this
  worktree; the checked-in driver hard-codes its old worktree path). This
  also proves `present_retry()` still functions after the PLOOP-12 guard
  (the driver's retry path calls it).

## 7. Incidents

1. **Counterfactual plan failed static check (proof staging):** the first
   alternative plan used the `mul` op, which the composer's
   PrimitiveRegistry does not register (`plan failed static check`) —
   the auth gate correctly reported `diverges=False`. Replaced with a
   genuinely executable wrong plan (`add(x,x)` → 10.0 ≠ 12.0); the gate
   then genuinely passed. The incident confirms the gate is not vacuous.
2. **Tamper probe used the wrong enum casing (probe bug):** wrote
   `"ACCEPTED"` into the store row; `AcceptanceState` values are
   lowercase. Fixed the probe to `"accepted"`; the router then refused
   loudly as designed.
3. **Double-present / verdict-clobber hole (real defect, repaired §2):**
   the mandated adversarial contrast revealed `present()` silently
   overwrote records, including ACCEPTED verdicts. Repaired with the
   duplicate guard; all six adversarial checks green after the repair.
4. **Backgrounded exec output swallowed by pipes (harness quirk, no
   impact):** two backgrounded runs piped through `tail` delivered empty
   stdout; re-running to a log file showed the real output. Proof logs
   below were all captured to files.

No protected trees were touched. No `git stash`. No canonical mutation.
Work stayed in the dedicated `ploop12` worktree; the shared canonical
checkout was never written to.
