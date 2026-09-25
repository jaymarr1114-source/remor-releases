"""
swarm_engine/improvement/observers.py

Observers: watch accumulated evidence for a specific subsystem and propose
Improvement candidates when a real, recurring pattern justifies one.

`ImprovementObserver` is the extension point item 9 asks for — "do not
hard-code a single self-improvement trick." A new subsystem gets a new
observer that knows what evidence to look at and how to express a candidate
change for that subsystem; the lifecycle, validation harness, admission,
persistence, and rollback machinery stay the same for every observer that
is ever added.

SearchPolicyObserver is the first one, and it is deliberately the strongest
mechanism the current architecture can honestly implement without a
reasoning model: the search policy (how many candidates GeneralSynthesizer
will try, how wide its per-slot pool is) is a small set of integers, and
whether the CURRENT integers are too small is a question the system can
answer from its own telemetry — specifically, ExhaustedSearchMemory's
replayable evidence — without any human specifying what the new numbers
should be.
"""
from __future__ import annotations

import statistics
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from swarm_engine.improvement.substrate import Improvement, ImprovementState


class ImprovementObserver(ABC):
    target_subsystem: str = ""

    @abstractmethod
    def observe(self) -> List[Improvement]:
        """Inspect available evidence; return zero or more candidates. An
        empty list is a real, common, correct answer — "the evidence does
        not yet justify a change" is not a failure of the observer."""


@dataclass
class SearchPolicyEvidence:
    exhausted_count: int
    max_candidates_seen: int
    median_candidates_seen: float
    incumbent_max_candidates: int
    sample_signatures: List[str]


class SearchPolicyObserver(ImprovementObserver):
    """Targets `cognition.search_policy` — GeneralSynthesizer's
    (max_size, pool_per_type, max_arg_combinations, max_candidates)
    configuration.

    The candidate's proposed_change is computed FROM the exhausted-search
    evidence, not asserted: the new max_candidates is derived from how many
    candidates the searches that actually failed had already spent when
    their budget ran out (with a real margin, not an arbitrary human
    number), and only for searches ExpressivenessAnalyzer already confirmed
    were type-reachable — recurring exhaustion on a genuinely inexpressible
    goal is not evidence the search policy is the problem, and is excluded
    on purpose.
    """
    target_subsystem = "cognition.search_policy"

    MIN_RECURRENCE = 2
    # A generous but bounded probe budget used ONLY at observation time to
    # MEASURE what a case actually needs — not a margin applied to the
    # exhaustion point. The first version of this observer multiplied the
    # point of failure by a fixed constant (1.5x, then 4x after evidence
    # showed 1.5x solved zero real cases) and both were still guesses dressed
    # as evidence: only the ANCHOR came from real data, the MULTIPLIER did
    # not. A 3-parameter case needed roughly 3x its exhaustion point; a
    # 4-parameter case needed roughly 22x — no single constant fits both,
    # and picking one that happens to fit whichever case is being tested at
    # the time is curve-fitting, not evidence-driven improvement. Measuring
    # the actual requirement directly removes the guess entirely.
    PROBE_BUDGET = 60000

    def _measure_actual_requirement(self, examples, param_names) -> Optional[int]:
        from swarm_engine.cognition.synthesis import GeneralSynthesizer
        current = self.engine.reasoning.synthesizer
        prober = GeneralSynthesizer(
            self.engine.reg, self.engine.bias,   # read: reuses real learned ordering
            max_size=current.max_size, pool_per_type=current.pool_per_type,
            max_arg_combinations=current.max_arg_combinations,
            max_candidates=self.PROBE_BUDGET)
        _, trace = prober.search(examples, param_names)   # search() never mutates bias
        return trace.candidates_tried if trace.candidates_tried < self.PROBE_BUDGET else None

    def __init__(self, cognitive_engine, min_recurrence: int = MIN_RECURRENCE):
        self.engine = cognitive_engine
        self.min_recurrence = min_recurrence

    def gather_evidence(self) -> Optional[SearchPolicyEvidence]:
        replayable = self.engine.exhausted.replayable(only_expressible=True)
        if len(replayable) < self.min_recurrence:
            return None
        counts = [r["candidates_tried"] for r in replayable]
        return SearchPolicyEvidence(
            exhausted_count=len(replayable),
            max_candidates_seen=max(counts),
            median_candidates_seen=statistics.median(counts),
            incumbent_max_candidates=self.engine.reasoning.synthesizer.max_candidates,
            sample_signatures=[r["signature"] for r in replayable[:10]])

    def observe(self) -> List[Improvement]:
        evidence = self.gather_evidence()
        if evidence is None:
            return []

        current = self.engine.reasoning.synthesizer
        replayable = self.engine.exhausted.replayable(only_expressible=True)

        measured = []
        unmeasurable = 0
        for record in replayable:
            requirement = self._measure_actual_requirement(
                record["examples"], record["param_names"])
            if requirement is None:
                unmeasurable += 1  # exceeds even the probe budget; not this cycle
                continue
            measured.append(requirement)

        if not measured:
            return []

        # A modest, stated margin over the WORST measured requirement — not
        # a multiplier applied to the failure point, a buffer applied to a
        # directly observed answer.
        # 1.2x was tested end-to-end and found insufficient: measuring the
        # SAME search twice in immediate succession produced different
        # candidate counts (13423 then 16501) despite no code path that
        # should mutate bias between the calls -- some source of run-to-run
        # variance in this search exists that hasn't been fully isolated.
        # Rather than let a margin tuned to one measurement silently fail on
        # the next, 1.6x gives real headroom over the measured point instead
        # of assuming the measurement is exactly reproducible.
        proposed_max_candidates = int(max(measured) * 1.6)
        if proposed_max_candidates <= current.max_candidates:
            return []

        proposed_pool_per_type = current.pool_per_type
        if evidence.median_candidates_seen > current.max_candidates * 0.8:
            proposed_pool_per_type = int(current.pool_per_type * 1.5)

        active = (self.engine.improvements.active_for(self.target_subsystem)
                 if hasattr(self.engine, "improvements") else None)
        predecessor_id = active.improvement_id if active else None
        version = (active.version + 1) if active else 1

        improvement_id = f"imp_{self.target_subsystem.replace('.', '_')}_v{version}"
        return [Improvement(
            improvement_id=improvement_id,
            target_subsystem=self.target_subsystem,
            motivation=(
                f"{evidence.exhausted_count} distinct, type-expressible searches "
                f"exhausted the current max_candidates budget "
                f"({current.max_candidates}) without finding an answer; directly "
                f"measuring each against a {self.PROBE_BUDGET}-candidate probe "
                f"found the worst actually needed {max(measured)} candidates"
                + (f" ({unmeasurable} case(s) exceeded even the probe budget and "
                   f"were excluded from this proposal)" if unmeasurable else "")),
            evidence={
                "exhausted_count": evidence.exhausted_count,
                "measured_requirements": measured,
                "unmeasurable_count": unmeasurable,
                "incumbent_max_candidates": evidence.incumbent_max_candidates,
                "sample_signatures": evidence.sample_signatures},
            proposed_change={"max_candidates": proposed_max_candidates,
                            "pool_per_type": proposed_pool_per_type,
                            "max_size": current.max_size,
                            "max_arg_combinations": current.max_arg_combinations},
            dependencies=[predecessor_id] if predecessor_id else [],
            expected_benefit=(
                f"solve the measured previously-exhausted case(s) without "
                f"reducing the solve rate on problems already solved under "
                f"the current policy"),
            validation_requirements={
                "no_regression_on_case_memory": True,
                "must_solve_at_least_one_exhausted_case": True,
                "max_time_overhead_factor": 5.0},
            provenance={"generated_by": "SearchPolicyObserver",
                       "generated_from": "ExhaustedSearchMemory.replayable() "
                                         "+ direct probe measurement"},
            predecessor_id=predecessor_id,
            version=version,
            rollback_info={"restore_max_candidates": current.max_candidates,
                          "restore_pool_per_type": current.pool_per_type,
                          "restore_max_size": current.max_size,
                          "restore_max_arg_combinations": current.max_arg_combinations},
        )]


class AcquisitionStrategyObserver(ImprovementObserver):
    """Targets `acquisition.strategy_policy` — which strategies the
    orchestrator tries for a requirement signature.

    Boundary 2's learner already reorders strategies by observed success
    rate, but reordering is not pruning: a strategy with a 0% success rate
    for a signature is still TRIED on every new requirement of that
    signature whenever it is reached in the (reordered) candidate list,
    burning its full attempt cost each time. This observer watches the
    learner's own accumulated (signature, strategy, success, cost) rows
    for exactly that pattern — recurring, expensive, never-successful
    attempts — and proposes signature-scoped pruning of the single
    worst-offending (signature, strategy) pair.

    The deficiency is SELECTED, not asserted: among all pairs with enough
    attempts to mean something and zero successes, it picks the one with
    the largest total wasted cost. Different evidence selects a different
    pair (or nothing). No strategy name and no goal text appears anywhere
    in the rule — everything comes from the stored rows.
    """
    target_subsystem = "acquisition.strategy_policy"

    # A 0% rate over fewer attempts is noise, not evidence.
    MIN_ATTEMPTS = 3
    # Do not bother pruning a strategy whose failures are already free:
    # the waste has to be material before a policy change is justified.
    MIN_WASTED_NS = 1_000_000_000

    def __init__(self, cognitive_engine):
        # Named `cognitive_engine` to match the ImprovementObserver
        # convention; this observer actually takes the SwarmEngine, which
        # owns the strategy learner and the improvements store.
        self.engine = cognitive_engine

    def _learner_rows(self):
        learner = getattr(self.engine, "strategy_learner", None)
        if learner is None:
            return []
        import sqlite3
        try:
            conn = sqlite3.connect(learner.db_path)
            try:
                return conn.execute(
                    "SELECT signature, strategy, success, cost "
                    "FROM acquisition_experience").fetchall()
            finally:
                conn.close()
        except Exception:
            return []

    def gather_evidence(self) -> List[Dict[str, Any]]:
        """Aggregate learner rows per (signature, strategy), worst waste
        first. Pure function of the stored evidence."""
        groups: Dict[tuple, Dict[str, Any]] = {}
        for signature, strategy, success, cost in self._learner_rows():
            key = (signature, strategy)
            g = groups.setdefault(
                key, {"signature": signature, "strategy": strategy,
                      "attempts": 0, "successes": 0, "total_cost_ns": 0})
            g["attempts"] += 1
            g["successes"] += int(success)
            g["total_cost_ns"] += int(cost or 0)
        qualifying = [
            g for g in groups.values()
            if g["attempts"] >= self.MIN_ATTEMPTS
            and g["successes"] == 0
            and g["total_cost_ns"] >= self.MIN_WASTED_NS
        ]
        qualifying.sort(key=lambda g: g["total_cost_ns"], reverse=True)
        return qualifying

    def _already_covered(self, signature: str, strategy: str) -> bool:
        """True if a pending/active improvement already prunes this pair,
        or the live policy already does — proposing again would be
        improvement theater."""
        pruning = getattr(self.engine, "acquisition_pruning", None) or {}
        if strategy in (pruning.get(signature) or set()):
            return True
        store = getattr(self.engine, "improvements", None)
        if store is None:
            return False
        live = {ImprovementState.CANDIDATE, ImprovementState.VALIDATED,
                ImprovementState.ADMITTED, ImprovementState.ACTIVE}
        for imp in store.all(self.target_subsystem):
            if imp.state not in live:
                continue
            prune = (imp.proposed_change or {}).get("prune") or {}
            if (prune.get("signature") == signature
                    and strategy in (prune.get("strategies") or [])):
                return True
        return False

    def observe(self) -> List[Improvement]:
        candidates = self.gather_evidence()
        for cand in candidates:
            if not self._already_covered(cand["signature"], cand["strategy"]):
                selected = cand
                break
        else:
            return []

        signature = selected["signature"]
        strategy = selected["strategy"]
        attempts = selected["attempts"]
        wasted_ns = selected["total_cost_ns"]
        mean_ns = wasted_ns // max(1, attempts)

        store = getattr(self.engine, "improvements", None)
        existing = store.all(self.target_subsystem) if store else []
        version = max([i.version for i in existing] + [0]) + 1
        active = (store.active_for(self.target_subsystem)
                  if store is not None else None)
        predecessor_id = active.improvement_id if active else None

        pruning_before = getattr(self.engine, "acquisition_pruning", None) or {}
        improvement_id = f"imp_{self.target_subsystem.replace('.', '_')}_v{version}"
        return [Improvement(
            improvement_id=improvement_id,
            target_subsystem=self.target_subsystem,
            motivation=(
                f"strategy {strategy!r} failed all {attempts} recorded "
                f"attempts for requirement signature {signature} "
                f"(mean cost {mean_ns / 1e9:.2f}s per attempt, "
                f"{wasted_ns / 1e9:.1f}s total wasted); the learner's "
                f"reordering already ranks it last but it is still tried on "
                f"every new requirement of this signature, so pruning it "
                f"for this signature removes the waste without changing "
                f"what can succeed"),
            evidence={
                "signature": signature, "strategy": strategy,
                "attempts": attempts, "successes": 0,
                "mean_cost_ns": mean_ns, "total_wasted_ns": wasted_ns,
                "min_attempts": self.MIN_ATTEMPTS,
                "min_wasted_ns": self.MIN_WASTED_NS,
                "qualifying_pairs": len(candidates)},
            proposed_change={"prune": {
                "signature": signature, "strategies": [strategy],
                "min_attempts": self.MIN_ATTEMPTS,
                "observed": {"attempts": attempts, "successes": 0,
                             "mean_cost_ns": mean_ns}}},
            dependencies=[predecessor_id] if predecessor_id else [],
            expected_benefit=(
                "stop paying the attempt cost of a strategy the evidence "
                "says never succeeds for this signature, with no change in "
                "which requirements resolve"),
            validation_requirements={
                "no_success_regression": True,
                "max_cost_ratio": 1.0,
                "min_trials_per_arm": 3},
            provenance={"generated_by": "AcquisitionStrategyObserver",
                        "generated_from": "acquisition_experience "
                                         "(signature, strategy, success, cost) rows"},
            predecessor_id=predecessor_id,
            version=version,
            rollback_info={"pruning_before": {
                s: sorted(v) for s, v in pruning_before.items()}},
        )]


# ---------------------------------------------------------------------------
# Multi-subsystem observers (verification + representation)
# ---------------------------------------------------------------------------

class VerificationCostObserver(ImprovementObserver):
    """Observes IndependentValidator / verification rejections recorded in
    acquisition experience and failure_memory-like patterns.

    Proposes a soft ordering change: prefer cheaper verification-first
    screening when generate repeatedly fails independent validation for a
    signature (evidence-only; no goal-text rules).
    """
    target_subsystem = "verification.strategy_policy"
    MIN_ATTEMPTS = 3
    MIN_WASTED_NS = 500_000_000  # 0.5s

    def __init__(self, engine):
        self.engine = engine

    def _rows(self):
        learner = getattr(self.engine, "strategy_learner", None)
        if learner is None:
            return []
        import sqlite3
        try:
            conn = sqlite3.connect(learner.db_path)
            try:
                return conn.execute(
                    "SELECT signature, strategy, success, cost "
                    "FROM acquisition_experience WHERE strategy = 'generate'"
                ).fetchall()
            finally:
                conn.close()
        except Exception:
            return []

    def gather_evidence(self):
        from collections import defaultdict
        g = defaultdict(lambda: {"attempts": 0, "successes": 0, "total_cost_ns": 0})
        for signature, strategy, success, cost in self._rows():
            e = g[signature]
            e["signature"] = signature
            e["attempts"] += 1
            e["successes"] += int(success)
            e["total_cost_ns"] += int(cost or 0)
        out = [v for v in g.values()
               if v["attempts"] >= self.MIN_ATTEMPTS
               and v["successes"] == 0
               and v["total_cost_ns"] >= self.MIN_WASTED_NS]
        out.sort(key=lambda x: -x["total_cost_ns"])
        return out

    def observe(self):
        from swarm_engine.improvement.substrate import Improvement, ImprovementState
        evidence_list = self.gather_evidence()
        if not evidence_list:
            return []
        top = evidence_list[0]
        # Slightly lower waste than acquisition prune so they can compete
        store = getattr(self.engine, "improvements", None)
        existing = store.all(self.target_subsystem) if store else []
        version = max([i.version for i in existing] + [0]) + 1
        return [Improvement(
            improvement_id=f"imp_verification_strategy_policy_v{version}",
            target_subsystem=self.target_subsystem,
            motivation=(
                f"generate/independent-validation path failed all "
                f"{top['attempts']} attempts for signature {top['signature']} "
                f"(wasted {top['total_cost_ns']/1e9:.1f}s); prefer early "
                f"behavioural screening before full independent validation"
            ),
            evidence={
                "signature": top["signature"],
                "attempts": top["attempts"],
                "successes": top["successes"],
                "total_wasted_ns": top["total_cost_ns"],
                "mean_cost_ns": top["total_cost_ns"] // max(1, top["attempts"]),
                "strategy": "generate",
            },
            proposed_change={
                "prefer_early_behavioural_screen": {
                    "signature": top["signature"],
                    "enabled": True,
                }
            },
            expected_benefit=(
                "reduce wasted full-validation cost on signatures where "
                "generate never admits"
            ),
            validation_requirements={"needs_verification_harness": True},
            provenance={
                "generated_by": "VerificationCostObserver",
                "generated_from": "acquisition_experience generate rows",
            },
            version=version,
        )]


class RepresentationSearchObserver(ImprovementObserver):
    """Observes compose-strategy failures and representation mismatches.

    When compose repeatedly fails behavioural checks or finds no template,
    proposes increasing structural-assembly preference for composite outputs.
    """
    target_subsystem = "representation.search_policy"
    MIN_ATTEMPTS = 3
    MIN_WASTED_NS = 100_000  # compose is cheap; still needs recurrence

    def __init__(self, engine):
        self.engine = engine

    def _rows(self):
        learner = getattr(self.engine, "strategy_learner", None)
        if learner is None:
            return []
        import sqlite3
        try:
            conn = sqlite3.connect(learner.db_path)
            try:
                return conn.execute(
                    "SELECT signature, strategy, success, cost "
                    "FROM acquisition_experience WHERE strategy = 'compose'"
                ).fetchall()
            finally:
                conn.close()
        except Exception:
            return []

    def gather_evidence(self):
        from collections import defaultdict
        g = defaultdict(lambda: {"attempts": 0, "successes": 0, "total_cost_ns": 0})
        for signature, strategy, success, cost in self._rows():
            e = g[signature]
            e["signature"] = signature
            e["attempts"] += 1
            e["successes"] += int(success)
            e["total_cost_ns"] += int(cost or 0)
        out = [v for v in g.values()
               if v["attempts"] >= self.MIN_ATTEMPTS
               and v["successes"] == 0
               and v["total_cost_ns"] >= self.MIN_WASTED_NS]
        out.sort(key=lambda x: -x["total_cost_ns"])
        return out

    def observe(self):
        from swarm_engine.improvement.substrate import Improvement
        evidence_list = self.gather_evidence()
        if not evidence_list:
            return []
        top = evidence_list[0]
        store = getattr(self.engine, "improvements", None)
        existing = store.all(self.target_subsystem) if store else []
        version = max([i.version for i in existing] + [0]) + 1
        return [Improvement(
            improvement_id=f"imp_representation_search_policy_v{version}",
            target_subsystem=self.target_subsystem,
            motivation=(
                f"compose failed all {top['attempts']} attempts for "
                f"signature {top['signature']}; prefer structural pair-assembly "
                f"when output_decomposition is present before template compose"
            ),
            evidence={
                "signature": top["signature"],
                "attempts": top["attempts"],
                "successes": top["successes"],
                "total_wasted_ns": top["total_cost_ns"],
                "mean_cost_ns": top["total_cost_ns"] // max(1, top["attempts"]),
                "strategy": "compose",
            },
            proposed_change={
                "prefer_structural_assembly": {
                    "signature": top["signature"],
                    "enabled": True,
                }
            },
            expected_benefit=(
                "avoid template-compose dead ends when structural decomposition "
                "already identifies the assembly plan"
            ),
            validation_requirements={"needs_representation_harness": True},
            provenance={
                "generated_by": "RepresentationSearchObserver",
                "generated_from": "acquisition_experience compose rows",
            },
            version=version,
        )]
