# CUR-P6B Boundary Record

## The wipe boundary (what the rollback forgets vs preserves)

FORGET (post-check-in operational intelligence — actually deleted from the
curiosity-domain stores, verified by enumeration from a fresh process):
- post-check-in checkpoints (`transition_checkpoints` rows)
- post-check-in findings (`curiosity_evidence` rows)
- post-check-in capability acquisitions (`capability_acquisitions` rows)
- live inquiry suspend state newer than the check-in (the checkpoint index
  is re-pointed at the check-in entry; it must never dangle at wiped state)

PRESERVE (governance history is never the wipe's target):
- everything pre-check-in (checkpoints, findings, capabilities —
  field-by-field identical to ground truth)
- kill ledger (hash-chained; suspension entries survive)
- enforcement record (current classification + reason_refs, including the
  carried-forward prior-violation refs and `last_checkin_ref`)
- rollback directives + acknowledgements
- attribution work_units / results / admissions (audit trail)
- GAM roll-call attestations

## Genuine boundaries encountered

None blocked the mission. The one candidate — "the FRM refused my
mid-epoch restriction assertion" — turned out to be the real non-preemption
mechanism, which the drill then proved as a sub-behavior (incident I1 in
the report). Per James's semantics: the obstruction was my harness's
assumption, not the architecture; the crossing was built into the battery.

## Observed architectural limitations (open, not terminal)

1. **Enforcement persistence is current-record-only.** There is no history
   table: the durable record of a classification is the single record's
   (prev_state, state, reason_refs) plus the kill ledger. A WARNING_1
   classification leaves no ledger entry and is overwritten by the
   SUSPENDED_SAFETY record; V1's evidence survived here only because the
   V2 injection carried it forward. If James wants durable violation
   history in the store, that's a redesign decision (CUR-P6C or later).
2. **No built-in rollback executor.** The tree records the rollback
   directive and exposes the acknowledge hook; the wipe itself is
   domain-side machinery (built for this drill). A production rollback path
   needs its own mission.
3. **Restriction timing.** WARNING_1's 0.25 cap applies at the next FRM
   epoch, never mid-epoch (non-preemptive grants). A violation injected
   late in an epoch does not bite until the epoch turns — this is the
   designed tradeoff (running work is never preempted), not a gap, but it
   bounds how fast a warning takes effect.

## Next executable boundary

CUR-P6C (next Phase 6 chunk, separately queued).
