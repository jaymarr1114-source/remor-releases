# GRANT-MIGRATE-1 — Mission Report

**Date:** 2026-10-01
**Worktree:** `~/workspace/worktrees/grant-migrate-1` (branch `grant-migrate-1-work`, from canonical `73b38a1`)
**Status:** complete — gate battery 8/8 green in fresh sequential processes. **Report, do NOT land** (landing via main-chat gate).

## Objective
Execute standing decision U-7 (James, 2026-09-30: "FrmGrant wins"): exactly ONE grant
contract in the tree — `FrmGrant` (frozen, `epoch_id: int`) — with every consumer of the
mutable attribution `Grant` migrated, the false "frozen FRM grant shape" docstring repaired,
and the full FRM grant battery passing end-to-end.

## What was done

**Re-map (mandate 1):** The attribution `Grant` had exactly five consumers: `grants.py`
itself (definition + `AllocationLedger` + `_grant_from_row`), `testdoubles.py`
(`InquiryDriver.allocate` annotation), `gate.py` (`AllocationLedger` only — no `Grant`
use), and two proof scripts (`cur_p1d_attribution_proof_2026-09-29.py`,
`brainscaffold1_proof.py`). `chain.py`/`spend.py` never touched it. `epoch_id: str`
existed ONLY in the attribution package; the rest of the curiosity tree was already
`epoch_id: int`.

**Migration (mandates 2–3):**
- `runtime/curiosity/attribution/grants.py`: deleted the mutable `Grant` class and its
  false docstring. The ledger now records `FrmGrant` directly: `record_grant(grant:
  FrmGrant)` validates via the new `validate_grant()` (isinstance gate + frozen
  six-key `as_dict()` shape assertion + the old allocation invariants: grant_id
  present, `epoch_id` int, `epoch_s` > 0, dimensions non-negative). `expired()` became
  the module function `grant_expired()`. `Allocation.epoch_id: str` → `int`. Schema:
  `epoch_id TEXT` → `INTEGER` in both tables, plus new `domain` and `lending_json`
  columns; `_migrate_schema()` ALTERs pre-migration DBs. `_grant_from_row`
  reconstructs a faithful `FrmGrant` (domain/lending round-tripped;
  `enforcement_state_at_issue`/`note` are round-record data per `as_dict()`'s own
  docstring and reconstruct as defaults; a legacy non-integer `epoch_id` raises
  `AllocationRefused`, never a bare `ValueError`).
- `testdoubles.py`: `allocate()` now takes `FrmGrant`; import updated.
- `__init__.py`: module doc fixed.
- `cur_p1d_attribution_proof_2026-09-29.py`: `make_grant` now issues a real `FrmGrant`
  (deterministic id pinned via `dataclasses.replace`, since `issue()` mints ids); S0e
  expiry is constructed at issue time instead of by post-issue mutation; the hardcoded
  stale `~/workspace/worktrees/cur-p1d` path now derives from the file's location.
- `brainscaffold1_proof.py`: B1's "three grant concepts" → two (the attribution Grant
  was migrated INTO FrmGrant); B4's adversarial `AttributionGrant(...)` replaced with
  a legacy-shaped dict impostor (stronger: something grant-*shaped* that still must
  be refused, not coerced).
- `swarm_engine.primitives.core.Grant` (`PrimitiveGrant`) deliberately untouched: it
  is an effect permission for the primitive governor (Effect + target pattern), not
  an FRM resource grant — a distinct record type, documented as such in `grants.py`.

**Cross-namespace identity hazard (found and fixed during the mission):**
`runtime/curiosity/**` and `pylib/swarm_engine/curiosity/**` are the same files
(hardlinked, same inodes) but import as *different module identities*, so a relative
`from ..frm.grant import FrmGrant` binds a *different class object* depending on which
namespace imported the ledger — and every FrmGrant issuer/consumer in the tree uses
the `swarm_engine` spelling (incl. `frm/evaluation.py` itself). My first cut failed
with the surreal `grant must be FrmGrant, got FrmGrant`. Fix: `grants.py` and
`testdoubles.py` import `FrmGrant` via the absolute `swarm_engine` path (the tree's
de-facto canonical spelling), with a load-bearing comment. Proven by battery b5.

## Evidence (all in fresh processes; `proofs/grant_migrate1/gate_run.sh` reproduces all)

| Battery | Result |
|---|---|
| b1_one_contract (AST: no `Grant` class; false docstring gone; FrmGrant frozen + `epoch_id: int`; PrimitiveGrant distinct+documented) | 4/4 PASS |
| b2_issue_consume_exhaust (record→get round-trip; allocate binds controller, `epoch_id` int; double-record → IntegrityError; unknown grant refused) | 4/4 PASS |
| b4_adversarial (post-issue mutation → FrozenInstanceError; dict/str/PrimitiveGrant/None impostors → AllocationRefused, never coerced or crashed; smuggled str `epoch_id` refused; negative budget, zero `epoch_s`, expired grant, 3 enforcement states, six-key shape assertion) | 8/8 PASS |
| b5_cross_namespace (one FrmGrant identity across both import paths; issuer-spelling grant accepted by runtime-spelling ledger; lending detail round-trips) | 2/2 PASS |
| b6_legacy_db_migration (pre-migration 7-column DB migrates; rows readable as FrmGrant with int epoch; legacy `"epoch-1"` row fails closed with a clear reason) | 2/2 PASS |
| cur_p1d_attribution_proof_2026-09-29.py (existing battery) | 25/25 PASS |
| brainscaffold1_proof.py (existing battery) | 58/58 PASS |
| cur_p1c_frm_proof.py (existing battery, issuance side untouched) | 15/15 PASS |
| **gate_run.sh end-to-end** | **8/8 batteries green** |

## Classification
- "Exactly one grant contract exists; all attribution consumers migrated; false docstring repaired" — **PROVEN** (AST + batteries b1/b2/b5).
- "No cross-feeding hazards; gates accept canonical, refuse legacy honestly" — **PROVEN** (b4/b5/b6 adversarial).
- "All previously-green FRM/attribution batteries still green" — **PROVEN** (25/25, 58/58, 15/15).
- Router/student batteries — **not re-run**: untouched by this change (no router, student, or governance-path edits); classified as unaffected rather than re-proven.

## Exact next boundary
The dual module identity (`runtime.*` vs `swarm_engine.*` hardlinked files creating
two class objects per class) is a tree-wide latent hazard beyond grants: ANY future
isinstance-based gate on a hardlinked class breaks unless all parties use one import
spelling. The convention (swarm_engine spelling) is now documented in `grants.py`, but
nothing enforces it. A follow-up (candidate: IMPORT-HYGIENE-1 scope) should either
enforce a single import spelling tree-wide or make the hardlink relationship explicit.

## What remains unproven
- Landing-HEAD re-proof (Felix's gate) — owed, as always.
- No production caller records into `AllocationLedger` today (only test doubles and
  proofs); the first production wiring will be the real end-to-end proof of the
  migrated ledger.
- `runtime.*`-spelling FrmGrants are refused by the isinstance gate (matches the
  pre-existing `granted_cognition` gate's behavior); if any future code issues via
  the runtime spelling, it will fail closed loudly rather than silently.

## Incidents
- The cross-namespace `isinstance` failure (`grant must be FrmGrant, got FrmGrant`)
  was found by the mission's own smoke test, diagnosed (hardlink dual identity),
  and repaired — no silent workaround; the repair is proven by battery b5.
- No protected trees touched. All writes inside the worktree.
