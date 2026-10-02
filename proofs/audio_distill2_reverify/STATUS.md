# AUDIO-DISTILL-2 Status: NOT VERIFIED THIS SHIFT

**Date:** 2026-10-02
**Phase:** 5 (Grower loop to completion)
**Provenance:** Felix

## What Was Reverted (and Why)

The old AUDIO-DISTILL-2 gate:
- Established native gap (valid)
- Used a hardcoded ground-truth dictionary (invalid as proof)
- Had `dummy_analyze()` return the hardcoded dictionary (circular)
- Did NOT perform real teacher distillation

The gate explicitly stated: "full teacher distillation remains required."
This was a harness, not a crossing. Correctly reverted.

## What Real Distillation Requires

Per the mandate ("real audio-distillation ground truth/evaluation"):

1. **Real audio task:** A genuine audio analysis task with objective ground truth
   (not a hardcoded dictionary). Example: chord function analysis where the
   correct answer is determined by music theory, not by the test author.

2. **Teacher demonstrations:** Qwen3 (via llama-server) must demonstrate the
   technique on multiple examples. The teacher's outputs are the training data.

3. **Distillation loop:** Route C (trace-guided) distills the teacher's
   technique into a native capability.

4. **Verification:** Fresh-task teacher-removed independence test — the
   distilled capability must work on NEW tasks without the teacher.

5. **No hardcoding:** The ground truth must come from the domain (music theory),
   not from a dictionary written by the test author.

## Why Not Done This Shift

Real audio distillation is substantial work:
- Requires designing a valid audio task with objective ground truth
- Needs multiple teacher demonstrations (each ~0.9s inference, plus prompting)
- Requires running the full Route C distillation loop
- Needs held-out verification tasks

The previous AUDIO-DISTILL-1 mission (also reverted) found that Qwen3-8B
**failed** MIDI voice-leading demonstrations (1/19 verified). This suggests
audio/music may be a genuine boundary for the teacher, not just an
implementation gap.

## Label

**NOT VERIFIED THIS SHIFT**

This is not a failure — it's unstarted real work. The hardcoded harness
was correctly reverted. A real distillation requires the full loop described
above.

## Path Forward

1. Design a valid audio task with objective ground truth
2. Test if Qwen3 can demonstrate the technique (if not, that's a boundary)
3. If teacher succeeds, run Route C distillation
4. Verify with teacher-removed independence test

OR: Acknowledge that audio distillation may be a genuine teacher boundary
(Qwen3-8B failed voice-leading) and prioritize other grower work.
