#!/usr/bin/env python3
"""ACQ-CTRL-1 gate battery (1/2): the controller drives mine -> acquire -> close.

Proves on real machinery, fresh processes:
  1. The controller runs the miner on its cadence (first cycle mines;
     second cycle does not re-mine; forced mine works).
  2. A mined gap is dispatched INSIDE a loop-rooted microcontroller
     (loop='acquisition') and closes through the real technique route
     (real M2 DistillationLoop) -- the controller orchestrates, the
     registry verifies.
  3. loop_view() is a LoopView: "Acquisition is active", no mc ids.
  4. The executive-facing status stays O(1).

Exit 0 only if every check passes.
"""
import os
import sys
import tempfile

WT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WT = os.path.dirname(WT)
sys.path.insert(0, WT + "/pylib")
sys.path.insert(0, WT)
os.environ["REMOR_TEST_MODE"] = "1"

WORK = tempfile.mkdtemp(prefix="acqctrl1_e2e_")
ENG_DB = os.path.join(WORK, "eng.db")
EPI_DB = os.path.join(WORK, "epi.db")
GAPS_DB = os.path.join(WORK, "gaps.db")

CHECKS = []


def check(name, cond, detail=""):
    CHECKS.append((name, bool(cond), detail))
    print(("PASS " if cond else "FAIL ") + name
          + (f" -- {detail}" if detail and not cond else ""))


def seed_delta(epistemic):
    """A REAL charter-9 delta (integer doubling) through the real path."""
    from runtime.intellect.delta_capture import emit_delta
    pairs = [(2, 4), (3, 6), (5, 10), (7, 14), (8, 16), (11, 22)]
    artifacts = []
    for x, y in pairs:
        assert 2 * x == y
        artifacts.append({
            "kind": "run",
            "cmd": ["double", str(x)],
            "output_includes": [str(y)],
            "io_pairs": [{"input": {"x": x}, "output": y}],
        })
    delta = {
        "objective_x": "double an integer presented as x",
        "external_demo_y": ("the demonstrator doubled integers: for each "
                            "presented x it returned 2*x across six cases"),
        "native_inventory_z": ("REMOR's admitted inventory holds no doubling "
                               "capability as a named operation"),
        "capability_gap": ("before this session, presenting x yielded no "
                           "doubled result; after the demonstration, the "
                           "technique 2*x is evidenced but still unowned"),
        "technique_t": {
            "name": "doubling",
            "probe": {"import": "operator", "attr": "mul",
                     "check": "operator.mul(2, 5) == 10"},
        },
        "evidence_e": artifacts,
        "dependencies_d": [],
        "verification_v": ("all six outputs recomputed as 2*x before "
                           "recording"),
        "resulting_capability_c": "double_integer",
    }
    return emit_delta(epistemic, session_id="acqctrl1-proof",
                      delta=delta, causal_chain=["acqctrl1-proof"])


def main():
    from runtime.core.engine import SwarmEngine
    from runtime.intellect.epistemic import EpistemicStore
    from runtime.acquisition.experience_miner import ExperienceMiner
    from runtime.acquisition.controller import AcquisitionController
    from swarm_engine.acquisition.gaps import GapRegistry, STATUS_CLOSED
    from swarm_engine.core.microcontroller import (
        MicrocontrollerSubstrate, LoopView)

    epistemic = EpistemicStore(db_path=EPI_DB)
    engine = SwarmEngine(db_path=ENG_DB, _test_allow_shared=True)
    registry = GapRegistry(engine, db_path=GAPS_DB)
    substrate = MicrocontrollerSubstrate()
    miner = ExperienceMiner(epistemic, engine.acquired_code)
    # Short cadence so the proof observes due/not-due without sleeping.
    ctl = AcquisitionController(
        registry=registry, substrate=substrate, miner=miner,
        mine_interval_s=3600.0)

    obs_id = seed_delta(epistemic)
    check("delta recorded through real emit_delta path", bool(obs_id), obs_id)

    # -- 1. miner cadence ----------------------------------------------
    r1 = ctl.run_cycle(budget_s=120.0, max_gaps=5)
    check("first cycle mined (cadence due)", r1["mined"].get("mined") is True,
          str(r1["mined"]))
    check("first cycle surfaced exactly one gap",
          len(r1["mined"].get("new_gaps", [])) == 1,
          str(r1["mined"].get("new_gaps")))
    gap_id = (r1["mined"]["new_gaps"] or [None])[0]

    r2 = ctl.run_cycle(budget_s=120.0, max_gaps=5, mine=True)
    check("second cycle did not re-mine (cadence not due)",
          r2["mined"].get("mined") is not True, str(r2["mined"]))

    forced = ctl.run_miner(force=True)
    check("forced mine runs regardless of cadence",
          forced.get("mined") is True, str(forced))
    check("forced re-mine emits zero duplicates",
          forced.get("new_gaps") == [], str(forced.get("new_gaps")))

    # -- 2. controller-driven close --------------------------------------
    check("gap was worked by the controller",
          any(g["gap_id"] == gap_id for g in r1["gaps"]),
          str([g["gap_id"] for g in r1["gaps"]]))
    worked = next(g for g in r1["gaps"] if g["gap_id"] == gap_id)
    check("gap acquired inside a microcontroller",
          worked.get("spawned") is True, str(worked))
    check("dispatch closed the gap", worked.get("outcome") == "closed",
          f"{worked.get('outcome')}: {worked.get('detail', '')[:150]}")
    check("route was the technique route",
          worked.get("route") == "technique", worked.get("route"))
    rec = registry.get(gap_id)
    check("registry shows closed", rec.status == STATUS_CLOSED, rec.status)
    check("microcontroller retired",
          worked.get("mc_outcome") == "resolved",
          str(worked.get("mc_outcome")))

    # -- 3. LoopView ------------------------------------------------------
    view = ctl.loop_view()
    check("loop_view is a LoopView", isinstance(view, LoopView),
          type(view).__name__)
    check("loop is acquisition", view.loop == "acquisition", view.loop)
    check("no mc ids leak into the view",
          "mc_id" not in view.as_dict()
          and "microcontroller" not in str(view.as_dict()).lower(),
          str(view.as_dict()))
    st = ctl.loop_status()
    check("status is O(1)", set(st) <= {"loop", "state", "cycles"}, str(st))

    # -- 4. closed gaps are never reworked --------------------------------
    r3 = ctl.run_cycle(budget_s=60.0, max_gaps=5)
    check("closed gap not redispatched",
          not any(g["gap_id"] == gap_id for g in r3["gaps"]),
          str([g["gap_id"] for g in r3["gaps"]]))

    failed = [n for n, ok, _ in CHECKS if not ok]
    print(f"\n{len(CHECKS) - len(failed)}/{len(CHECKS)} checks passed")
    sys.exit(0 if not failed else 1)


if __name__ == "__main__":
    main()
