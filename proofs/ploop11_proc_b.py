"""PLOOP-11 kill-resume proof, process B: fresh-process resume.

Reads <result.json> from process A, rebuilds the real live path on the
SAME db files in a FRESH process, and resumes the handoff through the
production resume() path: load checkpoint on a fresh connection,
integrity-check, honestly re-fetch the gap record, accept (verified
against the live world), enter the acquisition loop, consume the
checkpoint. Writes the verdict to <verdict.json>. Any failure exits
nonzero.
"""
import json
import os
import sys

WT = os.environ.get(
    "PLOOP11_WT", os.path.expanduser("~/workspace/remor_convergence/"
                                     "worktrees/ploop11"))
sys.path.insert(0, os.path.join(WT, "pylib"))


def main():
    workdir, result_path, verdict_path = sys.argv[1], sys.argv[2], sys.argv[3]
    with open(result_path) as f:
        identity = json.load(f)

    from swarm_engine.core.executive.live_path import (
        build_live_path, LivePathConfig)

    lp = build_live_path(LivePathConfig(workdir=workdir))
    verdict = lp.resume(identity["handoff_id"])

    if not verdict.get("entered"):
        raise SystemExit("PROC-B FAILED: receiving loop not entered")
    if verdict.get("to_loop") != "acquisition":
        raise SystemExit(
            f"PROC-B FAILED: wrong loop: {verdict.get('to_loop')}")
    if not verdict.get("verified_checkpoint"):
        raise SystemExit("PROC-B FAILED: checkpoint not verified at accept")
    if not verdict.get("checkpoint_consumed"):
        raise SystemExit("PROC-B FAILED: checkpoint not consumed")
    # Lineage: the resumed handoff's triggering boundary is the wake
    # boundary process A presented; the checkpoint row carries it.
    row = lp.store.load(identity["handoff_id"])
    if row["triggering_boundary_id"] != identity["triggering_boundary_id"]:
        raise SystemExit("PROC-B FAILED: lineage broken across processes")
    if row["status"] != "consumed":
        raise SystemExit(
            f"PROC-B FAILED: expected 'consumed', got {row['status']!r}")

    with open(verdict_path, "w") as f:
        json.dump(verdict, f, indent=1, default=str)
    print(f"PROC-B OK entered={verdict['to_loop']} "
          f"consumed={verdict['checkpoint_consumed']}", flush=True)
    lp.engine.close()


main()
