"""
swarm_engine/intellect/epistemic.py

The epistemic layer: persisted first-class objects for what SWarm has
observed, what it's investigating, what it believes and why, and what
evidence backs or contradicts each belief.

Deliberately separate from CaseMemory/ConceptGraph. Those record VERIFIED
compositions — settled facts about primitive composition. This layer records
BELIEFS UNDER INVESTIGATION — claims that might be wrong, hypotheses that
compete with each other, evidence that can point either way. Conflating the
two would mean either polluting settled, verified knowledge with
speculation, or losing the "why do I believe this" trail that makes a claim
different from a fact.

Every object here answers "why" by construction: a Hypothesis carries
supporting_evidence and contradicting_evidence as lists of Evidence IDs, not
a bare confidence number — the number is derivable from the evidence, the
evidence is what's actually retained.
"""
from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional
from uuid import uuid4


class HypothesisState(Enum):
    PROPOSED = "proposed"
    UNDER_TEST = "under_test"
    SUPPORTED = "supported"
    REFUTED = "refuted"
    RETIRED = "retired"


class QuestionState(Enum):
    OPEN = "open"
    INVESTIGATING = "investigating"
    ANSWERED = "answered"
    ABANDONED = "abandoned"


@dataclass
class Observation:
    observation_id: str
    content: str
    source: str
    raw: Dict[str, Any] = field(default_factory=dict)
    at: float = field(default_factory=time.time)

    def as_dict(self) -> Dict[str, Any]:
        return {"observation_id": self.observation_id, "content": self.content,
                "source": self.source, "raw": self.raw, "at": self.at}


@dataclass
class Evidence:
    evidence_id: str
    target_id: str
    supports: bool
    content: Dict[str, Any]
    source: str
    at: float = field(default_factory=time.time)

    def as_dict(self) -> Dict[str, Any]:
        return {"evidence_id": self.evidence_id, "target_id": self.target_id,
                "supports": self.supports, "content": self.content,
                "source": self.source, "at": self.at}


@dataclass
class Hypothesis:
    hypothesis_id: str
    question_id: str
    statement: str
    specification: Dict[str, Any] = field(default_factory=dict)
    supporting_evidence: List[str] = field(default_factory=list)
    contradicting_evidence: List[str] = field(default_factory=list)
    confidence: float = 0.5
    competing_with: List[str] = field(default_factory=list)
    provenance: Dict[str, Any] = field(default_factory=dict)
    state: HypothesisState = HypothesisState.PROPOSED
    created_at: float = field(default_factory=time.time)

    def as_dict(self) -> Dict[str, Any]:
        return {"hypothesis_id": self.hypothesis_id, "question_id": self.question_id,
                "statement": self.statement, "specification": self.specification,
                "supporting_evidence": self.supporting_evidence,
                "contradicting_evidence": self.contradicting_evidence,
                "confidence": self.confidence, "competing_with": self.competing_with,
                "provenance": self.provenance, "state": self.state.value,
                "created_at": self.created_at}

    @staticmethod
    def from_dict(d: Dict[str, Any]) -> "Hypothesis":
        d = dict(d)
        d["state"] = HypothesisState(d["state"])
        return Hypothesis(**d)


@dataclass
class Experiment:
    experiment_id: str
    question_id: str
    hypothesis_ids: List[str]
    design: Dict[str, Any]
    executed: bool = False
    result: Optional[Dict[str, Any]] = None
    at: float = field(default_factory=time.time)

    def as_dict(self) -> Dict[str, Any]:
        return {"experiment_id": self.experiment_id, "question_id": self.question_id,
                "hypothesis_ids": self.hypothesis_ids, "design": self.design,
                "executed": self.executed, "result": self.result, "at": self.at}

    @staticmethod
    def from_dict(d: Dict[str, Any]) -> "Experiment":
        return Experiment(**d)


class EpistemicStore:
    def __init__(self, db_path: str = "swarm_engine.db"):
        self.db_path = db_path
        with self._conn() as conn:
            conn.execute("""CREATE TABLE IF NOT EXISTS observations (
                observation_id TEXT PRIMARY KEY, data TEXT NOT NULL, at REAL)""")
            conn.execute("""CREATE TABLE IF NOT EXISTS evidence (
                evidence_id TEXT PRIMARY KEY, target_id TEXT, data TEXT NOT NULL, at REAL)""")
            conn.execute("""CREATE TABLE IF NOT EXISTS hypotheses (
                hypothesis_id TEXT PRIMARY KEY, question_id TEXT,
                data TEXT NOT NULL, updated_at REAL)""")
            conn.execute("""CREATE TABLE IF NOT EXISTS experiments (
                experiment_id TEXT PRIMARY KEY, question_id TEXT,
                data TEXT NOT NULL, updated_at REAL)""")

    def _conn(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def save_observation(self, obs: Observation) -> Observation:
        with self._conn() as conn:
            conn.execute("""INSERT OR REPLACE INTO observations
                (observation_id, data, at) VALUES (?,?,?)""",
                (obs.observation_id, json.dumps(obs.as_dict()), obs.at))
        return obs

    def record_observation(self, content: str, source: str,
                           raw: Optional[Dict[str, Any]] = None) -> Observation:
        """Record an observation in one call: builds an Observation with a
        unique id, persists it via save_observation, and returns it."""
        obs = Observation(
            observation_id=f"obs_{uuid4().hex[:12]}",
            content=content,
            source=source,
            raw=raw if raw is not None else {},
        )
        return self.save_observation(obs)

    def all_observations(self) -> List[Observation]:
        with self._conn() as conn:
            rows = conn.execute("SELECT data FROM observations ORDER BY at").fetchall()
        return [Observation(**json.loads(r["data"])) for r in rows]

    def save_evidence(self, ev: Evidence) -> Evidence:
        with self._conn() as conn:
            conn.execute("""INSERT OR REPLACE INTO evidence
                (evidence_id, target_id, data, at) VALUES (?,?,?,?)""",
                (ev.evidence_id, ev.target_id, json.dumps(ev.as_dict()), ev.at))
        return ev

    def evidence_for(self, target_id: str) -> List[Evidence]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT data FROM evidence WHERE target_id=? ORDER BY at",
                (target_id,)).fetchall()
        return [Evidence(**json.loads(r["data"])) for r in rows]

    def save_hypothesis(self, hyp: Hypothesis) -> Hypothesis:
        with self._conn() as conn:
            conn.execute("""INSERT OR REPLACE INTO hypotheses
                (hypothesis_id, question_id, data, updated_at) VALUES (?,?,?,?)""",
                (hyp.hypothesis_id, hyp.question_id, json.dumps(hyp.as_dict()),
                 time.time()))
        return hyp

    def record_hypothesis(self, question_id: str, statement: str,
                          specification: Optional[Dict[str, Any]] = None,
                          provenance: Optional[Dict[str, Any]] = None) -> Hypothesis:
        """Record a hypothesis in one call: builds a Hypothesis (state PROPOSED)
        with a unique id, persists it via save_hypothesis, and returns it."""
        hyp = Hypothesis(
            hypothesis_id=f"hyp_{uuid4().hex[:12]}",
            question_id=question_id,
            statement=statement,
            specification=specification if specification is not None else {},
            provenance=provenance if provenance is not None else {},
            state=HypothesisState.PROPOSED,
        )
        return self.save_hypothesis(hyp)

    def all_hypotheses(self) -> List[Hypothesis]:
        with self._conn() as conn:
            rows = conn.execute("SELECT data FROM hypotheses").fetchall()
        return [Hypothesis.from_dict(json.loads(r["data"])) for r in rows]

    def get_hypothesis(self, hypothesis_id: str) -> Optional[Hypothesis]:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT data FROM hypotheses WHERE hypothesis_id=?",
                (hypothesis_id,)).fetchone()
        return Hypothesis.from_dict(json.loads(row["data"])) if row else None

    def hypotheses_for(self, question_id: str) -> List[Hypothesis]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT data FROM hypotheses WHERE question_id=? ORDER BY updated_at",
                (question_id,)).fetchall()
        return [Hypothesis.from_dict(json.loads(r["data"])) for r in rows]

    def save_experiment(self, exp: Experiment) -> Experiment:
        with self._conn() as conn:
            conn.execute("""INSERT OR REPLACE INTO experiments
                (experiment_id, question_id, data, updated_at) VALUES (?,?,?,?)""",
                (exp.experiment_id, exp.question_id, json.dumps(exp.as_dict()),
                 time.time()))
        return exp

    def experiments_for(self, question_id: str) -> List[Experiment]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT data FROM experiments WHERE question_id=? ORDER BY updated_at",
                (question_id,)).fetchall()
        return [Experiment.from_dict(json.loads(r["data"])) for r in rows]
