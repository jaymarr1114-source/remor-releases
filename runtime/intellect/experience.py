"""
swarm_engine/intellect/experience.py

ExperienceEvent: one common representation for "something happened,"
carrying structural tags and numeric metrics but no assumption about WHICH
subsystem produced it or what KIND of thing it is. This is what makes
pattern discovery general rather than one detector per hardcoded category:
the miner in patterns.py never asks "is this a rollback" or "is this an
exhausted search" — it asks "which tag keys exist across these events, and
does any of them correlate with a metric." Whether that tag key turns out to
be param_count, primitive_name, question_origin, or something that doesn't
exist yet is not decided here or in the miner; it falls out of whatever the
harvested events actually contain.

Harvesting is done by adapter functions that PULL from existing stores
rather than requiring those stores to be rewritten to push events — every
store this pulls from is already built, tested, and load-bearing elsewhere,
and none of them needs to change shape or behavior for discovery to see
their data.
"""
from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class ExperienceEvent:
    event_id: str
    kind: str
    tags: Dict[str, Any] = field(default_factory=dict)
    metrics: Dict[str, float] = field(default_factory=dict)
    raw_ref: Optional[str] = None
    at: float = field(default_factory=time.time)

    def as_dict(self) -> Dict[str, Any]:
        return {"event_id": self.event_id, "kind": self.kind, "tags": self.tags,
                "metrics": self.metrics, "raw_ref": self.raw_ref, "at": self.at}


class ExperienceHarvester:
    """Pulls ExperienceEvents from existing stores. Each `harvest_*` method
    knows about exactly one existing store's shape; nothing downstream
    (PatternMiner) knows about any of them. Adding a new source means adding
    one more harvest method here — the miner itself never changes."""

    def __init__(self, swarm_engine):
        self.engine = swarm_engine

    def harvest_all(self) -> List[ExperienceEvent]:
        events: List[ExperienceEvent] = []
        events.extend(self.harvest_cases())
        events.extend(self.harvest_exhausted_searches())
        events.extend(self.harvest_improvements())
        events.extend(self.harvest_concepts())
        events.extend(self.harvest_intellect_process())
        return events

    def harvest_cases(self) -> List[ExperienceEvent]:
        out = []
        for case_id, entry in self.engine.cognition.cases.all():
            tags = {"param_count": len(entry.param_names) if entry.param_names else None,
                   "op_sequence_length": len(entry.op_sequence),
                   "primitives_used": list(entry.op_sequence)}
            out.append(ExperienceEvent(
                event_id=f"ev_case_{case_id}", kind="capability_solved",
                tags=tags, metrics={"success": 1.0}, raw_ref=str(case_id)))
        return out

    def harvest_exhausted_searches(self) -> List[ExperienceEvent]:
        out = []
        for record in self.engine.cognition.exhausted.replayable(only_expressible=None):
            tags = {"param_count": len(record["param_names"]),
                   "expressible_by_type": record.get("expressible_by_type")}
            out.append(ExperienceEvent(
                event_id=f"ev_exhausted_{record['signature']}", kind="search_exhausted",
                tags=tags, metrics={"candidates_tried": float(record["candidates_tried"]),
                                    "success": 0.0},
                raw_ref=record["signature"]))
        return out

    def harvest_improvements(self) -> List[ExperienceEvent]:
        out = []
        for imp in self.engine.improvements.all():
            recent = self.engine.improvements.recent_outcomes(imp.improvement_id, limit=50)
            predicted_pass = 1.0 if imp.validation_evidence.get("passed") else 0.0
            observed_rate = (sum(recent) / len(recent)) if recent else None
            tags = {"target_subsystem": imp.target_subsystem,
                   "final_state": imp.state.value,
                   "version": imp.version,
                   "has_predecessor": imp.predecessor_id is not None}
            metrics = {"predicted_pass": predicted_pass}
            if observed_rate is not None:
                metrics["observed_success_rate"] = observed_rate
                metrics["predicted_vs_observed_gap"] = predicted_pass - observed_rate
            out.append(ExperienceEvent(
                event_id=f"ev_improvement_{imp.improvement_id}",
                kind="improvement_outcome", tags=tags, metrics=metrics,
                raw_ref=imp.improvement_id))
        return out

    def harvest_concepts(self) -> List[ExperienceEvent]:
        out = []
        for concept in self.engine.cognition.concepts.all():
            out.append(ExperienceEvent(
                event_id=f"ev_concept_{concept.concept_id}", kind="concept",
                tags={"op_sequence_length": len(concept.op_sequence),
                     "primitives_used": list(concept.op_sequence)},
                metrics={"support": float(concept.support)},
                raw_ref=concept.concept_id))
        return out

    def harvest_intellect_process(self) -> List[ExperienceEvent]:
        """Item 6, recursion: the intellectual engine's own questions and
        hypotheses, harvested through the exact same event shape as
        everything else — this is what lets the SAME miner discover
        patterns in SWarm's intellectual process without a second,
        parallel mechanism built specifically to study the first one."""
        out = []
        intellect = getattr(self.engine, "intellect", None)
        if intellect is None:
            return out
        for q in intellect.agenda.all():
            hyps = intellect.epistemic.hypotheses_for(q.question_id)
            reached_verdict = any(h.state.value in ("supported", "refuted") for h in hyps)
            total_evidence = sum(len(h.supporting_evidence) + len(h.contradicting_evidence)
                                 for h in hyps)
            out.append(ExperienceEvent(
                event_id=f"ev_question_{q.question_id}", kind="question_outcome",
                tags={"origin": q.origin, "status": q.status.value,
                     "hypothesis_count": len(hyps)},
                metrics={"reached_verdict": 1.0 if reached_verdict else 0.0,
                        "evidence_count": float(total_evidence)},
                raw_ref=q.question_id))
        return out


class ExperienceLog:
    def __init__(self, db_path: str = "swarm_engine.db"):
        self.db_path = db_path
        with self._conn() as conn:
            conn.execute("""CREATE TABLE IF NOT EXISTS experience_events (
                event_id TEXT PRIMARY KEY, kind TEXT, data TEXT NOT NULL, at REAL)""")

    def _conn(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def save_all(self, events: List[ExperienceEvent]) -> None:
        with self._conn() as conn:
            for e in events:
                conn.execute("""INSERT OR REPLACE INTO experience_events
                    (event_id, kind, data, at) VALUES (?,?,?,?)""",
                    (e.event_id, e.kind, json.dumps(e.as_dict()), e.at))

    def get(self, event_id: str) -> Optional[ExperienceEvent]:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT data FROM experience_events WHERE event_id=?",
                (event_id,)).fetchone()
        return ExperienceEvent(**json.loads(row["data"])) if row else None
