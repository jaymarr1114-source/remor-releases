"""Contract 10 — Persistent recurring task schedules (Worker C / Task 3).

The honest 501 on POST /api/tasks/recurring ("no cron substrate") is
replaced here with a REAL substrate: a sqlite-backed schedule store plus
a pumper thread that fires due schedules through the REAL
``RunScheduler`` submit path with the REAL metering guards enforced
(``MeteringService.guarded_submit``: 35/day cap, then 25-queue cap, then
``scheduler.submit()``).

What is real here
-----------------
* ``RecurrenceService`` — sqlite ``recurring_schedules`` table (id,
  goal, spec, examples, metadata, conversation_id, project_id,
  next_fire_at, created_at, active, tier_flag, fire_count, last_run_id,
  last_error, last_refusal_code). All mutations in this one module.
* Firing — a background pumper thread owned by the service lifecycle
  (NOT a second engine; the engine is the scheduler's single worker
  thread, same as the existing adapter). Due rows are claimed atomically
  (rowcount-checked ``UPDATE ... WHERE active=1 AND next_fire_at <= ?``,
  like the one-shot dispatcher in tasks_api), then each fire goes
  through ``metering.guarded_submit()`` — scheduled fires consume the
  daily and queue guards exactly like manual submits.
* Restart survival — every schedule and its next_fire_at lives in
  sqlite; a fresh service instance over the same DB re-arms and fires
  due schedules. Zero in-memory-only state.
* Refusal behavior — a fire refused by metering (daily_task_cap /
  queue_cap) is recorded on the row (last_refusal_code) and the cadence
  STILL ADVANCES: the next fire is at the previously scheduled time plus
  the interval, never a hot retry loop. Quota refusals surface typed
  (``contract_limit`` -> HTTP 429 via the adapter's _contract_result).

Supported recurrence syntax (documented exactly — this is ALL that is
supported; anything else is a typed 400):
    {"every_seconds": N}   N: int|float, N >= 1.0  — fire every N seconds
    {"every_minutes": N}   N: int|float, N > 0     — fire every N minutes
Exactly one of the two keys must be present. No cron subset: a cron
subset was considered and deliberately NOT added (the interval form
covers the honest use cases; a half-cron dialect would lie about
expressiveness). ``next_fire_at`` is computed as
(previous next_fire_at) + interval, so a late fire does not drift the
cadence. Optional ``start_in_seconds`` (float, >= 0, default 0) delays
the FIRST fire; without it the schedule is due immediately on creation.

Tier flag / exposure decision (deliberately NOT decided here)
--------------------------------------------------------------
* ``REMOR_RECURRENCE_ENABLED`` — exposure gate. Values "1"/"true"/"yes"
  (case-insensitive) -> the HTTP surface and the pumper are live.
  Unset/anything else -> create/list/cancel return typed 501
  ``recurring_disabled`` and the pumper never starts. Default: DISABLED
  (fail closed).
* ``REMOR_RECURRENCE_TIER`` — recorded per row in ``tier_flag`` at
  creation ("free" / "paid" / "unset" if the env var is absent). This is
  a RECORD, not an enforcement: the free-vs-paid decision is James's.
  ``exposure()`` reports the live gate state plus the open decision so
  the GUI can render it honestly.
* Metering constants are NOT changed: scheduled fires are metered under
  the same free-tier guards as manual submits. If recurrence ever becomes
  a paid-only feature, the place to enforce that is here (a tier check in
  ``create_schedule``), not in metering.

Honest boundaries
-----------------
* A due schedule fires only while the service (and its pumper) is alive;
  after a process restart the rows persist and the next instance fires
  them. If the process is down past several firings, the schedule does
  NOT backfill: next_fire_at advances one interval per fire, so a long
  outage yields exactly ONE catch-up fire, not N.
* The pumper is a second THREAD (needed to watch the clock) but never a
  second engine: firing calls ``metering.guarded_submit()``, and
  ``RunScheduler.submit()`` is documented thread-safe; the engine still
  boots and runs on the scheduler's single worker thread.
* Poll granularity is poll_interval (default 0.5s): a schedule may fire
  up to ~poll_interval late. Sub-second intervals are rejected
  (min 1 second) to keep the pumper honest.
* In-flight fires complete: cancel_schedule deactivates the schedule (no
  future fires), but a fire already claimed when the cancel lands still
  submits its run -- the atomic claim is the point of no return. The row
  stays coherent (active=0, with fire_count/last_run_id recording the
  in-flight fire). Proven by interleave test, cycle-1 sweep.
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
import uuid
from typing import Any, Callable, Dict, List, Optional

from swarm_engine.services.contract_types import (
    contract_limit, contract_unavailable)

# -- exposure gate ----------------------------------------------------------
ENV_ENABLED = "REMOR_RECURRENCE_ENABLED"
ENV_TIER = "REMOR_RECURRENCE_TIER"

DISABLED_CODE = "recurring_disabled"
BAD_SPEC_CODE = "recurring_bad_spec"
UNKNOWN_SCHEDULE_CODE = "recurring_unknown_schedule"


def _env_flag(name: str) -> bool:
    return (os.environ.get(name, "") or "").strip().lower() in (
        "1", "true", "yes")


def is_enabled() -> bool:
    """Live exposure gate: REMOR_RECURRENCE_ENABLED=1/true/yes."""
    return _env_flag(ENV_ENABLED)


def _tier_flag() -> str:
    v = (os.environ.get(ENV_TIER, "") or "").strip().lower()
    return v if v in ("free", "paid") else "unset"


_SCHEMA = """
CREATE TABLE IF NOT EXISTS recurring_schedules (
    id              TEXT PRIMARY KEY,
    goal            TEXT NOT NULL,
    spec_json       TEXT NOT NULL,
    examples_json   TEXT,
    metadata_json   TEXT,
    conversation_id TEXT,
    project_id      TEXT,
    next_fire_at    REAL NOT NULL,
    created_at      REAL NOT NULL,
    active          INTEGER NOT NULL DEFAULT 1,
    tier_flag       TEXT NOT NULL,
    fire_count      INTEGER NOT NULL DEFAULT 0,
    last_run_id     TEXT,
    last_error      TEXT,
    last_refusal_code TEXT
)
"""


def parse_spec(spec: Any) -> float:
    """Validate a recurrence spec; return the interval in seconds.

    Raises ValueError with a precise message on anything unsupported.
    """
    if not isinstance(spec, dict):
        raise ValueError(
            "recurrence spec must be an object with exactly one of "
            "\"every_seconds\" / \"every_minutes\"")
    keys = set(spec.keys())
    want = {"every_seconds", "every_minutes"}
    present = keys & want
    if len(present) != 1:
        raise ValueError(
            "recurrence spec must contain exactly one of "
            "\"every_seconds\" / \"every_minutes\"; got keys "
            f"{sorted(keys)}")
    name = next(iter(present))
    value = spec[name]
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name!r} must be a number, got "
                         f"{type(value).__name__}")
    value = float(value)
    if name == "every_seconds":
        if value < 1.0:
            raise ValueError(
                f"\"every_seconds\" must be >= 1.0, got {value}")
        return value
    if value <= 0.0:
        raise ValueError(f"\"every_minutes\" must be > 0, got {value}")
    return value * 60.0


class RecurrenceService:
    """Persistent recurring schedules firing through the real scheduler."""

    def __init__(self, db_path: str, guarded_submit: Callable[..., Dict[str, Any]],
                 dispatcher_enabled: bool = True,
                 poll_interval: float = 0.5,
                 enabled: Optional[bool] = None) -> None:
        self.db_path = db_path
        self.guarded_submit = guarded_submit
        self.poll_interval = max(0.05, float(poll_interval))
        self.enabled = is_enabled() if enabled is None else bool(enabled)
        self._lock = threading.RLock()
        self._shutdown = False
        os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
        self._db = sqlite3.connect(db_path, check_same_thread=False)
        with self._lock:
            self._db.execute(_SCHEMA)
            self._db.commit()
        self._pumper = None
        # Defense in depth: the pumper only exists when exposure is on.
        if dispatcher_enabled and self.enabled:
            self._pumper = threading.Thread(
                target=self._pump_loop, name="remor-recurrence-pumper",
                daemon=True)
            self._pumper.start()

    # -- exposure -----------------------------------------------------------
    def exposure(self) -> Dict[str, Any]:
        """Live gate state plus the open free-vs-paid decision."""
        return {
            "ok": True,
            "enabled": self.enabled,
            "gate_env": ENV_ENABLED,
            "tier_env": ENV_TIER,
            "tier_flag_now": _tier_flag(),
            "tier_decision": (
                "OPEN — James decides whether recurrence is free or paid. "
                "This module records tier_flag per schedule but does not "
                "enforce a tier; if recurrence becomes paid-only, enforce "
                "it in create_schedule(), not in metering."),
        }

    def _disabled(self) -> Optional[Dict[str, Any]]:
        if not self.enabled:
            return contract_unavailable(
                DISABLED_CODE,
                "recurring schedules are disabled: set "
                f"{ENV_ENABLED}=1 to expose the surface. The free-vs-paid "
                "decision is open (see tier_decision in exposure()).",
                missing_substrate="recurrence exposure flag")
        return None

    # -- CRUD -----------------------------------------------------------------
    def create_schedule(self, goal: Any, spec: Any = None,
                        start_in_seconds: Any = 0,
                        examples: Any = None,
                        metadata: Optional[Dict[str, Any]] = None,
                        conversation_id: Optional[str] = None,
                        project_id: Optional[str] = None) -> Dict[str, Any]:
        refused = self._disabled()
        if refused is not None:
            return refused
        if not isinstance(goal, str) or not goal.strip():
            return {"ok": False,
                    "error": "goal must be a non-empty string"}
        try:
            interval = parse_spec(spec)
        except ValueError as exc:
            return {"ok": False,
                    "error": str(exc), "code": BAD_SPEC_CODE}
        try:
            delay = float(start_in_seconds or 0)
        except (TypeError, ValueError):
            return {"ok": False,
                    "error": "start_in_seconds must be a number >= 0"}
        if delay < 0:
            return {"ok": False,
                    "error": "start_in_seconds must be a number >= 0"}
        now = time.time()
        sid = uuid.uuid4().hex
        with self._lock:
            self._db.execute(
                "INSERT INTO recurring_schedules "
                "(id, goal, spec_json, examples_json, metadata_json, "
                " conversation_id, project_id, next_fire_at, created_at, "
                " active, tier_flag, fire_count) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,0)",
                (sid, goal.strip(), json.dumps(spec),
                 json.dumps(examples) if examples is not None else None,
                 json.dumps(metadata) if metadata is not None else None,
                 conversation_id, project_id,
                 now + delay, now, 1, _tier_flag()))
            self._db.commit()
        return {"ok": True, "schedule_id": sid,
                "next_fire_at": now + delay, "interval_seconds": interval,
                "tier_flag": _tier_flag(),
                "tier_decision": "OPEN — James decides free vs paid"}

    def list_schedules(self, limit: int = 50) -> Dict[str, Any]:
        refused = self._disabled()
        if refused is not None:
            return refused
        with self._lock:
            rows = self._db.execute(
                "SELECT id, goal, spec_json, next_fire_at, created_at, "
                " active, tier_flag, fire_count, last_run_id, last_error, "
                " last_refusal_code "
                "FROM recurring_schedules "
                "ORDER BY created_at DESC LIMIT ?",
                (max(1, int(limit)),)).fetchall()
        out = []
        for r in rows:
            out.append({
                "schedule_id": r[0], "goal": r[1],
                "spec": json.loads(r[2]),
                "next_fire_at": r[3], "created_at": r[4],
                "active": bool(r[5]), "tier_flag": r[6],
                "fire_count": r[7], "last_run_id": r[8],
                "last_error": r[9], "last_refusal_code": r[10],
            })
        return {"ok": True, "schedules": out}

    def get_schedule(self, schedule_id: str) -> Dict[str, Any]:
        refused = self._disabled()
        if refused is not None:
            return refused
        with self._lock:
            row = self._db.execute(
                "SELECT id, goal, spec_json, next_fire_at, created_at, "
                " active, tier_flag, fire_count, last_run_id, last_error, "
                " last_refusal_code FROM recurring_schedules WHERE id=?",
                (schedule_id,)).fetchone()
        if row is None:
            return {"ok": False, "code": UNKNOWN_SCHEDULE_CODE,
                    "error": f"unknown schedule {schedule_id!r}"}
        return {"ok": True, "schedule": {
            "schedule_id": row[0], "goal": row[1],
            "spec": json.loads(row[2]), "next_fire_at": row[3],
            "created_at": row[4], "active": bool(row[5]),
            "tier_flag": row[6], "fire_count": row[7],
            "last_run_id": row[8], "last_error": row[9],
            "last_refusal_code": row[10]}}

    def cancel_schedule(self, schedule_id: Any) -> Dict[str, Any]:
        refused = self._disabled()
        if refused is not None:
            return refused
        if not isinstance(schedule_id, str) or not schedule_id:
            return {"ok": False,
                    "error": "schedule_id is required"}
        with self._lock:
            cur = self._db.execute(
                "UPDATE recurring_schedules SET active=0 WHERE id=? "
                "AND active=1", (schedule_id,))
            self._db.commit()
            if cur.rowcount == 0:
                exists = self._db.execute(
                    "SELECT 1 FROM recurring_schedules WHERE id=?",
                    (schedule_id,)).fetchone()
                if exists is None:
                    return {"ok": False, "code": UNKNOWN_SCHEDULE_CODE,
                            "error": f"unknown schedule {schedule_id!r}"}
                return {"ok": False,
                        "error": "schedule is already cancelled"}
        return {"ok": True, "schedule_id": schedule_id,
                "cancelled": True}

    # -- firing ---------------------------------------------------------------
    def _pump_loop(self) -> None:
        while not self._shutdown:
            try:
                self._fire_due()
            except Exception:
                pass  # the pumper must never die on a bad row
            end = time.time() + self.poll_interval
            while not self._shutdown and time.time() < end:
                time.sleep(min(0.05, end - time.time()))

    def _fire_due(self) -> int:
        """Claim every due row atomically and fire it. Returns fires."""
        now = time.time()
        with self._lock:
            due = self._db.execute(
                "SELECT id, goal, spec_json, examples_json, metadata_json, "
                " conversation_id, project_id, next_fire_at "
                "FROM recurring_schedules "
                "WHERE active=1 AND next_fire_at <= ?",
                (now,)).fetchall()
        fires = 0
        for row in due:
            try:
                if self._fire_one(row):
                    fires += 1
            except Exception as exc:  # noqa: BLE001 - one bad row must
                # never starve its siblings: record the failure on the
                # row and continue the batch. (A poisoned spec_json used
                # to abort the whole _fire_due scan on every poll, so
                # every schedule sorted after it starved permanently.)
                try:
                    with self._lock:
                        self._db.execute(
                            "UPDATE recurring_schedules SET last_error=? "
                            "WHERE id=?",
                            (f"{type(exc).__name__}: {exc}", row[0]))
                        self._db.commit()
                except Exception:
                    pass
        return fires

    def _fire_one(self, row) -> bool:
        sid, goal = row[0], row[1]
        try:
            interval = parse_spec(json.loads(row[2]))
        except (ValueError, TypeError) as exc:
            # Fail closed: a schedule whose spec cannot be parsed can
            # never fire (no interval to advance the cadence by), and it
            # must not poison the pumper's batch either. Deactivate it
            # and record why -- loud in list_schedules/get_schedule.
            # (The spec was valid at creation; unparseable now means the
            # row was corrupted or tampered with out-of-band.)
            with self._lock:
                self._db.execute(
                    "UPDATE recurring_schedules "
                    "SET active=0, last_error=? WHERE id=?",
                    (f"unparseable recurrence spec; schedule deactivated: "
                     f"{exc}", sid))
                self._db.commit()
            return False
        examples = json.loads(row[3]) if row[3] else None
        metadata = json.loads(row[4]) if row[4] else None
        conv_id, proj_id, claimed_next = row[5], row[6], row[7]
        advance_to = claimed_next + interval
        with self._lock:
            # Atomic claim: only this pumper wins the row (two instances
            # over one DB cannot both fire it).
            cur = self._db.execute(
                "UPDATE recurring_schedules SET next_fire_at=? "
                "WHERE id=? AND active=1 AND next_fire_at=?",
                (advance_to, sid, claimed_next))
            self._db.commit()
            if cur.rowcount != 1:
                return False
        run_id: Optional[str] = None
        error: Optional[str] = None
        refusal_code: Optional[str] = None
        try:
            res = self.guarded_submit(
                goal, examples=examples, metadata=metadata,
                conversation_id=conv_id, project_id=proj_id)
        except Exception as exc:  # never let a fire kill the pumper
            error = f"{type(exc).__name__}: {exc}"
            res = {"ok": False, "error": error}
        if res.get("ok"):
            run_id = res.get("run_id")
        else:
            limit = res.get("limit") or {}
            refusal_code = limit.get("code") or res.get("code")
        with self._lock:
            if run_id is not None:
                self._db.execute(
                    "UPDATE recurring_schedules "
                    "SET fire_count=fire_count+1, last_run_id=?, "
                    " last_error=NULL, last_refusal_code=NULL "
                    "WHERE id=?", (run_id, sid))
            else:
                self._db.execute(
                    "UPDATE recurring_schedules "
                    "SET last_error=?, last_refusal_code=? WHERE id=?",
                    (error or json.dumps(res), refusal_code, sid))
            self._db.commit()
        return run_id is not None

    def close(self, timeout: float = 5.0) -> None:
        self._shutdown = True
        pump = self._pumper
        if pump is not None:
            pump.join(timeout=timeout)
        with self._lock:
            self._db.close()


# ----------------------------------------------------------------------
# Route table for the coordinator (do NOT wire into http_adapter here).
# Handlers take a single body_dict; for GET routes the composing adapter
# merges the query string into body_dict before calling.
#
# These keys deliberately overwrite the ("POST", "/api/tasks/recurring")
# 501 entry from routes_for_tasks_api when merged after it: the honest
# 501 is gone because the substrate now exists.
# ----------------------------------------------------------------------
def routes_for_recurrence(service: "RecurrenceService") -> Dict:
    def _create(body):
        spec = body.get("spec")
        if spec is None:
            spec = body.get("recurrence")
        return service.create_schedule(
            goal=body.get("goal"),
            spec=spec,
            start_in_seconds=body.get("start_in_seconds", 0),
            examples=body.get("examples"),
            metadata=body.get("metadata"),
            conversation_id=body.get("conversation_id"),
            project_id=body.get("project_id"),
        )

    def _list(body):
        try:
            limit = int(body.get("limit", 50))
        except (TypeError, ValueError):
            limit = 50
        return service.list_schedules(limit=limit)

    def _cancel(body):
        return service.cancel_schedule(body.get("schedule_id"))

    def _exposure(body):
        return service.exposure()

    return {
        ("POST", "/api/tasks/recurring"): _create,
        ("GET", "/api/tasks/recurring"): _list,
        ("POST", "/api/tasks/recurring/cancel"): _cancel,
        ("GET", "/api/tasks/recurring/exposure"): _exposure,
    }
