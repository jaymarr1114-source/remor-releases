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
    (the inlet Q8's present() will call -- owned here, not by present()),
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
        con = sqlite3.connect(self._path)
        try:
            con.executescript(_CHECKPOINT_SCHEMA)
            con.commit()
        finally:
            con.close()

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

    # -- control ---------------------------------------------------------
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

    # -- main run ----------------------------------------------------------
    def run(self) -> Dict[str, Any]:
        """The unprompted run. Loops on the authorized cadence until the run
        budget is exhausted, max_cycles is reached, or stop is requested.
        Never raises: the report carries what happened."""
        cfg = self.config
        ckpt = self._checkpoint
        report: Dict[str, Any] = {
            "run_id": self._run_id, "cycles": [], "stopped": False,
            "resumed": False, "budget_exhausted": False,
            "errors": [],
        }
        try:
            prior_status = ckpt.load_meta("status")
            prior_cycles = ckpt.cycles_completed()
            if prior_status == "running" and prior_cycles > 0:
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
                saved_deadline = ckpt.load_meta("deadline_wall")
                deadline_wall = (float(saved_deadline) if saved_deadline
                                 else time.time() + cfg.run_budget_s)
            else:
                deadline_wall = time.time() + cfg.run_budget_s
                ckpt.save_meta("deadline_wall", str(deadline_wall))
            run_deadline = (time.monotonic()
                            + max(0.0, deadline_wall - time.time()))
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
                ckpt.record_cycle(cycle_n, summary)
                ckpt.save_meta("status", "running")
                if not self._scheduler.wait_until_next_tick(self._stop):
                    report["stopped"] = True
                    break
        finally:
            try:
                ckpt.save_meta(
                    "status", "stopped" if self._stop.is_set()
                    else "complete")
                self._observe(
                    "run_finished",
                    f"Run Controller {self._run_id}: finished "
                    f"({len(report['cycles'])} cycles, "
                    f"stopped={report['stopped']}, "
                    f"budget_exhausted={report['budget_exhausted']}).",
                    {"cycles": len(report["cycles"]),
                     "stopped": report["stopped"],
                     "budget_exhausted": report["budget_exhausted"],
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
    def invoke_acceptance(self, result: Dict[str, Any]) -> Dict[str, Any]:
        """Acceptance-loop invocation inlet. OWNED BY THE CONTROLLER.

        Q8's present() is an inlet into this mechanism, not the owner of
        the loop. The production acceptance driver lands in V10-P6; until
        then the invocation is recorded honestly, never faked complete."""
        obs_id = self._observe(
            "acceptance_invoked",
            "Run Controller: acceptance-loop invocation recorded "
            "(production driver: V10-P6).",
            {"kind": "acceptance_invocation",
             "result_summary": str(result)[:1000]},
            causal_chain=[self._run_id])
        return {"invoked": True, "observation_id": obs_id,
                "note": "production acceptance driver lands in V10-P6; "
                        "the invocation inlet is owned here"}

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
