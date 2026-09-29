"""PLOOP-8 cross-process proof, process A: real tick -> produce with checkpoint.

Builds the real machinery on db files under <workdir>, runs a real
run-loop tick that surfaces a real open gap, persists the tick's
cycle record through the controller's real record_cycle, and produces
a handoff with a checkpoint store engaged. Writes the handoff's
durable identity to <result.json> and exits. Process B resumes from
these files in a fresh process.
"""
import json
import os
import sys

WT = os.environ.get(
    "PLOOP8_WT", os.path.expanduser("~/workspace/ploop-8-work"))
sys.path.insert(0, os.path.join(WT, "pylib"))


def main():
    workdir, result_path = sys.argv[1], sys.argv[2]
    os.makedirs(workdir, exist_ok=True)

    from swarm_engine.core.engine import SwarmEngine
    from swarm_engine.core.run_controller import RunController, RunConfig
    from swarm_engine.core.executive import (
        ExecutiveController, TransitionCheckpointStore)
    from swarm_engine.core.executive.boundary import BoundaryPresentation
    from swarm_engine.core.executive.handoff import produce_handoff
    from swarm_engine.core.executive.loops import LOOP_RUN
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
    rc._registry = registry  # proof wiring: the tick sees this registry
    acc_loop = AcceptanceLoop(
        AcceptanceStore(db_path=os.path.join(workdir, "acc.db")), epi,
        engine=eng)
    ex = ExecutiveController(
        engine=eng, run_controller=rc, gap_registry=registry,
        acceptance_loop=acc_loop)

    gap = registry.register_dependency_gap(
        "nonexistent_pkg_xyz", "package", "ploop-8 proof boundary",
        evidence=[{"kind": "observation", "observed": True,
                   "detail": "real probe: importlib.util.find_spec("
                             "'nonexistent_pkg_xyz') is None"}],
        registered_by="ploop-8-proof")
    b_run = BoundaryPresentation(
        kind="run_wake",
        evidence={"wake_reason": "cadence_tick", "run_id": rc._run_id},
        observed_by="run_controller")

    ckpt_db = os.path.join(workdir, "ckpt.db")
    store = TransitionCheckpointStore(ckpt_db)
    view_before = ex.loop_view(LOOP_RUN)
    out_run = ex.enter(b_run)
    view_after = ex.loop_view(LOOP_RUN)
    if not out_run.entered:
        raise SystemExit(f"PROC-A FAILED: tick did not enter: {out_run.detail}")
    # Persist the tick's cycle record through the controller's real
    # persistence (run() does this per cycle; the inlet path calls
    # tick() directly, so the proof persists it the same way).
    summary = out_run.result
    rc._checkpoint.record_cycle(int(summary.get("cycle", 0)), summary)

    handoff = produce_handoff(
        outcome=out_run, triggering_boundary=b_run,
        context={"gap_fetcher": registry.get,
                 "run_controller": rc,
                 "checkpoint_store": store},
        view_before=view_before, view_after=view_after)
    if handoff is None or not handoff.checkpoint_id:
        raise SystemExit("PROC-A FAILED: no checkpointed handoff produced")

    result = {
        "workdir": workdir,
        "ckpt_db": ckpt_db,
        "handoff_id": handoff.handoff_id,
        "checkpoint_id": handoff.checkpoint_id,
        "from_loop": handoff.from_loop,
        "to_loop": handoff.to_loop,
        "terminal_state": handoff.terminal_state,
        "boundary_kind": handoff.boundary_kind,
        "triggering_boundary_id": handoff.triggering_boundary_id,
        "chain_depth": handoff.chain_depth,
        "evidence_refs": dict(handoff.evidence_refs),
        "resource_delta": dict(handoff.resource_delta),
        "gap_id": handoff.evidence_refs.get("gap_record"),
    }
    with open(result_path, "w") as f:
        json.dump(result, f)
    print(f"PROC-A OK {handoff.handoff_id} {handoff.checkpoint_id}",
          flush=True)


main()
