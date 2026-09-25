"""
Multi-subsystem improvement candidate competition.

Collects candidates from independent observers, scores them with an explicit
utility function independent of subsystem identity, and selects at most one
winner for the existing ImprovementPipeline.
"""
from __future__ import annotations

import math
import time
from dataclasses import dataclass, field, asdict
from typing import Any, Callable, Dict, List, Optional, Sequence

from swarm_engine.improvement.substrate import Improvement, ImprovementState


@dataclass
class RankedCandidate:
    improvement: Improvement
    utility: float
    factors: Dict[str, float]
    rationale: str

    def as_dict(self) -> Dict[str, Any]:
        return {
            "improvement_id": self.improvement.improvement_id,
            "subsystem": self.improvement.target_subsystem,
            "utility": self.utility,
            "factors": self.factors,
            "rationale": self.rationale,
            "motivation": self.improvement.motivation,
            "proposed_change": self.improvement.proposed_change,
            "evidence": self.improvement.evidence,
            "provenance": self.improvement.provenance,
        }


def score_candidate(imp: Improvement) -> RankedCandidate:
    """Explicit, deterministic utility independent of candidate identity.

    utility = evidence_strength * confidence * expected_benefit_score
              * reversibility / (1 + estimated_cost_units)

    Factors are derived only from evidence/proposed_change fields that
    observers already populate — no subsystem-name special cases.
    """
    ev = dict(imp.evidence or {})
    attempts = float(ev.get("attempts") or ev.get("attempt_count") or 0)
    successes = float(ev.get("successes") or ev.get("success_count") or 0)
    wasted = float(ev.get("total_wasted_ns") or ev.get("wasted_cost") or 0)
    mean_cost = float(ev.get("mean_cost_ns") or 0)

    # Evidence strength: more failed attempts → stronger signal (log-scaled)
    evidence_strength = math.log1p(max(0.0, attempts)) / math.log1p(20.0)
    evidence_strength = max(0.0, min(1.5, evidence_strength))

    # Confidence: zero successes required for prune-style changes; otherwise success rate
    if attempts > 0 and successes == 0:
        confidence = min(1.0, attempts / 5.0)
    elif attempts > 0:
        confidence = max(0.1, 1.0 - successes / attempts)
    else:
        confidence = 0.1

    # Expected benefit from wasted cost (ns → unit scale)
    benefit = math.log1p(wasted / 1e9) / math.log1p(10.0)  # ~1.0 at 10s waste
    benefit = max(0.05, min(2.0, benefit))

    # Reversibility: default high for policy prunes; lower if marked irreversible
    rev = ev.get("reversibility")
    if rev is None:
        rev = 1.0 if "prune" in (imp.proposed_change or {}) else 0.7
    reversibility = float(rev)

    # Cost of applying improvement (validation cost proxy)
    est_cost = float(ev.get("estimated_apply_cost") or 1.0)

    utility = (evidence_strength * confidence * benefit * reversibility) / (1.0 + est_cost)

    factors = {
        "evidence_strength": round(evidence_strength, 4),
        "confidence": round(confidence, 4),
        "expected_benefit_score": round(benefit, 4),
        "reversibility": round(reversibility, 4),
        "estimated_cost": round(est_cost, 4),
        "attempts": attempts,
        "successes": successes,
        "wasted_ns": wasted,
    }
    rationale = (
        f"utility={utility:.4f} from evidence_strength={evidence_strength:.3f} "
        f"* confidence={confidence:.3f} * benefit={benefit:.3f} "
        f"* reversibility={reversibility:.2f} / (1+cost={est_cost:.2f}); "
        f"attempts={int(attempts)} successes={int(successes)} "
        f"wasted_s={wasted/1e9:.2f}"
    )
    return RankedCandidate(improvement=imp, utility=utility, factors=factors,
                           rationale=rationale)


class MultiSubsystemSelector:
    """Gather observer candidates, rank by utility, select one winner."""

    def __init__(self, observers: Sequence[Any], pipeline: Any = None):
        self.observers = list(observers)
        self.pipeline = pipeline

    def collect(self) -> List[Improvement]:
        out: List[Improvement] = []
        for obs in self.observers:
            try:
                found = obs.observe() or []
            except Exception:
                found = []
            for imp in found:
                # Ensure provenance names the observer
                prov = dict(imp.provenance or {})
                prov.setdefault("generated_by", type(obs).__name__)
                prov.setdefault("collected_at", time.time())
                imp.provenance = prov
                out.append(imp)
        return out

    def rank(self, candidates: List[Improvement]) -> List[RankedCandidate]:
        ranked = [score_candidate(c) for c in candidates]
        ranked.sort(key=lambda r: (-r.utility, r.improvement.improvement_id))
        return ranked

    def select(self, candidates: Optional[List[Improvement]] = None) -> Dict[str, Any]:
        cands = candidates if candidates is not None else self.collect()
        ranked = self.rank(cands)
        report = {
            "candidate_count": len(ranked),
            "candidates": [r.as_dict() for r in ranked],
            "selected": None,
            "selection_rationale": None,
        }
        if not ranked:
            report["selection_rationale"] = "no candidates from any observer"
            return report
        if ranked[0].utility <= 0:
            report["selection_rationale"] = "top utility non-positive; refusing selection"
            return report
        winner = ranked[0]
        report["selected"] = winner.as_dict()
        report["selection_rationale"] = (
            f"selected {winner.improvement.improvement_id} "
            f"(subsystem={winner.improvement.target_subsystem}) "
            f"with highest utility among {len(ranked)} candidates; "
            + winner.rationale
        )
        return report

    def select_and_process(self) -> Dict[str, Any]:
        """Select winner and route through existing ImprovementPipeline."""
        report = self.select()
        report["pipeline_outcome"] = None
        if not report.get("selected") or self.pipeline is None:
            return report
        # Reconstruct Improvement from selected id among collected
        selected_id = report["selected"]["improvement_id"]
        cands = self.collect()
        winner_imp = next((c for c in cands if c.improvement_id == selected_id), None)
        if winner_imp is None:
            report["pipeline_outcome"] = {"outcome": "error", "reason": "winner missing on recollect"}
            return report
        outcome = self.pipeline._process(winner_imp)
        report["pipeline_outcome"] = outcome
        return report
