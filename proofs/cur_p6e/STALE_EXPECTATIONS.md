# CUR-P6E stale expectations

Batteries that assert a structural invariant the tree has legitimately
outgrown since the battery was written. The battery is NEVER patched —
it runs unmodified, its log is preserved, and the staleness is
documented here with the causal chain. Each entry names: the battery,
the failing check, what the tree changed, the James-level decision
authorizing the change, and why the security-relevant property still
holds.

## 1. cur_p1a — A5b (2026-10-04)

- **Battery:** `proofs/cur_p1a_evidence_store_proof.py` (CUR-P1A, gated+landed 2026-09-29)
- **Failing check:** `A5b: no non-curiosity runtime module imports runtime.curiosity`
- **Observed:** 18/19 checks pass; the single failure names
  `runtime/creativity/budget.py` (`from runtime.curiosity.frm.grant
  import FrmGrant, issue_run_grant`, `from runtime.curiosity.frm.policy
  import CostInput, CostKind`).
- **What changed:** CREATIVITY-BUDGET-1 (commit `49d4f5b`, landed) added
  the creativity resource model, which consumes the FRM's real grant
  path — the module docstring states this is the production pattern.
- **Authorizing decision:** James, 2026-09-29 (creativity controller
  D-6): the dedicated conditional Creativity Run Controller's
  "resources flow through the FRM". The FRM's own docstring confirms
  it is shared infrastructure (the ResourceArbitrator is "a second
  INSTANCE of the same class the Primary path uses, never a second
  class").
- **Why the fenced property still holds:** A5b is a coarse proxy for
  "nothing outside curiosity can reach the evidence writer". The
  offending import is FRM grant machinery only — not the evidence
  writer. The security-relevant checks in the same battery still pass:
  A5 (Primary-side module write refused at runtime, store untouched),
  A1–A4 (fence, provenance, terminal-state discipline), and the
  fresh-process SIGKILL persistence check.
- **Classification:** STALE EXPECTATION — documented, not repaired.
  The battery's 2026-09-29 invariant ("curiosity is self-contained")
  was superseded by the James-decided, independently gated, landed
  creativity track.

## 2. cur_p3b — t10_integration_boundary gate 3 (2026-10-04)

- **Battery:** `proofs/cur_p3b/cur_p3b_proof.py` (CUR-P3B stage, via
  `proofs/cur_p3b/gate_run.sh`)
- **Failing check:** `t10_integration_boundary` — gate 3 expects
  `rc.dispatch()` on a `creative_exploration` decision to raise
  `ValueError` containing "unknown curiosity loop".
- **Observed:** 64/65 checks pass; the single failure is an uncaught
  `AdmissionRefused: no registered loop 'creative_exploration': the
  curiosity loop registry is fenced (vocabulary admission is James's
  U-1-class decision)` — the test's `except ValueError` clause does
  not catch it, so it surfaces as EXCEPTION.
- **What changed:** CUR-P3A-INT (commit `7edecc6`, James-authorized
  U-1-class decision, independently gated, landed 2026-10-03)
  refactored the run controller to registry-dispatched: unregistered
  loops are now refused via the dedicated `AdmissionRefused`
  exception instead of the old `ValueError("unknown curiosity
  loop")`. The P3A-INT commit updated its own stage battery
  (`cur_p3a_proof.py`) for the new contract; the P3B stage battery
  was never updated.
- **Why the fenced property still holds:** the test's INTENT is that
  dispatching an unadmitted loop "dies at the substrate backstop
  (real call)". It does — loudly, naming the fenced registry. Gates
  1 and 2 of the same t10 (executive LOOP_ABSENT refusal; substrate
  `register_loop` ValueError refusal) still pass, and the loop
  remains unregistered because the P3B-INT integration has not
  landed (correct: James admitted the term for classification only).
- **Classification:** STALE EXPECTATION — documented, not repaired.
  The battery's pre-P3A-INT exception contract was superseded by the
  landed registry refactor. The refusal is, if anything, more precise
  than what the test asserts.
