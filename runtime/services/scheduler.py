"""
swarm_engine/services/scheduler.py

RunScheduler — real multi-run concurrency management for the REMOR runtime.

Replaces the GUI's "409 while busy" with an honest FIFO queue:

* ONE SwarmEngine, lazily booted on first use (boot ~0.3s, same pattern as
  the GUI server's lazy boot).
* ONE worker thread. Serialization is deliberate, not a shortcut: the
  engine's sqlite stores (capabilities, provenance, memory, ...) are not
  safe under concurrent writers, so genuinely parallel runs would corrupt
  shared state. There is no fake parallelism here — FIFO queueing is the
  honest concurrency model for this engine.
* Every run gets its own RunControl (frozen contract in run_control.py).
  pause / resume / stop / cancel all flow through it: the worker installs it
  as the current control and passes it to handle() via
  metadata["run_control"], so the checkpoint() calls wired through the
  pipeline (stage boundaries via _trace, synthesis loops, plan executor)
  cooperate. A user stop becomes a STOPPED outcome with the trace intact up
  to the checkpoint; it is never relabeled as a failure.
* Run records live in sqlite (table scheduler_runs); per-run stage events
  are buffered in memory (bounded) for SSE adoption.

Status machine:
    queued -> running -> completed | failed | stopped | error
    running -> paused -> running            (pause is cooperative: the run
                                             parks at its next checkpoint)
    running | paused -> stopping -> stopped
    queued -> cancelled                       (cancel only; never executes)

All mutating calls return {"ok": True/False, ...} dicts for expected misuse
(bad id, wrong state) instead of raising.
"""
from __future__ import annotations

import asyncio
import collections
import json
import sqlite3
import threading
import time
import uuid
from typing import Any, Dict, List, Optional

from swarm_engine.core.task_interface import Stage, UniversalTaskInterface
from swarm_engine.services import run_control as _rc
from swarm_engine.services.run_control import RunControl

TERMINAL_STATUSES = frozenset(
    {"completed", "failed", "stopped", "cancelled", "error"}
)
ACTIVE_STATUSES = frozenset({"running", "paused", "stopping"})

_SCHEMA = """
CREATE TABLE IF NOT EXISTS scheduler_runs (
    id              TEXT PRIMARY KEY,
    goal            TEXT NOT NULL,
    status          TEXT NOT NULL,
    conversation_id TEXT,
    queued_at       REAL NOT NULL,
    started_at      REAL,
    ended_at        REAL,
    outcome_json    TEXT,
    error           TEXT
)
"""

_MAX_EVENTS_PER_RUN = 5000
_MAX_EVENT_RUNS_KEPT = 200


class _TracingTaskInterface(UniversalTaskInterface):
    """Minimal adapter: forwards real _trace stage events to a callback.

    Same pattern as the GUI server's TracingTaskInterface, written here so
    the scheduler does not depend on GUI code. Observes only; changes no
    runtime behavior.
    """

    def _trace(self, outcome, stage, ran, detail, elapsed_ms=0.0):
        super()._trace(outcome, stage, ran, detail, elapsed_ms)
        cb = getattr(self, "_event_cb", None)
        if cb is not None:
            try:
                cb({
                    "type": "stage",
                    "stage": stage.value,
                    "ran": ran,
                    "detail": detail[:250],
                    "elapsed_ms": round(elapsed_ms, 3),
                    "at": time.time(),
                })
            except Exception:
                pass


class RunScheduler:
    """FIFO run queue with one worker thread over one lazily-booted engine."""

    def __init__(self, db_path: str, runtime_db_path: str) -> None:
        self.db_path = db_path
        self.runtime_db_path = runtime_db_path
        self._lock = threading.RLock()
        self._cond = threading.Condition(self._lock)
        self._pending: collections.deque = collections.deque()  # run_ids, FIFO
        self._controls: Dict[str, RunControl] = {}
        self._payloads: Dict[str, tuple] = {}  # run_id -> (goal, examples, metadata)
        self._events: Dict[str, List[dict]] = {}
        self._engine = None
        self._engine_lock = threading.Lock()
        self._shutdown = False

        self._db = sqlite3.connect(db_path, check_same_thread=False)
        with self._lock:
            self._db.execute(_SCHEMA)
            self._db.commit()

        self._worker = threading.Thread(
            target=self._worker_loop, name="remor-scheduler", daemon=True
        )
        self._worker.start()

    # ------------------------------------------------------------------
    # engine
    # ------------------------------------------------------------------
    def _engine_lazy(self):
        """Boot the single shared SwarmEngine on first use (thread-safe)."""
        with self._engine_lock:
            if self._engine is None:
                from swarm_engine.core.engine import SwarmEngine

                self._engine = SwarmEngine(db_path=self.runtime_db_path)
            return self._engine

    # ------------------------------------------------------------------
    # submission
    # ------------------------------------------------------------------
    def submit(self, goal: str, examples=None, metadata=None,
               conversation_id: Optional[str] = None) -> Dict[str, Any]:
        """Queue a run. Returns {"ok": True, "run_id": ...} (thread-safe)."""
        with self._lock:
            if self._shutdown:
                return {"ok": False, "error": "scheduler is shut down"}
            if not isinstance(goal, str) or not goal.strip():
                return {"ok": False,
                        "error": "goal must be a non-empty string"}
            run_id = uuid.uuid4().hex
            control = RunControl()
            now = time.time()
            self._db.execute(
                "INSERT INTO scheduler_runs "
                "(id, goal, status, conversation_id, queued_at)"
                " VALUES (?, ?, 'queued', ?, ?)",
                (run_id, goal, conversation_id, now),
            )
            self._db.commit()
            self._controls[run_id] = control
            self._payloads[run_id] = (goal, examples, metadata)
            self._events[run_id] = []
            self._pending.append(run_id)
            self._push_event_locked(
                run_id, {"type": "status", "status": "queued", "at": now})
            self._cond.notify()
            return {"ok": True, "run_id": run_id}

    # ------------------------------------------------------------------
    # run control: pause / resume / stop / cancel
    # ------------------------------------------------------------------
    def pause_run(self, run_id: str) -> Dict[str, Any]:
        """Ask a running run to pause at its next checkpoint."""
        with self._lock:
            rec = self._get_row_locked(run_id)
            if rec is None:
                return {"ok": False, "error": f"unknown run {run_id!r}"}
            if rec["status"] != "running":
                return {"ok": False,
                        "error": f"cannot pause run in status "
                                 f"{rec['status']!r} (must be 'running')"}
            control = self._controls.get(run_id)
            if control is None:
                return {"ok": False, "error": "run control already released"}
            control.request_pause()
            self._set_status_locked(run_id, "paused")
            return {"ok": True, "run_id": run_id, "status": "paused"}

    def resume_run(self, run_id: str) -> Dict[str, Any]:
        """Release a paused run."""
        with self._lock:
            rec = self._get_row_locked(run_id)
            if rec is None:
                return {"ok": False, "error": f"unknown run {run_id!r}"}
            if rec["status"] != "paused":
                return {"ok": False,
                        "error": f"cannot resume run in status "
                                 f"{rec['status']!r} (must be 'paused')"}
            control = self._controls.get(run_id)
            if control is None:
                return {"ok": False, "error": "run control already released"}
            control.resume()
            self._set_status_locked(run_id, "running")
            return {"ok": True, "run_id": run_id, "status": "running"}

    def stop_run(self, run_id: str) -> Dict[str, Any]:
        """Ask a running/paused run to stop; status -> stopping -> stopped."""
        with self._lock:
            rec = self._get_row_locked(run_id)
            if rec is None:
                return {"ok": False, "error": f"unknown run {run_id!r}"}
            if rec["status"] == "queued":
                return {"ok": False,
                        "error": "run is queued, not active; "
                                 "use cancel_run to drop it"}
            if rec["status"] not in ("running", "paused", "stopping"):
                return {"ok": False,
                        "error": f"cannot stop run in status "
                                 f"{rec['status']!r}"}
            control = self._controls.get(run_id)
            if control is None:
                return {"ok": False, "error": "run control already released"}
            control.request_stop()
            if rec["status"] != "stopping":
                self._set_status_locked(run_id, "stopping")
            return {"ok": True, "run_id": run_id, "status": "stopping"}

    def cancel_run(self, run_id: str) -> Dict[str, Any]:
        """Drop a queued run; it never executes. Queued-only."""
        with self._lock:
            rec = self._get_row_locked(run_id)
            if rec is None:
                return {"ok": False, "error": f"unknown run {run_id!r}"}
            if rec["status"] != "queued":
                return {"ok": False,
                        "error": f"cannot cancel run in status "
                                 f"{rec['status']!r} (must be 'queued'); "
                                 f"use stop_run for an active run"}
            now = time.time()
            self._db.execute(
                "UPDATE scheduler_runs SET status='cancelled', ended_at=? "
                "WHERE id=?",
                (now, run_id),
            )
            self._db.commit()
            try:
                self._pending.remove(run_id)
            except ValueError:
                pass  # worker already popped it; it will skip on status
            self._controls.pop(run_id, None)
            self._payloads.pop(run_id, None)
            self._push_event_locked(
                run_id, {"type": "status", "status": "cancelled", "at": now})
            return {"ok": True, "run_id": run_id, "status": "cancelled"}

    # ------------------------------------------------------------------
    # queries
    # ------------------------------------------------------------------
    def get_run(self, run_id: str) -> Optional[Dict[str, Any]]:
        """Full record for one run, including outcome. None if unknown."""
        with self._lock:
            rec = self._get_row_locked(run_id)
            if rec is None:
                return None
            if rec["status"] == "queued":
                try:
                    rec["queue_position"] = list(self._pending).index(run_id) + 1
                except ValueError:
                    rec["queue_position"] = None
            else:
                rec["queue_position"] = None
            return rec

    def list_runs(self, limit: int = 50) -> List[Dict[str, Any]]:
        """Recent runs: id/goal/status/queue position/timing (no outcomes)."""
        with self._lock:
            cur = self._db.execute(
                "SELECT id, goal, status, conversation_id, queued_at, "
                "started_at, ended_at, error FROM scheduler_runs "
                "ORDER BY queued_at DESC LIMIT ?",
                (int(limit),),
            )
            pending = list(self._pending)
            rows = []
            for (rid, goal, status, conv, qa, sa, ea, err) in cur.fetchall():
                pos = pending.index(rid) + 1 if status == "queued" and rid in pending else None
                rows.append({
                    "id": rid, "goal": goal, "status": status,
                    "conversation_id": conv, "queue_position": pos,
                    "queued_at": qa, "started_at": sa, "ended_at": ea,
                    "error": err,
                })
            return rows

    def events(self, run_id: str) -> Optional[List[dict]]:
        """Buffered stage/status events for one run (for SSE adoption)."""
        with self._lock:
            if run_id not in self._events:
                return None
            return list(self._events[run_id])

    def queue_depth(self) -> int:
        """Number of runs currently waiting (queued, not yet dequeued)."""
        with self._lock:
            return len(self._pending)

    def close(self, timeout: float = 10.0) -> None:
        """Shut down the worker thread. Queued runs stay 'queued' in sqlite."""
        with self._lock:
            self._shutdown = True
            self._cond.notify_all()
        self._worker.join(timeout=timeout)

    # ------------------------------------------------------------------
    # worker
    # ------------------------------------------------------------------
    def _worker_loop(self) -> None:
        while True:
            with self._cond:
                while not self._pending and not self._shutdown:
                    self._cond.wait(timeout=0.5)
                if self._shutdown:
                    return
                run_id = self._pending.popleft()
            # A run cancelled between enqueue and dequeue never executes:
            # the status check below skips it.
            with self._lock:
                rec = self._get_row_locked(run_id)
                if rec is None or rec["status"] != "queued":
                    continue
            self._run_one(run_id)

    def _run_one(self, run_id: str) -> None:
        try:
            engine = self._engine_lazy()
        except Exception as ex:  # engine failed to boot: honest error
            with self._lock:
                self._finish_locked(run_id, "error", None,
                                    error=f"engine boot failed: {ex!r}")
            return

        with self._lock:
            control = self._controls.get(run_id)
            payload = self._payloads.get(run_id)
            if control is None or payload is None:
                return  # cancelled concurrently; worker_loop already skips
            goal, examples, metadata = payload
            self._set_status_locked(run_id, "running",
                                    started_at=time.time())

        outcome = None
        outcome_error = ""
        # The worker thread installs the run's control as current AND passes
        # it via metadata, per the frozen run_control contract. handle()
        # converts RunStopped into a STOPPED outcome with the trace intact.
        _rc.set_current(control)
        try:
            iface = _TracingTaskInterface(engine)
            iface._event_cb = lambda ev: self._push_event(run_id, ev)
            outcome = asyncio.run(iface.handle(
                goal,
                examples=examples,
                metadata={**(metadata or {}), "run_control": control},
            ))
            final = self._classify(outcome, control)
        except Exception as ex:  # honest, never silent
            outcome_error = f"{type(ex).__name__}: {ex}"
            final = "stopped" if control.stop_requested else "error"
        finally:
            _rc.set_current(None)

        with self._lock:
            self._finish_locked(run_id, final, outcome, error=outcome_error)

    @staticmethod
    def _classify(outcome, control: RunControl) -> str:
        """Map a TaskOutcome (+ control state) onto an honest run status."""
        if outcome is not None:
            trace = getattr(outcome, "trace", None) or []
            if any(getattr(getattr(t, "stage", None), "value", None)
                   == Stage.STOPPED.value for t in trace):
                return "stopped"
            if control.stop_requested and not outcome.success:
                # Stop was in flight; do not mislabel as a plain failure.
                return "stopped"
            return "completed" if outcome.success else "failed"
        return "stopped" if control.stop_requested else "error"

    # ------------------------------------------------------------------
    # sqlite + event helpers (all require self._lock held)
    # ------------------------------------------------------------------
    def _get_row_locked(self, run_id: str) -> Optional[Dict[str, Any]]:
        cur = self._db.execute(
            "SELECT id, goal, status, conversation_id, queued_at, started_at,"
            " ended_at, outcome_json, error FROM scheduler_runs WHERE id=?",
            (run_id,),
        )
        row = cur.fetchone()
        if row is None:
            return None
        rec = {
            "id": row[0], "goal": row[1], "status": row[2],
            "conversation_id": row[3], "queued_at": row[4],
            "started_at": row[5], "ended_at": row[6], "error": row[8],
        }
        if row[7]:
            try:
                rec["outcome"] = json.loads(row[7])
            except Exception:
                rec["outcome"] = {"_unparseable": True}
        else:
            rec["outcome"] = None
        return rec

    def _set_status_locked(self, run_id: str, status: str,
                           started_at: Optional[float] = None) -> None:
        now = time.time()
        if started_at is not None:
            self._db.execute(
                "UPDATE scheduler_runs SET status=?, started_at=? WHERE id=?",
                (status, started_at, run_id),
            )
        else:
            self._db.execute(
                "UPDATE scheduler_runs SET status=? WHERE id=?",
                (status, run_id),
            )
        self._db.commit()
        self._push_event_locked(
            run_id, {"type": "status", "status": status, "at": now})

    def _finish_locked(self, run_id: str, status: str, outcome,
                       error: str = "") -> None:
        now = time.time()
        outcome_json = None
        if outcome is not None:
            try:
                outcome_json = json.dumps(outcome.as_dict(), default=str)
            except Exception as ex:
                outcome_json = json.dumps(
                    {"_outcome_serialize_failed": repr(ex)})
        self._db.execute(
            "UPDATE scheduler_runs SET status=?, ended_at=?, outcome_json=?, "
            "error=? WHERE id=?",
            (status, now, outcome_json, error or None, run_id),
        )
        self._db.commit()
        self._push_event_locked(
            run_id, {"type": "status", "status": status, "at": now})
        # The run is terminal: release its control and payload. Events stay
        # buffered for SSE adoption (bounded; oldest terminal runs pruned).
        self._controls.pop(run_id, None)
        self._payloads.pop(run_id, None)
        self._prune_events_locked()

    def _push_event(self, run_id: str, ev: dict) -> None:
        """Thread-safe event push (for the worker's trace callback)."""
        with self._lock:
            self._push_event_locked(run_id, ev)

    def _push_event_locked(self, run_id: str, ev: dict) -> None:
        buf = self._events.get(run_id)
        if buf is None:
            return
        ev = dict(ev)
        ev.setdefault("run_id", run_id)
        buf.append(ev)
        if len(buf) > _MAX_EVENTS_PER_RUN:
            del buf[:len(buf) - _MAX_EVENTS_PER_RUN]

    def _prune_events_locked(self) -> None:
        if len(self._events) <= _MAX_EVENT_RUNS_KEPT:
            return
        # Caller holds self._lock (RLock).
        cur = self._db.execute(
            "SELECT id FROM scheduler_runs WHERE status IN "
            "('completed','failed','stopped','cancelled','error') "
            "ORDER BY ended_at ASC")
        for (rid,) in cur.fetchall():
            if len(self._events) <= _MAX_EVENT_RUNS_KEPT:
                break
            self._events.pop(rid, None)
