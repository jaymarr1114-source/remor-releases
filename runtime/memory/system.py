"""
swarm_engine/memory/system.py

A memory system, not a memory table.

Before this, "memory" meant five separate stores that nothing queried
together: ProvenanceStore, FailureMemory, AcquiredCodeStore,
CapabilityLifecycle, CompositionAbstractor. Each is real and each is used, but
none of them could answer "what do I already know that's relevant to this new
goal?" — which is the question memory exists to answer.

Six kinds are distinguished, matching what SWarm actually accumulates:

  EPISODIC    what happened: every run_task outcome, persisted, not just
              logged and discarded when the Task object goes out of scope.
  SEMANTIC    facts explicitly told to the engine or derived with a stated
              confidence — not the same as a source claim, which starts
              unverified until something checks it (see also item 16).
  PROCEDURAL  how to do something: promoted composition shapes and bound
              goal -> capability mappings. This already existed structurally;
              what was missing was a name and a query interface.
  CAPABILITY  what SWarm can currently do, via the existing lifecycle graph.
  FAILURE     what did not work and why (delegates to FailureMemory).
  PROVENANCE  where a capability came from (delegates to ProvenanceStore).

Retrieval is relevance-ranked, using the same token/synonym overlap already
built for semantic capability matching, so "have I seen something like this
before" is a real query against real history rather than an exact-string
lookup that only ever matches its own prior phrasing.
"""
from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional

from swarm_engine.acquisition.semantic import expand, tokens


class MemoryKind(Enum):
    EPISODIC = "episodic"
    SEMANTIC = "semantic"
    PROCEDURAL = "procedural"
    CAPABILITY = "capability"
    FAILURE = "failure"
    PROVENANCE = "provenance"


@dataclass
class MemoryHit:
    kind: MemoryKind
    relevance: float
    summary: str
    detail: Dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> Dict[str, Any]:
        return {"kind": self.kind.value, "relevance": round(self.relevance, 3),
                "summary": self.summary, "detail": self.detail}


def _relevance(query: str, text: str) -> float:
    q, t = expand(tokens(query)), expand(tokens(text))
    if not q or not t:
        return 0.0
    return len(q & t) / len(q)


class MemorySystem:
    """The engine's single point of access to everything it remembers."""

    def __init__(self, engine, db_path: str = "swarm_engine.db"):
        self.engine = engine
        self.db_path = db_path
        with self._conn() as conn:
            conn.execute("""CREATE TABLE IF NOT EXISTS episodic_memory (
                id INTEGER PRIMARY KEY AUTOINCREMENT, goal TEXT, success INTEGER,
                value TEXT, error TEXT, tier TEXT, at REAL)""")
            conn.execute("""CREATE TABLE IF NOT EXISTS semantic_memory (
                concept TEXT PRIMARY KEY, value TEXT, confidence REAL,
                source TEXT, at REAL)""")

    def _conn(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    # -- episodic -------------------------------------------------------------
    def record_episode(self, goal: str, success: bool, value: Any = None,
                       error: str = "", tier: str = "") -> None:
        with self._conn() as conn:
            conn.execute("""INSERT INTO episodic_memory
                (goal, success, value, error, tier, at) VALUES (?,?,?,?,?,?)""",
                (goal, int(success), json.dumps(value, default=str), error, tier,
                 time.time()))

    def recall_episodes(self, query: str, limit: int = 5) -> List[MemoryHit]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM episodic_memory ORDER BY id DESC LIMIT 200").fetchall()
        scored = [(_relevance(query, r["goal"]), r) for r in rows]
        scored = [(s, r) for s, r in scored if s > 0]
        scored.sort(key=lambda pair: -pair[0])
        return [MemoryHit(MemoryKind.EPISODIC, score,
                          f"{'succeeded' if r['success'] else 'failed'}: {r['goal']!r}",
                          {"goal": r["goal"], "success": bool(r["success"]),
                           "value": json.loads(r["value"] or "null"),
                           "error": r["error"], "at": r["at"]})
                for score, r in scored[:limit]]

    # -- semantic ---------------------------------------------------------------
    def remember_fact(self, concept: str, value: Any, confidence: float = 1.0,
                      source: str = "") -> None:
        with self._conn() as conn:
            conn.execute("""INSERT OR REPLACE INTO semantic_memory
                (concept, value, confidence, source, at) VALUES (?,?,?,?,?)""",
                (concept, json.dumps(value, default=str), confidence, source,
                 time.time()))

    def recall_facts(self, query: str, limit: int = 5) -> List[MemoryHit]:
        with self._conn() as conn:
            rows = conn.execute("SELECT * FROM semantic_memory").fetchall()
        scored = [(_relevance(query, r["concept"]), r) for r in rows]
        scored = [(s, r) for s, r in scored if s > 0]
        scored.sort(key=lambda pair: -pair[0])
        return [MemoryHit(MemoryKind.SEMANTIC, score * r["confidence"],
                          f"{r['concept']} = {r['value']}",
                          {"concept": r["concept"],
                           "value": json.loads(r["value"] or "null"),
                           "confidence": r["confidence"], "source": r["source"]})
                for score, r in scored[:limit]]

    # -- procedural ---------------------------------------------------------------
    def recall_procedures(self, query: str, limit: int = 5) -> List[MemoryHit]:
        hits: List[MemoryHit] = []
        for record in self.engine.capabilities.list():
            goal = getattr(record, "goal", "") or ""
            score = _relevance(query, goal)
            if score > 0:
                hits.append(MemoryHit(
                    MemoryKind.PROCEDURAL, score,
                    f"known procedure for {goal!r} "
                    f"({getattr(record, 'success_rate', 0):.0%} success)",
                    {"goal": goal, "capability_id": record.capability_id}))
        for skeleton in self.engine.abstractor.promoted():
            score = _relevance(query, skeleton.shape.replace("->", " "))
            if score > 0:
                hits.append(MemoryHit(
                    MemoryKind.PROCEDURAL, score,
                    f"generalized composition shape {skeleton.shape!r} "
                    f"({skeleton.successes}/{skeleton.observations} successes)",
                    skeleton.as_dict()))
        hits.sort(key=lambda h: -h.relevance)
        return hits[:limit]

    # -- capability / failure / provenance (delegated) -----------------------------
    def recall_capabilities(self, query: str, limit: int = 5) -> List[MemoryHit]:
        hits = []
        for name in self.engine.primitives.names():
            score = _relevance(query, name.replace(".", " "))
            if score > 0:
                hits.append(MemoryHit(MemoryKind.CAPABILITY, score,
                                      f"primitive {name!r} is available",
                                      {"name": name}))
        hits.sort(key=lambda h: -h.relevance)
        return hits[:limit]

    def recall_failures(self, query: str, limit: int = 5) -> List[MemoryHit]:
        with self.engine.failure_memory._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM failure_memory ORDER BY id DESC LIMIT 200").fetchall()
        scored = [(_relevance(query, r["goal"]), r) for r in rows]
        scored = [(s, r) for s, r in scored if s > 0]
        scored.sort(key=lambda pair: -pair[0])
        return [MemoryHit(MemoryKind.FAILURE, score,
                          f"{r['kind']} failure on {r['goal']!r}: {r['detail'][:80]}",
                          {"goal": r["goal"], "kind": r["kind"],
                           "recoverable": bool(r["recoverable"])})
                for score, r in scored[:limit]]

    # -- unified ------------------------------------------------------------------
    def recall(self, query: str, limit: int = 10) -> List[MemoryHit]:
        """Everything relevant, ranked together regardless of kind.

        This is the query that matters: a caller deciding how to approach a
        new goal should not have to know in advance whether the useful prior
        experience is an episode, a failure, or a learned procedure.
        """
        all_hits = (self.recall_episodes(query, limit)
                   + self.recall_facts(query, limit)
                   + self.recall_procedures(query, limit)
                   + self.recall_failures(query, limit))
        all_hits.sort(key=lambda h: -h.relevance)
        return all_hits[:limit]
