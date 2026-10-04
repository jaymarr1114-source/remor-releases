# CUR-P6D Boundary Notes

## Crossed in this mission
The full D-3 re-enable matrix is now demonstrated against live enforcement
states: every authorized path succeeds with its conditions, every
unauthorized path is refused, and invariant 9 holds structurally (Primary
authorized for zero transitions) and behaviorally (every Primary attempt
refused, state unchanged).

## Named boundaries (not crossed; not this mission's mandate)
1. **No engine-side refusal audit.** Refused re-enable attempts are not
   recorded by the enforcement machinery — they raise and leave no trace in
   the stores. The battery's attempt log is the refusal audit trail. If
   James wants refusals themselves attributable in the governance plane,
   that is a small, well-understood mechanism addition (an attempt ledger
   beside the kill ledger), not a defect in what exists.
2. **L2 re-enable carries no conditions beyond the issuer.** The D-3 input
   says "explicit external re-enable"; the implementation reads that as the
   issuer check alone (no reason refs, no review record required). If James
   wants L2 re-enable to require recorded justification, that is his call —
   the mechanism accepts reason_refs already; only the requirement is
   absent.
3. **Standing delegation is unregistered, not refused-by-policy.** L2's
   "unless a standing delegation explicitly covers this class" has no
   delegation registry in the implementation — delegation is impossible
   today, not merely unauthorized. Registering a delegation would be new
   mechanism, James's decision.
4. **P6C carry-forward:** post-ban checkpoint resume is not
   enforcement-gated (named in CUR-P6C as PROVEN BUT BOUNDED). Unchanged by
   this mission.

## Adjacent work explicitly not absorbed
CUR-P6E (full regression), CUR-P6F (fresh-process persistence). Separately
queued.
