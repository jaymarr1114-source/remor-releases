# CUR-P5A mission report — Self-generated objectives (2026-10-04)

**Mission:** curiosity-initiated work (Phase 5, chunk 5a): curiosity originates
inquiries with no Primary permission required and gains no Primary authority
by doing so (C-1.3).
**Worktree:** `~/workspace/worktrees/cur-p5a`, branch `cur-p5a`,
pinned at canonical `84b2a80` (verified 2026-10-04, fail-closed).
**Battery:** `proofs/cur_p5a/gate_run.sh` → `cur_p5a_proof.py`,
**36/36 checks green** (fresh process, real machinery throughout, stable
across three runs from a clean state).

## Per-mandate verdicts

| # | Mandate | Verdict |
|---|---------|---------|
| 1 | Fresh structural re-map before execution | **PROVEN** — pin verified fail-closed; C-1.3/C-1.4/C-1.5 read from the charter; executive origin handling (`ORIGINS = ("PRIMARY_REQUESTED", "CURIOUSITY_INITIATED")` — frozen misspelling used as-is); FRM grant flow; questioning end-to-end chain; triage vocabulary (`retain`/`propose_capability`/`propose_investigation`/`boundary`, advisory-only). |
| 2 | Initiation wiring as NEW files only | **PROVEN** — `runtime/curiosity/initiated/` (`__init__.py`, `ledger.py`, `initiate.py`, `lineage.py`): `mint_initiated_trigger()` validates fail-closed, mints the append-only `InitiationLedger` record, returns the trigger; `verify_initiated_lineage()` inspects the real trigger → ledger → inquiry-result chain. Zero frozen files touched. |
| 3 | No-authority proofs, executed not asserted | **PROVEN** — (a) curiosity `register_loop("run")` → ValueError naming the Primary side; `spawn("run")` → `loop_unregistered` refusal (T04). (b) Primary `register_loop("questioning")` → ValueError; `spawn("questioning")` → `loop_unregistered` (T05). (c) `set_verdict` → AttributeError; tree grep finds no acceptance-record writer in `runtime/curiosity` (T06). (d) `ExecutiveController.close_boundary` → AttributeError; no `close*` API at all (T07). (e) grant mutation → `FrozenInstanceError` (frozen dataclass); budget unchanged (T08). (f) Primary `retire()` of a live curiosity mc → `unknown_microcontroller` refusal (T09). |
| 4 | Provenance honesty | **PROVEN** — forged `PRIMARY_REQUESTED` on a minted trigger → `OriginForged` via the ledger cross-check; unminted initiated claim → `OriginForged` (T10). Initiated vs Primary-requested origins stay distinct in the ledger and the trigger. |
| 5 | Findings land triaged | **PROVEN** — end-to-end initiated inquiry lands `triage=propose_investigation` on the fenced-store finding (T11). Never un-triaged; never auto-admitted (admission is P5B/P5C's domain). |
| 6 | Battery discipline | **PROVEN** — gate_queue.md checked (no competing battery); uncontended; halt-on-first-fail. |
| 7 | gate_run.sh | **PROVEN** — 36/36, exit 0, one fresh invocation; merge-base ancestry pin check; no hardcoded home paths. |
| 8 | Report-don't-land | **PROVEN** — neutral commit message, no verification claims; canonical untouched. |

## End-to-end demonstration (T03)
Initiated trigger (`imprecise_question`, origin `CURIOUSITY_INITIATED`) →
`request_activation` approved → own FRM grant round (CountingFRM: 1 round,
60.00s) → `activate` → questioning → `QUESTION_RESOLVED` → provenance-stamped
finding in the fenced Evidence Store → lineage verified (trigger_id →
initiation record with `primary_request_id=None`, `livepath_involvement=False`).

## Structural guarantees (not just tests)
- `runtime/curiosity/initiated/` has no Primary-side import statement in any
  module (T01, per-module source check) and the `LivePath` module is never
  imported by the mint path (T01, T12).
- The FRM grant is a frozen dataclass: escalation is refused by the language
  runtime, not by convention (T08).

## Incidents
1. Three wrong import paths in the first battery draft (`frm.manager`,
   `rollcall.policy`, `rollcall.attestation`, local `HonestTestDouble`
   shadowing) — fixed against the real tree; an index-based edit accidentally
   deleted `new_stack` and it was restored. Test bugs, not mechanism.
2. Naive source grep for "runtime.core" tripped on the package's own
   docstrings; rewritten as import-statement checks (T01/T12). Test bug.
3. `CuriosityEvidenceStore.read` does not exist — the API is `.get()`.
   Test bug, not mechanism.
4. No frozen files written; no parallel batteries; no claims beyond what the
   battery demonstrates.

## What remains unproven
- Triage semantics (which flag when, and what the flags authorize): CUR-P5B's
  mandate. This mission proves findings land triaged, not the triage policy.
- Relevance fencing in Primary Acceptance (C-3.2/A24): CUR-P5C's mandate.
- Initiated inquiries through the non-questioning loops: follows the pending
  P3B-INT/P3C-INT integrations; this mission is scoped to questioning (the
  only loop with a proven end-to-end chain in canonical).

## Exact next executable boundary
**Gate + landing of branch `cur-p5a` in main chat** — Felix's independent
re-run of `proofs/cur_p5a/gate_run.sh` against the landing HEAD. After
landing: **CUR-P5B — first-pass triage** (RETAIN / PROPOSE_CAPABILITY /
PROPOSE_INVESTIGATION / BOUNDARY per C-3.3; triage flags live in the Evidence
Store; triage is not admission).
