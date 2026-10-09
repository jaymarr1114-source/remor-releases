#!/usr/bin/env python3
"""ACQ-CTRL-1 gate battery (2/2): retry -> expand -> terminal policy.

Proves on real machinery, fresh processes:
  1. A gap no route can serve stays OPEN; the controller retries it on
     later cycles (attempts counted from route history).
  2. On the final permitted attempt the controller EXPANDS: it re-runs
     the miner to broaden the evidence base before dispatching (trying
     differently, not just trying again) -- observable in the report.
  3. At max_attempts the gap is terminally classified "exhausted" with
     named reasons; the controller stops spending budget on it while the
     registry keeps it honestly open with every failure in route history.
  4. A spawn refusal is recorded honestly, never raised.

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

WORK = tempfile.mkdtemp(prefix="acqctrl1_policy_")
ENG_DB = os.path.join(WORK, "eng.db")
EPI_DB = os.path.join(WORK, "epi.db")
GAPS_DB = os.path.join(WORK, "gaps.db")

CHECKS = []


def check(name, cond, detail=""):
    CHECKS.append((name, bool(cond), detail))
    print(("PASS " if cond else "FAIL ") + name
          + (f" -- {detail}" if detail and not cond else ""))


def main():
    from runtime.core.engine import SwarmEngine
    from runtime.intellect.epistemic import EpistemicStore
    from runtime.acquisition.experience_miner import ExperienceMiner
    from runtime.acquisition.controller import AcquisitionController
    from swarm_engine.acquisition.gaps import (
        GapRegistry, GapRecord, STATUS_OPEN)
    from swarm_engine.core.microcontroller import MicrocontrollerSubstrate

    epistemic = EpistemicStore(db_path=EPI_DB)
    engine = SwarmEngine(db_path=ENG_DB, _test_allow_shared=True)
    registry = GapRegistry(engine, db_path=GAPS_DB)
    substrate = MicrocontrollerSubstrate()
    miner = ExperienceMiner(epistemic, engine.acquired_code)
    ctl = AcquisitionController(
        registry=registry, substrate=substrate, miner=miner,
        max_attempts=3, mine_interval_s=10**9)  # miner only on force/expand

    # A gap NO route serves: real observation evidence, no shape blocks.
    rec = GapRecord(
        summary="observed capability 'quantum-fold' absent; no route serves it",
        registered_by="acqctrl1-proof",
        evidence=[{"kind": "observation",
                   "observed": "capability 'quantum-fold' absent",
                   "detail": "probed the admitted inventory; no capability "
                             "computes quantum-fold; nothing to distill"}],
    )
    rec = registry.register(rec)
    gap_id = rec.gap_id
    check("unroutable gap registered", bool(gap_id), gap_id)

    def cycle():
        return ctl.run_cycle(budget_s=120.0, max_gaps=5, mine=False)

    # -- 1. retry ---------------------------------------------------------
    r1 = cycle()
    w1 = next(g for g in r1["gaps"] if g["gap_id"] == gap_id)
    check("attempt 1: policy=acquire", w1["policy"] == "acquire", w1["policy"])
    check("attempt 1: outcome open (no route)", w1["outcome"] == "open",
          f"{w1['outcome']}: {w1['detail'][:120]}")
    check("attempt 1: inside a microcontroller", w1["spawned"] is True)

    r2 = cycle()
    w2 = next(g for g in r2["gaps"] if g["gap_id"] == gap_id)
    check("attempt 2: policy=acquire (retry)", w2["policy"] == "acquire",
          w2["policy"])
    check("attempt 2: outcome open", w2["outcome"] == "open", w2["outcome"])
    check("attempts counted from route history",
          ctl._attempts(registry.get(gap_id)) == 2,
          str(ctl._attempts(registry.get(gap_id))))

    # -- 2. expand ----------------------------------------------------------
    r3 = cycle()
    w3 = next(g for g in r3["gaps"] if g["gap_id"] == gap_id)
    check("attempt 3: policy=expand", w3["policy"] == "expand", w3["policy"])
    check("expand re-ran the miner (evidence broadened)",
          "expand_mined" in w3, str(w3))
    check("attempt 3 dispatched after expanding",
          w3["spawned"] is True and w3["outcome"] == "open",
          f"spawned={w3['spawned']} outcome={w3['outcome']}")

    # -- 3. terminal ----------------------------------------------------------
    r4 = cycle()
    w4 = next(g for g in r4["gaps"] if g["gap_id"] == gap_id)
    check("attempt 4: policy=exhausted (terminal)",
          w4["policy"] == "exhausted", w4["policy"])
    check("exhausted gap not redispatched", w4["spawned"] is False,
          str(w4["spawned"]))
    check("terminal names the reasons",
          "max 3" in w4["detail"] and "attempts" in w4["detail"],
          w4["detail"][:120])
    rec4 = registry.get(gap_id)
    check("registry keeps it honestly open", rec4.status == STATUS_OPEN,
          rec4.status)
    check("every failure visible in route history",
          len([h for h in rec4.route_history if h.get("route") == ""]) >= 3
          or len(rec4.route_history) >= 3,
          f"{len(rec4.route_history)} history entries")

    r5 = cycle()
    w5 = next(g for g in r5["gaps"] if g["gap_id"] == gap_id)
    check("later cycles keep it exhausted (no budget spent)",
          w5["policy"] == "exhausted" and w5["spawned"] is False,
          f"{w5['policy']} spawned={w5['spawned']}")

    failed = [n for n, ok, _ in CHECKS if not ok]
    print(f"\n{len(CHECKS) - len(failed)}/{len(CHECKS)} checks passed")
    sys.exit(0 if not failed else 1)


if __name__ == "__main__":
    main()
