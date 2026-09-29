"""PLOOP-12 kill/resume proc A: real work + real auth + enter through the
executive (present() persists the CANDIDATE record to disk), then DIES
before routing. Usage: ploop12_proc_a.py <workdir> <out_json>"""
import json
import os
import sys
import uuid

WT = os.environ.get("PLOOP12_WT",
                    os.path.expanduser("~/workspace/remor_convergence/worktrees/ploop12"))
sys.path.insert(0, os.path.join(WT, "pylib"))
sys.path.insert(0, os.path.join(WT, "proofs"))


def main():
    from swarm_engine.core.executive.live_path import (
        build_live_path, LivePathConfig)
    from swarm_engine.core.executive.boundary import BoundaryPresentation
    from swarm_engine.core.executive.loops import LOOP_ACCEPTANCE
    from ploop12_work import do_real_work

    workdir, out_json = sys.argv[1], sys.argv[2]
    lp = build_live_path(LivePathConfig(workdir=workdir))

    attempt, auth, info = do_real_work(lp.engine)
    assert auth.passed is True

    run_id = f"run_ploop12_kr_{uuid.uuid4().hex[:8]}"
    goal = "induce two-input addition from worked examples"
    boundary = BoundaryPresentation(
        kind="completion_candidate",
        evidence={"run_id": run_id, "goal": goal,
                  "attempt": attempt, "auth": auth},
        observed_by="ploop12-kr-a").validate()

    outcome = lp.executive.enter(boundary)
    assert outcome.entered and outcome.loop == LOOP_ACCEPTANCE
    rec = outcome.result
    assert getattr(rec.state, "name", rec.state) == "CANDIDATE"

    # Durable: the CANDIDATE record is in the real AcceptanceStore on disk.
    # Die now -- routing happens in proc B.
    with open(out_json, "w") as f:
        json.dump({"run_id": run_id, "goal": goal,
                   "presented_at": rec.presented_at,
                   "boundary_id": boundary.boundary_id}, f)
    print(f"PROC-A OK: presented {run_id}, dying before route")
    # exit 0 without routing: the kill point


if __name__ == "__main__":
    main()
