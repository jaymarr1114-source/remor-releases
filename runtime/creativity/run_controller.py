"""The dedicated conditional Creativity Run Controller (paper T5, D-6; §11 Q6).

The controller owns the LIFECYCLE of a creative run — not the stage
machine (that is the executive's, EXEC-1). Its job:

- Conditional activation: explicit predicates evaluated synchronously
  on every run() call. When unmet, the controller is dormant — provably
  doing nothing. There is no start()/loop(), no polling, no background
  threads: activation is caller-driven, never continuous.
- Refinement-loop ownership: the required turn bound (a James-gated
  value — never invented here, never defaulted) is passed through to
  the executive's commission()/run(); the executive's bound + v1
  adaptive stop govern the turns. Bound exhaustion without release
  criteria is a first-class result (release with state/evidence or
  stop), never an error and never another turn.
- Budget wiring: every run opens with a real request_budget() through
  the FRM path; spend is recorded from measured actuals at close;
  BudgetRefused at open means the run never starts; a ceiling hit
  yields the CeilingStop path with full preservation and no further
  spend afterward. Preemption mid-execution is the kill ladder's job —
  the budget enforces at admission (open) and on actuals (close);
  the halt thereafter is non-cooperative.
- Kill: the kill ladder (a threading.Event shared with the executive)
  stops a run cold at the next step boundary, state preserved.
- Run records: every run() call — dormant or active — produces an
  auditable RunRecord.

Resumption is explicitly OUT OF SCOPE (documented, not silently
absent): after KILLED or a ceiling halt, run() starts a NEW run_id.
A halted work_id refuses at the budget predicate until the halt is
lifted by the budget layer, which this controller does not do.

Wiring requirement: pass the SAME threading.Event to the controller
that was given to the executive at construction — the controller
cannot verify the executive's private stop event, so this is a
documented wiring contract, asserted by the battery through behavior
(kill delivery observed end-to-end).
"""

from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from runtime.creativity.executive import (
    CreativityExecutiveController,
    ExecutiveRefused,
)
from runtime.creativity.intent import CreativeIntent
from runtime.creativity.budget import (
    BudgetHalted,
    BudgetRefused,
    CeilingStop,
    CreativityBudget,
)


class RunControllerRefused(Exception):
    """The run controller refused an operation: exact reason, never silent."""


# Run-record statuses. DORMANT/REFUSED are pre-run; the rest mirror the
# executive outcome; ERROR is an unexpected failure, recorded not swallowed.
DORMANT = "dormant"
REFUSED = "refused"
COMPLETED = "completed"
SUSPENDED = "suspended"
KILLED = "killed"
STOPPED = "stopped"
ERROR = "error"

_EXECUTIVE_STATUSES = (COMPLETED, SUSPENDED, KILLED, STOPPED)


@dataclass(frozen=True)
class PredicateOutcome:
    """One activation predicate, evaluated with its reason."""
    name: str
    met: bool
    reason: str


@dataclass
class RunRecord:
    """The auditable record of one run() call, dormant or active."""
    run_id: str
    work_id: str
    intent_summary: str
    refinement_bound: int
    predicates: Tuple[PredicateOutcome, ...] = ()
    status: str = DORMANT
    outcome_status: Optional[str] = None  # executive outcome, when a run executed
    outcome_detail: str = ""              # verbatim, never paraphrased
    grant_issued: bool = False
    grant_size_s: float = 0.0
    spend_s: float = 0.0
    ceiling_stop: Optional[Dict[str, Any]] = None
    kill_seen: bool = False
    error: str = ""


class CreativityRunController:
    """Lifecycle owner for creative runs (paper T5, D-6).

    The executive drives stages; this controller decides WHETHER a run
    happens, under WHAT bound, with WHAT budget, and records everything.
    """

    def __init__(self, *, executive: CreativityExecutiveController,
                 budget: CreativityBudget,
                 refinement_bound: int,
                 work_id: str,
                 kill_event: threading.Event) -> None:
        if not isinstance(executive, CreativityExecutiveController):
            raise RunControllerRefused(
                "run controller needs the CreativityExecutiveController, got "
                f"{type(executive).__name__} — it drives the executive, "
                "it does not replace it")
        if not isinstance(budget, CreativityBudget):
            raise RunControllerRefused(
                "run controller needs a CreativityBudget, got "
                f"{type(budget).__name__} — spend flows through the FRM path")
        # The bound's VALUE is James's gate (§11 Q6): required, never
        # defaulted, never invented. A run that cannot take a turn is not
        # a run — 0/negative is refused fail-closed here, not at run time.
        if not isinstance(refinement_bound, int) or refinement_bound < 1:
            raise RunControllerRefused(
                "refinement_bound is a required positive int "
                f"(§11 Q6, James's gate): got {refinement_bound!r} — "
                "a run that cannot take a turn is not a run")
        if not work_id or not str(work_id).strip():
            raise RunControllerRefused(
                "run controller needs a non-empty work_id — spend, grants "
                "and halts are accounted per work item")
        if not isinstance(kill_event, threading.Event):
            raise RunControllerRefused(
                "run controller needs a threading.Event as the kill signal, "
                f"got {type(kill_event).__name__} — pass the SAME event "
                "given to the executive at construction")
        self._executive = executive
        self._budget = budget
        self._refinement_bound = refinement_bound
        self._work_id = str(work_id)
        self._kill_event = kill_event

    @property
    def kill_event(self) -> threading.Event:
        return self._kill_event

    @property
    def refinement_bound(self) -> int:
        return self._refinement_bound

    def kill(self, reason: str = "") -> None:
        """The kill ladder's handle: set the shared event.

        The executive polls it between steps; the run stops cold at the
        next step boundary with state preserved.
        """
        self._kill_event.set()

    # -- activation predicates -------------------------------------------
    def _predicates(self, intent: Any,
                    estimated_compute_s: float
                    ) -> Tuple[List[PredicateOutcome], Any]:
        """Evaluate activation predicates in side-effect order.

        Kill, intent, and executive checks are pure; the budget check is
        LAST because it has a real side effect (the grant issuance that
        opens every run). A grant therefore implies all prior predicates
        were met.
        """
        outcomes: List[PredicateOutcome] = []
        grant = None

        if self._kill_event.is_set():
            outcomes.append(PredicateOutcome(
                "no_kill_active", False,
                "kill event is set — the run stays dormant"))
        else:
            outcomes.append(PredicateOutcome(
                "no_kill_active", True, "kill event clear"))

        if not isinstance(intent, CreativeIntent) or not intent.outcome \
                or not str(intent.outcome).strip():
            outcomes.append(PredicateOutcome(
                "intent_present", False,
                "no commissioned intent — there is nothing to run"))
        else:
            outcomes.append(PredicateOutcome(
                "intent_present", True,
                f"commissioned outcome: {str(intent.outcome)[:80]!r}"))

        # Checked at construction (fail-closed); re-asserted here so the
        # record always shows the full predicate set evaluated.
        outcomes.append(PredicateOutcome(
            "executive_runnable", True,
            "executive is the CreativityExecutiveController (construction)"))

        # Short-circuit: the budget check has a real side effect (grant
        # issuance), so it runs ONLY when every pure predicate is met.
        # A dormant run touches nothing — no grant, no spend.
        if not all(p.met for p in outcomes):
            return outcomes, None

        if not isinstance(estimated_compute_s, (int, float)) \
                or estimated_compute_s < 0:
            outcomes.append(PredicateOutcome(
                "budget_granted", False,
                f"estimated_compute_s must be >= 0, got "
                f"{estimated_compute_s!r}"))
            return outcomes, None

        try:
            grant = self._budget.request_budget(
                work_id=self._work_id,
                estimated_compute_s=float(estimated_compute_s),
                note=f"creativity run controller open (bound "
                     f"{self._refinement_bound})")
        except BudgetRefused as exc:
            outcomes.append(PredicateOutcome(
                "budget_granted", False,
                f"budget refused at open — the run never starts: {exc}"))
            return outcomes, None
        except BudgetHalted as exc:
            outcomes.append(PredicateOutcome(
                "budget_granted", False,
                f"work is halted at the budget ceiling: {exc}"))
            return outcomes, None

        outcomes.append(PredicateOutcome(
            "budget_granted", True,
            f"FRM grant issued for {float(estimated_compute_s):.2f}s "
            f"estimated (work {self._work_id!r})"))
        return outcomes, grant

    # -- the run -----------------------------------------------------------
    def run(self, intent: Any, *, estimated_compute_s: float,
            constraints: Sequence[Dict[str, Any]] = ()) -> RunRecord:
        """Run one creative lifecycle: predicates → commission → run.

        Returns a RunRecord in ALL cases — dormant, refused, completed,
        suspended, killed, stopped, or error. Nothing is silent.
        """
        run_id = uuid.uuid4().hex[:12]
        intent_summary = (str(intent.outcome)[:80]
                          if isinstance(intent, CreativeIntent)
                          and intent.outcome else "<none>")
        record = RunRecord(
            run_id=run_id, work_id=self._work_id,
            intent_summary=intent_summary,
            refinement_bound=self._refinement_bound)

        predicates, grant = self._predicates(intent, estimated_compute_s)
        record.predicates = tuple(predicates)
        if grant is not None:
            record.grant_issued = True
            record.grant_size_s = float(estimated_compute_s)

        if not all(p.met for p in predicates):
            record.status = DORMANT
            record.error = "; ".join(
                f"{p.name}: {p.reason}" for p in predicates if not p.met)
            return record

        # All predicates met (budget granted last, so the grant is live).
        try:
            commission = self._executive.commission(
                intent, refinement_bound=self._refinement_bound,
                constraints=tuple(constraints))
        except ExecutiveRefused as exc:
            # The grant was issued but no run starts: granted-but-unspent
            # is honest accounting, visible in the spend ledger.
            record.status = REFUSED
            record.error = (f"commission refused: {exc} "
                            "(grant issued but unspent)")
            return record

        t0 = time.perf_counter()
        try:
            outcome = self._executive.run(commission)
        except Exception as exc:  # recorded, never silent
            record.status = ERROR
            record.error = f"{type(exc).__name__}: {exc}"
            return record
        dt = time.perf_counter() - t0

        record.outcome_status = outcome.status
        record.outcome_detail = outcome.detail
        record.status = (outcome.status if outcome.status
                         in _EXECUTIVE_STATUSES else ERROR)
        if record.status == ERROR:
            record.error = (f"executive returned unknown status "
                            f"{outcome.status!r}: {outcome.detail}")

        # Spend the measured actuals. The ceiling is enforced here, on
        # actuals — a hit yields the CeilingStop path with full
        # preservation; afterward the work is halted (non-cooperative:
        # no further spend accrues). Preemption mid-execution is the
        # kill ladder's job, not the budget's.
        try:
            stop = self._budget.record_spend(
                self._work_id, dt,
                checkpoints={"run_id": run_id,
                             "outcome": outcome.status,
                             "detail": outcome.detail[:200]})
        except BudgetHalted as exc:
            record.spend_s = dt
            record.ceiling_stop = {
                "reason": f"already halted: {exc}",
                "preservation": {},
            }
            record.kill_seen = self._kill_event.is_set()
            return record
        record.spend_s = dt
        if stop is not None:
            record.ceiling_stop = {
                "reason": stop.reason,
                "preservation_keys": sorted(stop.preservation.keys()),
            }

        record.kill_seen = self._kill_event.is_set()
        return record
