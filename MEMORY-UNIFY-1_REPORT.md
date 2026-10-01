# MEMORY-UNIFY-1 — Final Report

**Disposition: REPORTED. Do not land.** Worktree: `~/workspace/worktrees/memory-unify-1`, branch `memory-unify-1-work`, at canonical HEAD `832cb1d`.

## James's directive (U-9, 2026-10-01 ~09:14 EDT)
"Wherever it seems to, you can retain what's usable. Kick what's not." Inventory-and-surgery, not blind adopt or blind delete.

## The finding

`runtime/intellect/unified_memory.py` was not "an un-adopted class." It was **two different things in one file**:

1. **A live unified write/read path** (module functions): `record_experience`, `read_experiences`, `record_evidence`, `record_hypothesis`, `record_experiment` — used by 15+ files across acquisition, generalization, intellect, services, and core. This IS the "one unified memory" v10 needs, and it's already adopted.

2. **An un-adopted query facade** (the `UnifiedMemory` class): `query()`, `trace_delta()`, `trace_capability()`, `similar_experiences()`, `prior_attempts()`, `z_check()` — instantiated only in its own test. No production caller. No live equivalent (nothing else does cross-store query).

The class was not a second memory system — it was a query interface over the same stores. But it was a dead class standing beside live functions.

## Per-component classification

### ADOPT (already live, verified, kept as-is)
| Component | Evidence |
|-----------|----------|
| `record_experience` | 15+ importing files; THE unified write path |
| `read_experiences` | 4 files (distill_driver, generalize_driver, generalization/controller) |
| `record_evidence` | 3 files (ingest, engine, competition) |
| `record_hypothesis` | 5 files |
| `record_experiment` | 2 files |
| `attempt_z` | acquisition loop Z-check (loop_driver, ingest) |
| `evidence_from_demo_actions` | acquisition loop (ingest, loop_driver) |
| `record_distillation_experience` | acquisition loop (loop_driver, delta_capture) |
| `ZCheckResult` + Z_* constants | attempt_z contract |
| `PROVENANCE_KEY`, `_canonical_provenance`, `_stamp_provenance` | write-path infrastructure |
| `EXPERIENCE_SCHEMA`, `ORIGIN_*` | provenance constants |
| `STORE_CATALOG`, `store_catalog`, `run_census` | audit tool (proven in PLOOP-4) |

### KEEP-AS-HELPER (extracted from class → plain module functions)
| Old method | New function | Notes |
|------------|--------------|-------|
| `UnifiedMemory.query` | `query_memory(epistemic, capabilities, registry, question, limit)` | Cross-store lexical query |
| `UnifiedMemory.trace_delta` | `trace_delta(epistemic, delta_id)` | Delta diagnostic trace |
| `UnifiedMemory.trace_capability` | `trace_capability(epistemic, capabilities, capability_id)` | Capability diagnostic trace |
| `UnifiedMemory.prior_attempts` | `prior_attempts(epistemic, objective)` | Z-check history |
| `UnifiedMemory.similar_experiences` | `similar_experiences(epistemic, objective, top_k)` | Distillation experience retrieval |
| `UnifiedMemory.z_check` | `z_check_with_experience(objective, evidence, epistemic, registry, planner)` | attempt_z + prior experiences |
| `UnifiedAnswer` | `UnifiedAnswer` (unchanged) | Return type for query_memory |

Supporting private helpers (`_search_epistemic`, `_search_capabilities`, `_search_primitives`, `_delta_id_of`, `_resolve_links`, `_raw_of`, `_tokens`, `_overlap_score`, `_json_norm`, `_values_equal`) kept as module-private functions.

### REMOVE
| Component | Reason |
|-----------|--------|
| `UnifiedMemory` class definition | Zero production instantiations; only its own test used it |
| `__init__` | Goes with the class |
| Monkey-patched method attachments (`_um_record_experience`, etc.) | Thin wrappers around module functions; module functions are what's used |
| `UnifiedMemory.census` attachment | Proof uses module-level `run_census` directly |
| Unused `UnifiedMemory` import in `test_delta_capture.py` | Was never used (import-only) |

## What changed (files)
- `runtime/intellect/unified_memory.py`: class removed (-302 lines), query/trace logic extracted as 12 module functions (+~280 lines), module docstring updated to describe the three mechanisms accurately. Net: -4,635 chars.
- `tests/intellect/test_unified_memory.py`: `UnifiedMemoryTests` → `QueryHelperTests`, uses module functions. All 15 tests pass.
- `tests/intellect/test_delta_capture.py`: removed unused import. 13 tests pass.
- `proofs/ploop4_memory_proof.py`: B5 check now verifies module functions directly (was testing class method attachments).

## Gate evidence (`proofs/memory_unify1/gate_run.sh` — ALL BATTERIES PASS)
- **b1** (17/17): inventory — every ADOPT function has production importers; every helper exists as module function; zero `class UnifiedMemory` definitions; zero instantiations tree-wide.
- **b2** (8/8): live write/read path — record/read with provenance filtering, additive provenance, all four record types persist.
- **b3** (9/9): extracted helpers — query_memory reaches all three stores; trace_delta/trace_capability find records; similar_experiences retrieves; z_check_with_experience attaches prior experiences.
- **b4** (2/2): no regressions — test_unified_memory.py (15 passed), test_delta_capture.py (13 passed).

**Total: 36 checks, all green.**

## Incidents
None. The surgery was clean — no production code referenced the class, so removal required no caller updates.

## Exact next boundary
The extracted query helpers (`query_memory`, `trace_delta`, etc.) are tested and working but have no production caller. They are diagnostic utilities, not engine paths. If a future mission needs "what does REMOR know about X?" (e.g., James asking the system about its memory), these are the building blocks. If they remain uncalled, a future DEAD-CODE sweep may remove them — but per James's "retain what's usable," they stay for now.

The three persisted delta schemas (M1 dict, M2 record, charter technique_t) remain un-unified — noted as the next boundary by DELTA-NAME-1, out of scope here.

## What remains unproven
- No 8B inference was run (host constraint respected; not needed for this inventory-and-surgery mission).
- The query helpers are proven in isolation and through real stores, but not "through the engine's real query path" — because the engine has no query path that uses them. This is honest: they are available, not adopted.
- Felix's independent gate re-run — this report is the coordinator's claim, not a crossing.
