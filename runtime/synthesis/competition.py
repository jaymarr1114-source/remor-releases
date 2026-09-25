"""
swarm_engine/synthesis/competition.py

Capability competition: when more than one implementation exists for the same
goal, keep them both and let evidence decide which is used — rather than the
first admitted candidate silently becoming permanent.

`acquire_capability` already ranks candidates within a single acquisition
call and keeps one winner; that is correct there — a single acquisition
should not register five implementations of the same brand-new capability.
This module is for the case that creates real evolutionary pressure: a goal
that already has a bound, working, deployed capability, and a *second*
independently-admitted implementation shows up later (from re-acquisition,
delegation, retrieval, or hand-registration) and needs to be compared against
the incumbent on real evidence rather than either being silently ignored or
silently replacing it.

A capability can be plan-based (in CapabilityStore, executed by the composer)
or code-based (in AcquiredCodeStore, executed as a registered primitive).
Competition treats both uniformly through a single executor abstraction, so a
synthesized plan and a generated-and-validated function compete on equal
terms — the pool does not care how a competitor was built, only what it does.
"""
from __future__ import annotations

import sqlite3
import statistics
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple


@dataclass
class CompetitorMetrics:
    capability_id: str
    correct: bool
    correctness_rate: float
    median_ms: float
    failure_rate: float
    runs: int

    def as_dict(self) -> Dict[str, Any]:
        return {"capability_id": self.capability_id, "correct": self.correct,
                "correctness_rate": round(self.correctness_rate, 3),
                "median_ms": round(self.median_ms, 4),
                "failure_rate": round(self.failure_rate, 3), "runs": self.runs}


@dataclass
class CompetitionResult:
    goal: str
    ranked: List[CompetitorMetrics] = field(default_factory=list)
    winner: Optional[str] = None
    reason: str = ""

    def as_dict(self) -> Dict[str, Any]:
        return {"goal": self.goal, "winner": self.winner, "reason": self.reason,
                "ranked": [c.as_dict() for c in self.ranked]}


class CompetitionPool:
    """Tracks competing implementations per goal and picks a winner on
    measured evidence: correctness first and always, then reliability, then
    speed. A faster but less correct competitor never outranks a correct one
    — the same ordering discipline `EvolutionEngine` already uses for 1-v-1
    comparison, generalized here to N-way.
    """

    def __init__(self, engine, db_path: Optional[str] = None):
        self.engine = engine
        self.db_path = db_path or getattr(engine.capabilities, "db_path",
                                          "swarm_engine.db")
        with self._conn() as conn:
            conn.execute("""CREATE TABLE IF NOT EXISTS competitors (
                goal TEXT NOT NULL, capability_id TEXT NOT NULL,
                registered_at REAL, PRIMARY KEY (goal, capability_id))""")
            # capabilities.resolve_goal only recognises plan-based capabilities
            # — it requires a full CapabilityRecord with a composer plan, which
            # a generated-and-registered function never has. Binding a
            # competition winner through it silently fails whenever the
            # winner happens to be code-based rather than plan-based. This
            # pool owns its own goal->winner mapping instead, so a win is
            # representable regardless of which kind of capability won.
            conn.execute("""CREATE TABLE IF NOT EXISTS goal_winners (
                goal TEXT PRIMARY KEY, capability_id TEXT NOT NULL, at REAL)""")

    def resolve(self, goal: str) -> Optional[str]:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT capability_id FROM goal_winners WHERE goal=?", (goal,)
            ).fetchone()
        return row[0] if row else None


    def _conn(self):
        return sqlite3.connect(self.db_path)

    def register(self, goal: str, capability_id: str) -> None:
        """Add a competitor without displacing whatever is currently bound —
        registering is not the same as winning."""
        with self._conn() as conn:
            conn.execute("""INSERT OR IGNORE INTO competitors
                (goal, capability_id, registered_at) VALUES (?,?,?)""",
                (goal, capability_id, time.time()))

    def competitors(self, goal: str) -> List[str]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT capability_id FROM competitors WHERE goal=?", (goal,)
            ).fetchall()
        return [r[0] for r in rows]

    def _executor_for(self, capability_id: str) -> Optional[Callable[[Dict], Any]]:
        """Produce a callable for a capability_id regardless of whether it is
        plan-based (CapabilityStore) or code-based (AcquiredCodeStore) — the
        one place that difference is resolved, so evaluation itself does not
        need to know or care.
        """
        record = self.engine.capabilities.get(capability_id)
        if record is not None:
            async def run_plan(args: Dict[str, Any]) -> Any:
                out = await self.engine.composer.execute(record.plan, dict(args))
                if not out.get("success"):
                    raise RuntimeError(str(out.get("error", "plan failed")))
                return out.get("value")
            return run_plan

        for entry in self.engine.acquired_code.all():
            if entry["capability_id"] == capability_id:
                name = entry["name"]
                async def run_primitive(args: Dict[str, Any], _name=name) -> Any:
                    return await self.engine.primitives.invoke(_name, None, **args)
                return run_primitive
        return None

    async def evaluate(self, goal: str, cases: List[Tuple[Dict[str, Any], Any]],
                       runs: int = 3) -> CompetitionResult:
        """Run every competitor against the same cases and rank them.

        Every competitor sees the same cases in the same order, run the same
        number of times, so a difference in the outcome reflects the
        implementation rather than which one happened to get an easier case.
        """
        result = CompetitionResult(goal=goal)
        ids = self.competitors(goal)
        if not ids:
            result.reason = "no registered competitors for this goal"
            return result

        for capability_id in ids:
            executor = self._executor_for(capability_id)
            if executor is None:
                result.ranked.append(CompetitorMetrics(
                    capability_id, False, 0.0, float("inf"), 1.0, 0))
                continue

            correct_runs, timings, failures = 0, [], 0
            total = 0
            for args, expected in cases:
                for _ in range(runs):
                    total += 1
                    started = time.perf_counter()
                    try:
                        value = await executor(args)
                        timings.append((time.perf_counter() - started) * 1000)
                        if value == expected:
                            correct_runs += 1
                    except Exception:
                        failures += 1

            result.ranked.append(CompetitorMetrics(
                capability_id=capability_id,
                correct=(correct_runs == total and total > 0),
                correctness_rate=correct_runs / max(1, total),
                median_ms=statistics.median(timings) if timings else float("inf"),
                failure_rate=failures / max(1, total), runs=total))

        # Correctness dominates absolutely; reliability (inverse failure
        # rate) breaks ties among the correct; speed only decides among
        # competitors that are otherwise equal on both.
        result.ranked.sort(key=lambda c: (not c.correct, c.failure_rate, c.median_ms))
        if result.ranked and result.ranked[0].correct:
            result.winner = result.ranked[0].capability_id
            result.reason = (f"won on {result.ranked[0].correctness_rate:.0%} "
                             f"correctness, {result.ranked[0].failure_rate:.0%} "
                             f"failure rate, {result.ranked[0].median_ms:.3f}ms median")
        else:
            result.reason = "no competitor was fully correct on the evaluation cases"
        return result

    async def promote_winner(self, goal: str, cases: List[Tuple[Dict[str, Any], Any]],
                             runs: int = 3) -> CompetitionResult:
        """Evaluate, then actually act on the result: bind the goal to the
        winner and demote the losers in the lifecycle graph, rather than
        just reporting a ranking nobody acts on.
        """
        result = await self.evaluate(goal, cases, runs)
        if result.winner is None:
            return result

        with self._conn() as conn:
            conn.execute("""INSERT OR REPLACE INTO goal_winners
                (goal, capability_id, at) VALUES (?,?,?)""",
                (goal, result.winner, time.time()))

        # Best-effort: if the winner happens to be plan-based, also bind it
        # through the capability store so the DIRECT arbitration tier can find
        # it the same way it finds any other synthesized capability. This is
        # additive, not required — resolve() above is the authoritative path
        # and works for both kinds.
        if self.engine.capabilities.get(result.winner) is not None:
            self.engine.capabilities.bind_goal(goal, result.winner)

        for competitor in result.ranked:
            if competitor.capability_id == result.winner:
                continue
            try:
                current = self.engine.lifecycle.state_of(competitor.capability_id)
                if current.value not in ("deprecated", "quarantined"):
                    self.engine.lifecycle.transition(
                        competitor.capability_id,
                        self.engine.lifecycle.state_of(competitor.capability_id),
                        reason="", force=True)  # no-op if already terminal
                    from swarm_engine.governance.lifecycle import LifecycleState
                    self.engine.lifecycle.transition(
                        competitor.capability_id, LifecycleState.DEPRECATED,
                        reason=f"lost capability competition for {goal!r} to "
                               f"{result.winner}", force=True)
            except Exception:
                pass  # lifecycle bookkeeping failure must not undo the promotion
        return result
