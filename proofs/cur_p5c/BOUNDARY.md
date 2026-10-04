# CUR-P5C boundary notes (2026-10-04)

## No new boundary hit

This mission crossed its mandate without hitting a terminal condition.
The tree already contained every mechanism the fencing needed: the
real `RelevanceGate` (decide/record/get_decision, no mutation API), the
real `ExecutiveController` (no override API; `set_operational_objective`
as the allowed new-question path), and the real `LivePath`
(submit_finding → ADMITTED-gated outbox → deliver_admitted re-check).

## Honest limits (PROVEN BUT BOUNDED)

- The non-override guarantee holds at the API/governance layer: no
  verdict-mutation API exists on the executive, the gate, or the
  curiosity side, and the decisions table is append-only. Out-of-band
  tampering (raw sqlite UPDATE on the store file by the same process)
  is not prevented by any in-tree mechanism — as with any database,
  the guarantee is against the machinery's own paths, not against the
  filesystem. Recorded here, not oversold.
- The relevance input's verbatim-content property holds by
  construction (no mutation code path exists in the presenter) plus
  battery round-trip checks.

## Named for the future

- None. No Primary-track boundary encountered (no missing interface).
