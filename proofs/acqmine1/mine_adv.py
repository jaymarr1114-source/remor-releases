#!/usr/bin/env python3
"""ACQ-MINE-1 adversarial battery: the miner must refuse to mine garbage.

  1. Invalid charter-9 delta (missing fields) -> skipped, never mined.
  2. Delta whose resulting capability IS admitted -> skipped (no gap).
  3. Delta with no io_pairs in evidence -> skipped (not acquirable).
  4. Direct GapRegistry.register with empty evidence -> GapRefused (the
     inlet itself fails closed; the miner never bypasses it).
  5. emit_gap on an already-mined delta -> ValueError (no duplicates).

Exit 0 only if every check passes.
"""
import os
import shutil
import sys
import tempfile

WT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WT = os.path.dirname(WT)
sys.path.insert(0, WT + "/pylib")
sys.path.insert(0, WT)
os.environ["REMOR_TEST_MODE"] = "1"

WORK = tempfile.mkdtemp(prefix="acqmine1_adv_")
CHECKS = []


def check(name, cond, detail=""):
    CHECKS.append((name, bool(cond), detail))
    print(("PASS " if cond else "FAIL ") + name + (f" -- {detail}" if detail and not cond else ""))


def base_delta(**over):
    pairs = [(2, 6), (3, 9), (4, 12), (5, 15)]
    arts = [{"kind": "run", "cmd": ["t", str(x)], "output_includes": [str(y)],
             "io_pairs": [{"input": {"x": x}, "output": y}]}
            for x, y in pairs]
    d = {
        "objective_x": "triple an integer presented as x",
        "external_demo_y": "demonstrator tripled integers across four cases",
        "native_inventory_z": "no tripling capability in admitted inventory",
        "capability_gap": ("before, no admitted capability tripled; after the "
                           "demonstration the technique is evidenced"),
        "technique_t": {"name": "tripling",
                        "probe": {"import": "operator", "attr": "mul",
                                  "check": "x"}},
        "evidence_e": arts,
        "dependencies_d": [],
        "verification_v": "all four outputs recomputed as 3*x before recording",
        "resulting_capability_c": "triple_integer_adv",
    }
    d.update(over)
    return d


def main():
    from runtime.core.engine import SwarmEngine
    from runtime.intellect.epistemic import EpistemicStore
    from runtime.intellect.delta_capture import emit_delta, DeltaRefused
    from runtime.acquisition.experience_miner import ExperienceMiner
    from swarm_engine.acquisition.gaps import (
        GapRegistry, GapRecord, GapRefused as RegistryRefused)

    epistemic = EpistemicStore(db_path=os.path.join(WORK, "epi.db"))
    engine = SwarmEngine(db_path=os.path.join(WORK, "eng.db"),
                         _test_allow_shared=True)
    registry = GapRegistry(engine, db_path=os.path.join(WORK, "gaps.db"))
    miner = ExperienceMiner(epistemic, engine.acquired_code)

    # 1. invalid delta never reaches the substrate (write-time refusal)
    try:
        emit_delta(epistemic, session_id="adv",
                   delta=base_delta(objective_x="x"))  # too short
        check("invalid delta refused at write time", False, "no refusal raised")
    except DeltaRefused:
        check("invalid delta refused at write time", True)

    # 2. valid delta, but capability already admitted -> skipped
    from runtime.governance.provenance import AcquiredCodeStore
    engine.acquired_code.save(name="triple_integer_adv",
                              capability_id="cap_adv_1", code="def run(x): return 3*x",
                              entrypoint="run", source="test",
                              effects=[], spec={}, evidence={})
    oid2 = emit_delta(epistemic, session_id="adv", delta=base_delta())
    mined_ids = [m.observation_id for m in miner.scan()]
    check("admitted capability is not mined", oid2 not in mined_ids,
          f"mined={mined_ids}")

    # 3. delta with no io_pairs -> skipped (not acquirable via technique route)
    d3 = base_delta(resulting_capability_c="quad_integer_adv")
    for a in d3["evidence_e"]:
        a.pop("io_pairs", None)
    oid3 = emit_delta(epistemic, session_id="adv", delta=d3)
    mined_ids = [m.observation_id for m in miner.scan()]
    check("delta without io_pairs is not mined", oid3 not in mined_ids,
          f"mined={mined_ids}")

    # 4. registry inlet fails closed on empty evidence (defense in depth)
    try:
        registry.register(GapRecord(summary="no evidence here",
                                    registered_by="attacker"))
        check("empty-evidence registration refused", False, "no refusal raised")
    except RegistryRefused:
        check("empty-evidence registration refused", True)

    # 5. duplicate emission refused
    d5 = base_delta(resulting_capability_c="quint_integer_adv")
    oid5 = emit_delta(epistemic, session_id="adv", delta=d5)
    m5 = [m for m in miner.scan() if m.observation_id == oid5][0]
    miner.emit_gap(registry, m5)
    try:
        miner.emit_gap(registry, m5)
        check("duplicate emission refused", False, "no refusal raised")
    except ValueError:
        check("duplicate emission refused", True)

    failed = [n for n, ok, _ in CHECKS if not ok]
    print(f"\n{len(CHECKS) - len(failed)}/{len(CHECKS)} checks passed")
    shutil.rmtree(WORK, ignore_errors=True)
    sys.exit(0 if not failed else 1)


if __name__ == "__main__":
    main()
