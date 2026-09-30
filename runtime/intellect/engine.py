"""
swarm_engine/intellect/engine.py

IntellectualEngine: the actual loop, not a diagram of one.

Observe (real accumulated evidence already in the engine) -> identify a
gap/uncertainty -> post a Question to the agenda -> generate competing
Hypotheses (internal generator; an ExternalReasoner may supply richer ones
if attached) -> design an Experiment -> execute it through EXISTING governed
machinery (SearchPolicyValidator, CognitiveEngine, the improvement
pipeline — never a parallel execution path) -> turn results into Evidence ->
EvidenceArbiter decides each Hypothesis's state from that evidence alone ->
update the Question's status -> post a follow-up Question if the answer
raises one.

Sources this pass actually observes, each backed by real, already-persisted
data rather than anything synthetic:
  - ROLLED_BACK improvements (a genuine "why did this regress" question)
  - recurring exhausted searches (a genuine "is this a vocabulary gap"
    question, reusing ExpressivenessAnalyzer)
  - high-support Concepts (a genuine "does this generalize further"
    question)

Internal hypothesis/experiment generation is real but deliberately narrow —
built for the specific, well-understood question shapes above. Where a
question doesn't match a known shape, the engine says so explicitly rather
than fabricating a plausible-looking guess; that is the stated boundary
where an ExternalReasoner would add real value.
"""
from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from swarm_engine.intellect.agenda import IntellectualAgenda, Question
from swarm_engine.intellect.epistemic import (
    Evidence, EpistemicStore, Experiment, Hypothesis, HypothesisState,
    Observation, QuestionState,
)
from swarm_engine.intellect.unified_memory import (
    record_evidence, record_experiment, record_experience, record_hypothesis,
)
from swarm_engine.intellect.capability_growth import CapabilityGrowthConnector
from swarm_engine.intellect.experience import ExperienceHarvester, ExperienceLog
from swarm_engine.intellect.patterns import IntellectualPattern, PatternMiner, PatternStore
from swarm_engine.intellect.reasoner import EvidenceArbiter, ExternalReasoner, NoExternalReasoner


def _stable_id(prefix: str, *parts: str) -> str:
    digest = hashlib.sha256("|".join(parts).encode()).hexdigest()[:16]
    return f"{prefix}_{digest}"


# Closed set of experiment design kinds _execute_experiment can run.
# A design whose kind is not in this set is refused, never executed.
# External reasoners must constrain design_experiment output to this set;
# it is the schema against which their output is validated.
KNOWN_EXPERIMENT_KINDS = frozenset({
    "replay_validation_comparison",
    "expressiveness_check",
    "pattern_robustness_check",
    "epistemic_reassessment",
})


@dataclass
class CycleReport:
    question_id: Optional[str]
    hypotheses_generated: int
    experiment_run: bool
    evidence_collected: int
    verdicts: List[Dict[str, Any]]
    follow_up_question_id: Optional[str]
    capability_growth_triggered: bool

    def as_dict(self) -> Dict[str, Any]:
        return {"question_id": self.question_id,
                "hypotheses_generated": self.hypotheses_generated,
                "experiment_run": self.experiment_run,
                "evidence_collected": self.evidence_collected,
                "verdicts": self.verdicts,
                "follow_up_question_id": self.follow_up_question_id,
                "capability_growth_triggered": self.capability_growth_triggered}


class IntellectualEngine:
    def __init__(self, swarm_engine, db_path: str = "swarm_engine.db",
                external_reasoner: Optional[ExternalReasoner] = None):
        self.engine = swarm_engine
        self.agenda = IntellectualAgenda(db_path=db_path)
        self.epistemic = EpistemicStore(db_path=db_path)
        self.arbiter = EvidenceArbiter()
        self.reasoner = external_reasoner or NoExternalReasoner()
        # Pattern discovery: general, not category-specific — see patterns.py.
        self.harvester = ExperienceHarvester(swarm_engine)
        self.experience_log = ExperienceLog(db_path=db_path)
        self.pattern_store = PatternStore(db_path=db_path)
        self.miner = PatternMiner(self.pattern_store)
        self.growth = CapabilityGrowthConnector(swarm_engine, self.experience_log)

    def observe(self) -> List[Observation]:
        """Rollback detection is deliberately NOT included here anymore.
        `_observe_rollbacks` is kept below for reference and for any code
        that still calls it directly, but concrete testing showed
        PredictedVsObservedMiner (run via `discover_patterns`) independently
        rediscovers the exact same real regression case this method was
        built specifically to find — running both would post two different
        Question IDs for the same underlying event. Recurring exhaustion and
        strong-concept detection stay: they signal on raw counts/support,
        not a tag-metric correlation, which is a genuinely different kind of
        signal the current general operators don't cover."""
        observations: List[Observation] = []
        observations.extend(self._observe_recurring_exhaustion())
        observations.extend(self._observe_strong_concepts())
        observations.extend(self._observe_reasoner_questions())
        # Writes go through the unified-memory facade: the builder-produced
        # observations keep their ids, content, source, and raw payload;
        # the facade adds the canonical provenance block.
        for obs in observations:
            record_experience(
                self.epistemic,
                origin_loop="intellect",
                kind="intellect_observation",
                content=obs.content,
                raw=dict(obs.raw or {}),
                source=obs.source,
                observation_id=obs.observation_id,
            )
        return observations

    def _observe_reasoner_questions(self) -> List[Observation]:
        """Ask the external reasoner for questions the fixed observers cannot open.

        No-op while NoExternalReasoner is installed (it returns []). Proposed
        questions enter the agenda like any other question — the reasoner only
        proposes; prioritization and verification stay with the engine.
        """
        proposed = self.reasoner.propose_questions(
            {"open_questions": len(self.agenda.open_questions())})
        out: List[Observation] = []
        for text in proposed or []:
            if not isinstance(text, str) or not text.strip():
                continue
            question_id = _stable_id("q_reasoner", text.strip())
            if self.agenda.get(question_id) is not None:
                continue
            question = Question(
                question_id=question_id, text=text.strip(),
                origin="external_reasoner",
                provenance={"generated_by": "external_reasoner"})
            question.log("raised from external_reasoner.propose_questions")
            self.agenda.add(question)
            out.append(Observation(
                observation_id=_stable_id("obs_reasoner_q", question_id),
                content=f"external reasoner proposed question: {text.strip()}",
                source="external_reasoner",
                raw={"question_id": question_id}))
        return out

    def _observe_rollbacks(self) -> List[Observation]:
        from swarm_engine.improvement.substrate import ImprovementState
        out = []
        for imp in self.engine.improvements.all():
            if imp.state is not ImprovementState.ROLLED_BACK:
                continue
            question_id = _stable_id("q_rollback", imp.improvement_id)
            if self.agenda.get(question_id) is not None:
                continue
            out.append(Observation(
                observation_id=_stable_id("obs_rollback", imp.improvement_id),
                content=f"improvement {imp.improvement_id} passed validation "
                       f"but was rolled back after real production evidence "
                       f"showed regression",
                source="rollback",
                raw={"improvement_id": imp.improvement_id,
                    "target_subsystem": imp.target_subsystem,
                    "validation_evidence": imp.validation_evidence}))
        return out

    def _observe_recurring_exhaustion(self) -> List[Observation]:
        out = []
        replayable = self.engine.cognition.exhausted.replayable(only_expressible=True)
        if len(replayable) < 2:
            return out
        signature_key = "|".join(sorted(r["signature"] for r in replayable))
        question_id = _stable_id("q_exhaustion", signature_key)
        if self.agenda.get(question_id) is not None:
            return out
        out.append(Observation(
            observation_id=_stable_id("obs_exhaustion", signature_key),
            content=f"{len(replayable)} type-expressible searches have "
                   f"exhausted the current search policy without success",
            source="recurring_exhaustion",
            raw={"count": len(replayable),
                "signatures": [r["signature"] for r in replayable]}))
        return out

    def _observe_strong_concepts(self) -> List[Observation]:
        out = []
        for concept in self.engine.cognition.concepts.all():
            if concept.support < 2:
                continue
            question_id = _stable_id("q_concept", concept.concept_id)
            if self.agenda.get(question_id) is not None:
                continue
            out.append(Observation(
                observation_id=_stable_id("obs_concept", concept.concept_id),
                content=f"concept {concept.concept_id} "
                       f"({' -> '.join(concept.op_sequence)}) has independently "
                       f"solved {concept.support} distinct goals",
                source="concept_support",
                raw={"concept_id": concept.concept_id,
                    "op_sequence": list(concept.op_sequence),
                    "support": concept.support}))
        return out

    # -- PATTERN DISCOVERY (general, not category-specific) ------------------------
    def discover_patterns(self) -> List[IntellectualPattern]:
        """The new primary route onto the agenda: harvest real accumulated
        experience from every wired source (including the intellect
        engine's own process, closing item 6's recursion), run every
        registered discovery operator over it, and post a Question for
        every newly-discovered pattern whose estimated information gain
        clears a floor. A pattern is never itself a belief — see
        `_question_from_pattern`, which is the only place a pattern's
        existence turns into something investigable, and even that only
        produces a QUESTION, never a conclusion."""
        events = self.harvester.harvest_all()
        self.experience_log.save_all(events)
        discovered = self.miner.run(events)

        for pattern in discovered:
            if pattern.information_gain_estimate < 0.3:
                continue  # noise floor — not every statistical blip is worth a question
            question = self._question_from_pattern(pattern)
            if question is not None:
                pattern.status = "investigating"
                pattern.related_hypotheses = []  # populated once hypotheses exist
                self.pattern_store.save(pattern)
        return discovered

    def _question_from_pattern(self, pattern: IntellectualPattern) -> Optional[Question]:
        """Structure-driven, not category-driven: the sentence FRAME is one
        of three (one per operator kind — that much is authored), but every
        value filled into it comes from the pattern's own discovered fields
        (which tag, which subgroup, which metric, what direction, what
        lift) — none of which were enumerated ahead of time. Whatever tag
        key the miner happened to find a correlation in becomes part of the
        question text mechanically, not by lookup."""
        question_id = _stable_id("q_pattern", pattern.pattern_id)
        if self.agenda.get(question_id) is not None:
            return None

        ev = pattern.supporting_evidence[0] if pattern.supporting_evidence else {}
        if pattern.kind == "subgroup_outlier":
            text = (f"What property distinguishes cases where "
                    f"{ev.get('tag_key')}={ev.get('group_value')!r} from typical "
                    f"cases, given their {ev.get('metric_key')} differs by "
                    f"roughly {abs(ev.get('group_mean', 0) / (ev.get('rest_mean') or 1)):.1f}x?")
        elif pattern.kind == "co_occurrence":
            text = (f"Are {ev.get('item_a')!r} and {ev.get('item_b')!r} instances "
                    f"of a deeper common structure, given they co-occur "
                    f"{ev.get('lift', 0):.1f}x more often than chance would predict?")
        elif pattern.kind == "predicted_vs_observed_gap":
            text = (f"Why did {ev.get('kind')} {pattern.related_capabilities} "
                    f"diverge from its predicted outcome by {ev.get('gap', 0):+.2f}?")
        else:
            text = f"What explains the discovered pattern: {pattern.description}?"

        question = Question(
            question_id=question_id, text=text, origin=f"pattern:{pattern.kind}",
            expected_information_gain=pattern.information_gain_estimate,
            uncertainty=pattern.uncertainty, usefulness=pattern.usefulness_estimate,
            novelty=pattern.novelty,
            provenance={"pattern_id": pattern.pattern_id,
                       "source_observation_ids": pattern.source_observation_ids,
                       "generated_by": pattern.provenance.get("generated_by")})
        question.log(f"raised from pattern {pattern.pattern_id} "
                    f"({pattern.provenance.get('generated_by')})")
        return self.agenda.add(question)

    # -- generic hypotheses/experiments for pattern-derived questions ---------------
    def _generate_pattern_hypotheses(self, question: Question) -> List[Hypothesis]:
        """A genuinely general pair for any subgroup_outlier/co_occurrence
        pattern, not one hardcoded per correlation found: either the
        correlation reflects a real structural relationship, or it's a
        small-sample artifact. Both are testable the same general way —
        does the effect persist against fresh data — regardless of which
        tag/metric the miner happened to flag."""
        statements = [
            "the discovered correlation reflects a genuine structural "
            "relationship and will persist when re-measured against "
            "additional data",
            "the discovered correlation is a small-sample artifact and "
            "will weaken or disappear when re-measured against additional "
            "data",
        ]
        ids = []
        hypotheses = []
        for statement in statements:
            hyp_id = _stable_id("hyp", question.question_id, statement[:40])
            ids.append(hyp_id)
            hypotheses.append(Hypothesis(
                hypothesis_id=hyp_id, question_id=question.question_id,
                statement=statement,
                provenance={"generated_by": "internal:pattern_robustness_generator"}))
        for h in hypotheses:
            h.competing_with = [i for i in ids if i != h.hypothesis_id]
            record_hypothesis(self.epistemic, origin_loop="intellect",
                              kind="pattern_robustness_hypothesis", hypothesis=h)
        return hypotheses

    def _run_pattern_robustness_experiment(self, question: Question,
                                           pattern: IntellectualPattern
                                           ) -> Dict[str, Any]:
        """Re-harvests current experience and re-runs the SAME operator
        criterion the pattern was found under — a real re-measurement
        against whatever data now exists, not a simulated one."""
        events = self.harvester.harvest_all()
        still_present = False
        for operator in self.miner.operators:
            for refound in operator.mine(events):
                if refound.pattern_id == pattern.pattern_id:
                    still_present = True
                    break
        return {"pattern_id": pattern.pattern_id, "still_present": still_present,
               "re_measurement_event_count": len(events)}

    def grow_capabilities_from_patterns(self) -> List[Dict[str, Any]]:
        """Generic over pattern kind and origin subsystem: iterates every
        pattern that isn't already retired/explained and asks the connector
        whether it traces back to a real, reproducible gap — never checking
        pattern.kind or pattern.provenance['generated_by'] to decide whether
        growth is 'applicable.' A pattern from a subgroup_outlier finding, a
        co_occurrence finding, or a future harvester's data is handled by
        the exact same call."""
        reports = []
        for pattern in self.pattern_store.all():
            if pattern.status in ("retired", "explained"):
                continue
            result = self.growth.attempt_growth(pattern)
            if not result.attempted:
                continue
            pattern.log(f"capability growth attempted: {result.reason}")
            if result.improved:
                pattern.status = "explained"
                pattern.related_capabilities.append(result.capability_id or "")
            self.pattern_store.save(pattern)
            reports.append({"pattern_id": pattern.pattern_id, **result.as_dict()})
        return reports

    def identify_gap(self, observation: Observation) -> Optional[Question]:
        if observation.source == "rollback":
            imp_id = observation.raw["improvement_id"]
            question = Question(
                question_id=_stable_id("q_rollback", imp_id),
                text=f"Why did {imp_id} pass validation but regress in real "
                    f"production use?",
                origin="rollback",
                expected_information_gain=0.8, uncertainty=0.7,
                usefulness=0.85, novelty=0.6,
                provenance={"observation_id": observation.observation_id,
                           "improvement_id": imp_id})
        elif observation.source == "recurring_exhaustion":
            question = Question(
                question_id=_stable_id("q_exhaustion",
                                       "|".join(sorted(observation.raw["signatures"]))),
                text=f"Are the {observation.raw['count']} recently-exhausted "
                    f"searches a search-budget problem or a genuine "
                    f"vocabulary gap?",
                origin="recurring_exhaustion",
                expected_information_gain=0.7, uncertainty=0.6,
                usefulness=0.8, novelty=0.5,
                provenance={"observation_id": observation.observation_id})
        elif observation.source == "concept_support":
            concept_id = observation.raw["concept_id"]
            question = Question(
                question_id=_stable_id("q_concept", concept_id),
                text=f"Does concept {concept_id} generalize to a broader "
                    f"class of problems than the ones that formed it?",
                origin="concept_support",
                expected_information_gain=0.4, uncertainty=0.4,
                usefulness=0.5, novelty=0.3,
                provenance={"observation_id": observation.observation_id,
                           "concept_id": concept_id})
        else:
            return None

        question.log(f"raised from observation {observation.observation_id}")
        return self.agenda.add(question)

    def generate_hypotheses(self, question: Question) -> List[Hypothesis]:
        external = self.reasoner.propose_hypotheses(
            question.text, {"origin": question.origin, "provenance": question.provenance})
        if external:
            statements = external
            source = "external_reasoner"
        elif question.origin.startswith("pattern:"):
            pattern_id = question.provenance.get("pattern_id")
            pattern = self.pattern_store.get(pattern_id) if pattern_id else None
            if (pattern is not None and pattern.kind == "predicted_vs_observed_gap"
                    and pattern.provenance.get("event_kind") == "improvement_outcome"):
                # Genuine reuse, not a parallel path: a predicted-vs-observed
                # gap discovered on improvement_outcome events IS a rollback
                # question in every way that matters, so it is generated the
                # same way — this is the concrete demonstration that the
                # general miner subsumes the old hardcoded rollback observer
                # rather than merely coexisting with it.
                statements = [
                    "the candidate configuration is a genuine net regression: it "
                    "underperforms the restored predecessor on the same "
                    "benchmark used at validation time, not just on the "
                    "specific production goals encountered",
                    "this is a false positive: the candidate configuration "
                    "performs comparably to the predecessor on the validation "
                    "benchmark, and the production failures reflect an "
                    "unrepresentative sample rather than a real regression",
                ]
                source = "internal:rollback_generator"
                if pattern.related_capabilities:
                    question.provenance["improvement_id"] = pattern.related_capabilities[0]
                    self.agenda.update(question)
            else:
                return self._generate_pattern_hypotheses(question)
        elif question.origin == "rollback":
            statements = [
                "the candidate configuration is a genuine net regression: it "
                "underperforms the restored predecessor on the same "
                "benchmark used at validation time, not just on the "
                "specific production goals encountered",
                "this is a false positive: the candidate configuration "
                "performs comparably to the predecessor on the validation "
                "benchmark, and the production failures reflect an "
                "unrepresentative sample rather than a real regression",
            ]
            source = "internal:rollback_generator"
        elif question.origin == "recurring_exhaustion":
            statements = [
                "these goals are type-expressible but exceed the current "
                "search budget/pool width — a search-policy problem",
                "these goals are not actually reachable by any composition "
                "in the current primitive vocabulary regardless of budget "
                "— a vocabulary problem",
            ]
            source = "internal:exhaustion_generator"
        else:
            return self._generic_hypotheses(question)

        hypotheses = []
        ids = []
        for statement in statements:
            hyp_id = _stable_id("hyp", question.question_id, statement[:40])
            ids.append(hyp_id)
            hypotheses.append(Hypothesis(
                hypothesis_id=hyp_id, question_id=question.question_id,
                statement=statement, provenance={"generated_by": source}))
        for h in hypotheses:
            h.competing_with = [i for i in ids if i != h.hypothesis_id]
            record_hypothesis(self.epistemic, origin_loop="intellect",
                              kind="generated_hypothesis", hypothesis=h)
        question.log(f"{len(hypotheses)} competing hypothesis(es) generated by {source}")
        self.agenda.update(question)
        return hypotheses

    def _generic_hypotheses(self, question: Question) -> List[Hypothesis]:
        hyp_id = _stable_id("hyp_generic", question.question_id)
        hyp = Hypothesis(
            hypothesis_id=hyp_id, question_id=question.question_id,
            statement="no internal generator exists for this question shape; "
                     "requires an ExternalReasoner or a new observer",
            provenance={"generated_by": "internal:no_generator"})
        record_hypothesis(self.epistemic, origin_loop="intellect",
                          kind="no_generator_hypothesis", hypothesis=hyp)
        question.log("no internal hypothesis generator for this origin; "
                    "external reasoner boundary reached")
        self.agenda.update(question)
        return [hyp]

    def design_and_run_experiment(self, question: Question,
                                  hypotheses: List[Hypothesis]) -> Optional[Experiment]:
        external = self.reasoner.design_experiment(
            question.text, [h.statement for h in hypotheses],
            {"origin": question.origin})
        if external is not None:
            design = external
        elif question.origin.startswith("pattern:") and "improvement_id" in question.provenance:
            design = self._design_rollback_experiment(question, hypotheses)
        elif question.origin.startswith("pattern:"):
            pattern_id = question.provenance.get("pattern_id")
            design = {"kind": "pattern_robustness_check", "pattern_id": pattern_id}
        elif question.origin == "rollback":
            design = self._design_rollback_experiment(question, hypotheses)
        elif question.origin == "recurring_exhaustion":
            design = self._design_exhaustion_experiment(question, hypotheses)
        else:
            return None

        experiment = Experiment(
            experiment_id=_stable_id("exp", question.question_id, str(time.time())),
            question_id=question.question_id,
            hypothesis_ids=[h.hypothesis_id for h in hypotheses],
            design=design)
        result = self._execute_experiment(experiment)
        experiment.executed = True
        experiment.result = result
        record_experiment(self.epistemic, origin_loop="intellect",
                          kind="pattern_robustness_experiment",
                          experiment=experiment)
        question.status = QuestionState.INVESTIGATING
        question.log(f"experiment {experiment.experiment_id} executed")
        self.agenda.update(question)
        return experiment

    def _design_rollback_experiment(self, question: Question,
                                    hypotheses: List[Hypothesis]) -> Dict[str, Any]:
        imp_id = question.provenance["improvement_id"]
        return {"kind": "replay_validation_comparison", "improvement_id": imp_id}

    def _design_exhaustion_experiment(self, question: Question,
                                      hypotheses: List[Hypothesis]) -> Dict[str, Any]:
        return {"kind": "expressiveness_check"}

    def _execute_experiment(self, experiment: Experiment) -> Dict[str, Any]:
        kind = experiment.design.get("kind")
        if kind not in KNOWN_EXPERIMENT_KINDS:
            return {"error": f"unknown experiment kind {kind!r}; not executed"}
        if kind == "replay_validation_comparison":
            return self._run_rollback_experiment(experiment)
        if kind == "expressiveness_check":
            return self._run_exhaustion_experiment(experiment)
        if kind == "pattern_robustness_check":
            pattern = self.pattern_store.get(experiment.design["pattern_id"])
            question = self.agenda.get(experiment.question_id)
            if pattern is None or question is None:
                return {"error": "pattern or question no longer exists"}
            return self._run_pattern_robustness_experiment(question, pattern)
        if kind == "epistemic_reassessment":
            return self._run_epistemic_reassessment(experiment)
        return {"error": f"unknown experiment kind {kind!r}; not executed"}

    def _live_probe_items(self, subject_id: str) -> List[Dict[str, Any]]:
        """Two generic live observations for any subject_id.

        1. Is an executable capability present under generic name forms?
        2. Can held-out exactness be confirmed live? (False if no callable
           or no examples bound on the primitive.)
        """
        if not subject_id:
            return []
        names = [subject_id, str(subject_id).lower().replace(" ", "_"),
                 str(subject_id).replace("_", " ")]
        prim = None
        used = None
        for n in names:
            try:
                prim = self.engine.primitives.get(n)
            except Exception:
                prim = None
            if prim is not None:
                used = n
                break
        live = prim is not None
        items = [{"supports": live,
                  "content": {"channel": "live_capability",
                              "present": live, "name": used}}]
        exact = False
        reason = "no executable capability"
        if prim is not None:
            reason = "live callable present but no bound held-out set on primitive"
            exact = False
        items.append({"supports": exact,
                      "content": {"channel": "live_heldout_exact",
                                  "exact": exact, "reason": reason}})
        return items

    def _run_epistemic_reassessment(self, experiment: Experiment) -> Dict[str, Any]:
        """Live probe + evidence counts. New Evidence is written by
        investigate_open_question from returned live_items."""
        subject_id = (experiment.design or {}).get("subject_id")
        before = len(self.epistemic.evidence_for(f"h_pos:{subject_id}")) if subject_id else 0
        items = []
        if getattr(self, "_live_probe_enabled", True) and subject_id:
            items = self._live_probe_items(str(subject_id))
        return {"kind": "epistemic_reassessment", "subject_id": subject_id,
                "n_pos_evidence_before": before, "live_items": items,
                "live_probe_enabled": getattr(self, "_live_probe_enabled", True)}

    def _run_rollback_experiment(self, experiment: Experiment) -> Dict[str, Any]:
        from swarm_engine.improvement.pipeline import SearchPolicyValidator
        from swarm_engine.improvement.substrate import Improvement
        imp_id = experiment.design["improvement_id"]
        rolled_back = self.engine.improvements.get(imp_id)
        if rolled_back is None:
            return {"error": "improvement record no longer exists"}

        probe = Improvement(
            improvement_id=f"{imp_id}_replay_probe",
            target_subsystem=rolled_back.target_subsystem,
            motivation="intellectual-engine replay for root-cause investigation",
            proposed_change=rolled_back.proposed_change,
            validation_requirements=rolled_back.validation_requirements)
        validator = SearchPolicyValidator(self.engine.cognition)
        verdict = validator.validate(probe)
        return {"replayed_verdict": verdict.as_dict(),
               "original_validation_evidence": rolled_back.validation_evidence}

    def _run_exhaustion_experiment(self, experiment: Experiment) -> Dict[str, Any]:
        replayable = self.engine.cognition.exhausted.replayable(only_expressible=True)
        checks = []
        for record in replayable[:10]:
            diagnosis = self.engine.cognition.reasoning.expressiveness.diagnose(
                record["examples"], record["param_names"])
            checks.append({"signature": record["signature"], **diagnosis})
        return {"checks": checks}

    def update_beliefs(self, experiment: Experiment,
                       hypotheses: List[Hypothesis]) -> List[Dict[str, Any]]:
        result = experiment.result or {}
        evidence_by_hyp: Dict[str, List[Evidence]] = {h.hypothesis_id: [] for h in hypotheses}

        if result.get("replayed_verdict") is not None:
            genuine_h = next((h for h in hypotheses if "genuine net regression"
                             in h.statement), None)
            false_positive_h = next((h for h in hypotheses if "false positive"
                                    in h.statement), None)
            replayed_passed = result["replayed_verdict"]["passed"]
            for h, supports_genuine in ((genuine_h, not replayed_passed),
                                        (false_positive_h, replayed_passed)):
                if h is None:
                    continue
                ev = Evidence(
                    evidence_id=_stable_id("ev", h.hypothesis_id, str(time.time())),
                    target_id=h.hypothesis_id, supports=supports_genuine,
                    content=result["replayed_verdict"],
                    source="replay_validation_comparison")
                record_evidence(self.epistemic, origin_loop="intellect",
                                kind="replay_validation", evidence=ev)
                evidence_by_hyp[h.hypothesis_id].append(ev)

        if "checks" in result:
            budget_h = next((h for h in hypotheses if "search-policy problem"
                            in h.statement), None)
            vocab_h = next((h for h in hypotheses if "vocabulary problem"
                           in h.statement), None)
            for check in result["checks"]:
                expressible = check.get("expressible_by_type")
                for h, supports_budget in ((budget_h, expressible),
                                           (vocab_h, expressible is False)):
                    if h is None or supports_budget is None:
                        continue
                    ev = Evidence(
                        evidence_id=_stable_id("ev", h.hypothesis_id, check["signature"]),
                        target_id=h.hypothesis_id, supports=bool(supports_budget),
                        content=check, source="expressiveness_check")
                    record_evidence(self.epistemic, origin_loop="intellect",
                                    kind="expressiveness_check", evidence=ev)
                    evidence_by_hyp[h.hypothesis_id].append(ev)

        if "still_present" in result:
            genuine_h = next((h for h in hypotheses if "genuine structural"
                             in h.statement), None)
            artifact_h = next((h for h in hypotheses if "small-sample artifact"
                              in h.statement), None)
            still_present = result["still_present"]
            for h, supports_genuine in ((genuine_h, still_present),
                                        (artifact_h, not still_present)):
                if h is None:
                    continue
                ev = Evidence(
                    evidence_id=_stable_id("ev", h.hypothesis_id, str(time.time())),
                    target_id=h.hypothesis_id, supports=supports_genuine,
                    content=result, source="pattern_robustness_check")
                record_evidence(self.epistemic, origin_loop="intellect",
                                kind="pattern_robustness_check", evidence=ev)
                evidence_by_hyp[h.hypothesis_id].append(ev)

        verdicts = []
        for h in hypotheses:
            all_evidence = self.epistemic.evidence_for(h.hypothesis_id)
            verdict = self.arbiter.decide(h.hypothesis_id, all_evidence)
            h.state = verdict.new_state
            h.confidence = verdict.new_confidence
            h.supporting_evidence = [e.evidence_id for e in all_evidence if e.supports]
            h.contradicting_evidence = [e.evidence_id for e in all_evidence if not e.supports]
            record_hypothesis(self.epistemic, origin_loop="intellect",
                              kind="arbitrated_hypothesis", hypothesis=h)
            verdicts.append(verdict.as_dict())
        return verdicts

    def record_task_evidence(self, subject_id: str, statement: str,
                             items: List[Dict[str, Any]],
                             source: str = "task_outcome") -> Dict[str, Any]:
        """Generic Evidence → EvidenceArbiter → persist hypothesis.

        `items` is a list of {"supports": bool, "content": dict}. The
        arbiter — not this method — chooses SUPPORTED / REFUTED /
        UNDER_TEST. A competing negation hypothesis is updated with
        inverted polarity so both directions are evidence-sensitive.
        """
        if not getattr(self, "_epistemic_enabled", True):
            return {"enabled": False, "verdicts": []}
        pos = self.epistemic.get_hypothesis(f"h_pos:{subject_id}")
        if pos is None:
            pos = Hypothesis(
                hypothesis_id=f"h_pos:{subject_id}",
                question_id=f"q:{subject_id}",
                statement=statement,
                state=HypothesisState.PROPOSED)
        neg = self.epistemic.get_hypothesis(f"h_neg:{subject_id}")
        if neg is None:
            neg = Hypothesis(
                hypothesis_id=f"h_neg:{subject_id}",
                question_id=f"q:{subject_id}",
                statement=f"not ({statement})",
                competing_with=[pos.hypothesis_id],
                state=HypothesisState.PROPOSED)
        pos.competing_with = [neg.hypothesis_id]
        stamp = str(time.time())
        for i, item in enumerate(items or []):
            supports = bool(item.get("supports"))
            content = dict(item.get("content") or {})
            ev_pos = Evidence(
                evidence_id=_stable_id("ev", pos.hypothesis_id, stamp, str(i)),
                target_id=pos.hypothesis_id, supports=supports,
                content=content, source=source)
            ev_neg = Evidence(
                evidence_id=_stable_id("ev", neg.hypothesis_id, stamp, str(i)),
                target_id=neg.hypothesis_id, supports=not supports,
                content=content, source=source)
            record_evidence(self.epistemic, origin_loop="intellect",
                            kind="task_outcome", evidence=ev_pos)
            record_evidence(self.epistemic, origin_loop="intellect",
                            kind="task_outcome", evidence=ev_neg)
        verdicts = []
        agenda_q = None
        for h in (pos, neg):
            all_ev = self.epistemic.evidence_for(h.hypothesis_id)
            verdict = self.arbiter.decide(h.hypothesis_id, all_ev)
            h.state = verdict.new_state
            h.confidence = verdict.new_confidence
            h.supporting_evidence = [e.evidence_id for e in all_ev if e.supports]
            h.contradicting_evidence = [e.evidence_id for e in all_ev if not e.supports]
            record_hypothesis(self.epistemic, origin_loop="intellect",
                              kind="arbitrated_hypothesis", hypothesis=h)
            verdicts.append(verdict.as_dict())
        if pos.state in (HypothesisState.REFUTED, HypothesisState.UNDER_TEST):
            qid = _stable_id("q_lh", subject_id)
            if self.agenda.get(qid) is None:
                from swarm_engine.intellect.agenda import Question
                q = Question(
                    question_id=qid,
                    text=f"Is {statement} still under-determined?",
                    origin="lh_outcome",
                    expected_information_gain=0.6, uncertainty=0.6,
                    usefulness=0.7, novelty=0.4,
                    provenance={"subject_id": subject_id})
                q.log(f"raised from arbiter state {pos.state.value}")
                agenda_q = self.agenda.add(q).question_id
        return {"enabled": True, "verdicts": verdicts,
                "agenda_question": agenda_q,
                "pos_state": pos.state.value, "neg_state": neg.state.value,
                "pos_confidence": pos.confidence}

    def investigate_open_question(self, question_id: str) -> Dict[str, Any]:
        """Execute one open agenda question through existing experiment
        machinery. Unknown origins get a generic epistemic_reassessment
        (re-adjudicate stored evidence; optional live probe via subject_id).
        """
        q = self.agenda.get(question_id)
        if q is None:
            return {"ok": False, "error": "question missing"}
        hyps = [h for h in (self.epistemic.all_hypotheses()
                            if hasattr(self.epistemic, "all_hypotheses") else [])
                if h.question_id == q.question_id
                or h.question_id == f"q:{(q.provenance or {}).get('subject_id')}"]
        if not hyps:
            sid = (q.provenance or {}).get("subject_id")
            for hid in (f"h_pos:{sid}", f"h_neg:{sid}"):
                h = self.epistemic.get_hypothesis(hid)
                if h is not None:
                    hyps.append(h)
        experiment = self.design_and_run_experiment(q, hyps)
        if experiment is None:
            # generic origin: force reassessment design
            from swarm_engine.intellect.epistemic import Experiment
            experiment = Experiment(
                experiment_id=_stable_id("exp", q.question_id, "reassess"),
                question_id=q.question_id,
                hypothesis_ids=[h.hypothesis_id for h in hyps],
                design={"kind": "epistemic_reassessment",
                        "subject_id": (q.provenance or {}).get("subject_id")})
            result = self._execute_experiment(experiment)
            experiment.executed = True
            experiment.result = result
            record_experiment(self.epistemic, origin_loop="intellect",
                              kind="epistemic_reassessment",
                              experiment=experiment)
            q.status = QuestionState.INVESTIGATING
            q.log(f"experiment {experiment.experiment_id} executed")
            self.agenda.update(q)
        live = (experiment.result or {}).get("live_items") or []
        sid = (q.provenance or {}).get("subject_id")
        pos = next((h for h in hyps if h.hypothesis_id.startswith("h_pos:")), None)
        before_ids = [e.evidence_id for e in self.epistemic.evidence_for(pos.hypothesis_id)] if pos else []
        probe_rec = None
        if live and sid and getattr(self, "_live_probe_enabled", True):
            statement = pos.statement if pos else q.text
            probe_rec = self.record_task_evidence(
                subject_id=str(sid), statement=statement,
                items=live, source="live_probe")
        verdicts = []
        if hyps:
            verdicts = self.update_beliefs(experiment, hyps)
        after_ids = [e.evidence_id for e in self.epistemic.evidence_for(pos.hypothesis_id)] if pos else []
        new_ids = [i for i in after_ids if i not in before_ids]
        pos2 = self.epistemic.get_hypothesis(pos.hypothesis_id) if pos else None
        follow = None
        if pos2 is not None and pos2.state in (HypothesisState.SUPPORTED, HypothesisState.REFUTED):
            q.status = QuestionState.ANSWERED
            q.log(f"answered via live probe: {pos2.state.value}")
            self.agenda.update(q)
            try:
                follow = self._generate_follow_up(q, hyps, verdicts)
            except Exception:
                follow = None
        return {"ok": True, "question_id": q.question_id,
                "origin": q.origin, "status": q.status.value,
                "experiment": experiment.as_dict() if experiment else None,
                "verdicts": verdicts, "probe": probe_rec,
                "n_evidence_before": len(before_ids),
                "n_evidence_after": len(after_ids),
                "new_evidence_ids": new_ids,
                "pos_state": getattr(getattr(pos2, "state", None), "value", None),
                "follow_up_question_id": None if follow is None else follow.question_id}

    def maybe_trigger_capability_growth(

self, question: Question,
                                        hypotheses: List[Hypothesis]) -> bool:
        if question.origin != "recurring_exhaustion":
            return False
        vocab_h = next((h for h in hypotheses if "vocabulary problem" in h.statement), None)
        budget_h = next((h for h in hypotheses if "search-policy problem" in h.statement), None)
        if budget_h is not None and budget_h.state is HypothesisState.SUPPORTED:
            reports = self.engine.improvement_pipeline.run_cycle()
            question.log(f"triggered improvement pipeline: "
                        f"{[r['outcome'] for r in reports]}")
            self.agenda.update(question)
            return bool(reports)
        if vocab_h is not None and vocab_h.state is HypothesisState.SUPPORTED:
            question.log("confirmed vocabulary gap — flagged for capability "
                        "acquisition; no acquisition observer connected yet")
            self.agenda.update(question)
        return False

    def run_cycle(self) -> Optional[CycleReport]:
        self.discover_patterns()
        self.grow_capabilities_from_patterns()
        self.observe()
        for obs in self.epistemic.all_observations():
            self.identify_gap(obs)

        question = self.agenda.next()
        if question is None:
            return None

        question.status = QuestionState.INVESTIGATING
        self.agenda.update(question)

        hypotheses = self.generate_hypotheses(question)
        experiment = self.design_and_run_experiment(question, hypotheses)

        verdicts: List[Dict[str, Any]] = []
        evidence_count = 0
        if experiment is not None:
            verdicts = self.update_beliefs(experiment, hypotheses)
            evidence_count = sum(len(h.supporting_evidence) + len(h.contradicting_evidence)
                                 for h in hypotheses)

        triggered = self.maybe_trigger_capability_growth(question, hypotheses)

        decided = any(v["new_state"] in ("supported", "refuted") for v in verdicts)
        question.status = QuestionState.ANSWERED if decided else QuestionState.OPEN
        question.log("cycle complete" + (" — answered" if decided else " — inconclusive"))
        self.agenda.update(question)

        follow_up = self._generate_follow_up(question, hypotheses, verdicts)

        return CycleReport(
            question_id=question.question_id,
            hypotheses_generated=len(hypotheses),
            experiment_run=experiment is not None,
            evidence_collected=evidence_count,
            verdicts=verdicts,
            follow_up_question_id=follow_up.question_id if follow_up else None,
            capability_growth_triggered=triggered)

    def _generate_follow_up(self, question: Question, hypotheses: List[Hypothesis],
                            verdicts: List[Dict[str, Any]]) -> Optional[Question]:
        supported = [v for v in verdicts if v["new_state"] == "supported"]
        refuted = [v for v in verdicts if v["new_state"] == "refuted"]
        if question.origin == "rollback" and supported:
            imp_id = question.provenance.get("improvement_id", "")
            follow_id = _stable_id("q_followup", imp_id)
            if self.agenda.get(follow_id) is not None:
                return None
            follow = Question(
                question_id=follow_id,
                text=f"Does the same shortfall that caused {imp_id}'s regression "
                    f"recur if a similarly-derived improvement is proposed again?",
                origin="rollback_followup",
                expected_information_gain=0.5, uncertainty=0.5, usefulness=0.6,
                novelty=0.7, provenance={"parent_question": question.question_id})
            follow.log(f"raised as follow-up to {question.question_id}")
            return self.agenda.add(follow)
        if (getattr(self, "_refute_followup_enabled", True)
                and refuted and question.origin != "refutation_followup"):
            # Generic: a refuted claim leaves a capability/knowledge gap.
            sid = (question.provenance or {}).get("subject_id") or question.question_id
            hid = refuted[0].get("hypothesis_id", "")
            follow_id = _stable_id("q_refute_follow", question.question_id, hid)
            if self.agenda.get(follow_id) is not None:
                return None
            follow = Question(
                question_id=follow_id,
                text=(f"What actionable work would replace or establish "
                      f"the refuted claim {hid or sid}?"),
                origin="refutation_followup",
                expected_information_gain=0.55, uncertainty=0.55,
                usefulness=0.65, novelty=0.75,
                provenance={"parent_question": question.question_id,
                            "subject_id": sid,
                            "refuted_hypothesis": hid,
                            "source": "epistemic_state",
                            "status": "refuted"})
            follow.log(f"raised because {hid or sid} is REFUTED")
            return self.agenda.add(follow)
        return None
