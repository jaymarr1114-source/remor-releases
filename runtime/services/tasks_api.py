"""
swarm_engine/services/tasks_api.py

Contract 1 — Scheduled/Queued Tasks: product-level task API over
swarm_engine.services.scheduler.RunScheduler.

What is real here
-----------------
* list_tasks()      — every scheduled task, statuses read live from the
                      scheduler's own records (never cached, never canned).
* schedule_task()   — immediate dispatch goes straight to
                      RunScheduler.submit(); delayed dispatch uses REAL
                      machinery: the due timestamp is persisted in sqlite
                      and a dispatcher thread claims due rows atomically
                      (UPDATE ... WHERE status='pending', rowcount-checked)
                      and submits them to the real scheduler. A pending row
                      survives process death: a fresh service instance over
                      the same DB re-dispatches it.
* cancel_task()     — pending (not-yet-due) tasks are cancelled in our own
                      table and never dispatch; submitted tasks delegate to
                      the scheduler's real cancel_run/stop_run.
* queue_status()    — depth + active + per-status counts from the
                      scheduler's real records, plus pending scheduled
                      tasks and the next due timestamp.

Honest boundaries
-----------------
* Delayed dispatch is one-shot only. Recurring schedules are
  HONESTLY-UNAVAILABLE (code "recurring_schedule"): the runtime has no
  cron/interval substrate.
* A due task is dispatched only while this service (and its dispatcher
  thread) is alive; after a process restart the rows persist and the next
  service instance over the same DB picks them up.
* run_at may be an epoch float/int or an ISO-8601 string. A naive ISO
  string is interpreted as UTC (documented, not guessed at silently).
  run_at <= now dispatches immediately — a past timestamp is "due now",
  not an error.

Route-table convention (for the coordinator composing http_adapter):
handlers take a single body_dict; for GET routes the composing adapter
must merge the query string into body_dict before calling.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from swarm_engine.services.contract_types import contract_unavailable
from swarm_engine.services.scheduler import ACTIVE_STATUSES, RunScheduler

_SCHEMA = """
CREATE TABLE IF NOT EXISTS scheduled_tasks (
    id              TEXT PRIMARY KEY,
    goal            TEXT NOT NULL,
    examples_json   TEXT,
    metadata_json   TEXT,
    conversation_id TEXT,
    run_at          REAL,          -- NULL = immediate; else due epoch (UTC)
    created_at      REAL NOT NULL,
    status          TEXT NOT NULL, -- pending | submitted | cancelled | failed
    run_id          TEXT,          -- scheduler run id once dispatched
    error           TEXT
)
"""

_TERMINAL_OWN = frozenset({"cancelled", "failed"})


def _parse_run_at(run_at) -> float:
    """Normalize run_at (epoch or ISO-8601) to an epoch float (UTC).

    Raises ValueError on anything unparseable.
    """
    if isinstance(run_at, bool):
        raise ValueError("run_at must be an epoch number or ISO-8601 string")
    if isinstance(run_at, (int, float)):
        return float(run_at)
    if isinstance(run_at, str):
        text = run_at.strip()
        try:
            return float(text)  # epoch as string
        except ValueError:
            pass
        try:
            dt = datetime.fromisoformat(text)
        except ValueError:
            raise ValueError(f"run_at is not an epoch or ISO-8601: {run_at!r}")
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)  # naive -> UTC, documented
        return dt.timestamp()
    raise ValueError(
        f"run_at must be an epoch number or ISO-8601 string, got "
        f"{type(run_at).__name__}")


class ScheduledTaskService:
    """Task-level product API over a RunScheduler."""

    def __init__(self, db_path: str, scheduler: RunScheduler,
                 dispatcher_enabled: bool = True,
                 poll_interval: float = 0.25) -> None:
        self.db_path = db_path
        self.scheduler = scheduler
        self.poll_interval = max(0.05, float(poll_interval))
        self._lock = threading.RLock()
        self._shutdown = False
        self._db = sqlite3.connect(db_path, check_same_thread=False)
        with self._lock:
            self._db.execute(_SCHEMA)
            self._db.commit()
        self._dispatcher = None
        if dispatcher_enabled:
            self._dispatcher = threading.Thread(
                target=self._dispatch_loop, name="remor-task-dispatcher",
                daemon=True)
            self._dispatcher.start()

    # ------------------------------------------------------------------
    # public API
    # ------------------------------------------------------------------
    def schedule_task(self, goal: str, run_at=None, examples=None,
                      metadata=None,
                      conversation_id: Optional[str] = None) -> Dict[str, Any]:
        """Schedule a task. Immediate when run_at is None or <= now.

        Returns {"ok": True, "task_id": ..., "status": "submitted"|"scheduled",
                 "run_id": ...|None, "run_at": ...|None}.
        """
        if not isinstance(goal, str) or not goal.strip():
            return {"ok": False,
                    "error": "goal must be a non-empty string"}
        due = None
        if run_at is not None:
            try:
                due = _parse_run_at(run_at)
            except ValueError as exc:
                return {"ok": False, "error": str(exc)}
        task_id = uuid.uuid4().hex
        now = time.time()
        ex_json = json.dumps(examples, default=str) if examples is not None else None
        md_json = json.dumps(metadata, default=str) if metadata is not None else None

        with self._lock:
            if due is not None and due > now:
                self._db.execute(
                    "INSERT INTO scheduled_tasks "
                    "(id, goal, examples_json, metadata_json, conversation_id,"
                    " run_at, created_at, status)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, 'pending')",
                    (task_id, goal, ex_json, md_json, conversation_id,
                     due, now),
                )
                self._db.commit()
                return {"ok": True, "task_id": task_id,
                        "status": "scheduled", "run_id": None,
                        "run_at": due}
            # Immediate (or already due): straight to the real scheduler.
            sub = self.scheduler.submit(
                goal, examples=examples, metadata=metadata,
                conversation_id=conversation_id)
            if not sub.get("ok"):
                return {"ok": False,
                        "error": f"scheduler refused: {sub.get('error')}"}
            run_id = sub["run_id"]
            self._db.execute(
                "INSERT INTO scheduled_tasks "
                "(id, goal, examples_json, metadata_json, conversation_id,"
                " run_at, created_at, status, run_id)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, 'submitted', ?)",
                (task_id, goal, ex_json, md_json, conversation_id,
                 due, now, run_id),
            )
            self._db.commit()
            return {"ok": True, "task_id": task_id, "status": "submitted",
                    "run_id": run_id, "run_at": due}

    def get_task(self, task_id: str) -> Dict[str, Any]:
        """One task with a live status (pending read from our table,
        submitted read from the scheduler's own records)."""
        with self._lock:
            row = self._row_locked(task_id)
            if row is None:
                return {"ok": False,
                        "error": f"unknown task {task_id!r}"}
            return {"ok": True, "task": self._enrich_locked(row)}

    def list_tasks(self, limit: int = 50) -> Dict[str, Any]:
        """All tasks, newest first, each with an honest live status."""
        with self._lock:
            cur = self._db.execute(
                "SELECT * FROM scheduled_tasks ORDER BY created_at DESC "
                "LIMIT ?", (max(1, int(limit)),))
            cols = [d[0] for d in cur.description]
            return {"ok": True,
                    "tasks": [self._enrich_locked(dict(zip(cols, r)))
                              for r in cur.fetchall()]}

    def cancel_task(self, task_id: str) -> Dict[str, Any]:
        """Cancel a task.

        Pending (not-yet-dispatched) -> cancelled in our table; it will
        never dispatch. Submitted -> delegates to the scheduler's real
        cancel_run/stop_run, whose honest result is returned verbatim.
        """
        with self._lock:
            row = self._row_locked(task_id)
            if row is None:
                return {"ok": False,
                        "error": f"unknown task {task_id!r}"}
            if row["status"] == "pending":
                n = self._db.execute(
                    "UPDATE scheduled_tasks SET status='cancelled' "
                    "WHERE id=? AND status='pending'",
                    (task_id,)).rowcount
                self._db.commit()
                if n == 1:
                    return {"ok": True, "task_id": task_id,
                            "status": "cancelled",
                            "note": "was pending; will never dispatch"}
                # Lost the race: the dispatcher claimed it concurrently.
                row = self._row_locked(task_id)
            if row["status"] == "submitted" and not row["run_id"]:
                # Millisecond claim window: dispatcher claimed the row but
                # has not recorded the scheduler run id yet.
                return {"ok": False, "task_id": task_id,
                        "status": "submitted",
                        "error": "dispatch in flight; retry shortly"}
            if row["status"] == "submitted" and row["run_id"]:
                run = self.scheduler.get_run(row["run_id"])
                if run is None:
                    return {"ok": False, "task_id": task_id,
                            "error": "scheduler has no record of the run"}
                status = run["status"]
                if status == "queued":
                    res = self.scheduler.cancel_run(row["run_id"])
                elif status in ("running", "paused", "stopping"):
                    res = self.scheduler.stop_run(row["run_id"])
                else:
                    return {"ok": False, "task_id": task_id,
                            "status": status,
                            "error": f"task already terminal in scheduler "
                                     f"status {status!r}"}
                out = {"ok": res.get("ok", False), "task_id": task_id,
                       "scheduler": res}
                if not res.get("ok"):
                    out["error"] = res.get("error")
                return out
            if row["status"] in ("cancelled", "failed"):
                return {"ok": False, "task_id": task_id,
                        "status": row["status"],
                        "error": f"task already {row['status']}"}
            return {"ok": False, "task_id": task_id,
                    "error": f"task in unexpected status {row['status']!r}"}

    def queue_status(self) -> Dict[str, Any]:
        """Depth + active + per-status counts, all from live records."""
        counts: Dict[str, int] = {}
        active = 0
        # Read the scheduler's own table directly: it is the source of
        # truth, and list_runs() would page it. Short timeout; brief
        # writer holds are the only contention and sqlite serializes them.
        conn = sqlite3.connect(self.scheduler.db_path, timeout=5.0)
        try:
            for status, n in conn.execute(
                    "SELECT status, COUNT(*) FROM scheduler_runs "
                    "GROUP BY status"):
                counts[status] = n
                if status in ACTIVE_STATUSES:
                    active += n
        finally:
            conn.close()
        with self._lock:
            pending = self._db.execute(
                "SELECT COUNT(*) FROM scheduled_tasks "
                "WHERE status='pending'").fetchone()[0]
            nxt = self._db.execute(
                "SELECT MIN(run_at) FROM scheduled_tasks "
                "WHERE status='pending'").fetchone()[0]
        return {"ok": True,
                "depth": self.scheduler.queue_depth(),
                "active": active,
                "counts": counts,
                "pending_scheduled": pending,
                "next_due_at": nxt}

    def schedule_recurring(self, *args, **kwargs) -> Dict[str, Any]:
        """Recurring schedules: honestly unavailable (typed payload)."""
        return contract_unavailable(
            "recurring_schedule",
            "Delayed dispatch is one-shot only: the runtime has no "
            "cron/interval substrate, and the in-process dispatcher does "
            "not re-arm a schedule after it fires.",
            "persistent cron-like recurrence scheduler")

    def close(self, timeout: float = 5.0) -> None:
        """Stop the dispatcher thread (pending rows stay in sqlite)."""
        if self._dispatcher is None:
            return
        with self._lock:
            self._shutdown = True
        self._dispatcher.join(timeout=timeout)

    # ------------------------------------------------------------------
    # dispatcher: sqlite-persisted due timestamps -> real scheduler submit
    # ------------------------------------------------------------------
    def _dispatch_loop(self) -> None:
        while True:
            with self._lock:
                if self._shutdown:
                    return
            try:
                self._dispatch_due()
            except Exception:
                pass  # never let the dispatcher die on a bad row
            time.sleep(self.poll_interval)

    def _dispatch_due(self) -> None:
        now = time.time()
        with self._lock:
            due_rows = self._db.execute(
                "SELECT id, goal, examples_json, metadata_json, "
                "conversation_id FROM scheduled_tasks "
                "WHERE status='pending' AND run_at <= ? "
                "ORDER BY run_at ASC",
                (now,)).fetchall()
            claimed = []
            for row in due_rows:
                tid = row[0]
                n = self._db.execute(
                    "UPDATE scheduled_tasks SET status='submitted', "
                    "run_id=NULL WHERE id=? AND status='pending'",
                    (tid,)).rowcount
                self._db.commit()
                if n == 1:
                    claimed.append(tid)
        # Submit outside the table lock; each claim is already atomic.
        for tid in claimed:
            try:
                self._dispatch_one(tid)
            except Exception as exc:  # noqa: BLE001 - one bad row must
                # never skip the rest of the batch: record the failure
                # on the row and continue. (A corrupt payload used to
                # raise out of _dispatch_one here, stranding every
                # batch-mate claimed after it as 'submitted'/NULL,
                # forever invisible to the 'pending' due-scan.)
                try:
                    with self._lock:
                        self._db.execute(
                            "UPDATE scheduled_tasks SET status='failed', "
                            "error=? WHERE id=?",
                            (f"{type(exc).__name__}: {exc}", tid))
                        self._db.commit()
                except Exception:
                    pass

    def _dispatch_one(self, task_id: str) -> None:
        with self._lock:
            cur = self._db.execute(
                "SELECT goal, examples_json, metadata_json, conversation_id "
                "FROM scheduled_tasks WHERE id=?", (task_id,))
            row = cur.fetchone()
        if row is None:
            return
        goal, ex_json, md_json, conv = row
        try:
            # Payload decode lives inside the try: a corrupt row (only
            # reachable via out-of-band DB writes -- schedule_task
            # always stores valid JSON) must fail THIS task loudly,
            # never raise out of the dispatch batch and strand its
            # batch-mates as claimed-but-undispatched.
            examples = json.loads(ex_json) if ex_json else None
            metadata = json.loads(md_json) if md_json else None
            sub = self.scheduler.submit(goal, examples=examples,
                                        metadata=metadata,
                                        conversation_id=conv)
        except Exception as exc:  # never strand a claimed row silently
            sub = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        with self._lock:
            if sub.get("ok"):
                self._db.execute(
                    "UPDATE scheduled_tasks SET run_id=?, error=NULL "
                    "WHERE id=?", (sub["run_id"], task_id))
            else:
                self._db.execute(
                    "UPDATE scheduled_tasks SET status='failed', error=? "
                    "WHERE id=?",
                    (f"scheduler refused at dispatch: {sub.get('error')}",
                     task_id))
            self._db.commit()

    # ------------------------------------------------------------------
    # sqlite helpers (lock held by callers)
    # ------------------------------------------------------------------
    def _row_locked(self, task_id: str) -> Optional[Dict[str, Any]]:
        cur = self._db.execute(
            "SELECT * FROM scheduled_tasks WHERE id=?", (task_id,))
        row = cur.fetchone()
        if row is None:
            return None
        return dict(zip([d[0] for d in cur.description], row))

    def _enrich_locked(self, row: Dict[str, Any]) -> Dict[str, Any]:
        """Attach the honest live status: pending from our table,
        submitted straight from the scheduler's own run record."""
        task = {
            "task_id": row["id"], "goal": row["goal"],
            "conversation_id": row["conversation_id"],
            "run_at": row["run_at"], "created_at": row["created_at"],
            "status": row["status"], "run_id": row["run_id"],
            "error": row["error"],
        }
        if row["status"] == "submitted" and row["run_id"]:
            run = self.scheduler.get_run(row["run_id"])
            if run is None:
                task["run_status"] = "unknown"
                task["run_status_note"] = ("scheduler has no record of this "
                                           "run id")
            else:
                task["run_status"] = run["status"]
                task["run_started_at"] = run["started_at"]
                task["run_ended_at"] = run["ended_at"]
                task["queue_position"] = run.get("queue_position")
        return task


# ----------------------------------------------------------------------
# Route table for the coordinator (do NOT wire into http_adapter here).
# Handlers take a single body_dict; for GET routes the composing adapter
# merges the query string into body_dict before calling.
# ----------------------------------------------------------------------
def routes_for_tasks_api(service: "ScheduledTaskService") -> Dict:
    def _schedule(body):
        return service.schedule_task(
            goal=body.get("goal"),
            run_at=body.get("run_at"),
            examples=body.get("examples"),
            metadata=body.get("metadata"),
            conversation_id=body.get("conversation_id"),
        )

    def _list(body):
        try:
            limit = int(body.get("limit", 50))
        except (TypeError, ValueError):
            limit = 50
        return service.list_tasks(limit=limit)

    def _get(body):
        tid = body.get("task_id")
        if not tid:
            return {"ok": False, "error": "task_id is required"}
        return service.get_task(tid)

    def _cancel(body):
        tid = body.get("task_id")
        if not tid:
            return {"ok": False, "error": "task_id is required"}
        return service.cancel_task(tid)

    def _recurring(body):
        return service.schedule_recurring()

    return {
        ("GET", "/api/tasks"): _list,
        ("POST", "/api/tasks"): _schedule,
        ("GET", "/api/tasks/item"): _get,
        ("POST", "/api/tasks/cancel"): _cancel,
        ("GET", "/api/tasks/queue"): lambda body: service.queue_status(),
        ("POST", "/api/tasks/recurring"): _recurring,
    }
