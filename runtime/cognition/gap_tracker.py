"""
swarm_engine/cognition/gap_tracker.py

GapTracker: minimal autonomous gap detection and objective formulation.

The reachable portion of the Agency Gap, opened by the language track's
fail-closed behavior.

The chain:
  1. Knowledge state: the SemanticLexicon (what phrases are known).
  2. Unresolved opportunity: an interpretation fails closed because a phrase
     has no confident association (e.g. "bellow"). This is a DETECTED gap,
     not a guess -- the interpreter refused rather than hallucinating.
  3. Candidate objectives: each distinct failed phrase becomes a candidate
     learning objective ("acquire training data for phrase X").
  4. Autonomous selection: the tracker ranks candidates by failure frequency
     (internal evidence) and selects the top one. No developer picks the
     winner; the ranking is from the system's own experience.
  5. Action: the tracker outputs a structured learning request. (Full
     autonomous data acquisition is beyond the reachable portion; the
     request is the actionable output.)
  6. Evaluation: when training data arrives and the lexicon learns the
     phrase, the gap is marked resolved.

What this is NOT:
  - Not full agency: the system cannot autonomously gather training data.
    It CAN autonomously detect gaps, formulate objectives, and select among
    them using internal evidence. That selection is the agency contribution.
  - Not hand-authored: the gaps come from actual interpretation failures,
    the ranking from failure counts, the selection from the ranking.

Fail-closed throughout: if no gaps are tracked, selection returns None.
"""
from __future__ import annotations

import time
from collections import Counter
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple


@dataclass
class GapRecord:
    """A detected capability gap."""
    phrase: str                  # the unknown phrase
    first_seen: float
    last_seen: float
    failure_count: int = 1
    example_sentences: List[str] = field(default_factory=list)
    resolved: bool = False

    def as_dict(self) -> Dict:
        return {
            "phrase": self.phrase,
            "failure_count": self.failure_count,
            "first_seen": self.first_seen,
            "last_seen": self.last_seen,
            "example_sentences": self.example_sentences[:3],
            "resolved": self.resolved,
        }


@dataclass
class LearningObjective:
    """An autonomously formulated learning objective."""
    phrase: str
    rationale: str               # why this was selected (evidence-based)
    failure_count: int
    example_sentences: List[str]

    def as_dict(self) -> Dict:
        return {
            "phrase": self.phrase,
            "rationale": self.rationale,
            "failure_count": self.failure_count,
            "example_sentences": self.example_sentences,
        }


class GapTracker:
    """Tracks interpretation failures and autonomously selects learning objectives."""

    def __init__(self):
        self._gaps: Dict[str, GapRecord] = {}

    def record_failure(self, sentence: str, unknown_phrases: List[str]) -> None:
        """Record that interpretation failed due to unknown phrases.
        Called by the interpreter (or its caller) on fail-closed."""
        now = time.time()
        for phrase in unknown_phrases:
            if phrase in self._gaps:
                rec = self._gaps[phrase]
                rec.failure_count += 1
                rec.last_seen = now
                if sentence not in rec.example_sentences:
                    rec.example_sentences.append(sentence)
            else:
                self._gaps[phrase] = GapRecord(
                    phrase=phrase, first_seen=now, last_seen=now,
                    example_sentences=[sentence])

    def mark_resolved(self, phrase: str) -> None:
        """Mark a gap as resolved (e.g. after the lexicon learns the phrase)."""
        if phrase in self._gaps:
            self._gaps[phrase].resolved = True

    def unresolved(self) -> List[GapRecord]:
        """All unresolved gaps, sorted by failure count descending."""
        out = [r for r in self._gaps.values() if not r.resolved]
        out.sort(key=lambda r: (-r.failure_count, r.first_seen))
        return out

    def select_next_objective(self) -> Optional[LearningObjective]:
        """Autonomously select the next learning objective.

        Selection criterion: highest failure frequency (the gap the system
        has encountered most often). Ties broken by earliest first seen.
        Returns None if no unresolved gaps (fail-closed).
        """
        cands = self.unresolved()
        if not cands:
            return None
        top = cands[0]
        return LearningObjective(
            phrase=top.phrase,
            rationale=(f"selected from {len(cands)} candidate gaps by "
                       f"failure frequency ({top.failure_count} failures); "
                       f"no developer chose this objective"),
            failure_count=top.failure_count,
            example_sentences=list(top.example_sentences[:3]))

    def gap_count(self) -> int:
        return len(self._gaps)

    def unresolved_count(self) -> int:
        return len(self.unresolved())
