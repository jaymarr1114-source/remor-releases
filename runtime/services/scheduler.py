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

Resource bounds (Phase 3 hardening, 2026-09-25):
  * FIFO queue depth capped at _MAX_PENDING (10,000); submit() beyond
    that refuses with an explicit "queue full" error -- never silent
    growth.
  * Per-run in-memory event buffer capped at _MAX_EVENTS_PER_RUN
    (10,000); overflow drops the OLDEST events and increments a
    per-run dropped-events counter surfaced by events_summary().
  * In-memory event buffers for terminal runs are LRU-evicted (by
    ended_at) beyond _MAX_EVENT_RUNS_KEPT (1,000) runs. _controls and
    _payloads only ever hold non-terminal runs (released on finish /
    cancel), so they are bounded by _MAX_PENDING + 1 in-flight.
  * list_runs() clamps its caller-supplied limit to _MAX_LIST_RUNS.
  * Per-event stage detail truncated to _MAX_TRACE_DETAIL chars;
    error strings to _MAX_ERROR_LEN chars; serialized outcome payloads
    to _MAX_OUTCOME_JSON_BYTES bytes (oversized outcomes are stored as
    an explicit truncation marker, never silently clipped mid-JSON).
  * The sqlite scheduler_runs table is deliberately NOT pruned. It is
    an append-only hash-chained audit log (one of the three integrity
    heads); deleting or NULL-ing a row breaks seq continuity and row
    digests, and re-chaining would be rewriting history -- a trust
    violation. Chain integrity > disk retention by design; operators
    needing disk bounds archive via history() and start a fresh DB.

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


def _truncate_error(text: str) -> str:
    """Cap an error string at _MAX_ERROR_LEN chars (explicit marker)."""
    if len(text) <= _MAX_ERROR_LEN:
        return text
    return text[:_MAX_ERROR_LEN] + (
        f"... [truncated; original {len(text)} chars]")

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

#: Side mapping scheduler-run -> project. Deliberately NOT part of the
#: hash-chained scheduler_runs schema: the chain covers the run record
#: itself, and project linkage is deployment metadata, not run content.
#: Plain table (no chain); survives fresh-process reopen like any sqlite
#: table. Written under the scheduler lock at submit time.
_SCHEMA_PROJECTS = """
CREATE TABLE IF NOT EXISTS scheduler_run_projects (
    run_id      TEXT PRIMARY KEY,
    project_id  TEXT NOT NULL,
    linked_at   REAL NOT NULL
)
"""
_SCHEMA_PROJECTS_INDEX = (
    "CREATE INDEX IF NOT EXISTS idx_scheduler_run_projects_project "
    "ON scheduler_run_projects(project_id)"
)

#: Columns of the pre-chain (legacy) scheduler_runs table. A database
#: whose scheduler_runs table lacks the chain columns is refused at
#: open -- see LegacySchemaError and migrate_legacy_scheduler_db().
_LEGACY_REQUIRED_ABSENT = ("seq", "prev_digest", "row_digest")

# ---------------------------------------------------------------------------
# Resource bounds (Phase 3 hardening). Every in-memory container in this
# module is capped; the sqlite scheduler_runs log is append-only by trust
# design (see module docstring) and is never pruned.
# ---------------------------------------------------------------------------
#: Per-run event buffer: beyond this, oldest events are dropped and the
#: per-run counter in self._event_drops is incremented.
_MAX_EVENTS_PER_RUN = 10000
#: In-memory event buffers kept for terminal runs; LRU-evicted by ended_at.
_MAX_EVENT_RUNS_KEPT = 1000
#: FIFO queue depth. submit() at or beyond this refuses "queue full".
_MAX_PENDING = 10000
#: Caller-supplied list_runs(limit=...) is clamped to this.
_MAX_LIST_RUNS = 1000
#: Per-event stage detail truncation (characters) in the trace callback.
_MAX_TRACE_DETAIL = 250
#: Error-string truncation (characters) before persisting to sqlite.
_MAX_ERROR_LEN = 2000
#: Serialized outcome payload cap (bytes); oversized outcomes are replaced
#: by an explicit truncation marker, never silently clipped mid-JSON.
_MAX_OUTCOME_JSON_BYTES = 1_000_000


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
                    "detail": detail[:_MAX_TRACE_DETAIL],
                    "elapsed_ms": round(elapsed_ms, 3),
                    "at": time.time(),
                })
            except Exception:
                pass


class RunScheduler:
    """FIFO run queue with one worker thread over one lazily-booted engine.

    D11 single-owner: exactly one SwarmEngine may exist per database file
    per process. Deployments where the engine is already owned (e.g. the
    GUI engine front owns RUNTIME_DB) MUST pass ``engine_call`` -- a
    callable ``fn(bundle) -> result`` that executes ``fn`` on the
    engine-owning thread with the shared ``{"engine", "router",
    "dispatcher"}`` bundle. The worker then runs the engine body through
    the hook and never constructs a second engine on the owned DB (which
    the D11 registry would fail-closed refuse). Without the hook, the
    scheduler keeps its legacy behavior of lazily booting its own engine
    on ``runtime_db_path`` (bench/test deployments where no other engine
    owns that file).
    """

    def __init__(self, db_path: str, runtime_db_path: str,
                 engine_call=None) -> None:
        self.db_path = os.path.abspath(db_path)
        self.runtime_db_path = runtime_db_path
        self._engine_call = engine_call
        self._lock = threading.RLock()
        self._cond = threading.Condition(self._lock)
        self._pending: collections.deque = collections.deque()  # run_ids, FIFO
        self._controls: Dict[str, RunControl] = {}
        self._payloads: Dict[str, tuple] = {}  # run_id -> (goal, examples, metadata)
        self._events: Dict[str, List[dict]] = {}
        self._event_drops: Dict[str, int] = {}  # run_id -> overflow drops
        self._engine = None
        self._engine_lock = threading.Lock()
        self._shutdown = False
        # NL intent-dispatch executor for kind="intent_dispatch" runs.
        # Registered by the intent-dispatch service; None means intent
        # runs are honestly refused (no silent fallback to a full run).
        self._intent_executor = None
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
            self._db.execute(_SCHEMA_PROJECTS)
            self._db.execute(_SCHEMA_PROJECTS_INDEX)
            self._db.commit()
            # A non-empty chain table must verify on open. The internal
            # audit catches row mutation and splice/deletion; tail
            # truncation is invisible to any hash chain and is caught by
            # the external journal tip at service boot (verify_all),
            # never silently adopted there either.
            if self._db.execute(
                    "SELECT COUNT(*) FROM scheduler_runs").fetchone()[0]:
                ok, msg = self.audit()
                if not ok:
                    raise ChainAuditError(
                        f"scheduler_runs chain audit failed on open: {msg}")
            # Tip BEFORE the orphan repair below, as the journal sees it
            # (head_digest, the full-table digest committed by anchor
            # records). attach_anchor() uses it to recognize the repair's
            # own appends: the journal tip must equal this value for the
            # audited-transition healing to apply. Anything else (attacker
            # truncation, foreign writes) keeps the fail-closed boot
            # refusal.
            self._pre_repair_tip = self.head_digest("scheduler_runs")
            self._boot_repair_appended = False
            # Orphaned-queue repair (cycle-1 sweep): close() leaves
            # not-yet-dequeued runs "queued" in sqlite, and run payloads
            # (goal/examples/metadata) live only in memory, so no new
            # instance can ever execute them -- but the rows still claim
            # "queued" with queue_position None and nothing reaps them.
            # At __init__ time this instance has submitted nothing, so
            # every row still "queued" is an orphan by construction:
            # relabel it honestly as "error" (never executed) via one
            # more append-only version, instead of leaving a "queued"
            # status no queue backs. Single-scheduler-per-DB is the
            # design (one worker thread; the only construction site is
            # build_services), so no live instance can own these rows.
            orphaned = self._db.execute(
                "SELECT s.id FROM scheduler_runs s JOIN "
                "(SELECT id, MAX(seq) AS m FROM scheduler_runs "
                "GROUP BY id) t ON s.id = t.id AND s.seq = t.m "
                "WHERE s.status = 'queued'").fetchall()
            for (orphan_id,) in orphaned:
                latest = self._latest_row_locked(orphan_id)
                data = dict(zip(_CHAIN_FIELDS, latest))
                data["status"] = "error"
                data["ended_at"] = time.time()
                data["error"] = (
                    "scheduler restarted while run was queued; run never "
                    "executed (run payloads are in-memory only)")
                self._append_version_locked(orphan_id, data)
            if orphaned:
                self._db.commit()
                self._boot_repair_appended = True

        self._worker = threading.Thread(
            target=self._worker_loop, name="remor-scheduler", daemon=True
        )
        self._worker.start()

    # ------------------------------------------------------------------
    # engine
    # ------------------------------------------------------------------
    def _engine_lazy(self):
        """Boot the scheduler-owned SwarmEngine on first use (thread-safe).

        Legacy path only: used when no ``engine_call`` hook was provided
        (no other engine owns ``runtime_db_path`` in this process). When
        the hook is set, this is never called -- the deployment's shared
        engine is the single D11 owner.
        """
        with self._engine_lock:
            if self._engine is None:
                from swarm_engine.core.engine import SwarmEngine

                self._engine = SwarmEngine(db_path=self.runtime_db_path)
            return self._engine

    # ------------------------------------------------------------------
    # submission
    # ------------------------------------------------------------------
    def submit(self, goal: str, examples=None, metadata=None,
               conversation_id: Optional[str] = None,
               project_id: Optional[str] = None) -> Dict[str, Any]:
        """Queue a run. Returns {"ok": True, "run_id": ...} (thread-safe).

        project_id is an optional linkage tag: the run is recorded in the
        scheduler_run_projects side mapping so project_runs(project_id)
        can find every scheduler run submitted for a project. It does NOT
        alter the hash-chained scheduler_runs schema (the linkage is
        deployment metadata, not run content), and the scheduler never
        validates the project exists -- the project service owns projects;
        this table is just the honest join index. Run-loop jobs are engine
        jobs, not scheduler runs; they carry the same project_id in their
        job record, which is what "share run records" means here: both
        record types are keyed by project_id, queryable per project.
        """
        with self._lock:
            if self._shutdown:
                return {"ok": False, "error": "scheduler is shut down"}
            if not isinstance(goal, str) or not goal.strip():
                return {"ok": False,
                        "error": "goal must be a non-empty string"}
            if len(self._pending) >= _MAX_PENDING:
                return {"ok": False,
                        "error": f"queue full ({_MAX_PENDING} runs pending); "
                                 f"retry later"}
            if project_id is not None and (
                    not isinstance(project_id, str) or not project_id.strip()):
                return {"ok": False,
                        "error": "project_id must be a non-empty string "
                                 "when given"}
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
                if project_id is not None:
                    self._db.execute(
                        "INSERT OR REPLACE INTO scheduler_run_projects "
                        "(run_id, project_id, linked_at) VALUES (?,?,?)",
                        (run_id, project_id, now))
                self._db.commit()

            self._attested_write_locked(_write_submit, "submit")
            self._controls[run_id] = control
            self._payloads[run_id] = (goal, examples, metadata)
            self._events[run_id] = []
            self._event_drops[run_id] = 0
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
        # The HTTP layer passes a caller-supplied limit straight through;
        # clamp it so one request cannot materialize an unbounded row list.
        limit = max(1, min(int(limit), _MAX_LIST_RUNS))
        with self._lock:
            cur = self._db.execute(
                "SELECT s.id, s.goal, s.status, s.conversation_id, "
                "s.queued_at, s.started_at, s.ended_at, s.error "
                "FROM scheduler_runs s JOIN "
                "(SELECT id, MAX(seq) AS m FROM scheduler_runs "
                "GROUP BY id) t ON s.id = t.id AND s.seq = t.m "
                "ORDER BY s.queued_at DESC LIMIT ?",
                (limit,),
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
        """Buffered stage/status events for one run (for SSE adoption).

        Returns None for an unknown/evicted run. The list shape is
        stable for the HTTP layer; drop accounting lives in
        events_summary().
        """
        with self._lock:
            buf = self._events.get(run_id)
            if buf is None:
                return None
            return list(buf)

    def events_summary(self, run_id: str) -> Optional[Dict[str, Any]]:
        """Events for one run plus overflow accounting. None if unknown.

        "dropped_events" counts events discarded from the head of the
        buffer under the _MAX_EVENTS_PER_RUN bound; "total_pushed" is
        events_kept + dropped_events.
        """
        with self._lock:
            buf = self._events.get(run_id)
            if buf is None:
                return None
            dropped = self._event_drops.get(run_id, 0)
            return {
                "run_id": run_id,
                "events": list(buf),
                "events_kept": len(buf),
                "dropped_events": dropped,
                "total_pushed": len(buf) + dropped,
            }

    def event_drop_count(self, run_id: str) -> Optional[int]:
        """Number of events dropped for one run's buffer. None if unknown."""
        with self._lock:
            if run_id not in self._events:
                return None
            return self._event_drops.get(run_id, 0)

    def queue_depth(self) -> int:
        """Number of runs currently waiting (queued, not yet dequeued)."""
        with self._lock:
            return len(self._pending)

    def project_runs(self, project_id: str) -> List[Dict[str, Any]]:
        """Every scheduler run tagged with project_id (latest row each).

        The honest join index for S6: run_loop jobs are engine jobs, not
        scheduler runs, so they never appear here -- they carry the same
        project_id in the project service's job record instead. This
        returns the scheduler-run side of the shared project_id key.
        Reads the plain side table (no chain involvement) plus the latest
        chained row per run for status.
        """
        with self._lock:
            cur = self._db.execute(
                "SELECT p.run_id, s.goal, s.status, s.queued_at, "
                "s.started_at, s.ended_at, s.error "
                "FROM scheduler_run_projects p JOIN scheduler_runs s "
                "ON s.id = p.run_id "
                "JOIN (SELECT id, MAX(seq) AS m FROM scheduler_runs "
                "GROUP BY id) t ON s.id = t.id AND s.seq = t.m "
                "WHERE p.project_id = ? "
                "ORDER BY s.queued_at DESC",
                (project_id,),
            )
            return [{
                "id": rid, "project_id": project_id, "goal": goal,
                "status": status, "queued_at": qa, "started_at": sa,
                "ended_at": ea, "error": err,
            } for (rid, goal, status, qa, sa, ea, err) in cur.fetchall()]

    # ------------------------------------------------------------------
    # tamper-evidence: anchor attachment + hash chain
    # ------------------------------------------------------------------
    def attach_anchor(self, anchor, authority: str = "scheduler") -> None:
        """Attach a CrossDbAnchor covering this store's chain.

        Verifies the existing chain first: a tampered table is never
        attached to a journal. The journal init/verify/refuse decision
        itself lives in build_services(), not here.

        Boot-time healing: __init__'s orphaned-queue repair appends
        chained versions BEFORE the anchor is attached, so the live tip
        has legitimately moved past the journal tip. When -- and only
        when -- the journal tip equals the exact pre-repair tip captured
        in __init__ (i.e. the sole divergence is this boot's own repair),
        the new heads are committed through the journal's AUDITED
        transition() path (reason + authority recorded), never a silent
        re-anchor. Any other divergence keeps the fail-closed boot
        refusal in build_services().
        """
        with self._lock:
            ok, msg = self.audit()
            if not ok:
                raise ChainAuditError(
                    f"refusing to attach anchor: scheduler_runs chain "
                    f"audit failed: {msg}")
            self._anchor = anchor
            self._authority = authority or "scheduler"
            if (getattr(self, "_boot_repair_appended", False)
                    and anchor.journal_exists()):
                journal_tip = anchor.latest_heads().get(
                    "scheduler:scheduler_runs")
                if journal_tip == getattr(self, "_pre_repair_tip", None):
                    # reason="recovery": the journal's transition allowlist
                    # is closed; the detailed why lives in the appended
                    # chain rows themselves ("scheduler restarted while
                    # run was queued...").
                    anchor.transition(reason="recovery",
                                      authority=self._authority)
                    self._boot_repair_appended = False

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

    # ------------------------------------------------------------------
    # NL intent dispatch (kind="intent_dispatch" runs)
    # ------------------------------------------------------------------
    def register_intent_executor(self, fn) -> None:
        """Register the callable the worker uses for intent-dispatch runs.

        `fn(text, args, producer)` must return an object with `.as_dict()`
        (the DispatchResult). Registered once by the intent-dispatch
        service; without it, intent runs fail closed with an honest error.
        """
        with self._lock:
            self._intent_executor = fn

    def _run_intent_dispatch(self, run_id: str, metadata: Dict[str, Any]) -> None:
        """Execute an NL intent dispatch AS this run's work.

        The dispatch goes through the real NLToolDispatcher (route ->
        re-verify -> arg validation -> real Composer execution -> audit
        record). A full engine run is never launched for an intent
        dispatch. The DispatchResult is stored as the run outcome via its
        .as_dict(); a dispatcher-level refusal (unknown_intent etc.) is a
        COMPLETED run whose outcome carries ok:false + refusal -- the
        machinery ran fine, the intent was refused.
        """
        executor = self._intent_executor
        try:
            if executor is None:
                raise RuntimeError(
                    "no intent executor registered on scheduler: "
                    "kind='intent_dispatch' runs cannot execute")
            result = executor(metadata.get("text"), metadata.get("args"),
                              metadata.get("producer"))
            if not hasattr(result, "as_dict"):
                raise RuntimeError(
                    "intent executor returned no DispatchResult")
            with self._lock:
                # Merge-time repair (2026-09-26): backend_ff's lineage used
                # the status string "complete", which is not a member of
                # the canonical status machine (TERMINAL_STATUSES uses
                # "completed"; _prune_events_locked's terminal-run query
                # keys on it). The hunk's own docstring says COMPLETED.
                self._finish_locked(run_id, "completed", result)
        except Exception as ex:  # honest, never silent
            with self._lock:
                self._finish_locked(run_id, "error", None,
                                    error=f"intent dispatch failed: {ex!r}")

    def _run_one(self, run_id: str) -> None:
        # Peek at the payload first: intent-dispatch runs do their NL
        # dispatch as the run's work and must never boot (or invoke) the
        # run engine. This peek is additive -- regular runs flow through
        # the original path below unchanged.
        with self._lock:
            _payload = self._payloads.get(run_id)
            _kind = ((_payload[2] if _payload else None) or {}).get("kind")
        if _kind == "intent_dispatch":
            with self._lock:
                if (self._controls.get(run_id) is None
                        or self._payloads.get(run_id) is None):
                    return  # cancelled concurrently; worker_loop skips
                _metadata = self._payloads[run_id][2] or {}
                self._set_status_locked(run_id, "running",
                                        started_at=time.time())
            self._run_intent_dispatch(run_id, _metadata)
            return

        with self._lock:
            control = self._controls.get(run_id)
            payload = self._payloads.get(run_id)
            if control is None or payload is None:
                return  # cancelled concurrently; worker_loop already skips
            goal, examples, metadata = payload
            self._set_status_locked(run_id, "running",
                                    started_at=time.time())

        if self._engine_call is not None:
            # D11 single-owner path: the deployment already owns the one
            # engine on this DB (e.g. the GUI engine front). The whole
            # engine body runs on the owning thread via the hook -- never
            # boot a second engine (the D11 registry would fail-closed
            # refuse it, which is exactly the phone bug this repairs).
            try:
                final, outcome, outcome_error = self._engine_call(
                    lambda bundle: self._execute_engine_run(
                        bundle["engine"], run_id, control,
                        goal, examples, metadata))
            except Exception as ex:  # hook failed: honest error
                with self._lock:
                    self._finish_locked(run_id, "error", None,
                                        error=_truncate_error(
                                            f"engine call failed: {ex!r}"))
                return
        else:
            try:
                engine = self._engine_lazy()
            except Exception as ex:  # engine failed to boot: honest error
                with self._lock:
                    self._finish_locked(run_id, "error", None,
                                        error=_truncate_error(
                                            f"engine boot failed: {ex!r}"))
                return
            final, outcome, outcome_error = self._execute_engine_run(
                engine, run_id, control, goal, examples, metadata)

        with self._lock:
            self._finish_locked(run_id, final, outcome, error=outcome_error)

    def _execute_engine_run(self, engine, run_id: str, control: RunControl,
                            goal: str, examples, metadata):
        """Run the engine body for one run; returns (final, outcome, error).

        MUST execute on the engine-owning thread (thread-affinity): the
        caller either booted this engine itself (legacy ``_engine_lazy``
        path, worker thread == constructing thread) or routed here through
        the deployment's ``engine_call`` hook (owning thread).
        """
        outcome = None
        outcome_error = ""
        # The thread installs the run's control as current AND passes
        # it via metadata, per the frozen run_control contract. handle()
        # converts RunStopped into a STOPPED outcome with the trace intact.
        _rc.set_current(control)
        try:
            iface = _TracingTaskInterface(engine)
            iface._event_cb = lambda ev: self._push_event(run_id, ev)
            outcome = asyncio.run(iface.handle(
                goal,
                examples=examples,
                # run_id is threaded so the task interface can namespace
                # run-scoped governed directories (NL file/media artifacts)
                # by the scheduler run that produced them.
                metadata={**(metadata or {}), "run_control": control,
                          "run_id": run_id},
            ))
            final = self._classify(outcome, control)
        except Exception as ex:  # honest, never silent
            outcome_error = _truncate_error(f"{type(ex).__name__}: {ex}")
            final = "stopped" if control.stop_requested else "error"
        finally:
            _rc.set_current(None)
        return final, outcome, outcome_error

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
                # Provenance: 2026-09-27 ~08:47 UTC — reverted a wiring-mission
                # edit that had flipped this branch to "stopped" on any
                # stop_requested (even when the outcome succeeded).
                # James's exact words in main chat (04:46 EDT): "Him let's
                # duck it" — read as "ditch it", i.e. revert the flip. The
                # revert was reported back to him the same hour without
                # objection. Pending-stop + successful outcome classifies
                # "completed" unless he directs otherwise.
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
            if outcome_json is not None and len(
                    outcome_json.encode("utf-8")) > _MAX_OUTCOME_JSON_BYTES:
                # Oversized outcomes are never silently clipped mid-JSON:
                # store an explicit marker with the original byte size.
                raw_bytes = len(outcome_json.encode("utf-8"))
                outcome_json = json.dumps({
                    "_outcome_truncated": True,
                    "outcome_bytes": raw_bytes,
                    "cap_bytes": _MAX_OUTCOME_JSON_BYTES,
                })

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
        overflow = len(buf) - _MAX_EVENTS_PER_RUN
        if overflow > 0:
            # Drop-oldest: the head (earliest) events go; the count is
            # kept in _event_drops and surfaced via events_summary().
            del buf[:overflow]
            self._event_drops[run_id] = (
                self._event_drops.get(run_id, 0) + overflow
            )

    def _prune_events_locked(self) -> None:
        """LRU-eviction (by ended_at) of terminal-run event buffers.

        Only the in-memory buffers go. The sqlite scheduler_runs rows
        stay -- the hash chain is append-only by trust design (chain
        integrity > retention; see module docstring).
        """
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
            self._event_drops.pop(rid, None)


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
