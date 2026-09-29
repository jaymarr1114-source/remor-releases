# PLOOP-4 REPORT — Unified-memory writer migration + intent.db bridge

Date: 2026-09-29. Worktree: `~/workspace/ploop-4-work`, branch `ploop-4-memory`,
based on canonical `6d412ce`. Mission ran as an independent lane per James's
2026-09-28 correction (not gated on PLOOP-1).

## Objective (end-state, from the dispatch)

Close Primary-loop audit item #4: every runtime memory write goes through the
unified-memory facade; the direct bypass at `runtime/acquisition/ingest.py:328-338`
closed; `intent.db` bridged (no longer a "private engine-schema copy"); zero
facade-bypassing writers proven by complete code scan; real facade
write → persist → read-back proven for every memory class including
intent-backed intents.

## What was done

**1. Facade expansion (inventory-first, no duplication).** The existing facade
(`runtime/intellect/unified_memory.py`) only had a write path for experience
records (`record_experience`). A complete structural scan found **30 direct
EpistemicStore write call sites** across 9 files — the audit's cited
`ingest.py` gap was not the only bypass. Rather than a second store or second
capture path, the facade was expanded with three functions mirroring the
`record_experience` pattern:

- `record_evidence(epistemic, origin_loop, kind, evidence, causal_chain=None)`
- `record_hypothesis(epistemic, origin_loop, kind, hypothesis, causal_chain=None)`
- `record_experiment(epistemic, origin_loop, kind, experiment, causal_chain=None)`

Design: each takes the caller-constructed domain object (callers own the domain
fields and the competition/engine code re-saves hypotheses after mutation —
arbiter verdicts, rival lists — so a field-values API would have forced
restructuring), stamps the canonical provenance block additively, and persists
via the frozen store API. Shared `_canonical_provenance()` helper extracted
(`record_experience` refactored onto it, behavior identical). All three
attached as `UnifiedMemory` methods, mirroring the existing pattern.

Provenance placement (documented in `_stamp_provenance`): hypotheses and
experiments carry the block merged at the top level of their dedicated
`provenance` field (that field already IS a provenance dict); observations and
evidence nest it under the reserved `_provenance` key in the generic
raw/content payload. Caller provenance keys are preserved; canonical keys win
on collision.

**2. All 30 bypass sites migrated** (gaps.py 1, ingest.py 3, loop_driver.py 5,
distill.py 1, acceptance.py 1, engine.py 13, competition.py 6). Return types,
idempotency (stable ids, INSERT OR REPLACE), and never-break-the-loop
try/except behavior preserved. Two real semantic hazards caught during
migration and fixed, not papered over:

- `acceptance.py` `NearMiss.from_dict` did `NearMiss(**d)` — the facade's
  additive `_provenance` key would have crashed `near_misses_for_goal`.
  `from_dict` now drops the envelope key.
- `epistemic.py` `Experiment` had no `provenance` field; added with a
  default so pre-cutover rows still load (`from_dict` does `Experiment(**d)`).

**3. intent.db bridged.** The catalog entry was stale (owner path
`runtime/agent_org/intent_dispatch_service.py` does not exist; actual
implementation is `runtime/services/intent_dispatch_api.py` — corrected).
`run_census(engine_db_path, intent_db_path=None)` now verifies the bridge when
a path is supplied: every table in intent.db must fall within the engine
schema (union of engine-claimed tables) — a schema copy with no private
tables. A table outside the schema lands in `unclaimed_tables` and fails the
census (`ok` is falsy). Absence is not a defect (the service boots intent.db
lazily). No rows are copied anywhere: the bridge asserts schema-conformance,
not a second source of truth. The service's single-thread/SQLite constraints
are untouched (the census only opens the file read-only via sqlite3).

**4. Proof.** `proofs/ploop4_memory_proof.py` (13 checks, all real components,
no mocks) + `proofs/ploop4_memory_proof.log` (13/13 PASS):

- A1–A3: structural scan over all of `runtime/` for the six write-API call
  patterns plus indirect `getattr` calls (the old distill.py pattern) — zero
  hits outside the two narrowly justified exclusions (the store defines the
  API; the facade is its single authorized caller). Vendored non-UTF8 files
  are still scanned (latin-1 fallback).
- B1–B5: real facade write → persist → read-back for observations, evidence,
  hypotheses (including the mutate-and-resave arbiter pattern), experiments,
  and the `UnifiedMemory` method attachments — against a real EpistemicStore
  on a temp DB.
- C1: the additive block is inert to the real `EvidenceArbiter` (supporting
  evidence raises confidence, refuting evidence lowers it).
- D1: a pre-cutover experiment row (JSON with no `provenance` key) still loads.
- E1–E3: intent.db bridge — positive (real intent.db: present, zero tables
  outside schema), negative control (rogue table flagged as
  `intent:rogue_private_store`), absent file (not a defect).

Existing suites re-run green on the final tree: 64 passed
(`test_unified_memory.py` + `test_delta_capture.py` 35, `tests/acquisition/` 21,
`test_acceptance_loop.py` 8).

## Exact next boundary

The commit is on the mission branch `ploop-4-memory` in the worktree only.
Landing into canonical is the next boundary: it needs the independent gate
re-run (Felix's standing rule — coordinators don't verify their own work)
against the landing HEAD, plus James's canonical-tree guardianship order
(2026-09-28 standing order: only Felix lands to canonical after inspection).

## What remains unproven

- Gate re-run of the proof battery and the 64-test subset on the landing HEAD
  (this mission's runs were on the worktree HEAD).
- Production behavior: the migrated writers are proven at the unit/proof
  level; no production loop run has exercised them end-to-end (no
  RunController runs in production per audit item #3).
- `intent.db` bridge proven against a real service-created intent.db file
  (the proof uses a store-initialized file with the identical full engine
  schema; the service's lazy boot path itself was not executed).
- Other cataloged stores (EvidenceStore, capability store, planner tables)
  keep their own store write APIs under census governance — the facade
  unifies the epistemic memory classes (observations, evidence, hypotheses,
  experiments). If James wants literally every store write behind one API,
  that is a larger, separately-scoped mission; the audit item as written is
  about the epistemic writers and intent.db.

## Incidents / notes for the record

- One bad edit mid-mission (dropped a `verdicts = []` line in engine.py);
  caught immediately by re-reading the hunk and repaired in the next edit.
  No other defects.
- `distill.py`'s bypass used an indirect `getattr(self.epistemic,
  "save_observation")` call — invisible to naive greps for `.save_observation(`.
  The proof's scan includes the getattr pattern so this class of bypass can't
  hide again.
- Commit message carries no verification claims (standing rule).
