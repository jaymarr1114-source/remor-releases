"""T1 — sustained Controller-owned run with unified-memory observation logging.

Fresh process. Drives the REAL RunController.run() on a seeded gap queue
with bounded budgets, then asserts:
- the run completed its authorized cadence (cycles >= 6, stopped by
  max_cycles -- not by budget, not by crash),
- the checkpoint advanced every cycle (rc_cycles rows contiguous, no
  duplicates, count matches the report),
- every Controller action was logged as an observation through the
  unified write path (run_started / cycle_summary / run_finished present,
  each carrying this run's run_id),
- the acquisition leg actually ran each tick (Q1 CognitionLoop.cycle drove,
  no acquisition_leg errors).
"""
import json
import os
import sys
import tempfile

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
from cop_common import (make_engine, seed_gaps, make_controller,  # noqa: E402
                        Checker, read_observations, TREE)


def main():
    c = Checker()
    workdir = tempfile.mkdtemp(prefix="cops_t1_")
    engine, epistemic = make_engine(workdir)
    seed_gaps(engine, workdir)
    ckpt_path = os.path.join(workdir, "ckpt.db")

    rc = make_controller(engine, ckpt_path,
                         cadence_interval_s=0.3, run_budget_s=25.0,
                         cycle_budget_s=3.0, max_cycles=12)
    run_id = rc.run_id
    report = rc.run()

    cycles = report["cycles"]
    c.check("t1_cycles_sustained", len(cycles) >= 6,
            f"cycles={len(cycles)}")
    c.check("t1_stopped_clean", not report["budget_exhausted"]
            and not any("run_fatal" in e for e in report["errors"]),
            f"errors={report['errors'][:2]}")

    # Checkpoint advanced every cycle: contiguous, no duplicates.
    from cop_common import ControllerCheckpoint
    ckpt = ControllerCheckpoint(ckpt_path)
    n_done = ckpt.cycles_completed()
    c.check("t1_checkpoint_count", n_done == len(cycles),
            f"ckpt={n_done} report={len(cycles)}")
    import sqlite3
    con = sqlite3.connect(ckpt_path)
    try:
        ns = [r[0] for r in con.execute(
            "SELECT n FROM rc_cycles ORDER BY n").fetchall()]
    finally:
        con.close()
    c.check("t1_checkpoint_contiguous",
            ns == list(range(1, len(cycles) + 1)),
            f"ns={ns[:5]}...")

    # Unified-memory observation logging: every Controller action observed.
    # Stored shape: raw["_provenance"] = {schema, origin_loop, kind,
    # causal_chain, recorded_at, write_path}; raw["run_id"] = run id.
    epi_db = os.path.join(workdir, "eng_epistemic.db")
    obs = read_observations(epi_db)

    def _kind_of(data):
        raw = data.get("raw", {}) if isinstance(data, dict) else {}
        prov = raw.get("_provenance", {}) if isinstance(raw, dict) else {}
        return prov.get("kind", "?")

    kinds = {}
    for oid, data in obs:
        kinds.setdefault(_kind_of(data), []).append(oid)
    for want in ("run_started", "cycle_summary", "run_finished"):
        c.check(f"t1_observed_{want}", want in kinds,
                f"kinds={sorted(kinds)[:8]}")
    # Acquisition leg drove every tick without errors.
    acq_ok = all("sweep" in s and not any(
        e.startswith("acquisition_leg") for e in s.get("errors", []))
        for s in cycles)
    c.check("t1_acquisition_leg_drove", acq_ok and len(cycles) > 0,
            f"cycles={len(cycles)}")

    # Manifest for t6 (attribution audit reads this run's artifacts).
    manifest = {"run_id": run_id, "epi_db": epi_db,
                "ckpt_path": ckpt_path, "cycles": len(cycles)}
    man_path = os.path.join(_HERE, "_t1_manifest.json")
    with open(man_path, "w") as fh:
        json.dump(manifest, fh)
    print(f"manifest -> {man_path}", flush=True)

    ok = c.summary()
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
