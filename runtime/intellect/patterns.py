"""
swarm_engine/intellect/patterns.py

IntellectualPattern: a persisted, first-class "this looks unusual and may be
worth investigating" — never a conclusion. Nothing in this file ever writes
a pattern's status as "true" or "confirmed" — the strongest state a pattern
can reach on its own is CONFIRMED_INTERESTING (worth a question), and even
that only means "the statistical/structural signal is real," never "the
explanation is known." The explanation is what the resulting Question ->
Hypothesis -> EvidenceArbiter chain in engine.py exists to establish.

PatternMiner: three general discovery operators, each operating purely on
ExperienceEvent tags/metrics with no hardcoded knowledge of what kind of
event it's looking at:

  SubgroupOutlierMiner     — for every tag key present in the data, group
                              events by that tag's value and flag groups
                              whose mean on some metric deviates sharply
                              from the population.
  CoOccurrenceMiner        — for every list-valued tag, computes lift versus
                              chance co-occurrence within successful events.
  PredictedVsObservedMiner — for events carrying both a predicted and an
                              observed metric, flags systematic gaps. A
                              rollback is exactly one instance of this, not
                              a category needing its own detector.

None of these is a lookup table keyed on event kind. Every one iterates over
whatever tag/metric keys the harvested events actually contain — a new
harvester method in experience.py automatically becomes minable without
touching this file.
"""
from __future__ import annotations

import json
import sqlite3
import statistics
import time
from dataclasses import dataclass, field
from itertools import combinations
from typing import Any, Dict, List, Optional, Tuple

from swarm_engine.intellect.experience import ExperienceEvent


@dataclass
class IntellectualPattern:
    pattern_id: str
    description: str
    kind: str
    source_observation_ids: List[str] = field(default_factory=list)
    supporting_evidence: List[Dict[str, Any]] = field(default_factory=list)
    counterexamples: List[Dict[str, Any]] = field(default_factory=list)
    recurrence: int = 1
    novelty: float = 0.5
    uncertainty: float = 0.5
    information_gain_estimate: float = 0.5
    usefulness_estimate: float = 0.5
    related_capabilities: List[str] = field(default_factory=list)
    related_hypotheses: List[str] = field(default_factory=list)
    provenance: Dict[str, Any] = field(default_factory=dict)
    status: str = "candidate"
    history: List[Dict[str, Any]] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)

    def as_dict(self) -> Dict[str, Any]:
        return {"pattern_id": self.pattern_id, "description": self.description,
                "kind": self.kind, "source_observation_ids": self.source_observation_ids,
                "supporting_evidence": self.supporting_evidence,
                "counterexamples": self.counterexamples, "recurrence": self.recurrence,
                "novelty": self.novelty, "uncertainty": self.uncertainty,
                "information_gain_estimate": self.information_gain_estimate,
                "usefulness_estimate": self.usefulness_estimate,
                "related_capabilities": self.related_capabilities,
                "related_hypotheses": self.related_hypotheses,
                "provenance": self.provenance, "status": self.status,
                "history": self.history, "created_at": self.created_at}

    @staticmethod
    def from_dict(d: Dict[str, Any]) -> "IntellectualPattern":
        return IntellectualPattern(**d)

    def log(self, event: str) -> None:
        self.history.append({"event": event, "at": time.time()})


class PatternStore:
    def __init__(self, db_path: str = "swarm_engine.db"):
        self.db_path = db_path
        with self._conn() as conn:
            conn.execute("""CREATE TABLE IF NOT EXISTS intellectual_patterns (
                pattern_id TEXT PRIMARY KEY, kind TEXT, status TEXT,
                data TEXT NOT NULL, updated_at REAL)""")

    def _conn(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def save(self, pattern: IntellectualPattern) -> IntellectualPattern:
        with self._conn() as conn:
            conn.execute("""INSERT OR REPLACE INTO intellectual_patterns
                (pattern_id, kind, status, data, updated_at) VALUES (?,?,?,?,?)""",
                (pattern.pattern_id, pattern.kind, pattern.status,
                 json.dumps(pattern.as_dict()), time.time()))
        return pattern

    def get(self, pattern_id: str) -> Optional[IntellectualPattern]:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT data FROM intellectual_patterns WHERE pattern_id=?",
                (pattern_id,)).fetchone()
        return IntellectualPattern.from_dict(json.loads(row["data"])) if row else None

    def all(self, status: Optional[str] = None) -> List[IntellectualPattern]:
        with self._conn() as conn:
            if status:
                rows = conn.execute(
                    "SELECT data FROM intellectual_patterns WHERE status=? "
                    "ORDER BY updated_at", (status,)).fetchall()
            else:
                rows = conn.execute(
                    "SELECT data FROM intellectual_patterns ORDER BY updated_at").fetchall()
        return [IntellectualPattern.from_dict(json.loads(r["data"])) for r in rows]


def _pattern_id(kind: str, *parts: str) -> str:
    import hashlib
    digest = hashlib.sha256("|".join(parts).encode()).hexdigest()[:16]
    return f"pat_{kind}_{digest}"


class SubgroupOutlierMiner:
    MIN_GROUP_SIZE = 2
    MIN_Z_SCORE = 1.5

    def mine(self, events: List[ExperienceEvent]) -> List[IntellectualPattern]:
        patterns = []
        tag_keys = {k for e in events for k in e.tags.keys()}
        metric_keys = {k for e in events for k in e.metrics.keys()}
        for tag_key in tag_keys:
            for metric_key in metric_keys:
                patterns.extend(self._mine_one(events, tag_key, metric_key))
        return patterns

    def _mine_one(self, events: List[ExperienceEvent], tag_key: str,
                 metric_key: str) -> List[IntellectualPattern]:
        groups: Dict[Any, List[float]] = {}
        group_event_ids: Dict[Any, List[str]] = {}
        for e in events:
            if tag_key not in e.tags or metric_key not in e.metrics:
                continue
            value = e.tags[tag_key]
            if isinstance(value, list):
                continue
            key = str(value)
            groups.setdefault(key, []).append(e.metrics[metric_key])
            group_event_ids.setdefault(key, []).append(e.event_id)

        if len(groups) < 2:
            return []

        found = []
        for group_value, values in groups.items():
            if len(values) < self.MIN_GROUP_SIZE:
                continue
            # Compare against the REST of the data, excluding this group —
            # not the pooled population including it. Comparing to a pooled
            # population is a real statistical error here: with only two
            # groups, a symmetric split mathematically caps the z-score near
            # 1.0 regardless of how separated the groups actually are,
            # because the group being tested inflates the very population
            # stdev it's being measured against. Verified directly: a
            # blatant 500-vs-15000 split scored z=1.0 and was silently
            # missed under the pooled version before this fix.
            rest = [v for gv, vs in groups.items() if gv != group_value for v in vs]
            if len(rest) < 2:
                continue
            rest_mean = statistics.mean(rest)
            rest_stdev = statistics.pstdev(rest) or 1e-9
            group_mean = statistics.mean(values)
            z = (group_mean - rest_mean) / rest_stdev
            if abs(z) < self.MIN_Z_SCORE:
                continue
            direction = "higher" if z > 0 else "lower"
            pid = _pattern_id("subgroup_outlier", tag_key, str(group_value), metric_key)
            found.append(IntellectualPattern(
                pattern_id=pid,
                description=(f"events where {tag_key}={group_value!r} have "
                            f"{direction} {metric_key} ({group_mean:.3g} vs "
                            f"{rest_mean:.3g} for everything else, "
                            f"z={z:.2f}, n={len(values)})"),
                kind="subgroup_outlier",
                source_observation_ids=list(group_event_ids[group_value]),
                supporting_evidence=[{"tag_key": tag_key, "group_value": group_value,
                                     "metric_key": metric_key, "group_mean": group_mean,
                                     "rest_mean": rest_mean, "z_score": z,
                                     "n": len(values)}],
                recurrence=len(values),
                novelty=min(1.0, abs(z) / 4.0),
                uncertainty=max(0.1, 1.0 - min(len(values), 10) / 10.0),
                information_gain_estimate=min(1.0, abs(z) / 3.0),
                usefulness_estimate=0.5,
                provenance={"generated_by": "SubgroupOutlierMiner",
                           "tag_key": tag_key, "metric_key": metric_key}))
        return found


class CoOccurrenceMiner:
    MIN_COUNT = 2
    MIN_LIFT = 1.5

    def mine(self, events: List[ExperienceEvent]) -> List[IntellectualPattern]:
        patterns = []
        list_tag_keys = {k for e in events for k, v in e.tags.items()
                         if isinstance(v, list)}
        for tag_key in list_tag_keys:
            patterns.extend(self._mine_one(events, tag_key))
        return patterns

    def _mine_one(self, events: List[ExperienceEvent], tag_key: str
                  ) -> List[IntellectualPattern]:
        successful = [e for e in events if e.metrics.get("success", 0) >= 1.0
                     and isinstance(e.tags.get(tag_key), list)]
        if len(successful) < self.MIN_COUNT:
            return []

        item_counts: Dict[str, int] = {}
        pair_counts: Dict[Tuple[str, str], int] = {}
        pair_event_ids: Dict[Tuple[str, str], List[str]] = {}
        for e in successful:
            items = sorted(set(e.tags[tag_key]))
            for item in items:
                item_counts[item] = item_counts.get(item, 0) + 1
            for a, b in combinations(items, 2):
                pair_counts[(a, b)] = pair_counts.get((a, b), 0) + 1
                pair_event_ids.setdefault((a, b), []).append(e.event_id)

        n = len(successful)
        found = []
        for (a, b), count in pair_counts.items():
            if count < self.MIN_COUNT:
                continue
            expected = (item_counts[a] / n) * (item_counts[b] / n) * n
            if expected <= 0:
                continue
            lift = count / expected
            if lift < self.MIN_LIFT:
                continue
            pid = _pattern_id("co_occurrence", tag_key, a, b)
            found.append(IntellectualPattern(
                pattern_id=pid,
                description=(f"{a!r} and {b!r} co-occur in successful "
                            f"{tag_key} {lift:.1f}x more than chance "
                            f"(n={count}/{n})"),
                kind="co_occurrence",
                source_observation_ids=list(pair_event_ids[(a, b)]),
                supporting_evidence=[{"tag_key": tag_key, "item_a": a, "item_b": b,
                                     "count": count, "expected": expected, "lift": lift}],
                recurrence=count,
                novelty=min(1.0, (lift - 1) / 3.0),
                uncertainty=max(0.1, 1.0 - min(count, 10) / 10.0),
                information_gain_estimate=min(1.0, (lift - 1) / 2.0),
                usefulness_estimate=0.5,
                related_capabilities=[a, b],
                provenance={"generated_by": "CoOccurrenceMiner", "tag_key": tag_key}))
        return found


class PredictedVsObservedMiner:
    MIN_GAP = 0.3

    def mine(self, events: List[ExperienceEvent],
            predicted_key: str = "predicted_pass",
            observed_key: str = "observed_success_rate") -> List[IntellectualPattern]:
        relevant = [e for e in events
                   if predicted_key in e.metrics and observed_key in e.metrics]
        if not relevant:
            return []

        found = []
        for e in relevant:
            gap = e.metrics[predicted_key] - e.metrics[observed_key]
            if abs(gap) < self.MIN_GAP:
                continue
            pid = _pattern_id("predicted_vs_observed", e.event_id)
            direction = "over-predicted" if gap > 0 else "under-predicted"
            found.append(IntellectualPattern(
                pattern_id=pid,
                description=(f"{e.kind} {e.raw_ref!r}: {direction} outcome — "
                            f"predicted {e.metrics[predicted_key]:.2f}, "
                            f"observed {e.metrics[observed_key]:.2f} "
                            f"(gap {gap:+.2f})"),
                kind="predicted_vs_observed_gap",
                source_observation_ids=[e.event_id],
                supporting_evidence=[{"event_id": e.event_id, "kind": e.kind,
                                     "predicted": e.metrics[predicted_key],
                                     "observed": e.metrics[observed_key], "gap": gap,
                                     "tags": e.tags}],
                recurrence=1,
                novelty=min(1.0, abs(gap)),
                uncertainty=0.5,
                information_gain_estimate=min(1.0, abs(gap) * 1.2),
                usefulness_estimate=0.7,
                related_capabilities=[e.raw_ref] if e.raw_ref else [],
                provenance={"generated_by": "PredictedVsObservedMiner",
                           "event_kind": e.kind}))
        return found


class PatternMiner:
    def __init__(self, store: PatternStore):
        self.store = store
        self.operators = [SubgroupOutlierMiner(), CoOccurrenceMiner(),
                          PredictedVsObservedMiner()]

    def run(self, events: List[ExperienceEvent]) -> List[IntellectualPattern]:
        discovered = []
        for operator in self.operators:
            for pattern in operator.mine(events):
                existing = self.store.get(pattern.pattern_id)
                if existing is not None:
                    existing.recurrence += 1
                    existing.log("re-observed")
                    self.store.save(existing)
                    continue
                pattern.log("discovered")
                self.store.save(pattern)
                discovered.append(pattern)
        return discovered
