"""PLOOP-12 proof: acceptance through the live-path seam.

Drives a REAL completion candidate -- an Attempt genuinely synthesized and
executed, with an AuthReport genuinely authenticated (held-out, claim
re-execution, negative control, counterfactual) -- through Q8's present()
gate VIA the live-path seam (executive.enter -> acceptance inlet ->
surface() -> TerminalRouter -> verdict-awaiting surface).

  A  real work + real auth: induction genuinely ran, claim genuinely
     executed, held-out genuinely verified, negative control failed
     closed, counterfactual diverged, and the gate is not vacuous (a
     wrong expectation really fails it).
  B  through the seam: completion_candidate boundary validated; entered
     through the executive into the real acceptance inlet; present()
     really ran -> CANDIDATE; surface() classified candidate and took
     the terminal path; the seam's own router routed to the real
     verdict-awaiting consumer; the terminal ledger holds the route;
     the stored record is still CANDIDATE (never auto-advanced).
  D  adversarial: failing auth refused at the boundary gate; failing
     auth refused at present(); tampered store state refused by the
     router; never-persisted candidate refused; double-present while
     CANDIDATE refused (PLOOP-12 repair); re-present after ACCEPTED
     refused, verdict intact (PLOOP-12 repair).

Exit 0 with COUNT PASS / 0 FAIL, else nonzero. Light: safe to run while
other work proceeds, but never overlapping another battery's heavy run.
"""
import json
import os
import sqlite3
import sys
import tempfile
import uuid

WT = os.environ.get("PLOOP12_WT",
                    os.path.expanduser("~/workspace/remor_convergence/worktrees/ploop12"))
sys.path.insert(0, os.path.join(WT, "pylib"))
sys.path.insert(0, os.path.join(WT, "proofs"))

CHECKS = []


def check(name, cond, detail=""):
    CHECKS.append((name, bool(cond), str(detail)[:220]))
    print(f"[{'PASS' if cond else 'FAIL'}] {name}"
          + (f" -- {str(detail)[:160]}" if detail and not cond else ""),
          flush=True)


def main():
    from swarm_engine.core.executive.live_path import (
        build_live_path, LivePathConfig)
    from swarm_engine.core.executive.boundary import (
        BoundaryPresentation, BoundaryRefused)
    from swarm_engine.core.executive.loops import LOOP_ACCEPTANCE
    from swarm_engine.core.executive.handoff import HandoffRefused
    from swarm_engine.services.acceptance import (
        Attempt, AuthReport, AcceptanceState)
    from ploop12_work import do_real_work, HELD_OUT

    workdir = tempfile.mkdtemp(prefix="ploop12_seam_")
    lp = build_live_path(LivePathConfig(workdir=workdir))
    check("setup: live path built", lp.status()["live_path"] == "live-path/v1")

    # ---- A: real work + real auth -------------------------------------
    attempt, auth, info = do_real_work(lp.engine)
    check("A1: technique genuinely induced "
          f"({info['candidates_tried']} candidates tried)",
          info["induce_status"] == "ok" and info["candidates_tried"] > 0,
          str(info["expr"]))
    check("A2: claim genuinely executed",
          info["claim"]["success"] is True
          and info["claim"]["value"] == attempt.result_summary,
          str(info["claim"]))
    check("A3: held-out genuinely verified (3/3, unseen by induction)",
          all(info["held_out"].values()) and len(info["held_out"]) == 3
          and info["claim_check"] is True,
          str(info["held_out"]))
    check("A4: negative control failed closed",
          info["neg_passed"] is True, "missing-arg execution must not succeed")
    check("A5: counterfactual diverged (wrong plan caught)",
          bool((info["cf"] or {}).get("diverges")) is True,
          str(info["cf"]))
    # A6: the gate is not vacuous -- a wrong expectation really fails it
    from swarm_engine.services.acceptance_driver import AcceptanceDriver
    from ploop12_work import _LoopShim
    bad_held = [dict(h, expected=h["expected"] + 999.0) for h in HELD_OUT]
    bad_auth = AcceptanceDriver(_LoopShim(lp.engine))._auth_gate(
        attempt, bad_held)
    check("A6: auth gate not vacuous (wrong held-out -> passed=False)",
          bad_auth.passed is False, f"passed={bad_auth.passed}")

    # ---- B: through the seam ------------------------------------------
    run_id = f"run_ploop12_{uuid.uuid4().hex[:8]}"
    goal = "induce two-input addition from worked examples"
    try:
        boundary = BoundaryPresentation(
            kind="completion_candidate",
            evidence={"run_id": run_id, "goal": goal,
                      "attempt": attempt, "auth": auth},
            observed_by="ploop12-proof").validate()
        b_ok = True
    except BoundaryRefused as exc:
        b_ok, boundary = False, None
        check("B1: completion_candidate boundary validated", False, str(exc))
    if b_ok:
        check("B1: completion_candidate boundary validated",
              boundary.kind == "completion_candidate", boundary.boundary_id)

        outcome = lp.executive.enter(boundary)
        check("B2: entered through the executive into the acceptance inlet",
              outcome.entered is True and outcome.loop == LOOP_ACCEPTANCE,
              outcome.detail[:120])
        rec = outcome.result
        st = getattr(rec.state, "name", rec.state)
        check("B3: real present() ran -> CANDIDATE record",
              st == "CANDIDATE", f"state={st}")

        report = lp.surface(outcome, boundary)
        check("B4: surface() took the terminal path (declared terminal)",
              report["path"] == "terminal"
              and report["terminal_state"] == "candidate",
              f"{report['path']}/{report['terminal_state']}")
        routing = report.get("terminal_routing", {})
        check("B5: seam router reached the real verdict-awaiting consumer",
              routing.get("mode") == "routed"
              and routing.get("consumer") == "acceptance_loop.verdict_awaiting",
              str(routing.get("consumer")))
        # ledger proof
        conn = sqlite3.connect(os.path.join(workdir, "terminal_ledger.db"))
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM terminal_routes WHERE route_id=?",
            (routing.get("route_id"),)).fetchone()
        conn.close()
        refs = json.loads(row["evidence_refs_json"]) if row else {}
        check("B6: terminal ledger holds the route with evidence refs",
              row is not None and refs.get("run_id") == run_id
              and refs.get("state") == "CANDIDATE",
              f"route_id={routing.get('route_id')}")
        stored = lp.acceptance_loop.store.get(run_id)
        st2 = getattr(stored.state, "name", stored.state)
        check("B7: stored record still CANDIDATE (never auto-advanced)",
              st2 == "CANDIDATE" and stored.decided_at is None,
              f"state={st2} decided_at={stored.decided_at}")

    # ---- D: adversarial ------------------------------------------------
    bad_attempt = Attempt(approach_signature=["adv"], plan={}, args={},
                          exec_ok=True)
    failing = AuthReport(held_out={}, passed=False)
    try:
        BoundaryPresentation(
            kind="completion_candidate",
            evidence={"run_id": "r_adv1", "goal": "g",
                      "attempt": bad_attempt, "auth": failing},
            observed_by="ploop12-adv").validate()
        check("D1: failing auth refused at the boundary gate", False,
              "validate() accepted auth.passed=False")
    except BoundaryRefused:
        check("D1: failing auth refused at the boundary gate", True)
    try:
        lp.acceptance_loop.present("r_adv2", "g", bad_attempt, failing)
        check("D2: failing auth refused at present()", False,
              "present() accepted auth.passed=False")
    except ValueError:
        check("D2: failing auth refused at present()", True)

    # D3: tamper the stored state, router must refuse
    r_t = f"run_ploop12_tamper_{uuid.uuid4().hex[:6]}"
    lp.acceptance_loop.present(r_t, goal, attempt, auth)
    conn = sqlite3.connect(os.path.join(workdir, "acc.db"))
    d = json.loads(conn.execute(
        "SELECT data FROM acceptance_records WHERE run_id=?",
        (r_t,)).fetchone()[0])
    d["state"] = "accepted"
    conn.execute("UPDATE acceptance_records SET data=? WHERE run_id=?",
                 (json.dumps(d), r_t))
    conn.commit()
    conn.close()
    from swarm_engine.core.executive.loops import LoopOutcome
    out_t = LoopOutcome(loop=LOOP_ACCEPTANCE, entered=True,
                        result=lp.acceptance_loop.store.get(r_t),
                        detail="tampered")
    try:
        lp.router.route_terminal(LOOP_ACCEPTANCE, out_t, {})
        check("D3: tampered store state refused by router", False,
              "routed a non-CANDIDATE record")
    except HandoffRefused:
        check("D3: tampered store state refused by router", True)

    # D4: never-persisted candidate refused
    ghost = type("Ghost", (), {"run_id": "r_ghost_never",
                               "state": AcceptanceState.CANDIDATE,
                               "goal": goal})()
    out_g = LoopOutcome(loop=LOOP_ACCEPTANCE, entered=True, result=ghost,
                        detail="ghost")
    try:
        lp.router.route_terminal(LOOP_ACCEPTANCE, out_g, {})
        check("D4: never-persisted candidate refused", False,
              "routed a candidate with no store record")
    except HandoffRefused:
        check("D4: never-persisted candidate refused", True)

    # D5: double-present while CANDIDATE awaits verdict (PLOOP-12 repair)
    r_d = f"run_ploop12_dup_{uuid.uuid4().hex[:6]}"
    lp.acceptance_loop.present(r_d, goal, attempt, auth)
    try:
        lp.acceptance_loop.present(r_d, goal, attempt, auth)
        check("D5: double-present refused", False,
              "second present silently overwrote")
    except ValueError as exc:
        check("D5: double-present refused", "already presented" in str(exc),
              str(exc)[:120])

    # D6: re-present after ACCEPTED (PLOOP-12 repair) -- verdict intact
    r_a = f"run_ploop12_acc_{uuid.uuid4().hex[:6]}"
    lp.acceptance_loop.present(r_a, goal, attempt, auth)
    lp.acceptance_loop.record_verdict(r_a, True, feedback="good")
    try:
        lp.acceptance_loop.present(r_a, goal, attempt, auth)
        check("D6: re-present after ACCEPTED refused", False,
              "clobbered an ACCEPTED verdict")
    except ValueError:
        kept = lp.acceptance_loop.store.get(r_a)
        ks = getattr(kept.state, "name", kept.state)
        check("D6: re-present after ACCEPTED refused, verdict intact",
              ks == "ACCEPTED", f"state={ks}")

    n_pass = sum(1 for _, ok, _ in CHECKS if ok)
    n_fail = len(CHECKS) - n_pass
    print(f"\nPLOOP-12 acceptance-seam proof: {n_pass}/{len(CHECKS)} PASS, "
          f"{n_fail} FAIL")
    sys.exit(0 if n_fail == 0 else 1)


if __name__ == "__main__":
    main()
