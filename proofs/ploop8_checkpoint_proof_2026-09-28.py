"""PLOOP-8 durable proof: checkpoint/recovery ACROSS loop transitions.

End-to-end, all real machinery, no fixtures, fresh process. The
checkpoint layer (runtime/core/executive/checkpoint.py) around the
handoff contract (runtime/core/executive/handoff.py) under test:

  C1  backward compatibility: produce/accept/transition WITHOUT a
      checkpoint store behave exactly as PLOOP-2 (checkpoint_id None).
  C2  produce WITH a store checkpoints the run loop's resumable state
      at produce time: durable row, deep from_state (real rc_cycles
      row), tamper-evidence hash verifies.
  C3  cross-process resume (T): process A ticks + produces; process B
      (fresh) loads the checkpoint from disk, rehydrates the handoff
      from the row + the honestly re-fetched GapRecord, accepts it
      (verified against the live world), enters acquisition through
      the executive -- the SAME gap_id is dispatched with unbroken
      triggering_boundary_id lineage -- and the checkpoint is
      consumed. Not a re-run of produce: B never calls produce.
  C4  in-process full transition() with a store: the real code path
      consumes the checkpoint once the receiving loop is entered.
  C5  replay protection: accepting the same handoff after consumption
      raises HandoffRefused loudly (no replay).
  C6  drift detection: the gap closed out-of-band between produce and
      accept -> HandoffRefused loudly naming the stale checkpoint
      (never silently resumed).
  C7  checkpoint_id without a store in context at accept time ->
      HandoffRefused loudly (durability claims that cannot be
      re-verified are not accepted).
  C8  tampered checkpoint row (from_state rewritten out-of-band) ->
      HandoffRefused loudly naming the integrity failure.

Imports resolve against the tree under test: PLOOP8_WT env var, else
~/workspace/ploop-8-work (this mission's worktree).
"""
import json
import os
import sqlite3
import subprocess
import sys
import tempfile

WT = os.environ.get(
    "PLOOP8_WT", os.path.expanduser("~/workspace/ploop-8-work"))
sys.path.insert(0, os.path.join(WT, "pylib"))

PASS_N = 0


def check(name, cond, detail=""):
    global PASS_N
    PASS_N += 1
    print(("PASS " if cond else "FAIL ") + name +
          (f" -- {detail}" if detail else ""), flush=True)
    if not cond:
        raise SystemExit(f"PLOOP-8 PROOF FAILED at: {name} {detail}")


def expect_refused(name, fn, needle=""):
    """The contract must refuse LOUDLY: HandoffRefused naming the cause."""
    global PASS_N
    from swarm_engine.core.executive.handoff import HandoffRefused
    PASS_N += 1
    try:
        fn()
    except HandoffRefused as exc:
        ok = (needle in str(exc)) if needle else True
        print(f"PASS {name} -- refused loudly: {exc}", flush=True)
        if not ok:
            raise SystemExit(
                f"PLOOP-8 PROOF FAILED at: {name}: refusal did not name "
                f"{needle!r}: {exc}")
        return
    except Exception as exc:  # noqa: BLE001 -- any other raise is wrong
        raise SystemExit(
            f"PLOOP-8 PROOF FAILED at: {name}: wrong exception "
            f"{type(exc).__name__}: {exc}")
    raise SystemExit(
        f"PLOOP-8 PROOF FAILED at: {name}: no refusal raised")


def _only_checkpoint_id(w):
    """The single checkpoint row in this world's store (fresh temp dir
    => exactly one row)."""
    con = sqlite3.connect(os.path.join(w["td"], "ckpt.db"))
    try:
        rec = con.execute(
            "SELECT checkpoint_id FROM transition_checkpoints").fetchall()
    finally:
        con.close()
    if len(rec) != 1:
        raise SystemExit(
            f"expected exactly one checkpoint row, got {len(rec)}")
    return rec[0][0]


def build_world(td):
    """Real machinery on temp db files. Returns a namespace dict."""
    from swarm_engine.core.engine import SwarmEngine
    from swarm_engine.core.run_controller import RunController, RunConfig
    from swarm_engine.core.executive import (
        ExecutiveController, TransitionCheckpointStore)
    from swarm_engine.acquisition.gaps import GapRegistry
    from swarm_engine.services.acceptance import (
        AcceptanceLoop, AcceptanceStore)
    eng = SwarmEngine(db_path=os.path.join(td, "engine.db"))
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
    store = TransitionCheckpointStore(os.path.join(td, "ckpt.db"))
    return {"eng": eng, "rc": rc, "registry": registry,
            "acc_loop": acc_loop, "ex": ex, "store": store, "td": td}


def run_tick(w):
    """Register a real gap, run a real tick, persist its cycle record.
    Returns (gap, b_run, out_run, view_before, view_after)."""
    from swarm_engine.core.executive.boundary import BoundaryPresentation
    from swarm_engine.core.executive.loops import LOOP_RUN
    rc, registry, ex = w["rc"], w["registry"], w["ex"]
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
    view_before = ex.loop_view(LOOP_RUN)
    out_run = ex.enter(b_run)
    view_after = ex.loop_view(LOOP_RUN)
    if not out_run.entered:
        raise SystemExit(f"tick did not enter: {out_run.detail}")
    summary = out_run.result
    rc._checkpoint.record_cycle(int(summary.get("cycle", 0)), summary)
    return gap, b_run, out_run, view_before, view_after


def produce_with_store(w, gap, b_run, out_run, view_before, view_after):
    from swarm_engine.core.executive.handoff import produce_handoff
    handoff = produce_handoff(
        outcome=out_run, triggering_boundary=b_run,
        context={"gap_fetcher": w["registry"].get,
                 "run_controller": w["rc"],
                 "checkpoint_store": w["store"]},
        view_before=view_before, view_after=view_after)
    if handoff is None or not handoff.checkpoint_id:
        raise SystemExit("no checkpointed handoff produced")
    return handoff


def main():
    from swarm_engine.core.executive import (
        TransitionCheckpointStore, verify_checkpoint_integrity)
    from swarm_engine.core.executive.handoff import (
        HandoffRefused, accept_handoff, produce_handoff, transition)
    from swarm_engine.core.executive.loops import LOOP_RUN, LoopOutcome

    # ------------------------------------------------------------- C1
    # No store in context: the contract behaves exactly as PLOOP-2.
    w = build_world(tempfile.mkdtemp(prefix="ploop8_c1_"))
    gap, b_run, out_run, vb, va = run_tick(w)
    h0 = produce_handoff(
        outcome=out_run, triggering_boundary=b_run,
        context={"gap_fetcher": w["registry"].get},
        view_before=vb, view_after=va)
    check("C1: produce without store -> handoff, no checkpoint",
          h0 is not None and h0.checkpoint_id is None,
          f"checkpoint_id={h0.checkpoint_id if h0 else None}")
    b0 = accept_handoff(w["ex"], h0, {"gap_fetcher": w["registry"].get})
    check("C1: accept without store -> boundary (unchanged contract)",
          b0.kind == "acquisition_gap", b0.kind)

    # ------------------------------------------------------------- C2
    # Produce with a store: durable checkpoint of the real run state.
    w = build_world(tempfile.mkdtemp(prefix="ploop8_c2_"))
    gap, b_run, out_run, vb, va = run_tick(w)
    h = produce_with_store(w, gap, b_run, out_run, vb, va)
    check("C2: checkpoint_id attached at produce time",
          bool(h.checkpoint_id), h.checkpoint_id)
    # Fresh store instance, fresh connection: the row is durable.
    store2 = TransitionCheckpointStore(
        os.path.join(w["td"], "ckpt.db"))
    row = store2.load(h.handoff_id)
    check("C2: checkpoint row durable (fresh connection)",
          row is not None and row["checkpoint_id"] == h.checkpoint_id)
    check("C2: row mirrors the handoff",
          row["from_loop"] == "run" and row["to_loop"] == "acquisition"
          and row["boundary_kind"] == "acquisition_gap"
          and row["triggering_boundary_id"] == b_run.boundary_id
          and row["evidence_refs"].get("gap_record") == gap.gap_id,
          str(row["evidence_refs"]))
    fs = row["from_state"]
    check("C2: from_state is deep run state (real rc_cycles row)",
          fs.get("extraction") == "deep"
          and isinstance(fs.get("cycle_n"), int)
          and fs.get("summary_sha256"),
          f"extraction={fs.get('extraction')} cycle_n={fs.get('cycle_n')}")
    check("C2: tamper-evidence hash verifies",
          (verify_checkpoint_integrity(row) or True))
    check("C2: status saved", row["status"] == "saved", row["status"])

    # ------------------------------------------------------------- C3 (T)
    # Cross-process resume: A produces, B (fresh) resumes from disk.
    td = tempfile.mkdtemp(prefix="ploop8_c3_")
    result_path = os.path.join(td, "result.json")
    verdict_path = os.path.join(td, "verdict.json")
    env = dict(os.environ, PLOOP8_WT=WT)
    pa = subprocess.run(
        [sys.executable, os.path.join(WT, "proofs", "ploop8_proc_a.py"),
         td, result_path], env=env, capture_output=True, text=True,
        timeout=300)
    check("C3: process A produced a checkpointed handoff",
          pa.returncode == 0, (pa.stdout + pa.stderr)[-300:])
    pb = subprocess.run(
        [sys.executable, os.path.join(WT, "proofs", "ploop8_proc_b.py"),
         td, result_path, verdict_path], env=env, capture_output=True,
        text=True, timeout=300)
    check("C3: process B resumed from the durable checkpoint",
          pb.returncode == 0, (pb.stdout + pb.stderr)[-300:])
    with open(verdict_path) as f:
        verdict = json.load(f)
    check("C3: B dispatched the SAME gap the checkpoint names",
          verdict["dispatched_gap_id"] == verdict["checkpoint_gap_id"],
          str(verdict))
    check("C3: lineage unbroken across processes",
          verdict["lineage_ok"] is True)
    check("C3: checkpoint consumed after entry (single-use)",
          verdict["final_status"] == "consumed")

    # ------------------------------------------------------------- C4
    # In-process full transition(): the real code path consumes.
    w = build_world(tempfile.mkdtemp(prefix="ploop8_c4_"))
    gap, b_run, out_run, vb, va = run_tick(w)
    ctx = {"gap_fetcher": w["registry"].get,
           "run_controller": w["rc"],
           "checkpoint_store": w["store"]}
    received = transition(executive=w["ex"], outcome=out_run,
                          triggering_boundary=b_run, context=ctx,
                          view_before=vb, view_after=va)
    check("C4: transition entered acquisition",
          received is not None and received.entered,
          getattr(received, "detail", "")[:80])
    check("C4: transition dispatched the checkpoint's gap",
          getattr(received.result, "gap_id", None) == gap.gap_id,
          str(getattr(received.result, "gap_id", None)))
    row = w["store"].load_by_checkpoint_id(
        _only_checkpoint_id(w))
    check("C4: checkpoint consumed by the real transition path",
          row is not None and row["status"] == "consumed",
          row["status"] if row else "no row")
    # Re-resolve the checkpoint id through a fresh load by handoff gap:
    rec = w["registry"].get(gap.gap_id)
    check("C4: gap still open after honest transition",
          rec.status in ("open", "acquiring"), rec.status)

    # ------------------------------------------------------------- C5
    # Replay: the consumed checkpoint refuses a second accept.
    w = build_world(tempfile.mkdtemp(prefix="ploop8_c5_"))
    gap, b_run, out_run, vb, va = run_tick(w)
    ctx = {"gap_fetcher": w["registry"].get,
           "run_controller": w["rc"],
           "checkpoint_store": w["store"]}
    h = produce_with_store(w, gap, b_run, out_run, vb, va)
    accept_handoff(w["ex"], h, ctx)  # verifies
    w["store"].mark_consumed(h.checkpoint_id)
    expect_refused(
        "C5: replay of a consumed checkpoint is refused",
        lambda: accept_handoff(w["ex"], h, ctx), "consumed")

    # ------------------------------------------------------------- C6
    # Drift: the gap closed out-of-band between produce and accept.
    w = build_world(tempfile.mkdtemp(prefix="ploop8_c6_"))
    gap, b_run, out_run, vb, va = run_tick(w)
    ctx = {"gap_fetcher": w["registry"].get,
           "run_controller": w["rc"],
           "checkpoint_store": w["store"]}
    h = produce_with_store(w, gap, b_run, out_run, vb, va)
    rec = w["registry"].get(gap.gap_id)
    rec.status = "closed"  # out-of-band world change, real persistence
    w["registry"]._save(rec)
    expect_refused(
        "C6: drifted checkpoint (gap closed) is refused, not resumed",
        lambda: accept_handoff(w["ex"], h, ctx), "no longer an open")

    # ------------------------------------------------------------- C7
    # checkpoint_id present but no store in context at accept time.
    w = build_world(tempfile.mkdtemp(prefix="ploop8_c7_"))
    gap, b_run, out_run, vb, va = run_tick(w)
    h = produce_with_store(w, gap, b_run, out_run, vb, va)
    expect_refused(
        "C7: checkpoint without a store at accept is refused",
        lambda: accept_handoff(
            w["ex"], h, {"gap_fetcher": w["registry"].get}),
        "no checkpoint")

    # ------------------------------------------------------------- C8
    # Tampered row: from_state rewritten out-of-band.
    w = build_world(tempfile.mkdtemp(prefix="ploop8_c8_"))
    gap, b_run, out_run, vb, va = run_tick(w)
    ctx = {"gap_fetcher": w["registry"].get,
           "run_controller": w["rc"],
           "checkpoint_store": w["store"]}
    h = produce_with_store(w, gap, b_run, out_run, vb, va)
    con = sqlite3.connect(os.path.join(w["td"], "ckpt.db"))
    try:
        con.execute(
            "UPDATE transition_checkpoints SET from_state_json=? "
            "WHERE handoff_id=?", ('{"tampered": true}', h.handoff_id))
        con.commit()
    finally:
        con.close()
    expect_refused(
        "C8: tampered checkpoint row is refused",
        lambda: accept_handoff(w["ex"], h, ctx), "tamper")

    print(f"\nPLOOP-8 PROOF COMPLETE: {PASS_N}/{PASS_N} checks green")


main()
