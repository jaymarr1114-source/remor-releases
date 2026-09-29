"""PLOOP-8 cross-process proof, process B: fresh-process resume.

Reads <result.json> from process A, rebuilds the real machinery on the
SAME db files, loads the checkpoint on a fresh connection (proving
durability), rehydrates the handoff from the checkpoint row plus the
honestly re-fetched GapRecord, accepts it (verification against the
live world), enters the acquisition loop through the executive, and
consumes the checkpoint -- replicating transition()'s consume tail.
Writes the verdict to <verdict.json>. Any failure exits nonzero.
"""
import json
import os
import sys

WT = os.environ.get(
    "PLOOP8_WT", os.path.expanduser("~/workspace/ploop-8-work"))
sys.path.insert(0, os.path.join(WT, "pylib"))


def main():
    workdir, result_path, verdict_path = sys.argv[1], sys.argv[2], sys.argv[3]
    with open(result_path) as f:
        res = json.load(f)

    from swarm_engine.core.engine import SwarmEngine
    from swarm_engine.core.run_controller import RunController, RunConfig
    from swarm_engine.core.executive import (
        ExecutiveController, TransitionCheckpointStore)
    from swarm_engine.core.executive.handoff import (
        accept_handoff, rehydrate_handoff)
    from swarm_engine.acquisition.gaps import GapRegistry
    from swarm_engine.services.acceptance import (
        AcceptanceLoop, AcceptanceStore)

    eng = SwarmEngine(db_path=os.path.join(workdir, "engine.db"))
    epi = eng.intellect.epistemic
    rc = RunController(
        eng,
        config=RunConfig(cadence_interval_s=60, cycle_budget_s=120,
                         max_gaps_per_cycle=5),
        checkpoint_path=os.path.join(workdir, "rc.db"))
    registry = GapRegistry(eng, db_path=os.path.join(workdir, "gaps.db"))
    rc._registry = registry
    acc_loop = AcceptanceLoop(
        AcceptanceStore(db_path=os.path.join(workdir, "acc.db")), epi,
        engine=eng)
    ex = ExecutiveController(
        engine=eng, run_controller=rc, gap_registry=registry,
        acceptance_loop=acc_loop)

    # Fresh connection, fresh process: the checkpoint must be durable.
    store = TransitionCheckpointStore(res["ckpt_db"])
    row = store.load(res["handoff_id"])
    if row is None:
        raise SystemExit("PROC-B FAILED: checkpoint not durable across "
                         "processes: no row for handoff")
    if row["checkpoint_id"] != res["checkpoint_id"]:
        raise SystemExit("PROC-B FAILED: checkpoint_id mismatch across "
                         "processes")
    if row["status"] != "saved":
        raise SystemExit(f"PROC-B FAILED: expected status 'saved', got "
                         f"{row['status']!r}")

    # Honest re-fetch: the checkpoint names the gap_id; the REAL record
    # comes from the registry, never from the row alone.
    rec = registry.get(res["gap_id"])
    if rec is None:
        raise SystemExit("PROC-B FAILED: checkpoint's gap_id not in registry")
    if rec.status not in ("open", "acquiring"):
        raise SystemExit(f"PROC-B FAILED: gap status {rec.status!r}: not "
                         "a live open boundary")

    handoff = rehydrate_handoff(row, {"gap_record": rec})
    ctx = {"gap_fetcher": registry.get,
           "run_controller": rc,
           "checkpoint_store": store}
    boundary = accept_handoff(ex, handoff, ctx)
    if handoff.verified_checkpoint is None:
        raise SystemExit("PROC-B FAILED: accept did not verify the checkpoint")
    if handoff.verified_checkpoint["triggering_boundary_id"] != \
            res["triggering_boundary_id"]:
        raise SystemExit("PROC-B FAILED: lineage broken across processes")

    out = ex.enter(boundary)
    if not out.entered:
        raise SystemExit(f"PROC-B FAILED: acquisition not entered: {out.detail}")
    dispatched = getattr(out.result, "gap_id", None)
    if dispatched != res["gap_id"]:
        raise SystemExit(f"PROC-B FAILED: resumed the wrong gap: dispatched "
                         f"{dispatched} != checkpoint {res['gap_id']}")

    # transition()'s consume tail, replicated exactly: single-use.
    if handoff.checkpoint_id is not None and out.entered:
        store.mark_consumed(handoff.checkpoint_id)
    row2 = store.load(res["handoff_id"])
    if row2["status"] != "consumed":
        raise SystemExit(f"PROC-B FAILED: checkpoint not consumed: "
                         f"{row2['status']!r}")

    verdict = {
        "entered": True,
        "dispatched_gap_id": dispatched,
        "checkpoint_gap_id": res["gap_id"],
        "lineage_ok": True,
        "final_status": "consumed",
    }
    with open(verdict_path, "w") as f:
        json.dump(verdict, f)
    print("PROC-B OK", flush=True)


main()
