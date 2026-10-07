# GEN-SYNTH-7/8/9/10 + GEN-SYNTH-11 — GATE VERDICT: CROSSED

**Date:** 2026-10-06/07 (Felix's independent gate completed 2026-10-07 ~01:26 UTC)
**Gate commit:** `feb4be5` (GEN-SYNTH-11 repair on top of merge `7946453`)
**Gate tree:** clean detached worktree `~/workspace/worktrees/gate-gensynth11`, zero uncommitted changes
**Verdict:** **CROSSED** — 13/13 drivers PASS on Felix's independent re-run

## History

- GEN-SYNTH-7/8/9/10 merge `7946453` FAILED its first gate: C1 (4-level synthesis, 8,087 evals) and C2 (run_cycle, 1,584.7s, 4/4 held-out) were genuine, but the 13-driver regression battery went 11/13 with two real defects.
- **Defect 1** (driver 2, gs6_threelevel_runcycle): OOM — RSS 10MB → 5.7GB in ~4 min, SIGKILL in B2 run_cycle. Root cause: three compounding GS8 changes (fail-open `_nest_may_complete` removed pruning; early binary post-pass ran for all LIST goals; causal contrast reused one PlanComposer, accumulating caches).
- **Defect 2** (driver 8, gs3_battery): filter-fusion regression 14/25, deterministic. Root cause: GS8 `_gs8_pair_lam_kind_ok` blocked ANY-kind Q's; the viable plan requires `coalesce:ANY`. Pure over-pruning.
- GEN-SYNTH-11 repaired both (2 files, +36/−6):
  - `runtime/synthesis/plan_composer.py`: kind filter allows ANY (fail open on unknown kinds); nest-gate fail-open only when 4-param extension active; early post-pass only for `len(params) > MAX_NEST_DEPTH`.
  - `runtime/generalization/controller.py`: causal contrast uses a fresh PlanComposer.

## Felix's independent gate (this verdict)

Clean detached worktree at `feb4be5` (repair commit + retargeted gate battery). All 13 drivers run sequentially, uncontended, in fresh processes. Driver targeting verified (no stale worktree references; all imports resolve to the gate tree). Repair markers verified in source.

| Driver | Result |
|---|---|
| gs6_filter_nested | PASS |
| gs6_threelevel_runcycle (was OOM) | PASS |
| gs5_three_level | PASS |
| gs5_mixed | PASS |
| gs5_runcycle | PASS |
| gs5_repair_focused | PASS |
| gs4_battery | PASS |
| gs3_battery (was 14/25) | PASS |
| gs2_battery | PASS |
| gs1_battery | PASS |
| gxdom_battery | PASS |
| genv_battery | PASS |
| gctrl_battery | PASS |

**13/13 PASS, 0 FAIL. Exit 0.**

## Conclusion

The 4-param synthesis capability (C1, C2) is real, and the two regressions it introduced are repaired with the 4-param mechanisms preserved (all 4-param-specific paths remain gated to `len(params) > MAX_NEST_DEPTH`). The gate is crossed.

**Disposition: CROSSED.** Queue row #59 updated FAILED → CROSSED.

Evidence: `~/workspace/remor_convergence/proofs/gensynth11_gate_2026-10-06/` (felix_gate_run.log + 13 driver logs).
Gate battery: `~/workspace/worktrees/gate-gensynth11/gate_run_felix.sh` (committed at `feb4be5`).
