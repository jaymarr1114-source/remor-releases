"""PLOOP-12 kill/resume proc B: fresh process resumes from disk.

Reads the persisted CANDIDATE record from the real AcceptanceStore (never
re-presents: the PLOOP-12 double-present guard would rightly refuse),
rebuilds the LoopOutcome from the durable record, surfaces it through the
seam's contracts, and routes the CANDIDATE terminal to the real
verdict-awaiting consumer. Verifies lineage against proc A's handoff file.
Usage: ploop12_proc_b.py <workdir> <in_json>"""
import json
import os
import sqlite3
import sys

WT = os.environ.get("PLOOP12_WT",
                    os.path.expanduser("~/workspace/remor_convergence/worktrees/ploop12"))
sys.path.insert(0, os.path.join(WT, "pylib"))
sys.path.insert(0, os.path.join(WT, "proofs"))


def main():
    from swarm_engine.core.executive.live_path import (
        build_live_path, LivePathConfig)
    from swarm_engine.core.executive.boundary import BoundaryPresentation
    from swarm_engine.core.executive.loops import LOOP_ACCEPTANCE, LoopOutcome

    workdir, in_json = sys.argv[1], sys.argv[2]
    with open(in_json) as f:
        handoff = json.load(f)
    run_id = handoff["run_id"]

    # Fresh process, same db files: rebuild the live path from disk.
    lp = build_live_path(LivePathConfig(workdir=workdir))

    # Honestly re-fetch: the durable record, not checkpointed bytes.
    # (Re-entering the inlet would double-present and be refused --
    # the guard forces resume-through-the-store.)
    record = lp.acceptance_loop.store.get(run_id)
    assert record is not None, "no persisted candidate: nothing to resume"
    assert getattr(record.state, "name", record.state) == "CANDIDATE"
    assert record.presented_at == handoff["presented_at"], \
        "lineage break: presented_at mismatch across processes"
    assert record.goal == handoff["goal"], "lineage break: goal mismatch"

    outcome = LoopOutcome(loop=LOOP_ACCEPTANCE, entered=True, result=record,
                          detail=f"resumed {run_id} from durable store")
    # The triggering boundary for a declared terminal is not consumed by
    # produce (declared terminals return None before accept); rebuild a
    # valid one from the durable record for the seam's contract shape.
    boundary = BoundaryPresentation(
        kind="completion_candidate",
        evidence={"run_id": record.run_id, "goal": record.goal,
                  "attempt": record.attempt, "auth": record.auth},
        observed_by="ploop12-kr-b").validate()

    report = lp.surface(outcome, boundary)
    assert report["path"] == "terminal", report
    routing = report["terminal_routing"]
    assert routing["consumer"] == "acceptance_loop.verdict_awaiting", routing

    conn = sqlite3.connect(os.path.join(workdir, "terminal_ledger.db"))
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT * FROM terminal_routes WHERE route_id=?",
        (routing["route_id"],)).fetchone()
    conn.close()
    refs = json.loads(row["evidence_refs_json"])
    assert refs["run_id"] == run_id and refs["state"] == "CANDIDATE"

    stored = lp.acceptance_loop.store.get(run_id)
    assert getattr(stored.state, "name", stored.state) == "CANDIDATE"
    print(f"PROC-B OK: resumed {run_id}, routed to "
          f"{routing['consumer']}, lineage intact")


if __name__ == "__main__":
    main()
