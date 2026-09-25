"""
swarm_engine/intellect/agenda.py

The persistent IntellectualAgenda: what SWarm could investigate next, each
item scored on priority, novelty, expected information gain, usefulness, and
uncertainty, with provenance and a history of what's happened to it.

Priority weights are themselves persisted data (`AgendaWeights`), not a
constant in code — this is what "eligible for future self-improvement
through the existing improvement machinery" (item 6) means concretely: a new
ImprovementObserver targeting "intellect.agenda_weights" could tune these
from evidence of which past investigations were actually valuable, using the
exact same Improvement/validate/admit/activate/rollback machinery already
built for the search policy. That observer is not built in this pass — the
weights are structured as a first-class, persisted, independently-swappable
object so building it later doesn't require touching this file.
"""
from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from swarm_engine.intellect.epistemic import QuestionState


@dataclass
class AgendaWeights:
    information_gain: float = 0.35
    uncertainty: float = 0.25
    usefulness: float = 0.25
    novelty: float = 0.15

    def as_dict(self) -> Dict[str, float]:
        return {"information_gain": self.information_gain,
                "uncertainty": self.uncertainty, "usefulness": self.usefulness,
                "novelty": self.novelty}


@dataclass
class Question:
    question_id: str
    text: str
    origin: str
    priority: float = 0.0
    novelty: float = 0.5
    expected_information_gain: float = 0.5
    usefulness: float = 0.5
    uncertainty: float = 0.5
    provenance: Dict[str, Any] = field(default_factory=dict)
    status: QuestionState = QuestionState.OPEN
    history: List[Dict[str, Any]] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)

    def as_dict(self) -> Dict[str, Any]:
        return {"question_id": self.question_id, "text": self.text,
                "origin": self.origin, "priority": self.priority,
                "novelty": self.novelty,
                "expected_information_gain": self.expected_information_gain,
                "usefulness": self.usefulness, "uncertainty": self.uncertainty,
                "provenance": self.provenance, "status": self.status.value,
                "history": self.history, "created_at": self.created_at}

    @staticmethod
    def from_dict(d: Dict[str, Any]) -> "Question":
        d = dict(d)
        d["status"] = QuestionState(d["status"])
        return Question(**d)

    def log(self, event: str) -> None:
        self.history.append({"event": event, "at": time.time()})


class IntellectualAgenda:
    def __init__(self, db_path: str = "swarm_engine.db"):
        self.db_path = db_path
        with self._conn() as conn:
            conn.execute("""CREATE TABLE IF NOT EXISTS agenda_questions (
                question_id TEXT PRIMARY KEY, status TEXT, data TEXT NOT NULL,
                updated_at REAL)""")
            conn.execute("""CREATE TABLE IF NOT EXISTS agenda_weights (
                id INTEGER PRIMARY KEY CHECK (id = 1), data TEXT NOT NULL)""")

    def _conn(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def weights(self) -> AgendaWeights:
        with self._conn() as conn:
            row = conn.execute("SELECT data FROM agenda_weights WHERE id=1").fetchone()
        if row is None:
            return AgendaWeights()
        return AgendaWeights(**json.loads(row["data"]))

    def set_weights(self, weights: AgendaWeights) -> None:
        with self._conn() as conn:
            conn.execute("""INSERT OR REPLACE INTO agenda_weights (id, data)
                VALUES (1, ?)""", (json.dumps(weights.as_dict()),))

    def score(self, question: Question, weights: Optional[AgendaWeights] = None) -> float:
        w = weights or self.weights()
        return (w.information_gain * question.expected_information_gain +
                w.uncertainty * question.uncertainty +
                w.usefulness * question.usefulness +
                w.novelty * question.novelty)

    def add(self, question: Question) -> Question:
        question.priority = self.score(question)
        with self._conn() as conn:
            conn.execute("""INSERT OR REPLACE INTO agenda_questions
                (question_id, status, data, updated_at) VALUES (?,?,?,?)""",
                (question.question_id, question.status.value,
                 json.dumps(question.as_dict()), time.time()))
        return question

    def get(self, question_id: str) -> Optional[Question]:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT data FROM agenda_questions WHERE question_id=?",
                (question_id,)).fetchone()
        return Question.from_dict(json.loads(row["data"])) if row else None

    def update(self, question: Question) -> Question:
        question.priority = self.score(question)
        with self._conn() as conn:
            conn.execute("""INSERT OR REPLACE INTO agenda_questions
                (question_id, status, data, updated_at) VALUES (?,?,?,?)""",
                (question.question_id, question.status.value,
                 json.dumps(question.as_dict()), time.time()))
        return question

    def open_questions(self) -> List[Question]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT data FROM agenda_questions WHERE status IN (?,?)",
                (QuestionState.OPEN.value, QuestionState.INVESTIGATING.value)).fetchall()
        return [Question.from_dict(json.loads(r["data"])) for r in rows]

    def next(self) -> Optional[Question]:
        open_qs = [q for q in self.open_questions() if q.status is QuestionState.OPEN]
        if not open_qs:
            return None
        weights = self.weights()
        return max(open_qs, key=lambda q: self.score(q, weights))

    def all(self) -> List[Question]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT data FROM agenda_questions ORDER BY updated_at").fetchall()
        return [Question.from_dict(json.loads(r["data"])) for r in rows]
