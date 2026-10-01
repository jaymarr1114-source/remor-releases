# DELTA-NAME-1 — Final Report

**Disposition: REPORTED. Do not land.** Worktree `~/workspace/worktrees/delta-name-1`, branch `delta-name-1-work`, at canonical HEAD `d6e72c9`.

## The two definitions

| | `runtime/acquisition/delta.py:53` (M2) | `runtime/acquisition/ingest.py:105` (M1) |
|---|---|---|
| Schema | Rich: objective, external_actions, prior_capability, capability_gap, technique, evidence, dependencies, verification, synthesized_capability, delta_id, source, at | Single-letter: delta_id, X, Y, Z, gap, T, E, D, V, C |
| Validation | `validate()` with causal discipline (raises DeltaValidationError) | None (only `as_dict()`) |
| Consumers | distill.py, distill_driver.py, gaps.py, loop_driver.py, boundary.py (`isinstance` check), proofs | **Zero external importers**; used once internally (line 318), immediately serialized |

**Canonical: M2's `delta.py` DeltaRecord** — live consumers, real validation, provenance. Per the packet's merge discipline (when in doubt, DROP the scaffolding version), the M1 class was removed.

## The change (not a naive deletion)

The M1 class could not simply be deleted because `loop_driver.py`'s `_adapt_m1_to_m2` adapter reads the **persisted single-letter dict schema** from the epistemic store. The class was a dataclass used once as a dict-builder; the frozen persisted schema is a separate concern from the class-name collision.

- `ingest.py`: deleted the `DeltaRecord` dataclass; added `_m1_delta_dict()` returning the identical dict. Single construction site migrated; `.as_dict()` call removed (already a dict).
- `loop_driver.py`: updated `_adapt_m1_to_m2` docstring ("two DeltaRecord classes are distinct by design" → one class + frozen dict schema); dropped the now-meaningless `M2Delta` alias.
- `tests/acquisition/test_ingest.py`: removed the unused `DeltaRecord` import (it was imported but never referenced).

## Gate evidence (`proofs/delta_name1/gate_run.sh` — 5/5 PASS)

- **b1**: old `DeltaRecord(...).as_dict()` vs new `_m1_delta_dict(...)` — 3 representative cases (normal, empty, unicode/edge) byte-identical JSON (sha256 match), key order identical.
- **b2**: exactly one `class DeltaRecord` in runtime/pylib/tests — confirmed `runtime/acquisition/delta.py:53`.
- **b3**: ingest module imports cleanly, `_m1_delta_dict` present, `DeltaRecord` absent.
- **b4**: full `tests/acquisition/` suite — 21 passed.
- **b5**: `_adapt_m1_to_m2` with an M1-schema dict produces a real M2 `DeltaRecord` (isinstance verified).

## Incidents

None. No protected-tree touches. No 8B inference run (per constraint). No live code depended on the removed class.

## Exact next boundary

The persisted M1 single-letter dict schema is still a **second delta schema** (frozen, consumed by the adapter). A future mission could migrate ingestion to emit M2-schema records directly — but M2's `validate()` requires ≥4 I/O evidence examples at construction, which ingestion-time records don't have yet. That's why the adapter exists. Unifying the *schemas* (not just the class names) requires solving the evidence-timing problem first. Named, not started.

Also note: `delta_capture.py` writes `technique_delta` records in a **third** schema (charter: `technique_t`, `objective_x`, ...), consumed by `distill_driver.charter_to_delta_record`. Three persisted delta schemas, one class — the class collision is fixed; schema convergence is future work.

## What remains unproven

- Full end-to-end ingestion→distillation→promotion run with the new builder (the adapter unit check + 21 ingest tests cover the seam; the full loop needs 8B inference, blocked on host contention).
- Felix's independent gate re-run.
