"""
swarm_engine/longhorizon/cycle_loop.py

Long-horizon autonomous operation as an emergent cycle loop.

The driver implements ONE fixed meta-skeleton per cycle:

    assess state -> detect gaps -> generate task candidates ->
    score/select -> acquire-or-recover -> verify (held-out) ->
    learn (adapt selection weights) -> persist -> (fresh process) -> reassess

Everything else EMERGES from state: which task is attempted in which cycle,
in what order, which tasks fail, which recoveries fire, and when the
objective is complete. No task sequence, no per-challenge recipe, and no
expected capability is supplied anywhere in this module. The module never
names a challenge; challenges arrive as opaque {id, goal, examples} records
from the environment, and gap detection is purely "does the current
capability store resolve this goal exactly on its training examples".

State crosses cycles ONLY through the engine's own persistence machinery
(the capability DB plus the lh_* tables created here). A fresh OS process
rehydrates everything from the DB; nothing is carried in memory.

Failure handling is classified, never canned:
    EVIDENCE-BOUNDED  -> ask the environment for more examples, retry later
    SEARCH-BOUNDED     -> retry later (learner priors may reorder), then defer
    RESOURCE-BOUNDED    -> record the measured boundary, defer
    REPRESENTATION-BOUNDED -> record, defer
Recovery is demonstrated, not assumed: a retried task must pass the
held-out gate to count as recovered.

Self-improvement: the task-selection utility weights adapt from measured
task outcomes (multiplicative update on the dominant factor, renormalized).
Weight trajectories are persisted per cycle so before/after behavior can be
compared honestly.
"""
from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

# ---------------------------------------------------------------------------
# Persistence: cycle state lives in the engine DB, not in the harness.
# ---------------------------------------------------------------------------

_SCHEMA = """
CREATE TABLE IF NOT EXISTS lh_objective (
    objective_id TEXT PRIMARY KEY,
    objective_text TEXT,
    created_at REAL,
    status TEXT,
    weights_json TEXT
);
CREATE TABLE IF NOT EXISTS lh_challenge (
    objective_id TEXT NOT NULL,
    challenge_id TEXT NOT NULL,
    goal TEXT,
    train_json TEXT,
    heldout_json TEXT,
    extra_json TEXT,
    attempts INTEGER DEFAULT 0,
    last_attempt_cycle INTEGER DEFAULT -1,
    train_at_last_attempt INTEGER DEFAULT 0,
    last_classification TEXT,
    resolved INTEGER DEFAULT 0,
    resolved_cycle INTEGER DEFAULT -1,
    capability_id TEXT,
    deferred INTEGER DEFAULT 0,
    defer_reason TEXT,
    heldout_failed INTEGER DEFAULT 0,
    PRIMARY KEY (objective_id, challenge_id)
);
CREATE TABLE IF NOT EXISTS lh_cycle (
    objective_id TEXT NOT NULL,
    cycle_n INTEGER NOT NULL,
    pid INTEGER,
    policy_version INTEGER,
    started_at REAL,
    ended_at REAL,
    record_json TEXT,
    PRIMARY KEY (objective_id, cycle_n)
);
CREATE TABLE IF NOT EXISTS lh_causal (
    objective_id TEXT NOT NULL,
    cycle_n INTEGER NOT NULL,
    kind TEXT,
    from_ref TEXT,
    to_ref TEXT,
    detail TEXT
);
"""


class CycleStore:
    """DB-backed state for the long-horizon loop. Same sqlite file as the
    engine's capability store: one persistence substrate."""

    def __init__(self, db_path: str):
        self.db_path = db_path
        with self._conn() as conn:
            conn.executescript(_SCHEMA)
            # Migration for DBs created before heldout_failed existed.
            try:
                conn.execute("ALTER TABLE lh_challenge "
                             "ADD COLUMN heldout_failed INTEGER DEFAULT 0")
            except Exception:
                pass  # already migrated

    def _conn(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    # -- objectives ------------------------------------------------------
    def register_objective(self, objective_id: str, objective_text: str,
                           weights: Dict[str, float]) -> Dict[str, Any]:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT weights_json, status FROM lh_objective WHERE objective_id=?",
                (objective_id,)).fetchone()
            if row is None:
                conn.execute(
                    "INSERT INTO lh_objective VALUES (?,?,?,?,?)",
                    (objective_id, objective_text, time.time(), "in_progress",
                     json.dumps(weights)))
                return {"weights": dict(weights), "status": "in_progress",
                        "new": True}
            return {"weights": json.loads(row["weights_json"]),
                    "status": row["status"], "new": False}

    def save_weights(self, objective_id: str, weights: Dict[str, float]) -> None:
        with self._conn() as conn:
            conn.execute("UPDATE lh_objective SET weights_json=? WHERE objective_id=?",
                         (json.dumps(weights), objective_id))

    def set_status(self, objective_id: str, status: str) -> None:
        with self._conn() as conn:
            conn.execute("UPDATE lh_objective SET status=? WHERE objective_id=?",
                         (status, objective_id))

    # -- challenges ------------------------------------------------------
    def sync_challenges(self, objective_id: str,
                        challenges: Sequence[Dict[str, Any]]) -> None:
        """First-cycle registration. Mutable fields (train top-ups, attempts,
        resolved flags) live here afterwards; the env only supplies the
        initial definitions and extra-example pools."""
        with self._conn() as conn:
            for ch in challenges:
                row = conn.execute(
                    "SELECT challenge_id FROM lh_challenge WHERE objective_id=? AND challenge_id=?",
                    (objective_id, ch["id"])).fetchone()
                if row is None:
                    conn.execute(
                        """INSERT INTO lh_challenge
                           (objective_id, challenge_id, goal, train_json,
                            heldout_json, extra_json)
                           VALUES (?,?,?,?,?,?)""",
                        (objective_id, ch["id"], ch["goal"],
                         json.dumps(ch["train"]), json.dumps(ch["heldout"]),
                         json.dumps(ch.get("extra", []))))

    def load_challenges(self, objective_id: str) -> List[Dict[str, Any]]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM lh_challenge WHERE objective_id=? ORDER BY challenge_id",
                (objective_id,)).fetchall()
        out = []
        for r in rows:
            keys = r.keys()
            out.append({
                "id": r["challenge_id"], "goal": r["goal"],
                "train": json.loads(r["train_json"]),
                "heldout": json.loads(r["heldout_json"]),
                "extra": json.loads(r["extra_json"]),
                "attempts": r["attempts"],
                "last_attempt_cycle": r["last_attempt_cycle"],
                "train_at_last_attempt": r["train_at_last_attempt"],
                "last_classification": r["last_classification"],
                "resolved": bool(r["resolved"]),
                "resolved_cycle": r["resolved_cycle"],
                "capability_id": r["capability_id"],
                "deferred": bool(r["deferred"]),
                "defer_reason": r["defer_reason"],
                "heldout_failed": bool(r["heldout_failed"])
                if "heldout_failed" in keys else False,
            })
        return out

    def update_challenge(self, objective_id: str, challenge_id: str,
                         **fields) -> None:
        colmap = {"train": "train_json", "heldout": "heldout_json",
                  "extra": "extra_json"}
        with self._conn() as conn:
            for k, v in fields.items():
                if k in colmap:
                    v = json.dumps(v)
                    k = colmap[k]
                conn.execute(
                    f"UPDATE lh_challenge SET {k}=? WHERE objective_id=? AND challenge_id=?",
                    (v, objective_id, challenge_id))

    # -- cycles / causal --------------------------------------------------
    def next_cycle_n(self, objective_id: str) -> int:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT MAX(cycle_n) AS m FROM lh_cycle WHERE objective_id=?",
                (objective_id,)).fetchone()
        return (row["m"] + 1) if row["m"] is not None else 0

    def save_cycle(self, objective_id: str, cycle_n: int, pid: int,
                   policy_version: int, started_at: float,
                   record: Dict[str, Any]) -> None:
        with self._conn() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO lh_cycle VALUES (?,?,?,?,?,?,?)",
                (objective_id, cycle_n, pid, policy_version, started_at,
                 time.time(), json.dumps(record, default=str)))

    def add_causal(self, objective_id: str, cycle_n: int, kind: str,
                   from_ref: str, to_ref: str, detail: str = "") -> None:
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO lh_causal VALUES (?,?,?,?,?,?)",
                (objective_id, cycle_n, kind, from_ref, to_ref, detail))

    def causal_links(self, objective_id: str) -> List[Dict[str, Any]]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM lh_causal WHERE objective_id=? ORDER BY cycle_n",
                (objective_id,)).fetchall()
        return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Environment interface (implemented by the harness, never by this module).
# ---------------------------------------------------------------------------

class ChallengeEnvironment:
    """The world the loop operates in. Supplies opaque challenges and, on
    request, more evidence. Never supplies solutions, recipes, or order."""

    def challenges(self) -> Sequence[Dict[str, Any]]:
        raise NotImplementedError

    def request_more_examples(self, challenge_id: str) -> List[Tuple[Dict, Any]]:
        """Environment affordance: return further training examples, or []."""
        return []


# ---------------------------------------------------------------------------
# The cycle loop.
# ---------------------------------------------------------------------------

@dataclass
class TaskCandidate:
    kind: str                      # "acquire" | "closure_audit" | "investigate" | "synthesize"
    challenge_id: Optional[str]
    goal: Optional[str]
    factors: Dict[str, float] = field(default_factory=dict)
    utility: float = 0.0
    detail: str = ""
    payload: Dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> Dict[str, Any]:
        d = {"kind": self.kind, "challenge_id": self.challenge_id,
             "goal": self.goal, "factors": self.factors,
             "utility": round(self.utility, 4), "detail": self.detail}
        if self.payload:
            d["payload"] = {k: self.payload[k] for k in self.payload
                            if k != "examples"}
            if "examples" in self.payload:
                d["payload"]["n_examples"] = len(self.payload["examples"] or [])
        return d


