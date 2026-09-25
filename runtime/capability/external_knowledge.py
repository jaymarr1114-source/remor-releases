"""
swarm_engine/capability/external_knowledge.py

ExternalKnowledgeSource: the same honest boundary pattern as
intellect/reasoner.py's ExternalReasoner, applied to research rather than
hypothesis generation.

The architectural reality this is built around: SWarm's own sandboxed
execution process has no network egress (verified directly this session —
even apt package mirrors return 403 under the sandbox's egress allowlist).
It cannot itself fetch documentation. What CAN provide real external
knowledge is whatever is orchestrating SWarm — in this session, the agent
running these tools, which has real web_search/web_fetch access.

This is not a workaround. It is the correct place to draw the boundary:
network access is a governed external resource, supplied by the environment
SWarm runs inside. SWarm's architecture defines the interface; whatever
process is actually running it decides how (or whether) to fulfill it.

NoExternalKnowledgeSource is the honest default — explicit empty results,
not a fabricated answer. RetrievedKnowledge always carries provenance,
because content with no traceable origin is not meaningfully different from
a fabricated answer.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class RetrievedKnowledge:
    query: str
    content: str
    source: str
    retrieved_at: Optional[float] = None
    metadata: Dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> Dict[str, Any]:
        return {"query": self.query, "content": self.content,
                "source": self.source, "retrieved_at": self.retrieved_at,
                "metadata": self.metadata}


class ExternalKnowledgeSource(ABC):
    # Shared invariant for every concrete source that matches a query
    # against a bank of previously-answered/injected queries. Centralized
    # here — not left for each subclass to reimplement — because leaving it
    # per-subclass is exactly what caused a real bug: QueuedKnowledgeSource
    # was tightened from 0.5 to 0.85 after a cross-contamination failure
    # (two genuinely different requirements sharing domain vocabulary
    # matched each other's cached answers), but InjectedKnowledgeSource,
    # which implements the identical concept with its own copy of the
    # matching logic, was never touched and stayed at the old, exploitable
    # threshold. A shared method makes that class of silent divergence
    # structurally impossible: any subclass reusing `_best_match` inherits
    # the current threshold automatically, and any subclass that wants a
    # different one has to override this method explicitly — a visible,
    # deliberate decision, not a copy-paste that quietly drifts.
    MATCH_THRESHOLD = 0.85

    @staticmethod
    def _best_match(query: str, keyed_candidates: Dict[str, Any]
                    ) -> Optional[Any]:
        """Overlap normalized by the SMALLER of the two word sets, at a
        high threshold — not the larger set, despite that being what
        QueuedKnowledgeSource used right after the cross-contamination fix
        that motivated this shared method. Measured directly against real
        cases before settling here: a legitimate match is often a short,
        precise key fully CONTAINED within a much longer, naturally-phrased
        autonomous query (e.g. a 4-word GDScript hint inside a 13-word
        formulated query) — normalizing by the larger set drives that
        overlap toward ~0.3 and would have rejected it outright, even
        though every word of the key is present. Genuine cross-
        contamination (two different requirements sharing only domain
        vocabulary) instead shows up as roughly HALF the smaller set's
        words overlapping, not all of them. Smaller-set normalization
        with a high threshold (0.85) correctly separates both: full
        containment passes, partial vocabulary overlap does not."""
        query_words = set(query.lower().split())
        best_key, best_score = None, 0.0
        for key in keyed_candidates:
            key_words = set(key.lower().split())
            if not key_words or not query_words:
                continue
            overlap = len(query_words & key_words) / len(
                min(query_words, key_words, key=len))
            if overlap > best_score:
                best_key, best_score = key, overlap
        if best_key is not None and best_score >= ExternalKnowledgeSource.MATCH_THRESHOLD:
            return keyed_candidates[best_key]
        return None

    @abstractmethod
    def research(self, query: str, context: Dict[str, Any]
                ) -> List[RetrievedKnowledge]:
        ...


class NoExternalKnowledgeSource(ExternalKnowledgeSource):
    def research(self, query: str, context: Dict[str, Any]
                ) -> List[RetrievedKnowledge]:
        return []


class InjectedKnowledgeSource(ExternalKnowledgeSource):
    """A concrete source backed by knowledge the orchestrating agent
    actually retrieved (real web_search/web_fetch results), supplied ahead
    of time and matched by query. The content was genuinely fetched from
    the real internet by the process running SWarm; it is injected here
    because SWarm's own process cannot make the HTTP call itself. Each
    entry's `source` is the real URL/title the content came from."""

    def __init__(self):
        self._bank: Dict[str, List[RetrievedKnowledge]] = {}

    def add(self, query_key: str, knowledge: List[RetrievedKnowledge]) -> None:
        self._bank[query_key] = knowledge

    def research(self, query: str, context: Dict[str, Any]
                ) -> List[RetrievedKnowledge]:
        match = self._best_match(query, self._bank)
        return match if match is not None else []


class QueuedKnowledgeSource(ExternalKnowledgeSource):
    """The autonomous version. `research()` is called BY SWarm's own growth
    machinery with a query SWarm itself formulated — never a human. If
    nothing is already known for that query, this does not fabricate an
    answer or silently block: it records the exact request (persisted, so
    it survives restart) and returns empty, honestly reporting "not yet
    available" rather than pretending no need exists.

    Fulfillment happens out-of-band: whatever is orchestrating SWarm calls
    `pending()` to see EXACTLY what SWarm asked for — not what a human
    guesses SWarm might want — performs the real fetch for that exact
    query, and calls `fulfill()`. This is the honest version of the
    network-egress boundary: SWarm decides what and when, the environment
    supplies the one thing it structurally cannot (raw network I/O).
    """

    def __init__(self, db_path: str = "swarm_engine.db"):
        self.db_path = db_path
        with self._conn() as conn:
            conn.execute("""CREATE TABLE IF NOT EXISTS knowledge_requests (
                request_id TEXT PRIMARY KEY, query TEXT, context TEXT,
                status TEXT, created_at REAL, fulfilled_at REAL)""")
            conn.execute("""CREATE TABLE IF NOT EXISTS knowledge_answers (
                request_id TEXT PRIMARY KEY, query TEXT, data TEXT NOT NULL,
                fulfilled_at REAL)""")

    def _conn(self):
        import sqlite3
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def research(self, query: str, context: Dict[str, Any]
                ) -> List[RetrievedKnowledge]:
        import hashlib
        import json
        import time as _time
        cached = self._find_answer(query)
        if cached:
            return cached

        request_id = "req_" + hashlib.sha256(query.encode()).hexdigest()[:16]
        with self._conn() as conn:
            existing = conn.execute(
                "SELECT status FROM knowledge_requests WHERE request_id=?",
                (request_id,)).fetchone()
            if existing is None:
                conn.execute("""INSERT INTO knowledge_requests
                    (request_id, query, context, status, created_at, fulfilled_at)
                    VALUES (?,?,?,?,?,NULL)""",
                    (request_id, query, json.dumps(context), "pending", _time.time()))
        return []

    def _find_answer(self, query: str) -> List[RetrievedKnowledge]:
        import json
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT query, data FROM knowledge_answers").fetchall()
        by_query = {r["query"]: r["data"] for r in rows}
        match = self._best_match(query, by_query)
        if match is not None:
            return [RetrievedKnowledge(**d) for d in json.loads(match)]
        return []

    def pending(self) -> List[Dict[str, Any]]:
        """What SWarm has actually asked for and not yet received —
        exactly this, nothing inferred or added."""
        import json
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM knowledge_requests WHERE status='pending' "
                "ORDER BY created_at").fetchall()
        return [{"request_id": r["request_id"], "query": r["query"],
                "context": json.loads(r["context"]), "created_at": r["created_at"]}
               for r in rows]

    def fulfill(self, request_id: str, knowledge: List[RetrievedKnowledge]) -> None:
        import json
        import time as _time
        with self._conn() as conn:
            row = conn.execute(
                "SELECT query FROM knowledge_requests WHERE request_id=?",
                (request_id,)).fetchone()
            if row is None:
                return
            conn.execute("""INSERT OR REPLACE INTO knowledge_answers
                (request_id, query, data, fulfilled_at) VALUES (?,?,?,?)""",
                (request_id, row["query"],
                 json.dumps([k.as_dict() for k in knowledge]), _time.time()))
            conn.execute("""UPDATE knowledge_requests SET status='fulfilled',
                fulfilled_at=? WHERE request_id=?""", (_time.time(), request_id))

    def fulfilled_count(self) -> int:
        """How many answers exist right now — used to detect whether any
        NEW information arrived between two pursuit cycles, which is the
        only thing that could make a repeated attempt legitimately
        different. Not a retry counter; a fact about the world."""
        with self._conn() as conn:
            row = conn.execute("SELECT COUNT(*) AS c FROM knowledge_answers").fetchone()
        return row["c"]

    def invalidate(self, query: str) -> bool:
        """Used for escalation: a cached answer that led to a system-
        validation failure should not keep being silently reused —
        removing it forces the next research() call for a related query to
        genuinely re-request, rather than replay the same wrong content."""
        import json
        with self._conn() as conn:
            rows = conn.execute("SELECT request_id, query FROM knowledge_answers").fetchall()
        match = self._best_match(query, {r["query"]: r["request_id"] for r in rows})
        if match is None:
            return False
        with self._conn() as conn:
            conn.execute("DELETE FROM knowledge_answers WHERE request_id=?", (match,))
            conn.execute("UPDATE knowledge_requests SET status='pending', "
                        "fulfilled_at=NULL WHERE request_id=?", (match,))
        return True
