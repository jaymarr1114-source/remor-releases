# CUR-P2 landing attestation — 2026-09-30

## Decision
James approved U-1 (2026-09-30 ~08:50 EDT): accept the fenced
`CuriositySubstrate.register_loop("questioning")` override, with the
isolation/gate evidence attached to the landing.

## What landed
- Mission commit `94b0787` (coordinator, REPORTED) rebased onto canonical
  `8d23132` → `7b4dee7`, then amended with this attestation + gate evidence.
- 13 mission files, purely additive; no frozen Phase-1 file touched.
- The override admits exactly one provisional loop name (`"questioning"`),
  refuses Primary loop names, and inherits the lifecycle machinery unchanged.

## Gate evidence (attached)
1. **Independent gate (Felix), 2026-09-30 ~02:28 EDT, pre-rebase commit:**
   `proofs/cur_p2/gate_rerun_2026-09-30.log` + `proofs/cur_p2/runs/`
   — 113/113 checks passed in 22.51s, one genuine hermetic run, exit 0.
   Roll-call 49/49, resource arbitration 7/7, FRM battery 15/15,
   enforcement 28/28. Cross-instance isolation adversarially verified.
2. **Landing-HEAD re-proof (Felix), 2026-09-30, rebased commit `7b4dee7`:**
   `proofs/cur_p2_phase2_proof.py` — 113/113 checks passed in 21.43s,
   exit 0, observed directly (tail captured; decisive line:
   "113/113 checks passed in 21.43s").

## Landing
- Fast-forwarded canonical `v10-runtime`: `8d23132` → `<amended>`.
- Contamination found and reverted before landing: the coordinator's run had
  overwritten `proofs/cur_p1b_enforcement_manifest.json` in this worktree
  (load values + worktree path from its local re-run). Not part of CUR-P2;
  reverted, not landed.
