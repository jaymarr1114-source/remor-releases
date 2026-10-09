"""BRIDGE-BOOT-1 e2e proof: the bridge is authorized at engine boot.

Runs against a REAL booted engine: serve() constructs _Service on the
serving thread (the production boot path), and the executive under test
is the one boot wired (svc.curiosity_executive) -- never a directly
constructed factory product. A harness that constructs the executive
directly without going through boot proves nothing (anti-simulation).

Checks:
  1. Boot wires a bound executive: serve() -> svc.curiosity_executive is
     a CuriosityExecutive with a non-None acquisition_bridge, and the
     pipeline behind it carries the 2 real governed sources.
  2. Activation fires the pipeline: after the drill-pattern roll-call
     attestation (the factory's contract: the caller attests), a
     BOUNDARY_MISSING_TEACHER decision travels request_activation ->
     activate -> _fire_acquisition; the pipeline really searches its
     sources and returns a truthful no-candidate.
  3. No-candidate stays honest: accepted is False; nothing faked.
  4. Boot fail-safe: when the factory raises, _Service still boots,
     curiosity_executive is None, and the failure is loudly logged
     (previous behavior preserved).
  5. Unbound refusal unchanged: an executive with bridge=None still
     refuses with NO_ACQUISITION_BRIDGE (today's behavior).
"""

from __future__ import annotations

import os
import sys
import tempfile
from unittest import mock


def _boot():
    from swarm_engine.services.http_adapter import serve
    base = tempfile.mkdtemp(prefix="bridgeboot1_")
    server, _ff, _thread, base_url = serve(base, "127.0.0.1", 0)
    svc = server.svc
    return server, svc, base_url


def _shutdown(server):
    try:
        server.shutdown()
    except Exception:
        pass


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


def _attest(svc):
    # Drill pattern (production.py's contract: the caller conducts the
    # roll-call). No production challenge responder exists (Phase-2
    # handler unbuilt; only test doubles), so boot does not attest --
    # the proof does, exactly as the factory documents.
    from swarm_engine.curiosity.rollcall.responder import HonestTestDouble
    gam = svc._curiosity_stack["gam"]
    att = gam.conduct_roll_call("curiosity", HonestTestDouble())
    assert att["classification"] == "MET", att


def test_boot_wires_bound_executive():
    server, svc, base_url = _boot()
    try:
        ex = svc.curiosity_executive
        assert ex is not None, "boot must wire curiosity_executive"
        assert type(ex).__name__ == "CuriosityExecutive", type(ex)
        assert ex._acquisition_bridge is not None, (
            "bridge must be bound at boot, not None")
        pipeline = svc._curiosity_stack["pipeline"]
        assert len(pipeline.sources) == 2, (
            "production source configuration behind the bound bridge")
        print("PASS test_boot_wires_bound_executive: booted engine "
              "carries a bound CuriosityExecutive (%s)" % base_url)
    finally:
        _shutdown(server)


def test_activation_fires_pipeline():
    server, svc, _ = _boot()
    try:
        ex = svc.curiosity_executive
        assert ex is not None
        _attest(svc)
        trigger = _teacher_trigger()
        decision = ex.request_activation(trigger)
        assert decision.approved, "decision must approve"
        outcome = ex.activate(decision)
        result = outcome.result
        assert hasattr(result, "accepted"), (
            "bridge must return the pipeline's AcquisitionResult, got %r"
            % type(result))
        assert result.accepted is False, "empty sources: accepted False"
        assert any("no candidate found" in r for r in result.reasons), (
            "pipeline must truthfully report no-candidate, reasons=%r"
            % (result.reasons,))
        print("PASS test_activation_fires_pipeline: missing_teacher "
              "activation fired the bound pipeline (truthful no-candidate)")
    finally:
        _shutdown(server)


def test_no_candidate_stays_honest():
    server, svc, _ = _boot()
    try:
        ex = svc.curiosity_executive
        _attest(svc)
        outcome = ex.activate(ex.request_activation(_teacher_trigger()))
        result = outcome.result
        assert result.accepted is False
        assert "accepted=False" in outcome.detail, outcome.detail
        print("PASS test_no_candidate_stays_honest: accepted=False "
              "recorded, nothing faked as acquired")
    finally:
        _shutdown(server)


def test_boot_failsafe():
    from swarm_engine.services.http_adapter import _Service
    base = tempfile.mkdtemp(prefix="bridgeboot1_failsafe_")
    db = os.path.join(base, "engine.db")
    with mock.patch(
            "swarm_engine.curiosity.executive.production."
            "build_production_executive",
            side_effect=RuntimeError("simulated construction failure")):
        svc = _Service(db, base)
    assert svc.curiosity_executive is None, (
        "construction failure must leave the executive unbound")
    assert svc.router is not None, "boot must survive the wiring failure"
    print("PASS test_boot_failsafe: factory failure -> executive None, "
          "boot continues (previous behavior preserved)")


def test_unbound_refusal_unchanged():
    from swarm_engine.curiosity.executive.executive import (
        CuriosityExecutive, R_NO_ACQUISITION_BRIDGE, ActivationRefused)
    server, svc, _ = _boot()
    try:
        stack = svc._curiosity_stack
        bare = CuriosityExecutive(
            frm=stack["frm"], enforcement_state_dir=stack["enf_dir"],
            gam=stack["gam"], run_controller=stack["run_controller"],
            acquisition_bridge=None)
        _attest_via(stack)
        trigger = _teacher_trigger()
        decision = bare.request_activation(trigger)
        assert decision.approved
        try:
            bare.activate(decision)
        except ActivationRefused as exc:
            assert R_NO_ACQUISITION_BRIDGE in str(exc), str(exc)
            print("PASS test_unbound_refusal_unchanged: unbound bridge -> "
                  "NO_ACQUISITION_BRIDGE refusal (today's behavior)")
            return
        raise AssertionError("unbound bridge must refuse, not fire")
    finally:
        _shutdown(server)


def _attest_via(stack):
    from swarm_engine.curiosity.rollcall.responder import HonestTestDouble
    att = stack["gam"].conduct_roll_call("curiosity", HonestTestDouble())
    assert att["classification"] == "MET", att


def main() -> int:
    test_boot_wires_bound_executive()
    test_activation_fires_pipeline()
    test_no_candidate_stays_honest()
    test_boot_failsafe()
    test_unbound_refusal_unchanged()
    print("ALL BRIDGE-BOOT-1 E2E CHECKS PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
