# PLOOP-11 REPORT — the Primary executive as one integrated production path

Date: 2026-09-29. Mission coordinator: PLOOP-11 (Backend Runtime track).
HEAD at execution: `eafba481751e0057b3bc0df1da68848c817e95ab`.
Worktree: `~/workspace/remor_convergence/worktrees/ploop11` (branch
`ploop-11-integration`). No canonical landing. No verification claim in
the commit. No delivery ZIP.

Objective (dispatch): prove the Primary executive as one integrated
production path — `wake → select loop → converge → accept → distill →
retain` — with the live tick using PLOOP-2 handoff contracts, PLOOP-8
transition checkpoints with a production caller, PLOOP-9 terminal routing
with a production caller, and PLOOP-7 admitted-finding delivery into real
loop inlets. End-to-end call paths, kill/resume across a real handoff,
adversarial refusals, loud unbound fallback states, sequential regression
batteries.

## 1. Structural re-map (fresh, at eafba48, before any wiring)

- Production tick: `runtime/core/run_controller.py`, `RunController.tick()`
  (~line 871). Owns cadence; the single production caller of the run loop.
- Executive: `runtime/core/executive/executive.py`. `ExecutiveController`
  owns six registered loop inlets; `enter()` invokes the real inlet.
  PLOOP-10 precedent: `RunController.bind_arbitration_substrate()` (~line
  432); `ExecutiveController.__init__` binds the substrate (~line 141).
- Handoffs: `runtime/core/executive/handoff.py` — `produce_handoff()`
  (~758), `accept_handoff()` (~849), `transition()` (~902). The declared
  route table `HANDOFF_ROUTES` (~line 491): run→execution_failure,
  run→acquisition_gap, distillation→novel_task; every other
  (loop, terminal_state) pair is a declared terminal. **No declared route
  chains two transitions** — every follow-on lands in a loop whose
  outcomes are all declared terminals. This is a deliberate contract fact,
  not a gap; it bounds what a single drive_cycle chain can traverse.
- Checkpoints: `runtime/core/executive/checkpoint.py`,
  `TransitionCheckpointStore` (~97). Single-use, integrity-hashed,
  verified-on-accept against live state.
- Terminal routing: `runtime/core/executive/terminal_routing.py`,
  `TerminalRouter` (~279), verifying real consumers
  (`run_controller.persisted_cycle_record`, `run_controller.rc_gap_backoff`).
- Seam doc: `runtime/core/executive/SEAM_EXECUTIVE.md` — declared the
  executive caller missing from production. PLOOP-11 builds exactly that
  caller.
- PLOOP-7 residual: admitted findings had no delivery consumer
  (`PLOOP-7-REPORT.md` ~lines 63–67). Closed here.
- `RunController` already accepts `registry=` cleanly; no private
  assignment needed. Existing checkpoint extraction covers all declared
  loop state extractors. No existing production constructor for the
  integrated path was found — the seam below is the first.

## 2. What was wired (exact files, hunks, why this seam)

**One new runtime module: `runtime/core/executive/live_path.py`** (~28 KB,
~470 lines). The mission's sole owned runtime file. It edits no other
mission's machinery — `git status` shows zero modified tracked files; the
change is purely additive.

- `LivePathConfig` / `build_live_path()` / `LivePath`: constructs the real
  `SwarmEngine`, `GapRegistry`, `RunController(registry=...)`,
  `AcceptanceLoop`, `RelevanceGate`, `ExecutiveController`,
  `TransitionCheckpointStore`, `TerminalRouter` — then wires the
  PLOOP-10-style bindings (store, router, arbitration, relevance gate) and
  reports `status()` as `bound`/`fallback_unbound` per component, never
  silent.
- `drive_tick()`: real wake — `ExecutiveController.enter()` with a
  validated `run_wake` boundary — then the production
  `RunController.tick()`, persisted through the real
  `ControllerCheckpoint.record_cycle()`.
- `surface()`: the production split of `transition()` — `produce_handoff`
  → `accept_handoff` → `executive.enter()` — with transition()'s
  checkpoint consume tail replicated exactly. The split (not `transition()`
  itself) is what lets a process die between produce and accept.
  Declared terminals go to `route_terminal`; contract violations go to
  `route_refusal`; unbound router/store degrade LOUDLY
  (`fallback_unrouted` with a named reason on every hop — nothing dropped).
- `drive_cycle(novel_spec=...)`: one integrated cycle — wake → tick →
  surface every contractual hop to its terminal → deliver admitted
  findings. `novel_spec` is single-use: consumed only by the
  distillation→generalization hop (novelty is never auto-generated
  downstream).
- `produce_for_resume()` (process A) / `resume()` (process B): the
  production kill-resume split. Resume loads the checkpoint on a fresh
  connection, verifies integrity, honestly re-fetches the gap/capability
  record from live state (never trusts checkpointed bytes), accepts
  through the contract, enters the real inlet, consumes the checkpoint.
- `submit_finding()` / `deliver_admitted()`: PLOOP-7 closure. Findings pass
  the relevance gate (truth standing → provenance link → bounded
  objective); admitted ones are delivered into the real declared loop
  inlet, with ownership validated against the executive's declared map
  before delivery — a false declaration is refused loudly.

**Lineage repair (mid-mission, at the seam):** `drive_cycle()` initially
kept the root wake as every hop's triggering boundary. Repaired so each
hop's `triggering_boundary_id` is the boundary the producing loop was
actually entered with: `surface()` records `accepted_boundary` (the
`BoundaryPresentation` `accept_handoff` minted) and exposes
`accepted_boundary_id`; `drive_cycle()` threads it as the next hop's
trigger. The public hop report carries only the id string (JSON-safe).

**Why this seam is the right one:** every mechanism it uses already exists
and is already proven (PLOOP-2/8/9/10 batteries, re-run green below). The
missing piece named by `SEAM_EXECUTIVE.md` was exactly the production
caller — not another store, router, or distiller (the no-duplication
mandate). One module, zero edits to existing machinery, every fallback
loud and inspectable.

**Proof files (all under `proofs/`):**

- `ploop11_live_path_proof.py` — 33 checks, sections A–G.
- `ploop11_proc_a.py` / `ploop11_proc_b.py` — fresh-process kill/resume
  halves (A ticks+produces then dies mid-handoff; B rebuilds from the same
  db files, honestly re-fetches, accepts, enters, consumes).
- `ploop11_kill_resume_driver.py` — runs A then B as sequential
  subprocesses, then a replay attempt that must be refused loudly. 7 checks.
- `proofs/logs/ploop11_live_path_2026-09-29.log` (33/33, exit 0),
  `proofs/logs/ploop11_kill_resume_2026-09-29.log` (7/7, exit 0).

## 3. Per-boundary results (exact vocabulary)

- Wake through the executive into the production tick — **PROVEN**.
  A1: tick entered via `ExecutiveController.enter()`; A2: cycle persisted
  through real `record_cycle()`.
- run→acquisition hop on production contracts — **PROVEN**. A3–A5: real
  open gap → handoff → checkpoint → acquisition inlet entered.
- run→execution hop, quarantine urgency priority — **PROVEN**. B1–B4: real
  capability quarantined through the engine's own path; failure surfaced
  before the gap queue; execution inlet diagnosed the live record.
- Checkpoints with a production caller (PLOOP-8 residual) — **PROVEN**.
  A4/B5: checkpointed and consumed; G2: tampered row refused;
  G3/KR6–KR7: replay refused (single-use enforced).
- Terminal routing with a production caller (PLOOP-9 residual) — **PROVEN**.
  A6/A7: chain terminated at declared terminals reaching real consumers
  (`run_controller.rc_gap_backoff` with the backoff really recorded —
  A8/A9, ledger-verified); D1/D2: quiet tick converged → routed to
  `run_controller.persisted_cycle_record`, ledger-proofed.
- Distillation→generalization with real distill (PLOOP-2 route) — **PROVEN**.
  C1: real `distill_session` on 6 worked examples, real success;
  C2/C3: handoff → generalization really entered through the executive.
- Retain (generalization retention through the seam) — **PROVEN**. C3:
  generalization inlet entered and retained. (Downstream trust-path
  promotion — ReviewBoard + verdict-bound promotion — is declared outside
  the six loops; not exercised here, see §5.)
- Admitted-finding delivery into real loop inlets (PLOOP-7 residual) —
  **PROVEN**. E1: admitted by provenance link; E2/E3: delivered into the
  real execution inlet and diagnosed; E4: ownership-contradicting
  declaration refused loudly; E5: outbox drained, gate record authoritative.
- Kill/resume across a real handoff — **PROVEN**. KR1–KR5: process A died
  mid-handoff; fresh process B resumed from disk with unbroken lineage
  (checkpoint triggering_boundary_id matches across processes), verified
  checkpoint, entered acquisition, consumed.
- Loud unbound fallback — **PROVEN**. F1: status names
  `fallback_unbound`; F2/F3: every hop accounted for, unrouted terminals
  recorded with reasons.
- Adversarial refusals — **PROVEN**. G1: out-of-contract handoff (tampered
  to_loop) refused; G2: tampered checkpoint row refused; E4: false
  declaration refused; KR6/KR7: replay refused loudly.
- Per-hop lineage (accepted-boundary threading) — **PROVEN BUT BOUNDED**.
  C4: checkpoint carries the producing loop's entry boundary; C5: each
  accept mints a fresh boundary id; `drive_cycle` threads hop N's accepted
  boundary as hop N+1's trigger. Bound: no declared route currently chains
  two transitions (every follow-on lands in a loop whose outcomes are all
  declared terminals — `HANDOFF_ROUTES`), so drive_cycle-level chaining is
  unexercisable by design, not by defect.
- The unified loop as one integrated path — **PROVEN BUT BOUNDED**. One
  seam (`live_path.py`) drives every contractual hop class through the
  production contracts with real state, real inlets, real consumers, and
  the full refusal surface. Bound: no single chain traverses all six loops
  (undeclarable under the current route table), and the acceptance inlet
  was not driven through the seam (§4).
- Acceptance inlet through the live-path seam — **UNPROVEN**. The inlet
  exists and works outside the seam; no completion candidate was presented
  through `live_path` in this mission.
- Single chain traversing all six loops in one drive_cycle — **UNPROVEN**
  (and undeclared by design — see the route-table note above).

## 4. Exact next boundary

**Acceptance through the live path.** Present a real completion candidate
(Attempt + passing AuthReport through Q8's `present()` gate) through
`LivePath.surface()`/`drive_cycle`, and route its CANDIDATE terminal to
the real verdict consumer. This is the only loop inlet never driven
through the integrated seam, and it is the last unproven hop class on the
wake→…→retain path. Everything it needs exists: the inlet, the present()
gate, the router, the checkpoint store.

## 5. What remains unproven

- Acceptance via the seam (§4 — the exact next boundary).
- Trust-path promotion downstream of generalization (ReviewBoard +
  verdict-bound promotion): declared outside the six loops; the seam hands
  off at the generalization terminal.
- Continuous operation (v10 scope): the seam drives on-demand cycles; the
  Run Controller's continuous cadence owning the live path is not yet wired.
- Production-wiring residual (declared in `HANDOFF_ROUTES`): the tick still
  dispatches gaps inline (V10-P2 behavior); the future seam has the tick
  surface instead of inline-dispatch. A surfaced gap may currently be
  re-dispatched by the acquisition inlet — redundant but real, recorded,
  never faked.
- Device proof: bench/runtime integration only; nothing here is
  device-ready (standing rule).

## 6. Regression (sequential, clean HEAD eafba48, dedicated worktree)

No other mission's machinery was edited (zero modified tracked files), but
the batteries were re-run anyway against a clean `eafba48` worktree,
sequentially, uncontended:

- PLOOP-2: 49/49 green (full battery incl. heavy T2).
- PLOOP-8: 21/21 green.
- PLOOP-9: 152/152 green.
- PLOOP-10: 46/46 green.

## 7. Incidents

1. **Import failure (repaired, disclosed):** first `live_path.py` import
   failed — `LOOPS` was imported from `runtime/core/executive/loops.py`;
   it lives in `runtime/core/microcontroller.py`. One-line repair;
   construction smoke then passed (store/router/relevance bound,
   arbitration `arbitrated`).
2. **G1 false alarm (proof bug, not a contract hole):** the tamper test
   rewrote `to_loop` to `execution` while the tick had actually surfaced
   `execution_failure` (the quarantined capability took urgency priority),
   so the "tampered" handoff named the TRUE owner and the contract
   correctly accepted it. The test now computes `route_owner()` dynamically
   and tampers to a genuinely wrong loop → refused loudly, 33/33.
3. **Capability-store identity guard fired during proof staging:** a
   hand-built `CapabilityRecord` was refused because its id wasn't the
   plan fingerprint (anti-substitution guard working as designed). The
   proof now derives the id via `plan_fingerprint()` exactly as legitimate
   writers do.
4. **Finding vocabulary mismatch:** `'converged'` is not in the finding
   terminal-state set; findings use `DISCOVERY_VERIFIED` (supported, not
   refuted) — proof staging fix.
5. **Lineage repair (§2):** `drive_cycle` collapsed hop triggers to the
   root wake; repaired to thread each accepted boundary forward; proven at
   the seam (C4/C5).

No protected trees were touched. No `git stash`. No canonical mutation.
Work stayed in the dedicated `ploop11` worktree; the shared canonical
checkout was never written to.
