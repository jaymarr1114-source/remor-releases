"""T2 child: run the REAL RunController until max_cycles, then exit.

Usage: cop_child_run.py <workdir> <ckpt_path> --max-cycles N --cadence S
         --run-budget S --report-out PATH [--tag NAME]

Builds the real engine/epistemic/gap-registry stack (same as t1), seeds
gaps idempotently, constructs the REAL RunController on the given
checkpoint path, and calls run(). Writes the run report as JSON to
--report-out. Prints READY once constructed (before run starts) so the
parent knows the checkpoint file is claimed.
"""
import argparse
import json
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
from cop_common import make_engine, seed_gaps, make_controller  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("workdir")
    ap.add_argument("ckpt_path")
    ap.add_argument("--max-cycles", type=int, default=200)
    ap.add_argument("--cadence", type=float, default=0.25)
    ap.add_argument("--run-budget", type=float, default=120.0)
    ap.add_argument("--report-out", required=True)
    ap.add_argument("--tag", default="eng")
    a = ap.parse_args()

    engine, _epistemic = make_engine(a.workdir, tag=a.tag)
    seed_gaps(engine, a.workdir)
    rc = make_controller(engine, a.ckpt_path,
                         cadence_interval_s=a.cadence,
                         run_budget_s=a.run_budget,
                         cycle_budget_s=3.0, max_cycles=a.max_cycles)
    print(f"READY run_id={rc.run_id}", flush=True)
    report = rc.run()
    with open(a.report_out, "w") as fh:
        json.dump({"run_id": rc.run_id,
                   "cycles": len(report["cycles"]),
                   "resumed": report["resumed"],
                   "budget_exhausted": report["budget_exhausted"],
                   "stopped": report["stopped"],
                   "errors": report["errors"]}, fh)
    print(f"DONE cycles={len(report['cycles'])} resumed={report['resumed']}",
          flush=True)


if __name__ == "__main__":
    main()
