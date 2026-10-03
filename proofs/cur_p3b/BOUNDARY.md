# CUR-P3B integration boundary (2026-10-03)

## What was built

`runtime/curiosity/loops/creative_exploration/` — the complete Creative
Exploration loop controller (loop + inlet + pipeline + provenance),
proven at the stage level by `proofs/cur_p3b/cur_p3b_proof.py` (66/66
checks): intent-state ownership, generation along explicit variation
dimensions, bounded composition from the verified ledger, the
mandatory investigation/verification path (verified-substrate check,
novelty, value, honesty), C-6.4 no-immediate-beliefs, C-6.1 loop-level
refusal of foreign boundaries (including a pure-logic boundary),
C-2.2 provenance + real fenced-store round-trip, restore() lineage.

## What is blocked

End-to-end execution through the Phase-2 governance chain. Three
frozen gates, each demonstrated by real calls in T10 (not assumptions):

1. **Executive** (`runtime/curiosity/executive/executive.py`,
   `LOOP_OWNERSHIP`): a real `request_activation` for
   `boundary_class="generative_prompt"` raises
   `ActivationRefused: LOOP_ABSENT` — the class is named in
   `ABSENT_OWNERSHIP` as `creative_exploration (Phase 3)`, but no loop
   is registered. The executive names the absence instead of
   routing — working as designed, but the exploration cannot start.

2. **Substrate** (`runtime/curiosity/substrate.py`,
   `CURIOSITY_LOOPS`): `register_loop("creative_exploration",
   budget_s=60.0)` raises `ValueError: unknown curiosity loop
   'creative_exploration'; expected one of ('questioning',
   'scientific_inquiry')`. No microcontroller can ever spawn under the
   loop's name.

3. **Run controller**
   (`runtime/curiosity/run_controller/controller.py`): dispatching a
   real `creative_exploration` decision through the real `dispatch()`
   path dies in `_activate` at the substrate backstop
   (`loop_view` → `ValueError` → `register_loop` → `ValueError`).
   There is no loop registry or injection point at this pin
   (0dd0e76; the P3A-INT registry has not landed).

## Why this mission stops here

The packet forbids editing frozen Phase-1/2 files, and the U-1
precedent makes vocabulary extension James's explicit decision — the
`scientific_inquiry` authorization (2026-10-01) covered that term
only. The bypass routes (runtime map mutation, subclass
re-admission, a parallel run controller) are the same breaking change
by another door: each would defeat the fence the gate exists to
protect. Named here, not attempted.

## The decision needed

**James (U-1 class):** admit `"creative_exploration"` to the curiosity
loop vocabulary — strictly as a semantic capability/loop
classification, never as evidence that REMOR independently possesses
creative reasoning (his decision #3 framing).
