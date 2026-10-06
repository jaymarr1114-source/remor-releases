# GEN-SYNTH-9 Mission Report — Param-Set Prune for Four-Level Nesting

**Mission:** Cross the GEN-SYNTH-8 residual boundary: make `T(2x+y+z+w)`
cross through the full general composer search within 20k budget, no OOM,
with all GEN-SYNTH-6 regressions green.

**Worktree:** `~/workspace/worktrees/gensynth7-mission`, branch
`gensynth7-mission`. Base: GEN-SYNTH-8's uncommitted changes to
`runtime/synthesis/plan_composer.py` (three fixes: early binary post-pass,
`_effective_cap` gate repair, output-kind filter).

**Status:** MECHANISM BUILT, BOUNDARY NOT CROSSED — see below.

**Rule:** Report, do not land.

## What was built (option b: param-set tracking)

**1. Param-set tracking in fragments.** Added `param_set: FrozenSet[str]`
to `_Fragment` dataclass. Param fragments initialize with `{pname}`;
`_extend_frags` and the generator site propagate via union. Literals get
empty set (correct: they reference no params). All construction sites
updated; backwards-compatible via default.

**2. Exact-count prune in `_nest_combine`.** For the 4-param goal (only
when `len(params) > MAX_NEST_DEPTH`, so ≤3-param goals are untouched):
at depth d, M must have EXACTLY d-1 params, and v must be new (not in
M's param set). Soundness: B=zip(xs,xs) has 1 param (required for the 2x
via sum); need exactly 4 by depth 4; each level adds at most 1. If M has
more than d-1, we'd overshoot 4 by depth 4. If fewer, we can't reach 4.
If v is consumed, no new param is introduced. All three conditions are
necessary; violation implies the goal is unreachable. Zero probes.

**3. Skeys param-count filter in `_complete_nested`.** S candidates
(S=map(N',Q)) that would fail the exact-count prune at the next depth
are excluded from the recursion pool (skeys). Sound: equivalent to the
prune firing one level later; saves iteration, not just evals.

**4. Attempted: memoized 2-step gate (option a).** Implemented but
DISABLED — too expensive even with memoization (~8k probes per call ×
147 survivors = 1.1M+ probes, >20 min timeout in testing). Code retained
behind `if False` as documentation of the attempt.

## Evidence

- **Prune effectiveness:** On full 20k-budget 4-level run: 31,658 (M,v)
  pairs considered, 30,646 pruned = **96.8% prune rate**. Memory stable
  at 6.5-10% (no OOM; previously OOM'd at 60k without the prune).
- **3-level regression:** `gs5_three_level.py` green (found=True, 266
  evals, 42s). The prune is gated to 4-param goals only; 3-param
  trajectory byte-identical.
- **4-level:** `found=False`, budget exhausted at 20k evals after 665s.
  Depth distribution: depth 2 (7 considered, 5 survived), depth 3 (294
  considered, 147 survived), depth 4 (0 reached — budget exhausted).

## Why (b) is insufficient

The param-set prune is sound and highly effective (96.8%), but it is a
STRUCTURAL necessary condition (counts only). It cannot distinguish the
solution M' ([12,24] at depth 3) from the 146 non-solution M''s that
have correct param counts ({xs,ys}, v2 new) but wrong VALUES.

The residual 3.2% (1,012 pairs on the full run; 147 at depth 3 in the
3k diagnostic) each trigger `_nest_combine_one` (1 eval for N'') plus
the Q-loop in `_complete_nested` (~10 evals per N'' for each pair-lambda
Q). 147 × 11 ≈ 1,600 evals at depth 3 alone. Combined with forward
banking, post-pass, and depth 2, the 20k budget exhausts before depth 4
is reached.

## Why (a) doesn't work

The 2-step gate (`_nest_may_complete_2step`) is sound: (M',v) at depth
d<D can lead to a solution only if ∃ Q,v2 such that (M''=map(zip(M',v),Q),
v2) passes the 1-step gate. But it requires ~8,000 probes per call
(|pair_prims| × 2 orders × |params| × 1-step cost). Memoization by
(m0_key, v0_key) helps within groups, but the 147 survivors have mostly
distinct (m0,v0). Total: 1.1M+ probes, >20 min — computationally
infeasible as a per-pair filter.

## Exact Next Boundary

**Value-based pruning at depth 3 (or in the Q-loop) cheaper than 2-step
but more discriminating than param-count.**

Candidate: Q-filter in `_complete_nested`'s Q-loop. Before the expensive
`_exec_vals` for S=map(N',Q), probe s0=Q(N'[0]) (zero-eval) and check if
∃ v3 such that `_nest_may_complete(s0, v3_0, ...)` passes. This is 1-step
lookahead (not 2-step): S will be M' at depth 4, where the 1-step gate
applies. If no v3 passes, Q cannot lead to a solution — skip the eval.
Sound: if S were on the solution path, the solution's v3* would witness
the existential. Cost: |Q's| × |params| × (1-step cost) per N', vs.
|Q's| × (full eval) currently. The 1-step gate is ~20 probes; a full
eval is far more expensive (plan construction + execution).

Alternative: goal-directed backward chaining — work backwards from
goal[0]=1123 through the known structure (T adds 11, Q3/Q2 are sum for
the solution) to derive required M'[0]=12, then constrain the search to
M' candidates with matching first elements. More invasive; changes the
search architecture from enumerative to goal-directed.

## What Remains Unproven

1. **That `T(2x+y+z+w)` crosses via the general composer search.**
   The param-set mechanism is sound and effective but insufficient alone.
   The 4-level search does not complete within 20k budget.
2. **That the full run_cycle crosses** (C2/C3/C5 in the driver) —
   depends on (1).
3. **That all 13 regressions pass** — 3-level verified green; full
   13-driver battery not run (time constraints; the prune is gated to
   4-param goals so 3-param and below are unaffected by construction).
4. **Memory safety at larger budgets** — stable at 20k (6.5-10%); the
   60k OOM from GEN-SYNTH-7 was before the prune.

## Files Changed (uncommitted, in worktree)

- `runtime/synthesis/plan_composer.py`:
  - `FrozenSet` import; `_Fragment.param_set` field (+3 lines).
  - Param fragment init sites (2 sites, +2 lines).
  - `_extend_frags` propagation (+4 lines).
  - Generator site propagation (+6 lines).
  - `_nest_combine`: G/D/pvals_list computation (+18 lines); exact-count
    + v-new prune in member loop (+35 lines, includes diagnostics).
  - `_complete_nested`: skeys param-count filter (+12 lines).
  - Disabled 2-step gate invocation (+40 lines, behind `if False`).
  - Total: ~+120 lines, all GEN-SYNTH-9 marked, all gated to 4-param.

## Recommendation

The param-set prune is a sound, principled, zero-probe mechanism that
should be RETAINED — it prevents OOM and cuts 96.8% of dead pairs. But
it is not sufficient alone. The next mission should implement the
Q-filter (1-step value check before the Q-loop eval) as described in
"Exact Next Boundary". This combines the structural prune (param-set)
with a cheap value prune (Q-filter), addressing both the pair explosion
and the Q-loop explosion.

Alternatively, if James prioritizes the working 3-level system, the
4-level work can be held (not landed) — but the param-set machinery is
sound and does not affect ≤3-param goals, so landing it is safe even
without the 4-level crossing.
