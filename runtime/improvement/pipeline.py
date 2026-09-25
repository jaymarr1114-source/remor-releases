"""
swarm_engine/improvement/pipeline.py

The improvement lifecycle, end to end: observe -> generate candidate ->
construct -> isolate -> validate -> compare against incumbent -> admit ->
activate -> persist -> learn from subsequent performance.

"Isolate" for a search-policy candidate means something specific and
honestly scoped: this is not executing untrusted code needing a subprocess
sandbox (that machinery exists elsewhere, for acquired/synthesized CODE). A
search-policy candidate is a different set of integers fed into the SAME
already-verified GeneralSynthesizer algorithm. Isolation here means the
candidate configuration runs read-only against benchmark problems, touching
a throwaway SearchBias, and never touches the live CognitiveEngine's stores
until admitted — so a bad candidate cannot pollute production learning state
while it is still being judged.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from swarm_engine.improvement.substrate import (
    Improvement, ImprovementState, ImprovementStore,
)


@dataclass
class ValidationVerdict:
    passed: bool
    reasons: List[str] = field(default_factory=list)
    regression_solved: int = 0
    regression_total: int = 0
    improvement_solved: int = 0
    improvement_total: int = 0
    incumbent_time_s: float = 0.0
    candidate_time_s: float = 0.0

    def as_dict(self) -> Dict[str, Any]:
        return {"passed": self.passed, "reasons": self.reasons,
                "regression": f"{self.regression_solved}/{self.regression_total}",
                "improvement": f"{self.improvement_solved}/{self.improvement_total}",
                "incumbent_time_s": round(self.incumbent_time_s, 3),
                "candidate_time_s": round(self.candidate_time_s, 3)}


class SearchPolicyValidator:
    """Independent, head-to-head comparison of a candidate search-policy
    configuration against the incumbent, on two real evidence sets:
    CaseMemory (must not regress) and the replayable exhausted searches
    (should improve). Nothing here trusts the candidate's own "it worked" —
    every claim is re-measured against real problems."""

    def __init__(self, cognitive_engine, regression_sample: int = 15):
        self.engine = cognitive_engine
        self.regression_sample = regression_sample

    def validate(self, improvement: Improvement) -> ValidationVerdict:
        import os
        import tempfile
        from swarm_engine.cognition.synthesis import GeneralSynthesizer
        from swarm_engine.cognition.representations import SearchBias

        verdict = ValidationVerdict(passed=False)
        change = improvement.proposed_change

        # A real throwaway file, not ":memory:" — this codebase's stores open
        # a fresh connection per call (the same pattern used everywhere
        # else), and ":memory:" creates a brand new, separate, empty
        # database on every single connection rather than one shared
        # in-memory database, so the tables built in __init__ would already
        # be gone by the first real query. A real file behaves the way every
        # other store in this codebase already assumes.
        fd, isolated_path = tempfile.mkstemp(suffix=".db", prefix="swarm_isolate_")
        os.close(fd)
        try:
            isolated_bias = SearchBias(db_path=isolated_path)
            # Seed from the incumbent's accumulated knowledge — see
            # SearchBias.snapshot_into for why this is required, not
            # optional, for the comparison to test the policy rather than
            # the presence or absence of learning.
            self.engine.bias.snapshot_into(isolated_bias)
            candidate = GeneralSynthesizer(
                self.engine.reg, isolated_bias,
                max_size=change.get("max_size", 3),
                pool_per_type=change.get("pool_per_type", 24),
                max_arg_combinations=change.get("max_arg_combinations", 300),
                max_candidates=change.get("max_candidates", 20000))
            return self._compare(improvement, candidate, verdict)
        finally:
            os.remove(isolated_path)

    def _compare(self, improvement: Improvement, candidate,
                verdict: ValidationVerdict) -> ValidationVerdict:
        incumbent = self.engine.reasoning.synthesizer

        regression_cases = [(cid, entry) for cid, entry in self.engine.cases.all()
                            if entry.examples and entry.param_names][-self.regression_sample:]
        verdict.regression_total = len(regression_cases)
        if not regression_cases:
            verdict.reasons.append("no regression cases available with stored "
                                   "examples; cannot validate without one")
            return verdict

        started = time.time()
        incumbent_solved_ids = set()
        for cid, entry in regression_cases:
            hyp, _ = incumbent.search(entry.examples, entry.param_names)
            if hyp is not None:
                incumbent_solved_ids.add(cid)
        verdict.incumbent_time_s = time.time() - started

        started = time.time()
        candidate_solved_ids = set()
        for cid, entry in regression_cases:
            hyp, _ = candidate.search(entry.examples, entry.param_names)
            if hyp is not None:
                candidate_solved_ids.add(cid)
        verdict.candidate_time_s = time.time() - started
        verdict.regression_solved = len(candidate_solved_ids)

        regressed = incumbent_solved_ids - candidate_solved_ids
        if regressed:
            verdict.reasons.append(
                f"candidate fails {len(regressed)} case(s) the incumbent "
                f"currently solves — a real regression, refused regardless "
                f"of any improvement elsewhere")
            return verdict

        replayable = self.engine.exhausted.replayable(only_expressible=True)
        verdict.improvement_total = len(replayable)
        for record in replayable:
            hyp, _ = candidate.search(record["examples"], record["param_names"])
            if hyp is not None:
                verdict.improvement_solved += 1

        requirements = improvement.validation_requirements
        if requirements.get("must_solve_at_least_one_exhausted_case") and \
                verdict.improvement_solved == 0:
            verdict.reasons.append(
                "candidate solved none of the previously-exhausted cases; "
                "no evidence it is actually better, only that it is "
                "differently configured")
            return verdict

        max_overhead = requirements.get("max_time_overhead_factor")
        if max_overhead and verdict.incumbent_time_s > 0:
            ratio = verdict.candidate_time_s / max(verdict.incumbent_time_s, 1e-6)
            if ratio > max_overhead:
                verdict.reasons.append(
                    f"candidate is {ratio:.1f}x slower than the incumbent on "
                    f"the regression set, exceeding the "
                    f"{max_overhead}x allowance")
                return verdict

        verdict.passed = True
        verdict.reasons.append(
            f"no regression on {len(regression_cases)} known-solved case(s); "
            f"solved {verdict.improvement_solved}/{verdict.improvement_total} "
            f"previously-exhausted case(s)")
        return verdict


def apply_pruning_to_engine(engine, pruning) -> None:
    """Apply an acquisition-policy pruning to an engine: the single place
    where the {signature: {strategy names}} policy becomes live. Used by
    ImprovementPipeline._activate, by reactivate_persisted (via _activate),
    and by AcquisitionPolicyValidator to set up trial arms — so the
    validated change and the activated change are literally the same
    operation."""
    engine.acquisition_pruning = {
        s: set(v) for s, v in (pruning or {}).items()}


@dataclass
class AcquisitionValidationVerdict:
    """Head-to-head A/B of a proposed acquisition-policy pruning against
    the incumbent policy, on held-out goals of the pruned signature class.

    The comparison is on the ORIGINAL objective: did the requirement
    resolve (success), and what did the attempts really cost (the
    learner's own recorded per-attempt costs). Pruning can only remove
    candidates from the trial list, so a success regression means the
    pruned strategy was load-bearing and the change is refused."""
    passed: bool
    reasons: List[str] = field(default_factory=list)
    trials_per_arm: int = 0
    baseline_successes: int = 0
    candidate_successes: int = 0
    baseline_cost_ns: int = 0
    candidate_cost_ns: int = 0
    baseline_pruned_attempted: bool = False
    # O14: content-addressed id of the sampled goal batch this verdict
    # rested on (None when unbound or unsampled).
    goal_batch_id: Optional[str] = None

    def as_dict(self) -> Dict[str, Any]:
        t = max(1, self.trials_per_arm)
        return {
            "passed": self.passed, "reasons": self.reasons,
            "trials_per_arm": self.trials_per_arm,
            "baseline_successes": self.baseline_successes,
            "candidate_successes": self.candidate_successes,
            "baseline_success_rate": round(self.baseline_successes / t, 3),
            "candidate_success_rate": round(self.candidate_successes / t, 3),
            "baseline_cost_ns": self.baseline_cost_ns,
            "candidate_cost_ns": self.candidate_cost_ns,
            "baseline_cost_s": round(self.baseline_cost_ns / 1e9, 2),
            "candidate_cost_s": round(self.candidate_cost_ns / 1e9, 2),
            "baseline_pruned_attempted": self.baseline_pruned_attempted,
            "goal_batch_id": self.goal_batch_id,
        }


class AcquisitionPolicyValidator:
    """Validates a proposed acquisition.strategy_policy pruning by A/B.

    Each trial runs the REAL AcquisitionOrchestrator._resolve_node path on
    a CLONED database (the seeded world: learner experience, failure
    memory, registry — everything the production decision sees), so trials
    cannot pollute production learning state or each other via the
    failure-memory skip rule. The ONLY difference between arms is the
    pruning under test. Held-out goals come from an injected
    `goal_sampler(signature) -> [(goal, examples), ...]` — the world
    supplies fresh goals of the signature class the observer selected;
    the validator never invents them.

    Fail-closed: any proposed_change key outside the pruning schema, a
    missing sampler, or too few held-out goals rejects the improvement
    without running anything."""

    def __init__(self, engine, goal_sampler=None, trials_per_arm: int = 3,
                 producer_id: Optional[str] = None,
                 token: Optional[str] = None,
                 oracle_registry=None, engine_oracle=None,
                 _engine_supplied: bool = False):
        self.engine = engine
        self.goal_sampler = goal_sampler
        self.trials_per_arm = trials_per_arm
        # O14: the goal sampler is a supply oracle -- the world (or the
        # engine's synthetic stand-in) supplies the held-out goals that
        # validation A/B trials run on. In bound mode the sampler must be a
        # registered oracle; every sampled batch is recorded chained and
        # the verdict cites the batch id.
        self.oracle_registry = (
            oracle_registry if oracle_registry is not None
            else getattr(engine, "oracle_registry", None))
        self.engine_oracle = (
            engine_oracle if engine_oracle is not None
            else getattr(engine, "oracle", None))
        self._sampler_binding = None
        if goal_sampler is not None and self.oracle_registry is not None:
            from swarm_engine.governance.binding_helpers import (
                authenticated_registrar)
            from swarm_engine.governance.oracle_binding import (
                OracleBindingError)
            sname = getattr(goal_sampler, "__name__", "goal_sampler")
            if _engine_supplied:
                # Engine-internal synthetic sampler: pinned under the engine
                # identity (trusted boot path). The record then honestly
                # shows validation ran on engine-synthesized goals, not on
                # world-supplied ones.
                register, engine_producer, _note = authenticated_registrar(
                    self.oracle_registry, self.engine_oracle)
                oracle_id, version = register(
                    f"goal_sampler.{sname}", goal_sampler,
                    input_contract="signature -> [(goal, examples), ...]",
                    output_contract="held-out goal batch",
                    source="acquisition policy validator")
                producer = engine_producer
            else:
                if not producer_id or not token:
                    raise OracleBindingError(
                        "AcquisitionPolicyValidator: bound mode requires an "
                        "authenticated producer for goal_sampler -- an "
                        "unattributed sampler is refused")
                if not self.oracle_registry.authenticate(producer_id, token):
                    raise OracleBindingError(
                        "AcquisitionPolicyValidator: goal_sampler producer "
                        "authentication failed -- forged identity refused")
                oracle_id, version = self.oracle_registry.register_oracle(
                    producer_id, token, f"goal_sampler.{sname}",
                    goal_sampler,
                    input_contract="signature -> [(goal, examples), ...]",
                    output_contract="held-out goal batch",
                    source="acquisition policy validator")
                producer = producer_id
            self._sampler_binding = {
                "oracle_id": oracle_id, "version": version,
                "producer_id": producer}

    def _sample_goals(self, signature: str):
        """Invoke the goal sampler with O14 binding: re-digest against the
        registration, record the sampled batch chained, and return
        (goals, batch_id). Unbound configuration returns (goals, None)
        with legacy behavior."""
        sampler = self.goal_sampler
        binding = self._sampler_binding
        if sampler is None:
            return [], None
        if self.oracle_registry is not None and binding is not None:
            from swarm_engine.governance.binding_helpers import (
                BoundCallable, canonical_digest, verify_live_callable)
            verify_live_callable(self.oracle_registry, binding["oracle_id"],
                                 binding["version"], sampler,
                                 what="goal_sampler")
            if self.engine_oracle is not None:
                sampler = BoundCallable(
                    self.oracle_registry, self.engine_oracle, sampler,
                    binding["oracle_id"], binding["version"],
                    binding["producer_id"], what="goal_sampler")
        goals = list(sampler(signature) or [])
        batch_id = None
        if binding is not None and self.engine_oracle is not None:
            from swarm_engine.governance.binding_helpers import (
                canonical_digest)
            batch = [{"goal": g, "examples_digest": canonical_digest(ex)}
                     for g, ex in goals]
            batch_id = "sampled_" + canonical_digest(
                {"signature": signature, "batch": batch})
            self.engine_oracle.evaluate(
                binding["oracle_id"],
                {"signature": signature},
                {"batch_id": batch_id, "goals": batch, "n": len(goals)},
                input_ref=f"goal_sampler:{signature}",
                version=binding["version"],
                supplier_id=binding["producer_id"])
        return goals, batch_id

    # -- fail-closed change schema --------------------------------------
    @staticmethod
    def _check_change(change: Any) -> tuple:
        if not isinstance(change, dict) or set(change.keys()) != {"prune"}:
            got = sorted(change.keys()) if isinstance(change, dict) else type(change).__name__
            return (False, f"proposed_change must contain exactly 'prune'; got {got}")
        prune = change["prune"]
        if not isinstance(prune, dict):
            return (False, "prune must be a mapping")
        unknown = set(prune.keys()) - {"signature", "strategies",
                                       "min_attempts", "observed"}
        if unknown:
            return (False,
                    f"unrecognized change keys {sorted(unknown)}: this "
                    f"validator only executes signature-scoped strategy "
                    f"pruning and refuses anything else outright, so a "
                    f"proposal cannot smuggle in a weaker admission or "
                    f"verification path")
        sig = prune.get("signature")
        strategies = prune.get("strategies")
        if not isinstance(sig, str) or not sig:
            return (False, "prune.signature must be a non-empty string")
        if (not isinstance(strategies, list) or not strategies
                or not all(isinstance(s, str) and s for s in strategies)):
            return (False, "prune.strategies must be a non-empty list of "
                           "strategy-name strings")
        return (True, "")

    # -- trial harness ---------------------------------------------------
    def _base_db(self) -> Optional[str]:
        import os
        learner = getattr(self.engine, "strategy_learner", None)
        path = (getattr(learner, "db_path", None)
                or getattr(self.engine, "db_path", None))
        if not path or not os.path.exists(path):
            return None
        return path

    @staticmethod
    def _experience_waterline(db_path: str) -> int:
        import sqlite3
        conn = sqlite3.connect(db_path)
        try:
            row = conn.execute(
                "SELECT MAX(id) FROM acquisition_experience").fetchone()
            return int(row[0] or 0)
        finally:
            conn.close()

    @staticmethod
    def _experience_cost_since(db_path: str, waterline: int) -> int:
        import sqlite3
        conn = sqlite3.connect(db_path)
        try:
            row = conn.execute(
                "SELECT COALESCE(SUM(cost),0) FROM acquisition_experience "
                "WHERE id > ?", (waterline,)).fetchone()
            return int(row[0] or 0)
        finally:
            conn.close()

    def _run_trial(self, base_db: str, goal: str, examples,
                   pruning: Dict[str, Any]) -> Dict[str, Any]:
        """One real orchestrator run on a throwaway clone of the world."""
        import asyncio
        import os
        import shutil
        import tempfile
        import time
        # Deferred: core.engine already imports this module at boot.
        from swarm_engine.core.engine import SwarmEngine
        from swarm_engine.acquisition.orchestrator import AcquisitionOrchestrator

        fd, clone = tempfile.mkstemp(suffix=".db", prefix="swarm_ab_")
        os.close(fd)
        try:
            shutil.copyfile(base_db, clone)
            eng = SwarmEngine(db_path=clone)
            # The trial arm's ONLY difference from production: this pruning.
            apply_pruning_to_engine(eng, pruning)
            orch = AcquisitionOrchestrator(eng)
            names = list(orch.reasoner.analyze(goal).nodes.keys())
            if len(names) != 1:
                return {"ok": False,
                        "error": f"expected a single-node goal, got {names}"}
            waterline = self._experience_waterline(clone)
            t0 = time.perf_counter_ns()
            res = asyncio.run(orch.resolve(
                goal, examples_by_node={names[0]: list(examples)}))
            wall_ns = time.perf_counter_ns() - t0
            return {"ok": True, "success": bool(res.fully_resolved),
                    "cost_ns": self._experience_cost_since(clone, waterline),
                    "wall_ns": wall_ns,
                    "attempted": [a.strategy for a in res.attempts]}
        except Exception as exc:
            return {"ok": False,
                    "error": f"{type(exc).__name__}: {exc}"}
        finally:
            try:
                os.remove(clone)
            except OSError:
                pass

    # -- validation ------------------------------------------------------
    def validate(self, improvement: Improvement) -> AcquisitionValidationVerdict:
        verdict = AcquisitionValidationVerdict(passed=False)
        change = improvement.proposed_change

        ok, reason = self._check_change(change)
        if not ok:
            verdict.reasons.append(reason)
            return verdict
        prune = change["prune"]
        signature = prune["signature"]
        strategies = list(prune["strategies"])

        if self.goal_sampler is None:
            verdict.reasons.append(
                "no held-out goal sampler configured; refusing to validate "
                "without fresh goals of the pruned signature class")
            return verdict

        base_db = self._base_db()
        if base_db is None:
            verdict.reasons.append(
                "no database backing the experience store; cannot clone a "
                "trial world")
            return verdict

        goals, goal_batch_id = self._sample_goals(signature)
        verdict.goal_batch_id = goal_batch_id
        req = improvement.validation_requirements
        n_trials = int(req.get("min_trials_per_arm", self.trials_per_arm))
        if len(goals) < n_trials:
            verdict.reasons.append(
                f"sampler supplied {len(goals)} held-out goal(s) for the "
                f"signature, need at least {n_trials}; refusing rather "
                f"than validating on too little evidence"
                + (f" [batch {goal_batch_id}]" if goal_batch_id else ""))
            return verdict
        goals = goals[:n_trials]
        verdict.trials_per_arm = len(goals)

        # The honest incumbent: whatever pruning is live in production
        # right now. The candidate is the incumbent PLUS the proposed
        # prune — the comparison isolates exactly the change under test.
        incumbent = {s: set(v) for s, v in (
            getattr(self.engine, "acquisition_pruning", None) or {}).items()}
        candidate_pruning = {s: set(v) for s, v in incumbent.items()}
        candidate_pruning.setdefault(signature, set()).update(strategies)

        baseline, candidate = [], []
        for goal, examples in goals:
            baseline.append(self._run_trial(base_db, goal, examples, incumbent))
            candidate.append(self._run_trial(
                base_db, goal, examples, candidate_pruning))

        failed = [t for t in baseline + candidate if not t["ok"]]
        if failed:
            verdict.reasons.append(
                f"{len(failed)} trial(s) errored instead of producing a "
                f"measured outcome ({failed[0]['error'][:120]}); "
                f"fail-closed, the change is not validated")
            return verdict

        verdict.baseline_successes = sum(1 for t in baseline if t["success"])
        verdict.candidate_successes = sum(1 for t in candidate if t["success"])
        verdict.baseline_cost_ns = sum(t["cost_ns"] for t in baseline)
        verdict.candidate_cost_ns = sum(t["cost_ns"] for t in candidate)
        verdict.baseline_pruned_attempted = any(
            s in t["attempted"] for t in baseline for s in strategies)

        if req.get("no_success_regression", True) and \
                verdict.candidate_successes < verdict.baseline_successes:
            verdict.reasons.append(
                f"success regressed on the original objective: "
                f"{verdict.candidate_successes}/{verdict.trials_per_arm} "
                f"held-out goals resolved with the prune vs "
                f"{verdict.baseline_successes}/{verdict.trials_per_arm} "
                f"without it — the pruned strategy was load-bearing; refused")
            return verdict

        if not verdict.baseline_pruned_attempted:
            verdict.reasons.append(
                "the pruned strategy was not attempted in any baseline "
                "trial, so this A/B cannot tell whether the prune matters; "
                "no evidence of benefit, refused")
            return verdict

        max_ratio = req.get("max_cost_ratio", 1.0)
        if verdict.baseline_cost_ns > 0 and \
                verdict.candidate_cost_ns > verdict.baseline_cost_ns * max_ratio:
            verdict.reasons.append(
                f"candidate cost {verdict.candidate_cost_ns / 1e9:.2f}s "
                f"exceeds {max_ratio}x the baseline "
                f"{verdict.baseline_cost_ns / 1e9:.2f}s; refused")
            return verdict

        # Genuine improvement, not just "differently configured": with no
        # success regression established above, the prune must actually
        # remove real cost on the held-out goals.
        if verdict.baseline_cost_ns > 0 and \
                verdict.candidate_cost_ns >= verdict.baseline_cost_ns:
            verdict.reasons.append(
                "candidate did not reduce real attempt cost on the held-out "
                "goals; no evidence it is actually better, only differently "
                "configured — refused")
            return verdict

        verdict.passed = True
        verdict.reasons.append(
            f"no success regression "
            f"({verdict.candidate_successes}/{verdict.trials_per_arm} vs "
            f"{verdict.baseline_successes}/{verdict.trials_per_arm}); "
            f"real attempt cost "
            f"{verdict.baseline_cost_ns / 1e9:.1f}s -> "
            f"{verdict.candidate_cost_ns / 1e9:.1f}s per "
            f"{verdict.trials_per_arm} held-out goal(s)")
        return verdict


class ImprovementPipeline:
    """Orchestrates the full lifecycle for one or more subsystems' observers,
    reusing ImprovementStore for persistence/lifecycle and a per-subsystem
    validator for the actual comparison."""

    def __init__(self, cognitive_engine, store: ImprovementStore,
                observers: List, validators: Dict[str, Any],
                provenance=None):
        self.engine = cognitive_engine
        self.store = store
        self.observers = observers
        self.validators = validators
        self.provenance = provenance
        self.engine.improvements = store

    def run_cycle(self) -> List[Dict[str, Any]]:
        reports = []
        for observer in self.observers:
            for candidate in observer.observe():
                reports.append(self._process(candidate))
        return reports

    def _process(self, improvement: Improvement) -> Dict[str, Any]:
        self.store.save(improvement)

        for dep_id in improvement.dependencies:
            dep = self.store.get(dep_id)
            if dep is None or dep.state is not ImprovementState.ACTIVE:
                self.store.transition(
                    improvement.improvement_id, ImprovementState.REJECTED,
                    f"dependency {dep_id!r} is not active "
                    f"(state={dep.state.value if dep else 'missing'})")
                return {"improvement_id": improvement.improvement_id,
                       "outcome": "rejected", "reason": "dependency not active"}

        validator = self.validators.get(improvement.target_subsystem)
        if validator is None:
            self.store.transition(improvement.improvement_id,
                                  ImprovementState.REJECTED,
                                  "no validator registered for this subsystem")
            return {"improvement_id": improvement.improvement_id,
                   "outcome": "rejected", "reason": "no validator"}

        verdict = validator.validate(improvement)
        improvement.validation_evidence = verdict.as_dict()
        self.store.save(improvement)

        if not verdict.passed:
            self.store.transition(improvement.improvement_id,
                                  ImprovementState.REJECTED,
                                  "; ".join(verdict.reasons))
            return {"improvement_id": improvement.improvement_id,
                   "outcome": "rejected", "verdict": verdict.as_dict()}

        self.store.transition(improvement.improvement_id,
                              ImprovementState.VALIDATED,
                              "; ".join(verdict.reasons))
        self.store.transition(improvement.improvement_id,
                              ImprovementState.ADMITTED,
                              "validated candidate accepted for activation")

        self._activate(improvement)
        self.store.transition(improvement.improvement_id,
                              ImprovementState.ACTIVE,
                              "applied to the live subsystem")

        # Make activation ordering explicit rather than relying on "highest
        # version among possibly-several ACTIVE rows" — the predecessor is
        # no longer governing anything, so its own state should say so.
        if improvement.predecessor_id:
            predecessor = self.store.get(improvement.predecessor_id)
            if predecessor is not None and predecessor.state is ImprovementState.ACTIVE:
                self.store.transition(improvement.predecessor_id,
                                      ImprovementState.SUPERSEDED,
                                      f"superseded by {improvement.improvement_id}")

        if self.provenance is not None:
            from swarm_engine.governance.provenance import Origin, ProvenanceRecord, TrustLevel
            self.provenance.record(ProvenanceRecord(
                capability_id=improvement.improvement_id,
                origin=Origin.DERIVED, trust=TrustLevel.TESTED,
                source=improvement.provenance.get("generated_by", "unknown"),
                parents=improvement.dependencies))
            self.provenance.log(improvement.improvement_id, "activated",
                                improvement.motivation[:400])

        return {"improvement_id": improvement.improvement_id,
               "outcome": "active", "verdict": verdict.as_dict()}

    def _activate(self, improvement: Improvement,
                  from_persisted: bool = False) -> None:
        # from_persisted=True is the boot-time reactivation path: the
        # improvement is already ACTIVE and its rollback snapshot must be
        # reapplied, never recomputed from a fresh engine (recomputing
        # would snapshot an empty baseline and, for chained improvements,
        # drop every earlier delta).
        if improvement.target_subsystem == "cognition.search_policy":
            change = improvement.proposed_change
            synth = self.engine.reasoning.synthesizer
            synth.max_size = change.get("max_size", synth.max_size)
            synth.pool_per_type = change.get("pool_per_type", synth.pool_per_type)
            synth.max_arg_combinations = change.get(
                "max_arg_combinations", synth.max_arg_combinations)
            synth.max_candidates = change.get("max_candidates", synth.max_candidates)
            # Every prior exhaustion record is evidence about the OLD
            # policy; keeping them would block retrying the very goals that
            # motivated this improvement, under the dedup mechanism built
            # for a different purpose (item 2) that has no way to know the
            # thing it deduplicates against just changed.
            cleared = self.engine.exhausted.clear()
            improvement.rollback_info["exhaustion_records_cleared"] = cleared
        elif improvement.target_subsystem == "acquisition.strategy_policy":
            if from_persisted and "pruning_after" in improvement.rollback_info:
                # Reapply the cumulative snapshot recorded at activation
                # time. The rollback baseline stays exactly as it was.
                merged = {s: set(v) for s, v in
                          improvement.rollback_info["pruning_after"].items()}
                apply_pruning_to_engine(self.engine, merged)
            else:
                # Snapshot-then-apply, so rollback restores exactly what
                # was live before this improvement (which may itself
                # include an earlier improvement's pruning).
                prune = improvement.proposed_change.get("prune", {}) or {}
                signature = prune.get("signature")
                strategies = prune.get("strategies", []) or []
                before = {s: set(v) for s, v in (
                    getattr(self.engine, "acquisition_pruning", None) or {}).items()}
                improvement.rollback_info["pruning_before"] = {
                    s: sorted(v) for s, v in before.items()}
                merged = {s: set(v) for s, v in before.items()}
                if signature:
                    merged.setdefault(signature, set()).update(strategies)
                apply_pruning_to_engine(self.engine, merged)
                improvement.rollback_info["pruning_after"] = {
                    s: sorted(v) for s, v in merged.items()}
        # Activation mutates rollback_info (above); persist it now — but the
        # in-memory object may be stale: transition() re-reads the row
        # from the store and does not mutate our copy, so a blind save
        # would clobber the state transitions above back to CANDIDATE.
        # Merge the rollback mutations into the stored row instead.
        stored = self.store.get(improvement.improvement_id)
        if stored is not None:
            stored.rollback_info.update(improvement.rollback_info)
            improvement = stored
        self.store.save(improvement)

    def record_production_outcome(self, target_subsystem: str, succeeded: bool
                                  ) -> Optional[Dict[str, Any]]:
        """Called once per real, post-activation outcome for a subsystem —
        the actual accumulation `check_for_regression` reasons over. A
        single call here is never sufficient evidence by itself; only
        `check_for_regression`'s minimum-sample floor decides that."""
        active = self.store.active_for(target_subsystem)
        if active is None:
            return None
        self.store.record_outcome(active.improvement_id, succeeded)
        return self.check_for_regression(active.improvement_id)

    def check_for_regression(self, improvement_id: str,
                             min_samples: int = 5,
                             regression_threshold: float = 0.5
                             ) -> Optional[Dict[str, Any]]:
        """Evidence-driven rollback: reads the improvement's own accumulated
        production outcome log (never a value the caller has to assemble by
        hand, which invites exactly the "one failure looks like enough
        evidence" mistake this exists to prevent) and only acts once at
        least `min_samples` real outcomes have been recorded. Policy
        rollback is safe by construction — accumulated SearchBias/
        CaseMemory/ConceptGraph are knowledge independent of which policy
        was active when recorded, so reverting the policy cannot corrupt
        them."""
        improvement = self.store.get(improvement_id)
        if improvement is None or improvement.state is not ImprovementState.ACTIVE:
            return None
        recent = self.store.recent_outcomes(improvement_id)
        if len(recent) < min_samples:
            return None
        recent_success_rate = sum(recent) / len(recent)
        # What counts as "regression" depends on what the improvement
        # claimed. A search-policy improvement claims to solve MORE, so an
        # absolute floor is the right bar. A pruning improvement claims
        # "same success, lower cost", so the bar is the validation-time
        # baseline success rate (with a margin) — an absolute 0.5 floor
        # would spuriously roll back a correct prune of strategies for a
        # signature class where nothing ever succeeded.
        if improvement.target_subsystem == "acquisition.strategy_policy":
            baseline_rate = (improvement.validation_evidence or {}).get(
                "baseline_success_rate", 0.0)
            if recent_success_rate >= baseline_rate - 0.15:
                return None
        elif recent_success_rate >= regression_threshold:
            return None

        rollback = improvement.rollback_info
        if improvement.target_subsystem == "cognition.search_policy":
            synth = self.engine.reasoning.synthesizer
            synth.max_candidates = rollback.get("restore_max_candidates",
                                                synth.max_candidates)
            synth.pool_per_type = rollback.get("restore_pool_per_type",
                                               synth.pool_per_type)
            synth.max_size = rollback.get("restore_max_size", synth.max_size)
            synth.max_arg_combinations = rollback.get(
                "restore_max_arg_combinations", synth.max_arg_combinations)
        elif improvement.target_subsystem == "acquisition.strategy_policy":
            before = rollback.get("pruning_before") or {}
            apply_pruning_to_engine(self.engine, before)

        self.store.transition(improvement_id, ImprovementState.ROLLED_BACK,
                              f"recent production success rate "
                              f"{recent_success_rate:.0%} over {len(recent)} "
                              f"real outcomes indicates regression")

        # Restore the predecessor to ACTIVE, not just revert the raw config
        # numbers — the store's own state needs to agree with what is
        # actually governing the subsystem, or a later lookup would find
        # nothing ACTIVE at all despite the live policy being perfectly
        # functional (the predecessor's).
        restored_id = None
        if improvement.predecessor_id:
            predecessor = self.store.get(improvement.predecessor_id)
            if predecessor is not None and predecessor.state is ImprovementState.SUPERSEDED:
                self.store.transition(improvement.predecessor_id,
                                      ImprovementState.ACTIVE,
                                      f"restored after {improvement_id} was "
                                      f"rolled back")
                restored_id = improvement.predecessor_id
                # Same staleness argument as activation, in reverse: any
                # exhaustion recorded while the now-rolled-back policy was
                # active is evidence about a policy that no longer exists.
                # (Only the cognition pipeline's engine exposes the
                # exhausted-search memory directly; the acquisition
                # pipeline's engine does not, and its rollback must not
                # touch cognition's records.)
                _exhausted = getattr(self.engine, "exhausted", None)
                if _exhausted is not None:
                    _exhausted.clear()

        return {"rolled_back": True, "improvement_id": improvement_id,
               "recent_success_rate": recent_success_rate,
               "sample_size": len(recent), "restored": restored_id}

    def reactivate_persisted(self) -> List[str]:
        """Boot-time: apply whatever improvement is ACTIVE for each
        subsystem an observer targets — what makes "a future SWarm instance
        recognizes the improvement exists and uses it" true rather than
        merely stored and ignored."""
        reactivated = []
        subsystems = {obs.target_subsystem for obs in self.observers}
        for subsystem in subsystems:
            active = self.store.active_for(subsystem)
            if active is not None:
                self._activate(active, from_persisted=True)
                reactivated.append(active.improvement_id)
        return reactivated
