# CUR-P5C mission report — Relevance fencing in Primary Acceptance (2026-10-04)

**Mission:** prove the C-3.2 fencing around Primary Acceptance: relevance
first (D-4 threshold 0.25, via the real gate as a component of
acceptance), the acceptance bar second; the relevance verdict and the
acceptance verdict separately recorded and separately final; the Primary
Executive unable to flip either.
**Worktree:** `~/workspace/worktrees/cur-p5c`, branch `cur-p5c`, base
canonical `84b2a80` (clean — no stack; verified 2026-10-04).
**Battery:** `proofs/cur_p5c/gate_run.sh` → `cur_p5c_proof.py`,
**54/54 checks green** (fresh processes, real machinery throughout:
real SwarmEngine, real ExecutiveController, real RelevanceGate, real
acceptance inlet via `build_live_path`).

## Per-mandate verdicts

| # | Mandate | Verdict |
|---|---------|---------|
| 1 | Fresh structural re-map | **PROVEN** — pin fail-closed; C-3.2/A24, D-4 grounding, real `RelevanceGate` interface, the Primary Executive's actual override surface, and the `LivePath` delivery path read at landed state before building. |
| 2 | Curiosity side's relevance input, new files only | **PROVEN** — `runtime/curiosity/relevance/` (`__init__.py`, `present.py`): `build_gate_finding` adapts a curiosity finding to the gate's Finding shape with content VERBATIM, provenance VERBATIM (triage advisory), `requested_by_primary` DERIVED from origin (never asserted), and NO score anywhere (the gate judges). `present_for_acceptance` returns the frozen claim record. Zero frozen files touched. |
| 3 | Ordering proof | **PROVEN** — T02: the gate judges first (content-score / provenance-link admit; below-threshold retains; refuted/malformed reject; threshold recorded as 0.25). T03: a below-threshold finding never reaches the outbox — `submit_finding`'s ADMITTED branch is the ONLY writer. T04: the delivery re-check is live — ADMITTED then re-decided RETAINED is NOT delivered ("gate no longer confirms ADMITTED"). T05: ADMITTED + boundary-invalid → delivered=False ("relevance is not acceptance"). T06: ADMITTED + valid boundary → entered via the real acceptance inlet. |
| 4 | Separateness proof | **PROVEN** — T07: relevance decisions append-only (re-decide → new row, old rows untouched, latest wins); the delivery record is a different shape/record from the relevance verdict. |
| 5 | Primary Executive cannot flip (load-bearing) | **PROVEN** — T08: `override_verdict` / `set_verdict` / `flip_decision` do not exist on the executive or the gate (AttributeError ×5); the recorded verdict stands after all attempts. Allowed paths proven working: `set_operational_objective` starts a NEW relevance question (old verdicts intact); re-decide appends a NEW record. T09: the curiosity side has no verdict writer either (neither side flips). |
| 6 | Primary-side gaps | **PROVEN** — none. No missing interface encountered; `runtime/core/` and `runtime/services/acceptance.py` untouched (empty diffs). |
| 7 | Battery discipline | **PROVEN** — gate_queue.md checked (no competing battery), load < 1.0, niced, halt-on-first-fail. |
| 8 | gate_run.sh | **PROVEN** — 54/54, exit 0, one fresh invocation; merge-base ancestry pin check (passes post-commit); no hardcoded home paths. |
| 9 | Report-don't-land | **PROVEN** — neutral commit message, no verification claims; canonical untouched. |

## The two verdicts (what the tree actually implements)

- **Relevance verdict**: `RelevanceGate.decide()` → `RelevanceDecision`
  (admitted/retained/rejected + criterion + score + threshold), recorded
  in the append-only `decisions` table. Threshold 0.25 = James's D-4
  input, consumed, never re-derived.
- **Acceptance verdict**: `LivePath.deliver_admitted()` → per-finding
  delivery record (`delivered` True/False + reason). A finding enters
  Primary machinery only if the gate STILL confirms ADMITTED at delivery
  time, the boundary validates, and the loop inlet enters.
- Below-threshold is RETAINED, not "rejected at relevance" (the tree's
  vocabulary, followed exactly): true-but-irrelevant is kept with its
  terminal state, never promoted.

## Incidents

1. None on the mechanism. Two battery-harness fixes pre-green (a dead
   loop left in T01, a trivially-true assertion in T10 tightened to a
   direct sqlite read of `requested_by_primary`/`triage`).
2. No frozen files written; no parallel batteries; no claims beyond what
   the battery demonstrates.

## What remains unproven

Relevance judgments for findings from not-yet-integrated loops
(creative_exploration, discovery_novelty) — the input package is
loop-agnostic by design; the fence is proven at the gate, which is
loop-independent.

## Exact next executable boundary

**Gate + landing of branch `cur-p5c` in main chat** — Felix's
independent re-run of `proofs/cur_p5c/gate_run.sh` against the landing
HEAD (clean base, no rebase needed). After landing: **Phase 6 —
hardening** (per the track plan).
