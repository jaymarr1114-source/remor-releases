# PLOOP-9 REPORT — terminal-state routing (LoopOutcome consumers)

**Date:** 2026-09-28 (mission executed 2026-09-29 ~02:00 UTC)
**Branch:** `ploop-9-terminal-routing` (worktree `~/workspace/ploop-9-work`, base `cfe7915`)
**Status:** COMPLETE — proof 152/152 green on the mission tree.

## Objective (James's mission)

Route declared-terminal loop outcomes to real consumers instead of letting
`produce_handoff()` return `None` into a void: acceptance candidates to the
persisted verdict-awaiting surface, OPEN acquisition outcomes to the real
`rc_gap_backoff` path, acquisition/handoff refusals to
`ExecutiveController.submit_finding()` preserving the exact violation,
execution results to their diagnosis records, distillation/generalization
results to real records or explicit named sinks, ABSENT outcomes to a
queryable explicit sink, evidence references intact on every route.

## What was built

**`runtime/core/executive/terminal_routing.py`** (new, ~750 lines):
- `TerminalRouter.route_terminal(loop, outcome, context)` — classifies with
  the real `classify_terminal()`; runs the pair's declared follow-on builders
  (the same ones `produce_handoff` uses) and refuses loudly if any builder
  returns evidence or raises; otherwise delivers to the consumer route.
- `TerminalRouter.route_refusal(exc, loop=...)` — a `HandoffRefused` raised
  mid-transition becomes a finding carrying the EXACT violation.
- `TerminalLedger` — append-only sqlite; the explicit sink. Every routed
  outcome is a queryable row (loop, terminal state, consumer, reason,
  evidence refs).
- 19 consumer routes covering every reachable (loop, terminal) pair.

**`runtime/core/executive/__init__.py`** (additive only): exports
`TerminalRouter`, `TerminalLedger`, `TerminalRoute`,
`TERMINAL_ROUTING_VERSION`, `FINDING_STATE_REFUSAL`.

No changes to `handoff.py`, `run_controller.py`, or any loop machinery.

## Proof

**`proofs/ploop9_terminal_routing_2026-09-28.py`** — 152/152 green, fresh
process, temp dirs, real machinery throughout (SwarmEngine, RunController,
GapRegistry, AcceptanceLoop+AcceptanceStore, ExecutiveController+RelevanceGate).
Durable log: `proofs/ploop9_terminal_routing_proof_2026-09-28.log`.

- **S1:** 19 reachable pairs PROVEN reachable by driving the real classifier
  with real-shaped inputs; every reachable pair is produce_handoff's domain
  or has a consumer route; 23 unreachable pairs documented, unrouted.
- **R1:** real `present()` → CANDIDATE verified in the real store, still
  CANDIDATE after routing (never auto-advanced).
- **R2:** real refused dispatch (routed=False) → executive finding with the
  exact refusal; gate's persisted decision re-checked as the receipt.
- **R3:** real tick with exhausted budget → persisted via `record_cycle`
  (run()'s own step) → routed to the controller's accepted cycle record.
- **R4:** real attempted-but-open dispatch → real `rc_gap_backoff`
  (failures 0→1, future backoff_until).
- **R5:** real route + real utilization check → real `_close` → router
  verifies the closed record via the real gap fetcher.
- **R6:** real `diagnose_quarantine()` (converged) + real result type with a
  named holding reason (open) → diagnosis record.
- **R7/R8:** real `DistillationResult` shapes → own records; generalization
  converged → explicit sink naming the external trust path.
- **R9:** all six loops absent → explicit sink, 6 ledger rows.
- **R10:** live follow-ons refused loudly (novel_goal propagation;
  produce_handoff/transition named).
- **R11:** `route_refusal` carries the exact violation as a finding.
- **R12:** missing run controller / acceptance loop / executive → loud
  `HandoffRefused`, never a silent drop.
- **R13:** 17/17 ledger rows carry evidence refs and consumers.

## Two real defects the proof caught and repaired

1. **Refused dispatches went to backoff, not the executive.** The real
   classifier reads `outcome="open"` before `routed=False`, so a genuinely
   refused dispatch classified as OPEN and the router sent it to backoff —
   backing off a gap no route can serve retries nothing, and the refusal
   never reached the executive. The router now splits refused-from-attempted
   at route time on the dispatch's own `routed` flag: refused → executive
   finding with the exact violation; attempted-but-open → backoff.
2. **Refusal findings were inadmissible.** The router built findings with the
   loop-terminal `terminal_state="refused"`, but `Finding.validate()`
   requires relevance.py's epistemic vocabulary and raised `FindingRefused`.
   Fixed with the honest mapping `FINDING_STATE_REFUSAL =
   "BOUNDARY_ESTABLISHED"` (a truth-SUPPORTED state: the refusal's occurrence
   establishes the claim "this loop hit a boundary it cannot cross").

## Regression

Re-ran the PLOOP-2 handoff proof (`proofs/ploop_2_handoff_proof_2026-09-28.py`,
PLOOP2_WT pointed at this tree) to guard the additive `__init__.py` change
and the shared contract: **49/49 green** (a ~5s light battery; no
contention with the concurrent GEN-SYNTH-5 regression).

## Commit

Work commit `b495e47` — "PLOOP-9: route declared-terminal loop outcomes
to real consumers" (neutral message; no verification claim in the commit)
on branch `ploop-9-terminal-routing`. The report itself was finalized in a
follow-up commit on the same branch.

## Exact next boundary

The production tick still dispatches gaps inline; handoff contracts are not
yet the production seam — `TerminalRouter` is proven machinery with no
production caller. The next boundary is **wiring the router into the real
production path**: the executive's loop-entry/exit points (and/or the run
controller's tick boundary) must offer declared-terminal outcomes to
`produce_handoff()` first and fall through to `route_terminal()` on `None`,
in the real tick — not in a proof harness. That wiring is a separate mission
(it touches the production tick path, PLOOP-10's file); this mission's
contract was the router itself, and that contract is crossed.

## What remains unproven

- The router has no production caller yet (above).
- `route_refusal` for violations raised inside `transition()`/`accept_handoff()`
  is proven only via a synthetic `HandoffRefused`, not via a real mid-transition
  violation (no real transition violation was inducible in this tree).
- The generalization-converged sink names the trust path as declared-but-external;
  wiring ReviewBoard's work-product flow needs a real technique→work-product
  adapter that does not exist (not fabricated).
- Device proof: none of this has run on James's hardware (bench only).
