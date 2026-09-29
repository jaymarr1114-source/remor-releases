#!/usr/bin/env python3
"""PLOOP-3 proof: continuous turning + per-cycle acceptance evidence.

Proves, on the real machinery (no mocks, no staged demos):
  RUN A -- the RunController sustains 10 consecutive autonomous cycles
           (one run() call, back-to-back, no manual reset), and EVERY
           cycle produces an acceptance record persisted in acceptance.db.
  RUN B -- cycles that fail acceptance are recorded REJECTED (never
           skipped, never auto-passed): a zero cycle budget genuinely
           trips the budget_exceeded path through the real tick().
  RECHECK -- an independent pass re-reads acceptance.db + ckpt.db with
           fresh sqlite connections (no controller objects) and
           re-derives every verdict from the persisted rows via the
           driver's own evaluate_cycle (single source of truth, imported
           -- never copied).

Exit 0 only if every count below holds exactly.
"""

import json
import os
import sqlite3
import sys
import tempfile

CANON = "/home/hatch/workspace/worktrees/ploop-3"
sys.path.insert(0, os.path.join(CANON, "pylib"))
sys.path.insert(0, CANON)


def seed(workdir, cycle_budget_s=30.0, max_cycles=10):
    from swarm_engine.core.engine import SwarmEngine
    from swarm_engine.core.run_controller import RunController, RunConfig
    from swarm_engine.acquisition.gaps import (
        GapRegistry, GapRecord, DissatisfactionBlock)
    eng = SwarmEngine(db_path=os.path.join(workdir, "eng.db"))
    registry = GapRegistry(eng)
    # One genuine user gap: real registration, real dispatch (routes by
    # shape -> honestly "open", then backed off -- real machinery).
    registry.register(GapRecord(
        registered_by="ploop3/proof",
        summary="user dissatisfied: proof gap for continuous turning",
        dissatisfaction=DissatisfactionBlock(
            attempt_ref="ploop3_attempt_1",
            feedback="the draft missed the key figure",
            unmet_criteria="must include the key figure"),
        evidence=[{"kind": "observation",
                   "observed": "seeded user gap for PLOOP-3",
                   "detail": "proof seed"}]))
    cfg = RunConfig(
        cadence_interval_s=0.05, run_budget_s=120.0,
        cycle_budget_s=cycle_budget_s, gap_budget_s=5.0,
        max_cycles=max_cycles, max_gaps_per_cycle=25,
        trusted_indexes=[],  # no Q7: no network in this proof
        staging_dir=os.path.join(workdir, "rc_q7_staging"))
    rc = RunController(eng, config=cfg,
                       checkpoint_path=os.path.join(workdir, "ckpt.db"),
                       registry=registry)
    return eng, rc


def check(cond, msg):
    if not cond:
        print(f"FAIL: {msg}")
        sys.exit(1)
    print(f"ok: {msg}")


# ---------------------------------------------------------------- RUN A ---
workdir_a = tempfile.mkdtemp(prefix="ploop3_runA_")
eng_a, rc_a = seed(workdir_a)
report_a = rc_a.run()
run_id_a = rc_a._run_id

check(len(report_a["cycles"]) == 10,
      f"RUN A: 10 consecutive cycles in one run() call "
      f"(got {len(report_a['cycles'])})")
check([s.get("cycle") for s in report_a["cycles"]] == list(range(1, 11)),
      "RUN A: cycles numbered 1..10 back-to-back, no manual reset")
check(not report_a["resumed"], "RUN A: fresh run, not a resume")
n_acc = 0
for s in report_a["cycles"]:
    a = s.get("acceptance")
    check(isinstance(a, dict) and a.get("state") == "accepted",
          f"RUN A cycle {s.get('cycle')}: acceptance record accepted "
          f"(state={a.get('state') if isinstance(a, dict) else a!r})")
    check("tick_error" not in s, f"RUN A cycle {s['cycle']}: no tick_error")
    n_acc += 1
check(n_acc == 10, "RUN A: 10/10 cycles carry accepted acceptance records")

# ---------------------------------------------------------------- RUN B ---
workdir_b = tempfile.mkdtemp(prefix="ploop3_runB_")
eng_b, rc_b = seed(workdir_b, cycle_budget_s=0.0, max_cycles=2)
report_b = rc_b.run()
run_id_b = rc_b._run_id

check(len(report_b["cycles"]) == 2, "RUN B: 2 cycles ran")
for s in report_b["cycles"]:
    a = s.get("acceptance")
    check(isinstance(a, dict) and a.get("state") == "rejected",
          f"RUN B cycle {s.get('cycle')}: failed cycle recorded REJECTED "
          f"(state={a.get('state') if isinstance(a, dict) else a!r})")
    check("budget_honored" in (a.get("failed_checks") or []),
          f"RUN B cycle {s.get('cycle')}: failed check names "
          f"budget_honored ({a.get('failed_checks')})")
    check(s.get("budget_exceeded") is True,
          f"RUN B cycle {s.get('cycle')}: budget_exceeded genuinely True "
          f"through the real tick() path")

# ------------------------------------------------------------- RECHECK ---
from swarm_engine.services.acceptance_driver import AcceptanceDriver


def recheck(workdir, run_id, cycles, expect_state):
    ck = sqlite3.connect(os.path.join(workdir, "ckpt.db"))
    ac = sqlite3.connect(os.path.join(workdir, "acceptance.db"))
    n = 0
    try:
        for cyc in cycles:
            rid = f"{run_id}:cycle:{cyc}"
            arow = ac.execute(
                "SELECT data FROM acceptance_records WHERE run_id=?",
                (rid,)).fetchone()
            check(arow is not None,
                  f"RECHECK {rid}: acceptance record exists on disk")
            rec = json.loads(arow[0])
            check(rec["state"] == expect_state,
                  f"RECHECK {rid}: persisted state == {expect_state}")
            check(rec["auth"]["passed"] == (expect_state == "accepted"),
                  f"RECHECK {rid}: auth.passed consistent with state")
            check(len(rec["auth"]["held_out"]) == 6,
                  f"RECHECK {rid}: all 6 named checks persisted")
            crow = ck.execute(
                "SELECT summary_json FROM rc_cycles WHERE n=?",
                (cyc,)).fetchone()
            check(crow is not None,
                  f"RECHECK cycle {cyc}: checkpoint row exists on disk")
            persisted = json.loads(crow[0])
            # Re-derive the verdict from the persisted row via the single
            # source of truth (imported, not copied).
            presented = {"errors": persisted.get("errors", []),
                         "budget_exceeded": persisted.get("budget_exceeded",
                                                          False)}
            passed, checks = AcceptanceDriver.evaluate_cycle(
                cyc, presented, persisted)
            check(passed == (expect_state == "accepted"),
                  f"RECHECK {rid}: independently re-derived verdict "
                  f"matches persisted state")
            check(all(v["passed"] for v in checks.values())
                  == (expect_state == "accepted"),
                  f"RECHECK {rid}: check-level agreement")
            n += 1
    finally:
        ck.close()
        ac.close()
    return n


n_a = recheck(workdir_a, run_id_a, range(1, 11), "accepted")
n_b = recheck(workdir_b, run_id_b, range(1, 3), "rejected")
check(n_a == 10 and n_b == 2,
      f"RECHECK: 12/12 acceptance records verified from disk "
      f"({n_a} accepted, {n_b} rejected)")

# Near-misses for the failed cycles persisted as epistemic observations.
nms = [o for o in eng_b.intellect.epistemic.all_observations()
       if (o.source or "") == "acceptance_loop"
       and (o.raw or {}).get("type") == "near_miss"]
check(len(nms) >= 2,
      f"RUN B: near-miss observations persisted for failed cycles "
      f"({len(nms)} found)")

print("\nPLOOP-3 PROOF GREEN: 10/10 accepted, 2/2 rejected, "
      "12/12 re-verified from disk.")
