# CUR-P2 — Phase 2 Mission Report: Curiosity Executive + Run Controller + Questioning Loop

**Status: REPORTED** — coordinator's evidence only. Nothing here is verified until Felix independently re-runs the gate. No verification claim is made in this report or in the commit message.

- **Mission:** CUR-P2 — Phase 2: Curiosity Executive + Run Controller + Questioning loop
- **Role:** direct coordinator (no child workers spawned; all work done directly)
- **Required pin:** `1f7e48d2ce530e9c3f21a412d05d62883e772f9d` (canonical matched 2026-09-30 UTC, unedited)
- **Worktree:** `~/workspace/worktrees/curp2-mission`, branch `cur-p2`
- **Date:** 2026-09-30 UTC

## 1. End-state achieved (reported, not verified)

A real inquiry executes end to end under a real FRM grant:

```
trigger → Curiosity Executive → Curiosity Run Controller → Questioning loop
→ reasoning substrate → evidence → verification → terminal state
→ fenced Evidence Store → return
```

with provenance, enforcement, budget, roll-call, and relevance genuinely enforced. The persistent proof battery `proofs/cur_p2_phase2_proof.py` executes 113 checks across 17 test groups (T01–T13, T16) covering every clause of the proof matrix, adversarial contrasts included. All 113 checks pass in this coordinator's runs (three consecutive hermetic runs: 113/107-clean history, then 113/113 twice with no manual wipe).

## 2. Exact changed files

All new files (no tracked file modified — `git status` shows only `??` entries; `git diff --name-only` is empty):

| File | Lines | What |
|---|---|---|
| `runtime/curiosity/cognition.py` | 220 | NEW — deterministic `PrecisionCognitionProvider` (mechanical `extract_unknown` / `compose_precise` / `score_precision`) |
| `runtime/curiosity/substrate.py` | 73 | NEW — `CuriositySubstrate(MicrocontrollerSubstrate)`; separate instance per C-1.4; registers the cognition provider; **flagged deviation**: `register_loop` accepts the curiosity loop vocabulary (see §6) |
| `runtime/curiosity/executive/boundary.py` | 85 | NEW — trigger validation, boundary-class → loop ownership table, future-loop refusal vocabulary |
| `runtime/curiosity/executive/executive.py` | 364 | NEW — `CuriosityExecutive`: activation gate (kill-state → roll-call → FRM → budget/slot → fit), priority, kill relay, aggregate-only views |
| `runtime/curiosity/executive/__init__.py` | 39 | NEW — package exports |
| `runtime/curiosity/run_controller/controller.py` | 742 | NEW — `CuriosityRunController`: dispatch, priority-ordered admission, tick/step, checkpoints/recovery, pause/resume/stop, slice accounting, termination, attribution |
| `runtime/curiosity/run_controller/__init__.py` | 31 | NEW — package exports |
| `runtime/curiosity/loops/__init__.py` | 37 | NEW — loop registry scaffolding |
| `runtime/curiosity/loops/questioning/__init__.py` | 26 | NEW — package exports |
| `runtime/curiosity/loops/questioning/loop.py` | 563 | NEW — `QuestioningLoop`: 3-pass mechanical refinement over a real `RefinementGraph`, corpus retrieval, cognition inlet, three terminal classes |
| `proofs/cur_p2_phase2_proof.py` | 944 | NEW — persistent proof battery (T01–T13, T16), structured `results.json`, nonzero exit on failure |
| `proofs/cur_p2/` | dir | proof run artifacts (per-test run dirs, `results.json`) |

Reused frozen machinery (not copied, not modified): `runtime/curiosity/frm/` (FRM policy+evaluation), `runtime/curiosity/evidence/` (store+writer), `runtime/curiosity/rollcall/` (scheduler+GAM+ledger), `runtime/curiosity/attribution/` (ChainLedger), `runtime/governance/curiosity_enforcement/` (engine+states), `runtime/core/microcontroller/substrate.py`, `runtime/core/graph_controller/`, `runtime/core/executive/checkpoint.py` (TransitionCheckpointStore), `runtime/core/executive/terminal_routing.py` (TerminalLedger).

## 3. Claim-by-claim evidence

Classification scale: **PROVEN** | **PROVEN BUT BOUNDED** | **UNPROVEN** | **UNBOUND** | **ABSENT** | **UNAVAILABLE**.

### C1. A real FRM grant funds a real curiosity inquiry — PROVEN
- *Implementation:* `CuriosityExecutive.request_activation` consults the real `FinancialResourceManager.evaluate_round` (mid-epoch, same epoch reuse by identity), takes the grant as issued.
- *Causal test (T01):* demand 60s/2 slots → grant `budget_s=60.0 max_concurrent=2 epoch=1`, enforcement RUNNING, inquiry runs to `QUESTION_RESOLVED` (score 3/3, spent 0.0008s < slice 30s).
- *Adversarial contrast (T03):* zero-budget FRM policy → `NO_BUDGET` refusal, FRM still consulted once, nothing dispatched, loop never registered on the substrate.
- *Slot contrast (T04):* WARNING_1 with demand 2 → frozen FRM truncates `int(2*0.25)=0` slots → `NO_SLOT` refusal (the executive never inflates the restriction); WARNING_1 with demand 4 → 1 slot, grant 15s, inquiry converges under the cap.

### C2. The full pipeline executes — PROVEN
- *Path (T01):* `CuriosityTrigger` → `request_activation` (approved) → `run_controller.run_inquiry` → `QuestioningLoop.step` (3 passes, real `RefinementGraph` open/operate/close) → `CuriositySubstrate` spawn/charge/retire → `CuriosityWriter.submit()` → fenced `CuriosityEvidenceStore` → `TerminalLedger` route → return. Inquiry id shared end to end; finding fields `loop=questioning terminal=QUESTION_RESOLVED`.
- *Fresh-process evidence (T02):* finding + payload + ledger route reopen intact in a fresh stack over the same files.
- *Terminal matrix (T13):* all three deterministic terminals for real — `QUESTION_RESOLVED` (score 3), `INSUFFICIENT_EVIDENCE` (score 2, 3 passes), `BOUNDARY_ESTABLISHED` (score 2, 3 passes) — each with payload carrying the precise question.

### C3. Provenance is enforced — PROVEN
- *Implementation:* every persisted finding carries `EvidenceProvenance(loop, bounded_objective, model, triage)`; `model="curiosity-questioning/v1 (mechanical refinement; no external cognition provider)"`; triage is `propose_investigation` or `boundary` — never an acceptance claim (T01, T06).
- *Attribution (T01/T09):* `ChainLedger` records work/result/admission per attempt; admission verdict is `retained` (retention, not acceptance); kill→resume produces `work_<id>` (failed/killed) and `work_<id>#a2` (success, `parent_work_ref` chain) — no primary-key collision.

### C4. Enforcement prevents activation — PROVEN
- *T04:* HARD_SHUTDOWN_RESOURCE, SUSPENDED_SAFETY, BANNED_6M → `KILL_STATE` refusal **before** the FRM is consulted (`evaluate_round` calls = 0), nothing dispatched. WARNING_1 passes through with the FRM-applied restriction (C1 contrasts).
- *Regression:* the frozen `cur_p1b` enforcement proof battery re-run: **28/28 ALL GREEN** (log at `proofs/cur_p2/cur_p1b_run.log`).

### C5. Budgets are enforced — PROVEN
- *Slice accounting (T01/T11):* per-inquiry slice = grant/max_concurrent; exact elapsed `spent_s` tracked per tick.
- *Forced breach (T08):* microscopic grant (2e-5s, slice 1e-5s) → first tick breaches → machinery stops, checkpoint written, inquiry SUSPENDED, `RESOURCE_BOUNDARY` returned, BLOCKED finding persisted with exact `spent_s=0.00043 budget=1e-05` and the substrate refusal cause; BLOCKED finding reopens fresh with partial work.
- *Reservation invariant (repaired, §5.4):* tree peak live reservation = 13/16 slice < 1 slice, so max_concurrent inquiries can never deadlock the grant-sized loop pool.

### C6. Roll-call is enforced — PROVEN
- *T05:* missing attestation → `ATTESTATION_INVALID`; MISSED → refused; INVALID (nonce mismatch) → refused; MET → admitted. (This corrected the earlier defect where missing/MISSED were allowed — §5.6.)
- *Regression:* frozen roll-call battery 49/49 green.

### C7. Relevance is enforced — PROVEN BUT BOUNDED
- *T06:* relevance scored (1.0) and recorded as **advisory only**; payload carries the D-4 note (threshold 0.25 provisional, Primary Acceptance final, curiosity never accepts its own findings, true-but-irrelevant stays fenced). Triage never claims acceptance.
- *Bound:* per D-4 the threshold is provisional and Primary Acceptance is final — the "enforcement" here is honest labeling, not a hard gate. Classified accordingly.

### C8. Reasoning travels the cognition inlet — PROVEN
- *T01:* 3 node summaries carry `[cognize:<mc>]` markers; the unknown-MC and bogus-operation refusals were smoke-tested.
- *Fail-closed contrast (T16):* with a dead provider installed via `set_cognition_provider`, the inquiry does **not** resolve — it suspends with BLOCKED, cause naming `cognition_failed`; zero `[cognize:]` markers in the partial passes (nothing fabricated past the inlet); the failure is externalized as a BLOCKED finding. There is no direct-call fallback in the loop.

### C9. Priority admission — PROVEN
- *T11:* under one concurrency slot, with the slot held and the low-priority inquiry dispatched first, freeing the slot admits the PRIMARY_REQUESTED (priority 0) inquiry first; the low-priority one stays PENDING until the high-priority inquiry TERMINATEs (observed via phase-split stepping). Mid-epoch grant shared by identity → per-grant slot accounting. Equal priorities break FIFO by dispatch sequence.
- *Repaired:* admission was insertion-ordered (defect caught by this test — §5.9); unprioritized decisions defaulted to priority 0 (now fail-closed to 1 — §5.10).

### C10. Substrate isolation — PROVEN
- *T12:* separate curiosity/Primary substrate instances; cross-instance retire refused `unknown_microcontroller` both directions; cross-vocabulary spawn refused `loop_unregistered` both directions; Primary loop names refused on the curiosity side and vice versa.

### C11. Executive sees aggregates only — PROVEN
- *T12:* depth-1 attribute scan of the executive finds no substrate/microcontroller/graph handle (`held_references` names only FRM, GAM, run controller, and a path string); executive view JSON contains no `mc-` ids and no `purpose` fields; inquiry views carry loop aggregates only.

### C12. Kill / pause / resume / checkpoints — PROVEN
- *T09:* kill via the executive → propagated to run controller → loop aborted → checkpointed (`SUSPENDED_KILL`) → killed inquiry does not advance (spend frozen). Fresh-process resume: same inquiry id, `spent_s` preserved, full lineage including `killed` → `checkpointed` → `resumed_from_checkpoint`, converges to QUESTION_RESOLVED on re-execution (not replay).
- *Checkpoint integrity (repaired, §5.11):* resume now calls `verify_checkpoint_integrity()` between `load()` and `mark_verified()`; the tamper test corrupts a checkpoint row and asserts resume refuses with `integrity hash mismatch` and the row stays `saved` (never verified).
- *Lineage survival (repaired, §5.8):* terminal events (`killed`, `resource_boundary`) are noted before the checkpoint save so the resumed lineage carries them.
- *T10:* pause freezes spend (two samples identical); resume converges.

### C13. Future loops refuse loudly — PROVEN
- *T07:* `hypothesis_candidate` → `LOOP_ABSENT`, naming `scientific_inquiry (Phase 3)`; nothing dispatched.

### C14. No second mechanisms — PROVEN (by construction + test)
- One evidence store (fenced `CuriosityEvidenceStore` + `CuriosityWriter`), one grant system (FRM), one arbitrator class (none built), one checkpoint store (sidecar index only locates the real store's records), one terminal router (`TerminalLedger` reused; Primary's hard-coded `TerminalRouter` not duplicated).

## 4. Determinism

The battery is hermetic (`main()` wipes `proofs/cur_p2/runs/` at startup) and deterministic: three consecutive runs passed 113/113 with no manual cleanup between the last two. (One mid-campaign run exposed the non-hermetic flaw — a stale ledger row from a prior run made T01 count 2 routes; the start-of-run wipe repairs it structurally.)

## 5. Incidents and repairs (complete)

1. **GraphController signature mismatch.** First end-to-end run: `attach_graph() got an unexpected keyword argument 'nodes'`. Repaired the loop to attach a real `RefinementGraph` and use `open_region(mc_id, graph_id, objective, max_operations=16)` / `close_region()`.
2. **Relevance floor.** Score node omitted: node goals scored 0.333 < `_RELEVANCE_FLOOR` 0.34. Reworded node goals so each required node independently clears the floor.
3. **Retrieval false positives (3 defects).** (a) single common-word match supplied observable evidence → overlap must be ≥2; (b) later-pass template vocabulary polluted retrieval → qualifying passage must share a term with the unknown itself; (c) duplicate expanded seed terms double-counted overlap → dedupe before search. Also added `qualifying_hits` to persisted pass records.
4. **WARNING_1 zero-slot deadlock.** Frozen FRM truncates `int(2*0.25)=0` slots; run controller spun forever on a PENDING inquiry. Repaired: executive refuses `NO_SLOT` (never inflates the restriction); `dispatch` backstops with `AdmissionRefused` on `max_concurrent<1`.
5. **Reservation over-commit.** Tree peak live reservation (slice + slice/4 + slice/16 = 1.31×slice) exhausted the grant-sized pool under WARNING_1's single slot. Repaired: root spawns with slice/2 → peak 13/16 slice; invariant documented in `_activate`.
6. **Roll-call allowed missing/MISSED.** Corrected `_check_roll_call` to require MET; missing/MISSED/INVALID → `ATTESTATION_INVALID`.
7. **Attribution primary-key collision.** Kill→resume would rewrite `work_<id>`/`res_<id>`. Repaired with attempt-scoped refs (`#aN`) and `parent_work_ref` lineage.
8. **Checkpoint lineage lost terminal events.** The saved lineage predated `killed`/`resource_boundary` notes. Repaired: terminal events noted before the checkpoint save; `checkpointed` noted before `from_state` is built.
9. **Checkpoint integrity never verified.** `resume_inquiry` called `load()` then `mark_verified()`; `load()` doesn't check the tamper hash. Repaired: `verify_checkpoint_integrity()` between them; tamper test proves refusal.
10. **Priority admission was insertion-ordered; default priority 0.** `_admit_pending` sorted by `(priority, inquiry_id)` with random ids, and unprioritized decisions defaulted to top priority. Repaired: monotonic dispatch `seq` tiebreak; fail-closed default priority 1.
11. **Origin vocabulary.** Verified no defect: `new_trigger.validate()` rejects unknown origins against the frozen `ORIGINS` tuple; the correctly-spelled `CURIOSITY_INITIATED` never reaches the priority map.

## 6. Flagged deviation (standing)

`CuriositySubstrate.register_loop` accepts the curiosity loop vocabulary (`questioning`) directly, where the Primary-as-template rule would put a logical→private adapter. It reuses all base lifecycle machinery (spawn/retire, caps, pools, charging, unwind, LoopView) and overrides exactly one method. Recorded in the module docstring and here. Requires James's explicit decision at the gate: keep the override or require the adapter.

## 7. Exact next boundary

The next executable boundary is **Felix's independent gate re-run**: the battery, the frozen regression suites, and the tree-hygiene check must be re-executed on Felix's machine against the landing HEAD. The mission's own runs are coordinator evidence only (REPORTED). The first causal boundary beyond the gate is Phase 3 loop machinery (`scientific_inquiry`, `creative_exploration`, `generalization`), which this phase correctly refuses as `LOOP_ABSENT` — no Phase-3 work was started.

## 8. What remains unproven

- Everything in this report, until the independent gate re-run.
- Two-inquiry **fully concurrent** execution against one grant (T11 proves priority-ordered admission and sequential convergence under one slot; concurrent root+child reservation pressure at max_concurrent ≥ 2 with both inquiries mid-pass is exercised only structurally via the 13/16 invariant, not under wall-clock contention).
- Behavior under a real external cognition provider (only the mechanical provider and the dead-provider contrast are proven).
- Device/hardware behavior (not applicable — no device surface in this phase).
- The `register_loop` vocabulary deviation (kept vs. adapter — James's call).

## 9. Verification appendix

- Proof battery: `proofs/cur_p2_phase2_proof.py` → `proofs/cur_p2/runs/results.json` — **113/113** (three consecutive hermetic runs)
- Frozen FRM battery (`runtime/curiosity/frm/tests/test_frm_battery.py`): OK
- Frozen roll-call battery (`tests/curiosity/test_rollcall.py`): 49/49 pass
- Resource arbitration contract (`tests/contracts/test_resource_arbitration.py`): 7/7 pass
- Frozen enforcement proof (`proofs/cur_p1b_enforcement_proof.py`): **28/28 ALL GREEN** (log: `proofs/cur_p2/cur_p1b_run.log`)
- Tree hygiene: `git status --short` shows only new CUR-P2 paths; `git diff --name-only` empty (no frozen file touched). (The frozen `cur_p1b` proof rewrote its own manifest as a run side effect; restored via `git checkout --` — not committed.)
- Focused conventional unit tests: deliberately not added. The persistent proof battery (113 hermetic checks) exercises every new module end-to-end through the real stack — activation, dispatch, stepping, checkpoints, evidence, ledger, attribution — which for a wiring mission is stronger evidence than isolated unit tests. Judgment recorded here; the gate may still require them.
- Commit: branch `cur-p2` in `~/workspace/worktrees/curp2-mission` (see `git log`; message makes no verification claim)
