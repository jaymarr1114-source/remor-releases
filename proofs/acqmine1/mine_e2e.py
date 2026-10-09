#!/usr/bin/env python3
"""ACQ-MINE-1 gate battery: mine -> gap -> acquire-close, fresh processes.

Proves the experience miner end-to-end on real machinery:
  1. A real charter-9 delta is recorded through the REAL emit_delta path
     (validated, written via record_experience) -- technique: integer tripling,
     demonstrated with real computed I/O pairs.
  2. ExperienceMiner.scan() finds exactly it (and nothing else).
  3. miner.emit_gap() registers a well-formed GapRecord through the registry's
     real inlet (registered_by="experience-miner", evidence cites the delta).
  4. registry.dispatch() closes it through the technique route (real
     DistillationLoop distills tripling from the mined I/O pairs).
  5. Re-mining yields zero new gaps (already-mined dedup).
  6. Negative control: a fresh gapless substrate yields zero gaps.

Exit 0 only if every check passes. Nothing is faked: the delta is a real
validated record, the I/O pairs are real computations, the distillation is
the real M2 loop, the close is the registry's real _close path.
"""
import json
import os
import shutil
import sys
import tempfile

WT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# WT/proofs/acqmine1 -> WT
WT = os.path.dirname(WT)
sys.path.insert(0, WT + "/pylib")
sys.path.insert(0, WT)
os.environ["REMOR_TEST_MODE"] = "1"

WORK = tempfile.mkdtemp(prefix="acqmine1_")
ENG_DB = os.path.join(WORK, "eng.db")
EPI_DB = os.path.join(WORK, "epi.db")
GAPS_DB = os.path.join(WORK, "gaps.db")

CHECKS = []


def check(name, cond, detail=""):
    CHECKS.append((name, bool(cond), detail))
    print(("PASS " if cond else "FAIL ") + name + (f" -- {detail}" if detail and not cond else ""))


def main():
    from runtime.core.engine import SwarmEngine
    from runtime.intellect.epistemic import EpistemicStore
    from runtime.intellect.delta_capture import emit_delta
    from runtime.acquisition.experience_miner import (
        ExperienceMiner, REGISTERED_BY)
    from swarm_engine.acquisition.gaps import GapRegistry

    # -- substrate -----------------------------------------------------
    epistemic = EpistemicStore(db_path=EPI_DB)
    engine = SwarmEngine(db_path=ENG_DB, _test_allow_shared=True)
    registry = GapRegistry(engine, db_path=GAPS_DB)

    # -- 1. record a REAL charter-9 delta through the real emit path ----
    # Technique: integer tripling. The I/O pairs are real computations
    # (3*x for real x), recorded as run artifacts with io_pairs.
    pairs = [(2, 6), (3, 9), (4, 12), (5, 15), (7, 21), (8, 24), (11, 33)]
    artifacts = []
    for x, y in pairs:
        assert 3 * x == y  # the demonstration is real, not asserted
        artifacts.append({
            "kind": "run",
            "cmd": ["triple", str(x)],
            "output_includes": [str(y)],
            "io_pairs": [{"input": {"x": x}, "output": y}],
        })
    delta = {
        "objective_x": "triple an integer presented as x",
        "external_demo_y": (
            "the demonstrator tripled integers: for each presented x it "
            "returned 3*x, showing the multiplication explicitly across "
            "seven verified cases"),
        "native_inventory_z": (
            "REMOR's admitted inventory holds no tripling capability; "
            "no primitive computes 3*x as a named operation"),
        "capability_gap": (
            "before this session, presenting x yielded no tripled result "
            "from any admitted capability; after the demonstration, the "
            "technique 3*x is evidenced but still unowned"),
        "technique_t": {
            "name": "tripling",
            "probe": {"import": "operator", "attr": "mul",
                      "check": "operator.mul(3, 4) == 12"},
        },
        "evidence_e": artifacts,
        "dependencies_d": [],
        "verification_v": (
            "all seven outputs recomputed independently as 3*x; "
            "every pair verified before recording"),
        "resulting_capability_c": "triple_integer",
    }
    obs_id = emit_delta(epistemic, session_id="acqmine1-proof",
                        delta=delta, causal_chain=["acqmine1-proof"])
    check("delta recorded through real emit_delta path", bool(obs_id), obs_id)

    # -- 2. mine --------------------------------------------------------
    miner = ExperienceMiner(epistemic, engine.acquired_code)
    mined = miner.scan()
    check("scan finds exactly the seeded delta", len(mined) == 1,
          f"found {len(mined)}")
    if mined:
        check("mined gap cites the real observation",
              mined[0].observation_id == obs_id, mined[0].observation_id)
        check("mined gap carries >=4 I/O pairs",
              len(mined[0].io_pairs) >= 4, str(len(mined[0].io_pairs)))

    # -- 3. emit through the real registry inlet ------------------------
    rec = miner.emit_gap(registry, mined[0])
    check("gap registered with id", bool(rec.gap_id), rec.gap_id)
    check("registered_by names the miner",
          rec.registered_by == REGISTERED_BY, rec.registered_by)
    check("technique block present with objective+delta",
          bool(rec.technique and rec.technique.objective
               and rec.technique.delta),
          str(bool(rec.technique)))
    cited = [ev.get("detail", {}).get("delta_observation_id")
             for ev in rec.evidence if isinstance(ev, dict)]
    check("evidence cites the source delta", obs_id in cited, str(cited))

    # -- 4. dispatch closes through the real acquire path ---------------
    result = registry.dispatch(rec.gap_id)
    check("dispatch outcome is closed", result.outcome == "closed",
          f"{result.outcome}: {result.detail[:200]}")
    check("route was the technique route", result.route_name == "technique",
          result.route_name)

    # -- 5. re-mining yields zero new gaps -------------------------------
    again = miner.mine_and_emit(registry)
    check("re-mine emits zero duplicates", len(again) == 0,
          f"emitted {len(again)}")

    # -- 6. negative control: gapless substrate -> zero gaps -------------
    epi2 = EpistemicStore(db_path=os.path.join(WORK, "epi2.db"))
    miner2 = ExperienceMiner(epi2, engine.acquired_code)
    check("gapless substrate mines zero gaps",
          len(miner2.scan()) == 0,
          f"found {len(miner2.scan())}")

    # -- verdict ----------------------------------------------------------
    failed = [n for n, ok, _ in CHECKS if not ok]
    print(f"\n{len(CHECKS) - len(failed)}/{len(CHECKS)} checks passed")
    shutil.rmtree(WORK, ignore_errors=True)
    sys.exit(0 if not failed else 1)


if __name__ == "__main__":
    main()
