"""PLOOP-11 kill-resume proof, process A: tick -> produce, then DIE.

Builds the real live path on db files under <workdir>, registers a real
open gap, drives one tick through the executive, produces a handoff with
the checkpoint store engaged, writes the handoff's durable identity to
<result.json>, and EXITS WITHOUT ACCEPTING -- the process dies
mid-handoff. Process B resumes from these files in a fresh process.
"""
import json
import os
import sys

WT = os.environ.get(
    "PLOOP11_WT", os.path.expanduser("~/workspace/remor_convergence/"
                                     "worktrees/ploop11"))
sys.path.insert(0, os.path.join(WT, "pylib"))


def main():
    workdir, result_path = sys.argv[1], sys.argv[2]
    os.makedirs(workdir, exist_ok=True)

    from swarm_engine.core.executive.live_path import (
        build_live_path, LivePathConfig)

    lp = build_live_path(LivePathConfig(workdir=workdir))
    gap = lp.registry.register_dependency_gap(
        "nonexistent_pkg_xyz", "package", "ploop-11 kill-resume proof",
        evidence=[{"kind": "observation", "observed": True,
                   "detail": "real probe: importlib.util.find_spec("
                             "'nonexistent_pkg_xyz') is None"}],
        registered_by="ploop-11-proc-a")

    identity, outcome, handoff = lp.produce_for_resume()
    identity["gap_id"] = gap.gap_id
    with open(result_path, "w") as f:
        json.dump(identity, f)
    print(f"PROC-A OK handoff={identity['handoff_id']} "
          f"checkpoint={identity['checkpoint_id']} "
          f"gap={gap.gap_id}", flush=True)
    # Die mid-handoff: no accept, no enter, no consume. The checkpoint
    # row is the only thing that survives.
    lp.engine.close()


main()
