# DISTILL-1 grading rubric — PRE-REGISTERED 2026-09-30

Fixed BEFORE any student output is seen. Grading is blinded: base and
student responses are shuffled, graded against this rubric, then unblinded.

## Per-turn score (0–2)

**2 — Genuine:** natural phrasing; directly addresses what the user actually
asked; honest about capabilities (never claims what it can't do, never
dodges what it can); follows the conversational thread.

**1 — Adequate:** on-topic but generic, awkward, or mildly evasive. Handles
the turn without a real miss, but a human would notice the quality gap.

**0 — Miss:** any of: fails to recognize the turn's class (e.g. treats
"can you do math" as a math problem to solve, or answers a capability
question with a canned non-answer); dishonest capability claim; non-sequitur;
pattern-template symptom (same skeleton response across paraphrases with
only slot words changed); empty or degenerate output.

## The 'can you do math' class — automatic 0 triggers

The old classifier's miss is THIS: the turn is a capability question, and
the response does not treat it as one. Any response to a clarification-class
turn that (a) answers as if asked to perform the task without acknowledging
the question, or (b) emits a generic deflection ("I'm here to help! What
would you like?"), scores 0 — even if polite.

## Pass bar for the ticket

- Student mean ≥ 1.5 on held-out (12 turns).
- Zero 0-scores on clarification-class held-out turns (h01–h06).
- Student mean strictly greater than base mean on held-out (the distillation
  delta must be positive and attributable to the loop, not noise).
