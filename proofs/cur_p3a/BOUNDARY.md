# CUR-P3A integration boundary (2026-10-01)

## What was built

`runtime/curiosity/loops/scientific_inquiry/` — the complete Scientific
Inquiry loop controller (loop + inlet + pipeline + provenance), proven
at the stage level by `proofs/cur_p3a/cur_p3a_proof.py` (61/61 checks):
hypothesis formation/falsifiability, prediction derivation, evidence
test, convergence to the charter's terminal states, C-6.1 loop-level
refusal of foreign boundaries, C-2.2 provenance + real fenced-store
round-trip, restore() lineage.

## What is blocked

End-to-end execution through the Phase-2 governance chain. Three
frozen gates, each demonstrated (not assumed) by T10 of the proof
battery:

1. **Executive** (`runtime/curiosity/executive/executive.py`,
   `LOOP_OWNERSHIP`): `request_activation` for
   `boundary_class="hypothesis_candidate"` raises
   `ActivationRefused: LOOP_ABSENT: boundary class
   'hypothesis_candidate' is owned by scientific_inquiry (Phase 3 --
   no loop registered)`. The executive names the absence instead of
   routing -- working as designed, but the inquiry cannot start.

2. **Substrate** (`runtime/curiosity/substrate.py`,
   `CURIOSITY_LOOP_SET`): `register_loop("scientific_inquiry",
   budget_s=60.0)` raises `ValueError: unknown curiosity loop
   'scientific_inquiry'; expected one of ('questioning',)`. No
   microcontroller can ever spawn under the loop's name -- not on the
   curiosity substrate, not on the base substrate (whose own
   `LOOPS` vocabulary is the six level-2 loops).

3. **Run controller**
   (`runtime/curiosity/run_controller/controller.py`):
   `__init__` hard-codes `self._loop = QuestioningLoop()` and
   `self._inlet = QuestioningLoopInlet(self._loop)`; `_activate`,
   `_step_inquiry`, `_execute_kill`, and `resume_inquiry` all drive
   that instance. There is no loop registry or injection point.

## Why the current mechanism cannot cross it

The Phase-2 chain was built, deliberately, to admit exactly one loop:
the U-1 decision (2026-09-30) fenced the curiosity vocabulary at
`("questioning",)` with the docstring rule that widening it "would be
a breaking change requiring James's explicit decision". This
mission's packet forbids editing any frozen Phase-1/2 file, so the
three gates above cannot be opened from inside the mission. Runtime
mutation of the module-level maps, subclassing the substrate to
re-admit the name, or duplicating the run controller around the
frozen one would each be the same breaking change by another door --
dishonest, and in the duplication case a violation of the
no-duplication rule and the one-authoritative-runtime invariant.

## The exact crossing mechanism (decision requested)

1. **James's decision (U-1 class):** extend the curiosity loop
   vocabulary to admit `"scientific_inquiry"` -- the Phase-3
   counterpart of the U-1 "questioning" approval.
2. **Track-owner authorization** for the minimal frozen-file
   integration edits (or a re-dispatched mission carrying it):
   - `substrate.py`: `CURIOSITY_LOOPS += ("scientific_inquiry",)`
   - `executive.py`: move `BOUNDARY_HYPOTHESIS_CANDIDATE` and
     `BOUNDARY_NOVEL_OBSERVATION` from `ABSENT_OWNERSHIP` to
     `LOOP_OWNERSHIP` (plus the `_check_fit` branch and the
     `executive_view` entry for the new loop)
   - `run_controller/controller.py`: replace the hard-coded
     `self._loop`/`self._inlet` with a loop-name-keyed registry
     (`"questioning"` and `"scientific_inquiry"`), dispatched by
     `inq.loop` in `_activate`/`_step_inquiry`/`_execute_kill`/
     `resume_inquiry`; generalize `_persist_terminal`'s
     questioning-specific payload fields.
3. Re-run this mission's battery plus a new end-to-end inquiry proof
   (trigger -> executive -> run controller -> scientific inquiry ->
   terminal -> fenced store) through the real chain.

Until (1)+(2) land, the loop controller is complete but unrunnable:
proven machinery awaiting its governance decision.
