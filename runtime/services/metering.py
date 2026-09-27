"""Contract 9 — Metering: real usage counting over the real RunScheduler.

Everything enforced here is measured, not declared:

* tasks_today() counts distinct submitted tasks (one task = one count)
  in the scheduler's own sqlite DB with queued_at inside the last 24h
  (rolling window).
* can_submit() / guarded_submit() refuse the 36th task in that window
  with a typed limit payload (code daily_task_cap).
* queue_depth() is the scheduler's real queue depth; guarded_submit()
  refuses the 26th queued run (code queue_cap).
* concurrency() reports the REAL number: the scheduler serializes onto
  ONE worker thread (its sqlite stores are not safe under concurrent
  writers), so 1 concurrent run -- not the plan's aspirational 3. The
  plan constant 3 is documented as a future ceiling, explicitly not
  implemented or enforced.

Honest absences (classified, not simulated):
* Token-based balances: no token-measurement substrate exists anywhere
  in the runtime (coordinator-verified zero hits) -> HONESTLY-UNAVAILABLE.
* Chat-turn metering (50/day in the plan draft): there is no chat
  substrate in these backend services (the GUI chat is frontend-local)
  -> HONESTLY-UNAVAILABLE here.
* tier() returns the plan constants together with what is actually
  enforced vs. unavailable, so the GUI cannot display an unenforced cap
  as if it were real.
"""
from __future__ import annotations

import sqlite3
import time
from typing import Any, Dict, Optional

from swarm_engine.services.contract_types import (
    contract_limit,
    contract_unavailable,
    dispatch,
)

# Free-tier plan constants (plan-driven; only some are enforced).
TASKS_PER_DAY_FREE = 35
MAX_QUEUE_FREE = 25
CONCURRENT_PLAN_FREE = 3          # plan ceiling; NOT implemented
CONCURRENT_REAL = 1               # the scheduler's single worker thread
CHAT_TURNS_PER_DAY_PLAN = 50      # plan draft; no chat substrate here

DAILY_CAP_CODE = "daily_task_cap"
QUEUE_CAP_CODE = "queue_cap"
TOKEN_BALANCE_CODE = "metering_token_balance"
CHAT_TURNS_CODE = "chat_turn_metering"

_WINDOW_SECONDS = 24 * 3600


class MeteringService:
    """Usage metering over a real RunScheduler instance."""

    def __init__(self, scheduler: Any,
                 tasks_per_day: int = TASKS_PER_DAY_FREE,
                 max_queue: int = MAX_QUEUE_FREE) -> None:
        self.scheduler = scheduler
        self.tasks_per_day = tasks_per_day
        self.max_queue = max_queue

    # -- real counting --------------------------------------------------
    def _count_since(self, cutoff: float) -> int:
        """Count distinct submitted tasks with queued_at >= cutoff.

        Reads the scheduler's own sqlite DB on a fresh connection so the
        count is what the scheduler itself recorded -- nothing is kept in
        a parallel in-memory counter that could drift. Counts DISTINCT
        run ids (one submitted task = one count): scheduler_runs holds
        one row per run *version* (queued -> running -> terminal), so a
        plain COUNT(*) would bill a task once per lifecycle transition.
        """
        conn = sqlite3.connect(self.scheduler.db_path)
        try:
            cur = conn.execute(
                "SELECT COUNT(DISTINCT id) FROM scheduler_runs "
                "WHERE queued_at >= ?",
                (cutoff,))
            return int(cur.fetchone()[0])
        finally:
            conn.close()

    def tasks_since(self, cutoff: float) -> int:
        return self._count_since(cutoff)

    def tasks_today(self) -> Dict[str, Any]:
        """Tasks submitted in the last 24h (rolling window), counted from
        the scheduler's real run records."""
        n = self._count_since(time.time() - _WINDOW_SECONDS)
        return {
            "ok": True,
            "tasks_today": n,
            "limit": self.tasks_per_day,
            "remaining": max(0, self.tasks_per_day - n),
            "window_seconds": _WINDOW_SECONDS,
            "window": "rolling 24h",
        }

    def queue_depth(self) -> Dict[str, Any]:
        """The scheduler's real current queue depth."""
        n = self.scheduler.queue_depth()
        return {
            "ok": True,
            "queue_depth": n,
            "limit": self.max_queue,
            "remaining": max(0, self.max_queue - n),
        }

    def concurrency(self) -> Dict[str, Any]:
        """Honest concurrency: 1. The scheduler runs ONE worker thread over
        one engine because the engine's sqlite stores are not safe under
        concurrent writers; FIFO queueing is the real concurrency model.
        The plan's '3 concurrent' is a future ceiling, not a fact."""
        return {
            "ok": True,
            "concurrent_runs": CONCURRENT_REAL,
            "mechanism": ("single FIFO worker thread; engine sqlite stores "
                          "are not safe under concurrent writers"),
            "plan_ceiling": CONCURRENT_PLAN_FREE,
            "plan_ceiling_status": "not implemented; reported for honesty",
        }

    # -- enforcement ----------------------------------------------------
    def can_submit(self) -> Dict[str, Any]:
        """True-path returns ok; over the daily cap returns a typed limit."""
        n = self._count_since(time.time() - _WINDOW_SECONDS)
        if n >= self.tasks_per_day:
            return contract_limit(
                DAILY_CAP_CODE, self.tasks_per_day, n,
                reset="rolling 24h window: each task's slot frees 24h after "
                      "it was submitted")
        return {"ok": True, "tasks_today": n,
                "remaining": self.tasks_per_day - n}

    def _check_queue_cap(self) -> Optional[Dict[str, Any]]:
        n = self.scheduler.queue_depth()
        if n >= self.max_queue:
            return contract_limit(
                QUEUE_CAP_CODE, self.max_queue, n,
                reset="queued runs start executing and leave the queue")
        return None

    def guarded_submit(self, goal: str, examples: Any = None,
                       metadata: Optional[Dict[str, Any]] = None,
                       conversation_id: Optional[str] = None,
                       project_id: Optional[str] = None) -> Dict[str, Any]:
        """Submit a run through the real scheduler, enforcing both caps.

        Daily cap is checked first (a task refused for the day never
        consumes queue), then the queue cap, then the real
        scheduler.submit(). Returns the scheduler's own result dict on
        success. project_id is passed straight through to the scheduler's
        linkage tag (S6); caps are enforced identically either way.
        """
        day = self.can_submit()
        if not day.get("ok"):
            return day
        q = self._check_queue_cap()
        if q is not None:
            return q
        return self.scheduler.submit(
            goal, examples=examples, metadata=metadata,
            conversation_id=conversation_id, project_id=project_id)

    # -- honest absences -------------------------------------------------
    def token_balances(self) -> Dict[str, Any]:
        return contract_unavailable(
            TOKEN_BALANCE_CODE,
            "no token-measurement substrate exists in the runtime: tokens "
            "are never counted for any run or template, so no token balance "
            "can be reported, metered, or enforced",
            missing_substrate="token_meter")

    def chat_turns(self) -> Dict[str, Any]:
        return contract_unavailable(
            CHAT_TURNS_CODE,
            "no chat substrate exists in these backend services: the GUI "
            "chat is frontend-local, so backend metering cannot count chat "
            "turns. The 50/day plan figure is a draft constant with no "
            "enforcement point",
            missing_substrate="chat_service")

    def tier(self) -> Dict[str, Any]:
        """Plan constants plus what is actually enforced vs. unavailable,
        so the GUI can render caps honestly."""
        return {
            "ok": True,
            "tier": "free",
            "constants": {
                "max_agents": 3,                    # enforced in agent_api
                "tasks_per_day": self.tasks_per_day,
                "max_queue": self.max_queue,
                "concurrent_plan_ceiling": CONCURRENT_PLAN_FREE,
                "chat_turns_per_day_plan": CHAT_TURNS_PER_DAY_PLAN,
            },
            "enforced": [
                {"code": "free_tier_agent_cap", "by": "agent_api",
                 "mechanism": "registry row count under lock"},
                {"code": DAILY_CAP_CODE, "by": "metering",
                 "mechanism": ("count of distinct submitted runs in "
                               "rolling 24h")},
                {"code": QUEUE_CAP_CODE, "by": "metering",
                 "mechanism": "scheduler.queue_depth()"},
                {"code": "concurrency", "by": "scheduler",
                 "mechanism": "single FIFO worker thread (1, not 3)"},
            ],
            "unavailable": [
                {"code": TOKEN_BALANCE_CODE,
                 "missing_substrate": "token_meter"},
                {"code": CHAT_TURNS_CODE,
                 "missing_substrate": "chat_service"},
                {"code": "purchase_permanent_agents",
                 "missing_substrate": "payment_provider"},
            ],
        }

    def usage(self) -> Dict[str, Any]:
        """One-shot usage summary for the GUI metering view."""
        today = self.tasks_today()
        q = self.queue_depth()
        c = self.concurrency()
        return {
            "ok": True,
            "tasks_today": today["tasks_today"],
            "tasks_per_day": today["limit"],
            "tasks_remaining": today["remaining"],
            "queue_depth": q["queue_depth"],
            "queue_limit": q["limit"],
            "concurrent_runs": c["concurrent_runs"],
            "token_balances": "unavailable: no token-measurement substrate",
            "chat_turns": "unavailable: no chat substrate in backend services",
        }


# ---------------------------------------------------------------------
# construction + route table
# ---------------------------------------------------------------------

def build_metering_service(scheduler: Any,
                           tasks_per_day: int = TASKS_PER_DAY_FREE,
                           max_queue: int = MAX_QUEUE_FREE) -> MeteringService:
    return MeteringService(scheduler, tasks_per_day=tasks_per_day,
                           max_queue=max_queue)


def routes_for_metering(service: MeteringService) -> Dict[Any, Any]:
    """{(method, path): handler(body_dict)} for the GUI to adopt."""

    def _usage(body: Dict[str, Any]) -> Dict[str, Any]:
        return service.usage()

    def _tier(body: Dict[str, Any]) -> Dict[str, Any]:
        return service.tier()

    def _can_submit(body: Dict[str, Any]) -> Dict[str, Any]:
        return service.can_submit()

    def _submit(body: Dict[str, Any]) -> Dict[str, Any]:
        goal = body.get("goal")
        if not isinstance(goal, str) or not goal.strip():
            return {"ok": False,
                    "error": "goal must be a non-empty string"}
        # [Worker D / S6] project_id passes straight through to the
        # scheduler's linkage tag; caps are enforced identically either way.
        return service.guarded_submit(
            goal, examples=body.get("examples"),
            metadata=body.get("metadata"),
            conversation_id=body.get("conversation_id"),
            project_id=body.get("project_id"))

    def _tokens(body: Dict[str, Any]) -> Dict[str, Any]:
        return service.token_balances()

    def _chat(body: Dict[str, Any]) -> Dict[str, Any]:
        return service.chat_turns()

    return {
        ("GET", "/api/metering"): _usage,
        ("GET", "/api/metering/tier"): _tier,
        ("GET", "/api/metering/can-submit"): _can_submit,
        ("POST", "/api/metering/submit"): _submit,
        ("GET", "/api/metering/token-balances"): _tokens,
        ("GET", "/api/metering/chat-turns"): _chat,
    }


def dispatch_metering(service: MeteringService, method: str, path: str,
                      body: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    return dispatch(routes_for_metering(service), method, path, body)
