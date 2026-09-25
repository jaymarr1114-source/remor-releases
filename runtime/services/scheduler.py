"""
swarm_engine/services/scheduler.py

RunScheduler — real multi-run concurrency management for the REMOR runtime.

Tamper-evidence (2026-09-25, Batch 11 cross-DB trust scope):
scheduler_runs is one of the three integrity heads. Status changes are
APPEND-ONLY chained row versions -- no in-place UPDATE -- and the whole
table carries a head digest covered by the service anchor journal
(CrossDbAnchor in runtime/governance/anchor.py). Legacy pre-chain
databases are migrated once at open (guarded rebuild, attested via
transition(reason="migration") when an anchor is attached).

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
import hashlib
import json
import os
import sqlite3
import threading
import time
import uuid
from typing import Any, Dict, List, Optional, Tuple

from swarm_engine.governance.anchor import (
    AnchorVerifyError,
    ChainAuditError,
    CrossDbAnchor,
    LegacySchemaError,
)


# ---------------------------------------------------------------------------
# Hash-chain machinery (mirrors runtime/agent_org/store.py OrgStore)


_CHAIN_FIELDS = [
    "id", "goal", "status", "conversation_id", "queued_at",
    "started_at", "ended_at", "outcome_json", "error",
]

_GENESIS_DIGEST = hashlib.sha256(
    b"REMOR|scheduler_runs|genesis").hexdigest()


def _canonical(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=True)


def _row_digest(fields: Dict[str, Any], prev_digest: str) -> str:
    return hashlib.sha256(
        (_canonical(fields) + prev_digest).encode("utf-8")).hexdigest()


def _chain_row(run_id: str, data: Dict[str, Any],
               prev_digest: str) -> Tuple[Dict[str, Any], str]:
    ordered = {k: data.get(k) for k in _CHAIN_FIELDS}
    ordered["id"] = run_id
    digest = _row_digest(ordered, prev_digest)
    return ordered, digest


def _head_digest(rows: List[Tuple[int, str, str, Dict[str, Any]]]) -> str:
    h = hashlib.sha256()
    for seq, prev_digest, row_digest, fields in rows:
        h.update(_canonical(
            [seq, prev_digest, row_digest, fields]).encode("utf-8"))
    return h.hexdigest()

from swarm_engine.core.task_interface import Stage, UniversalTaskInterface
from swarm_engine.services import run_control as _rc
from swarm_engine.services.run_control import RunControl

TERMINAL_STATUSES = frozenset(
    {"completed", "failed", "stopped", "cancelled", "error"}
)
ACTIVE_STATUSES = frozenset({"running", "paused", "stopping"})

_SCHEMA = """
CREATE TABLE IF NOT EXISTS scheduler_runs (
    seq             INTEGER PRIMARY KEY AUTOINCREMENT,
    id              TEXT NOT NULL,
    prev_digest     TEXT NOT NULL,
    row_digest      TEXT NOT NULL,
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
_SCHEMA_INDEX = (
    "CREATE INDEX IF NOT EXISTS idx_scheduler_runs_id "
    "ON scheduler_runs(id)"
)

#: Columns of the pre-chain (legacy) scheduler_runs table. A database
#: whose scheduler_runs table lacks the chain columns is refused at
#: open -- see LegacySchemaError and migrate_legacy_scheduler_db().
_LEGACY_REQUIRED_ABSENT = ("seq", "prev_digest", "row_digest")

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
        self.db_path = os.path.abspath(db_path)
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
        # Service anchor attachment (CrossDbAnchor). None in unit tests /
        # anchor-free drivers; chaining is always on regardless.
        self._anchor = None
        self._authority = "scheduler"

        self._db = sqlite3.connect(self.db_path, check_same_thread=False)
        with self._lock:
            cols = [r[1] for r in self._db.execute(
                "PRAGMA table_info(scheduler_runs)")]
            if cols and all(c not in cols
                            for c in _LEGACY_REQUIRED_ABSENT):
                raise LegacySchemaError(
                    "scheduler_runs has the pre-chain schema (id PRIMARY "
                    "KEY, no chain columns). Refusing to open: run "
                    "migrate_legacy_scheduler_db() explicitly to adopt "
                    "this database (trust-on-first-use).")
            self._db.execute(_SCHEMA)
            self._db.execute(_SCHEMA_INDEX)
            self._db.commit()
            # A non-empty chain table must verify on open -- a tampered
            # chain is never silently adopted, anchor or not.
            if self._db.execute(
                    "SELECT COUNT(*) FROM scheduler_runs").fetchone()[0]:
                ok, msg = self.audit()
                if not ok:
                    raise ChainAuditError(
                        f"scheduler_runs chain audit failed on open: {msg}")

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

            def _write_submit() -> None:
                self._append_version_locked(run_id, {
                    "goal": goal,
                    "status": "queued",
                    "conversation_id": conversation_id,
                    "queued_at": now,
                })
                self._db.commit()

            self._attested_write_locked(_write_submit, "submit")
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

            def _write_cancel() -> None:
                latest = self._latest_row_locked(run_id)
                data = dict(zip(_CHAIN_FIELDS, latest))
                data["status"] = "cancelled"
                data["ended_at"] = now
                self._append_version_locked(run_id, data)
                self._db.commit()

            self._attested_write_locked(_write_cancel, "cancel_run")
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
                "SELECT s.id, s.goal, s.status, s.conversation_id, "
                "s.queued_at, s.started_at, s.ended_at, s.error "
                "FROM scheduler_runs s JOIN "
                "(SELECT id, MAX(seq) AS m FROM scheduler_runs "
                "GROUP BY id) t ON s.id = t.id AND s.seq = t.m "
                "ORDER BY s.queued_at DESC LIMIT ?",
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

    # ------------------------------------------------------------------
    # tamper-evidence: anchor attachment + hash chain
    # ------------------------------------------------------------------
    def attach_anchor(self, anchor, authority: str = "scheduler") -> None:
        """Attach a CrossDbAnchor covering this store's chain.

        Verifies the existing chain first: a tampered table is never
        attached to a journal. The journal init/verify/refuse decision
        itself lives in build_services(), not here.
        """
        with self._lock:
            ok, msg = self.audit()
            if not ok:
                raise ChainAuditError(
                    f"refusing to attach anchor: scheduler_runs chain "
                    f"audit failed: {msg}")
            self._anchor = anchor
            self._authority = authority or "scheduler"

    def _attested_write_locked(self, write_fn, op: str) -> None:
        """Run write_fn() (chained appends) under the anchor discipline.

        Caller must hold self._lock. With no anchor attached this is a
        plain chained write (unit-test path). With an anchor: pre-write
        verify of live heads against the journal tip (fail closed --
        never silently re-anchor already-tampered data), then the
        appends, then a post-write internal chain audit, then
        anchor_all(reason="attest"). Lock order is always
        self._lock -> CrossDbAnchor._ATTEST_LOCK.
        """
        anchor = self._anchor
        if anchor is None:
            write_fn()
            return
        with CrossDbAnchor._ATTEST_LOCK:
            ok, msg = anchor.verify_all()
            if not ok:
                raise AnchorVerifyError(
                    f"scheduler {op}: pre-write anchor verify failed: "
                    f"{msg}")
            write_fn()
            audits = anchor.audit_providers()
            bad = {s: m for s, (ok2, m) in audits.items() if not ok2}
            if bad:
                raise AnchorVerifyError(
                    f"scheduler {op}: post-write chain audit failed: "
                    f"{bad}")
            anchor.anchor_all(reason="attest",
                              authority=self._authority)

    def _tip_digest_locked(self) -> str:
        """row_digest of the chain tip (max seq), or genesis if empty."""
        row = self._db.execute(
            "SELECT row_digest FROM scheduler_runs ORDER BY seq DESC "
            "LIMIT 1").fetchone()
        return row[0] if row else _GENESIS_DIGEST

    def _append_version_locked(self, run_id: str,
                               data: Dict[str, Any]) -> None:
        """Append one chained version row. Caller holds self._lock."""
        prev = self._tip_digest_locked()
        ordered, digest = _chain_row(run_id, data, prev)
        self._db.execute(
            "INSERT INTO scheduler_runs (id, prev_digest, row_digest, "
            "goal, status, conversation_id, queued_at, started_at, "
            "ended_at, outcome_json, error) VALUES "
            "(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (run_id, prev, digest,
             ordered["goal"], ordered["status"],
             ordered["conversation_id"], ordered["queued_at"],
             ordered["started_at"], ordered["ended_at"],
             ordered["outcome_json"], ordered["error"]),
        )

    def _latest_row_locked(self, run_id: str) -> Optional[sqlite3.Row]:
        """Latest version of one run as a _CHAIN_FIELDS-ordered tuple."""
        return self._db.execute(
            "SELECT id, goal, status, conversation_id, queued_at, "
            "started_at, ended_at, outcome_json, error "
            "FROM scheduler_runs WHERE id = ? "
            "ORDER BY seq DESC LIMIT 1",
            (run_id,),
        ).fetchone()

    # -- provider surface for CrossDbAnchor (no store locks inside:
    #    collection runs under CrossDbAnchor._ATTEST_LOCK; taking
    #    self._lock here would invert the lock order) -------------------
    def audit(self) -> Tuple[bool, str]:
        """Recompute the whole scheduler_runs chain. (ok, msg)."""
        rows = self._db.execute(
            "SELECT seq, id, prev_digest, row_digest, goal, status, "
            "conversation_id, queued_at, started_at, ended_at, "
            "outcome_json, error FROM scheduler_runs ORDER BY seq"
        ).fetchall()
        expect_prev = _GENESIS_DIGEST
        expect_seq = 1
        for row in rows:
            (seq, run_id, prev_digest, row_digest, goal, status,
             conv, qa, sa, ea, ojson, err) = row
            if seq != expect_seq:
                return False, (
                    f"chain broken: expected seq {expect_seq}, found "
                    f"{seq} (deletion or splice)")
            if prev_digest != expect_prev:
                return False, (
                    f"chain broken at seq {seq}: prev_digest mismatch")
            ordered = {"id": run_id, "goal": goal, "status": status,
                       "conversation_id": conv, "queued_at": qa,
                       "started_at": sa, "ended_at": ea,
                       "outcome_json": ojson, "error": err}
            if _row_digest(ordered, prev_digest) != row_digest:
                return False, (
                    f"chain broken at seq {seq}: row_digest mismatch "
                    f"(row tampered)")
            expect_prev = row_digest
            expect_seq = seq + 1
        return True, f"scheduler_runs chain ok ({len(rows)} version rows)"

    def audit_all(self) -> Dict[str, Tuple[bool, str]]:
        return {"scheduler_runs": self.audit()}

    def head_digest(self, table: str = "scheduler_runs") -> str:
        """Full-table head digest (GENESIS when empty)."""
        if table != "scheduler_runs":
            raise KeyError(f"unknown table {table!r}")
        rows = self._db.execute(
            "SELECT seq, prev_digest, row_digest, id, goal, status, "
            "conversation_id, queued_at, started_at, ended_at, "
            "outcome_json, error FROM scheduler_runs ORDER BY seq"
        ).fetchall()
        if not rows:
            return _GENESIS_DIGEST
        h = hashlib.sha256()
        for (seq, prev_digest, row_digest, run_id, goal, status,
             conv, qa, sa, ea, ojson, err) in rows:
            h.update(_canonical(
                [seq, prev_digest, row_digest,
                 {"id": run_id, "goal": goal, "status": status,
                  "conversation_id": conv, "queued_at": qa,
                  "started_at": sa, "ended_at": ea,
                  "outcome_json": ojson, "error": err}]
            ).encode("utf-8"))
        return h.hexdigest()

    def history(self, run_id: str) -> List[Dict[str, Any]]:
        """Every chained version of one run, oldest first (audit aid)."""
        with self._lock:
            rows = self._db.execute(
                "SELECT seq, status, queued_at, started_at, ended_at, "
                "error FROM scheduler_runs WHERE id = ? ORDER BY seq",
                (run_id,),
            ).fetchall()
        return [
            {"seq": r[0], "status": r[1], "queued_at": r[2],
             "started_at": r[3], "ended_at": r[4], "error": r[5]}
            for r in rows
        ]

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
        row = self._latest_row_locked(run_id)
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

        def _write() -> None:
            latest = self._latest_row_locked(run_id)
            data = dict(zip(_CHAIN_FIELDS, latest))
            data["status"] = status
            if started_at is not None:
                data["started_at"] = started_at
            self._append_version_locked(run_id, data)
            self._db.commit()

        self._attested_write_locked(_write,
                                    f"set_status->{status}")
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

        def _write() -> None:
            latest = self._latest_row_locked(run_id)
            data = dict(zip(_CHAIN_FIELDS, latest))
            data["status"] = status
            data["ended_at"] = now
            data["outcome_json"] = outcome_json
            data["error"] = error or None
            self._append_version_locked(run_id, data)
            self._db.commit()

        self._attested_write_locked(_write, f"finish->{status}")
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
            "SELECT s.id FROM scheduler_runs s JOIN "
            "(SELECT id, MAX(seq) AS m FROM scheduler_runs GROUP BY id) t "
            "ON s.id = t.id AND s.seq = t.m "
            "WHERE s.status IN "
            "('completed','failed','stopped','cancelled','error') "
            "ORDER BY s.ended_at ASC")
        for (rid,) in cur.fetchall():
            if len(self._events) <= _MAX_EVENT_RUNS_KEPT:
                break
            self._events.pop(rid, None)


# ---------------------------------------------------------------------------
# Explicit legacy migration (operator step, never silent)
# ---------------------------------------------------------------------------
def migrate_legacy_scheduler_db(db_path: str) -> Dict[str, Any]:
    """Rebuild a pre-chain scheduler_runs table as a hash-chained table.

    This is the ONLY adoption path for legacy databases: RunScheduler
    refuses to open the pre-chain schema (LegacySchemaError). Calling
    this function is the operator's explicit trust-on-first-use decision
    -- pre-chain rows carry no tamper evidence, so their content is
    re-anchored as-is and the adoption is loudly reported.

    Guarded: the rebuild runs in one transaction (new table -> copy
    chained in rowid order -> drop old -> rename); any failure rolls
    back and the legacy table is untouched.
    """
    db_path = os.path.abspath(db_path)
    conn = sqlite3.connect(db_path, timeout=30)
    try:
        cols = [r[1] for r in conn.execute(
            "PRAGMA table_info(scheduler_runs)")]
        if not cols:
            return {"ok": False,
                    "error": "no scheduler_runs table; nothing to migrate"}
        if any(c in cols for c in _LEGACY_REQUIRED_ABSENT):
            return {"ok": True, "migrated": False,
                    "detail": "already the chain schema"}
        legacy_cols = ["id", "goal", "status", "conversation_id",
                       "queued_at", "started_at", "ended_at",
                       "outcome_json", "error"]
        if any(c not in cols for c in legacy_cols):
            return {"ok": False,
                    "error": f"unrecognized scheduler_runs schema: {cols}"}
        rows = conn.execute(
            "SELECT id, goal, status, conversation_id, queued_at, "
            "started_at, ended_at, outcome_json, error "
            "FROM scheduler_runs ORDER BY rowid").fetchall()
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(_SCHEMA.replace(
                "scheduler_runs", "scheduler_runs_new"))
            conn.execute(_SCHEMA_INDEX.replace(
                "scheduler_runs", "scheduler_runs_new").replace(
                "idx_scheduler_runs_id", "idx_scheduler_runs_new_id"))
            prev = _GENESIS_DIGEST
            for row in rows:
                data = dict(zip(legacy_cols, row))
                ordered, digest = _chain_row(data["id"], data, prev)
                conn.execute(
                    "INSERT INTO scheduler_runs_new "
                    "(id, prev_digest, row_digest, goal, status, "
                    "conversation_id, queued_at, started_at, ended_at, "
                    "outcome_json, error) VALUES "
                    "(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (data["id"], prev, digest, ordered["goal"],
                     ordered["status"], ordered["conversation_id"],
                     ordered["queued_at"], ordered["started_at"],
                     ordered["ended_at"], ordered["outcome_json"],
                     ordered["error"]),
                )
                prev = digest
            conn.execute("DROP TABLE scheduler_runs")
            conn.execute(
                "ALTER TABLE scheduler_runs_new RENAME TO scheduler_runs")
            conn.execute(
                "DROP INDEX IF EXISTS idx_scheduler_runs_new_id")
            conn.execute(_SCHEMA_INDEX)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        return {"ok": True, "migrated": True,
                "detail": f"re-chained {len(rows)} legacy row(s) in "
                          f"rowid order; content adopted as-is "
                          f"(trust-on-first-use)"}
    finally:
        conn.close()
