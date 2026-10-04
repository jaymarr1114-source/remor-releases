# CUR-P4A boundary record

## What was built
`runtime/curiosity/activation/` — the curiosity side of the C-9
Primary-requested activation path (request record, take-up decision,
withdrawal, return-channel record view), proven by 44/44 checks against
the real executive, run controller, FRM, and fenced Evidence Store.

## Primary-track boundary (named, not built here)

**The ActivationRequestStore is UNBUILT.** The C-9 design assigns it to
the Primary track ("[UNBUILT → Primary-track owner]", design §1), and
`runtime/core/` contains no issuance hook, no withdrawal hook, and no
named-refusal return channel as of canonical 84b2a80 (verified by grep;
`runtime/core/` untouched per git status).

What the curiosity side defines (this mission): the §1 record contract,
fail-closed intake validation, the take-up decision procedure with named
refusals, the withdrawal lifecycle, and the decision record as the
defined return channel (`ActivationTakeUp.get_request`).

What the Primary track must bind: issuing conforming requests, storing
them, reading back decisions/withdrawals, and (4c) Primary Acceptance
inspecting curiosity findings and recording verdicts.

## Design-vs-as-built note

Design §2 specifies take-up order kill → grant → fit; the landed
`request_activation` evaluates kill → ownership → grant → fit. Every
refusal is named either way; recorded, not repaired.

## What this mission does not cover

- C-9.4 kill-during-inquiry (chunk 4b) — the kill path exists
  (`kill_inquiry`); the Primary-requested KILLED termination record and
  its delivery to the Primary side belong to 4b.
- Evidence-return acceptance verdicts (chunk 4c).
- The `novel_pattern` boundary declaration + `discovery_novelty`
  vocabulary admission (awaiting James's two-part decision; P3C-INT).
