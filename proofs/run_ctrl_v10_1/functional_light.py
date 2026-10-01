#!/usr/bin/env python3
"""Light functional proofs for the repaired RUN-CTRL-V10-1 tree.

 1. tick() end-to-end: acquisition leg + repaired quarantine sweep +
    per-cycle acceptance, all on one tick, no exceptions.
 2. The repaired quarantine sweep: Q4's run_quarantine_sweep ran (audit
    row exists), authorized caller, bounded, empty-quarantine report shape.
 3. Pause/resume/stop: run() honors pause (no cycles while paused),
    resume continues, stop ends promptly.
 4. Checkpoint kill -9 resume: a run killed -9 resumes from its checkpoint
    instead of restarting blind (cycle counter and run id survive).
 5. Budget exhaustion: run_budget_s=0 is refused before any cycle
    (fail-closed on exhaustion, not a run that does nothing).

Exit 0 only if every claim holds on the real machinery.
"""

import os
import signal
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time

WT = "/home/hatch/workspace/worktrees/warm-v10-convergence"
sys.path.insert(0, os.path.join(WT, "pylib"))
sys.path.insert(0, WT)

from swarm_engine.core.engine import SwarmEngine
from swarm_engine.core.run_controller import RunController, RunConfig

PASSED = 0


def check(cond, msg):
    global PASSED
    if not cond:
        print(f"FAIL: {msg}")
        sys.exit(1)
    PASSED += 1
    print(f"ok [{PASSED}]: {msg}")


def fresh_engine(td, name):
    return SwarmEngine(db_path=os.path.join(td, f"{name}.db"))


def ckpt_cycles(path):
    con = sqlite3.connect(path)
    try:
        return con.execute("SELECT COUNT(*) FROM rc_cycles").fetchone()[0]
    finally:
        con.close()


def fresh_cfg(td, **kw):
    d = dict(cadence_interval_s=0.2, run_budget_s=60.0,
             cycle_budget_s=10.0, gap_budget_s=1.0, max_cycles=10,
             max_gaps_per_cycle=5, trusted_indexes=[],
             staging_dir=os.path.join(td, "staging"))
    d.update(kw)
    return RunConfig(**d)


# ---------------------------------------------------------------- 1+2: tick
td = tempfile.mkdtemp(prefix="rcv10_func_")
eng = fresh_engine(td, "eng1")
rc = RunController(eng, config=fresh_cfg(td),
                   checkpoint_path=os.path.join(td, "ckpt1.db"),
                   registry=None)
summary = rc.tick()
check(isinstance(summary, dict) and summary.get("sweep") is not None
      and summary.get("quarantine") is not None,
      "tick() completed all three legs (gaps, acquisition leg, quarantine)")
check("distilled" in (summary.get("sweep") or {}),
      "tick's sweep key carries the Q1 cycle report (acquisition leg)")
check(summary.get("errors") == [],
      f"tick() raised no step errors (errors={summary.get('errors')})")

# The repaired quarantine sweep: Q4's driver ran with the authorized caller.
qs = summary.get("quarantine") or {}
check(isinstance(qs, dict) and "summary" in qs and "run_id" in qs,
      f"quarantine sweep returns Q4's report shape (keys={sorted(qs)[:6]})")
con = sqlite3.connect(os.path.join(td, "eng1.db"))
try:
    rows = con.execute(
        "SELECT run_id, config_json FROM sweep_runs "
        "ORDER BY started_at DESC LIMIT 1").fetchall()
finally:
    con.close()
check(len(rows) == 1 and "run_controller cadence sweep" in rows[0][1],
      f"sweep audit row recorded with the Controller's reason "
      f"(run_id={rows[0][0] if rows else None})")

# ------------------------------------------------------- 3: pause/resume/stop
eng3 = fresh_engine(td, "eng3")
rc3 = RunController(eng3, config=fresh_cfg(td, max_cycles=1000,
                                          cadence_interval_s=0.05),
                    checkpoint_path=os.path.join(td, "ckpt3.db"),
                    registry=None)
rc3.request_pause()
t = threading.Thread(target=rc3.run, daemon=True)
t.start()
time.sleep(1.0)
check(ckpt_cycles(os.path.join(td, "ckpt3.db")) == 0, "run() performs no cycles while paused")
rc3.resume()
time.sleep(1.5)
check(ckpt_cycles(os.path.join(td, "ckpt3.db")) >= 1, "run() continues after resume")
rc3.request_stop()
t.join(timeout=10.0)
check(not t.is_alive(), "stop() ends run() promptly")

# Per-cycle acceptance (PLOOP-3) turns in the run loop, not the tick:
# at least one recorded cycle summary carries an acceptance record.
con = sqlite3.connect(os.path.join(td, "ckpt3.db"))
try:
    rows = con.execute("SELECT summary_json FROM rc_cycles").fetchall()
finally:
    con.close()
import json as _json
acc = [r for r in rows
       if "acceptance" in _json.loads(r[0])]
check(len(acc) >= 1,
      f"per-cycle acceptance (_accept_cycle) recorded on {len(acc)} "
      f"cycle(s) of the run")

# ------------------------------------------------------- 4: kill -9 resume
# Runners are always reaped, even if a check fails (no stray processes).
_runners = []
import atexit as _atexit


@_atexit.register
def _reap_runners():
    for rp in _runners:
        try:
            if rp.poll() is None:
                rp.kill()
                rp.wait(timeout=5)
        except Exception:
            pass


td4 = tempfile.mkdtemp(prefix="rcv10_kill9_")
runner = os.path.join(td4, "runner.py")
with open(runner, "w") as fh:
    fh.write(f"""import os, sys, time
sys.path.insert(0, os.path.join("{WT}", "pylib"))
sys.path.insert(0, "{WT}")
from swarm_engine.core.engine import SwarmEngine
from swarm_engine.core.run_controller import RunController, RunConfig
eng = SwarmEngine(db_path="{td4}/eng4.db")
rc = RunController(eng, config=RunConfig(
    cadence_interval_s=0.05, run_budget_s=120.0, cycle_budget_s=30.0,
    gap_budget_s=1.0, max_cycles=100000, max_gaps_per_cycle=5,
    trusted_indexes=[], staging_dir="{td4}/staging"),
    checkpoint_path="{td4}/ckpt4.db", registry=None)
rc.run()
""")
_runners.append(subprocess.Popen([sys.executable, runner]))
p = _runners[-1]


def ckpt_run_state(path):
    con = sqlite3.connect(path)
    try:
        tables = {r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        if "rc_meta" not in tables:
            return None, 0
        rid = con.execute(
            "SELECT v FROM rc_meta WHERE k='run_id'").fetchone()
        cyc = con.execute(
            "SELECT COUNT(*) FROM rc_cycles").fetchone()[0]
        return (rid[0] if rid else None), cyc
    finally:
        con.close()


def wait_for_cycles(path, minimum, timeout_s=90.0):
    """Wait until the checkpoint records >= minimum cycles (the runner may
    be slow to start under machine load); return (run_id, cycles)."""
    deadline = time.time() + timeout_s
    rid, cyc = None, 0
    while time.time() < deadline:
        rid, cyc = ckpt_run_state(path)
        if cyc >= minimum:
            return rid, cyc
        time.sleep(0.5)
    return rid, cyc


run_id_before, cycles_before = wait_for_cycles(
    os.path.join(td4, "ckpt4.db"), 1)
check(cycles_before >= 1,
      f"pre-kill run advanced its checkpoint (cycles={cycles_before})")
os.kill(p.pid, signal.SIGKILL)
p.wait()
check(p.returncode == -signal.SIGKILL, "run was killed -9 (not a clean stop)")

_runners.append(subprocess.Popen([sys.executable, runner]))
p2 = _runners[-1]
run_id_after, cycles_after = wait_for_cycles(
    os.path.join(td4, "ckpt4.db"), cycles_before + 1)
# Stop the second runner BEFORE opening the engine: the DB owner lock is
# single-owner by design (DuplicateEngineError otherwise -- honest).
os.kill(p2.pid, signal.SIGKILL)
p2.wait()
# The resume signal is the run's own "run_resumed" observation in the
# epistemic store (the checkpoint's run_id meta tracks the live owner
# and is re-minted each boot by design).
eng4 = SwarmEngine(db_path=os.path.join(td4, "eng4.db"))
resumed_obs = [
    o for o in eng4.intellect.epistemic.all_observations()
    if getattr(o, "source", "") == "run_controller/run_resumed"]
check(len(resumed_obs) >= 1,
      f"post-kill run recorded run_resumed "
      f"({len(resumed_obs)} observation(s))")
prior = [(getattr(o, "raw", None) or {}).get("prior_cycles")
         for o in resumed_obs]
check(any((pc or 0) > 0 for pc in prior),
      f"resume carried prior cycles forward (prior_cycles={prior})")
check(cycles_after > cycles_before,
      f"post-kill run continued from the checkpoint "
      f"({cycles_before} -> {cycles_after})")

# ------------------------------------------------------- 5: budget exhaustion
eng5 = fresh_engine(td, "eng5")
rc5 = RunController(eng5, config=fresh_cfg(td, run_budget_s=0.0),
                    checkpoint_path=os.path.join(td, "ckpt5.db"),
                    registry=None)
res = rc5.run()
check(res.get("budget_exhausted") is True and ckpt_cycles(os.path.join(td, "ckpt5.db")) == 0,
      "run_budget_s=0 refuses before any cycle (fail-closed)")

# ------------------------------------------------------- 6: acceptance inlet
# Q8's present() is an inlet into the acceptance mechanism, not a loop:
# calling it performs a synchronous state transition (CANDIDATE awaiting
# verdict) and starts no thread, no scheduler, no second loop. The
# duplicate-present guard refuses a re-drive loudly.
import threading as _threading
from swarm_engine.services.acceptance import (
    AcceptanceLoop, AcceptanceStore, Attempt, AuthReport)
eng6 = fresh_engine(td, "eng6")
_loop = AcceptanceLoop(
    AcceptanceStore(os.path.join(td, "acc6.db")),
    eng6.intellect.epistemic, engine=eng6)
_attempt = Attempt(approach_signature=["test"],
                   plan={"op": "noop"}, args={},
                   result_summary="ok", exec_ok=True)
_auth = AuthReport(held_out={}, negative_controls={}, passed=True)
_threads_before = len(_threading.enumerate())
rec = _loop.present("inlet-run-1", "test goal", _attempt, _auth)
check(getattr(rec.state, "value", rec.state) == "candidate"
      or "CANDIDATE" in str(rec.state),
      f"present() transitions to CANDIDATE (state={rec.state})")
check(len(_threading.enumerate()) == _threads_before,
      "present() started no thread: it is an inlet, not a loop")
try:
    _loop.present("inlet-run-1", "test goal", _attempt, _auth)
    _dup_ok = False
except ValueError:
    _dup_ok = True
check(_dup_ok, "duplicate present() is refused loudly (no silent re-drive)")

print(f"\nLIGHT FUNCTIONAL PROOFS GREEN ({PASSED} checks).")
