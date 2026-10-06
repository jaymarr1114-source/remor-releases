"""CURIOSITY-HAIRTRIGGER-1 proof battery.

Proves: a mission result reporting "no teacher" (BOUNDARY_MISSING_TEACHER)
fires the Curiosity executive's acquisition path -- not just the package
existing, but the actual call from trigger -> executive -> acquisition bridge.

Batteries:
  b1: boundary class exists, owned by scientific_inquiry
  b2: fit check -- empty teacher description refused; valid passes
  b3: request_activation approves a valid missing_teacher trigger
  b4: activate() fires the acquisition bridge (run_controller NOT called)
  b5: fail-closed -- no bridge bound -> ActivationRefused(NO_ACQUISITION_BRIDGE)
  b6: end-to-end -- real AcquisitionPipeline with a test source receives
      the CapabilityRequirement built from the trigger
"""

import sys
import os
import tempfile

# Worktree root on sys.path
_HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(_HERE))
sys.path.insert(0, os.path.join(REPO, "pylib"))
sys.path.insert(0, REPO)

from swarm_engine.curiosity.executive.boundary import (
    BOUNDARY_MISSING_TEACHER,
    new_trigger,
)
from swarm_engine.curiosity.executive.executive import (
    LOOP_OWNERSHIP,
    R_FIT,
    R_NO_ACQUISITION_BRIDGE,
    ActivationRefused,
    CuriosityExecutive,
)
from swarm_engine.curiosity.substrate import LOOP_SCIENTIFIC_INQUIRY


# -- test doubles --------------------------------------------------------

class FakeGrant:
    def __init__(self):
        self.budget_s = 60.0
        self.max_concurrent = 2
    def as_dict(self):
        return {"budget_s": self.budget_s,
                "max_concurrent": self.max_concurrent}


class FakeRound:
    def __init__(self):
        self.grants = {"curiosity": FakeGrant()}
        self.epoch_id = 7
        self.mid_epoch_refusal = False


class FakeFRM:
    def evaluate_round(self, **kwargs):
        return FakeRound()


class FakeGAM:
    def roll_call_status(self, domain):
        return {"latest_attestation": {
            "attestation_id": "att_test",
            "classification": "MET",
            "validation_detail": "test",
        }}


class FakeRunController:
    def __init__(self):
        self.run_inquiry_called = False
    def run_inquiry(self, decision):
        self.run_inquiry_called = True
        raise AssertionError("run_controller must NOT be called for missing_teacher")
    def loop_view(self, loop):
        return {}


def make_executive(acquisition_bridge=None):
    tmpdir = tempfile.mkdtemp()
    return CuriosityExecutive(
        frm=FakeFRM(),
        enforcement_state_dir=tmpdir,  # no record -> RUNNING by bootstrap semantic
        gam=FakeGAM(),
        run_controller=FakeRunController(),
        acquisition_bridge=acquisition_bridge,
    )


def make_trigger(question_text="AUDIO-DISTILL-2 needs teacher demonstrations for voice distillation; no teacher available"):
    return new_trigger(
        boundary_class=BOUNDARY_MISSING_TEACHER,
        question_text=question_text,
        bounded_objective="Acquire teacher demonstrations for audio distillation",
        origin="PRIMARY_REQUESTED",
    )


# -- batteries ------------------------------------------------------------

def b1_ownership():
    """BOUNDARY_MISSING_TEACHER is owned by scientific_inquiry."""
    assert BOUNDARY_MISSING_TEACHER == "missing_teacher", \
        f"unexpected value: {BOUNDARY_MISSING_TEACHER}"
    owner = LOOP_OWNERSHIP.get(BOUNDARY_MISSING_TEACHER)
    assert owner == LOOP_SCIENTIFIC_INQUIRY, \
        f"owner is {owner}, expected {LOOP_SCIENTIFIC_INQUIRY}"
    print("b1_ownership: PASS")


def b2_fit():
    """Empty teacher description refused; valid passes."""
    exe = make_executive(acquisition_bridge=lambda req: None)
    # empty -> refused
    bad = make_trigger(question_text="   ")
    try:
        exe.request_activation(bad)
        raise AssertionError("empty question_text should be refused")
    except ActivationRefused as e:
        assert R_FIT in str(e), f"expected FIT refusal, got: {e}"
    # valid -> approved
    good = make_trigger()
    decision = exe.request_activation(good)
    assert decision.approved, "valid trigger should be approved"
    assert decision.loop == LOOP_SCIENTIFIC_INQUIRY
    print("b2_fit: PASS")


def b3_activation():
    """request_activation approves a valid missing_teacher trigger."""
    exe = make_executive(acquisition_bridge=lambda req: None)
    decision = exe.request_activation(make_trigger())
    assert decision.approved
    assert decision.trigger.boundary_class == BOUNDARY_MISSING_TEACHER
    assert decision.grant.budget_s > 0
    print("b3_activation: PASS")


def b4_fires_bridge():
    """activate() invokes the acquisition bridge, NOT the run controller."""
    fired = []
    def bridge(requirement):
        fired.append(requirement)
        return {"accepted": True, "requirement": requirement.name}
    exe = make_executive(acquisition_bridge=bridge)
    decision = exe.request_activation(make_trigger())
    outcome = exe.activate(decision)
    assert outcome.entered, "outcome should show entered"
    assert len(fired) == 1, f"bridge fired {len(fired)} times, expected 1"
    req = fired[0]
    assert "teacher" in req.name, f"requirement name: {req.name}"
    assert "AUDIO-DISTILL-2" in req.description, \
        f"requirement description should carry the trigger text: {req.description}"
    assert req.origin["boundary_class"] == BOUNDARY_MISSING_TEACHER
    assert not exe._run_controller.run_inquiry_called, \
        "run_controller.run_inquiry must NOT be called for missing_teacher"
    print("b4_fires_bridge: PASS")


def b5_fail_closed():
    """No bridge bound -> ActivationRefused with NO_ACQUISITION_BRIDGE."""
    exe = make_executive(acquisition_bridge=None)
    decision = exe.request_activation(make_trigger())
    assert decision.approved, "activation should approve (bridge checked at fire time)"
    try:
        exe.activate(decision)
        raise AssertionError("should refuse when no bridge bound")
    except ActivationRefused as e:
        assert R_NO_ACQUISITION_BRIDGE in str(e), \
            f"expected NO_ACQUISITION_BRIDGE, got: {e}"
    print("b5_fail_closed: PASS")


def b6_end_to_end():
    """Real AcquisitionPipeline receives the requirement via the bridge."""
    from swarm_engine.acquisition.pipeline import (
        AcquisitionPipeline, CapabilityRequirement)
    received = []
    class TestSource:
        def search(self, requirement):
            received.append(requirement)
            return []  # no candidates -> honest "no candidate found"
    pipeline = AcquisitionPipeline(sources=[TestSource()], provenance=None)
    def bridge(requirement):
        return pipeline.acquire(requirement)
    exe = make_executive(acquisition_bridge=bridge)
    decision = exe.request_activation(make_trigger(
        question_text="Need teacher demos for Kokoro voice distillation"))
    outcome = exe.activate(decision)
    assert len(received) == 1, "pipeline source should receive exactly one requirement"
    req = received[0]
    assert isinstance(req, CapabilityRequirement)
    assert "Kokoro" in req.description
    # Pipeline honestly reports no candidates (test source is empty)
    assert outcome.result.stage == "discovery"
    assert "no candidate found" in outcome.result.reasons[0]
    print("b6_end_to_end: PASS")


def main():
    b1_ownership()
    b2_fit()
    b3_activation()
    b4_fires_bridge()
    b5_fail_closed()
    b6_end_to_end()
    print("\nALL BATTERIES PASS")


if __name__ == "__main__":
    main()
