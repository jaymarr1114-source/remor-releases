"""ACQ-CTRL-1 -- the Acquisition Controller as a runtime entity.

Owns the acquisition loop's convergence mechanism: acquire -> verify ->
retry/expand -> resolve.

Ownership (deliberate, no conflict with the 2026-09-27 Run Controller
decision, and mirroring ACC-CTRL-1's established pattern): the Run
Controller owns acquisition-loop INVOCATION (when the loop runs in the
cadence; its inline gap sweep is untouched). This controller owns the
convergence MECHANISM (how the loop converges): the experience miner's
cadence, the gap-work orchestration through microcontrollers, and the
retry/expand/terminal policy. It SUBORDINATES the existing machinery --
GapRegistry (frozen, called never edited), the experience miner
(ACQ-MINE-1), the MicrocontrollerSubstrate (frozen, RUN-MICRO-1
interface) -- by wrapping and owning, never rewriting.

The executive's view stays O(1): loop_view() returns a LoopView --
"Acquisition is active" while the loop works, never microcontroller ids
or purposes.

Convergence policy (mechanical, documented here):
  acquire: open gaps, user-gaps first then oldest first (the Run
      Controller's ordering discipline, mirrored not duplicated),
      each dispatched through GapRegistry.dispatch inside one
      loop-rooted microcontroller (loop='acquisition').
  verify:  dispatch closes ONLY on verified utilization (the registry's
      guarantee); the controller observes and records the outcome.
  retry:   a gap left open is re-dispatched on later cycles while its
      attempt count (route-history entries with a route) stays below
      max_attempts.
  expand:  on the final permitted attempt the controller first re-runs
      the miner to broaden the evidence base, then dispatches -- trying
      differently, not just trying again.
  resolve: at max_attempts the gap is terminally classified "exhausted"
      with named reasons; the registry keeps it open honestly (its
      route history names every failure), the controller simply stops
      spending budget on it.

Seam (named, not rewired here): the RunController's tick still runs its
own inline gap sweep. This controller's run_cycle() is the canonical
convergence mechanism; delegating the tick's sweep to it is a run-track
wiring decision, out of scope for this mission.
"""

from __future__ import annotations

import time
from typing import Any, Dict, List, Optional

# ---------------------------------------------------------------------------
# the RUN-MICRO-1 seam -- the substrate module is FROZEN; this controller
# calls it, never edits it.
# ---------------------------------------------------------------------------

EXPECTED_INTERFACE = "microcontroller-interface/v1"


class AcquisitionController:
    """Owns acquire -> verify -> retry/expand -> resolve for the loop."""

    #: Retry budget per gap before terminal classification.
    DEFAULT_MAX_ATTEMPTS = 3
    #: Miner cadence: minimum seconds between mining passes.
    DEFAULT_MINE_INTERVAL_S = 3600.0
    #: Default budget for one hosted gap acquisition, seconds.
    DEFAULT_GAP_BUDGET_S = 120.0

    def __init__(self, *, registry: Any, substrate: Any,
                 miner: Any = None,
                 max_attempts: int = DEFAULT_MAX_ATTEMPTS,
                 mine_interval_s: float = DEFAULT_MINE_INTERVAL_S) -> None:
        self._registry = registry
        self._substrate = substrate
        self._miner = miner
        self._max_attempts = int(max_attempts)
        self._mine_interval_s = float(mine_interval_s)
        self._last_mine_ts: Optional[float] = None
        self._cycles = 0
        self._ensure_loop_registered()

    def _ensure_loop_registered(self) -> None:
        """The substrate's register_loop is idempotent; but another owner
        (the RunController, the resource arbitrator) may have registered
        the loop already with its own budget -- never clobber it."""
        try:
            self._substrate.loop_view("acquisition")
            return
        except Exception:
            pass
        self._substrate.register_loop(
            "acquisition", budget_s=3600.0, max_concurrent=16)

    # -- loop view ------------------------------------------------------
    def loop_view(self) -> Any:
        """The ONLY type the executive layer may consume."""
        return self._substrate.loop_view("acquisition")

    def loop_status(self) -> Dict[str, Any]:
        view = self.loop_view()
        state = getattr(view, "state", "unknown")
        return {"loop": "acquisition", "state": state,
                "cycles": self._cycles}

    # -- miner cadence (the controller owns the schedule) ----------------
    def _miner_due(self, *, force: bool = False) -> bool:
        if self._miner is None:
            return False
        if force:
            return True
        if self._last_mine_ts is None:
            return True
        return (time.monotonic() - self._last_mine_ts
                >= self._mine_interval_s)

    def run_miner(self, *, force: bool = False) -> Dict[str, Any]:
        """Run one mining pass if due. The miner never self-schedules."""
        if not self._miner_due(force=force):
            return {"mined": False, "reason": "cadence: not due",
                    "new_gaps": []}
        try:
            records = self._miner.mine_and_emit(self._registry)
        except Exception as exc:  # mining never kills the cycle
            return {"mined": False, "reason": f"{type(exc).__name__}: {exc}",
                    "new_gaps": []}
        self._last_mine_ts = time.monotonic()
        return {"mined": True,
                "new_gaps": [getattr(r, "gap_id", "?") for r in records]}

    # -- gap policy ------------------------------------------------------
    @staticmethod
    def _attempts(record: Any) -> int:
        """Dispatch attempts so far: every dispatch appends exactly one
        route-history entry (routed or not -- _log_run always records)."""
        hist = getattr(record, "route_history", None) or []
        return sum(1 for h in hist if isinstance(h, dict))

    @staticmethod
    def _is_user_gap(record: Any) -> bool:
        d = getattr(record, "dissatisfaction", None)
        return bool(d is not None and (d.feedback or d.attempt_ref))

    def _classify(self, record: Any) -> str:
        """acquire | expand | exhausted -- the retry/expand/terminal policy."""
        attempts = self._attempts(record)
        if attempts >= self._max_attempts:
            return "exhausted"
        if attempts == self._max_attempts - 1:
            return "expand"
        return "acquire"

    def _acquire_one(self, record: Any, budget_s: float) -> Dict[str, Any]:
        """Dispatch one gap inside one loop-rooted microcontroller."""
        gap_id = record.gap_id
        outcome: Dict[str, Any] = {
            "gap_id": gap_id, "policy": "", "spawned": False,
            "outcome": "error", "route": "", "detail": "",
        }
        policy = self._classify(record)
        outcome["policy"] = policy
        if policy == "exhausted":
            outcome["outcome"] = "exhausted"
            outcome["detail"] = (
                f"terminal: {self._attempts(record)} attempts "
                f"(max {self._max_attempts}); reasons in route history")
            return outcome
        if policy == "expand":
            # Trying differently, not just trying again: broaden the
            # evidence base before the final attempt.
            mine_report = self.run_miner(force=True)
            outcome["expand_mined"] = mine_report.get("new_gaps", [])
        spawn = self._substrate.spawn(
            loop="acquisition", purpose=f"acquire:{gap_id}",
            budget_s=budget_s)
        if not spawn.ok:
            outcome["outcome"] = "error"
            outcome["detail"] = (
                "spawn refused: "
                f"{spawn.refusal.reason if spawn.refusal else 'unknown'}")
            return outcome
        outcome["spawned"] = True
        mc_id = spawn.mc.mc_id
        mc_outcome = "exhausted"
        try:
            result = self._registry.dispatch(gap_id)
            outcome["outcome"] = result.outcome
            outcome["route"] = result.route_name or ""
            outcome["detail"] = (result.detail or "")[:500]
            mc_outcome = ("resolved" if result.outcome == "closed"
                          else "exhausted")
        except Exception as exc:  # dispatch never kills the cycle
            outcome["detail"] = f"{type(exc).__name__}: {exc}"[:500]
        finally:
            self._substrate.retire(mc_id, outcome=mc_outcome,
                                   loop="acquisition")
        outcome["mc_outcome"] = mc_outcome
        return outcome

    # -- the cycle --------------------------------------------------------
    def run_cycle(self, *, budget_s: float = 300.0,
                  max_gaps: int = 10,
                  mine: bool = True) -> Dict[str, Any]:
        """One convergence cycle: mine on cadence, then work the gap queue.

        Returns a report dict. Never raises: every subordinate failure is
        recorded, never propagated.
        """
        report: Dict[str, Any] = {
            "cycle": self._cycles + 1, "mined": {}, "gaps": [],
            "closed": 0, "open": 0, "exhausted": 0, "errors": [],
        }
        deadline = time.monotonic() + max(1.0, float(budget_s))
        try:
            if mine:
                report["mined"] = self.run_miner()
            try:
                open_gaps = self._registry.list_gaps(status="open")
            except Exception as exc:
                report["errors"].append(f"list_gaps: {exc}")
                open_gaps = []
            # user-gaps first, then oldest first (mirrors the tick's
            # ordering discipline).
            open_gaps.sort(key=lambda g: (
                0 if self._is_user_gap(g) else 1,
                getattr(g, "registered_at", 0.0)))
            gap_budget = max(1.0, float(budget_s) / max(1, max_gaps))
            for record in open_gaps[:max(1, int(max_gaps))]:
                if time.monotonic() >= deadline:
                    break
                one = self._acquire_one(record, gap_budget)
                report["gaps"].append(one)
                if one["outcome"] == "closed":
                    report["closed"] += 1
                elif one["outcome"] == "exhausted":
                    report["exhausted"] += 1
                elif one["outcome"] == "open":
                    report["open"] += 1
                else:
                    report["errors"].append(
                        f"{one['gap_id']}: {one['detail'][:120]}")
        except Exception as exc:  # the cycle never raises
            report["errors"].append(f"cycle: {type(exc).__name__}: {exc}")
        self._cycles += 1
        report["loop_view"] = self.loop_view().as_dict()
        return report
