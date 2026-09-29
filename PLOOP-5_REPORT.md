# PLOOP-5 REPORT — GraphController symmetry (the missing class)

**Date:** 2026-09-28 ~21:05 EDT
**Branch:** `ploop-5-graphcontroller` off canonical `6d412ce` (worktree
`~/workspace/ploop-5-work`; main tree untouched)
**Lane:** audit item #6 (EXEC2 prerequisite #2). Own lane, not gated on siblings.

## What was built

`runtime/core/graph_controller/` — a real `GraphController` class completing
the hierarchy's fourth level:
Executive → Loop Controller → Microcontroller → **GraphController** → Graph.

The controller mediates between microcontroller intent and graph traversal. It
answers the theory's question: "How do I navigate and operate the structural
graph necessary to resolve this bounded objective?" Cursor model — the
microcontroller declares its objective, the controller decides WHAT may be
operated next, in WHAT order, WITHIN what bounds; the microcontroller does the
work and records results:

- `attach_graph(graph)` — registers a structural graph via the
  `StructuralGraph` protocol (nodes / goals / dependencies). Ships with
  `TaskGraphAdapter` over the real swarm `TaskGraph`.
- `open_region(mc_id, graph_id, objective, max_operations=64)` — the
  controller's real decision: relevance-scores nodes against the objective
  (token overlap with crude stemming), expands to dependency closure so the
  region is self-sufficient. Fail-closed on empty matches.
- `next_operable(region_id)` — dependency-ordered cursor; failed nodes mark
  dependents unreachable (mirrors TaskGraph semantics).
- `record_result(region_id, node_id, success, value/error)` — bound-enforced;
  excess operations refused as values, never exceptions.
- `open_subregion(region_id, node_ids, ...)` — subordinate GraphController
  over a sub-region (theory: recursive decomposition); nodes outside the
  parent region refused.
- `close_region(region_id)` → `RegionSummary` (counts, bound_hit, values).
- `region_view(region_id)` — aggregates only; traversal internals never leak
  above the loop level (theory sec. 10 visibility boundary).

Refusals are values (`unknown_graph`, `no_matching_nodes`,
`region_bound_exceeded`, `out_of_region`, `node_not_operable`,
`already_recorded`, `outside_parent_region`, …), matching the frozen
microcontroller substrate's conventions. The controller owns no execution
logic — same separation as the swarm Scheduler, which it does not duplicate:
the Scheduler is engine-driven whole-graph scheduling; this is
microcontroller-driven scoped mediation (region selection, bounds, ordering,
recursion).

## No-duplication accounting

Inventory before writing: the frozen microcontroller substrate
(`microcontroller-interface/v1`) has NO graph calls; `TaskGraph` is driven
only by `engine._run_decomposed` via `Scheduler`; `plan_composer` is driven
directly by the GEN controller. No microcontroller→graph path existed — this
is genuinely new machinery, not a renamed path. One design correction made
mid-build: an initial fail-closed-at-open rule for oversized regions made the
operation budget unreachable dead code; replaced with bounded partial
progress (open succeeds, budget enforced during operation, `bound_hit`
reported) — mirroring the substrate's admission-at-spawn + exhaustion-via-charge.

## Proof

`proofs/ploop5_graphcontroller_battery.py` — **42/42 green**, no mocks: real
`TaskGraph`s, the real frozen `MicrocontrollerSubstrate`, real deterministic
worker computation. Covers region selection (relevant picked, irrelevant
excluded, closure, empty/tight-budget behavior), dependency-ordered
navigation, failure→unreachable propagation, bound enforcement with
`bound_hit`, all refusal codes, subregion recursion + outside-parent refusal,
and a full e2e: spawn mc → open_region → operate loop → close → summary
assertions (dependency order verified, irrelevant nodes untouched) → retire.
Plus a second e2e over a graph built by the real `build_graph` composite
decomposer.

Log: `proofs/ploop5_graphcontroller_battery.log` (exit 0).

**Contention note:** the first green run executed while GEN-SYNTH-5's heavy
e2e battery was running on the same machine. A clean uncontended re-run was
performed after the sibling finished (no heavy processes on the box, load
falling): **42/42 green, exit 0** — count re-confirmed.

## Exact next boundary

Wiring: nothing instantiates `GraphController` in production yet — same
structural gap as the rest of the loop (audit: zero production
`RunController(`/`ExecutiveController(` instantiations). The class is proven
in-harness; its production inlet (a loop controller handing a
microcontroller's objective to a GraphController over a real task graph)
belongs to the loop-controller missions, not this lane.

## What remains unproven

- Production wiring (above).
- `export_state`/`import_state` for regions (checkpoint seam) — deferred;
  overlaps PLOOP item #5 (checkpoint/recovery) territory.
- Relevance scoring is token-overlap; a cognition-backed scorer is future work
  (the substrate's cognition inlet exists for exactly this).
- The interface is `graph-controller-interface/v1-draft` — NOT frozen; the
  gate decides.
