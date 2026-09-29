# PLOOP-7 REPORT — relevance ownership (retry)

**Mission:** implement the Primary side's relevance owner per charter C-3
("a fact can be true and still be irrelevant"). Retry: the previous attempt
ended after inventory with zero commits; the carried-forward finding
(existing relevance machinery is all advisory retrieval/search ranking, no
admission owner) was confirmed and built on directly.

**Worktree:** `~/workspace/ploop-7-work`, branch `ploop-7-relevance`, base `6d412ce`.
No other trees touched.

## What was built

**`runtime/core/executive/relevance.py`** (new) — the `RelevanceGate`, the
Primary executive's relevance owner:
- `Finding` record: content + provenance (source loop, bounded objective id,
  Primary-requested vs curiosity-initiated, advisory triage label) +
  terminal state from the charter's enumerated set. `validate()` enforces
  charter C-2.2 admissibility (raises `FindingRefused`).
- Truth standing derived from terminal state: `HYPOTHESIS_REFUTED` →
  REFUTED; supported states → SUPPORTED; candidates/inconclusive/blocked →
  UNESTABLISHED.
- `OperationalObjective`: the objective relevance is judged against, with
  synonym-expanded token terms (reuses `runtime/acquisition/semantic`,
  the same machinery the memory system uses).
- Decision order: malformed → REJECTED; refuted → REJECTED (kept as
  knowledge, not admitted); provenance link to current/sub-objective →
  ADMITTED; synonym-expanded token recall ≥ 0.25 → ADMITTED with overlap
  terms recorded; otherwise → RETAINED (true-but-irrelevant: kept with
  terminal state, never promoted, never silently discarded).
- Triage is recorded but never decisive (charter C-3.3).
- Every decision persisted to sqlite (findings + decisions tables) and
  re-checkable via `get_decision` / `findings_by_admission`.

**`runtime/core/executive/executive.py`** (edit) — the executive now accepts
an optional `relevance_gate` and exposes the findings inlet:
`set_operational_objective()` / `submit_finding()`. Fail-closed: without a
gate, both raise `RelevanceRefused` naming the absence. No change to
`route()`/`enter()` — boundaries are a separate inlet.

## Proof

`proofs/ploop7_relevance_proof.py` — 20 checks, all green on two consecutive
runs (log: `proofs/ploop7_relevance_proof.log`):
- C1 provenance-link admission; C2 content-score admission (0.300);
  C3 true-but-irrelevant retained; C4 refuted rejected-but-kept;
  C5 relevant-to-a-different-objective retained at this inlet;
  C6/C7 malformed rejected (bad terminal state, missing provenance);
  C8 triage="propose" on irrelevant content still retained;
  C9 threshold edge: score 0.250 admitted vs 0.200 retained — one overlapping
  term flips the verdict;
  C10 persistence re-checked from a fresh sqlite connection (10/10 verdicts
  match; retained rows keep terminal state; refuted finding kept as
  knowledge; retained never promoted);
  C11 determinism across two independent gates (identical verdict/criterion/score);
  C12 real `ExecutiveController` (all six loops ABSENT, its documented
  degraded-state construction) with a real gate: inlet verdicts match, and a
  gateless executive fail-closes on both methods.

**Counts: 20/20 PASS, 0 FAIL, two consecutive runs.**

## Exact next boundary

Admitted findings are eligible for loop entry, but nothing consumes the
admission yet: routing an ADMITTED finding into a specific loop's inlet is
the handoff-contract work (PLOOP-2's territory). The gate decides; the
delivery contract after admission does not exist.

## What remains unproven

- The 0.25 threshold is a documented judgment call (cost asymmetry), not a
  derived constant. Who tunes it in production is James's call.
- Admission against a *changing* operational objective mid-run (objective
  rotation invalidating prior admissions) has no policy.
- No production wiring: no executive is constructed with a gate outside
  proofs; the Primary loop still never turns (PLOOP-1's boundary detection
  is the load-bearing prerequisite).
- Synonym expansion is one-directional-bounded (`expand`); adversarial
  phrasing that dodges the token set would score low. Recorded as a known
  limitation, not a defect for this boundary.

## Stopping condition

Report complete. Code committed in the worktree; nothing staged as verified
beyond what the proof logs show.
