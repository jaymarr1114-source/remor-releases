# CUR-P4C boundary record (2026-10-04)

## What was built
`runtime/curiosity/activation/evidence_return.py` — the curiosity side
of the C-9 evidence-return leg: `present_for_acceptance()` loads a
terminal finding from the fenced Evidence Store, validates it, runs
the causal cross-check (finding's bounded objective + inquiry id
against the live take-up request), adapts it to the gate's Finding
shape, and calls the REAL `RelevanceGate.decide()`. The ONLY gate
method touched is `decide()`. Proven by `proofs/cur_p4c/cur_p4c_proof.py`
(29/29): ADMITTED via provenance link end-to-end, RETAINED below
threshold, REJECTED malformed/refuted, append-only verdicts,
forgery refused, terminations refused as not-a-finding, no
boundary-closing capability on the curiosity side.

## Named boundaries (not built here)

1. **ActivationRequestStore / Primary issuance hook** — inherited from
   CUR-P4A: the Primary side has no ActivationRequestStore and no
   issuance hook in `runtime/core/`. The battery drives the real
   take-up path with Primary-shaped requests; issuance itself stays
   Primary-track-owned.

2. **Inspection inlet** (`inspect_finding` / `InspectionRecord`) — the
   C-9 design §7 marks this UNBUILT and Primary-track-owned. The
   return leg goes straight from the fenced Evidence Store to the
   relevance gate; the inspection record is a Primary-side step this
   track does not implement.

3. **C-2.1/C-2.3 enforcement is structural, not runtime.** There is no
   runtime capability check stopping curiosity-side code from opening
   the gate's sqlite file — the boundary is: (a) no writer exists in
   the curiosity tree (verified by tree-wide grep in the battery,
   T04), (b) the return leg's exposed surface is present-only
   (verified by source inspection, T06), (c) the decisions table is
   append-only with latest-wins reads, so a verdict can only be
   re-decided through the real `decide()`, never set (T04). The honest
   classification is PROVEN BUT BOUNDED (structural): it holds by code
   ownership and review, not by a runtime ACL.

4. **`AcceptanceLoop.present` (services/acceptance.py) is NOT the C-9
   Primary Acceptance.** It is the user-facing attempt-acceptance loop
   (attempt -> CANDIDATE -> user verdict). The C-9 §7 acceptance is
   the Primary side's inspection/relevance/disposition chain, whose
   executable parts here are the real `RelevanceGate`. The two were
   not confused: the return leg never touches the services loop.

## Observed deviations (not defects)
- None in this mission's scope. (P4B's `CURIOUSITY_INITIATED`
  frozen-vocabulary misspelling stands, unrepaired, as recorded there.)
