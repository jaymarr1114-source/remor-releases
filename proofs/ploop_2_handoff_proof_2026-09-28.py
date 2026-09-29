"""PLOOP-2 durable proof: contractual loop-to-loop handoffs.

End-to-end, all real machinery, no fixtures, fresh process. The handoff
contract (runtime/core/executive/handoff.py) under test:

  H1  classification: real result types -> terminal states (tick summary,
      DispatchResult, DistillationResult, QuarantineDiagnosis-shaped,
      not-entered outcome).
  H2  table coverage: every (loop, terminal_state) pair has a DECLARED
      answer in HANDOFF_ROUTES (structural completeness, all six loops).
  T1  run -> acquisition: real tick surfaces a real open gap ->
      LoopHandoff produced (real GapRecord evidence, measured resource
      accounting, chain_depth 1) -> accepted -> acquisition really
      entered through the executive. Gap stays open (nothing faked).
  T2  distillation -> generalization: real delta distilled (real
      success) -> handoff with explicit novel spec -> novel_task
      boundary validated -> generalization really entered through the
      executive (real DistillationResult out, success or named refusal --
      the loop's real behavior either way).
  T3  contractual terminals: acquisition OPEN -> produce returns None;
      acceptance CANDIDATE -> produce returns None (declared answers,
      never silent drops).
  V1  tampered handoff (to_loop does not own the boundary kind) ->
      accept raises HandoffRefused, loudly.
  V2  handoff with empty evidence / unmeasured resources -> validate
      raises HandoffRefused, loudly.
  V3  chain depth beyond MAX_HANDOFF_DEPTH -> produce raises
      HandoffRefused, loudly (no ping-pong).
  V4  distillation converged WITHOUT novel spec in context -> produce
      raises HandoffRefused, loudly (novelty never auto-generated,
      never silently skipped).

Production-wiring residual (named, not hidden): the tick still dispatches
gaps inline (V10-P2 behavior, unchanged); the handoff may therefore
surface a gap the tick already dispatched, and the acquisition inlet
re-dispatches it -- redundant but real. The future seam
(SEAM_EXECUTIVE.md) has the tick surface instead of inline-dispatch.

Imports resolve against the tree under test: PLOOP2_WT env var, else
~/workspace/ploop-2-work (this mission's worktree).
"""

import os
import sys
import tempfile
import uuid

WT = os.environ.get(
    "PLOOP2_WT",
    os.path.expanduser("~/workspace/ploop-2-work"))
sys.path.insert(0, os.path.join(WT, "pylib"))

PASS_N = 0


def check(name, cond, detail=""):
    global PASS_N
    PASS_N += 1
    print(("PASS " if cond else "FAIL ") + name +
          (f" -- {detail}" if detail else ""), flush=True)
    if not cond:
        raise SystemExit(f"PLOOP-2 PROOF FAILED at: {name} {detail}")


def expect_refused(name, fn, detail=""):
    """The contract must refuse LOUDLY: HandoffRefused, never silent."""
    global PASS_N
    from swarm_engine.core.executive.handoff import HandoffRefused
    PASS_N += 1
    try:
        fn()
    except HandoffRefused as exc:
        print(f"PASS {name} -- refused loudly: {exc}", flush=True)
        return
    except Exception as exc:  # noqa: BLE001 -- any other raise is wrong
        raise SystemExit(
            f"PLOOP-2 PROOF FAILED at: {name}: wrong exception "
            f"{type(exc).__name__}: {exc} {detail}")
    raise SystemExit(
        f"PLOOP-2 PROOF FAILED at: {name}: no refusal raised {detail}")


def main():
    td = tempfile.mkdtemp(prefix="ploop_2_")
    db = os.path.join(td, "engine.db")

    from swarm_engine.core.engine import SwarmEngine
    from swarm_engine.core.run_controller import RunController, RunConfig
    from swarm_engine.core.executive import ExecutiveController
    from swarm_engine.core.executive.boundary import BoundaryPresentation
    from swarm_engine.core.executive.handoff import (
        HANDOFF_ROUTES, MAX_HANDOFF_DEPTH, TERMINAL_STATES,
        HandoffRefused, LoopHandoff, accept_handoff, classify_terminal,
        produce_handoff, transition)
    from swarm_engine.core.executive.loops import (
        LOOP_ACCEPTANCE, LOOP_ACQUISITION, LOOP_DISTILLATION, LOOP_EXECUTION,
        LOOP_GENERALIZATION, LOOP_RUN, LoopOutcome)
    from swarm_engine.acquisition.gaps import GapRegistry, GapRecord
    from swarm_engine.acquisition.delta import DeltaRecord
    from swarm_engine.services.acceptance import (
        AcceptanceLoop, AcceptanceStore)

    eng = SwarmEngine(db_path=db)
    epi = eng.intellect.epistemic
    rc = RunController(
        eng,
        config=RunConfig(cadence_interval_s=60, cycle_budget_s=120,
                         max_gaps_per_cycle=5),
        checkpoint_path=os.path.join(td, "rc.db"))
    registry = GapRegistry(eng, db_path=os.path.join(td, "gaps.db"))
    rc._registry = registry  # proof wiring: the tick sees this registry
    acc_loop = AcceptanceLoop(
        AcceptanceStore(db_path=os.path.join(td, "acc.db")), epi,
        engine=eng)
    ex = ExecutiveController(
        engine=eng, run_controller=rc, gap_registry=registry,
        acceptance_loop=acc_loop)

    # ------------------------------------------------------------- H1
    # classification reads the real result types, never invented.
    tick_like = {"gaps": [], "sweep": None, "quarantine": None,
                 "q7_attempts": [], "errors": [], "budget_exceeded": False}
    check("H1: tick summary (clean) -> converged",
          classify_terminal(
              LOOP_RUN, LoopOutcome(loop=LOOP_RUN, entered=True,
                                    result=tick_like)) == "converged")
    check("H1: tick summary (budget exceeded) -> exhausted",
          classify_terminal(
              LOOP_RUN, LoopOutcome(loop=LOOP_RUN, entered=True,
                                    result={**tick_like,
                                            "budget_exceeded": True}))
          == "exhausted")
    check("H1: tick summary (named errors) -> open",
          classify_terminal(
              LOOP_RUN, LoopOutcome(loop=LOOP_RUN, entered=True,
                                    result={**tick_like,
                                            "errors": ["x"]})) == "open")
    from swarm_engine.acquisition.gaps import DispatchResult
    check("H1: DispatchResult open -> open",
          classify_terminal(
              LOOP_ACQUISITION,
              LoopOutcome(loop=LOOP_ACQUISITION, entered=True,
                          result=DispatchResult(gap_id="g", routed=True,
                                                outcome="open")))
          == "open")
    check("H1: DispatchResult closed -> converged",
          classify_terminal(
              LOOP_ACQUISITION,
              LoopOutcome(loop=LOOP_ACQUISITION, entered=True,
                          result=DispatchResult(gap_id="g", routed=True,
                                                outcome="closed")))
          == "converged")
    from swarm_engine.acquisition.distill import DistillationResult
    check("H1: DistillationResult success -> converged",
          classify_terminal(
              LOOP_DISTILLATION,
              LoopOutcome(loop=LOOP_DISTILLATION, entered=True,
                          result=DistillationResult(
                              delta_id="d", success=True))) == "converged")
    check("H1: DistillationResult named failure -> open",
          classify_terminal(
              LOOP_DISTILLATION,
              LoopOutcome(loop=LOOP_DISTILLATION, entered=True,
                          result=DistillationResult(
                              delta_id="d", success=False,
                              reason="nope"))) == "open")
    check("H1: not entered -> absent (any loop)",
          classify_terminal(
              LOOP_EXECUTION,
              LoopOutcome(loop=LOOP_EXECUTION, entered=False))
          == "absent")
    from swarm_engine.synthesis.integrity import QuarantineDiagnosis
    check("H1: QuarantineDiagnosis reason_cleared -> converged",
          classify_terminal(
              LOOP_EXECUTION,
              LoopOutcome(loop=LOOP_EXECUTION, entered=True,
                          result=QuarantineDiagnosis(
                              capability_id="c", quarantined=False,
                              reason=None, since=None, system="s",
                              reason_still_holds=False,
                              verdict="reason_cleared")))
          == "converged")
    check("H1: QuarantineDiagnosis still quarantined -> open",
          classify_terminal(
              LOOP_EXECUTION,
              LoopOutcome(loop=LOOP_EXECUTION, entered=True,
                          result=QuarantineDiagnosis(
                              capability_id="c", quarantined=True,
                              reason="missing substrate",
                              since=None, system="s",
                              reason_still_holds=True,
                              verdict="reason_holds")))
          == "open")
    check("H1: acceptance CANDIDATE record -> candidate (never terminal)",
          classify_terminal(
              LOOP_ACCEPTANCE,
              LoopOutcome(loop=LOOP_ACCEPTANCE, entered=True,
                          result=type("R", (),
                                      {"state": "CANDIDATE"})()))
          == "candidate")
    check("H1: generalization DistillationResult success -> converged",
          classify_terminal(
              LOOP_GENERALIZATION,
              LoopOutcome(loop=LOOP_GENERALIZATION, entered=True,
                          result=DistillationResult(
                              delta_id="d", success=True)))
          == "converged")
    check("H1: generalization named refusal -> open",
          classify_terminal(
              LOOP_GENERALIZATION,
              LoopOutcome(loop=LOOP_GENERALIZATION, entered=True,
                          result=DistillationResult(
                              delta_id="d", success=False,
                              reason="out_of_envelope")))
          == "open")
    expect_refused(
        "H1: unknown loop is refused, never guessed",
        lambda: classify_terminal(
            "nope",
            LoopOutcome(loop="nope", entered=True, result={})))

    # ------------------------------------------------------------- H2
    # every (loop, terminal_state) pair has a declared answer.
    loops = (LOOP_RUN, LOOP_ACQUISITION, LOOP_EXECUTION, LOOP_ACCEPTANCE,
             LOOP_DISTILLATION, LOOP_GENERALIZATION)
    missing = [(lp, st) for lp in loops for st in TERMINAL_STATES
               if (lp, st) not in HANDOFF_ROUTES]
    check("H2: HANDOFF_ROUTES declares every (loop, terminal) pair",
          not missing, f"missing={missing}")
    check("H2: six loops covered",
          {lp for lp, _ in HANDOFF_ROUTES.keys()} == set(loops))

    # ------------------------------------------------------------- T1
    # run -> acquisition through the contract, all real.
    gap = registry.register_dependency_gap(
        "nonexistent_pkg_xyz", "package", "ploop-2 proof boundary",
        evidence=[{"kind": "observation", "observed": True,
                   "detail": "real probe: importlib.util.find_spec("
                             "'nonexistent_pkg_xyz') is None"}],
        registered_by="ploop-2-proof")
    b_run = BoundaryPresentation(
        kind="run_wake",
        evidence={"wake_reason": "cadence_tick", "run_id": rc._run_id},
        observed_by="run_controller")
    view_before = ex.loop_view(LOOP_RUN)
    out_run = ex.enter(b_run)
    view_after = ex.loop_view(LOOP_RUN)
    check("T1: run loop entered (real tick)", out_run.entered is True,
          out_run.detail[:100])
    check("T1: tick surfaced the open gap",
          any(isinstance(g, dict) and g.get("gap_id") == gap.gap_id
              for g in (out_run.result.get("gaps") or [])),
          str(out_run.result.get("gaps"))[:120])

    handoff = produce_handoff(
        outcome=out_run, triggering_boundary=b_run,
        context={"gap_fetcher": registry.get},
        view_before=view_before, view_after=view_after)
    check("T1: handoff produced (not terminal)", handoff is not None)
    check("T1: handoff names run -> acquisition",
          handoff.from_loop == LOOP_RUN
          and handoff.to_loop == LOOP_ACQUISITION,
          f"{handoff.from_loop}->{handoff.to_loop}")
    check("T1: handoff carries the real GapRecord",
          isinstance(handoff.evidence.get("gap_record"), GapRecord)
          and handoff.evidence["gap_record"].gap_id == gap.gap_id)
    check("T1: evidence_refs names the real record",
          handoff.evidence_refs.get("gap_record") == gap.gap_id)
    check("T1: resource accounting measured (contract-required keys)",
          all(k in handoff.resource_delta
              for k in ("spawned", "retired", "refused")),
          str(handoff.resource_delta))
    check("T1: chain depth 1, lineage recorded",
          handoff.chain_depth == 1
          and handoff.triggering_boundary_id == b_run.boundary_id)
    check("T1: handoff validates clean",
          handoff.validate() is handoff)

    b_acq = accept_handoff(ex, handoff, {})
    check("T1: accepted boundary is acquisition_gap, validated",
          b_acq.kind == "acquisition_gap")
    check("T1: accepted boundary carries handoff provenance",
          b_acq.observed_by == f"handoff:{handoff.handoff_id}")
    out_acq = ex.enter(b_acq)
    check("T1: acquisition really entered through the executive",
          out_acq.entered is True and out_acq.loop == LOOP_ACQUISITION,
          out_acq.detail[:120])
    check("T1: gap still OPEN (nothing faked closed)",
          registry.get(gap.gap_id).status == "open")

    # the full contractual path in one call: a FRESH gap so the second
    # tick honestly surfaces a new boundary (the first gap is cooling
    # down in backoff -- the contract does not resurface it).
    gap2 = registry.register_dependency_gap(
        "nonexistent_pkg_abc", "package", "ploop-2 proof second boundary",
        evidence=[{"kind": "observation", "observed": True,
                   "detail": "real probe: importlib.util.find_spec("
                             "'nonexistent_pkg_abc') is None"}],
        registered_by="ploop-2-proof")
    out_run2 = ex.enter(BoundaryPresentation(
        kind="run_wake",
        evidence={"wake_reason": "cadence_tick", "run_id": rc._run_id},
        observed_by="run_controller"))
    check("T1: second tick surfaced the fresh gap",
          any(isinstance(g, dict) and g.get("gap_id") == gap2.gap_id
              for g in (out_run2.result.get("gaps") or [])))
    received = transition(
        executive=ex, outcome=out_run2, triggering_boundary=b_run,
        context={"gap_fetcher": registry.get},
        view_before=ex.loop_view(LOOP_RUN),
        view_after=ex.loop_view(LOOP_RUN))
    check("T1: transition() runs produce->accept->enter end to end",
          received is not None and received.entered is True
          and received.loop == LOOP_ACQUISITION)

    # ------------------------------------------------------------- T2
    # distillation -> generalization through the contract, all real.
    # Heavy (real distill + real generalize): skipped when SKIP_T2=1
    # so the light sections can run while a sibling heavy battery is
    # in flight; the full battery runs clean afterwards. With SKIP_T2,
    # V4 uses a real-typed (constructed) DistillationResult -- it tests
    # the contract's refusal path, not the loop.
    skip_t2 = os.environ.get("SKIP_T2")
    uid = uuid.uuid4().hex[:8]
    if skip_t2:
        check("T2: skipped by SKIP_T2 (heavy segment; full battery "
              "runs clean after the sibling)", True)
        b_dis = BoundaryPresentation(
            kind="technique_delta", evidence={"delta": None},
            observed_by="distillation_session")
        out_dis = LoopOutcome(
            loop=LOOP_DISTILLATION, entered=True,
            result=DistillationResult(
                delta_id=f"ploop2-delta-synth-{uid}", success=True,
                promoted_name="synthetic_running_totals"))
        vb3 = va3 = ex.loop_view(LOOP_DISTILLATION)
    else:
        ev = [{"input": {"xs": xs}, "output": out_} for xs, out_ in [
            ([1, 2, 3], [1, 3, 6]), ([4], [4]), ([], []),
            ([5, 5], [5, 10]), ([1, 1, 1, 1], [1, 2, 3, 4]),
            ([10, -3], [10, 7])]]
        delta = DeltaRecord(
            objective="compute running totals of a number list",
            external_actions=("external agent demonstrated running totals "
                              "on 6 worked inputs"),
            prior_capability="inventory: no list-accumulation primitive",
            capability_gap=("REMOR cannot compute running totals; the "
                            "external agent demonstrably can"),
            technique=("iterative accumulation: keep a running total, "
                       "append after each element"),
            evidence=ev, dependencies=[],
            verification={"worked_examples": 6},
            delta_id=f"ploop2-delta-{uid}", source="ploop-2-proof")
        b_dis = BoundaryPresentation(
            kind="technique_delta", evidence={"delta": delta},
            observed_by="distillation_session")
        vb3 = ex.loop_view(LOOP_DISTILLATION)
        out_dis = ex.enter(b_dis)
        va3 = ex.loop_view(LOOP_DISTILLATION)
        check("T2: distillation really entered", out_dis.entered is True)
        res = out_dis.result
        check("T2: real DistillationResult, real success",
              isinstance(res, DistillationResult) and res.success is True
              and res.heldout_passed == res.heldout_examples > 0,
              f"success={res.success} heldout="
              f"{res.heldout_passed}/{res.heldout_examples}")

        novel_examples = [({"xs": xs}, out_) for xs, out_ in [
            ([1, 2, 3], [1, 2, 6]), ([4], [4]), ([], []),
            ([2, 5], [2, 10]), ([3, 3], [3, 9])]]
        handoff2 = produce_handoff(
            outcome=out_dis, triggering_boundary=b_dis,
            context={"novel_goal": "compute running products of a number "
                                  "list",
                     "novel_examples": novel_examples},
            view_before=vb3, view_after=va3)
        check("T2: handoff produced distillation -> generalization",
              handoff2 is not None
              and handoff2.from_loop == LOOP_DISTILLATION
              and handoff2.to_loop == LOOP_GENERALIZATION,
              f"{handoff2.from_loop}->{handoff2.to_loop}")
        check("T2: handoff terminal state converged",
              handoff2.terminal_state == "converged")
        b_novel = accept_handoff(ex, handoff2, {})
        check("T2: accepted boundary is novel_task, validated",
              b_novel.kind == "novel_task")
        out_gen = ex.enter(b_novel)
        check("T2: generalization really entered through the executive",
              out_gen.entered is True and out_gen.loop == LOOP_GENERALIZATION,
              out_gen.detail[:140])
        gres = out_gen.result
        check("T2: receiving loop produced its real outcome type",
              isinstance(gres, DistillationResult),
              f"success={gres.success} reason={gres.reason[:100]}")
        # success or named refusal are both the loop's real behavior; a
        # silent/empty result would be the failure mode.
        check("T2: outcome is decisive (success or NAMED refusal)",
              gres.success is True or bool(gres.reason),
              f"success={gres.success}")

    # ------------------------------------------------------------- T3
    # contractual terminals: declared answers, never silent drops.
    out_open = LoopOutcome(
        loop=LOOP_ACQUISITION, entered=True,
        result=DispatchResult(gap_id="g", routed=True, outcome="open",
                              detail="missing piece named"))
    check("T3: acquisition OPEN -> produce returns None (declared terminal)",
          produce_handoff(
              outcome=out_open, triggering_boundary=b_acq, context={},
              view_before=ex.loop_view(LOOP_ACQUISITION),
              view_after=ex.loop_view(LOOP_ACQUISITION)) is None)
    out_cand = LoopOutcome(loop=LOOP_ACCEPTANCE, entered=True,
                           result=type("R", (), {"state": "CANDIDATE"})())
    check("T3: acceptance CANDIDATE -> produce returns None (verdict is "
          "James's; never auto-advanced)",
          produce_handoff(
              outcome=out_cand, triggering_boundary=b_acq, context={},
              view_before=ex.loop_view(LOOP_ACCEPTANCE),
              view_after=ex.loop_view(LOOP_ACCEPTANCE)) is None)

    # ------------------------------------------------------------- V1
    # tampered handoff: to_loop does not own the boundary kind.
    # Build a REAL handoff (run -> acquisition via T1 outcome), then
    # tamper its to_loop -- acceptance must refuse loudly.
    _real = produce_handoff(
        outcome=out_run, triggering_boundary=b_run,
        context={"gap_fetcher": registry.get},
        view_before=view_before, view_after=view_after)
    check("V1: baseline handoff for tamper test", _real is not None)
    tampered = LoopHandoff(
        handoff_id=_real.handoff_id, from_loop=_real.from_loop,
        to_loop=LOOP_EXECUTION,  # acquisition_gap is owned by
                                 # acquisition, not execution
        terminal_state=_real.terminal_state,
        boundary_kind=_real.boundary_kind,  # not tampered: the evidence
                                            # was honestly built for this
                                            # boundary kind
        triggering_boundary_id=_real.triggering_boundary_id,
        outcome_detail=_real.outcome_detail,
        evidence=dict(_real.evidence),
        evidence_refs=dict(_real.evidence_refs),
        resource_delta=dict(_real.resource_delta),
        chain_depth=_real.chain_depth)
    expect_refused(
        "V1: to_loop/ownership mismatch refused loudly",
        lambda: accept_handoff(ex, tampered, {}))

    # ------------------------------------------------------------- V2
    # empty evidence / unmeasured resources fail validation loudly.
    expect_refused(
        "V2: handoff with empty evidence refused loudly",
        lambda: LoopHandoff(
            handoff_id="h_x", from_loop=LOOP_RUN, to_loop=LOOP_ACQUISITION,
            terminal_state="converged", boundary_kind="acquisition_gap",
            triggering_boundary_id="b_x",
            evidence={}, evidence_refs={"gap_record": "g"},
            resource_delta={"spawned": 0, "retired": 0, "refused": 0},
            chain_depth=1).validate())
    expect_refused(
        "V2: handoff with unmeasured resources refused loudly",
        lambda: LoopHandoff(
            handoff_id="h_x", from_loop=LOOP_RUN, to_loop=LOOP_ACQUISITION,
            terminal_state="converged", boundary_kind="acquisition_gap",
            triggering_boundary_id="b_x",
            evidence={"gap_record": gap},
            evidence_refs={"gap_record": gap.gap_id},
            resource_delta={},
            chain_depth=1).validate())
    expect_refused(
        "V2: self-handoff refused loudly",
        lambda: LoopHandoff(
            handoff_id="h_x", from_loop=LOOP_RUN, to_loop=LOOP_RUN,
            terminal_state="converged", boundary_kind="acquisition_gap",
            triggering_boundary_id="b_x",
            evidence={"x": 1}, evidence_refs={},
            resource_delta={"spawned": 0, "retired": 0, "refused": 0},
            chain_depth=1).validate())

    # ------------------------------------------------------------- V3
    expect_refused(
        "V3: chain depth beyond MAX refused loudly at produce",
        lambda: produce_handoff(
            outcome=out_run, triggering_boundary=b_run,
            context={"gap_fetcher": registry.get,
                     "chain_depth": MAX_HANDOFF_DEPTH},
            view_before=view_before, view_after=view_after))
    deep = LoopHandoff(
        handoff_id="h_deep", from_loop=LOOP_RUN, to_loop=LOOP_ACQUISITION,
        terminal_state="converged", boundary_kind="acquisition_gap",
        triggering_boundary_id="b_x",
        evidence={"gap_record": gap},
        evidence_refs={"gap_record": gap.gap_id},
        resource_delta={"spawned": 0, "retired": 0, "refused": 0},
        chain_depth=MAX_HANDOFF_DEPTH + 1)
    expect_refused(
        "V3: over-deep handoff refused loudly at validate",
        lambda: deep.validate())

    # ------------------------------------------------------------- V4
    # distillation converged but no novel spec: novelty is never
    # auto-generated, never silently skipped.
    expect_refused(
        "V4: missing novel spec refused loudly (not silently skipped)",
        lambda: produce_handoff(
            outcome=out_dis, triggering_boundary=b_dis, context={},
            view_before=vb3, view_after=va3))

    print(f"\nPLOOP-2 PROOF COMPLETE: {PASS_N}/{PASS_N} checks green",
          flush=True)


if __name__ == "__main__":
    main()
