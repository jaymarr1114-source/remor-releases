# CUR-P5B mission report — First-pass triage (2026-10-04)

**Mission:** build the unified first-pass triage machinery (charter
C-3.3/C-3.4): every terminal finding from the curiosity side receives
exactly one of RETAIN / PROPOSE_CAPABILITY / PROPOSE_INVESTIGATION /
BOUNDARY by mechanical, reproducible rules, recorded in the fenced
Evidence Store — and triage is NOT admission, proven adversarially.

**Worktree:** `~/workspace/worktrees/cur-p5b`, branch `cur-p5b`,
base `84b2a80` (clean — no stack). **Battery:**
`proofs/cur_p5b/gate_run.sh` → `cur_p5b_proof.py`, **35/35 green**
(fresh processes, real chain throughout).

## What was built

`runtime/curiosity/triage/` (new package, 4 modules):

- `rules.py` — the mechanical rules. Pure functions of finding
  content (terminal_state, payload); same content always yields the
  same (triage, rule_id). Priority: R-BOUNDARY (BOUNDARY_ESTABLISHED
  → boundary) → R-INVESTIGATE (QUESTION_RESOLVED, INSUFFICIENT_EVIDENCE,
  INCONCLUSIVE, BLOCKED → propose_investigation) → R-CAPABILITY
  (well-formed capability_proposal block + capability-eligible terminal
  → propose_capability) → R-RETAIN (default for known terminals).
  Unknown terminal → TriageRefused (fail-closed, never a default guess).
- `ledger.py` — append-only triage event ledger (own sqlite).
  First event carries cause 'first-pass'; re-triage REQUIRES new
  evidence (an evidence_ref not already cited) else TriageRefused.
  `get_triage()` is latest-wins for readers; history only grows.
- `triage.py` — `triage_finding()` (first-pass: rules → ledger event →
  provenance.triage stamped; refuses already-triaged findings),
  `retriage()` (new-evidence path), `persist_triaged()` (fenced write
  through the domain fence; refuses un-triaged findings).
- `__init__.py` — exports. Deliberately NO admit/promote/register API
  anywhere in the package: the prohibition is structural.

## Per-mandate verdicts

| # | Mandate | Verdict |
|---|---------|---------|
| 1 | Fresh structural re-map | **PROVEN** — pin 84b2a80 fail-closed; C-3.3/C-3.4 read; evidence records' triage field; questioning terminal shapes; run-controller payload envelope; the as-built score rule discovered and documented (see below). |
| 2 | Triage machinery, new files only | **PROVEN** — 4 modules, mechanical auditable rules; 755+/0- diff; zero frozen files touched. |
| 3 | All four flags on real findings | **PROVEN** — T01 (QUESTION_RESOLVED→propose_investigation via the real chain), T02 (BOUNDARY_ESTABLISHED→boundary via the real chain), T03 (INSUFFICIENT_EVIDENCE→propose_investigation via the real chain), T04 (capability block+HYPOTHESIS_SUPPORTED→propose_capability; malformed block→retain; refuted+block→retain). |
| 4 | Triage-is-not-admission adversarial | **PROVEN** — no admit/promote/register API (AttributeError ×4); no admission marker on persisted records; no auto-admit consumer in the tree (AST scan); re-triage without new evidence refused, with new evidence appended latest-wins; boundary+capability block still maps to boundary; no acceptance-record writer on the curiosity side. |
| 5 | Advisory flags | **PROVEN** — the only out-of-package triage readers are the field definition (records.py) and Primary Acceptance's relevance reader (core/executive/relevance.py, the DESIGNED consumer per the records docstring) — neither admits. |
| 6 | Battery discipline | **PROVEN** — gate_queue.md checked (no competing battery), load < 1.0, halt-on-first-fail. |
| 7 | gate_run.sh | **PROVEN** — 35/35, exit 0, ancestry pin check, no hardcoded paths. |
| 8 | Report-don't-land | **PROVEN** — neutral commit message, canonical untouched. |

## DELTA vs the as-built score rule (named, not papered over)

The run controller's `_persist_terminal` applies a Phase-2 legacy
heuristic for questioning terminals: propose_investigation iff
precision score ≥ 3 else retain. These rules AGREE on
QUESTION_RESOLVED (both propose investigation — pipeline-faithful:
a resolved question seeds the next inquiry) and DIFFER on
INSUFFICIENT_EVIDENCE (legacy: retain; here: propose_investigation --
open work is not buried) and BOUNDARY_ESTABLISHED (legacy: retain;
here: boundary -- the flag exists precisely for mapped limits). The
scientific_inquiry loop's own _VERDICT_TO_TRIAGE agrees with these
rules on all four of its verdicts. This package stands alone; the
controller is untouched. When the controller is authorized to call
this pass, the score rule is superseded -- recorded here as the
integration note, not smuggled in.

## Honest limits

- The fenced store does NOT validate the triage string against the
  four-value vocabulary (records.py `validate()` checks provenance
  completeness, not triage membership). The guarantee "triage ∈ {4
  values}" holds because this package is the sole assigner and its
  rules only emit the four -- PROVEN BUT BOUNDED (by code ownership +
  battery, not store enforcement).
- The no-auto-admit scan is static (AST over the tree); a future
  module could read triage and act. The structural guarantee is that
  no admission API exists for it to call.

## Incidents

1. Mid-build discovery of the as-built score rule (via live probe, not
   assumption) forced the rule-design re-think documented above. The
   battery's T01 expectation was rewritten to the charter-grounded
   rule, not to the legacy heuristic.
2. Three test-harness bugs in the first battery draft (wrong stack
   import path, naive docstring-tripping greps, `store.read` vs
   `.get()`); fixed against the real tree. Test bugs, not mechanism.
3. No frozen files written; no parallel batteries; no claims beyond
   what the battery demonstrates.

## What remains unproven

Relevance fencing in Primary Acceptance (CUR-P5C); triage of
initiated-origin findings through the P5A path (the pass is
origin-agnostic by design; the combination is unproven); triage
through non-questioning loops (follows P3B-INT/P3C-INT).
