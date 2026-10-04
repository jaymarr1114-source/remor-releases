# CUR-P6E Boundary Notes

Genuine boundaries and named residuals from the Phase 6e regression.
None of these block the regression's verdict (all landed batteries
green or stale-documented); they are recorded for the final Phase 6
gate.

## 1. Two stale battery expectations (documented, not repaired)
See `STALE_EXPECTATIONS.md` §§1–2. The P1A A5b scan and the P3B t10
gate-3 assert pre-landing structural contracts that James-decided,
gated, landed changes (creativity-track FRM consumption; P3A-INT
registry refactor) legitimately superseded. The owning tracks should
eventually revise these batteries to the landed architecture. Until
then, the P6E orchestrator carries the loud, signature-verified
exemptions.

## 2. Phase 0 battery is pin-locked to 44e19e4
`proofs/cur_p0_gate_1/gate_run.sh` fail-closes unless the tree is at
44e19e4 and provisions its own worktree there. It re-proves Phase 0 at
its original gate pin, not at the current HEAD. The Phase 0 mechanisms
are re-proven at the current HEAD by the Phase 1 batteries (all
green). Re-pinning the P0 battery to a newer HEAD would be a new
mission, not a repair.

## 3. Unlanded phases excluded (by mandate)
P4A/P4B/P4C, P5A/P5B/P5C, P6A/P6B/P6C/P6D are reported but unlanded;
their batteries live on unlanded branches and were not re-run here.
Each is gated individually in the main chat. The final Phase 6 gate
requires them actually landed.

## 4. Carry-forward residuals for the final Phase 6 gate
(Not this mission's scope; recorded so the gate sees them together.)
- **P6C:** post-ban checkpoint resume is not enforcement-gated
  (PROVEN BUT BOUNDED) — a retained checkpoint empirically resumed to
  ACTIVE under BANNED_6M. The ban still starves it (activation refused,
  FRM zero allocation), but the state inconsistency is real.
- **P6D:** no engine-side refusal audit — refusals raise and leave no
  store trace; evidenced by exception + fresh-process state read + the
  battery's attempt log.
- **P6C landing note:** cur-p6a, cur-p6c, cur-p6d each created
  `runtime/curiosity/hardening/__init__.py` with different content —
  reconcile at landing.

## 5. Outstanding James decisions (unchanged)
- The two-part `novel_pattern` / `discovery_novelty` decision (declare
  in frozen boundary.py; admit to loop vocabulary, classification-only).
- James's hardware acceptance (standing — nothing is "working" until
  he confirms on his hardware).
