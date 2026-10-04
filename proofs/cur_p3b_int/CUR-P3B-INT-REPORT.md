# CUR-P3B-INT mission report — creative_exploration vocabulary admission + integration (2026-10-03)

**Mission:** wire the landed Creative Exploration loop through the full
Phase-2 governance chain under James's U-1-class admission of
`"creative_exploration"` to the curiosity loop vocabulary (2026-10-03,
main chat).
**Worktree:** `~/workspace/worktrees/cur-p3b-int`, branch `cur-p3b-int`,
based at canonical `25cc5f5` (fail-closed pin verified at mission start;
merge-base ancestry check in `gate_run.sh`).
**Battery:** `proofs/cur_p3b_int/gate_run.sh` → landed 66-check stage
battery (invoked as regression) + `cur_p3b_int_proof.py` (44 checks).
**Result: 44/44 integration checks green + 60/63 stage checks green
(the 3 deltas are the decided boundary change, see below), exit 0,
stable across runs.**

## Classification note (James's decisions #3/#5 framing)

`"creative_exploration"` is admitted as a **semantic capability/loop
classification** — the name of the boundary class the Executive routes
to the Creative Exploration loop controller. It is **never** evidence
that REMOR independently possesses creative reasoning. This report
claims only: the loop controller exists, its pipeline runs, its
refusals fire, its findings carry provenance. The mechanism is real;
the capability claim is not made.

## Per-mandate results

| # | Mandate | Verdict |
|---|---------|---------|
| 1 | Fresh structural re-map | **PROVEN** — pin `25cc5f5` fail-closed; verified the as-landed state (loop files present, vocabulary `("questioning", "scientific_inquiry")`, `BOUNDARY_GENERATIVE_PROMPT` in `ABSENT_OWNERSHIP`, registry with two loops); read all three frozen files at their landed shape before editing. |
| 2 | Exactly three frozen files, minimally | **PROVEN** — substrate.py (vocabulary + constant), executive.py (ownership move + fit branch + view entry), run_controller/controller.py (imports, models, terminals, registry, `_build_ctx`, `_verified_ledger`, except clause, payload extras). One integration-exposed defect repaired in the stage-built loop (see Incidents). Per-edit justifications below. |
| 3 | End-to-end creative proof | **PROVEN** — generative_prompt + intent-state → executive → run controller → creative pipeline → CANDIDATE_GENERATED → provenance-stamped finding → fenced store → return, under a real FRM grant (CountingFRM: 1 round, 60.00s). C-6.4 adversarial **PROVEN** through the integrated path (every exit `epistemic_status: "hypothesis"`; no belief string in any mechanism output; terminal vocabulary has no belief state). C-4.2 **PROVEN** (NO_BUDGET). C-4.3 **PROVEN** (BLOCKED, exact consumption externalized, checkpoint preserved, SUSPENDED). C-6.2 **PROVEN** (resolved, active=0, spawned=2, retired=2; second exploration converges, no leaked state). Kill/resume **PROVEN** across real process death (kill in process A → resume in fresh process B → CANDIDATE_GENERATED, lineage carries killed + resumed_from_checkpoint). Terminal routing + attribution **PROVEN**. |
| 4 | Questioning + scientific_inquiry regression | **PROVEN** — QUESTION_RESOLVED with byte-identical payload shape (15 keys); HYPOTHESIS_SUPPORTED unchanged. |
| 5 | Adversarial | **PROVEN** — executive routes questioning-shaped → questioning, inquiry-shaped → scientific_inquiry (no fixed pipeline); formal_proof refused UNOWNED; provenance-stripped finding refused at the integrated store (through the loop's own write path); `discovery_novelty` refused at dispatch by the registry fence. |
| 6 | Battery discipline | **PROVEN** — gate_queue.md checked (no competing battery), load < 2.0, niced, halt-on-first-fail. |
| 7 | `gate_run.sh` | **PROVEN** — full output below; pin check uses merge-base ancestry (the P3B lesson); no hardcoded home paths (derived from `__file__`). |
| 8 | Report-don't-land | **PROVEN** — branch `cur-p3b-int`, neutral commit messages, canonical untouched. |

## Per-edit justification (frozen files)

1. **`runtime/curiosity/substrate.py`** — `CURIOSITY_LOOPS` gains
   `"creative_exploration"` (+ `LOOP_CREATIVE_EXPLORATION` constant);
   the closed tuple + frozenset + ValueError fence is intact, not
   generalized. This is the literal content of James's U-1-class
   decision 2026-10-03.
2. **`runtime/curiosity/executive/executive.py`** — `BOUNDARY_GENERATIVE_PROMPT`
   moves from `ABSENT_OWNERSHIP` to `LOOP_OWNERSHIP` → the creative
   loop; `_check_fit` gains the creative branch (non-empty
   commission/intent-state + bounded objective — the loop judges
   ownership via assess_intent, mirroring the questioning/inquiry
   division of labor; per the §8 selection grammar the grammar
   begins with intent-state ownership); `executive_view` gains the
   creative entry. `ABSENT_OWNERSHIP` keeps the genuinely absent
   `novel_task`.
3. **`runtime/curiosity/run_controller/controller.py`** — imports for
   the creative loop; `LOOP_MODELS` gains the creative MODEL_ID;
   `SUCCESS_TERMINALS` gains `CANDIDATE_GENERATED` (the creative
   loop's decisive release; `BOUNDARY_ESTABLISHED` was already
   present via the questioning terminal); the `_loops`/`_inlets`
   registries gain the creative entries (questioning + inquiry
   construction byte-identical); `_build_ctx` gains the creative
   branch (verified ledger via the documented, checkable
   `primitive_id | verification_event | gate_reference` convention;
   corpus docs as prior art); `_verified_ledger()` helper;
   `_step_inquiry` catches `CreativeSubstrateRefused`;
   `_persist_terminal` carries the honest creative extras
   (`epistemic_status`, `candidates`) from the terminal dict —
   never fabricated; relevance stays `None` (no score invented
   for a verdict loop, D-4).

## Incidents

1. **Integration-exposed defect in the stage-built loop
   (`restore()` vs checkpoint serialization) — FOUND, ROOT-CAUSED,
   REPAIRED, VERIFIED.** The checkpoint store round-trips
   `loop_state` through JSON with `default=str`; the creative loop
   stores the `IntentState` dataclass in its state, which degrades
   to its `str()` form. `restore()` never rebuilt it, so the first
   post-resume `_operate_node` crashed on `intent.reason`
   (`AttributeError: 'str' object has no attribute 'reason'`).
   The stage battery never checkpointed, so only the kill/resume
   path exposed it (same class as CUR-P3A-INT's NODE_GOALS).
   Repair: `restore()` rebuilds the `IntentState` via
   `assess_intent` from the checkpoint-preserved trigger — faithful
   because `assess_intent` is a pure function of commission +
   bounded objective. T20 now passes across real process death.
   No redesign; the live path is untouched.
2. **Landed stage battery's T10 went stale by decision.** T10
   encoded the pre-admission boundary (three frozen-gate
   refusals). Post-admission its assertions invert: the gate now
   opens, so the bare-harness T10 fails 2 assertions and crashes
   on the third (its `gam=None` executive cannot proceed past the
   now-open ownership gate). The landed battery was NOT modified
   (packet constraint); `gate_run.sh` documents the contract
   explicitly: 60/63 pass and the ONLY deviations are the three
   known-stale T10 lines (`STALE-AS-DECIDED`, fail-closed
   otherwise). The real coverage lives in this battery's
   T13/T14, which prove the admission on a full stack.
3. **Two battery-assertion fixes (test, not mechanism):** the
   `TerminalLedger` query API is `routes(loop=...)`, not
   `routes_for(...)`; the T16 "no belief" scan initially caught
   the test's own `bounded_objective` string ("t16 belief push")
   — rescoped to the mechanism's outputs only and renamed.
   No mechanism defect in either case.
4. No protected-tree touches beyond the three authorized files
   (+ the one integration-exposed loop repair, disclosed above);
   no parallel batteries; canonical untouched (report-don't-land).

## What remains unproven

Nothing in the mandate is unproven. Adjacent work deliberately not
absorbed: CUR-P3C (Discovery/Novelty) is a separately queued
mission; the `discovery_novelty` vocabulary term is not admitted
(registry fence proven closed to it in T24).

## Exact next executable boundary

**Gate + landing of branch `cur-p3b-int` in main chat** — Felix's
independent re-run of `proofs/cur_p3b_int/gate_run.sh` against the
landing HEAD. After landing: **CUR-P3C — Discovery/Novelty loop
controller** (stage build, new files only; its vocabulary
admission will be a fresh U-1-class decision for James — this
mission's authorization covered `creative_exploration` only).
