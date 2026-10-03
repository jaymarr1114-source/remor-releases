# CUR-P3C mission report — Discovery/Novelty loop controller (2026-10-03)

**Mission:** build the Discovery/Novelty loop (Phase 3, third loop
after questioning): adjacent-space exploration → novelty detection →
classification → NOVELTY_CLASSIFIED, feeding Questioning when the
novelty is not immediately understood, through the full Phase-2
governance chain.
**Worktree:** `~/workspace/worktrees/cur-p3c`, branch `cur-p3c`,
pinned at canonical `25cc5f5` (verified 2026-10-03 ~19:25 EDT).
**Battery:** `proofs/cur_p3c/gate_run.sh` → `cur_p3c_proof.py`,
**61/61 checks green** (fresh process, real machinery throughout).

## Classification note (James's decisions #3/#5 framing)

`"discovery_novelty"` is proposed as a semantic capability/loop
classification only — never evidence that REMOR independently
possesses discovery reasoning. This report claims only mechanism
behavior: the loop controller exists, its stage machinery executes,
and its refusals are real.

## Per-mandate verdicts

| # | Mandate | Verdict |
|---|---------|---------|
| 1 | Fresh structural re-map before execution | **PROVEN** — re-mapped the live tree at `25cc5f5`: the three landed loop templates, the frozen executive ownership maps, the run controller's two registries, the frozen substrate vocabulary, the frozen evidence terminal vocabulary (which already carries `NOVELTY_CLASSIFIED`), theory §7 (Discovery / Novelty Controller). |
| 2 | Boundary-presentation design (explicit) | **PROVEN** — `novel_pattern`: a detected pattern in adjacent space whose classification is not established. Validated against theory §7 (the loop detects *patterns*: unknown relationships, anomalies, unexpected patterns, ...) and distinguished from `novel_observation` (reproducibility hypothesis, scientific_inquiry) and `novel_task` (task routing, generalization). Recorded in `proofs/cur_p3c/BOUNDARY.md`. |
| 3 | Build the loop controller as NEW files only | **PROVEN** — `runtime/curiosity/loops/discovery_novelty/` (`loop.py`, `__init__.py`): `DiscoveryNoveltyLoop` + `DiscoveryNoveltyLoopInlet` mirroring the P3B stage pattern (loop/inlet, per-stage MCs nested under a root MC, `cognize` through the governed inlet, `charge` per step, `SubstrateRefused`), five-node discovery graph (explore → detect → investigate → verify → classify), pure mechanical stage functions, provenance stamping, JSON-native checkpoint forms, `restore()` lineage, the Questioning feed path. Zero frozen files touched. |
| 4 | Stage-level proofs | **PROVEN** — exploration with lineage (T02); novelty assessment with the core adversarial (T03: exact_repeat / trivial_variation / already_classified / no_stable_structure all refused with named reasons; genuine novelty admitted with distinguishing markers); investigation for novel sightings only (T04); verified-substrate check with named gaps (T05); classification convergence incl. the resolved-question semantics (T06); the Questioning feed artifact (T06/T12); C-2.2 real fenced-store round-trip (T09); restore() from JSON-native forms (T11); end-to-end pure-stage convergence both paths (T12); NODE_GOALS relevance floor (T13, the permanent P3A-INT lesson check). |
| 5 | C-6.1: no fixed pipeline — loops refuse foreign boundaries | **PROVEN at loop level; executive selection BLOCKED** — the inlet AND the loop refuse `imprecise_question`, `hypothesis_candidate`, `generative_prompt`, `formal_proof` with `LoopRefused` (T07); `novel_pattern` admitted with correct state shape; pattern-less presentations refused. The executive cannot route the presentation: it is not declared (T10). |
| 6 | Integration boundary | **PROVEN** — three frozen gates demonstrated by real calls (T10): executive `request_activation` → `ActivationRefused: UNOWNED` (`novel_pattern` is not even in `ABSENT_OWNERSHIP` — stronger than the P3A/P3B boundary); `register_loop("discovery_novelty")` → `ValueError`; run-controller registry has no discovery entry. Bypass routes considered and rejected explicitly. |
| 7 | Battery discipline | **PROVEN** — gate_queue.md checked (the CUR-P3B-INT main-chat gate takes precedence; this battery is light/mechanical, seconds); load checked; niced; halt-on-first-fail. |
| 8 | gate_run.sh | **PROVEN** — 61/61, exit 0, one fresh invocation; pin check uses merge-base ancestry (the P3B lesson); no hardcoded home paths (derived from `__file__`, James 2026-10-03). |
| 9 | Report-don't-land | **PROVEN** — neutral commit message, no verification claims; canonical untouched. |

## Design decisions made in-mission (all recorded, none hidden)

1. **Resolved-question semantics** (mechanism repair during the
   battery, not a test fix): the first battery run showed the pure
   `investigate_pattern` always leaves open questions, so no novelty
   could ever converge to NOVELTY_CLASSIFIED — the loop would only
   ever feed Questioning. The repair: `verify_pattern` now resolves
   the investigation's open questions whose content terms the
   verified entry covers (≥ 2 shared terms, mechanical); the
   substrate answers them, so they are not fed onward. Genuinely
   unanswered questions still feed Questioning. This is the honest
   reading of theory §7 (VERIFY answers what it can; what remains
   unanswered goes to QUESTION).
2. **JSON-native checkpoint forms** (the CUR-P3B-INT lesson, applied
   pre-battery): all loop state holds `asdict()` forms; `restore()`
   rebuilds the dataclasses; T11 proves `json.dumps(state)`
   succeeds.
3. **NODE_VERIFY goal reworded** for the relevance floor (the
   CUR-P3A-INT lesson): first wording scored 0.300 vs the 0.34
   floor; reworded in the objective's literal vocabulary; all five
   now ≥ 0.50 (T13, permanent check).

## What remains unproven

Everything requiring execution: executive routing of the
presentation, FRM grant admission, per-stage MC spawn/charge/retire
on the curiosity substrate, C-6.2 forest containment, C-4.2/C-4.3
budget paths, kill/resume against the real checkpoint store,
terminal routing of a discovery finding, attribution of a discovery
run, and the Questioning feed through the integrated path. The loop
controller itself is complete and stage-proven; it is unrunnable
until the two governance decisions land.

## Incidents

1. **Mechanism repair in-mission** (resolved-question semantics,
   above) — found by the battery's first run, root-caused to the
   investigate→classify contract, repaired in the mechanism (not the
   test), empirically verified (T06/T12 green).
2. **Battery script truncated on first write** — the proof file was
   cut mid-write by the tool; detected immediately (syntax error on
   first run), rewritten in two chunks, no mechanism impact.
3. No frozen files written; no parallel batteries; no claims beyond
   what the battery demonstrates.

## Exact next executable boundary

1. **James decides (U-1 class, two parts):** (a) declare the
   `novel_pattern` boundary presentation in frozen `boundary.py`
   (the presentation is not even named as absent today); (b) admit
   `discovery_novelty` to the curiosity loop vocabulary — both as
   semantic capability/loop classification only, never as evidence
   of independent discovery reasoning.
2. **Track owner authorizes** the minimal frozen-file integration:
   `executive.py` LOOP_OWNERSHIP + fit branch + view entry;
   `run_controller/controller.py` registry entry + per-loop context
   + generalized terminal persistence.
3. Re-run this battery plus a new end-to-end discovery proof through
   the real chain, including the Questioning feed through the
   integrated path.
