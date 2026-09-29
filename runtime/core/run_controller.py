"""V10-P2 -- REMOR Core Run Controller.

James's architectural decision (2026-09-27, standing): the REMOR Core Run
Controller owns cadence and execution control.

    Not an external reasoning model.
    Not an individual agent.
    Not the GUI.
    Not the scheduler as an independent intelligence layer.

Ownership chain:

    REMOR Core Run Controller
            |
            |-- cadence / wake conditions
            |-- mission dispatch
            |-- pause / resume / stop
            |-- checkpoint / recovery
            |-- resource + concurrency admission
            |-- acceptance-loop invocation
            |-- developmental-stage transitions
                    |
                    v
              Agent / Capability
                    |
                    v
              Evidence / Result
                    |
                    v
              REMOR verification

The scheduler underneath is dumb execution infrastructure: it fires when
told, it decides nothing. Dependency direction (load-bearing):

    REMOR cognition/state
           |
    Run Controller
           |
    appropriate capability/agent
           |
    evidence
           |
    REMOR learns/adapts
           |
    Run Controller continues

NEVER scheduler -> external model -> cognition. This module never routes
cognition through an external reasoning model; every call below is to
internal machinery (engine, registry, loops, drivers).

What this file builds (only the missing ownership layer):
  - RunConfig: the AUTHORIZED cadence -- the budgets/schedule/trust
    anchors the run may act within. The run never acts outside it.
  - DumbScheduler: fires when told; decides nothing.
  - RunController: cadence/wake, mission dispatch (drives the loops),
    pause/resume/stop, checkpoint/recovery, resource + concurrency
    admission (budgets + backoff), acceptance-loop invocation hook
    (the inlet Q8's present() will call -- owned here, not by present())
    plus per-cycle acceptance evidence (PLOOP-3): every tick's summary is
    accepted against its persisted checkpoint row, so the ACCEPT stage
    turns every cycle instead of waiting for an operator,
    developmental-stage transition hooks (dormant until V10-P7).

What it reuses (called, never edited, never rebuilt):
  - run_distillation_sweep (V10-P4): the sweep; this file is the clock.
  - GapRegistry.list_gaps / dispatch (M7): the gap queue and its routes.
  - repair_all_quarantined / diagnose_quarantine (M5): the sweep + diagnosis.
  - SubstrateAcquisitionDriver.attempt (M3): the Q7 trigger -- M5's
    diagnosis fires M3's driver from a real failure, no operator.
  - record_experience (V10-P1): every Controller action is an observation.
  - parse_dependency_reason (M3): reads the diagnosis, asserts nothing.

Q1's CognitionLoop.cycle() is deliberately NOT driven here: it targets the
pre-V10 record generation (M1-schema technique_delta source observations);
the V10 generation (V10-P3 provenance-stamped deltas) is driven by V10-P4's
sweep, which this Controller clocks. Driving both would double-drive
distillation -- that is the duplication this mission refuses.

Anti-duplication contract (James, 2026-09-27): a second driver, a second
scheduler, or a parallel gap queue in this file is the defect.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional

from .microcontroller.substrate import LOOPS, LOOP_ACQUISITION
from .resource_arbitrator import ResourceArbitrator


#: Hosted cognition-cycle budget used for acquisition demand measurement.
#: Mirrors AcquisitionLoopInlet._cognition_cycle_budget_s
#: (runtime/core/executive/loops.py): the real per-cycle budget the
#: acquisition loop's microcontroller-spawning path consumes. One hosted
#: cycle per open gap is the honest upper bound the loop states; the
#: arbitrator decides what it actually gets.
_ACQUISITION_HOSTED_CYCLE_BUDGET_S = 600.0

ORIGIN_LOOP = "run_controller"

_CHECKPOINT_SCHEMA = """
CREATE TABLE IF NOT EXISTS rc_meta (k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE IF NOT EXISTS rc_processed_gaps (
    gap_id TEXT PRIMARY KEY, outcome TEXT, at REAL);
CREATE TABLE IF NOT EXISTS rc_gap_backoff (
    gap_id TEXT PRIMARY KEY, failures INTEGER, backoff_until REAL);
CREATE TABLE IF NOT EXISTS rc_cycles (
    n INTEGER PRIMARY KEY, at REAL, summary_json TEXT);
"""


# ---------------------------------------------------------------------------
# Authorized cadence
# ---------------------------------------------------------------------------

@dataclass
class RunConfig:
    """The authorized cadence. The run never acts outside these bounds."""
    cadence_interval_s: float = 300.0   # dumb-ticker wake interval
    run_budget_s: float = 3600.0        # total wall-clock for run()
    cycle_budget_s: float = 600.0       # cooperative budget per tick
    gap_budget_s: float = 120.0         # wall-clock guard per gap dispatch
    max_cycles: Optional[int] = None
    max_gaps_per_cycle: int = 25
    backoff_base_s: float = 60.0
    backoff_max_s: float = 1800.0
    max_q7_per_cycle: int = 5
    # Operator-supplied trust anchors for the Q7 governed channel, same
    # model as gaps.py _governed_install's gov config: nothing invented.
    # Each entry: {"name","base_url","artifacts":{aid:{"url","sha256",
    # "media_type"}}}
    trusted_indexes: List[Dict[str, Any]] = field(default_factory=list)
    staging_dir: str = ""

    def as_dict(self) -> Dict[str, Any]:
        return asdict(self)


# ---------------------------------------------------------------------------
# Dumb scheduler: fires when told, decides nothing
# ---------------------------------------------------------------------------

class DumbScheduler:
    """Execution infrastructure subordinate to the Run Controller.

    It sleeps until the next authorized wake and reports back. It does not
    check budgets, skip ticks, prioritize work, or decide anything at all --
    every decision lives in RunController.
    """

    def __init__(self, interval_s: float) -> None:
        self._interval = max(0.0, float(interval_s))

    def wait_until_next_tick(self, stop_event: threading.Event) -> bool:
        """Block until the next wake. False => stop was requested."""
        return not stop_event.wait(self._interval)


# ---------------------------------------------------------------------------
# Checkpoint: run-scoped, sqlite-backed
# ---------------------------------------------------------------------------

class ControllerCheckpoint:
    """Run state that survives a kill. A run that died mid-cadence resumes
    from here instead of restarting blind or losing state."""

    def __init__(self, path: str) -> None:
        self._path = path
        # Set when a corrupt checkpoint file was quarantined at open: the
        # boot survived a damaged checkpoint instead of crashing on it.
        self._quarantined_from: Optional[str] = None
        try:
            con = sqlite3.connect(self._path)
            try:
                con.executescript(_CHECKPOINT_SCHEMA)
                con.commit()
            finally:
                con.close()
        except sqlite3.DatabaseError:
            # Corrupt checkpoint file (a real Android failure mode: flash
            # corruption). Fail closed: quarantine the damaged file and
            # start fresh -- never crash the boot on a bad checkpoint.
            self.quarantine_and_reset()

    def quarantine_and_reset(self) -> Optional[str]:
        """Quarantine the current checkpoint file and start a fresh one.

        The damaged file is renamed to ``<path>.corrupt-<epoch>`` (kept for
        forensics, never read again); a new empty checkpoint DB takes its
        place. Returns the quarantine path, or None if there was nothing
        to quarantine. Raises only if the fresh DB itself cannot be
        created (then the storage itself is broken -- an honest failure).
        """
        bad: Optional[str] = None
        try:
            stamp = int(time.time())
            bad = "%s.corrupt-%d" % (self._path, stamp)
            os.replace(self._path, bad)
            self._quarantined_from = bad
        except OSError:
            pass  # nothing to quarantine (or already moved); recreate below
        con = sqlite3.connect(self._path)
        try:
            con.executescript(_CHECKPOINT_SCHEMA)
            con.commit()
        finally:
            con.close()
        return bad

    def _conn(self) -> sqlite3.Connection:
        return sqlite3.connect(self._path)

    # -- meta -----------------------------------------------------------
    def save_meta(self, key: str, value: str) -> None:
        con = self._conn()
        try:
            con.execute("INSERT OR REPLACE INTO rc_meta (k, v) VALUES (?,?)",
                        (key, value))
            con.commit()
        finally:
            con.close()

    def load_meta(self, key: str) -> Optional[str]:
        con = self._conn()
        try:
            row = con.execute("SELECT v FROM rc_meta WHERE k=?",
                              (key,)).fetchone()
        finally:
            con.close()
        return row[0] if row else None

    # -- gaps -----------------------------------------------------------
    def mark_gap_processed(self, gap_id: str, outcome: str) -> None:
        con = self._conn()
        try:
            con.execute(
                "INSERT OR REPLACE INTO rc_processed_gaps "
                "(gap_id, outcome, at) VALUES (?,?,?)",
                (gap_id, outcome, time.time()))
            con.commit()
        finally:
            con.close()

    def is_gap_processed(self, gap_id: str) -> bool:
        con = self._conn()
        try:
            row = con.execute(
                "SELECT 1 FROM rc_processed_gaps WHERE gap_id=?",
                (gap_id,)).fetchone()
        finally:
            con.close()
        return row is not None

    def note_gap_failure(self, gap_id: str, base_s: float,
                         max_s: float) -> float:
        """Increment failure count; return the new backoff_until."""
        con = self._conn()
        try:
            row = con.execute(
                "SELECT failures FROM rc_gap_backoff WHERE gap_id=?",
                (gap_id,)).fetchone()
            failures = (row[0] if row else 0) + 1
            backoff_until = time.time() + min(base_s * (2 ** (failures - 1)),
                                              max_s)
            con.execute(
                "INSERT OR REPLACE INTO rc_gap_backoff "
                "(gap_id, failures, backoff_until) VALUES (?,?,?)",
                (gap_id, failures, backoff_until))
            con.commit()
        finally:
            con.close()
        return backoff_until

    def backoff_until(self, gap_id: str) -> float:
        con = self._conn()
        try:
            row = con.execute(
                "SELECT backoff_until FROM rc_gap_backoff WHERE gap_id=?",
                (gap_id,)).fetchone()
        finally:
            con.close()
        return float(row[0]) if row else 0.0

    def gap_failure_count(self, gap_id: str) -> int:
        """Consecutive dispatch failures recorded for a gap (read-only).

        PLOOP-1: the boundary detector's stagnation signal. Zero when the
        gap has no backoff record."""
        con = self._conn()
        try:
            row = con.execute(
                "SELECT failures FROM rc_gap_backoff WHERE gap_id=?",
                (gap_id,)).fetchone()
        finally:
            con.close()
        return int(row[0]) if row else 0

    def last_cycle_at(self) -> Optional[float]:
        """Wall-clock of the most recent recorded tick (read-only).

        PLOOP-1: the boundary detector's cadence signal. None when no
        tick has ever been recorded."""
        con = self._conn()
        try:
            row = con.execute(
                "SELECT MAX(at) FROM rc_cycles").fetchone()
        finally:
            con.close()
        return float(row[0]) if row and row[0] else None

    def last_cycle_summary(self) -> Optional[Dict[str, Any]]:
        """The most recent tick's summary dict (read-only).

        PLOOP-1: the boundary detector's envelope signal
        (budget_exceeded). None when no tick has been recorded."""
        con = self._conn()
        try:
            row = con.execute(
                "SELECT summary_json FROM rc_cycles "
                "ORDER BY at DESC LIMIT 1").fetchone()
        finally:
            con.close()
        if not row or not row[0]:
            return None
        try:
            return json.loads(row[0])
        except Exception:
            return None

    def clear_gap_backoff(self, gap_id: str) -> None:
        con = self._conn()
        try:
            con.execute("DELETE FROM rc_gap_backoff WHERE gap_id=?", (gap_id,))
            con.commit()
        finally:
            con.close()

    # -- cycles ---------------------------------------------------------
    def record_cycle(self, n: int, summary: Dict[str, Any]) -> None:
        con = self._conn()
        try:
            con.execute(
                "INSERT OR REPLACE INTO rc_cycles (n, at, summary_json) "
                "VALUES (?,?,?)", (n, time.time(), json.dumps(summary)))
            con.commit()
        finally:
            con.close()

    def cycles_completed(self) -> int:
        con = self._conn()
        try:
            row = con.execute("SELECT COUNT(*) FROM rc_cycles").fetchone()
        finally:
            con.close()
        return int(row[0]) if row else 0

    def processed_gap_ids(self) -> List[str]:
        con = self._conn()
        try:
            rows = con.execute(
                "SELECT gap_id FROM rc_processed_gaps").fetchall()
        finally:
            con.close()
        return [r[0] for r in rows]


# ---------------------------------------------------------------------------
# The Controller
# ---------------------------------------------------------------------------

class RunController:
    """Owns cadence and execution control. Drives the loops; never becomes
    an intelligence layer."""

    def __init__(self, engine: Any, *, config: Optional[RunConfig] = None,
                 checkpoint_path: Optional[str] = None,
                 registry: Any = None) -> None:
        self.engine = engine
        self.epistemic = engine.intellect.epistemic
        self.config = config or RunConfig()
        self._registry = registry
        self._stop = threading.Event()
        self._pause = threading.Event()  # set => paused
        self._scheduler = DumbScheduler(self.config.cadence_interval_s)
        if checkpoint_path is None:
            base = getattr(engine, "db_path", None)
            if base:
                checkpoint_path = os.path.join(
                    os.path.dirname(os.path.abspath(base)),
                    "run_controller_checkpoint.db")
            else:
                checkpoint_path = os.path.abspath(
                    "run_controller_checkpoint.db")
        self._checkpoint = ControllerCheckpoint(checkpoint_path)
        self.checkpoint_path = checkpoint_path
        self._run_id = f"run_{uuid.uuid4().hex[:12]}"
        # PLOOP-10: the Run Controller owns resource+concurrency admission
        # (James's architecture decision, standing). The arbitrator is the
        # mechanism; it is constructed at bind time, when the substrate's
        # initial pools are measurable -- capacity is the sum of those real
        # pools, never a hard-coded constant. Until a substrate is bound,
        # arbitration is dormant and the static pools are the live path
        # (named by arbitration_status(), never silent).
        self._arbitrator: Optional[ResourceArbitrator] = None
        self._arbitration_substrate: Any = None
        self._arbitration_last: Optional[Dict[str, Any]] = None
        if not self.config.staging_dir:
            self.config.staging_dir = os.path.join(
                os.path.dirname(os.path.abspath(checkpoint_path)),
                "rc_q7_staging")

    # -- registry (lazy; M7's file is called, never edited) ---------------
    def _gap_registry(self) -> Any:
        if self._registry is None:
            from swarm_engine.acquisition.gaps import GapRegistry
            self._registry = GapRegistry(self.engine)
        return self._registry

    # -- resource arbitration (PLOOP-10) -----------------------------------
    # The Run Controller owns resource+concurrency admission. Each tick it
    # measures each loop's real demand, arbitrates, and applies the grants
    # to the shared substrate's per-loop pools -- replacing the static
    # initial registrations with arbitrated grants on the live path.

    def bind_arbitration_substrate(self, substrate: Any) -> Dict[str, Any]:
        """Bind the shared microcontroller substrate to the arbitration path.

        Capacity is MEASURED: the sum of the substrate's initial per-loop
        pools (the regime the static defaults established), so adoption is
        never more permissive than the static path it replaces. All six
        loops register with equal weight (no loop privileged -- the charter
        peer relationship) and zero minimums: demand rules, and starvation
        shows up as recorded refusals instead of hiding inside minimums.
        Reserved spend is always part of measured demand, so shrinking a
        grant never silently starves already-running work -- the decision
        record shows it.

        Raises loudly (KeyError/ValueError) when the substrate does not
        carry all six loop pools: a misconfigured bind is a construction
        error, never a silent partial adoption.
        """
        pools = substrate.export_state()["loops"]  # public snapshot
        missing = [loop for loop in LOOPS if loop not in pools]
        if missing:
            raise KeyError(
                "bind_arbitration_substrate: substrate has no pools for "
                f"{missing}; register the loops before binding")
        total_b = round(
            sum(float(p["budget_s"]) for p in pools.values()), 6)
        total_c = int(sum(int(p["max_concurrent"]) for p in pools.values()))
        if not (total_b > 0 and total_c > 0):
            raise ValueError(
                "bind_arbitration_substrate: measured capacity must be "
                f"> 0 (got {total_b}s / {total_c} slots)")
        arb = ResourceArbitrator(
            total_budget_s=total_b, total_max_concurrent=total_c,
            epoch_length_s=self.config.cadence_interval_s)
        for loop in LOOPS:
            arb.register_loop(loop, weight=1.0,
                              minimum_budget_s=0.0, minimum_concurrent=0)
        self._arbitrator = arb
        self._arbitration_substrate = substrate
        return {"bound": True, "capacity_budget_s": total_b,
                "capacity_concurrent": total_c,
                "epoch_length_s": self.config.cadence_interval_s}

    def _measure_demands(self) -> Dict[str, Dict[str, Any]]:
        """Measure each loop's substrate demand from real state.

        budget_s = currently-reserved spend (from the substrate's public
        snapshot: the grant must keep covering active work) + pending new
        work x that work's real per-unit cost. concurrent = active MCs +
        pending units. A loop with no microcontroller-spawning work path
        and no measurable backlog honestly states its reserved spend only;
        new demand appears as reserved/active the moment it spawns.
        """
        substrate = self._arbitration_substrate
        pools = substrate.export_state()["loops"]
        n_open = len(self._gap_registry().list_gaps(status="open"))
        demands: Dict[str, Dict[str, Any]] = {}
        for loop in LOOPS:
            view = substrate.loop_view(loop)
            reserved = float(pools[loop]["reserved_s"])
            if loop == LOOP_ACQUISITION:
                # The acquisition loop's real MC-spawning path is the hosted
                # cognition cycle (AcquisitionLoopInlet.drive_cognition);
                # each open gap is a candidate for one hosted cycle.
                pending_units = n_open
                budget = (reserved + pending_units
                          * _ACQUISITION_HOSTED_CYCLE_BUDGET_S)
                concurrent = view.active_count + pending_units
                method = (
                    f"reserved {reserved:.1f}s + {pending_units} open gaps x "
                    f"{_ACQUISITION_HOSTED_CYCLE_BUDGET_S:.0f}s hosted-cycle "
                    "budget (upper bound: the loop states what it could "
                    "host; the arbitrator decides what it gets)")
            else:
                # No MC-spawning work path with a measurable backlog signal:
                # the run loop dispatches gaps inline in tick(); execution
                # repair runs inline; acceptance verification is
                # driver-driven; distillation's spawn_microcontroller has no
                # callers yet; generalization's run_cycle is mission-driven
                # with no steady backlog queue.
                budget = reserved
                concurrent = view.active_count
                method = (
                    f"reserved {reserved:.1f}s + {view.active_count} active "
                    "(no measurable pending microcontroller work)")
            demands[loop] = {"budget_s": budget, "concurrent": concurrent,
                             "method": method}
        return demands

    def _arbitrate_resources(self) -> Dict[str, Any]:
        """One arbitration round: measure -> set_demand -> arbitrate ->
        apply. Returns a JSON-serializable summary for the cycle record.

        Grants are written into the substrate's per-loop pools, where the
        substrate's own admission refuses spawns beyond them
        (R_ADMISSION_EXHAUSTED / R_CONCURRENCY_CAP, recorded in
        total_refused) -- the grant is enforced, not advisory. The refused
        portion of each loop's demand is computed here and carried in the
        summary: refused demand is visible, never vanished.
        """
        if self._arbitrator is None or self._arbitration_substrate is None:
            return {
                "mode": "static-fallback",
                "reason": ("no substrate bound via "
                           "bind_arbitration_substrate: arbitration dormant; "
                           "the pools as registered (static initial grants) "
                           "remain the live path"),
            }
        measured = self._measure_demands()
        for loop, m in measured.items():
            self._arbitrator.set_demand(loop, budget_s=m["budget_s"],
                                       concurrent=int(m["concurrent"]))
        decision = self._arbitrator.apply(self._arbitration_substrate)
        pools_now = self._arbitration_substrate.export_state()["loops"]
        grants: Dict[str, Any] = {}
        for loop, m in measured.items():
            g = decision.grants.get(loop)
            pool = pools_now[loop]
            if g is None:
                # Zero demand: the arbitrator states no grant and the pool
                # keeps its previous grant (no thrash). Nothing is refused:
                # refused demand is demand minus grant, and both are zero.
                grants[loop] = {
                    "demand_budget_s": round(float(m["budget_s"]), 3),
                    "demand_concurrent": int(m["concurrent"]),
                    "grant_stated": False,
                    "grant_budget_s": None,
                    "grant_concurrent": None,
                    "pool_budget_s": pool["budget_s"],
                    "pool_concurrent": pool["max_concurrent"],
                    "refused_budget_s": 0.0,
                    "refused_concurrent": 0,
                    "method": m["method"],
                    "rationale": ("zero demand: no grant stated; the pool "
                                  "keeps its previous grant"),
                }
                continue
            grants[loop] = {
                "demand_budget_s": round(float(m["budget_s"]), 3),
                "demand_concurrent": int(m["concurrent"]),
                "grant_stated": True,
                "grant_budget_s": g.budget_s,
                "grant_concurrent": g.max_concurrent,
                "pool_budget_s": pool["budget_s"],
                "pool_concurrent": pool["max_concurrent"],
                "refused_budget_s": round(
                    max(0.0, float(m["budget_s"]) - g.budget_s), 3),
                "refused_concurrent": max(
                    0, int(m["concurrent"]) - g.max_concurrent),
                "method": m["method"],
                "rationale": g.rationale,
            }
        total_stated = round(sum(
            g["grant_budget_s"] for g in grants.values()
            if g["grant_stated"]), 6)
        summary = {
            "mode": "arbitrated",
            "epoch_id": decision.epoch_id,
            "capacity_budget_s": decision.total_budget_s,
            "capacity_concurrent": decision.total_max_concurrent,
            "total_stated_budget_s": total_stated,
            "headroom_budget_s": decision.headroom_budget_s,
            "headroom_concurrent": decision.headroom_concurrent,
            "minimums_scaled": decision.minimums_scaled,
            "notes": list(decision.notes),
            "grants": grants,
        }
        self._arbitration_last = summary
        return summary

    def arbitration_status(self) -> Dict[str, Any]:
        """Explicit live-path report: 'arbitrated' or 'static-fallback'.

        The static path is never the silent live path: when arbitration is
        dormant this method names the reason, and the tick's cycle summary
        carries the same mode on every round.
        """
        if self._arbitrator is None or self._arbitration_substrate is None:
            return {"mode": "static-fallback",
                    "reason": ("no substrate bound via "
                               "bind_arbitration_substrate"),
                    "live_path": "static initial pools"}
        return {"mode": "arbitrated",
                "live_path": "arbitrator grants",
                "epoch_id": self._arbitrator.epoch_id,
                "capacity_budget_s": self._arbitrator.last_decision.total_budget_s
                if self._arbitrator.last_decision else None,
                "last_round": self._arbitration_last}

    # -- control ---------------------------------------------------------
    @property
    def run_id(self) -> str:
        """The controller's run id (read-only).

        PLOOP-1: the boundary detector needs the run id for run_wake
        evidence; the id was previously private-only."""
        return self._run_id

    def request_stop(self) -> None:
        self._stop.set()

    def request_pause(self) -> None:
        self._pause.set()
        self._observe("run_paused", "Run Controller: pause requested.",
                      {"run_id": self._run_id})

    def resume(self) -> None:
        self._pause.clear()
        self._observe("run_resumed", "Run Controller: resume requested.",
                      {"run_id": self._run_id})

    def _paused_wait(self) -> bool:
        """Wait while paused. Returns False if stop was requested."""
        while self._pause.is_set():
            if self._stop.wait(0.5):
                return False
        return True

    # -- observation ------------------------------------------------------
    def _observe(self, kind: str, content: str,
                 raw: Optional[Dict[str, Any]] = None,
                 causal_chain: Optional[List[str]] = None) -> str:
        """Every Controller action is an observation through V10-P1's path.
        Observation failure never breaks the loop."""
        try:
            from swarm_engine.intellect.unified_memory import (
                record_experience)
            payload = dict(raw or {})
            payload["run_id"] = self._run_id
            return record_experience(
                self.epistemic, origin_loop=ORIGIN_LOOP, kind=kind,
                content=content, raw=payload,
                causal_chain=causal_chain or [],
                source=f"{ORIGIN_LOOP}/{kind}")
        except Exception:
            return ""

    @property
    def stop_requested(self) -> bool:
        """True once request_stop() has been called. Lets a cooperative
        host (the Android wrapper's serve+cadence pump) poll for shutdown
        without touching the controller's internals."""
        return self._stop.is_set()

    # -- main run ----------------------------------------------------------
    def run(self, wait_fn=None) -> Dict[str, Any]:
        """The unprompted run. Loops on the authorized cadence until the run
        budget is exhausted, max_cycles is reached, or stop is requested.
        Never raises on operational failures: the report carries what
        happened, and a crashed loop is recorded "crashed" (never
        mislabeled "complete") so the next boot resumes it.

        wait_fn: optional zero-arg callable used INSTEAD of the dumb
        scheduler's blocking wait. It must serve the host while waiting
        (the Android wrapper's cooperative serve+cadence pump) and return
        False when the run should stop. None (default) keeps the
        standalone blocking cadence.
        """
        cfg = self.config
        ckpt = self._checkpoint
        report: Dict[str, Any] = {
            "run_id": self._run_id, "cycles": [], "stopped": False,
            "resumed": False, "budget_exhausted": False,
            "errors": [],
        }
        crashed = False
        try:
            # Resume-load: every value is validated. A checkpoint that is
            # corrupt or carries unparsable values is quarantined and the
            # run starts fresh -- fail closed, named, never crash the boot.
            try:
                prior_status = ckpt.load_meta("status")
                prior_cycles = ckpt.cycles_completed()
                saved_deadline = ckpt.load_meta("deadline_wall")
                if saved_deadline is not None:
                    float(saved_deadline)  # validate; may raise ValueError
            except (sqlite3.DatabaseError, ValueError, TypeError) as exc:
                bad = ckpt.quarantine_and_reset()
                self._observe(
                    "checkpoint_quarantined",
                    f"Run Controller {self._run_id}: checkpoint failed "
                    f"integrity ({type(exc).__name__}: {exc}); quarantined "
                    f"to {bad}; starting fresh, fail-closed.",
                    {"quarantined_to": bad,
                     "reason": f"{type(exc).__name__}: {exc}"})
                prior_status, prior_cycles, saved_deadline = None, 0, None
            # A loop that died from an exception is recorded "crashed", and
            # "crashed" is a resume signal alongside "running": a crash is
            # never mislabeled a clean finish, and the next boot picks the
            # run back up instead of silently dropping it.
            if prior_status in ("running", "crashed") and prior_cycles > 0:
                report["resumed"] = True
                self._observe(
                    "run_resumed",
                    f"Run Controller {self._run_id}: resumed from checkpoint "
                    f"({prior_cycles} cycles completed, "
                    f"{len(ckpt.processed_gap_ids())} gaps processed).",
                    {"prior_status": prior_status,
                     "prior_cycles": prior_cycles})
            else:
                self._observe(
                    "run_started",
                    f"Run Controller {self._run_id}: starting on authorized "
                    f"cadence (interval {cfg.cadence_interval_s}s, run budget "
                    f"{cfg.run_budget_s}s).",
                    {"config": cfg.as_dict()})
            ckpt.save_meta("status", "running")
            ckpt.save_meta("run_id", self._run_id)
            ckpt.save_meta("config", json.dumps(cfg.as_dict()))

            # The authorized run deadline persists across kills: a resumed
            # run gets the REMAINDER of its original budget, never a fresh
            # one. Without this, kill -> resume could exceed the authorized
            # total wall-clock the RunConfig grants.
            if report["resumed"]:
                deadline_wall = float(saved_deadline)
            else:
                deadline_wall = time.time() + cfg.run_budget_s
                ckpt.save_meta("deadline_wall", str(deadline_wall))
            # Wall-clock-jump clamp: a forward jump must not silently zero
            # the remaining budget (deadline in the past => expired, named
            # via the budget_exhausted path below), and a backward jump
            # must not grant more than the authorized run_budget_s. Both
            # directions are bounded without pretending to know the true
            # elapsed time.
            wall_now = time.time()
            remaining = deadline_wall - wall_now
            clamped = False
            if remaining > cfg.run_budget_s:
                remaining = cfg.run_budget_s
                clamped = True
            run_deadline = (time.monotonic() + max(0.0, remaining))
            if clamped:
                self._observe(
                    "deadline_clamped",
                    f"Run Controller {self._run_id}: persisted deadline_wall "
                    f"exceeded the authorized run budget ({cfg.run_budget_s}s"
                    f") -- wall-clock jump or tampered checkpoint suspected; "
                    f"remaining budget clamped to {cfg.run_budget_s}s.",
                    {"deadline_wall": deadline_wall, "wall_now": wall_now,
                     "run_budget_s": cfg.run_budget_s})
            cycle_n = prior_cycles
            while True:
                if self._stop.is_set():
                    report["stopped"] = True
                    break
                if (cfg.max_cycles is not None
                        and cycle_n >= cfg.max_cycles):
                    break
                if time.monotonic() >= run_deadline:
                    report["budget_exhausted"] = True
                    self._observe(
                        "budget_exhausted",
                        f"Run Controller {self._run_id}: run budget "
                        f"{cfg.run_budget_s}s exhausted after {cycle_n} "
                        f"cycles; stopping, not overrunning.",
                        {"cycles": cycle_n})
                    break
                if not self._paused_wait():
                    report["stopped"] = True
                    break
                cycle_n += 1
                try:
                    summary = self.tick(cycle_n=cycle_n)
                except Exception as exc:  # a tick never kills the run
                    summary = {"cycle": cycle_n, "tick_error":
                               f"{type(exc).__name__}: {exc}"}
                    report["errors"].append(summary["tick_error"])
                report["cycles"].append(summary)
                # Per-cycle checkpoint writes are intentionally unprotected:
                # a storage failure here (real under app storage quotas)
                # flows to the except below -- honest stop, status
                # "crashed", next boot resumes. Swallowing it would fake a
                # healthy run while losing state.
                ckpt.record_cycle(cycle_n, summary)
                ckpt.save_meta("status", "running")
                # PLOOP-3: the ACCEPT stage of the loop, every cycle. The
                # checkpoint row above is what the operational auth
                # re-reads; the acceptance record lands in acceptance.db
                # next to it. Acceptance never kills the run and never
                # goes silent: its own failure is recorded on the summary.
                try:
                    summary["acceptance"] = self._accept_cycle(
                        cycle_n, summary)
                except Exception as exc:
                    summary["acceptance"] = {
                        "cycle": cycle_n, "state": "acceptance_error",
                        "detail": f"{type(exc).__name__}: {exc}"}
                    self._observe(
                        "acceptance_error",
                        f"Run Controller {self._run_id}: per-cycle "
                        f"acceptance failed for cycle {cycle_n} "
                        f"({type(exc).__name__}: {exc}); recorded, not "
                        f"swallowed.",
                        {"cycle": cycle_n,
                         "error": f"{type(exc).__name__}: {exc}"})
                ckpt.record_cycle(cycle_n, summary)
                if wait_fn is not None:
                    if not wait_fn():
                        report["stopped"] = True
                        break
                elif not self._scheduler.wait_until_next_tick(self._stop):
                    report["stopped"] = True
                    break
        except Exception as exc:
            # The run died from an operational failure: record it honestly
            # as crashed so the next boot resumes instead of believing a
            # clean finish happened. run() never propagates.
            crashed = True
            report["errors"].append(
                f"run_fatal: {type(exc).__name__}: {exc}")
            self._observe(
                "run_crashed",
                f"Run Controller {self._run_id}: run loop died "
                f"({type(exc).__name__}: {exc}); recorded crashed, next "
                f"boot resumes.",
                {"error": f"{type(exc).__name__}: {exc}",
                 "cycles": len(report["cycles"])})
        finally:
            try:
                ckpt.save_meta(
                    "status", "stopped" if self._stop.is_set()
                    else ("crashed" if crashed else "complete"))
                self._observe(
                    "run_finished",
                    f"Run Controller {self._run_id}: finished "
                    f"({len(report['cycles'])} cycles, "
                    f"stopped={report['stopped']}, "
                    f"budget_exhausted={report['budget_exhausted']}, "
                    f"crashed={crashed}).",
                    {"cycles": len(report["cycles"]),
                     "stopped": report["stopped"],
                     "budget_exhausted": report["budget_exhausted"],
                     "crashed": crashed,
                     "errors": report["errors"]})
            except Exception:
                pass
        return report

    # -- one cadence tick ----------------------------------------------------
    def tick(self, cycle_n: int = 0) -> Dict[str, Any]:
        """One authorized tick: gap queue (user-gaps first), distillation
        sweep, quarantine sweep + Q7 triggers, observe. Cooperative with the
        cycle budget: a step already running is never killed mid-flight."""
        cfg = self.config
        started = time.monotonic()
        summary: Dict[str, Any] = {
            "cycle": cycle_n, "at": time.time(),
            "gaps": [], "sweep": None, "quarantine": None,
            "q7_attempts": [], "errors": [],
            "budget_exceeded": False,
        }

        def _over_budget() -> bool:
            return (time.monotonic() - started) >= cfg.cycle_budget_s

        # 0. resource arbitration (PLOOP-10) --------------------------------
        # Measured demands -> arbitrate -> apply, every tick, before the
        # tick's own work: the loops that spawn microcontrollers do so
        # under fresh grants. A failed round never breaks the tick -- the
        # pools keep their previous grants (fail-closed) and the failure is
        # observed in the cycle summary.
        try:
            summary["arbitration"] = self._arbitrate_resources()
        except Exception as exc:
            summary["arbitration"] = {
                "mode": "error",
                "error": f"{type(exc).__name__}: {exc}",
            }
            self._observe(
                "arbitration_error",
                f"Run Controller tick {cycle_n}: arbitration round failed "
                f"({type(exc).__name__}: {exc}); pools keep their previous "
                "grants, fail-closed.",
                {"kind": "arbitration_error",
                 "error": f"{type(exc).__name__}: {exc}"},
                causal_chain=[self._run_id])

        # 1. gap queue -- user-gaps first --------------------------------
        try:
            summary["gaps"] = self._process_gap_queue(
                deadline=started + cfg.cycle_budget_s)
        except Exception as exc:
            summary["errors"].append(f"gap_queue: {type(exc).__name__}: {exc}")
        if _over_budget():
            summary["budget_exceeded"] = True

        # 2. distillation sweep (V10-P4; this Controller is the clock) -----
        if not _over_budget():
            try:
                summary["sweep"] = self._distill_sweep()
            except Exception as exc:
                summary["errors"].append(
                    f"distill_sweep: {type(exc).__name__}: {exc}")
        else:
            summary["budget_exceeded"] = True

        # 3. quarantine sweep + Q7 triggers --------------------------------
        if not _over_budget():
            try:
                q = self._quarantine_sweep()
                summary["quarantine"] = q["sweep"]
                summary["q7_attempts"] = q["q7_attempts"]
            except Exception as exc:
                summary["errors"].append(
                    f"quarantine_sweep: {type(exc).__name__}: {exc}")
        else:
            summary["budget_exceeded"] = True

        summary["elapsed_s"] = round(time.monotonic() - started, 2)
        self._observe(
            "cycle_summary",
            f"Run Controller tick {cycle_n}: {len(summary['gaps'])} gaps "
            f"processed, sweep={bool(summary['sweep'])}, "
            f"q7_attempts={len(summary['q7_attempts'])}, "
            f"errors={len(summary['errors'])}, "
            f"budget_exceeded={summary['budget_exceeded']}.",
            {"kind": "cycle_summary", "summary": summary},
            causal_chain=[self._run_id])
        return summary

    # -- gap queue ------------------------------------------------------------
    @staticmethod
    def _is_user_gap(record: Any) -> bool:
        d = getattr(record, "dissatisfaction", None)
        return bool(d is not None and (d.feedback or d.attempt_ref))

    def _process_gap_queue(self, deadline: float) -> List[Dict[str, Any]]:
        """Dispatch open gaps through the registry, user-gaps first.

        Closed gaps are checkpointed and never redispatched. Anything else
        -- an acquisition that failed open, a dispatch error -- backs off
        exponentially and is retried on a later cycle, never spun on and
        never silently dropped: repeated failures back off, they do not
        vanish."""
        cfg = self.config
        registry = self._gap_registry()
        try:
            open_gaps = registry.list_gaps(status="open")
        except Exception as exc:
            self._observe("gap_queue_error",
                         f"Run Controller: could not list open gaps: "
                         f"{type(exc).__name__}: {exc}", {})
            return []
        # user-gaps first, then oldest first
        open_gaps.sort(key=lambda g: (
            0 if self._is_user_gap(g) else 1,
            getattr(g, "registered_at", 0.0)))
        processed: List[Dict[str, Any]] = []
        for record in open_gaps[:cfg.max_gaps_per_cycle]:
            if time.monotonic() >= deadline:
                break
            gap_id = record.gap_id
            if self._checkpoint.is_gap_processed(gap_id):
                continue
            if time.time() < self._checkpoint.backoff_until(gap_id):
                continue
            gap_deadline = min(deadline,
                               time.monotonic() + cfg.gap_budget_s)
            outcome = self._dispatch_one_gap(registry, record, gap_deadline)
            processed.append(outcome)
            if outcome["outcome"] == "closed":
                self._checkpoint.mark_gap_processed(gap_id, "closed")
                self._checkpoint.clear_gap_backoff(gap_id)
            else:
                # "open" (acquisition failed open / no route) and "error"
                # are both repeated-failure candidates: back off, retry
                # later, never spin, never drop.
                self._checkpoint.note_gap_failure(
                    gap_id, cfg.backoff_base_s, cfg.backoff_max_s)
        return processed

    def _dispatch_one_gap(self, registry: Any, record: Any,
                          gap_deadline: float) -> Dict[str, Any]:
        """Dispatch a single gap through the registry's shape routing."""
        gap_id = record.gap_id
        outcome: Dict[str, Any] = {
            "gap_id": gap_id, "user_gap": self._is_user_gap(record),
            "outcome": "error", "route": "", "detail": "",
        }
        try:
            result = registry.dispatch(
                gap_id, context={"dispatched_by": "run_controller",
                                 "run_id": self._run_id,
                                 "deadline": gap_deadline})
            outcome["outcome"] = result.outcome
            outcome["route"] = result.route_name or ""
            outcome["detail"] = (result.detail or "")[:500]
        except Exception as exc:  # dispatch never kills the tick
            outcome["detail"] = f"{type(exc).__name__}: {exc}"[:500]
        self._observe(
            "gap_dispatched",
            f"Run Controller: gap {gap_id} dispatched "
            f"(user_gap={outcome['user_gap']}): {outcome['outcome']} "
            f"via route {outcome['route'] or 'none'}.",
            {"kind": "gap_dispatch", "gap_id": gap_id,
             "user_gap": outcome["user_gap"], "outcome": outcome["outcome"],
             "route": outcome["route"], "detail": outcome["detail"]},
            causal_chain=[self._run_id, gap_id])
        return outcome

    # -- distillation sweep ------------------------------------------------------
    def _distill_sweep(self) -> Dict[str, Any]:
        """Clock V10-P4's sweep. The sweep is the work; this is the clock."""
        from swarm_engine.acquisition.distill_driver import (
            run_distillation_sweep)
        summary = run_distillation_sweep(
            self.engine, self.epistemic, limit=1000)
        self._observe(
            "distill_sweep",
            f"Run Controller: distillation sweep examined "
            f"{summary.get('examined', 0)}, distilled "
            f"{len(summary.get('distilled', []))}, refused "
            f"{len(summary.get('refused', []))}, errors "
            f"{len(summary.get('errors', []))}.",
            {"kind": "distill_sweep", "summary": summary},
            causal_chain=[self._run_id])
        return summary

    # -- quarantine sweep + Q7 ------------------------------------------------------
    def _quarantine_sweep(self) -> Dict[str, Any]:
        """M5's sweep on the Controller's cadence, then the Q7 trigger:
        where the diagnosis identifies a missing substrate, M3's driver
        fires from that diagnosis -- no operator."""
        from swarm_engine.synthesis.integrity import repair_all_quarantined
        sweep = repair_all_quarantined(
            self.engine, caller="run_controller",
            reason="run_controller cadence sweep")
        q7_attempts: List[Dict[str, Any]] = []
        try:
            quarantined = self.engine.capabilities.list(
                status="quarantined", limit=10000)
        except Exception:
            quarantined = []
        for rec in quarantined[:self.config.max_q7_per_cycle]:
            cid = getattr(rec, "capability_id", None)
            if cid is None and isinstance(rec, dict):
                cid = rec.get("capability_id")
            if not cid:
                continue
            try:
                attempt = self._q7_substrate_trigger(str(cid))
            except Exception as exc:  # a trigger never kills the sweep
                attempt = {"capability_id": str(cid), "fired": False,
                           "error": f"{type(exc).__name__}: {exc}"}
            if attempt:
                q7_attempts.append(attempt)
        self._observe(
            "quarantine_sweep",
            f"Run Controller: quarantine sweep saw "
            f"{sweep.get('count', 0)} quarantined; "
            f"{len(q7_attempts)} Q7 substrate triggers fired.",
            {"kind": "quarantine_sweep",
             "count": sweep.get("count", 0),
             "q7_attempts": q7_attempts},
            causal_chain=[self._run_id])
        return {"sweep": sweep, "q7_attempts": q7_attempts}

    def _q7_substrate_trigger(self, capability_id: str) -> Optional[Dict[str, Any]]:
        """Q7: M5's diagnosis fires M3's substrate driver.

        Only fires where the REAL diagnosis identifies a missing substrate
        (parse_dependency_reason succeeds on the live diagnosis reason).
        The outcome -- readmitted, still_quarantined, unavailable -- is
        whatever the governed path honestly produces; the firing itself is
        what this trigger proves (no operator in the middle)."""
        from swarm_engine.synthesis.integrity import diagnose_quarantine
        from swarm_engine.acquisition.substrate import (
            ArtifactPin, GovernedFetchChannel, SubstrateAcquisitionDriver,
            TrustedIndex, parse_dependency_reason)
        from swarm_engine.governance.caller_authorization import (
            _engine_caller_context)
        diag = diagnose_quarantine(self.engine, capability_id)
        if not diag.quarantined or not diag.reason:
            return None
        spec = parse_dependency_reason(diag.reason)
        if spec is None:
            return None  # not a missing-substrate diagnosis: do not fire
        indexes = []
        for entry in self.config.trusted_indexes:
            pins = {}
            for aid, pin in (entry.get("artifacts") or {}).items():
                pins[aid] = ArtifactPin(
                    artifact_id=aid, url=pin["url"],
                    sha256=pin["sha256"],
                    media_type=pin.get("media_type",
                                       "application/json"))
            indexes.append(TrustedIndex(
                name=entry.get("name", "unnamed"),
                base_url=entry.get("base_url", ""),
                artifacts=pins))
        channel = GovernedFetchChannel(
            indexes, staging_dir=self.config.staging_dir)
        # The Controller is engine-owned machinery (like quarantine_as_engine):
        # it carries the engine's own caller context, not a bare string, so
        # the restore path's authorization gate is satisfied, never bypassed.
        driver = SubstrateAcquisitionDriver(
            self.engine, channel=channel,
            caller=_engine_caller_context(self.engine))
        result = driver.attempt(capability_id)
        attempt = {
            "capability_id": capability_id, "fired": True,
            "dependency": {"name": spec.name, "kind": spec.kind},
            "diagnosis_verdict": diag.verdict,
            "attempt_outcome": result.outcome,
            "attempt_label": result.label,
            "attempt_detail": (result.detail or "")[:500],
        }
        self._observe(
            "q7_substrate_attempt",
            f"Run Controller Q7: diagnosis of {capability_id} identified "
            f"missing {spec.kind} {spec.name!r}; M3's driver fired: "
            f"{result.outcome} ({result.label}).",
            {"kind": "q7_trigger", **attempt},
            causal_chain=[self._run_id, capability_id])
        return attempt

    # -- acceptance-loop invocation (owned here; Q8's present() calls in) --
    def _acceptance_driver(self):
        """The production acceptance driver behind this inlet (V10-P6).
        Built once from the Controller's own engine + epistemic store;
        M6's AcceptanceLoop is called, never rebuilt."""
        drv = getattr(self, "_driver", None)
        if drv is None:
            from swarm_engine.services.acceptance_driver import (
                AcceptanceDriver)
            store_path = os.path.join(
                os.path.dirname(os.path.abspath(self.checkpoint_path)),
                "acceptance.db")
            drv = AcceptanceDriver.from_controller(self, store_path)
            self._driver = drv
        return drv

    def _accept_cycle(self, cycle_n: int,
                      summary: Dict[str, Any]) -> Dict[str, Any]:
        """Per-cycle acceptance, owned here; the driver owns the
        mechanics (PLOOP-3). Every tick's summary is accepted against
        its persisted checkpoint row -- the ACCEPT stage of the loop."""
        return self._acceptance_driver().accept_cycle(
            self, cycle_n, summary)

    def acceptance_driver(self):
        """Access to the production driver (verdict submission, chains)."""
        return self._acceptance_driver()

    def invoke_acceptance(self, result: Dict[str, Any]) -> Dict[str, Any]:
        """Acceptance-loop invocation inlet. OWNED BY THE CONTROLLER.

        Q8's present() is an inlet into this mechanism, not the owner of
        the loop. A produced result enters the acceptance/review pathway:
        the production driver (V10-P6) authenticates it with real
        executions and presents it as a CANDIDATE. Completed is a
        candidate state -- never terminal, never accepted-by-default."""
        drv = self._acceptance_driver()
        rec = drv.present_result(result)
        return {"invoked": True, "run_id": rec.run_id,
                "state": rec.state.value, "rounds": rec.rounds,
                "note": "production acceptance driver (V10-P6) behind "
                        "the Controller-owned inlet"}

    # -- developmental-stage hooks (dormant until V10-P7) ------------------
    def note_stage_transition(self, from_stage: str, to_stage: str,
                              evidence: Dict[str, Any]) -> str:
        """Record-only until V10-P7 designs staging semantics. The hook
        exists so the ownership is settled; the semantics are not invented
        here."""
        return self._observe(
            "stage_transition_noted",
            f"Run Controller: stage transition noted {from_stage} -> "
            f"{to_stage} (semantics: V10-P7).",
            {"kind": "stage_transition", "from": from_stage,
             "to": to_stage, "evidence": evidence},
            causal_chain=[self._run_id])
