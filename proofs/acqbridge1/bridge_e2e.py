"""ACQ-BRIDGE-1 e2e proof: the bound bridge fires into the real pipeline.

Runs in a real process with real components (no mocks for the units under
test): the production executive factory builds the real governance stack,
the bridge invokes the real AcquisitionPipeline with real governed sources,
and the BOUNDARY_MISSING_TEACHER decision travels the executive's actual
request_activation -> activate -> _fire_acquisition path.

Checks:
  1. Bound bridge, empty sources -> pipeline really searches -> truthful
     no-candidate (accepted=False), no refusal. The firing happened.
  2. Bound bridge, candidate offered to LocalSource -> pipeline discovers it
     (the search is real, not hard-coded empty).
  3. Negative control: bridge unbound -> ActivationRefused with
     NO_ACQUISITION_BRIDGE (today's behavior, unchanged).
  4. Refusal intact: the no-candidate outcome is honest (accepted=False);
     nothing is faked as acquired.
"""

from __future__ import annotations

import sys
import tempfile


def _build_bound_stack():
    from swarm_engine.curiosity.executive.production import (
        build_production_executive)
    from swarm_engine.curiosity.rollcall.responder import HonestTestDouble
    base = tempfile.mkdtemp(prefix="acqbridge1_")
    stack = build_production_executive(base_dir=base)
    # MET roll-call attestation (drill pattern): the activation gate
    # requires it; without MET the decision refuses before the bridge.
    att = stack["gam"].conduct_roll_call("curiosity", HonestTestDouble())
    assert att["classification"] == "MET", att
    return stack


def _teacher_trigger():
    from swarm_engine.curiosity.executive.boundary import (
        new_trigger, BOUNDARY_MISSING_TEACHER)
    return new_trigger(
        boundary_class=BOUNDARY_MISSING_TEACHER,
        question_text=(
            "No teacher demonstrates speech-to-text transcription; "
            "the capability must come from governed external acquisition."),
        bounded_objective=(
            "Acquire a speech-to-text teacher via governed external acquisition."),
        origin="CURIOUSITY_INITIATED")


def test_bound_bridge_searches_and_reports_no_candidate():
    stack = _build_bound_stack()
    ex = stack["executive"]
    trigger = _teacher_trigger()
    decision = ex.request_activation(trigger)
    assert decision.approved, "decision must approve"
    outcome = ex.activate(decision)
    result = outcome.result
    # The bridge returned the pipeline's real AcquisitionResult.
    assert hasattr(result, "accepted"), (
        "bridge must return the pipeline's AcquisitionResult, got %r" % type(result))
    assert result.accepted is False, (
        "empty sources: accepted must be False")
    assert any("no candidate found" in r for r in result.reasons), (
        "pipeline must truthfully report no-candidate, reasons=%r" % (result.reasons,))
    # The pipeline really searched its real sources (not a stubbed empty).
    assert len(stack["pipeline"].sources) == 2, "production source configuration"
    print("PASS test_bound_bridge_searches_and_reports_no_candidate: "
          "bridge fired, pipeline searched 2 real sources, truthful no-candidate")


def test_bound_bridge_discovers_offered_candidate():
    from swarm_engine.acquisition.pipeline import Candidate
    stack = _build_bound_stack()
    ex = stack["executive"]
    local_source = stack["local_source"]
    assert local_source is not None
    trigger = _teacher_trigger()
    cand = Candidate(
        name="teacher:%s" % trigger.trigger_id,
        source="proof-offer",
        code="def capability():\n    return 42\n",
        entrypoint="capability")
    local_source.offer("teacher:%s" % trigger.trigger_id, cand)
    decision = ex.request_activation(trigger)
    outcome = ex.activate(decision)
    result = outcome.result
    # Discovery proven: the pipeline found the candidate (accepted or
    # rejected-with-reasons -- either way it was really discovered, not
    # hard-coded absent).
    discovered = result.accepted or any(
        cand.name in str(r.get("candidate", "")) for r in result.rejected)
    assert discovered, (
        "offered candidate must be discovered by the pipeline; "
        "accepted=%r rejected=%r reasons=%r"
        % (result.accepted, result.rejected, result.reasons))
    assert not any("no candidate found" in r for r in result.reasons), (
        "with an offered candidate the pipeline must not report no-candidate")
    print("PASS test_bound_bridge_discovers_offered_candidate: "
          "pipeline really discovered the offered candidate")


def test_unbound_bridge_refuses_honestly():
    from swarm_engine.curiosity.executive.executive import (
        CuriosityExecutive, R_NO_ACQUISITION_BRIDGE, ActivationRefused)
    stack = _build_bound_stack()
    # Same real components, bridge deliberately unbound: today's behavior.
    bare = CuriosityExecutive(
        frm=stack["frm"], enforcement_state_dir=stack["enf_dir"],
        gam=stack["gam"], run_controller=stack["run_controller"],
        acquisition_bridge=None)
    trigger = _teacher_trigger()
    decision = bare.request_activation(trigger)
    assert decision.approved
    try:
        bare.activate(decision)
    except ActivationRefused as exc:
        assert R_NO_ACQUISITION_BRIDGE in str(exc), str(exc)
        print("PASS test_unbound_bridge_refuses_honestly: "
              "unbound bridge -> NO_ACQUISITION_BRIDGE refusal (unchanged)")
        return
    raise AssertionError("unbound bridge must refuse, not fire")


def test_no_candidate_stays_honest():
    # Mandate 4: a truthful no-candidate must never present as acquired.
    stack = _build_bound_stack()
    ex = stack["executive"]
    trigger = _teacher_trigger()
    outcome = ex.activate(ex.request_activation(trigger))
    result = outcome.result
    assert result.accepted is False
    assert "acquisition fired" in outcome.detail
    assert "accepted=False" in outcome.detail, outcome.detail
    print("PASS test_no_candidate_stays_honest: "
          "no-candidate outcome records accepted=False, nothing faked")


def main() -> int:
    test_bound_bridge_searches_and_reports_no_candidate()
    test_bound_bridge_discovers_offered_candidate()
    test_unbound_bridge_refuses_honestly()
    test_no_candidate_stays_honest()
    print("ALL ACQ-BRIDGE-1 E2E CHECKS PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
