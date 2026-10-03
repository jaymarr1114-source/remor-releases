# CUR-P3A mission report — Scientific Inquiry loop controller (2026-10-01)

**Mission:** build the Scientific Inquiry loop (Phase 3, first loop
after questioning): hypothesis candidate / novel observation →
scientific pipeline → terminal state + provenance-stamped finding,
through the full Phase-2 governance chain.
**Worktree:** `~/workspace/worktrees/cur-p3a`, branch `cur-p3a`,
pinned at canonical `44e19e4` (verified 2026-10-01 ~11:12 EDT).
**Battery:** `proofs/cur_p3a/gate_run.sh` → `cur_p3a_proof.py`,
**61/61 checks green** (fresh process, real machinery throughout).

## Per-mandate verdicts

| # | Mandate | Verdict |
|---|---------|---------|
| 1 | Fresh structural re-map before execution | **PROVEN** — re-mapped the live tree at `44e19e4`: the questioning template (`loops/questioning/loop.py`), the frozen executive ownership maps, the frozen run controller's hard-coded loop binding, the frozen substrate vocabulary, the frozen evidence terminal vocabulary (which already carries `HYPOTHESIS_SUPPORTED` / `HYPOTHESIS_REFUTED` / `INSUFFICIENT_EVIDENCE` / `INCONCLUSIVE` — the store was designed for this loop's findings). |
| 2 | Build the loop controller as NEW files only | **PROVEN** — `runtime/curiosity/loops/scientific_inquiry/` (`loop.py`, `__init__.py`): `ScientificInquiryLoop` + `ScientificInquiryLoopInlet` mirroring the questioning template (loop/inlet, per-stage MCs nested under a root MC, `cognize` through the governed inlet, `charge` per step, `SubstrateRefused`), five-stage pipeline (observe → hypothesize → predict → test → conclude) as a `GraphController` structural graph, pure mechanical stage functions, provenance stamping, `restore()` lineage. Zero frozen files touched. |
| 3 | Converge to the charter's terminal states through the full Phase-2 governance chain | **BLOCKED** — the loop converges to the enumerated terminal states at the stage level (T12), but end-to-end execution through the executive → run controller → substrate chain is refused by three frozen gates (see Boundary). |
| 4 | C-2.2: store refuses provenance-less/terminal-less findings | **PROVEN** — the loop stamps complete provenance on every finding (`stamp_provenance`/`build_finding`); the REAL fenced `CuriosityEvidenceStore` accepts the loop's finding and refuses a provenance-stripped and a terminal-invented finding through the loop's own curiosity-domain write path (T09). |
| 5 | C-6.1: no fixed pipeline — Executive selects, loops refuse foreign boundaries | **PROVEN at loop level; executive selection BLOCKED** — the inlet AND the loop refuse `imprecise_question`, `generative_prompt`, `novel_task` with `LoopRefused` (T06); inquiry boundaries admitted with correct state shape (T07). The executive cannot select the loop: it raises `LOOP_ABSENT` for the inquiry boundary classes (T10, gate 1). |
| 6 | C-6.2: microcontrollers retire inside the curiosity forest | **NOT VERIFIED THIS SHIFT** — no MC can spawn under the loop's name (substrate vocabulary closed, T10 gate 2); the abort/retire path is written to the template but unexecuted. |
| 7 | C-4.2/C-4.3 budget behaviors | **NOT VERIFIED THIS SHIFT** — same cause: the loop cannot execute, so no-budget refusal and forced-breach checkpointing are unproven for this loop (the mechanisms are Phase-2 machinery, proven for questioning in CUR-P2). |
| 8 | Kill/resume lineage (PLOOP-12 KR pattern) | **PARTIAL** — `restore()` rebuilds runnable state from a checkpoint (T11, pure); `abort()` retires MCs per the template (unexecuted — same cause). |

## The boundary (terminal condition: decision only James can make)

Three frozen gates refuse `"scientific_inquiry"`, each demonstrated by
T10 (real refusals, not assumptions) — full detail in
`proofs/cur_p3a/BOUNDARY.md`:

1. Executive `LOOP_OWNERSHIP` has no entry for the inquiry boundary
   classes → `ActivationRefused: LOOP_ABSENT`.
2. `CuriositySubstrate.register_loop` → `ValueError: unknown curiosity
   loop 'scientific_inquiry'` (vocabulary fenced at `("questioning",)`
   by the U-1 decision).
3. The run controller hard-codes `QuestioningLoop()`/`QuestioningLoopInlet()`
   — no registry, no injection point.

The packet forbids editing frozen Phase-1/2 files, and the U-1
precedent makes vocabulary extension James's explicit decision, so
this mission cannot open these gates itself. Bypass routes (runtime
map mutation, subclass re-admission, a parallel run controller) were
considered and rejected as the same breaking change by another door.

## Exact next executable boundary

1. **James decides (U-1 class):** admit `"scientific_inquiry"` to the
   curiosity loop vocabulary.
2. **Track owner authorizes** the minimal frozen-file integration (or
   re-dispatches carrying it): `substrate.py` vocabulary;
   `executive.py` `LOOP_OWNERSHIP` + fit branch + view entry;
   `run_controller/controller.py` loop registry dispatched by
   `inq.loop` + generalized terminal persistence.
3. Re-run this battery plus a new end-to-end inquiry proof through the
   real chain (trigger → executive → run controller → scientific
   inquiry → terminal → fenced store → return).

## What remains unproven

Everything requiring execution: executive selection of the loop, FRM
grant admission, per-stage MC spawn/charge/retire on the curiosity
substrate, C-6.2 forest containment, C-4.2/C-4.3 budget paths,
kill/resume against the real checkpoint store, terminal routing of a
scientific-inquiry finding, attribution of a scientific-inquiry
inquiry. The loop controller itself is complete and stage-proven;
it is unrunnable until the governance decision lands.

## Incidents

None. No frozen file was written; no battery was run in parallel
with other missions; no verification claim is made beyond what the
battery demonstrates.
