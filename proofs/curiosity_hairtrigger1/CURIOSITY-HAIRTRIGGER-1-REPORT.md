# CURIOSITY-HAIRTRIGGER-1 Mission Report

**Mission:** Verify and wire the external acquisition hair-trigger
**Worktree:** `~/workspace/worktrees/curiosity-hairtrigger-1`, branch `curiosity-hairtrigger-1-work`
**Status:** GATE-READY. Report, do not land.

## What was found

James was right: the hair-trigger did not exist.

**What existed:**
- `CuriosityExecutive` — fail-closed activation gate routing 6 boundary classes to 4 loops
- Loops emit `GAP_ACQUISITION` on `TERMINAL_INSUFFICIENT` (scientific_inquiry, creative_exploration, discovery_novelty)
- `AcquisitionPipeline.acquire()` — searches sources for `CapabilityRequirement`
- `SubstrateFetcher.fetch()` — governed fetch (trusted index, sha256, governor network-effect check, audit trail)
- `ingest_external_demonstration()` — ingests handed-in demonstrations

**What was missing:**
- NO boundary class for "no teacher" / "missing external substrate"
- NOTHING consumed `GAP_ACQUISITION` outside the loops (verified: zero consumers in the tree)
- ZERO coupling between `runtime/curiosity/` and `runtime/acquisition/`
- The executive had no path from gap → acquisition

The loops could NAME the gap. Nothing could FIRE on it.

## What was built (minimal wiring)

**`runtime/curiosity/executive/boundary.py`:**
- New `BOUNDARY_MISSING_TEACHER = "missing_teacher"`

**`runtime/curiosity/executive/executive.py`:**
- `LOOP_OWNERSHIP[BOUNDARY_MISSING_TEACHER] = LOOP_SCIENTIFIC_INQUIRY`
- New optional `acquisition_bridge` constructor param (callable: `bridge(requirement) -> result`)
- New refusal code `R_NO_ACQUISITION_BRIDGE`
- Fit check: `missing_teacher` trigger must name the teacher gap in `question_text` (non-empty) + bounded objective
- `activate()`: when trigger presents `missing_teacher`, fires `_fire_acquisition()` instead of entering the run controller
- `_fire_acquisition()`: builds `CapabilityRequirement` from trigger (name, description, keywords, origin provenance) → invokes bridge → returns `CuriosityLoopOutcome`. Fail-closed: raises `ActivationRefused(NO_ACQUISITION_BRIDGE)` when unbound.

**`runtime/curiosity/loops/scientific_inquiry/loop.py`:**
- Added `BOUNDARY_MISSING_TEACHER` to `INQUIRY_BOUNDARIES` (defense in depth; the executive fires the bridge directly, the loop never runs its graph for this class)

**`proofs/curiosity_hairtrigger1/`:**
- `proof_battery.py`: 6 batteries
- `gate_run.sh`: gate-ready

Total diff: +329/-1 across 3 runtime files + 2 proof files.

## Evidence

Gate `proofs/curiosity_hairtrigger1/gate_run.sh`: **ALL PASS**
- b1_ownership: `missing_teacher` → `scientific_inquiry` ✓
- b2_fit: empty description refused (FIT); valid approved ✓
- b3_activation: `request_activation` approves valid trigger ✓
- b4_fires_bridge: `activate()` invokes bridge (run_controller NOT called); requirement carries trigger text + provenance ✓
- b5_fail_closed: no bridge → `ActivationRefused(NO_ACQUISITION_BRIDGE)` ✓
- b6_end_to_end: real `AcquisitionPipeline` with test source receives the `CapabilityRequirement`; honestly reports "no candidate found" ✓
- Regressions: `tests/curiosity/` 49/49 PASS ✓

## Exact next boundary

**Wiring the bridge to a real acquisition source.** The executive now fires, but the `acquisition_bridge` must be bound to a real `AcquisitionPipeline` with real sources at deployment time. Currently no production code constructs the executive with a bridge — the wiring is proven but not yet connected in any running system. The next step is binding it in the curiosity executive's construction site (wherever `CuriosityExecutive(...)` is instantiated for production).

**AUDIO-DISTILL-2's teacher gap** can now be presented as a `missing_teacher` trigger. The gap → acquisition path exists. Whether the acquisition finds a real teacher demonstration depends on the sources bound to the pipeline.

## What remains unproven

1. Felix's independent gate re-run (this is the coordinator's claim)
2. The bridge bound in a production construction site (no production `CuriosityExecutive` instantiation currently passes `acquisition_bridge`)
3. A real "no teacher" gap from AUDIO-DISTILL-2 flowing through this path end-to-end
4. Whether any trusted index actually has teacher demonstrations for the specific gaps (the pipeline honestly reports "no candidate found" when sources are empty — that path is proven, but a successful acquisition is not)
5. Device proof (standing rule)

## Incidents

None. No protected-tree touches. No mid-run repairs.
