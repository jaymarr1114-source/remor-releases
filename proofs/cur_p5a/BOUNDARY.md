# CUR-P5A boundaries (2026-10-04)

## Terminal boundaries hit
None. The mission completed: the initiation path is built, the end-to-end
chain runs, and every no-authority proof is an executed refusal.

## Deferred (not boundaries — owned by queued missions)
- **Triage semantics** (which flag when; what the flags authorize): CUR-P5B.
- **Relevance fencing in Primary Acceptance** (C-3.2/A24): CUR-P5C.
- **Initiated inquiries through non-questioning loops**: follows the pending
  P3B-INT/P3C-INT integrations. Scoping to questioning was a mission-design
  decision, not a mechanism limit — the initiation path is loop-agnostic
  (boundary_class is a mint parameter).

## Observed facts (not defects)
- The frozen origin vocabulary spells the initiated origin
  `"CURIOUSITY_INITIATED"`. Used as-is; renaming breaks landed callers
  (observed in CUR-P4B, correctly left unrepaired).
- The FRM grant is a frozen dataclass: grant escalation is refused by
  `dataclasses.FrozenInstanceError`, a language-runtime guarantee rather
  than a policy check.
- `CuriositySubstrate.register_loop` names the Primary side's substrate
  instance in its refusal message — the two substrate instances are
  disjoint by vocabulary fencing, both directions.
