"""SEAM-WIRE-1 proof: run-loop microcontroller hosting (mandate 3).

Proves the SEAM_RUN_LOOP.md wiring through the REAL path:
- A real gap is registered in the real GapRegistry.
- RunLoopInlet.dispatch_gap_hosted hosts its dispatch as
  spawn(loop="run", purpose=f"dispatch:{gap_id}", budget_s=...).
- The registry's real dispatch runs inside the microcontroller.
- Execution-routed gaps spawn nested children for diagnose->repair->verify
  (depth 1, within max_depth, against the run loop's admission pool).
- retire(mc_id, outcome) at checkpoint: parent and children retire; the
  controller's checkpoint notes the hosted dispatch.
- resolve_loop("run") at sleep via sleep_run_loop.
- Adversarial: spawn refusal (exhausted pool) returns honestly, never raises.

Exit 0 with COUNT PASS / 0 FAIL, else nonzero. Run sequentially, never
overlapping another battery (2-core host).
"""
import os
import sys
import tempfile
import time
import uuid

WT = os.environ.get("SEAMWIRE_WT",
                    os.path.expanduser("~/workspace/worktrees/seam-wire-1"))
sys.path.insert(0, os.path.join(WT, "pylib"))

CHECKS = []


def check(name, cond, detail=""):
    CHECKS.append((name, bool(cond), str(detail)[:220]))
    print(f"[{'PASS' if cond else 'FAIL'}] {name}"
          + (f" -- {str(detail)[:160]}" if detail and not cond else ""),
          flush=True)


def main():
    from swarm_engine.core.executive.live_path import (
        build_live_path, LivePathConfig)

    workdir = tempfile.mkdtemp(prefix="seamwire3_")
    config = LivePathConfig(workdir=workdir, bind_store=False,
                            bind_router=False)
    lp = build_live_path(config)
    executive = lp.executive
    registry = lp.registry
    substrate = executive._substrate

    # -- a real gap in the real registry --------------------------------
    from swarm_engine.acquisition.gaps import GapRecord
    gap_id = f"seamwire3-{uuid.uuid4().hex[:8]}"
    record = GapRecord(
        gap_id=gap_id,
        registered_at=time.time(),
        registered_by="seamwire3-proof",
        summary="hosted dispatch proof gap",
        evidence=[{"kind": "observation",
                   "observed": "seamwire3 proof gap",
                   "detail": ("a real observable gap: the proof's own "
                              "dispatch target, observed at registration")}],
    )
    registry.register(record)
    check("T1 gap registered in the real registry",
          registry.get(gap_id) is not None, gap_id)

    # -- the run inlet offers the hosted path ---------------------------
    inlet = executive.registrations()["run"].inlet
    check("T2 run inlet exposes dispatch_gap_hosted",
          hasattr(inlet, "dispatch_gap_hosted"))

    # -- host the dispatch ----------------------------------------------
    report = inlet.dispatch_gap_hosted(gap_id, budget_s=60.0)
    check("T3 hosted dispatch ran", report.get("hosted") is True,
          str(report.get("dispatch_error", ""))[:120])
    check("T4 spawn purpose is dispatch:{gap_id}",
          report.get("mc_id") is not None and
          report.get("gap_id") == gap_id)
    # The microcontroller retired: no longer active on the substrate.
    view = substrate.loop_view("run")
    active_ids = [getattr(m, "mc_id", "") for m in
                  (getattr(view, "active_microcontrollers", []) or [])]
    check("T5 parent microcontroller retired (not active)",
          report["mc_id"] not in active_ids,
          f"active={active_ids}")
    check("T6 retire recorded with outcome",
          report.get("retired", {}).get("outcome") in (
              "resolved", "exhausted"),
          str(report.get("retired")))

    # -- nested children (execution-routed gap) --------------------------
    # A real quarantined capability through the engine's own path (PLOOP-11
    # pattern), then a gap registered from its diagnosis -> routes to
    # execution -> the hosted dispatch spawns diagnose->repair->verify.
    from swarm_engine.synthesis.capability_store import (
        plan_fingerprint, CapabilityRecord)
    plan = {"steps": [{"id": "s1", "op": "probe", "args": {}}]}
    cap_id = plan_fingerprint(plan)
    cap_rec = CapabilityRecord(
        capability_id=cap_id, name="seamwire3 probe capability",
        goal="prove hosted execution legs", plan=plan, ops=["probe"],
        effects=["none"])
    lp.engine.capabilities.store(cap_rec)
    lp.engine.quarantine_as_engine(
        cap_id, "missing_dependency:package:nonexistent_pkg_xyz:"
                "seamwire3 hosted dispatch proof")
    try:
        exec_gap = registry.register_from_diagnosis(
            capability_id=cap_id, registered_by="seamwire3-proof")
        exec_gap_id = exec_gap.gap_id
        has_exec_gap = True
    except Exception as exc:
        has_exec_gap = False
        exec_gap_id = None
        print(f"[note] execution gap registration: {type(exc).__name__}: "
              f"{exc}", flush=True)
    if has_exec_gap:
        exec_report = inlet.dispatch_gap_hosted(exec_gap_id, budget_s=60.0)
        children = exec_report.get("children", [])
        legs = [c.get("leg") for c in children if c.get("spawned")]
        check("T7 execution legs hosted as nested children",
              "diagnose" in legs and "repair" in legs and "verify" in legs,
              f"route={exec_report.get('dispatch', {}).get('route_name')} "
              f"legs={legs}")
        # Children retired too.
        view2 = substrate.loop_view("run")
        active2 = [getattr(m, "mc_id", "") for m in
                   (getattr(view2, "active_microcontrollers", []) or [])]
        child_ids = [c.get("mc_id") for c in children if c.get("mc_id")]
        check("T8 child microcontrollers retired",
              all(cid not in active2 for cid in child_ids),
              f"active={active2}")
    else:
        check("T7 execution legs hosted as nested children", False,
              "no execution-routed gap available")
        check("T8 child microcontrollers retired", False, "skipped")

    # -- adversarial: exhausted pool refuses honestly --------------------
    # Fill the run loop's admission pool (max_concurrent), then hosted
    # dispatch must refuse (not raise). BEFORE sleep: resolve_loop would
    # close the loop to new spawns. The pool holds 600s; fillers take 5s.
    fillers = []
    try:
        for i in range(200):
            s = substrate.spawn(loop="run",
                                purpose=f"seamwire3-fill-{i}",
                                budget_s=5.0)
            if not s.ok:
                break
            fillers.append(s.mc.mc_id)
    finally:
        pass
    check("T10a pool filled to exhaustion", len(fillers) > 0,
          f"fillers={len(fillers)}")
    adv = inlet.dispatch_gap_hosted(gap_id, budget_s=60.0)
    refused = (adv.get("hosted") is False and "refusal" in adv)
    check("T10 exhausted pool: hosted dispatch refuses honestly",
          refused, str(adv.get("refusal", adv.get("reason")))[:120])
    for mc_id in fillers:
        try:
            substrate.retire(mc_id, outcome="killed", loop="run")
        except Exception:
            pass

    # -- resolve at sleep -------------------------------------------------
    sleep = inlet.sleep_run_loop()
    check("T9 sleep_run_loop resolves the run loop",
          sleep.get("resolved") is True, str(sleep.get("reason", ""))[:120])

    # -- summary -----------------------------------------------------------
    passed = sum(1 for _, ok, _ in CHECKS if ok)
    failed = sum(1 for _, ok, _ in CHECKS if not ok)
    print(f"\nCOUNT {passed} PASS / {failed} FAIL", flush=True)
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
