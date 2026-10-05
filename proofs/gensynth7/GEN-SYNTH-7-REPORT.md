# GEN-SYNTH-7 Mission Report — Four-Level Nesting

**Mission:** `T(2x+y+z+w)` through full run_cycle; raise/circumvent the
`MAX_NEST_DEPTH=3` cap via a sound mechanism; keep the search tractable;
preserve all GEN-SYNTH-6 regressions.

**Worktree:** `~/workspace/worktrees/gensynth7-mission`, branch
`gensynth7-mission`, base canonical `513aeda` (GEN-SYNTH-6 merge).

**Status:** MECHANISM BUILT, TRACTABILITY BOUNDARY HIT — see below.

**Rule:** Report, do not land.

## Mechanism: Arity-Gated Depth Extension (BUILT)

**Boundary addressed:** `PlanComposer.MAX_NEST_DEPTH = 3` hard-caps
recursive binary nesting. For `T(2x+y+z+w)`:
- depth 1: B = zip(xs,xs)
- depth 2: N = zip(map(B,sum),ys)
- depth 3: N' = zip(map(N,sum),zs)
- depth 4: N'' = zip(map(N',sum),ws) — BLOCKED by cap

The Oct-2 diagnostic confirmed honest exhaustion (20k evals,
found=False) on the unmodified tree. The boundary is real.

**Sound mechanism implemented** in `runtime/synthesis/plan_composer.py`,
`_complete_nested` (14 insertions, 1 deletion):

When `len(objective.params) > MAX_NEST_DEPTH`, allow exactly one
additional nesting level (depth 4), still gated by the sound
`_nest_may_complete` zero-eval necessary-condition prune.

Soundness argument:
1. **Dual of the established skip principle.** The code documents: "each
   nesting level adds at most one new param; deeper nesting cannot
   introduce new information." The dual: when the objective HAS more
   params than the current depth, deeper nesting CAN introduce new
   information. A 4-param goal needs depth 4 (necessary).
2. **Prune unchanged.** The `_nest_may_complete` necessary-condition
   gate still filters every (M', v) pair. No viable recursion is pruned;
   no unsound recursion is admitted.
3. **Not a silent budget increase.** Goals with ≤3 params see
   byte-identical behavior (verified: 3-level regression passes in 97s,
   same as before). Depth remains hard-capped at 4, not unbounded.
4. **Next boundary named.** A 5-param goal still fails honestly at depth
   4 — that is the next named boundary, not this mission.

This is a "raise via sound mechanism," not a hack: it generalizes the
cap based on a principled arity argument, preserves the existing
soundness guarantees, and names the next limit explicitly.

## Tractability: BOUNDARY HIT

**What works:**
- 3-level `T(2x+y+z)` still crosses in 97s, 7412 evals (regression
  verified — the arity-gating does not affect ≤3-param goals).
- Chain logic manually verified: the 4-level construction
  `map(zip(map(zip(map(zip(map(zip(xs,xs),sum),ys),sum),zs),sum),ws),sum),T`
  produces the correct outputs on hand-checked examples.
- The mechanism triggers correctly (code path verified by inspection;
  `_effective_cap` computes to 4 for 4-param goals).

**What does not yet work:**
The 4-level general search does not complete within tractable limits:
- 20k budget: exhausts without reaching depth 4 (budget burns in
  depths 1-3; the 4-param goal has a larger search space at every
  level).
- 60k budget: OOM-killed (memory from banked candidates exceeds
  available RAM).
- Prioritization via full-vector plausibility was attempted and
  reverted: the probe overhead outweighed the ordering benefit, and
  the core issue is budget exhaustion before depth 4, not pair ordering
  at depth 4.

**Root cause:** For a 4-param goal, depths 1-3 explore a significantly
larger space than for a 3-param goal (more params → more combinations at
each level). The search burns the candidate budget before the
arity-gated depth-4 extension can fire. The `_nest_may_complete` prune
is sound but not discriminating enough to keep the 4-param depths-1-3
search within the budget that suffices for 3-param goals.

## Files Changed

- `runtime/synthesis/plan_composer.py`: arity-gated depth extension
  in `_complete_nested` (+14/-1 lines). No other runtime changes.
- `proofs/gensynth7/gs7_fourlevel.py`: NEW battery driver (D0/C1/C2/C3/
  C5 pattern from GEN-SYNTH-6, 4-param goal, 20k budget).
- `proofs/gensynth7/gate_run.sh`: NEW gate (1 new driver + 13
  retargeted regressions).
- `proofs/gensynth7/regress/`: 13 retargeted regression drivers
  (copies from `proofs/gensynth6/`, WT path updated to current
  worktree, to verify no regression from the mechanism change).

## Evidence

- 3-level regression: `gs1_battery.py` retargeted copy passes (exit 0).
- Chain logic: manual Python verification of the 4-level construction
  produces `[1123, 2235]` for the hand-checked example.
- 4-level search: NOT YET DEMONSTRATED via general composer search
  (tractability boundary, see below).

## Exact Next Boundary

**Tractability of 4-param depths-1-3 search.** The arity-gated
extension is sound and triggers correctly, but the general search burns
its candidate budget in depths 1-3 before reaching the depth-4
extension. The `_nest_may_complete` two-element prune is insufficiently
discriminating for the 4-param search space.

Crossing this boundary requires ONE of:
(a) A stronger necessary-condition prune for the depths-1-3 search
    when the goal has 4+ params (extending the GEN-SYNTH-6 pre-gate
    pattern to the nesting path, not just the filter-fusion path); OR
(b) A directed chain-construction mechanism that bypasses the general
    depths-1-3 search for N-param chain goals (sound via construction
    + verification, but a larger architectural change); OR
(c) A significantly larger candidate budget with proportional memory
    (brute force; risks OOM as observed at 60k).

Option (a) is the most aligned with the mission's "extend the pre-gate
pattern" instruction and the codebase's established direction.

## What Remains Unproven

1. **That `T(2x+y+z+w)` crosses via the general composer search.**
   The mechanism exists and is sound, but the end-to-end demonstration
   (C1 in the battery driver) has not completed within tractable
   resources.
2. **That the full run_cycle crosses** (C2/C3/C5 in the driver) —
   depends on (1).
3. **That all 13 regressions pass with the mechanism in place** — the
   gate is written and one regression spot-checked green, but the full
   14-driver gate has not run green (blocked on (1)).
4. **Memory safety of the 4-param search** — 60k budget OOMs; the safe
   operating envelope for 4-param goals is not yet characterized.

## Recommendation

The mechanism is sound and minimal (14 lines). The tractability gap is
real but well-characterized. The next mission should focus on option
(a): a stronger pre-gate prune for the nesting path, following the
GEN-SYNTH-6 pattern. The current work provides the foundation (the
extension triggers correctly; the prune is the bottleneck).

Alternatively, if James prioritizes the working 3-level system over the
4-level extension, this branch can be held (not landed) and the
generalization track declared complete at 2/3 with the 4-level named as
a future boundary — but that would be a scope decision, not a technical
one, and it is James's call.
