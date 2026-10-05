"""T2 — kill-resume across GENUINE process death.

Fresh driver process. Spawns child A (REAL RunController.run() in its own
OS process), polls the REAL ControllerCheckpoint until >= 4 cycles are
recorded, then SIGKILLs child A (-9: no cleanup, no finally handlers --
genuine death). Spawns child B on the SAME checkpoint path in a new
process. Asserts:
- child B's report has resumed=True (the checkpoint's status=crashed /
  running + prior_cycles>0 resume path fired),
- cycle numbers are contiguous with no duplicates across the death
  (no work repeated, none lost),
- child B got a DIFFERENT run_id but the SAME persisted deadline_wall
  (a resumed run gets the remainder of the original budget, never a
  fresh one),
- the checkpoint status after B is a clean terminal state.
"""
import json
import os
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
from cop_common import Checker, ControllerCheckpoint  # noqa: E402


def cycles_done(ckpt_path):
    try:
        return ControllerCheckpoint(ckpt_path).cycles_completed()
    except Exception:
        return 0


def cycle_numbers(ckpt_path):
    con = sqlite3.connect(ckpt_path)
    try:
        return [r[0] for r in con.execute(
            "SELECT n FROM rc_cycles ORDER BY n").fetchall()]
    finally:
        con.close()


def meta(ckpt_path, key):
    try:
        return ControllerCheckpoint(ckpt_path).load_meta(key)
    except Exception:
        return None


def main():
    c = Checker()
    workdir = tempfile.mkdtemp(prefix="cops_t2_")
    ckpt = os.path.join(workdir, "ckpt.db")
    child = os.path.join(_HERE, "cop_child_run.py")
    rep_a = os.path.join(workdir, "rep_a.json")
    rep_b = os.path.join(workdir, "rep_b.json")

    # Child A: long run; we will murder it mid-flight.
    proc_a = subprocess.Popen(
        [sys.executable, child, workdir, ckpt,
         "--max-cycles", "200", "--cadence", "0.25",
         "--run-budget", "180",
         "--report-out", rep_a, "--tag", "engA"],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    try:
        # Wait for >= 4 checkpointed cycles (genuine progress, not a guess).
        deadline = time.time() + 90
        n_at_kill = 0
        while time.time() < deadline:
            n_at_kill = cycles_done(ckpt)
            if n_at_kill >= 4:
                break
            if proc_a.poll() is not None:
                break
            time.sleep(0.5)
        c.check("t2_child_a_progressed", n_at_kill >= 4,
                f"cycles_at_kill={n_at_kill}")
        run_id_a = meta(ckpt, "run_id")
        deadline_a = meta(ckpt, "deadline_wall")
        c.check("t2_child_a_had_run_id", bool(run_id_a),
                f"run_id_a={run_id_a}")

        # Genuine death: SIGKILL, no cleanup.
        proc_a.kill()  # SIGKILL
        proc_a.wait(timeout=10)
        c.check("t2_child_a_killed", proc_a.returncode == -signal.SIGKILL,
                f"rc={proc_a.returncode}")
        status_after_kill = meta(ckpt, "status")
        c.check("t2_checkpoint_says_running_or_crashed",
                status_after_kill in ("running", "crashed"),
                f"status={status_after_kill}")

        # Child B: fresh process, same checkpoint path.
        proc_b = subprocess.Popen(
            [sys.executable, child, workdir, ckpt,
             "--max-cycles", str(n_at_kill + 6), "--cadence", "0.25",
             "--run-budget", "180",
             "--report-out", rep_b, "--tag", "engB"],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        try:
            rc_b = proc_b.wait(timeout=120)
        except subprocess.TimeoutExpired:
            proc_b.kill()
            proc_b.wait()
            rc_b = "TIMEOUT"
        c.check("t2_child_b_exited_clean", rc_b == 0, f"rc={rc_b}")

        rep = {}
        if os.path.exists(rep_b):
            with open(rep_b) as fh:
                rep = json.load(fh)
        c.check("t2_resumed", rep.get("resumed") is True,
                f"resumed={rep.get('resumed')}")
        run_id_b = meta(ckpt, "run_id")
        c.check("t2_new_run_id", bool(run_id_b) and run_id_b != run_id_a,
                f"a={run_id_a} b={run_id_b}")
        deadline_b = meta(ckpt, "deadline_wall")
        c.check("t2_deadline_not_refreshed", deadline_b == deadline_a,
                f"a={deadline_a} b={deadline_b}")

        ns = cycle_numbers(ckpt)
        c.check("t2_no_duplicate_cycles", len(ns) == len(set(ns)),
                f"n={len(ns)}")
        c.check("t2_cycles_contiguous",
                ns == list(range(1, len(ns) + 1)),
                f"last={ns[-3:] if ns else []}")
        c.check("t2_progress_continued", len(ns) >= n_at_kill + 1,
                f"at_kill={n_at_kill} final={len(ns)}")
        final_status = meta(ckpt, "status")
        c.check("t2_final_status_clean",
                final_status in ("complete", "stopped"),
                f"status={final_status}")
    finally:
        try:
            proc_a.kill()
        except Exception:
            pass

    ok = c.summary()
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
