# EVIDENCE-WIRE-1 — Mission Report

**Status:** REPORTED — not landed. Awaiting Felix's independent gate.
**Worktree:** `~/workspace/worktrees/evidence-wire-1` (branch `evidence-wire-1-work`)
**Date:** 2026-10-01

## Objective (James's design, 2026-10-01)

The Evidence view becomes the live verified exchange between Curiosity and
Creativity: verified items are usable substrate; unverified items are
quarantined — visible and labeled, fenced, never built on — until the gate
verifies them.

## What was actually wrong (corrected mid-mission)

The packet assumed the Evidence view reads the Contract 8 `EvidenceStore`
(`/api/evidence/*`). It does not. The GUI's `renderEvidence()` calls
`/api/epistemic/observations` and `/api/epistemic/hypotheses`, which read the
**EpistemicStore**. The `EvidenceStore` is a parallel island the view never
touches. Wiring `evidence_store` alone would not have put a single
observation on James's screen.

The real chain: `ingest → EpistemicStore.observations → /api/epistemic/observations → view`.
That path existed but had (a) no verification status anywhere, (b) no
quarantine fence on substrate consumption, and (c) the `evidence_store`
mirror genuinely unplugged (default None).

## Changes (all in worktree, canonical untouched)

1. **`pylib/swarm_engine/intellect/epistemic.py`** (hardlinked to `runtime/`):
   - `Observation` / `Hypothesis`: `verified: bool = False`,
     `verified_by: Optional[str]`, `verified_at: Optional[float]`; in `as_dict()`.
     JSON-column storage means no schema migration; old rows default to
     unverified (quarantined) — the safe default.
   - `verify_observation()` / `verify_hypothesis()`: attested verification
     (verifier named).
   - `QuarantinedSubstrateRefused` exception; `substrate_observation()` /
     `substrate_hypothesis()`: the ONLY substrate path; raises a NAMED
     refusal on unverified entries, never a silent skip. Display paths
     (`all_observations`, the GUI) deliberately bypass the gate — they show
     quarantined entries labeled.

2. **`runtime/services/evidence.py`** (Contract 8 store):
   - `verified` / `verified_by` / `verified_at` columns with additive
     migration for pre-existing DBs.
   - `verify_entry()`, `get_substrate_entry()` (named quarantine refusal),
     `list_substrate()` (verified-only + quarantined count).
   - New routes: `POST /api/evidence/verify`,
     `POST /api/evidence/substrate/get`, `POST /api/evidence/substrate/list`.

3. **`runtime/acquisition/loop_driver.py`**: `CognitionLoop.epistemic` is now
   a lazy property — construction at boot no longer forces engine boot
   (thread-affinity preserved).

4. **`runtime/services/http_adapter.py`**: boot constructs
   `CognitionLoop(engine=lazy_engine, evidence_store=evidence)` and registers
   it as `services["cognition_loop"]`. The loop driver now receives the real
   evidence store, not None.

5. **Tests**: `tests/backend/test_evidence.py`,
   `tests/contracts/test_evidence.py` route-count assertions updated 5 → 8
   (3 new routes added).

## Proof

`proofs/evidence_wire1/gate_run.sh` → `proof_battery.py`: **20/20 PASS**.
- P1: loop receives the real evidence store (not None).
- P2: real ingest of a genuine gap (`gap_recorded`) lands 1 observation in
  the evidence store AND observations in the epistemic store (the view's
  backend), unverified by default, carrying the GUI's expected fields
  (`content`, `source`, `at`) plus `verified`.
- P3: verification attested with named verifier; visible in `get_entry`,
  `as_dict`.
- P4: unverified → `QUARANTINED_SUBSTRATE_REFUSED` (named, never silent);
  verified → flows; `list_substrate` verified-only with quarantine count;
  display path still shows quarantined entries labeled.
- P5: construction does not boot the lazy engine.
- P6: legacy `evidence.db` migrates additively, no data loss.

Regression: `tests/backend/test_evidence.py` + `tests/contracts/test_evidence.py`
28/28; `tests/acquisition/test_ingest.py` 9/9; `test_q5_ingestion.py` 12/12.

## Exact next boundary

**Frontend (Gui Frontend track):** the API now returns `verified`,
`verified_by`, `verified_at` on every observation/hypothesis from
`/api/epistemic/observations` and `/api/epistemic/hypotheses`. The view's
`renderEvidence()` in `gui_src/app/static/app.js` must render verified vs
quarantined distinctly (badge/label + fenced styling for unverified). ~5
lines of JS; the contract is stable and proven above. Until that lands,
James sees observations but no visual quarantine distinction.

## What remains unproven

- The view rendering (frontend, not this mission).
- A production drive of `services["cognition_loop"].ingest_demo()` through
  the real executive (the inlet `drive_cognition` still has no caller —
  pre-existing, out of scope).
- Which verifier attests in production (gate policy decision for James:
  the gate, a loop, or him).
- Device proof on James's phone (standing rule).
