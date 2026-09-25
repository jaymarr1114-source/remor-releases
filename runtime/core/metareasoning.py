"""
swarm_engine/core/metareasoning.py

Meta-reasoning: SWarm assessing its own reasoning before acting on it, rather
than executing the first thing that type-checks.

This is deliberately built on evidence the engine already produces —
SemanticMatcher scores, provenance confidence, verification evidence,
composition analysis — rather than a new confidence number invented for this
module. Meta-reasoning that manufactures its own opinion of quality,
independent of what was actually measured, is exactly the self-certification
problem the independent-verification layer exists to prevent; this module
reads evidence, it does not generate it.

Five judgements, each answerable from something concrete:

  assess_confidence     how much evidence actually backs this plan/capability?
  detect_contradiction  do two candidate answers for the same goal disagree?
  localize_error        which stage of a failed plan is the likely fault?
  needs_verification    is the evidence behind this thin enough to warrant
                         another check before it is trusted?
  should_escalate        confidence/evidence too weak to proceed; what should
                         happen instead (acquire, ask, retry, refuse)?
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional, Sequence, Tuple


class EscalationAction(Enum):
    PROCEED = "proceed"                # confidence is sufficient
    VERIFY_FURTHER = "verify_further"  # run more evidence-gathering first
    ACQUIRE = "acquire"                # the real gap is a missing capability
    ASK = "ask"                        # ambiguous enough to need clarification
    REFUSE = "refuse"                  # no responsible action available


@dataclass
class ConfidenceAssessment:
    score: float                        # 0..1, evidence-weighted
    basis: List[str] = field(default_factory=list)
    weaknesses: List[str] = field(default_factory=list)

    def as_dict(self) -> Dict[str, Any]:
        return {"score": round(self.score, 3), "basis": self.basis,
                "weaknesses": self.weaknesses}


@dataclass
class Contradiction:
    goal: str
    values: List[Any]
    sources: List[str]

    def as_dict(self) -> Dict[str, Any]:
        return {"goal": self.goal, "values": [repr(v) for v in self.values],
                "sources": self.sources}


@dataclass
class EscalationDecision:
    action: EscalationAction
    reason: str

    def as_dict(self) -> Dict[str, Any]:
        return {"action": self.action.value, "reason": self.reason}


class MetaReasoner:
    """Reads evidence the engine already produced and judges what it's worth."""

    MIN_TRUSTED_USES = 5
    LOW_CONFIDENCE_THRESHOLD = 0.35
    THIN_EVIDENCE_USES = 3

    def __init__(self, provenance, matcher=None, diagnoser=None):
        self.provenance = provenance
        self.matcher = matcher
        self.diagnoser = diagnoser

    # -- confidence -----------------------------------------------------------
    # Trust level is real evidence on its own: TESTED means the capability
    # already survived independent scanning, sandboxing, behavioural and
    # adversarial checks before it was ever invoked live. Reading only
    # `record.confidence` (which is purely usage-frequency-weighted) collapsed
    # a brand-new TESTED capability to a flat 0.0 — the same "no evidence yet
    # means zero" mistake as the role-reliability bug, just in the provenance
    # reader instead of the role tracker. A trust-level baseline fixes it: a
    # TESTED capability starts meaningfully above zero, and live usage moves
    # the score further as it accumulates, rather than usage being the only
    # signal that counts.
    _TRUST_BASELINE = {"QUARANTINED": 0.0, "UNKNOWN": 0.1, "SCANNED": 0.3,
                       "SANDBOXED": 0.45, "TESTED": 0.6, "TRUSTED": 0.85}

    def assess_confidence(self, capability_id: Optional[str] = None,
                          match_score: Optional[float] = None,
                          verification_evidence: Optional[Dict[str, Any]] = None
                          ) -> ConfidenceAssessment:
        """Combine whatever evidence is actually available. Missing evidence
        lowers confidence rather than being treated as neutral — an untested
        plan is not a 50% plan, it is a plan with nothing behind it yet."""
        basis, weaknesses, parts = [], [], []

        if capability_id is not None:
            record = self.provenance.get(capability_id)
            if record is None:
                weaknesses.append("no provenance record for this capability")
                parts.append(0.0)
            else:
                baseline = self._TRUST_BASELINE.get(record.trust.name, 0.1)
                weight = record.total_uses / (record.total_uses + 5.0)
                blended = baseline * (1 - weight) + record.confidence * weight
                parts.append(blended)
                basis.append(f"provenance: {record.trust.name}, "
                             f"{record.total_uses} uses, "
                             f"{record.success_rate:.0%} success")
                if record.total_uses < self.THIN_EVIDENCE_USES:
                    weaknesses.append(
                        f"only {record.total_uses} use(s) recorded; confidence "
                        f"is not yet well established")
                if record.trust.name == "QUARANTINED":
                    weaknesses.append("capability is quarantined")
                    parts[-1] = 0.0

        if match_score is not None:
            parts.append(match_score)
            basis.append(f"semantic match score {match_score:.2f}")
            if match_score < 0.6:
                weaknesses.append("semantic match to the goal is marginal")

        if verification_evidence is not None:
            levels = verification_evidence.get("levels", {})
            failed = [k for k, v in levels.items() if v is False]
            passed_fraction = (sum(1 for v in levels.values() if v) / len(levels)
                               if levels else 0.0)
            parts.append(passed_fraction)
            basis.append(f"verification: {sum(1 for v in levels.values() if v)}"
                         f"/{len(levels)} levels passed")
            if failed:
                weaknesses.append(f"failed verification levels: {failed}")

        score = sum(parts) / len(parts) if parts else 0.0
        return ConfidenceAssessment(score=score, basis=basis, weaknesses=weaknesses)

    # -- contradiction ----------------------------------------------------------
    def detect_contradiction(self, goal: str,
                             results: Sequence[Tuple[str, Any]]
                             ) -> Optional[Contradiction]:
        """Do independent routes to the same goal disagree?

        `results` is (source_label, value) pairs — e.g. two competing
        capabilities, or a capability vs. a freshly composed plan, evaluated
        on the same input. Disagreement is real evidence something is wrong,
        even before either side is individually diagnosed.
        """
        distinct = {}
        for source, value in results:
            key = repr(value)
            distinct.setdefault(key, []).append((source, value))
        if len(distinct) <= 1:
            return None
        values = [group[0][1] for group in distinct.values()]
        sources = [", ".join(s for s, _ in group) for group in distinct.values()]
        return Contradiction(goal=goal, values=values, sources=sources)

    # -- error localization -------------------------------------------------------
    def localize_error(self, plan_steps: Sequence[Dict[str, Any]],
                       trace: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
        """Which step is the likely fault, given a plan and its execution
        trace? The last step that appears in the trace is where execution
        stopped; anything before it ran, anything after it never got the
        chance to. This is a structural inference from what actually
        executed, not a guess."""
        step_ids = [s.get("id") for s in plan_steps]
        traced_ids = [t.get("primitive") or t.get("id") for t in trace]

        if not traced_ids:
            return {"stage": step_ids[0] if step_ids else None,
                    "reason": "no step in the plan executed at all",
                    "valid_prior_steps": []}

        last_traced = traced_ids[-1]
        try:
            index = next(i for i, s in enumerate(step_ids)
                        if s == last_traced or str(s) in str(last_traced))
        except StopIteration:
            index = len(traced_ids) - 1

        # The fault is the step that never got the chance to run, not the
        # last one that did. If s1 and s2 both appear in the trace and s3
        # does not, execution got through s1 and s2 cleanly — the likely
        # fault is s3, or whatever stands between s2 and s3. Reporting s2
        # (the last thing that worked) as "the stage" pointed at the wrong
        # step entirely: the one place we have positive evidence ran fine.
        fault_index = index + 1
        stage = (step_ids[fault_index] if fault_index < len(step_ids)
                 else last_traced)

        return {"stage": stage,
                "reason": (f"execution completed step {index + 1}/{len(step_ids)} "
                          f"({last_traced}); the fault lies at or after the next "
                          f"step, which never ran" if fault_index < len(step_ids)
                          else f"the last step ({last_traced}) is where execution "
                               f"stopped"),
                "valid_prior_steps": step_ids[:index + 1]}

    # -- evidence sufficiency -----------------------------------------------------
    def needs_verification(self, assessment: ConfidenceAssessment) -> bool:
        return (assessment.score < self.LOW_CONFIDENCE_THRESHOLD
                or bool(assessment.weaknesses))

    # -- escalation -----------------------------------------------------------
    def should_escalate(self, assessment: ConfidenceAssessment,
                        gap_exists: bool = False,
                        ambiguous: bool = False) -> EscalationDecision:
        """What should happen given the evidence, rather than proceeding by
        default. This is the concrete form of "I have a weak answer" /
        "I need another capability" / "I cannot establish correctness"."""
        if gap_exists:
            return EscalationDecision(
                EscalationAction.ACQUIRE,
                "no existing capability or composition covers this; the "
                "weak confidence reflects a genuine capability gap, not a "
                "weak candidate")
        if ambiguous:
            return EscalationDecision(
                EscalationAction.ASK,
                "the goal admits more than one materially different reading "
                "and evidence does not favour one")
        if assessment.score >= 0.7 and not assessment.weaknesses:
            return EscalationDecision(EscalationAction.PROCEED,
                                      "confidence is well-supported")
        if assessment.score >= self.LOW_CONFIDENCE_THRESHOLD:
            return EscalationDecision(
                EscalationAction.VERIFY_FURTHER,
                f"confidence {assessment.score:.2f} is usable but thin "
                f"({'; '.join(assessment.weaknesses) or 'no specific weakness'}); "
                f"more evidence should be gathered before treating this as settled")
        return EscalationDecision(
            EscalationAction.REFUSE,
            f"confidence {assessment.score:.2f} is too low to responsibly act on "
            f"({'; '.join(assessment.weaknesses) or 'no evidence available'})")
