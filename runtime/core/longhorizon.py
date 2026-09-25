"""
swarm_engine/core/longhorizon.py

Checkpointed long-horizon execution.

An objective of a hundred steps that loses everything when step 47 fails is
not autonomy, it is a long single attempt. The difference this module makes is
that steps 1-46 remain valid: the engine records what succeeded, diagnoses why
47 failed, repairs or replans that step alone, and resumes — rather than
restarting from the top or reporting total failure.

Three behaviours make that real:

  CHECKPOINTS   every completed step is persisted with its output, so a
                process killed at step 47 restarts at 47, not at 1.
  REPLANNING    the initial plan is not sacred. A failed step is diagnosed and
                retried by a different route before the objective is abandoned.
  ACQUIRE       when the diagnosis is that no capability exists, acquisition
                is attempted mid-run and the plan continues with the new
                capability, which is where acquisition and planning converge.

Progress is measured in completed steps rather than success/failure, because
"got 46 of 50 steps done and stalled on a missing dependency" is a materially
different report from "failed", and only one of them is actionable.
"""
from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple


class StepState(Enum):
    PENDING = "pending"
    COMPLETED = "completed"
    FAILED = "failed"
    REPAIRED = "repaired"     # failed, then succeeded by another route
    SKIPPED = "skipped"


@dataclass
class Step:
    index: int
    goal: str
    payload: Dict[str, Any] = field(default_factory=dict)
    state: StepState = StepState.PENDING
    value: Any = None
    error: str = ""
    attempts: int = 0
    route: str = ""

    def as_dict(self) -> Dict[str, Any]:
        return {"index": self.index, "goal": self.goal, "state": self.state.value,
                "value": self.value, "error": self.error[:200],
                "attempts": self.attempts, "route": self.route}


class CheckpointStore:
    """Per-step persistence. Completed work outlives the process."""

    def __init__(self, db_path: str = "swarm_engine.db"):
        self.db_path = db_path
        with self._conn() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS checkpoints (
                    objective_id TEXT NOT NULL,
                    step_index INTEGER NOT NULL,
                    goal TEXT, payload TEXT, state TEXT, value TEXT,
                    error TEXT, attempts INTEGER, route TEXT, at REAL,
                    PRIMARY KEY (objective_id, step_index)
                )""")

    def _conn(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def save(self, objective_id: str, step: Step) -> None:
        with self._conn() as conn:
            conn.execute("""INSERT OR REPLACE INTO checkpoints
                (objective_id, step_index, goal, payload, state, value, error,
                 attempts, route, at) VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (objective_id, step.index, step.goal,
                 json.dumps(step.payload, default=str), step.state.value,
                 json.dumps(step.value, default=str), step.error,
                 step.attempts, step.route, time.time()))

    def load(self, objective_id: str) -> List[Step]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM checkpoints WHERE objective_id=? ORDER BY step_index",
                (objective_id,)).fetchall()
        steps = []
        for row in rows:
            try:
                state = StepState(row["state"])
            except ValueError:
                state = StepState.FAILED
            try:
                value = json.loads(row["value"] or "null")
                payload = json.loads(row["payload"] or "{}")
            except json.JSONDecodeError:
                # A damaged checkpoint invalidates that step only. Treating it
                # as fatal would throw away every step that completed cleanly.
                value, payload, state = None, {}, StepState.FAILED
            steps.append(Step(index=row["step_index"], goal=row["goal"],
                              payload=payload, state=state, value=value,
                              error=row["error"] or "", attempts=row["attempts"] or 0,
                              route=row["route"] or ""))
        return steps

    def resume_index(self, objective_id: str) -> int:
        """The first step that still needs doing."""
        steps = self.load(objective_id)
        for step in steps:
            if step.state not in (StepState.COMPLETED, StepState.REPAIRED,
                                  StepState.SKIPPED):
                return step.index
        return len(steps)


@dataclass
class HorizonResult:
    objective_id: str
    completed: int = 0
    total: int = 0
    failed_at: Optional[int] = None
    resumed_from: int = 0
    replans: int = 0
    acquisitions: int = 0
    value: Any = None
    steps: List[Dict[str, Any]] = field(default_factory=list)
    reason: str = ""

    @property
    def finished(self) -> bool:
        return self.total > 0 and self.completed == self.total

    @property
    def progress(self) -> float:
        return self.completed / self.total if self.total else 0.0

    def as_dict(self) -> Dict[str, Any]:
        return {"objective_id": self.objective_id, "finished": self.finished,
                "progress": round(self.progress, 3),
                "completed": self.completed, "total": self.total,
                "failed_at": self.failed_at, "resumed_from": self.resumed_from,
                "replans": self.replans, "acquisitions": self.acquisitions,
                "value": self.value, "reason": self.reason, "steps": self.steps}


class LongHorizonRunner:
    """Executes a multi-step objective with checkpointing and replanning."""

    def __init__(self, engine, checkpoints: Optional[CheckpointStore] = None,
                 max_attempts_per_step: int = 3):
        self.engine = engine
        self.checkpoints = checkpoints or CheckpointStore(
            db_path=getattr(engine.capabilities, "db_path", "swarm_engine.db"))
        self.max_attempts_per_step = max_attempts_per_step

    async def run(self, objective_id: str, steps: List[Tuple[str, Dict[str, Any]]],
                  thread_results: bool = True) -> HorizonResult:
        existing = {s.index: s for s in self.checkpoints.load(objective_id)}
        result = HorizonResult(objective_id=objective_id, total=len(steps))
        result.resumed_from = self.checkpoints.resume_index(objective_id)

        carried: Any = None
        for index, (goal, payload) in enumerate(steps):
            prior = existing.get(index)
            if prior and prior.state in (StepState.COMPLETED, StepState.REPAIRED):
                # Completed work is never redone. This is the whole point of a
                # checkpoint: a restart costs the failed step, not the run.
                result.completed += 1
                carried = prior.value
                result.steps.append(prior.as_dict())
                continue

            step = Step(index=index, goal=goal, payload=dict(payload))
            if thread_results and carried is not None and not step.payload:
                step.payload = self._thread(carried)

            outcome = await self._attempt(step, result)
            self.checkpoints.save(objective_id, step)
            result.steps.append(step.as_dict())

            if step.state in (StepState.COMPLETED, StepState.REPAIRED):
                result.completed += 1
                carried = step.value
                continue

            # Stop at the first unrepairable step, but keep everything before
            # it. Partial progress is the report.
            result.failed_at = index
            result.value = carried
            result.reason = (f"step {index} ({goal!r}) could not be completed "
                             f"after {step.attempts} attempt(s): {step.error[:160]}")
            return result

        result.value = carried
        result.reason = "all steps completed"
        return result

    async def _attempt(self, step: Step, result: HorizonResult) -> None:
        """Run one step, replanning and acquiring as needed."""
        for attempt in range(1, self.max_attempts_per_step + 1):
            step.attempts = attempt
            task_id = self.engine.submit_task(step.goal, payload=step.payload)
            outcome = await self.engine.run_task(task_id)

            if outcome.get("success"):
                step.state = (StepState.COMPLETED if attempt == 1
                              else StepState.REPAIRED)
                raw = outcome.get("result")
                if isinstance(raw, dict):
                    if "value" in raw and not isinstance(raw.get("value"), dict):
                        step.value = raw["value"]
                    elif isinstance(raw.get("result"), dict) and "value" in raw["result"]:
                        step.value = raw["result"]["value"]
                    elif "value" in raw:
                        step.value = raw["value"]
                    else:
                        step.value = raw
                else:
                    step.value = raw
                step.route = f"attempt {attempt}"
                return

            step.error = str(outcome.get("error") or "step failed")
            diagnosis = self.engine.diagnoser.diagnose(step.error)

            # Escalation is terminal by design: retrying a permission failure
            # cannot succeed and only burns budget.
            if diagnosis.action.value == "escalate":
                step.state = StepState.FAILED
                step.route = f"escalated: {diagnosis.kind.value}"
                return

            if diagnosis.action.value == "acquire":
                acquired = await self._acquire_for(step)
                result.acquisitions += 1
                if acquired:
                    step.route = "acquired a missing capability"
                    continue

            ok, value, attempts = await self.engine.recovery.recover(
                step.goal, step.payload, step.error)
            result.replans += 1
            if ok:
                step.state = StepState.REPAIRED
                step.value = value
                step.route = "; ".join(a.detail for a in attempts)[:120]
                return

        step.state = StepState.FAILED

    async def _acquire_for(self, step: Step) -> bool:
        """Try to acquire what the failed step needed."""
        try:
            gap = self.engine.gaps.detect(step.goal)
            if not gap.has_gap:
                return False
            outcome = await self.engine.acquire_for_goal(step.goal)
            return bool(outcome.get("acquired"))
        except Exception:
            return False

    def _thread(self, value: Any) -> Dict[str, Any]:
        payload: Dict[str, Any] = {"input": value}
        if isinstance(value, list):
            payload["values"] = value
        elif isinstance(value, str):
            payload["text"] = value
        return payload
