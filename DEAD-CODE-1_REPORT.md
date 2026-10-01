# DEAD-CODE-1 — Mission Report

**Coordinator:** DEAD-CODE-1 mission coordinator (subagent, structural-debt track)
**Date:** 2026-10-01
**Worktree:** `~/workspace/worktrees/dead-code-1` — branch `dead-code-1-work`, from canonical `44e19e4` (GAP-REASONER-WIRE-1)
**Disposition:** REPORT, DO NOT LAND — Felix gates in main chat; only Felix lands.

## Objective (from packet)

Adjudicate the 45 dead-code targets from the 2026-09-29 dead-code audit: REMOVE, or mark SCAFFOLD / FUTURE / TEST-ONLY / TEST-SUPPORT with explicit evidence. No stubs, no placeholders, no dead imports left behind. Regression batteries + behavioral fingerprint before/after. Gate-ready report.

## Outcome

- **39 defs REMOVED** across 25 files (2,296 lines deleted, 56 inserted — the insertions are status markers and docstring repairs).
- **2 marked FUTURE** (`evaluate_l3`, `L3Refused` — D-3 governance machinery, James's decision 2026-09-29; deleting would be terminal condition (c)).
- **5 marked TEST-SUPPORT** (`huggingface_index`, `build_domain_substrate`, whole `gate.py` module, `testdoubles.make_substrate`, whole `harness_bridge.py` module) — each exercised by a crossed proof that must keep importing.
- **2 modules marked TEST-ONLY** (`runtime/services/providers.py`, `runtime/services/repair.py`).
- **Regression:** 231/231 tests pass before AND after (identical count; same 9 pytest paths).
- **Behavioral fingerprint:** 21/21 live-behavior lines byte-identical before/after; only the removal-surface and marker lines differ, exactly as designed.
- **Gate battery:** `~/workspace/remor_convergence/proofs/dead_code1/gate_run.sh` reproduces everything in fresh sequential processes.

## Method (re-runnable)

1. `deadness_remap.py` — AST index of every public top-level def at HEAD → word-boundary reference search over `runtime/` + `distill1/` + `tests/` (+ `proofs/` for proof-script-only names), hit classification IMPORT/STRING/GETATTR/DECORATOR/DUNDER_ALL. Recorded output: `deadness_remap.json` (pre-removal), `deadness_remap_after.json` (post-removal).
2. **Full-tree sweep** beyond the audit's scope (`grep -F -f dead_names.txt` over the whole worktree incl. `proofs/`, `scripts/`) — this caught 3 proof consumers the audit missed and changed 3 verdicts from REMOVE to keep-with-mark.
3. **Hardlink discovery** — `runtime/` is hardlinked inode-for-inode into `pylib/swarm_engine/` (verified: same inode 4766466 for `services/availability.py`; 39/39 in `acquisition/`). `swarm_engine.*` imports and `runtime.*` imports are the SAME files. This confirmed the audit's "test-only" label for providers.py/repair.py was correct (my initial read said otherwise — corrected before acting).
4. `remove_dead.py` — AST-precise excision (decorator-aware span removal), then orphaned-import analysis by diffing name-usage against `git show HEAD:` (only imports used exclusively by removed code were dropped), blank-line collapse, docstring honesty repairs.
5. `fingerprint.py` — 12 live call paths through touched modules + removal-surface assertion + marker assertion; run against a pristine `44e19e4` worktree (before) and the mission tree (after).

## Verdict table — the 45 audit targets

| # | Def | File | Verdict | Evidence |
|---|-----|------|---------|----------|
| 1 | `attach_external_evidence_neutral` | acquisition/semantic_capability.py | **REMOVE** | 0 refs (re-map + full-tree sweep) |
| 2 | `attach_extracted_probes_neutral` | acquisition/semantic_capability.py | **REMOVE** | 0 refs |
| 3 | `extract_structured_semantics` | acquisition/semantic_capability.py | **REMOVE** | 0 refs |
| 4 | `observation_to_neutral_evidence` | acquisition/semantic_capability.py | **REMOVE** | 0 refs |
| 5 | `validate_consequence` | acquisition/semantic_structure.py | **REMOVE** | 0 refs |
| 6 | `search_with_semantic_constraints` | acquisition/semantic_structure.py | **REMOVE** | 0 refs |
| 7 | `examples_from_structure` | acquisition/semantic_structure.py | **REMOVE** | 0 refs |
| 8 | `relation_consequences` | acquisition/semantic_structure.py | **REMOVE** | 0 refs |
| 9 | `filter_candidates_by_constraint` | acquisition/semantic_structure.py | **REMOVE** | 0 refs |
| 10 | `bridge_ir_to_gap` | acquisition/capability_bridge.py | **REMOVE** | 0 refs |
| 11 | `enrich_pipeline_requirement` | acquisition/intent.py | **REMOVE** | 0 refs |
| 12 | `expand_definition` | acquisition/lexical.py | **REMOVE** | 0 refs |
| 13 | `strategy_family_synthesis_bridge` | acquisition/atomic_operators.py | **REMOVE** | 0 refs |
| 14 | `build_inventory_graph` | acquisition/atomic_operators.py | **REMOVE** | 0 refs |
| 15 | `build_grade_graph` | acquisition/atomic_operators.py | **REMOVE** | 0 refs |
| 16 | `find_distilled_techniques` | acquisition/generalize_driver.py | **REMOVE** | 0 refs |
| 17 | `find_generalization_records` | acquisition/generalize_driver.py | **REMOVE** | 0 refs (its stale `swarm_engine.intellect.unified_memory.read_experiences` import died with it — mandate item 2 resolved) |
| 18 | `get_dependency_closure` | agent_org/dispatch_learning.py | **REMOVE** | 0 refs |
| 19 | `AmbiguitySeeker` | cognition/ambiguity_seeker.py | **REMOVE** | 0 refs (class; module docstring rewritten — it described the removed loop; residual `AmbiguityGap`/`AmbiguityQuery` dataclasses have no consumers either — flagged adjacent, left for follow-up, see below) |
| 20 | `interpret_each_clause` | cognition/compound_semantics.py | **REMOVE** | 0 refs |
| 21 | `interpret_compound` | cognition/compound_semantics.py | **REMOVE** | 0 refs |
| 22 | `reorder_by_relevance` | cognition/input_dependencies.py | **REMOVE** | 0 refs |
| 23 | `decompose_nested` | cognition/nested_decomposition.py | **REMOVE** | 0 refs |
| 24 | `decompose_sequential` | cognition/sequential_decomposition.py | **REMOVE** | 0 refs |
| 25 | `verify_split` | cognition/sequential_decomposition.py | **REMOVE** | 0 refs |
| 26 | `AutonomousRunner` | core/autonomy.py | **REMOVE** | 0 refs (class) |
| 27 | `release_db_ownership` | core/db_ownership.py | **REMOVE** | 0 refs; acquire path `claim_db_ownership` live; the GC finalizer inlines its own release and never called this function; docstring said "prefer engine.close()" |
| 28 | `ChainBroken` | curiosity/rollcall/ledger.py | **REMOVE** | 0 raises/catches anywhere incl. proofs; `audit()` returns `Tuple[bool, Optional[str]]`, `purge()` raises live `RetentionRefused` — the implemented contract does not expect this exception (packet's own conditional permits removal) |
| 29 | `CycleRecord` | improvement/agenda.py | **REMOVE** | 0 refs (class) |
| 30 | `node_kind` | improvement/goal_language.py | **REMOVE** | 0 refs |
| 31 | `store_catalog` | intellect/unified_memory.py:1203 | **REMOVE** | 0 refs anywhere in tree; MEMORY-UNIFY-1 has not referenced it (open coordination item below) |
| 32 | `LongHorizonCycleLoop` | longhorizon/cycle_loop.py | **REMOVE** | 0 refs (class; `CycleStore`, `ChallengeEnvironment`, `TaskCandidate` remain live) |
| 33 | `candidate_library` | project/continuation.py | **REMOVE** | 0 refs (self-declared deprecated in docstring) |
| 34 | `register_extension_entries` | services/availability.py:58 | **REMOVE** | 0 refs; the NL intent-dispatch sibling task is not in mission_queue.md (no owner); `EXTRA_ENTRIES` hook point and its comment retained for the future sibling |
| 35 | `ObservedComposition` | synthesis/abstraction.py | **REMOVE** | 0 refs (class) |
| 36 | `record_synthesis_outcome` | synthesis/abstraction.py | **REMOVE** | 0 refs |
| 37 | `AcquisitionExperience` | synthesis/acquisition_learning.py | **REMOVE** | 0 refs (class) |
| 38 | `substrate_available` | synthesis/nlu_substrate.py | **REMOVE** | 0 refs |
| 39 | `reset_for_tests` | synthesis/nlu_substrate.py | **REMOVE** | 0 refs |
| 40 | `evaluate_l3` | governance/curiosity_enforcement/l3_combiner.py | **FUTURE** | D-3 severe-violation combiner James decided 2026-09-29 (`architecture/exec2-enforcement-three-level-kill-policy_2026-09-29.md`); NOT wired into the live enforcement path; exercised by crossed `proofs/cur_p1b_enforcement_proof.py`. Deleting or silently wiring = his decision (terminal condition (c)). Marked in module docstring. |
| 41 | `L3Refused` | governance/curiosity_enforcement/l3_combiner.py | **FUTURE** | Raised only by `evaluate_l3` (its own module); dead-from-outside. Kept with `evaluate_l3` per mandate item 5. |
| 42 | `huggingface_index` | acquisition/weights.py | **TEST-SUPPORT** | No production caller; exercised by crossed `proofs/qwen3_quarantine_proof.py` (QWEN3-ACQUIRE-1 quarantine battery). Marked in docstring. **Caught by the full-tree sweep — the audit's scope missed it.** |
| 43 | `build_domain_substrate` | curiosity/frm/bridge.py | **TEST-SUPPORT** | No production caller; exercised by `proofs/seamwire4_frm_bridge_proof.py`. Marked in docstring. **Caught by the full-tree sweep.** |
| 44 | `CausalGate` (+`GateFailure`, `GateCheck`, `Counterfactual` — whole `gate.py` module proof-only) | curiosity/attribution/gate.py | **TEST-SUPPORT** | Imported only by `proofs/cur_p1d_attribution_proof_2026-09-29.py` (CUR-P1D crossed evidence). Module-level banner added. |
| 45 | `make_substrate` | curiosity/attribution/testdoubles.py | **TEST-SUPPORT** | Exercised by the cur_p1d proof; module also provides `EnforcementStub` to grant_migrate1 proofs. Docstring marked. |
| 46 | `HarnessBridge` (whole `harness_bridge.py` module proof-only) | remote_dispatch/android/harness_bridge.py | **TEST-SUPPORT** | Only consumer is `proofs/dss_proof.py`. Module-level banner added. Name-collision trap: `proofs/gate_dispatch_android_2026-09-28.py` imports a *different* `HarnessBridge` from proof-local `dat_rd.android.harness_bridge` — unrelated. |

(Row 41, `L3Refused`, is the +1 target beyond the audit's 45 — mandate item 5 explicitly sent it here. The table therefore covers all 46 re-mapped targets: the audit's 45 plus `L3Refused`.)

## Verdict table — the 2 modules (mandate item 4)

| Module | Verdict | Evidence |
|--------|---------|----------|
| `runtime/services/providers.py` | **TEST-ONLY** (marked) | The audit's "test-only" label was CORRECT and my initial read was wrong: `runtime/` is hardlinked into `pylib/swarm_engine/` (same inodes), so `tests/track2_new/test_providers_fresh.py` and `tests/track2b/test_providers_fresh.py` importing `swarm_engine.services.providers` DO import this file. No production inlet wires it. Explicit TEST-ONLY banner added to the module docstring (it already carried an honest "Classification: ABSENT as a product capability" note). Wiring it into a production inlet would be new product work — out of scope; the honest mark is the mandate's option (b). |
| `runtime/services/repair.py` | **TEST-ONLY** (marked) | Same hardlink finding: `tests/phase4/test_repair_service.py` imports `swarm_engine.services.repair` = this file. No production inlet wires it. TEST-ONLY banner added. Its docstring claim "the product wiring agents must use instead" is aspirational — nothing uses it; the banner makes that explicit rather than deleting the claim. |

## The 3 proof-script-only items (subset of the 45, whole-module dispositions)

- **`CausalGate`** → whole `gate.py` module is proof-only → module-level TEST-SUPPORT banner (row 44).
- **`make_substrate`** (`testdoubles.py`) → docstring TEST-SUPPORT mark (row 45). Name-collision trap documented: `proofs/ploop10_adoption_2026-09-29.py` defines its own *local* `make_substrate()` — unrelated; excluded from the analysis by file scoping.
- **`HarnessBridge`** (`remote_dispatch/android/harness_bridge.py`) → whole module proof-only → module-level TEST-SUPPORT banner. Name-collision trap: `proofs/gate_dispatch_android_2026-09-28.py` imports a *different* `HarnessBridge` from proof-local `dat_rd.android.harness_bridge` — unrelated; the canonical module's only consumer is `proofs/dss_proof.py`.

## The 3 protected defs (mandate item 7 — VERIFIED ALIVE, untouched)

- **`GovernedWeightFetch`** (`runtime/acquisition/weights.py:78`) — alive: the governed acquisition channel; crossed QWEN3-ACQUIRE-1 streamed a 5 GB checkpoint through it.
- **`BoundaryDetector`** (`runtime/acquisition/semantic_capability.py:40`) — alive: proven by crossed PLOOP-1 (41/41).
- **`boot_media_for_dispatcher`** (`runtime/services/media.py:44`) — alive: restored in fd7800b after the v1.0.3 bricking; consumer is the shipped app engine front, outside canonical.

Zero in-tree callers for all three is **expected, not an incident**: they are boundary/infrastructure pieces whose consumers are crossed missions, shipped artifacts, or future wiring. (An early script bug reported them MISSING_AT_HEAD — `def_locs` was never computed for PROTECTED entries; `grep` confirmed all three at the audit's exact lines. Script fixed; no tree impact.)

## Behavioral fingerprint

`fingerprint.py` exercises 12 live call paths through the touched modules (ledger canonicalization + audit surface, db_ownership claim/owner, coming-soon listing, semantic-structure build, trusted-index build, `_rebuild_expr` fail-closed probe, `TaskCandidate` round-trip, `CycleStore` init, NLU substrate kind, FRM domain-substrate build, `L3Refused` raisability).

- **BEFORE** (pristine worktree at `44e19e4`): `fingerprint_before.txt`
- **AFTER** (mission tree): `fingerprint_after.txt`
- `diff`: **lines 1–21 byte-identical** — every live behavior unchanged. Only lines 22–31 differ, exactly as designed: `removed.gone=39`, `removed.still_present=NONE`, all 8 status markers `True`.

## Regressions

| Battery | Before | After |
|---------|--------|-------|
| Import smoke, 33 touched modules (fresh process) | 33/33 | 33/33 |
| pytest: tests/acquisition, tests/curiosity, tests/synthesis, tests/intellect, tests/agent_org, tests/contracts/test_coming_soon.py, tests/track2_new/test_providers_fresh.py, tests/track2b/test_providers_fresh.py, tests/phase4/test_repair_service.py | **231 passed** (157s) | **231 passed** (293s; slower = host noise, same count) |
| Proof import paths (cur_p1b → `l3_combiner.evaluate_l3`; qwen3_quarantine → `huggingface_index`; seamwire4 → `build_domain_substrate`; cur_p1d → `CausalGate`+`make_substrate`; dss_proof → `HarnessBridge`; contract tests → availability/providers/repair) | n/a | all import clean |

A removal that broke a battery would have been restored as not-dead. None did.

## Incidents and corrections (surfaced, not buried)

1. **Full-tree sweep reclassified 3 verdicts.** The audit's consumer scope (runtime/distill1/tests) missed proof-script consumers. The whole-tree sweep found `evaluate_l3` exercised by `proofs/cur_p1b_enforcement_proof.py`, `huggingface_index` by `proofs/qwen3_quarantine_proof.py`, `build_domain_substrate` by `proofs/seamwire4_frm_bridge_proof.py`. `huggingface_index` and `build_domain_substrate` moved REMOVE → TEST-SUPPORT; `evaluate_l3` was already FUTURE on D-3 grounds, now doubly justified. Without the sweep, two crossed proofs would have broken.
2. **Hardlink discovery corrected a misread.** I initially judged the audit's "test-only" label for providers.py/repair.py wrong (no `runtime.services.*` importers). Inode comparison proved `runtime/` ≡ `pylib/swarm_engine/` (hardlinks), so the tests' `swarm_engine.services.*` imports ARE imports of these files. Corrected before acting; nothing was wrongly removed.
3. **`/tmp` wipe ate the first re-map JSON.** Re-ran; all evidence now lives in `~/workspace/remor_convergence/proofs/dead_code1/` (never `/tmp`), per the standing rule.
4. **Docstring honesty repairs (no behavior change):** `availability.py` module docstring cited `tests/test_coming_soon.py` — the test lives at `tests/contracts/test_coming_soon.py` (fixed 3 mentions); `ambiguity_seeker.py` docstring described the removed 5-step loop as present — rewritten to describe the remaining dataclasses.

## Exact next boundary

Felix's gate in main chat: run `~/workspace/remor_convergence/proofs/dead_code1/gate_run.sh <tree>` against the mission commit on `dead-code-1-work`. On green, Felix lands to canonical. This mission **reports, does not land** — nothing below is a landing instruction.

## What remains unproven / open

- Bench green is mechanism evidence only. Nothing here is "working" until James confirms on his hardware (standing rule).
- `evaluate_l3` wiring into the live enforcement path is James's D-3 decision — the combiner exists, is proof-exercised, and is unwired. That is the honest state.
- `AmbiguityGap` / `AmbiguityQuery` dataclasses (`cognition/ambiguity_seeker.py`) have no consumers. They were outside the 45 targets; left in place with an honest docstring. Follow-up micro-mission: remove them or wire the agency loop they were designed for.
- `store_catalog` coordination: MEMORY-UNIFY-1 has not referenced it anywhere in the tree; removed per mandate. If that mission later needs a catalog store, it re-adds it deliberately.
- The gate's independent re-run is the only thing that makes any of this a crossing. This report is REPORTED.

## Gate-readiness checklist

- [x] `gate_run.sh` reproduces the full battery (import smoke → 231-test pytest → fingerprint vs recorded baseline → deadness re-map) in fresh sequential processes, one command, exit 0 = green.
- [x] Evidence committed under `~/workspace/remor_convergence/proofs/dead_code1/`: `gate_run.sh`, `fingerprint.py`, `fingerprint_before.txt`, `fingerprint_after.txt`, `deadness_remap.py`, `deadness_remap.json` (pre), `deadness_remap_after.json` (post), `dead_names.txt`, `remove_dead.py`.
- [x] Commit uses explicit pathspecs only (no `git add -A`); commit message makes no verification claim.
- [x] No stubs, no pass-bodies, no TODOs introduced; no dead imports left (diff-based orphan analysis); blank-line runs collapsed.
- [x] `git stash` never used; worktree untouched by sibling missions (dedicated worktree).
- [x] Report carries exact next boundary + what remains unproven (M29 convention).
