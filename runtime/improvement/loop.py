"""
swarm_engine/improvement/loop.py

Failure diagnosis with layered recovery, and capability evolution.

Two loops live here.

RECOVERY answers "this failed — now what?" by classifying the failure and
matching it to a strategy, because the right response depends entirely on the
cause. A type error wants a different plan; a denied permission wants an
escalation to the operator, not a retry; a missing primitive wants
resynthesis; a timeout wants a retry with backoff. Retrying everything is how
a system turns one failure into a hundred.

EVOLUTION answers "this works — can it work better?" A capability is only
replaced when a candidate beats it on the evidence: same correctness, better
cost, measured over repeated runs rather than one lucky timing. The incumbent
is always retained so promotion is reversible, and a promoted capability that
regresses in use is rolled back automatically.

Neither loop invents improvements out of nothing. Both draw candidates from
the planner's alternative proposals, so everything they produce is already
type-checked, governed and admissible.
"""
from __future__ import annotations

import asyncio
import statistics
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Tuple


class FailureKind(Enum):
    PERMISSION = "permission"          # governor refused
    MISSING_PRIMITIVE = "missing_primitive"
    TYPE_ERROR = "type_error"
    BAD_ARGUMENT = "bad_argument"
    TIMEOUT = "timeout"
    RESOURCE = "resource"
    EXTERNAL = "external"              # network/provider unavailable
    LOGIC = "logic"                    # ran, produced the wrong answer
    STRATEGY_FAILURE = "strategy_failure"  # tried admissible strategies, none worked
    UNKNOWN = "unknown"


class RecoveryAction(Enum):
    REPLAN = "replan"                  # ask the planner for a different plan
    RESYNTHESIZE = "resynthesize"      # rebuild from primitives
    SUBSTITUTE = "substitute"          # swap in an alternative capability
    RETRY = "retry"                    # transient; try again with backoff
    ACQUIRE = "acquire"                # the vocabulary genuinely lacks this
    ESCALATE = "escalate"              # needs a human decision
    ROLLBACK = "rollback"              # revert to the previous version
    ABANDON = "abandon"                # no strategy applies


@dataclass
class Diagnosis:
    kind: FailureKind
    action: RecoveryAction
    detail: str
    retryable: bool = False
    confidence: float = 0.0

    def as_dict(self) -> Dict[str, Any]:
        return {"kind": self.kind.value, "action": self.action.value,
                "detail": self.detail[:300], "retryable": self.retryable,
                "confidence": round(self.confidence, 2)}


class FailureDiagnoser:
    """Classifies a failure and chooses a recovery strategy.

    Matching is on the error text because that is what the layers below
    actually produce. Each rule carries a confidence so an unrecognised failure
    is reported as unrecognised rather than being forced into the nearest
    category — a wrong diagnosis costs more than an honest "unknown", since it
    sends the engine down a recovery path that cannot possibly work.
    """

    RULES: List[Tuple[Tuple[str, ...], FailureKind, RecoveryAction, bool, float]] = [
        (("permissionerror", "denied:", "no grant for"),
         FailureKind.PERMISSION, RecoveryAction.ESCALATE, False, 0.95),
        (("unknown primitive", "missing primitive"),
         FailureKind.MISSING_PRIMITIVE, RecoveryAction.RESYNTHESIZE, False, 0.9),
        (("expects", "got", "typeerror", "type mismatch"),
         FailureKind.TYPE_ERROR, RecoveryAction.REPLAN, False, 0.8),
        (("missing plan parameter", "missing required argument", "unexpected argument"),
         FailureKind.BAD_ARGUMENT, RecoveryAction.REPLAN, False, 0.85),
        (("timeout", "timed out", "exceeded time budget"),
         FailureKind.TIMEOUT, RecoveryAction.RETRY, True, 0.85),
        (("memory", "resource", "exceeded size budget", "budget"),
         FailureKind.RESOURCE, RecoveryAction.RETRY, True, 0.7),
        (("name or service not known", "connection", "urlerror", "unreachable",
          "temporary failure"),
         FailureKind.EXTERNAL, RecoveryAction.RETRY, True, 0.8),
        (("no plan could be composed", "none passed admission"),
         FailureKind.MISSING_PRIMITIVE, RecoveryAction.ACQUIRE, False, 0.75),
        (("expected", "assertion", "wrong result"),
         FailureKind.LOGIC, RecoveryAction.SUBSTITUTE, False, 0.6),
    ]

    def diagnose(self, error: Any) -> Diagnosis:
        text = str(error or "").lower()
        if not text:
            return Diagnosis(FailureKind.UNKNOWN, RecoveryAction.ABANDON,
                             "no error detail was reported", False, 0.0)

        if ("no viable strategy" in text or "strategy exhaustion" in text
                or "all strategies failed" in text):
            return Diagnosis(FailureKind.STRATEGY_FAILURE,
                             RecoveryAction.REPLAN, str(error), False, 0.8)

        for tokens, kind, action, retryable, confidence in self.RULES:
            if any(token in text for token in tokens):
                return Diagnosis(kind, action, str(error), retryable, confidence)

        return Diagnosis(FailureKind.UNKNOWN, RecoveryAction.REPLAN, str(error),
                         False, 0.2)


@dataclass
class RecoveryAttempt:
    action: RecoveryAction
    succeeded: bool
    detail: str = ""
    value: Any = None
    # Which phase produced this record: "candidate_execution" for the
    # recovery's own attempt at a plan, "independent_validation" for the
    # fresh re-execution + semantic-bar check that must pass before a
    # candidate counts as repaired. A plan that executed but failed
    # validation stays visible here instead of silently counting as fixed.
    phase: str = "candidate_execution"


class RecoveryEngine:
    """Applies the diagnosed strategy, in order, until one works.

    Strategies are bounded and non-repeating: each is tried at most once per
    failure so a systematically broken goal cannot loop. Escalation is a real
    terminal state, not a disguised retry.

    A candidate repair is only reported as recovered after independent
    validation: the winning plan is re-executed fresh through the composer
    (the recovery's own outcome is never trusted) and must still clear the
    semantic-matcher's sufficiency bar against the goal. Validation is
    recorded as its own attempt phase, so a plan that executed but failed
    validation shows up honestly in the attempt history rather than
    silently counting as a repair.
    """

    def __init__(self, planner, composer, diagnoser: Optional[FailureDiagnoser] = None,
                 max_attempts: int = 3, backoff_s: float = 0.05, matcher=None):
        self.planner = planner
        self.composer = composer
        self.diagnoser = diagnoser or FailureDiagnoser()
        self.max_attempts = max_attempts
        self.backoff_s = backoff_s
        # Recovery must apply the same semantic bar as synthesis. Without it,
        # a blind-search plan that merely executes counts as a recovery, and
        # the engine reports a nonsense goal as repaired -- which is worse
        # than the original failure, because the failure was at least honest.
        if matcher is None:
            from swarm_engine.acquisition.semantic import SemanticMatcher
            matcher = SemanticMatcher()
        self.matcher = matcher

    async def recover(self, goal: str, args: Dict[str, Any], error: Any,
                      exclude_ops: Optional[List[List[str]]] = None
                      ) -> Tuple[bool, Any, List[RecoveryAttempt]]:
        diagnosis = self.diagnoser.diagnose(error)
        attempts: List[RecoveryAttempt] = []
        tried = set(tuple(o) for o in (exclude_ops or []))

        plan_order = self._strategy_order(diagnosis)
        for action in plan_order[: self.max_attempts]:
            if action in (RecoveryAction.ESCALATE, RecoveryAction.ABANDON):
                attempts.append(RecoveryAttempt(
                    action, False,
                    f"{diagnosis.kind.value} requires intervention: {diagnosis.detail[:160]}"))
                return False, None, attempts

            if action is RecoveryAction.RETRY:
                await asyncio.sleep(self.backoff_s)

            ok, value, detail, proposal = await self._try_alternative(goal, args, tried)
            attempts.append(RecoveryAttempt(action, ok, detail, value,
                                            phase="candidate_execution"))
            if not ok or proposal is None:
                continue

            # Independent validation before the repair counts: a fresh
            # re-execution of the winning plan (never the recovery's own
            # outcome) plus the semantic-matcher's sufficiency bar against
            # the goal. A candidate that executes but fails validation is
            # not a repair -- keep trying the remaining alternatives.
            validated, validation_detail = await self._validate_recovery(
                goal, args, proposal)
            attempts.append(RecoveryAttempt(action, validated, validation_detail,
                                            value if validated else None,
                                            phase="independent_validation"))
            if validated:
                return True, value, attempts
            # The winning plan's signature is already in `tried`, so it
            # cannot be re-offered; the loop moves on to the next action.

        return False, None, attempts

    def _strategy_order(self, diagnosis: Diagnosis) -> List[RecoveryAction]:
        """The diagnosed action first, then sensible fallbacks. A confident
        diagnosis of a non-recoverable failure is not softened by appending
        recoverable strategies behind it."""
        primary = diagnosis.action
        if primary in (RecoveryAction.ESCALATE, RecoveryAction.ABANDON):
            return [primary]
        order = [primary]
        for action in (RecoveryAction.REPLAN, RecoveryAction.SUBSTITUTE,
                       RecoveryAction.RESYNTHESIZE):
            if action not in order:
                order.append(action)
        return order

    async def _try_alternative(self, goal: str, args: Dict[str, Any],
                               tried: set) -> Tuple[bool, Any, str, Any]:
        """Try untried alternative plans. Returns (ok, value, detail,
        proposal): `proposal` is the winning plan's proposal when ok is
        True, retained so the caller can independently re-execute it for
        validation; None otherwise."""
        for proposal in self.planner.propose(goal, limit=5):
            signature = tuple(proposal.ops_used)
            if signature in tried:
                continue
            tried.add(signature)
            analysis = self.composer.analyze(proposal.plan)
            if not analysis.ok:
                continue
            if not proposal.strategy.startswith("template"):
                match = self.matcher.score(
                    goal, description=" ".join(proposal.ops_used),
                    ops_used=proposal.ops_used, strategy=proposal.strategy)
                if not match.sufficient:
                    continue
            outcome = await self.composer.execute(proposal.plan, dict(args))
            if outcome.get("success"):
                return (True, outcome.get("value"),
                        f"recovered via {proposal.strategy}", proposal)
        return False, None, "no untried alternative plan succeeded", None

    async def _validate_recovery(self, goal: str, args: Dict[str, Any],
                                 proposal) -> Tuple[bool, str]:
        """Independently validate a candidate repair.

        The winning plan is re-executed fresh through the composer -- the
        recovery's own outcome is deliberately not reused, so a plan that
        only worked because of transient state cannot pass -- and must
        still clear the same semantic-match sufficiency bar applied before
        execution. A plan that executes but no longer means the goal is not
        a repair.
        """
        try:
            fresh = await self.composer.execute(proposal.plan, dict(args))
        except Exception as exc:
            return False, (f"independent validation failed: fresh "
                           f"re-execution of the recovery plan raised {exc}")
        if not fresh.get("success"):
            return False, ("independent validation failed: fresh re-execution "
                           "of the recovery plan did not succeed")
        if not proposal.strategy.startswith("template"):
            match = self.matcher.score(
                goal, description=" ".join(proposal.ops_used),
                ops_used=proposal.ops_used, strategy=proposal.strategy)
            if not match.sufficient:
                return False, (f"independent validation failed: the plan "
                               f"executes but no longer clears the semantic "
                               f"bar for the goal (score {match.score:.3f})")
        return True, (f"independent validation passed: fresh re-execution "
                      f"succeeded and the semantic bar was met "
                      f"(strategy {proposal.strategy})")


# ---------------------------------------------------------------------------
# EVOLUTION
# ---------------------------------------------------------------------------

@dataclass
class Measurement:
    correct: bool
    latency_ms: float
    runs: int
    failures: int = 0

    def dominates(self, other: "Measurement", margin: float = 0.15) -> bool:
        """Better only if at least as correct and meaningfully faster.

        The margin exists because two timings that differ by 3% differ by
        noise, and promoting on noise produces churn that looks like progress.
        """
        if not self.correct:
            return False
        if other.correct and self.latency_ms > other.latency_ms * (1 - margin):
            return False
        return True


@dataclass
class EvolutionResult:
    promoted: bool
    goal: str
    incumbent_id: Optional[str] = None
    challenger_id: Optional[str] = None
    incumbent: Optional[Measurement] = None
    challenger: Optional[Measurement] = None
    reason: str = ""

    def as_dict(self) -> Dict[str, Any]:
        def m(x):
            return None if x is None else {"correct": x.correct,
                                           "latency_ms": round(x.latency_ms, 3),
                                           "runs": x.runs, "failures": x.failures}
        return {"promoted": self.promoted, "goal": self.goal,
                "incumbent": m(self.incumbent), "challenger": m(self.challenger),
                "incumbent_id": self.incumbent_id,
                "challenger_id": self.challenger_id, "reason": self.reason}


class EvolutionEngine:
    """Benchmarks alternative plans against the incumbent and promotes winners.

    Promotion is reversible by construction: the incumbent is never deleted,
    only superseded, and `rollback` restores it. A challenger must prove itself
    on the same cases the incumbent is measured on, so the comparison is like
    for like.
    """

    def __init__(self, planner, composer, store, provenance, runs: int = 5):
        self.planner = planner
        self.composer = composer
        self.store = store
        self.provenance = provenance
        self.runs = runs

    async def measure(self, plan: Dict[str, Any],
                      cases: List[Tuple[Dict[str, Any], Any]]) -> Measurement:
        timings: List[float] = []
        failures = 0
        correct = True
        for args, expected in cases:
            for _ in range(self.runs):
                started = time.perf_counter()
                outcome = await self.composer.execute(plan, dict(args))
                timings.append((time.perf_counter() - started) * 1000)
                if not outcome.get("success"):
                    failures += 1
                    correct = False
                elif expected is not None and outcome.get("value") != expected:
                    correct = False
        return Measurement(correct=correct,
                           latency_ms=statistics.median(timings) if timings else 0.0,
                           runs=len(timings), failures=failures)

    async def evolve(self, goal: str,
                     cases: List[Tuple[Dict[str, Any], Any]]) -> EvolutionResult:
        record = self.store.resolve_goal(goal)
        if record is None:
            return EvolutionResult(False, goal, reason="no incumbent capability to improve")

        incumbent = await self.measure(record.plan, cases)
        best: Optional[Tuple[Any, Measurement, Any]] = None

        for proposal in self.planner.propose(goal, limit=5):
            if proposal.ops_used == record.ops:
                continue
            analysis = self.composer.analyze(proposal.plan)
            if not analysis.ok:
                continue
            measured = await self.measure(proposal.plan, cases)
            if not measured.correct:
                continue
            if best is None or measured.latency_ms < best[1].latency_ms:
                best = (proposal, measured, analysis)

        if best is None:
            return EvolutionResult(False, goal, incumbent_id=record.capability_id,
                                   incumbent=incumbent,
                                   reason="no correct alternative was found")

        proposal, measured, analysis = best
        if not measured.dominates(incumbent):
            return EvolutionResult(
                False, goal, incumbent_id=record.capability_id,
                incumbent=incumbent, challenger=measured,
                reason=f"challenger did not clearly beat the incumbent "
                       f"({measured.latency_ms:.3f}ms vs {incumbent.latency_ms:.3f}ms)")

        # revise() requires the op list and effect set for the new plan;
        # the analysis was already computed above (the plan was checked ok).
        # effects are stored as plain strings (CapabilityRecord.effects is
        # List[str]); sorting raw Effect enums would raise TypeError for
        # multi-effect plans, so project to .value first (same projection
        # PlanAnalysis.as_dict() uses).
        revised = self.store.revise(record, proposal.plan,
                                    ops=proposal.ops_used,
                                    effects=sorted(e.value for e in analysis.effects),
                                    note=f"evolved: {measured.latency_ms:.3f}ms vs "
                                         f"{incumbent.latency_ms:.3f}ms")
        new_id = getattr(revised, "capability_id", None)
        if new_id and self.provenance is not None:
            from swarm_engine.governance.provenance import Origin, ProvenanceRecord, TrustLevel
            self.provenance.record(ProvenanceRecord(
                capability_id=new_id, origin=Origin.DERIVED,
                trust=TrustLevel.TESTED, source=f"evolution of {record.capability_id}",
                parents=[record.capability_id], primitives_used=list(proposal.ops_used)))

        return EvolutionResult(True, goal, incumbent_id=record.capability_id,
                               challenger_id=new_id, incumbent=incumbent,
                               challenger=measured,
                               reason=f"promoted: {measured.latency_ms:.3f}ms beats "
                                      f"{incumbent.latency_ms:.3f}ms")

    async def evolve_generations(self, goal: str,
                                 cases: List[Tuple[Dict[str, Any], Any]],
                                 generations: int = 3) -> List[EvolutionResult]:
        """Run evolution repeatedly until it stops finding wins.

        Stopping early on the first barren generation is deliberate: continuing
        to re-benchmark the same alternatives burns budget to rediscover that
        nothing improved, and the churn looks like progress in the logs.
        """
        history: List[EvolutionResult] = []
        for _ in range(max(1, generations)):
            result = await self.evolve(goal, cases)
            history.append(result)
            if not result.promoted:
                break
        return history

    async def detect_regression(self, goal: str,
                                cases: List[Tuple[Dict[str, Any], Any]],
                                tolerance: float = 2.0) -> Dict[str, Any]:
        """Check whether the current capability has got worse, and roll back if
        it has.

        A promotion is only as good as its next measurement. Without this, a
        challenger that won its benchmark by luck stays in place permanently
        and the engine has no path back to the version that actually worked.
        """
        record = self.store.resolve_goal(goal)
        if record is None:
            return {"checked": False, "reason": "no capability bound to this goal"}

        measured = await self.measure(record.plan, cases)
        provenance = self.provenance.get(record.capability_id) if self.provenance else None
        baseline = getattr(record, "baseline_latency_ms", None)

        regressed = (not measured.correct) or (
            baseline is not None and measured.latency_ms > baseline * tolerance)

        if not regressed:
            return {"checked": True, "regressed": False,
                    "latency_ms": round(measured.latency_ms, 3),
                    "correct": measured.correct}

        rolled_back = False
        if hasattr(self.store, "rollback"):
            try:
                rolled_back = bool(self.store.rollback(record.capability_id))
            except Exception:
                rolled_back = False
        if self.provenance is not None:
            from swarm_engine.governance.provenance import TrustLevel
            self.provenance.set_trust(
                record.capability_id, TrustLevel.QUARANTINED,
                "regressed against its own acceptance cases")
        return {"checked": True, "regressed": True, "rolled_back": rolled_back,
                "correct": measured.correct,
                "latency_ms": round(measured.latency_ms, 3),
                "reason": ("produced incorrect results" if not measured.correct
                           else "latency regressed beyond tolerance")}


# ---------------------------------------------------------------------------
# SELF-IMPROVEMENT OF THE ENGINE'S OWN MACHINERY
# ---------------------------------------------------------------------------

@dataclass
class ComponentDiagnosis:
    component: str
    symptom: str
    hypothesis: str
    evidence: Dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> Dict[str, Any]:
        return {"component": self.component, "symptom": self.symptom,
                "hypothesis": self.hypothesis, "evidence": self.evidence}


@dataclass
class SelfImprovementResult:
    deployed: bool
    component: str = ""
    diagnosis: Optional[Dict[str, Any]] = None
    before: Dict[str, Any] = field(default_factory=dict)
    after: Dict[str, Any] = field(default_factory=dict)
    regression_passed: Optional[bool] = None
    rolled_back: bool = False
    reason: str = ""
    # O13: which regression runner gated this result ("orc_…:vN"),
    # or "none" / "unbound" when no gate ran. Recorded, never trusted as
    # proof the runner tested anything.
    regression_gate: str = "unbound"

    def as_dict(self) -> Dict[str, Any]:
        return {"deployed": self.deployed, "component": self.component,
                "diagnosis": self.diagnosis, "before": self.before,
                "after": self.after, "regression_passed": self.regression_passed,
                "rolled_back": self.rolled_back, "reason": self.reason,
                "regression_gate": self.regression_gate}


class SelfImprovementEngine:
    """Improves the machinery, not just the capabilities it produces.

    The distinction matters: adding a capability is expansion, while changing
    how the planner or recovery layer behaves is improvement. The second has a
    failure mode the first does not — a change that helps the case it was
    designed for and quietly breaks everything else.

    So a candidate modification is never deployed on its own benchmark alone.
    It must (1) fix the diagnosed symptom, (2) pass the full existing
    regression suite unchanged, and (3) not regress the baseline measurements.
    The engine that proposed the change does not get to be the sole judge of
    it: the regression suite is external to the proposal, and a candidate that
    passes its own benchmark while failing regression is rejected.

    The previous configuration is always retained, so deployment is reversible.
    """

    def __init__(self, engine, regression_runner=None,
                 producer_id: Optional[str] = None,
                 token: Optional[str] = None,
                 oracle_registry=None, engine_oracle=None):
        self.engine = engine
        # `regression_runner() -> (passed: bool, detail: dict)`. Injected so
        # the check is genuinely independent of whatever is being changed.
        self.regression_runner = regression_runner
        self.history: List[SelfImprovementResult] = []
        # O13: the regression runner is the final deploy/revert gate. In
        # bound mode it must be a registered oracle (definition digest +
        # authenticated producer); every gate evaluation is recorded
        # chained (change id, passed, detail digest, runner id/version).
        # A runner that is merely assigned -- never registered -- is
        # refused at gate time (fail closed). Binding proves which runner
        # gated each deployment; it cannot prove the runner tested
        # anything (a stub has a stable digest too).
        self.oracle_registry = (
            oracle_registry if oracle_registry is not None
            else getattr(engine, "oracle_registry", None))
        self.engine_oracle = (
            engine_oracle if engine_oracle is not None
            else getattr(engine, "oracle", None))
        self._runner_binding = None
        if regression_runner is not None and self.oracle_registry is not None:
            self._runner_binding = self._register_runner(
                regression_runner, producer_id, token)

    def _register_runner(self, runner, producer_id, token):
        from swarm_engine.governance.oracle_binding import OracleBindingError
        if not producer_id or not token:
            raise OracleBindingError(
                "SelfImprovementEngine: bound mode requires an authenticated "
                "producer for regression_runner -- an unattributed runner is "
                "refused")
        if not self.oracle_registry.authenticate(producer_id, token):
            raise OracleBindingError(
                "SelfImprovementEngine: regression_runner producer "
                "authentication failed -- forged identity refused")
        rname = getattr(runner, "__name__", "regression_runner")
        oracle_id, version = self.oracle_registry.register_oracle(
            producer_id, token, f"regression_runner.{rname}", runner,
            input_contract="() -> (passed: bool, detail: dict)",
            output_contract="deploy/revert gate verdict",
            source="self-improvement engine")
        return {"oracle_id": oracle_id, "version": version,
                "producer_id": producer_id, "fn": runner}

    def _bound_runner(self):
        """Return (runner, binding) for this gate evaluation. In bound
        mode the live runner must be the registered one -- re-digested
        against its registration (O1 pattern). A runner present without a
        matching registration (direct attribute assignment, or a
        post-registration swap) is refused: fail closed, never silently
        gated. Unbound configuration returns the legacy runner."""
        runner = self.regression_runner
        if runner is None:
            return None, None
        if self.oracle_registry is None:
            return runner, None
        binding = self._runner_binding
        if binding is None or binding.get("fn") is not runner:
            from swarm_engine.governance.oracle_binding import (
                OracleBindingError)
            raise OracleBindingError(
                "refusing to gate deployment on an unregistered "
                "regression_runner: the runner was not installed via an "
                "authenticated registration (or was swapped afterwards)")
        from swarm_engine.governance.binding_helpers import (
            verify_live_callable)
        verify_live_callable(self.oracle_registry, binding["oracle_id"],
                             binding["version"], runner,
                             what="regression_runner")
        return runner, binding

    def _record_gate(self, result: SelfImprovementResult,
                     binding: Dict[str, Any], passed: bool,
                     detail: Any) -> None:
        """O13: record one gate evaluation chained: (change id -> passed,
        detail digest, runner id/version)."""
        from swarm_engine.governance.binding_helpers import canonical_digest
        change_id = canonical_digest({
            "component": result.component,
            "diagnosis": result.diagnosis,
            "before": {"solve_rate": result.before.get("solve_rate"),
                       "median_ms": result.before.get("median_ms")},
            "after": {"solve_rate": result.after.get("solve_rate"),
                      "median_ms": result.after.get("median_ms")}})
        self.engine_oracle.evaluate(
            binding["oracle_id"],
            {"change_id": change_id},
            {"passed": passed,
             "detail_digest": canonical_digest(detail)},
            input_ref="regression_gate:%s" % (result.component or "unknown"),
            version=binding["version"],
            supplier_id=binding["producer_id"])

    def observe(self, samples: List[Dict[str, Any]]) -> List[ComponentDiagnosis]:
        """Look for patterns in failures that point at a component."""
        diagnoses: List[ComponentDiagnosis] = []
        if not samples:
            return diagnoses

        failures = [s for s in samples if not s.get("success")]
        if not failures:
            return diagnoses

        # The diagnoser normally rides on the engine (production engines
        # always carry one); a same-class default keeps observe() honest
        # for engine-like collaborators that do not.
        diagnoser = getattr(self.engine, "diagnoser", None) or FailureDiagnoser()
        kinds: Dict[str, int] = {}
        for sample in failures:
            diagnosis = diagnoser.diagnose(sample.get("error"))
            kinds[diagnosis.kind.value] = kinds.get(diagnosis.kind.value, 0) + 1

        rate = len(failures) / len(samples)
        dominant, count = max(kinds.items(), key=lambda kv: kv[1])

        component = {
            "bad_argument": "planner", "type_error": "planner",
            "missing_primitive": "acquisition", "logic": "verification",
            "timeout": "scheduler", "resource": "budget",
        }.get(dominant, "planner")

        diagnoses.append(ComponentDiagnosis(
            component=component,
            symptom=f"{len(failures)}/{len(samples)} objectives failed "
                    f"({rate:.0%}), dominated by {dominant}",
            hypothesis=f"the {component} is the likeliest locus: {count} of "
                       f"{len(failures)} failures share this cause",
            evidence={"failure_rate": round(rate, 3), "kinds": kinds}))
        return diagnoses

    async def measure_multi_objective(
            self, goals: List[Tuple[str, Dict[str, Any], Any]],
            adversarial_goals: Optional[List[Tuple[str, Dict[str, Any]]]] = None
            ) -> Dict[str, Any]:
        """Measure across several independent objectives at once.

        A single "solve rate" metric is gameable: a change that shaves
        milliseconds off the common case while breaking a rare one looks like
        a pure win by that number alone. Correctness, generalization (a held-
        out goal set separate from what the change targeted), robustness
        (adversarial/malformed goals must fail *cleanly*, not crash), and
        resource use are measured independently so a change can be rejected
        for winning on one axis while losing on another.
        """
        base = await self.measure_baseline(goals)
        robustness_ok, robustness_crashes = True, 0
        if adversarial_goals:
            for goal, payload in adversarial_goals:
                try:
                    task_id = self.engine.submit_task(goal, payload=dict(payload))
                    await self.engine.run_task(task_id)
                except Exception:
                    robustness_crashes += 1
                    robustness_ok = False
        return {
            "correctness": base["solve_rate"],
            "performance_ms": base["median_ms"],
            "robustness": 1.0 - (robustness_crashes / max(1, len(adversarial_goals or [1]))),
            "robustness_ok": robustness_ok,
            "solved": base["solved"], "total": base["total"],
            "failures": base["failures"],
        }

    async def measure_baseline(self, goals: List[Tuple[str, Dict[str, Any], Any]],
                               repeats: int = 7) -> Dict[str, Any]:
        """Measure current behaviour across a spread of goals.

        Every goal is run once and discarded before timing begins. The engine
        synthesizes on first encounter and reuses afterwards, so a cold "before"
        against a warm "after" makes any change look like a threefold speedup --
        the measurement would reward doing nothing. Warming both sides means the
        comparison reflects the change rather than the cache.

        Each goal is then timed `repeats` times rather than once. A single
        measurement with only a couple of goals is not a distribution, it is
        two numbers a scheduler hiccup can swing either way -- this was a real
        bug, not a hypothetical one: a no-op change (apply/revert that alter
        nothing) was measured as a "genuine speedup" purely from timing jitter
        often enough to fail the regression suite on a fair fraction of runs.
        Taking the median across goals*repeats samples is what makes the
        comparison reflect the change being tested rather than a scheduler.
        """
        for goal, payload, _ in goals:
            try:
                warm = self.engine.submit_task(goal, payload=dict(payload))
                await self.engine.run_task(warm)
            except Exception:
                pass

        solved, latencies, failures = 0, [], []
        for goal, payload, expected in goals:
            goal_solved = True
            for _ in range(repeats):
                started = time.time()
                task_id = self.engine.submit_task(goal, payload=dict(payload))
                outcome = await self.engine.run_task(task_id)
                latencies.append((time.time() - started) * 1000)
                value = (outcome.get("result") or {}).get("value")
                if not (outcome.get("success") and (expected is None or value == expected)):
                    goal_solved = False
            if goal_solved:
                solved += 1
            else:
                failures.append(goal)
        return {"solved": solved, "total": len(goals),
                "solve_rate": solved / max(1, len(goals)),
                "median_ms": round(statistics.median(latencies), 3) if latencies else 0.0,
                "failures": failures}

    async def improve(self, diagnosis: ComponentDiagnosis,
                      apply_change,
                      revert_change,
                      goals: List[Tuple[str, Dict[str, Any], Any]]
                      ) -> SelfImprovementResult:
        """Apply a candidate change to the machinery, judge it, keep or revert.

        `apply_change` and `revert_change` are callables supplied by the
        caller, so this engine never writes to its own source directly -- that
        path goes through SelfModificationGuard, which is snapshot-backed and
        refuses protected files.
        """
        result = SelfImprovementResult(deployed=False,
                                       component=diagnosis.component,
                                       diagnosis=diagnosis.as_dict())
        result.before = await self.measure_baseline(goals)

        try:
            apply_change()
        except Exception as exc:
            result.reason = f"the change could not be applied: {exc}"
            return result

        try:
            result.after = await self.measure_baseline(goals)
        except Exception as exc:
            revert_change()
            result.rolled_back = True
            result.reason = f"measurement crashed after the change: {exc}"
            return result

        # Independent regression check, external to the proposal.
        # O13: the gate runner is re-verified against its registration
        # before it runs; an unregistered runner fails closed (revert).
        # Every gate evaluation is recorded chained (change id, passed,
        # detail digest, runner id/version).
        from swarm_engine.governance.oracle_binding import OracleBindingError
        try:
            runner, binding = self._bound_runner()
        except OracleBindingError as exc:
            revert_change()
            result.rolled_back = True
            result.regression_gate = "refused"
            result.reason = f"regression gate refused: {exc}"
            self.history.append(result)
            return result
        if runner is not None:
            try:
                passed, detail = runner()
            except Exception as exc:
                passed, detail = False, {"error": str(exc)}
            result.regression_passed = passed
            if binding is not None:
                result.regression_gate = (
                    "%s:v%d" % (binding["oracle_id"], binding["version"]))
                self._record_gate(result, binding, passed, detail)
            if not passed:
                revert_change()
                result.rolled_back = True
                result.reason = (f"the change passed its own measurement but "
                                 f"broke the regression suite: {detail}")
                self.history.append(result)
                return result
        elif self.oracle_registry is not None:
            # Bound mode with no runner at all: the legacy behavior (gate
            # skipped, deployment on measurement alone) is preserved, but
            # the absence is marked explicitly on the result rather than
            # left silent. Binding cannot conjure a gate that was never
            # supplied.
            result.regression_gate = "none"

        correctness_regressed = result.after["solve_rate"] < result.before["solve_rate"]
        if correctness_regressed:
            revert_change()
            result.rolled_back = True
            result.reason = (
                f"correctness regressed: {result.before['solve_rate']:.0%} -> "
                f"{result.after['solve_rate']:.0%}; reverted rather than kept "
                f"on the assumption that newer is better")
            self.history.append(result)
            return result

        if result.after["solve_rate"] > result.before["solve_rate"]:
            genuinely_better = True
        else:
            # Correctness is tied, so any deployment decision here rests on
            # speed alone. A single before/after median pair was not enough
            # evidence for that: at these operation sizes (single-digit
            # milliseconds), two independent measurement windows can differ
            # by >15% from scheduler jitter alone with nothing having
            # changed -- a real, reproduced failure mode, not a hypothetical
            # one. `_confirm_speed_win` re-applies and re-reverts the change
            # across several alternating rounds and requires the win to hold
            # in most of them, so a lucky single sample cannot pass this gate
            # by itself.
            genuinely_better = await self._confirm_speed_win(
                apply_change, revert_change, goals, result.before["median_ms"])

        if not genuinely_better:
            revert_change()
            result.rolled_back = True
            result.reason = (
                f"no measurable gain: "
                f"{result.before['solve_rate']:.0%} -> {result.after['solve_rate']:.0%}, "
                f"{result.before['median_ms']:.1f}ms -> {result.after['median_ms']:.1f}ms "
                f"(not consistent enough across repeated measurement to trust); "
                f"reverted rather than kept on the assumption that newer is better")
            self.history.append(result)
            return result

        result.deployed = True
        result.reason = (f"deployed: solve rate "
                         f"{result.before['solve_rate']:.0%} -> "
                         f"{result.after['solve_rate']:.0%}, median "
                         f"{result.before['median_ms']:.1f}ms -> "
                         f"{result.after['median_ms']:.1f}ms")
        self.history.append(result)
        return result

    async def _confirm_speed_win(self, apply_change, revert_change,
                                 goals: List[Tuple[str, Dict[str, Any], Any]],
                                 baseline_ms: float, rounds: int = 9,
                                 margin: float = 0.85,
                                 min_absolute_ms: float = 1.5,
                                 repeats_per_round: int = 7) -> bool:
        """Require a speed win to hold across several independent, order-
        alternated measurement rounds rather than a single before/after pair,
        and require the win to clear an absolute floor as well as a
        percentage margin.

        `repeats_per_round` matters more than it looks: this was originally
        3, lower than the 7 repeats `measure_baseline` uses for the initial
        check, on the assumption that confirmation only needed to be
        approximate. In practice a lower repeat count means each round's own
        median is noisier, so a run of pure jitter clears the margin+floor
        gate on individual rounds more often than intended, and enough of
        those add up to pass the 60% majority even for a genuine no-op —
        reproduced empirically at roughly a coin flip's failure rate before
        this fix. Matching the per-round precision to the initial
        measurement removes that gap rather than papering over it with a
        blunter threshold.

        The floor and round count were tightened again after further
        observed failures: a no-op change was still occasionally confirmed
        as a "win" with reported differences as small as 0.8-1.3ms, at or
        below the previous 1.0ms floor and well inside the noise band for
        operations that complete in a handful of milliseconds under a
        virtualized scheduler. Raising the floor to 1.5ms and the round
        count to 9 is the same kind of evidence-driven adjustment as the
        repeats_per_round fix above, not a guess -- it directly targets the
        size of difference actually observed to slip through.
        """
        wins = 0
        for round_index in range(rounds):
            measure_after_first = round_index % 2 == 0
            try:
                if measure_after_first:
                    apply_change()
                    after = await self.measure_baseline(goals, repeats=repeats_per_round)
                    revert_change()
                    before = await self.measure_baseline(goals, repeats=repeats_per_round)
                else:
                    before = await self.measure_baseline(goals, repeats=repeats_per_round)
                    apply_change()
                    after = await self.measure_baseline(goals, repeats=repeats_per_round)
                    revert_change()
            except Exception:
                continue
            cleared_margin = after["median_ms"] < before["median_ms"] * margin
            cleared_floor = (before["median_ms"] - after["median_ms"]) >= min_absolute_ms
            if cleared_margin and cleared_floor:
                wins += 1
        return wins > rounds * 0.6


# ---------------------------------------------------------------------------
# RECURSIVE / CHAINED SELF-IMPROVEMENT
# ---------------------------------------------------------------------------

@dataclass
class ImprovementCycle:
    cycle: int
    diagnosis: Optional[Dict[str, Any]]
    result: Optional[Dict[str, Any]]
    stopped_reason: str = ""

    def as_dict(self) -> Dict[str, Any]:
        return {"cycle": self.cycle, "diagnosis": self.diagnosis,
                "result": self.result, "stopped_reason": self.stopped_reason}


class RecursiveImprovementLoop:
    """Chains improvement cycles: observe -> diagnose -> improve -> re-observe
    against the *new* state -> improve again, until nothing more is diagnosable
    or a hard cycle limit is hit.

    The OBSERVE -> DIAGNOSE step is not re-implemented here. Each cycle draws
    raw failure samples from FailureMemory -- one sample per recorded failure
    of a diagnosable kind, shaped {"success": False, "error": detail,
    "goal": goal} -- and hands them to SelfImprovementEngine.observe; the
    cycle then uses the ComponentDiagnosis observe returns, verbatim. The
    kind->component mapping therefore lives in exactly one place (observe),
    not in a second private copy inside this loop, so the loop cannot drift
    out of agreement with standalone improvement passes about which
    component a failure kind implicates.

    The dangerous version of this loop is one that keeps "improving" forever
    because it always finds something to fix. Two things stop that here.
    First, each cycle's diagnosis is drawn from FailureMemory *since the last
    cycle*, not the whole history — a component that was fixed two cycles ago
    should not keep re-triggering on stale evidence. Second, an `apply_change`
    that is offered again and rejected again (same diagnosis, same outcome) is
    treated as a fixed point and the loop stops rather than retrying the
    identical unsuccessful change forever.

    A `known_good` snapshot from before the *first* cycle is retained
    independent of any single cycle's own before/after — so even after several
    successful improvements, the loop can always be walked all the way back to
    where it started, not just one step back.
    """

    # Failure kinds this loop will spend improvement budget on. A kind is
    # diagnosable here only if SelfImprovementEngine.observe maps it onto a
    # component the caller can offer changes for; anything else (permission
    # denials, strategy exhaustion, unrecognised errors) is left to the
    # recovery layer rather than being "improved" on.
    DIAGNOSABLE_KINDS = frozenset({
        "bad_argument", "type_error", "missing_primitive",
        "logic", "timeout", "resource"})

    def __init__(self, improver: SelfImprovementEngine, failure_memory,
                max_cycles: int = 5):
        self.improver = improver
        self.failure_memory = failure_memory
        self.max_cycles = max_cycles

    def _failure_samples(self, limit_per_kind: int = 10000
                         ) -> List[Dict[str, Any]]:
        """One raw sample per recorded failure of a diagnosable kind, shaped
        for SelfImprovementEngine.observe. Every record in FailureMemory is a
        failure, so `success` is always False; observe keys on that."""
        samples: List[Dict[str, Any]] = []
        for kind in sorted(self.DIAGNOSABLE_KINDS):
            for rec in self.failure_memory.by_kind(kind, limit=limit_per_kind):
                samples.append({"success": False, "error": rec.detail,
                                "goal": rec.goal})
        return samples

    async def run(self, component_changes: Dict[str, Tuple[Any, Any]],
                 goals: List[Tuple[str, Dict[str, Any], Any]]
                 ) -> List[ImprovementCycle]:
        """`component_changes` maps a component name to (apply_fn, revert_fn)
        pairs the caller is offering for that component -- this loop decides
        *whether* and *how many times* to use them, not how to build them,
        which keeps it agnostic to what "improving the planner" concretely
        means for any given engine.
        """
        cycles: List[ImprovementCycle] = []
        last_seen_failure_count = 0
        seen_diagnoses: set = set()

        for cycle_index in range(1, self.max_cycles + 1):
            # Raw failure evidence through the production OBSERVE path.
            # This is the only way this loop turns failures into a
            # diagnosis: it never classifies or maps kinds itself.
            samples = self._failure_samples()
            if len(samples) <= last_seen_failure_count:
                cycles.append(ImprovementCycle(
                    cycle_index, None, None,
                    "no new failure evidence since the last cycle; stopping "
                    "rather than re-diagnosing stale history"))
                break

            diagnoses = self.improver.observe(samples)
            if not diagnoses:
                cycles.append(ImprovementCycle(
                    cycle_index, None, None,
                    "observe produced no diagnosis from the failure samples; "
                    "stopping rather than improving on no diagnosis"))
                break
            diagnosis = diagnoses[0]

            # Minimum-evidence gate (previously dominant_kind(min_samples=3),
            # now applied to the observe-produced evidence): the dominant
            # kind among the observed samples must still have enough samples
            # to say anything about it.
            kinds = diagnosis.evidence.get("kinds", {}) or {}
            if not kinds or max(kinds.values()) < 3:
                cycles.append(ImprovementCycle(
                    cycle_index, None, None,
                    "no component has enough failure evidence to diagnose"))
                break

            # Fixed-point fingerprint, rebuilt from the observe-produced
            # diagnosis: the implicated component plus the observed
            # failure-kind profile.
            fingerprint = (diagnosis.component, tuple(sorted(kinds)))
            if fingerprint in seen_diagnoses:
                cycles.append(ImprovementCycle(
                    cycle_index, diagnosis.as_dict(), None,
                    "the same component is implicated with the same "
                    "failure-kind profile as a previous cycle; offering the "
                    "same change against this evidence again is a fixed "
                    "point, not progress"))
                break
            seen_diagnoses.add(fingerprint)

            change = component_changes.get(diagnosis.component)
            if change is None:
                cycles.append(ImprovementCycle(
                    cycle_index, diagnosis.as_dict(), None,
                    f"no candidate change was offered for component "
                    f"{diagnosis.component!r}; diagnosis recorded, nothing "
                    f"attempted"))
                break

            apply_fn, revert_fn = change
            result = await self.improver.improve(diagnosis, apply_fn, revert_fn, goals)
            cycles.append(ImprovementCycle(cycle_index, diagnosis.as_dict(),
                                           result.as_dict()))
            last_seen_failure_count = len(samples)

            if not result.deployed:
                # The offered change did not help this diagnosis. Trying the
                # same change again next cycle cannot produce a different
                # result, so this is also a fixed point.
                break

        return cycles
