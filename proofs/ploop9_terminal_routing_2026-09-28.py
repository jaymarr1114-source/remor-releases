"""PLOOP-9 durable proof: terminal-state routing (LoopOutcome consumers).

PLOOP-2's handoff contract declares an answer for every (loop,
terminal_state) pair in HANDOFF_ROUTES. Pairs with follow-on routes are
driven by produce_handoff()/transition(). Pairs with an EMPTY follow-on
list are DECLARED TERMINALS: produce_handoff() returns None for them --
and runtime/core/executive/terminal_routing.py routes each one to a
REAL consumer instead of letting it die silently.

  S1  coverage: the 19 REACHABLE (loop, terminal) pairs are proven
      reachable by driving the REAL classify_terminal() with
      real-shaped inputs (not asserted). Every reachable pair is either
      produce_handoff's domain (follow-on routes) or has a terminal
      consumer route. The 23 unreachable pairs (classify_terminal
      raises rather than guessing, e.g. ('run','candidate')) are
      documented as unreachable-by-construction and unrouted.
  R1  acceptance CANDIDATE: a REAL AcceptanceLoop.present() record
      (state CANDIDATE) is verified persisted in the real
      AcceptanceStore and routed to the verdict-awaiting surface --
      never auto-advanced (record_verdict is not called; the stored
      record is still CANDIDATE afterwards).
  R2  acquisition refusal: a REAL GapRegistry.dispatch with no
      satisfiable route (routed=False) reaches the executive as a
      finding via the real submit_finding(); the relevance gate's
      persisted decision is the receipt, carrying the EXACT refusal
      reason. (The real classifier reads outcome="open" first, so the
      router -- not the classifier -- splits refused-from-attempted on
      the dispatch's own routed flag.)
  R3  run EXHAUSTED: a REAL RunController.tick() in a provably-exhausted
      budget state (zero authorized cycle budget -- exhaustion structural,
      not timed) is persisted through the same record_cycle path
      run() uses, verified against the persisted cycle row, and routed
      to the controller's own accepted cycle record.
  R4  acquisition OPEN (attempted): a REAL dispatch that a real route
      attempted and failed open is handed to the REAL rc_gap_backoff
      table (note_gap_failure with the controller's own configured
      bounds); failures and backoff_until verified in the table.
  R5  acquisition CONVERGED: a REAL dispatch through a real route with
      a real utilization check closes the gap through the real _close
      path; the router verifies the closed record via the real
      gap_fetcher (status closed, real closing evidence).
  R6  execution: (converged) a REAL diagnose_quarantine() diagnosis;
      (open) the loop's real result type with a named holding reason.
      Both verified against the real diagnosis and routed to the
      quarantine-diagnosis record.
  R7  distillation: (open) named failure and (converged, no promotion)
      on the loop's real DistillationResult type, routed to the
      distillation loop's own record.
  R8  generalization: (converged) explicit sink naming the trust path
      as the declared-but-external consumer; (open) honest refusal
      routed to the loop's own record.
  R9  absent: all six loops, entered=False -- explicit sink, observably
      dropped with a reason, one ledger row each.
  R10 the terminal router REFUSES to swallow live follow-ons: a
      (distillation, converged) outcome with a promoted technique but
      no novel spec propagates the builder's HandoffRefused (novelty
      never auto-generated); a (run, converged) tick with a real open
      gap is refused loudly naming produce_handoff/transition.
  R11 route_refusal: a HandoffRefused raised mid-transition reaches the
      executive as a finding carrying the EXACT violation.
  R12 missing consumers fail loud: no run controller / no acceptance
      loop / no executive -> HandoffRefused, never a silent drop.
  R13 evidence intact: every ledger row carries non-empty evidence_refs
      and a consumer; the row count matches the routed outcomes.

Real machinery throughout: SwarmEngine, RunController,
ControllerCheckpoint, GapRegistry, AcceptanceLoop + AcceptanceStore,
ExecutiveController + RelevanceGate. Fresh process, temp dirs.
Proof-wiring notes are inline where the proof reaches past a private
seam (same convention as the PLOOP-2 proof).

Imports resolve against the tree under test: PLOOP9_WT env var, else
~/workspace/ploop-9-work (this mission's worktree).
"""

import importlib.util
import os
import sys
import tempfile
import time
from types import SimpleNamespace

WT = os.environ.get(
    "PLOOP9_WT",
    os.path.expanduser("~/workspace/ploop-9-work"))
sys.path.insert(0, os.path.join(WT, "pylib"))

PASS_N = 0


def check(name, cond, detail=""):
    global PASS_N
    PASS_N += 1
    print(f"PASS {name}" + (f" -- {detail}" if detail else ""), flush=True)
    if not cond:
        raise SystemExit(f"PLOOP-9 PROOF FAILED at: {name} {detail}")


def expect_refused(name, fn, detail=""):
    """The router must refuse LOUDLY: HandoffRefused, never silent."""
    global PASS_N
    from swarm_engine.core.executive.handoff import HandoffRefused
    PASS_N += 1
    try:
        fn()
    except HandoffRefused as exc:
        print(f"PASS {name} -- refused loudly: {exc}", flush=True)
        return str(exc)
    except Exception as exc:  # noqa: BLE001 -- any other raise is wrong
        raise SystemExit(
            f"PLOOP-9 PROOF FAILED at: {name}: wrong exception "
            f"{type(exc).__name__}: {exc} {detail}")
    raise SystemExit(
        f"PLOOP-9 PROOF FAILED at: {name}: no refusal raised {detail}")


def main():
    td = tempfile.mkdtemp(prefix="ploop_9_")

    from swarm_engine.core.engine import SwarmEngine
    from swarm_engine.core.run_controller import RunController, RunConfig
    from swarm_engine.core.executive import ExecutiveController
    from swarm_engine.core.executive.handoff import (
        HANDOFF_ROUTES, HandoffRefused, classify_terminal)
    from swarm_engine.core.executive.loops import (
        LOOP_ACCEPTANCE, LOOP_ACQUISITION, LOOP_DISTILLATION, LOOP_EXECUTION,
        LOOP_GENERALIZATION, LOOP_RUN, LoopOutcome)
    from swarm_engine.core.microcontroller import LOOPS
    from swarm_engine.core.executive.relevance import (
        OperationalObjective, RelevanceGate)
    from swarm_engine.core.executive.terminal_routing import (
        TERMINAL_ROUTING_VERSION, TerminalRouter, _TERMINAL_HANDLERS)
    from swarm_engine.acquisition.gaps import (
        DataBlock, DispatchResult, GapRecord, GapRegistry, Route)
    from swarm_engine.acquisition.distill import DistillationResult
    from swarm_engine.services.acceptance import (
        AcceptanceLoop, AcceptanceStore, Attempt, AuthReport)
    from swarm_engine.synthesis.integrity import (
        QuarantineDiagnosis, diagnose_quarantine)

    OBJECTIVE = OperationalObjective(
        objective_id="obj-terminal-routing",
        statement=("Keep the Primary executive loop turning: observe "
                   "runtime loop state, route refused dispatches and "
                   "contract violations to the executive loop owners, "
                   "and never silently drop a loop outcome."),
    )
    eng = SwarmEngine(db_path=os.path.join(td, "engine.db"))
    epi = eng.intellect.epistemic
    rc = RunController(
        eng,
        # Structural budget-exhaustion (R3): the tick is authorized ZERO
        # cycle budget, so budget_exceeded is provable by construction --
        # (monotonic() - started) >= 0.0 at the first cooperative check,
        # always. A wall-clock threshold (was: 0.01) is flaky here by
        # mechanism, not luck: the budget check is cooperative (between
        # steps only), so a tick can overrun *inside* its final step and
        # still report False, while warm caches can run the whole tick in
        # ~4ms and report False correctly. Zero budget makes exhaustion
        # certain; the tick itself stays real (real arbitration round,
        # real gap-queue pass, real summary through the real path).
        config=RunConfig(cadence_interval_s=60, cycle_budget_s=0.0,
                         max_gaps_per_cycle=5),
        checkpoint_path=os.path.join(td, "rc.db"))
    registry = GapRegistry(eng, db_path=os.path.join(td, "gaps.db"))
    rc._registry = registry  # proof wiring: the tick sees this registry
    acc_loop = AcceptanceLoop(
        AcceptanceStore(db_path=os.path.join(td, "acc.db")), epi,
        engine=eng)
    gate = RelevanceGate(objective=OBJECTIVE,
                         store_path=os.path.join(td, "relevance.db"))
    ex = ExecutiveController(
        engine=eng, run_controller=rc, gap_registry=registry,
        acceptance_loop=acc_loop, relevance_gate=gate)

    router = TerminalRouter(
        executive=ex, run_controller=rc, acceptance_loop=acc_loop,
        gap_fetcher=registry.get,
        diagnosis_fetcher=lambda cid: diagnose_quarantine(eng, cid),
        ledger_path=os.path.join(td, "terminal_routes.db"))

    # ------------------------------------------------------------- S1
    # coverage: reachability PROVEN by driving the real classifier.
    _tick = {"gaps": [], "sweep": None, "quarantine": None,
             "q7_attempts": [], "errors": [], "budget_exceeded": False}
    _reach_inputs = {
        (LOOP_RUN, "converged"): dict(_tick),
        (LOOP_RUN, "exhausted"): {**_tick, "budget_exceeded": True},
        (LOOP_RUN, "open"): {**_tick, "errors": ["x"]},
        (LOOP_ACQUISITION, "converged"):
            DispatchResult("g", True, "r", "closed", "d", None),
        (LOOP_ACQUISITION, "open"):
            DispatchResult("g", True, "r", "open", "d", None),
        (LOOP_ACQUISITION, "refused"):
            DispatchResult("g", False, "", "", "no route", None),
        (LOOP_EXECUTION, "converged"):
            QuarantineDiagnosis("c", False, None, None, "s", None,
                                "not_quarantined"),
        (LOOP_EXECUTION, "open"):
            QuarantineDiagnosis("c", True, "r", None, "s", True,
                                "reason_holds"),
        (LOOP_ACCEPTANCE, "candidate"):
            SimpleNamespace(state="CANDIDATE"),
        (LOOP_DISTILLATION, "converged"):
            DistillationResult(delta_id="d", success=True),
        (LOOP_DISTILLATION, "open"):
            DistillationResult(delta_id="d", success=False, reason="named"),
        (LOOP_GENERALIZATION, "converged"):
            DistillationResult(delta_id="d", success=True),
        (LOOP_GENERALIZATION, "open"):
            DistillationResult(delta_id="d", success=False, reason="named"),
    }
    for loop in (LOOP_RUN, LOOP_ACQUISITION, LOOP_EXECUTION, LOOP_ACCEPTANCE,
                 LOOP_DISTILLATION, LOOP_GENERALIZATION):
        _reach_inputs[(loop, "absent")] = None  # entered=False below
    for (loop, state), shaped in sorted(_reach_inputs.items()):
        outcome = LoopOutcome(loop=loop, entered=False, result=None,
                              detail="absent") if shaped is None else \
            LoopOutcome(loop=loop, entered=True, result=shaped)
        got = classify_terminal(loop, outcome)
        check(f"S1: ({loop}, {state}) reachable via classify_terminal",
              got == state, f"got {got!r}")
    reachable = set(_reach_inputs)
    check("S1: 19 reachable pairs", len(reachable) == 19,
          f"got {len(reachable)}")
    for pair in sorted(reachable):
        has_follow_ons = bool(HANDOFF_ROUTES.get(pair))
        has_terminal_route = pair in _TERMINAL_HANDLERS
        check(f"S1: {pair} routed-or-follow-on",
              has_follow_ons or has_terminal_route,
              f"follow_ons={has_follow_ons} "
              f"terminal_route={has_terminal_route}")
    for pair in sorted(set(HANDOFF_ROUTES) - reachable):
        check(f"S1: {pair} unreachable-by-construction (not routed)",
              pair not in _TERMINAL_HANDLERS)
    for pair in _TERMINAL_HANDLERS:
        check(f"S1: handler {pair} names a reachable pair",
              pair in reachable)
    check("S1: six loops covered", len(LOOPS) == 6, str(sorted(LOOPS)))

    # ------------------------------------------------------------- R1
    # acceptance CANDIDATE: the real present() path, never auto-advanced.
    attempt = Attempt(approach_signature=["ploop-9-proof"],
                      plan={"goal": "prove candidate routing"},
                      exec_ok=True, result_summary="proof ran")
    auth = AuthReport(held_out={"n": 1}, negative_controls={},
                      passed=True)
    rec = acc_loop.present("run_ploop9_candidate",
                           "prove candidate routing", attempt, auth)
    check("R1: real present() -> CANDIDATE record",
          getattr(getattr(rec, "state", None), "name",
                   getattr(rec, "state", None)) == "CANDIDATE",
          f"state={getattr(rec, 'state', None)!r}")
    out_cand = LoopOutcome(loop=LOOP_ACCEPTANCE, entered=True, result=rec,
                           detail="present() -> CANDIDATE")
    check("R1: candidate classifies candidate",
          classify_terminal(LOOP_ACCEPTANCE, out_cand) == "candidate")
    route = router.route_terminal(LOOP_ACCEPTANCE, out_cand)
    check("R1: candidate routed", route.consumer ==
          "acceptance_loop.verdict_awaiting", route.consumer)
    check("R1: candidate evidence intact",
          route.evidence_refs.get("run_id") == "run_ploop9_candidate",
          str(route.evidence_refs))
    stored = acc_loop.store.get("run_ploop9_candidate")
    state_name = getattr(getattr(stored, "state", None), "name",
                         getattr(stored, "state", None))
    check("R1: candidate NOT auto-advanced (still CANDIDATE in store)",
          state_name == "CANDIDATE", f"state={state_name!r}")
    rows = router.ledger.routes(loop=LOOP_ACCEPTANCE,
                                terminal_state="candidate")
    check("R1: ledger row for the candidate",
          len(rows) == 1 and rows[0]["evidence_refs"]["run_id"] ==
          "run_ploop9_candidate")

    # ------------------------------------------------------------- R2
    # acquisition refusal: a REAL refused dispatch reaches the executive
    # as a finding. The real classifier reads outcome="open" first, so
    # the refusal is split at ROUTE time on the dispatch's routed flag.
    bare = registry.register(GapRecord(
        summary="ploop-9 proof: shape no route satisfies",
        evidence=[{"kind": "observation", "observed": True,
                   "detail": "real probe: this record carries no block "
                             "any route requires"}],
        registered_by="ploop-9-proof"))
    refused_res = registry.dispatch(bare.gap_id)
    check("R2: real dispatch genuinely refused (routed=False)",
          refused_res.routed is False,
          f"routed={refused_res.routed!r} outcome={refused_res.outcome!r}")
    out_refused = LoopOutcome(loop=LOOP_ACQUISITION, entered=True,
                              result=refused_res,
                              detail="dispatch refused: no route")
    check("R2: refusal classifies open under the real contract "
          "(outcome read first)",
          classify_terminal(LOOP_ACQUISITION, out_refused) == "open")
    route = router.route_terminal(LOOP_ACQUISITION, out_refused)
    check("R2: the refusal is routed to the executive (routed=False split)",
          route.consumer == "executive.submit_finding", route.consumer)
    check("R2: the EXACT refusal reason is carried",
          "no route satisfied" in route.evidence_refs.get("refusal", ""),
          route.evidence_refs.get("refusal", "")[:80])
    check("R2: finding id is a real receipt",
          route.evidence_refs.get("finding_id", "").startswith(
              "finding_refusal_"))
    decision = gate.get_decision(route.evidence_refs["finding_id"])
    check("R2: the gate's persisted decision is re-checkable (the receipt)",
          decision is not None and decision.verdict in
          ("admitted", "retained", "rejected"),
          f"verdict={getattr(decision, 'verdict', None)!r}")
    rows = router.ledger.routes(loop=LOOP_ACQUISITION)
    check("R2: ledger row for the refusal",
          any(r["evidence_refs"].get("gap_id") == bare.gap_id
              for r in rows))

    # ------------------------------------------------------------- R3
    # run EXHAUSTED: a real tick in a provably-exhausted budget state.
    # The controller is authorized zero cycle budget (see construction
    # above): exhaustion is structural -- elapsed >= 0.0 at the first
    # cooperative check -- never a wall-clock race.
    summary = rc.tick()
    check("R3: zero-budget tick is provably exhausted",
          summary.get("budget_exceeded") is True,
          f"budget=0.0 elapsed={summary.get('elapsed_s')} "
          f"arbitration_mode={summary.get('arbitration', {}).get('mode')}")
    # what run() does after every tick (PLOOP-3): persist the cycle.
    rc._checkpoint.record_cycle(0, summary)  # proof wiring: run()'s step
    out_exh = LoopOutcome(loop=LOOP_RUN, entered=True, result=summary,
                          detail="tick budget exhausted")
    check("R3: exhausted classifies exhausted",
          classify_terminal(LOOP_RUN, out_exh) == "exhausted")
    route = router.route_terminal(LOOP_RUN, out_exh)
    check("R3: exhausted tick routed to the controller's own record",
          route.consumer == "run_controller.persisted_cycle_record",
          route.consumer)
    check("R3: evidence names the terminal condition",
          route.evidence_refs.get("budget_exceeded") == "True",
          str(route.evidence_refs))
    persisted = rc._checkpoint.last_cycle_summary()
    check("R3: the persisted cycle IS this outcome's record",
          persisted.get("budget_exceeded") is True)

    # ------------------------------------------------------------- R4
    # acquisition OPEN (attempted): real route attempted, failed open ->
    # the real backoff table.
    dep_gap = registry.register_dependency_gap(
        "nonexistent_pkg_xyz", "package",
        "ploop-9 proof: open gap for the backoff route",
        evidence=[{"kind": "observation", "observed": True,
                   "detail": "real probe: importlib.util.find_spec("
                             "'nonexistent_pkg_xyz') is None"}],
        registered_by="ploop-9-proof")
    open_res = registry.dispatch(dep_gap.gap_id)
    check("R4: real dispatch failed open (substrate unavailable)",
          open_res.outcome == "open" and "substrate_unavailable" in
          open_res.detail, open_res.detail[:80])
    check("R4: the dispatch was attempted (routed=True)",
          open_res.routed is True)
    out_open = LoopOutcome(loop=LOOP_ACQUISITION, entered=True,
                           result=open_res, detail="dispatch open")
    check("R4: open classifies open",
          classify_terminal(LOOP_ACQUISITION, out_open) == "open")
    before = rc._checkpoint.gap_failure_count(dep_gap.gap_id)
    route = router.route_terminal(LOOP_ACQUISITION, out_open)
    check("R4: attempted-but-open gap routed to backoff",
          route.consumer == "run_controller.rc_gap_backoff",
          route.consumer)
    after = rc._checkpoint.gap_failure_count(dep_gap.gap_id)
    check("R4: failures incremented in the REAL rc_gap_backoff table",
          after == before + 1, f"{before} -> {after}")
    check("R4: backoff_until is in the future",
          rc._checkpoint.backoff_until(dep_gap.gap_id) > time.time())
    check("R4: evidence names the gap",
          route.evidence_refs.get("gap_id") == dep_gap.gap_id)

    # ------------------------------------------------------------- R5
    # acquisition CONVERGED: real route, real utilization check, real
    # _close path -- the router verifies the closed record.
    def _proof_acquire(reg, record, context):
        # Proof wiring: a REAL route with a REAL (if trivial)
        # utilization check -- the capability the gap is about must
        # demonstrably work before the gap may close.
        spec = importlib.util.find_spec("json")
        import json as _json
        if spec is None or not callable(_json.dumps):
            return ("open", "proof utilization check failed", None)
        return ("closed",
                "proof route: stdlib json verified importable with a "
                "callable dumps",
                {"utilization_verified": True,
                 "check": "find_spec(json) and callable(dumps)"})

    registry._routes.append(Route(  # proof wiring: a real test route
        name="ploop9_proof", requires=frozenset({"data.description"}),
        acquire=_proof_acquire))
    data_gap = registry.register(GapRecord(
        summary="ploop-9 proof: data gap the proof route closes",
        # description only: the builtin "data" route needs
        # data.description + data.source, so this real shape is
        # genuinely unserved by the builtin table -- the proof route
        # is the most-specific match.
        data=DataBlock(description="ploop-9 test data"),
        evidence=[{"kind": "observation", "observed": True,
                   "detail": "real probe: this record carries a data "
                             "block the proof route serves"}],
        registered_by="ploop-9-proof"))
    closed_res = registry.dispatch(data_gap.gap_id)
    check("R5: real dispatch closed the gap through the real _close path",
          closed_res.outcome == "closed" and
          (closed_res.closing_evidence or {}).get("utilization_verified")
          is True, closed_res.detail[:80])
    out_closed = LoopOutcome(loop=LOOP_ACQUISITION, entered=True,
                             result=closed_res, detail="dispatch closed")
    check("R5: closed classifies converged",
          classify_terminal(LOOP_ACQUISITION, out_closed) == "converged")
    route = router.route_terminal(LOOP_ACQUISITION, out_closed)
    check("R5: converged gap routed to its own record",
          route.consumer == "gap_registry.closed_record", route.consumer)
    check("R5: closing evidence verified present",
          "utilization_verified" in route.evidence_refs.get(
              "closing_keys", ""),
          route.evidence_refs.get("closing_keys"))

    # ------------------------------------------------------------- R6
    # execution: real diagnosis (converged) + real result type (open).
    diag = diagnose_quarantine(eng, "ploop9_never_quarantined_cap")
    check("R6: real diagnosis -> not_quarantined",
          diag.verdict == "not_quarantined")
    out_exec = LoopOutcome(loop=LOOP_EXECUTION, entered=True, result=diag,
                           detail="diagnosis: not quarantined")
    check("R6: not_quarantined classifies converged",
          classify_terminal(LOOP_EXECUTION, out_exec) == "converged")
    route = router.route_terminal(LOOP_EXECUTION, out_exec)
    check("R6: converged execution routed to the diagnosis record",
          route.consumer ==
          "execution_loop.quarantine_diagnosis_record", route.consumer)
    check("R6: evidence names the verdict",
          route.evidence_refs.get("verdict") == "not_quarantined")

    diag_open = QuarantineDiagnosis(
        capability_id="ploop9_holding_cap", quarantined=True,
        reason="ploop-9 proof: staged holding reason (named, never "
               "silent)",
        since=time.time(), system="proof", reason_still_holds=True,
        verdict="reason_holds")
    out_exec_open = LoopOutcome(loop=LOOP_EXECUTION, entered=True,
                                result=diag_open,
                                detail="diagnosis: reason holds")
    check("R6: quarantined diagnosis classifies open",
          classify_terminal(LOOP_EXECUTION, out_exec_open) == "open")
    router_exec = TerminalRouter(
        executive=ex, run_controller=rc, acceptance_loop=acc_loop,
        gap_fetcher=registry.get,
        diagnosis_fetcher=lambda cid: diag_open,  # proof wiring: the
        # loop's real result type with a staged holding reason
        ledger_path=os.path.join(td, "terminal_routes_exec.db"))
    route = router_exec.route_terminal(LOOP_EXECUTION, out_exec_open)
    check("R6: open execution routed to the diagnosis record",
          route.consumer ==
          "execution_loop.quarantine_diagnosis_record")
    check("R6: the named reason is carried",
          "staged holding reason" in route.evidence_refs.get("reason",
                                                             ""))

    # ------------------------------------------------------------- R7
    # distillation: the loop's real DistillationResult type.
    d_open = DistillationResult(delta_id="d_ploop9_open", success=False,
                                reason="ploop-9 proof: named distillation "
                                       "failure")
    out_d = LoopOutcome(loop=LOOP_DISTILLATION, entered=True, result=d_open,
                        detail="distill failed, named")
    check("R7: failed distill classifies open",
          classify_terminal(LOOP_DISTILLATION, out_d) == "open")
    route = router.route_terminal(LOOP_DISTILLATION, out_d)
    check("R7: open distillation routed to the loop's own record",
          route.consumer == "distillation_loop.own_record",
          route.consumer)
    check("R7: the named failure reason is carried",
          "named distillation failure" in route.evidence_refs.get(
              "reason", ""))
    d_conv = DistillationResult(delta_id="d_ploop9_conv", success=True,
                                promoted_name="", capability_id="")
    out_d2 = LoopOutcome(loop=LOOP_DISTILLATION, entered=True,
                         result=d_conv, detail="distill ok, no promotion")
    route = router.route_terminal(LOOP_DISTILLATION, out_d2)
    check("R7: converged-without-promotion routed to the own record",
          route.consumer == "distillation_loop.own_record" and
          route.terminal_state == "converged")

    # ------------------------------------------------------------- R8
    # generalization: trust-path sink + honest-refusal record.
    g_conv = DistillationResult(
        delta_id="g_ploop9", success=True, promoted_name="t_x",
        capability_id="cap_g", heldout_examples=2, heldout_passed=2)
    out_g = LoopOutcome(loop=LOOP_GENERALIZATION, entered=True,
                        result=g_conv, detail="generalized")
    check("R8: generalized classifies converged",
          classify_terminal(LOOP_GENERALIZATION, out_g) == "converged")
    route = router.route_terminal(LOOP_GENERALIZATION, out_g)
    check("R8: converged generalization names the external trust path",
          "trust_path.external" in route.consumer, route.consumer)
    check("R8: the technique refs are carried",
          route.evidence_refs.get("capability_id") == "cap_g")
    g_open = DistillationResult(
        delta_id="g_ploop9b", success=False, capability_id="cap_g",
        reason="ploop-9 proof: new goal names no single target constant")
    out_g2 = LoopOutcome(loop=LOOP_GENERALIZATION, entered=True,
                         result=g_open, detail="generalize refused")
    route = router.route_terminal(LOOP_GENERALIZATION, out_g2)
    check("R8: open generalization routed to the loop's own record",
          route.consumer == "generalization_loop.own_record")
    check("R8: the honest refusal reason is carried",
          "no single target constant" in route.evidence_refs.get(
              "reason", ""))

    # ------------------------------------------------------------- R9
    # absent: every loop, observably dropped with a reason.
    for loop in (LOOP_RUN, LOOP_ACQUISITION, LOOP_EXECUTION, LOOP_ACCEPTANCE,
                 LOOP_DISTILLATION, LOOP_GENERALIZATION):
        out_abs = LoopOutcome(loop=loop, entered=False, result=None,
                              detail=f"{loop} not entered")
        check(f"R9: not-entered {loop} classifies absent",
              classify_terminal(loop, out_abs) == "absent")
        route = router.route_terminal(loop, out_abs)
        check(f"R9: absent {loop} -> explicit sink",
              route.consumer == "terminal_ledger.explicit_sink",
              route.consumer)
    check("R9: six sink rows in the ledger",
          len(router.ledger.routes(terminal_state="absent")) == 6)

    # ------------------------------------------------------------- R10
    # the router REFUSES to swallow live follow-ons.
    d_promoted = DistillationResult(
        delta_id="d_ploop9_prom", success=True, promoted_name="technique_x",
        capability_id="cap_y")
    out_dp = LoopOutcome(loop=LOOP_DISTILLATION, entered=True,
                         result=d_promoted, detail="promoted, no spec")
    msg = expect_refused(
        "R10: promoted technique without novel spec propagates loudly",
        lambda: router.route_terminal(LOOP_DISTILLATION, out_dp))
    check("R10: the refusal names the missing novel spec",
          "novel_goal" in msg, msg[:100])
    tick_with_gap = {"cycle": 9, "at": time.time(), "gaps":
                     [{"gap_id": dep_gap.gap_id, "outcome": "open"}],
                     "sweep": None, "quarantine": None, "q7_attempts": [],
                     "errors": [], "budget_exceeded": False}
    out_rg = LoopOutcome(loop=LOOP_RUN, entered=True, result=tick_with_gap,
                         detail="converged tick, open gap surfaced")
    msg = expect_refused(
        "R10: converged tick with a live gap follow-on is refused",
        lambda: router.route_terminal(LOOP_RUN, out_rg))
    check("R10: the refusal names produce_handoff/transition",
          "produce_handoff" in msg, msg[:120])

    # ------------------------------------------------------------- R11
    # route_refusal: the exact violation, observed as a finding.
    exc = HandoffRefused(
        "ploop-9 proof: synthetic contract violation for the refusal path")
    route = router.route_refusal(exc, loop="distillation")
    check("R11: refusal routed to the executive",
          route.consumer == "executive.submit_finding", route.consumer)
    check("R11: the EXACT violation is carried",
          "synthetic contract violation for the refusal path" in
          route.evidence_refs.get("violation", ""))
    check("R11: finding id is a real receipt",
          route.evidence_refs.get("finding_id", "").startswith(
              "finding_refusal_"))

    # ------------------------------------------------------------- R12
    # missing consumers fail loud -- never a silent drop.
    router_norc = TerminalRouter(
        executive=ex, run_controller=None, acceptance_loop=acc_loop,
        gap_fetcher=registry.get,
        ledger_path=os.path.join(td, "t2.db"))
    expect_refused("R12: no run controller -> backoff refused loudly",
                   lambda: router_norc.route_terminal(LOOP_ACQUISITION,
                                                      out_open))
    router_noacc = TerminalRouter(
        executive=ex, run_controller=rc, acceptance_loop=None,
        gap_fetcher=registry.get,
        ledger_path=os.path.join(td, "t3.db"))
    expect_refused("R12: no acceptance loop -> candidate refused loudly",
                   lambda: router_noacc.route_terminal(LOOP_ACCEPTANCE,
                                                       out_cand))
    router_noex = TerminalRouter(
        executive=None, run_controller=rc, acceptance_loop=acc_loop,
        gap_fetcher=registry.get,
        ledger_path=os.path.join(td, "t4.db"))
    expect_refused("R12: no executive -> refusal refused loudly",
                   lambda: router_noex.route_terminal(LOOP_ACQUISITION,
                                                      out_refused))

    # ------------------------------------------------------------- R13
    # evidence intact across every routed outcome.
    rows = router.ledger.routes()
    check("R13: every ledger row carries evidence refs and a consumer",
          all(r["evidence_refs"] and r["consumer"] for r in rows),
          f"{len(rows)} rows")
    # R1..R9 (1+1+1+1+1+1+2+2+6) + R11 (1) = 17; R10/R12 raise, no rows.
    check("R13: ledger row count matches routed outcomes", len(rows) == 17,
          f"got {len(rows)}")

    print(f"PLOOP-9 PROOF COMPLETE: {PASS_N} checks green "
          f"({TERMINAL_ROUTING_VERSION})", flush=True)


if __name__ == "__main__":
    main()
