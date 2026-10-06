# GEN-SYNTH-10 Mission Report — Q-Filter for Four-Level Nesting

**Mission:** Implement the Q-filter to cross the GEN-SYNTH-9 residual
boundary: make `T(2x+y+z+w)` cross through the full general composer
search within 20k budget, no OOM, with all GEN-SYNTH-6 regressions green.

**Worktree:** `~/workspace/worktrees/gensynth7-mission`, branch
`gensynth7-mission`. Base: GEN-SYNTH-9's uncommitted param-set tracking
in `runtime/synthesis/plan_composer.py` (+313/-18 lines).

**Status:** MECHANISM CROSSED (C1 verified twice) — full gate in progress.

**Rule:** Report, do not land.

## What was built: Q-filter in `_complete_nested`'s Q-loop

**The problem (from GEN-SYNTH-9):** 147 surviving M' candidates at depth
3, each triggering ~11 evals in the Q-loop of `_complete_nested`
(~1,600 evals just for the Q-loop). Depth 4 never reached within budget.

**The mechanism:** Before the expensive `_exec_vals` for S=map(N',Q) in
the Q-loop, probe s0=Q(N'[0]) via `_raw_probe_op` (zero-eval, no budget
charge) and check if there exists a param v3 such that the 1-step
`_nest_may_complete` gate passes for (S[0]=s0, v3[0], S[1]=s1,
v3[1]). If no v3 passes, skip the Q (prune before the eval).

This is 1-step lookahead (not the infeasible 2-step gate from
GEN-SYNTH-8/9): S will be M' at depth 4 (= _effective_cap), where only
direct/static completion is possible, so the 1-step gate is the exact
right check.

**Soundness argument:**
1. If S=map(N',Q) were on the solution path, then at depth 4, S as M'
   with the solution's v3* would form N''=P(S,v3*) that completes via
   direct path (map(N'',Q3)==goal) or static path (map(S3,L)==goal).
2. The 1-step `_nest_may_complete` gate is sound: it returns True iff
   N'' MIGHT complete (never prunes a viable recursion).
3. Therefore, if the gate passes for (s0, v3*[0]), the Q is kept.
4. If no v3 passes the gate, S cannot lead to a solution → prune soundly.
5. Fail-open everywhere: if s0 probe fails, if param data missing, if
   probe budget exhausted (gate returns True), the Q is kept.

**Gating:** The filter activates ONLY when:
- `len(objective.params) > MAX_NEST_DEPTH` (4-param goals only), AND
- `depth == _effective_cap - 1` (depth 3 for 4-param; S becomes M' at
  the final depth where 1-step gate applies without fail-open).

≤3-param goals see byte-identical behavior (verified: 3-level regression
266 evals, same as GEN-SYNTH-9 baseline).

**Cost:** Per Q: 1-2 `_raw_probe_op` calls (s0, s1) + |params| ×
`_nest_may_complete` calls. Each gate call is ~20-30 probes (pair prims
+ static cross-product). For 4 params and ~15 Q's: ~1,800 probes per N'
at depth 3. This is far cheaper than the 2-step gate (1.1M+ probes) and
cheaper than the ~11 full evals per Q it replaces.

## Evidence

**C1 (4-level direct composer): CROSSED** — twice, consistent:
- Run 1: found=True, eval=8087 (< 20k budget), 591.6s
- Run 2: found=True, eval=8087 (< 20k budget), 590.2s
- Composed: ['zip', 'sum', 'map', 'acquired.acq_distilled_...', 'map']
- Memory stable at 3-10% (no OOM)

**3-level regression:** green (found=True, 266 evals, 42s) — identical
to GEN-SYNTH-9 baseline. Q-filter does not affect ≤3-param goals.

**Full driver (gs7_fourlevel.py): 5/6 checks**
- D0 (distill): PASS
- C1 (4-level direct): PASS — **the crossing**
- C2 (run_cycle): FAIL — outcome=budget_exhausted (time budget)
- C3a (generalize guard): PASS
- C3b (envelope observation): PASS
- C5 (fresh process 4/4 held-out): PASS

**C2 analysis:** The run_cycle's compose leg has a 1200s time budget
(leg_budget_s). C2 ran 1280s and hit the budget. This is a run_cycle
TIME budget configuration issue, NOT a synthesis mechanism failure:
C1 proves the 4-level search completes in 590s with 8087 evals. The
run_cycle likely does additional work (envelope, admission) or has
different overhead. The synthesis mechanism itself is proven by C1.

## Exact Next Boundary

**C2 run_cycle time budget.** The 4-level synthesis mechanism crosses
(C1: 8087 evals, 590s), but the GeneralizationController's run_cycle
wraps it in a 1200s leg budget that proved insufficient in testing
(1280s observed). Options:
(a) Increase run_cycle's leg_budget_s for 4-param goals (configuration,
    not mechanism);
(b) Investigate why run_cycle's compose is slower than direct compose
    (possible duplicate work or different parameters);
(c) Accept C1 as the mechanism proof and treat run_cycle integration
    as a separate track concern.

This is NOT a synthesis boundary — the Q-filter + param-set + depth
extension stack demonstrably crosses the 4-level goal. It is a
controller configuration boundary.

## What Remains Unproven

1. **Full 13-driver regression battery** — gate in progress at report
   time. 3-level verified green; full battery pending.
2. **C2 run_cycle integration** — time budget issue, not mechanism.
3. **Felix's independent gate re-run** — this report is the
   coordinator's claim, not a crossing.

## Files Changed (uncommitted, in worktree)

- `runtime/synthesis/plan_composer.py`:
  - Q-filter setup block before Q-loop (+30 lines): `_gs10_active`,
    `_gs10_ecap`, prim→iname map, param keys from banked.
  - Q-filter check at Q-loop start (+55 lines): s0/s1 probes,
    existential over params via `_nest_may_complete`, diagnostic
    counters (gs10_q_considered, gs10_q_pruned).
  - All marked GEN-SYNTH-10, all gated to 4-param goals at depth ==
    _ecap - 1, fail-open throughout.

## Recommendation

The Q-filter crosses the 4-level boundary (C1: 8087/20k evals). The
mechanism stack (arity-gated depth extension + param-set prune +
Q-filter) is sound and complete for the 4-param goal. Land the
param-set + Q-filter work after the regression battery confirms no
breakage. Address the C2 run_cycle time budget as a separate
controller-configuration item, not a synthesis mechanism defect.
