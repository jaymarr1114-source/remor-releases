# CUR-P3C integration boundary (2026-10-03)

## What was built

`runtime/curiosity/loops/discovery_novelty/` — the complete
Discovery/Novelty loop controller (loop + inlet + pipeline +
provenance), proven at the stage level by
`proofs/cur_p3c/cur_p3c_proof.py` (61/61 checks): adjacent-space
exploration with lineage, novelty assessment (routine patterns never
claimed novel, each refusal named), investigation, verified-substrate
check, classification convergence (NOVELTY_CLASSIFIED / feed to
Questioning / BOUNDARY_ESTABLISHED), C-6.1 refusal of foreign
boundaries, C-2.2 provenance + real fenced-store round-trip,
restore() lineage from JSON-native checkpoint forms, NODE_GOALS
relevance floor.

## Boundary-presentation design (mandate 2)

The loop's proposed presentation is `novel_pattern`: a detected
pattern in the adjacent space of the known envelope whose
classification is not established. Validated against theory §7
(Discovery / Novelty Controller: the loop detects "unknown
relationships, unexplored capability spaces, anomalies, unexpected
patterns, new dependencies, new opportunities, previously unseen task
structures, and potentially useful phenomena" -- all *patterns*) and
against the as-built vocabulary:

- `novel_observation` (scientific_inquiry) is an observation whose
  *reproducibility* is in question -- the observation is established;
- `novel_task` (generalization) is an unseen *task structure* to
  route, not a pattern to classify;
- `novel_pattern` is neither: a structured detection awaiting
  classification.

## What is blocked

End-to-end execution through the Phase-2 governance chain. Three
frozen gates, each demonstrated (not assumed) by T10 of the proof
battery:

1. **Executive** (`runtime/curiosity/executive/executive.py`):
   `novel_pattern` is not declared anywhere in the frozen vocabulary
   -- not in `LOOP_OWNERSHIP`, not even in `ABSENT_OWNERSHIP`.
   `request_activation` raises `ActivationRefused: UNOWNED: boundary
   class 'novel_pattern' is owned by no curiosity loop`. (Stronger
   than the P3A/P3B boundary: the presentation is not even *named* as
   absent.)
2. **Substrate** (`runtime/curiosity/substrate.py`,
   `CURIOSITY_LOOPS`): `register_loop("discovery_novelty",
   budget_s=60.0)` raises `ValueError: unknown curiosity loop
   'discovery_novelty'; expected one of ('questioning',
   'scientific_inquiry', 'creative_exploration')`. No
   microcontroller can ever spawn under the loop's name.
3. **Run controller**
   (`runtime/curiosity/run_controller/controller.py`): the
   `_loops`/`_inlets` registries (built by CUR-P3A-INT, extended by
   CUR-P3B-INT) hold exactly the three admitted loops -- no
   discovery entry, no injection path for one.

The packet forbids editing frozen files, and the U-1 precedent makes
both the boundary declaration and the vocabulary extension James's
explicit decisions, so this mission cannot open these gates itself.
Bypass routes (runtime map mutation, subclass re-admission, a
parallel run controller, declaring the presentation in the loop's own
module and hoping the executive reads it) were considered and
rejected: the executive validates against its own frozen vocabulary,
so a module-local declaration would be a second vocabulary -- the
same breaking change by another door.

## Exact decisions needed (in order)

1. **James declares the boundary presentation** (frozen
   `boundary.py`): add `BOUNDARY_NOVEL_PATTERN = "novel_pattern"`
   with the `-> discovery_novelty` annotation, and the
   `ABSENT_OWNERSHIP` entry, so the executive names the absence
   honestly. (U-1 class.)
2. **James admits `discovery_novelty` to the curiosity loop
   vocabulary** (frozen `substrate.py`): the closed tuple gains the
   term; the fence stays closed. (U-1 class -- same standing as
   `scientific_inquiry` and `creative_exploration`: a semantic
   capability/loop classification, never evidence that REMOR
   independently possesses discovery reasoning.)
3. **Track owner authorizes** the minimal frozen-file integration
   (or re-dispatches carrying it): `executive.py` LOOP_OWNERSHIP +
   fit branch + view entry; `run_controller/controller.py` registry
   entry + per-loop context + generalized terminal persistence.
4. Re-run this battery plus a new end-to-end discovery proof through
   the real chain (trigger -> executive -> run controller ->
   discovery -> terminal -> fenced store -> return), including the
   Questioning feed through the integrated path.
