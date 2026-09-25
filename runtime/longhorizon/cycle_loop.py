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

import asyncio
import json
import os
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


class LongHorizonCycleLoop:
    """One emergent cycle per run_cycle() call. Cross-cycle state is only
    the DB."""

    FACTORS = ("evidence", "novelty", "urgency", "cost")

    def __init__(self, engine, objective_id: str, objective_text: str,
                 env: ChallengeEnvironment,
                 tasks_per_cycle: int = 2,
                 resolve_timeout_s: float = 240.0,
                 max_attempts: int = 3,
                 max_cycles: int = 8):
        self.engine = engine
        self.objective_id = objective_id
        self.objective_text = objective_text
        self.env = env
        self.tasks_per_cycle = tasks_per_cycle
        self.resolve_timeout_s = resolve_timeout_s
        self.max_attempts = max_attempts
        self.max_cycles = max_cycles
        self.store = CycleStore(getattr(engine.capabilities, "db_path",
                                        "swarm_engine.db"))
        init_weights = {f: 0.25 for f in self.FACTORS}
        reg = self.store.register_objective(objective_id, objective_text,
                                            init_weights)
        self.weights = reg["weights"]
        self._prior_wall: List[float] = []  # in-cycle cost estimates

    # -- state assessment -------------------------------------------------
    def assess(self) -> Dict[str, Any]:
        plan_caps = list(self.engine.capabilities.list(limit=100000))
        code_caps = [r for r in self.engine.acquired_code.all()
                     if r["status"] == "active"]
        bindings = self.engine.capabilities.goal_bindings()
        return {
            "n_capabilities": len(plan_caps) + len(code_caps),
            "n_plan_capabilities": len(plan_caps),
            "n_acquired_code": len(code_caps),
            "n_goal_bindings": len(bindings),
            "weights": dict(self.weights),
        }

    @staticmethod
    def _norm_num(v):
        if isinstance(v, float) and v.is_integer():
            return int(v)
        return v

    def _find_capability(self, goal: str) -> Tuple[Optional[str], Optional[str]]:
        """Locate the live capability serving `goal` through the REAL stores.

        Returns (kind, identifier): kind "plan" -> plan_capabilities id
        resolvable via resolve_goal; kind "code" -> acquired_code name whose
        spec description matches and whose primitive is registered.
        """
        cap = self.engine.capabilities.resolve_goal(goal)
        if cap is not None and getattr(cap, "plan", None):
            return "plan", cap.capability_id
        for rec in self.engine.acquired_code.all():
            if rec["status"] == "active" and \
                    (rec["spec"] or {}).get("description") == goal:
                if self.engine.primitives.get(rec["name"]) is not None:
                    return "code", rec["name"]
        return None, None

    def _exec_capability_exact(self, kind: str, ident: str,
                               examples: Sequence[Tuple[Dict, Any]]
                               ) -> Tuple[bool, str]:
        """Execute a stored capability on examples; require exact match."""
        if kind == "plan":
            from swarm_engine.synthesis.composer import Composer
            cap = self.engine.capabilities.get(ident)
            if cap is None or not getattr(cap, "plan", None):
                return False, "plan capability missing"
            comp = Composer(self.engine.primitives)
            for inp, want in examples:
                try:
                    run = comp.execute_sync(plan=cap.plan, args=dict(inp),
                                            skip_check=True)
                except Exception as exc:  # noqa: BLE001 - probe, don't crash
                    return False, f"exec raised {type(exc).__name__}"
                if not run.get("success"):
                    return False, f"exec failed: {str(run.get('error'))[:120]}"
                if self._norm_num(run.get("value")) != self._norm_num(want):
                    return False, f"value mismatch on {inp}"
            return True, "exact on examples"
        # kind == "code": call the registered acquired primitive directly
        prim = self.engine.primitives.get(ident)
        if prim is None:
            return False, "acquired primitive not registered"
        for inp, want in examples:
            try:
                val = prim.fn(**dict(inp))
            except Exception as exc: # noqa: BLE001 - probe, don't crash
                return False, f"primitive raised {type(exc).__name__}"
            if self._norm_num(val) != self._norm_num(want):
                return False, f"value mismatch on {inp}: {val!r}"
        return True, "exact on examples"

    def _epistemic_agenda_subjects(self) -> set:
        """Open lh_outcome questions whose positive hypothesis is still
        UNDER_TEST or REFUTED. Generic: subject_id from provenance only."""
        out = set()
        intellect = getattr(self.engine, "intellect", None)
        if intellect is None or not getattr(intellect, "_epistemic_enabled", True):
            return out
        if not hasattr(intellect, "agenda"):
            return out
        try:
            questions = intellect.agenda.open_questions()
        except Exception:
            return out
        for q in questions or []:
            if getattr(q, "origin", "") != "lh_outcome":
                continue
            sid = (getattr(q, "provenance", None) or {}).get("subject_id")
            if not sid:
                continue
            pos = None
            try:
                pos = intellect.epistemic.get_hypothesis(f"h_pos:{sid}")
            except Exception:
                pos = None
            state = getattr(getattr(pos, "state", None), "value", None) if pos else None
            if state in ("under_test", "refuted"):
                out.add(str(sid))
        return out

    def detect_gaps(self, challenges: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        gaps = []
        for ch in challenges:
            if ch["resolved"] or ch["deferred"]:
                continue
            kind, ident = self._find_capability(ch["goal"])
            if kind is not None:
                ok, why = self._exec_capability_exact(kind, ident, ch["train"])
                if ok and not ch["heldout_failed"]:
                    # State already covers it (acquired outside this loop or
                    # in an earlier cycle): no gap, record the fact.
                    self.store.update_challenge(
                        self.objective_id, ch["id"], resolved=1,
                        capability_id=f"{kind}:{ident}")
                    ch["resolved"] = True
                    ch["capability_id"] = f"{kind}:{ident}"
                    continue
                if ch["heldout_failed"]:
                    # A stored capability is train-exact but was PROVEN wrong
                    # on held-out inputs and quarantined: accepting it would
                    # be teaching to the training set. Still a gap.
                    reason = ("stored capability failed held-out verification "
                              f"(quarantined): {why}")
                else:
                    reason = f"stored capability inexact: {why}"
            else:
                reason = "no live capability serves goal"
            gaps.append({"challenge_id": ch["id"], "goal": ch["goal"],
                         "reason": reason, "challenge": ch})
        return gaps

    # -- novel task generation + scoring ------------------------------------
    def _factors(self, ch: Dict[str, Any], cycle_n: int) -> Dict[str, float]:
        n_train = len(ch["train"])
        evidence = min(1.0, n_train / 6.0)
        if (ch["attempts"] > 0 and ch["last_classification"] == "EVIDENCE-BOUNDED"
                and n_train > ch["train_at_last_attempt"]):
            evidence = 1.0  # recovery retry: evidence now available
        novelty = 1.0 if ch["attempts"] == 0 else 0.5 ** ch["attempts"]
        waiting = cycle_n if ch["last_attempt_cycle"] < 0 else cycle_n - ch["last_attempt_cycle"]
        urgency = min(1.0, 0.4 + 0.15 * min(3, waiting))
        if self._prior_wall:
            est = sum(self._prior_wall) / len(self._prior_wall)
        else:
            est = 30.0
        cost = 1.0 / (1.0 + est / 60.0)
        return {"evidence": round(evidence, 3), "novelty": round(novelty, 3),
                "urgency": round(urgency, 3), "cost": round(cost, 3)}

    def _subject_has_unused_io_failure(self, subject_id: str) -> bool:
        intellect = getattr(self.engine, "intellect", None)
        if intellect is None:
            return False
        for hid in (f"h_pos:{subject_id}", f"h_neg:{subject_id}"):
            try:
                evs = intellect.epistemic.evidence_for(hid)
            except Exception:
                evs = []
            for ev in evs or []:
                content = getattr(ev, "content", None) or {}
                if content.get("channel") == "unused_io" and not getattr(ev, "supports", True):
                    return True
        return False

    def _candidates_from_agenda(self) -> List[TaskCandidate]:
        """Any OPEN/INVESTIGATING agenda question becomes an investigate
        candidate. Not an acquire retry. Origin-agnostic."""
        out: List[TaskCandidate] = []
        intellect = getattr(self.engine, "intellect", None)
        if intellect is None or not hasattr(intellect, "agenda"):
            return out
        try:
            questions = intellect.agenda.open_questions()
        except Exception:
            return out
        weights = None
        try:
            weights = intellect.agenda.weights()
        except Exception:
            weights = None
        for q in questions or []:
            prov = getattr(q, "provenance", None) or {}
            if (prov.get("status") == "refuted"
                    and getattr(self, "_synthesis_from_refute_enabled", True)):
                continue
            sid = prov.get("subject_id")
            if (sid and getattr(self, "_validation_synth_enabled", True)
                    and getattr(self.engine, "_validation_synth_enabled", True)
                    and self._subject_has_unused_io_failure(sid)):
                continue
            try:
                util = float(intellect.agenda.score(q, weights))
            except Exception:
                util = 0.5
            sid = prov.get("subject_id")
            factors = {"evidence": min(1.0, util), "novelty": 0.7,
                       "urgency": 0.8, "cost": 0.7}
            out.append(TaskCandidate(
                kind="investigate",
                challenge_id=q.question_id,
                goal=q.text,
                factors=factors,
                utility=round(util, 4),
                detail=f"agenda origin={q.origin} subject={sid} status={q.status.value}"))
        return out

    def _refuted_hypotheses(self):
        intellect = getattr(self.engine, "intellect", None)
        if intellect is None or not hasattr(intellect, "epistemic"):
            return []
        try:
            hyps = intellect.epistemic.all_hypotheses()
        except Exception:
            return []
        out = []
        for h in hyps or []:
            st = getattr(getattr(h, "state", None), "value", None)
            if st == "refuted":
                out.append(h)
        return out

    def _candidates_from_synthesis(self) -> List[TaskCandidate]:
        """When any hypothesis is REFUTED, surface TaskSynthesizer work.
        Generic state feature (refuted hyp exists), not an origin branch."""
        if not getattr(self, "_synthesis_from_refute_enabled", True):
            return []
        refuted = self._refuted_hypotheses()
        if not refuted:
            return []
        loop = getattr(self.engine, "task_synthesis_loop", None)
        if loop is None or not hasattr(loop, "synth"):
            return []
        try:
            tasks = loop.synth.generate()
        except Exception:
            return []
        hid = refuted[0].hypothesis_id
        out: List[TaskCandidate] = []
        for t in tasks or []:
            out.append(TaskCandidate(
                kind="synthesize",
                challenge_id=t.task_id,
                goal=t.goal,
                factors={"evidence": 0.6, "novelty": float(t.novelty or 0.5),
                         "urgency": 0.7, "cost": float(t.cost or 0.5)},
                utility=float(t.utility or 0.0),
                detail=(f"synth op={t.synthesis_trace.get('operator')} "
                        f"class={t.capability_class} "
                        f"because {hid} refuted"),
                payload={"examples": t.examples,
                         "operator": t.synthesis_trace.get("operator"),
                         "capability_class": t.capability_class,
                         "example_kind": t.synthesis_trace.get("example_kind"),
                         "n_examples": t.synthesis_trace.get("n_examples"),
                         "hypothesis_id": t.synthesis_trace.get("hypothesis_id"),
                         "evidence_ids": t.synthesis_trace.get("evidence_ids"),
                         "example_source": t.synthesis_trace.get("example_source"),
                         "refuted_hypothesis": hid,
                         "source": "epistemic_state"}))
        return out

    def generate_candidates(self, gaps: List[Dict[str, Any]],
                            cycle_n: int) -> List[TaskCandidate]:
        cands: List[TaskCandidate] = []
        for g in gaps:
            ch = g["challenge"]
            factors = self._factors(ch, cycle_n)
            utility = sum(self.weights[f] * factors[f] for f in self.FACTORS)
            detail = (f"attempts={ch['attempts']} n_train={len(ch['train'])} "
                      f"last={ch['last_classification']}")
            if ch["attempts"] > 0:
                detail += " retry"
            cands.append(TaskCandidate(kind="acquire", challenge_id=ch["id"],
                                       goal=ch["goal"], factors=factors,
                                       utility=utility, detail=detail))
        if getattr(self, "_agenda_candidates_enabled", True):
            cands.extend(self._candidates_from_agenda())
        cands.extend(self._candidates_from_synthesis())
        if not gaps:
            # Nothing left to acquire -- BUT the closure audit is only
            # generated when no challenge is deferred. A deferred challenge
            # is a measured boundary, not a resolution; auditing "closure"
            # while one stands would certify an incomplete objective.
            # (detect_gaps excludes deferred challenges from gaps, so an
            # empty gaps list does not imply none are deferred -- check
            # the store directly.)
            chs = self.store.load_challenges(self.objective_id)
            any_deferred = any(c["deferred"] for c in chs)
            if not any_deferred:
                # Nothing left to acquire and nothing deferred: generate a
                # closure-audit task from state (revocation/recovery drill
                # on a real acquired capability).
                cands.append(TaskCandidate(
                    kind="closure_audit", challenge_id=None, goal=None,
                    factors={"evidence": 1.0, "novelty": 0.5, "urgency": 0.8,
                             "cost": 0.7},
                    utility=sum(self.weights[f] * v for f, v in
                                {"evidence": 1.0, "novelty": 0.5,
                                 "urgency": 0.8, "cost": 0.7}.items()),
                    detail="no gaps: audit persistence via revoke/recover drill"))
            # else: deferred challenges remain; no audit, no candidates --
            # the cycle records the standing boundary.
        # Deterministic order: utility desc, then challenge id.
        cands.sort(key=lambda c: (-c.utility, c.challenge_id or ""))
        return cands

    # -- failure classification ----------------------------------------------
    @staticmethod
    def classify_failure(blob: str) -> str:
        t = blob.lower()
        if any(s in t for s in ["i7", "need n_examples", "insufficient",
                                "not enough examples", "ambiguous",
                                "evidence-bounded", "evidence insufficiency"]):
            return "EVIDENCE-BOUNDED"
        if "timeout" in t or "timed out" in t or "budget" in t:
            return "RESOURCE-BOUNDED"
        if "representation" in t or "no grammar" in t or "cannot represent" in t:
            return "REPRESENTATION-BOUNDED"
        return "SEARCH-BOUNDED"

    def _capability_snapshot(self) -> Dict[str, str]:
        """kind:id -> display for every live capability in both stores."""
        snap = {}
        for c in self.engine.capabilities.list(limit=100000):
            snap[f"plan:{c.capability_id}"] = c.capability_id
        for r in self.engine.acquired_code.all():
            if r["status"] == "active":
                snap[f"code:{r['name']}"] = r["name"]
        return snap

    # -- task execution -------------------------------------------------------
    def _execute_acquire(self, cand: TaskCandidate,
                         ch: Dict[str, Any]) -> Dict[str, Any]:
        orch = self.engine.acquisition_orchestrator
        before = self._capability_snapshot()
        t0 = time.time()
        error_text = ""
        result = None
        timed_out = False
        try:
            result = asyncio.run(asyncio.wait_for(
                orch.resolve(cand.goal, examples=ch["train"]),
                timeout=self.resolve_timeout_s))
        except asyncio.TimeoutError:
            timed_out = True
            error_text = f"resolve timed out after {self.resolve_timeout_s}s"
        except Exception as exc:  # noqa: BLE001 - classify, don't crash
            error_text = f"{type(exc).__name__}: {exc}"
        wall = time.time() - t0
        self._prior_wall.append(wall)
        out: Dict[str, Any] = {
            "kind": "acquire", "challenge_id": cand.challenge_id,
            "wall_s": round(wall, 2), "timed_out": timed_out,
            "fully_resolved": bool(result and result.fully_resolved),
            "acquired": list(getattr(result, "acquired", []) or []),
            "failed": list(getattr(result, "failed", []) or []),
            "rolled_back": list(getattr(result, "rolled_back", []) or []),
            "attempts": [a.as_dict() for a in
                         (getattr(result, "attempts", []) or [])],
        }
        blob_parts = [error_text] + out["failed"]
        for a in out["attempts"]:
            blob_parts.append(str(a.get("detail", "")))
            blob_parts.append(str(a.get("strategy", "")))
        classification = self.classify_failure(" | ".join(blob_parts))
        if timed_out:
            classification = "RESOURCE-BOUNDED"
        out["classification"] = classification
        out["error_text"] = error_text[:300]

        after = self._capability_snapshot()
        out["new_capability_ids"] = sorted(set(after) - set(before))

        # Verify on MY held-out examples (engine never sees these), through
        # the real stores: plan capability or acquired-code primitive.
        verified = False
        held_detail = ""
        found_kind, found_ident = None, None
        if out["fully_resolved"]:
            found_kind, found_ident = self._find_capability(cand.goal)
            if found_kind is not None:
                ok, why = self._exec_capability_exact(
                    found_kind, found_ident, ch["heldout"])
                verified = ok
                held_detail = why
            else:
                held_detail = "no live capability after resolve"
        out["heldout_verified"] = verified
        out["heldout_detail"] = held_detail
        out["capability_ref"] = (f"{found_kind}:{found_ident}"
                                 if found_kind else None)
        out["verification_failure"] = bool(out["fully_resolved"] and not verified)

        # Verification failure is EVIDENCE-BOUNDED, not search-bounded: the
        # engine's own admission gate passed a hypothesis that the training
        # evidence underdetermined (proven wrong on unseen inputs). The wrong
        # capability is quarantined through the real integrity path so it
        # cannot poison later decompositions as a reused child.
        out["quarantined_wrong_capability"] = None
        if out["verification_failure"]:
            classification = "EVIDENCE-BOUNDED"
            out["classification"] = classification
            out["classification_note"] = (
                "resolve succeeded but held-out verification failed: "
                "training evidence underdetermined the admitted hypothesis")
            qid = found_ident
            if found_kind == "code":
                rec = next((r for r in self.engine.acquired_code.all()
                            if r["name"] == found_ident), None)
                qid = rec["capability_id"] if rec else found_ident
            try:
                from swarm_engine.synthesis.integrity import \
                    quarantine_everywhere
                quarantine_everywhere(
                    self.engine, qid,
                    "f10: loop held-out verification rejected capability "
                    f"for challenge {cand.challenge_id}")
                out["quarantined_wrong_capability"] = qid
            except Exception as exc:  # noqa: BLE001 - record, don't crash
                out["quarantine_error"] = f"{type(exc).__name__}: {exc}"
        elif not out["fully_resolved"] and not timed_out \
                and wall >= 25.0 and classification == "SEARCH-BOUNDED":
            # Heuristic with a measured basis: the behavioral decomposer's
            # production wall-clock budget is 30s; a failed resolve that
            # burned >=25s without an evidence/representation signal most
            # likely expired that search budget. Recorded as a likelihood,
            # not a certainty.
            classification = "RESOURCE-BOUNDED"
            out["classification"] = classification
            out["classification_note"] = (
                f"likely: resolve burned {wall:.0f}s without resolving; "
                "behavioral decomposition budget is 30s")
        if out["fully_resolved"] and verified:
            out["classification"] = "RESOLVED"
        out["epistemic"] = self._record_epistemic(cand, ch, out)

        # Causal reuse: does the new capability reference capabilities that
        # existed BEFORE this task? Those are genuine prerequisite reuses.
        reuse_links = []
        if verified and found_kind == "plan":
            cap = self.engine.capabilities.get(found_ident)
            ops_text = json.dumps(getattr(cap, "plan", None) or {},
                                  default=str)
            for key, name in before.items():
                if key.startswith("code:") and name in ops_text:
                    reuse_links.append(key)
                elif key.startswith("plan:") and len(name) >= 12 \
                        and name[:12] in ops_text:
                    reuse_links.append(key)
        out["reused_capability_ids"] = sorted(set(reuse_links))
        out["success"] = bool(out["fully_resolved"] and verified)
        return out

    def _record_epistemic(self, cand, ch, out) -> Optional[Dict[str, Any]]:
        """LH outcome → existing Evidence → EvidenceArbiter → persist.

        Generic: any challenge id/goal. Two observations (resolve +
        held-out) so the arbiter can leave UNDER_TEST or decide.
        Disabled when engine.intellect._epistemic_enabled is False.
        """
        intellect = getattr(self.engine, "intellect", None)
        if intellect is None or not hasattr(intellect, "record_task_evidence"):
            return None
        resolved = bool(out.get("fully_resolved"))
        held = bool(out.get("heldout_verified"))
        def _io(pairs):
            rows = []
            for item in pairs or []:
                if isinstance(item, (list, tuple)) and len(item) == 2 and isinstance(item[0], dict):
                    rows.append({"input": dict(item[0]), "expected": item[1]})
            return rows
        attach = getattr(self.engine, "_evidence_io_enabled", True)
        resolve_content = {"channel": "resolve",
                           "classification": out.get("classification"),
                           "challenge_id": cand.challenge_id}
        held_content = {"channel": "heldout",
                        "heldout_verified": held,
                        "challenge_id": cand.challenge_id}
        if attach:
            resolve_content["examples"] = _io(ch.get("train"))
            held_content["examples"] = _io(ch.get("heldout"))
        items = [
            {"supports": resolved, "content": resolve_content},
            {"supports": held, "content": held_content},
        ]
        try:
            return intellect.record_task_evidence(
                subject_id=str(cand.challenge_id or ch.get("id")),
                statement=f"goal {ch.get('goal')} is held-out exact",
                items=items,
                source="lh_task_outcome")
        except Exception as exc:  # noqa: BLE001 - epistemic must not abort acquire
            return {"error": f"{type(exc).__name__}: {exc}"}

    def _execute_synthesize(self, cand) -> Dict[str, Any]:
        out = {"kind": "synthesize", "success": False, "goal": cand.goal}
        examples = (cand.payload or {}).get("examples") or []
        rec = None
        try:
            import asyncio
            coro = self.engine.resolve(cand.goal, examples=examples)
            try:
                asyncio.get_running_loop()
                running = True
            except RuntimeError:
                running = False
            if running:
                import concurrent.futures
                with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                    rec = pool.submit(asyncio.run, coro).result()
            else:
                rec = asyncio.run(coro)
        except Exception as exc:
            out["detail"] = f"{type(exc).__name__}: {exc}"
            return out
        fully = bool((rec or {}).get("fully_resolved")) if isinstance(rec, dict) else False
        out["success"] = fully
        out["classification"] = "SYNTH-RESOLVED" if fully else "SYNTH-UNRESOLVED"
        if isinstance(rec, dict):
            out["resolve"] = {k: rec.get(k) for k in
                              ("goal", "fully_resolved", "failed", "acquired")
                              if k in rec}
            atts = rec.get("attempts") or []
            out["resolve"]["n_attempts"] = len(atts)
        # epistemic consequence: record whether synthesized goal held out
        intellect = getattr(self.engine, "intellect", None)
        if intellect is not None and hasattr(intellect, "record_task_evidence"):
            sid = cand.challenge_id or cand.goal
            try:
                out["epistemic"] = intellect.record_task_evidence(
                    subject_id=str(sid),
                    statement=f"synthesized goal {cand.goal} is held-out exact",
                    items=[{"supports": fully,
                            "content": {"channel": "synth_resolve",
                                        "goal": cand.goal}}],
                    source="synth_outcome")
            except Exception as exc:
                out["epistemic_error"] = str(exc)
        if fully:
            gen = self._generalize_acquired(cand, examples)
            if gen:
                out["generalization"] = gen
                if intellect is not None and hasattr(intellect, "record_task_evidence"):
                    try:
                        items = [{"supports": bool(p.get("match")),
                                  "content": {"channel": "unused_io",
                                              "input": p.get("input"),
                                              "expected": p.get("expected"),
                                              "observed": p.get("observed"),
                                              "goal": cand.goal,
                                              "from_hypothesis": p.get("from_hypothesis"),
                                              "from_evidence": p.get("from_evidence")}}
                                 for p in gen.get("probes") or []]
                        if items:
                            out["generalization_epistemic"] = intellect.record_task_evidence(
                                subject_id=str(cand.challenge_id or cand.goal),
                                statement=f"synthesized goal {cand.goal} generalizes off Evidence examples",
                                items=items,
                                source="unused_io")
                    except Exception as exc:
                        out["generalization_error"] = str(exc)
        return out

    def _generalize_acquired(self, cand, used_examples) -> Dict[str, Any]:
        """Run the admitted cap on Evidence I/O that was not used to acquire it."""
        used = set()
        for item in used_examples or []:
            if isinstance(item, (list, tuple)) and len(item) == 2 and isinstance(item[0], dict):
                used.add((tuple(sorted(item[0].items())), item[1]))
        unused = []
        intellect = getattr(self.engine, "intellect", None)
        if intellect is None:
            return {}
        try:
            hyps = intellect.epistemic.all_hypotheses()
        except Exception:
            return {}
        for h in hyps or []:
            st = getattr(getattr(h, "state", None), "value", None)
            if st != "refuted":
                continue
            try:
                evs = intellect.epistemic.evidence_for(h.hypothesis_id)
            except Exception:
                evs = []
            for ev in evs or []:
                for row in (getattr(ev, "content", None) or {}).get("examples") or []:
                    if not isinstance(row, dict) or "input" not in row:
                        continue
                    inp, exp = row.get("input"), row.get("expected")
                    if not isinstance(inp, dict):
                        continue
                    key = (tuple(sorted(inp.items())), exp)
                    if key in used:
                        continue
                    unused.append({"input": dict(inp), "expected": exp,
                                   "from_hypothesis": h.hypothesis_id,
                                   "from_evidence": ev.evidence_id})
        if not unused:
            return {"probes": [], "note": "no unused Evidence I/O"}
        slug = cand.goal
        prim = None
        try:
            prim = self.engine.primitives.get(slug)
        except Exception:
            prim = None
        if prim is None:
            return {"probes": [], "note": "no admitted primitive"}
        fn = getattr(prim, "fn", None)
        probes = []
        for u in unused:
            observed = None
            err = None
            try:
                observed = fn(**dict(u["input"]))
            except Exception as exc:
                err = f"{type(exc).__name__}: {exc}"
            match = (err is None and observed == u["expected"])
            if not match and err is None:
                try:
                    match = float(observed) == float(u["expected"])
                except Exception:
                    match = False
            probes.append({**u, "observed": observed, "error": err, "match": bool(match)})
        return {"probes": probes,
                "n_unused": len(probes),
                "n_match": sum(1 for p in probes if p["match"])}

    def _execute_investigate(self, cand) -> Dict[str, Any]:
        intellect = getattr(self.engine, "intellect", None)
        out = {"kind": "investigate", "success": False,
               "question_id": cand.challenge_id}
        if intellect is None or not hasattr(intellect, "investigate_open_question"):
            out["detail"] = "intellect surface missing"
            return out
        try:
            rec = intellect.investigate_open_question(cand.challenge_id)
        except Exception as exc:  # noqa: BLE001
            out["detail"] = f"{type(exc).__name__}: {exc}"
            return out
        out["success"] = bool(rec.get("ok"))
        out["classification"] = "INVESTIGATED" if out["success"] else "INVESTIGATE-FAILED"
        out["intellect"] = rec
        return out

    def _execute_closure_audit(self) -> Dict[str, Any]:
        """No gaps remain: prove the persisted state is genuinely robust by
        revoking one real acquired capability and recovering it."""
        from swarm_engine.synthesis.integrity import quarantine_everywhere
        challenges = self.store.load_challenges(self.objective_id)
        resolved = [c for c in challenges if c["resolved"] and c["capability_id"]]
        out: Dict[str, Any] = {"kind": "closure_audit", "success": False}
        if not resolved:
            out["detail"] = "no resolved capabilities to audit"
            return out
        # Prefer a mid-chain capability (plan referencing other acquired
        # capabilities); fall back to any resolved one.
        scored = []
        for ch in resolved:
            ref = ch["capability_id"] or ""
            kind, _, ident = ref.partition(":")
            refs = 0
            if kind == "plan":
                cap = self.engine.capabilities.get(ident)
                if cap is not None and getattr(cap, "plan", None):
                    refs = json.dumps(cap.plan, default=str).count("acquired.")
            live_k, _ = self._find_capability(ch["goal"])
            if live_k is None:
                slug = (ch.get("goal") or "").lower().replace(" ", "_")
                if self.engine.primitives.get(slug) is None:
                    continue
            scored.append((refs, ch))
        if not scored:
            scored = [(0, ch) for ch in resolved]
        scored.sort(key=lambda t: (-t[0], t[1]["id"]))
        target = scored[0][1]
        tkind, _, tident = (target["capability_id"] or "").partition(":")
        # Revoke the LIVE serving capability, not a stale challenge-table
        # id left over from an earlier admission.
        live_k, live_i = self._find_capability(target["goal"])
        if live_k == "plan" and live_i:
            tkind, tident = live_k, live_i
        elif live_k == "code" and live_i:
            tkind, tident = live_k, live_i
        # Baseline: executes exactly now.
        ok0, _ = self._exec_capability_exact(tkind, tident, target["train"][:2])
        out["baseline_executes"] = ok0
        # Revoke through the real integrity path (all status systems).
        rev_id = tident
        if tkind == "code":
            rec = next((r for r in self.engine.acquired_code.all()
                        if r["name"] == tident), None)
            rev_id = rec["capability_id"] if rec else tident
        quarantine_everywhere(self.engine, rev_id,
                              "f10 closure audit: revoke/recover drill")
        # Must now be invalid: the live-capability lookup must fail or the
        # execution must no longer be exact.
        k2, i2 = self._find_capability(target["goal"])
        still_ok = False
        if k2 is not None:
            still_ok, _ = self._exec_capability_exact(k2, i2, target["train"][:2])
        out["revoked_invalid"] = not still_ok
        # Recover via the normal acquisition path (no manual rebuild).
        rec = asyncio.run(asyncio.wait_for(
            self.engine.acquisition_orchestrator.resolve(
                target["goal"], examples=target["train"]),
            timeout=self.resolve_timeout_s))
        out["recovery_resolved"] = bool(getattr(rec, "fully_resolved", False))
        register = getattr(self.engine.admission, "_register_capability_as_primitive", None)
        new_cid = None
        for a in reversed(list(getattr(rec, "attempts", None) or [])):
            accepted = getattr(a, "accepted", None)
            if accepted is None and isinstance(a, dict):
                accepted = a.get("accepted")
            cid = getattr(a, "capability_id", None) or (a.get("capability_id") if isinstance(a, dict) else None)
            if accepted and cid:
                new_cid = cid
                break
        if new_cid:
            try:
                crec = self.engine.capabilities.get(new_cid)
                if crec is not None:
                    if getattr(crec, "status", "") != "active":
                        self.engine.capabilities.set_status(new_cid, "active")
                        crec = self.engine.capabilities.get(new_cid)
                    if register is not None and crec is not None:
                        register(new_cid, crec)
                    self.engine.capabilities.bind_goal(target["goal"], new_cid)
            except Exception:
                pass
        k3, i3 = self._find_capability(target["goal"])
        if k3 is not None:
            ok3, _ = self._exec_capability_exact(k3, i3, target["heldout"])
            out["recovery_heldout"] = ok3
            out["new_capability_ref"] = f"{k3}:{i3}"
        elif out.get("recovery_resolved"):
            slug = target["goal"].lower().replace(" ", "_")
            prim = self.engine.primitives.get(slug)
            if prim is None:
                for n in self.engine.primitives.names():
                    if n.replace(" ", "_").lower() == slug or n.lower() == target["goal"].lower():
                        prim = self.engine.primitives.get(n)
                        slug = n
                        break
            if prim is not None:
                ok3 = True
                for inp, want in target["heldout"]:
                    try:
                        got = prim.fn(**dict(inp))
                    except Exception:
                        ok3 = False
                        break
                    if got != want:
                        try:
                            if not (float(got) == float(want)):
                                ok3 = False
                                break
                        except Exception:
                            ok3 = False
                            break
                out["recovery_heldout"] = ok3
                out["new_capability_ref"] = f"prim:{slug}"
        out["target_challenge"] = target["id"]
        out["target_kind"] = tkind
        out["success"] = bool(out["baseline_executes"] and out["revoked_invalid"]
                              and out["recovery_resolved"]
                              and out.get("recovery_heldout"))
        return out

    # -- learning: adapt selection weights from measured outcomes -------------
    def _learn(self, executed: List[Tuple[TaskCandidate, Dict[str, Any]]]) -> Dict[str, Any]:
        before = dict(self.weights)
        for cand, outcome in executed:
            if not cand.factors:
                continue
            dom = max(cand.factors, key=lambda f: cand.factors[f])
            mult = 1.15 if outcome.get("success") else 0.85
            self.weights[dom] = max(0.05, self.weights[dom] * mult)
        total = sum(self.weights.values()) or 1.0
        for f in self.weights:
            self.weights[f] = round(self.weights[f] / total, 4)
        self.store.save_weights(self.objective_id, self.weights)
        return {"before": before, "after": dict(self.weights)}

    # -- recovery ---------------------------------------------------------------
    def _recover(self, ch: Dict[str, Any], outcome: Dict[str, Any],
                 cycle_n: int) -> Dict[str, Any]:
        """Classify-driven recovery. Returns a recovery record; the challenge
        stays unresolved so a LATER cycle retries it (causal downstream)."""
        cid = ch["id"]
        cls = outcome["classification"]
        rec: Dict[str, Any] = {"classification": cls, "action": None}
        attempts = ch["attempts"] + 1
        verif_fail = bool(outcome.get("verification_failure"))
        if cls == "EVIDENCE-BOUNDED":
            new_examples = [tuple(e) for e in
                            self.env.request_more_examples(cid)]
            seen = {json.dumps(i, sort_keys=True) for i, _ in ch["train"]}
            fresh = [e for e in new_examples
                     if json.dumps(e[0], sort_keys=True) not in seen]
            if fresh:
                train = ch["train"] + [[e[0], e[1]] for e in fresh]
                self.store.update_challenge(
                    self.objective_id, cid, train=train, attempts=attempts,
                    last_attempt_cycle=cycle_n,
                    train_at_last_attempt=len(ch["train"]),
                    last_classification=cls,
                    heldout_failed=1 if verif_fail else ch["heldout_failed"])
                rec["action"] = (f"evidence_acquired: +{len(fresh)} examples "
                                 f"({len(ch['train'])}->{len(train)}); retry scheduled")
                rec["recovered_path"] = "retry_next_cycle"
                if verif_fail:
                    rec["action"] += "; wrong capability quarantined"
                self.store.add_causal(
                    self.objective_id, cycle_n, "evidence_for",
                    f"env:extra_examples:{cid}", f"challenge:{cid}",
                    f"+{len(fresh)} examples after {cls} failure")
            else:
                self.store.update_challenge(
                    self.objective_id, cid, attempts=attempts,
                    last_attempt_cycle=cycle_n,
                    train_at_last_attempt=len(ch["train"]),
                    last_classification=cls, deferred=1,
                    heldout_failed=1 if verif_fail else ch["heldout_failed"],
                    defer_reason=f"{cls}: environment has no more examples")
                rec["action"] = "deferred: no more evidence available"
        elif attempts >= self.max_attempts:
            self.store.update_challenge(
                self.objective_id, cid, attempts=attempts,
                last_attempt_cycle=cycle_n,
                train_at_last_attempt=len(ch["train"]),
                last_classification=cls, deferred=1,
                defer_reason=f"{cls} after {attempts} attempts (measured boundary)")
            rec["action"] = f"deferred: {cls} boundary measured"
        else:
            self.store.update_challenge(
                self.objective_id, cid, attempts=attempts,
                last_attempt_cycle=cycle_n,
                train_at_last_attempt=len(ch["train"]),
                last_classification=cls)
            rec["action"] = f"retry scheduled (attempt {attempts + 1} of {self.max_attempts})"
            rec["recovered_path"] = "retry_next_cycle"
        return rec

    # -- one full cycle ----------------------------------------------------------
    def run_cycle(self) -> Dict[str, Any]:
        from swarm_engine.cognition.synthesis import SEARCH_POLICY_VERSION
        t_start = time.time()
        cycle_n = self.store.next_cycle_n(self.objective_id)
        pid = os.getpid()
        record: Dict[str, Any] = {
            "objective_id": self.objective_id, "cycle_n": cycle_n, "pid": pid,
            "policy": SEARCH_POLICY_VERSION,
        }

        # 1. state assessment
        state = self.assess()
        record["state_before"] = {k: v for k, v in state.items()
                                  if k != "capability_ids"}
        record["n_capabilities_before"] = state["n_capabilities"]

        # 2. sync challenges (first cycle) + gap detection
        self.store.sync_challenges(self.objective_id, self.env.challenges())
        challenges = self.store.load_challenges(self.objective_id)
        gaps = self.detect_gaps(challenges)
        record["gaps"] = [{"challenge_id": g["challenge_id"],
                           "reason": g["reason"]} for g in gaps]

        # 3-4. generate candidates, select
        candidates = self.generate_candidates(gaps, cycle_n)
        record["candidates"] = [c.as_dict() for c in candidates]
        selected = candidates[:self.tasks_per_cycle]
        record["selected"] = [c.as_dict() for c in selected]

        # 5-8. execute / verify / recover, then learn
        executed = []
        for cand in selected:
            if cand.kind == "closure_audit":
                outcome = self._execute_closure_audit()
                ch = None
            elif cand.kind == "investigate":
                outcome = self._execute_investigate(cand)
                ch = None
            elif cand.kind == "synthesize":
                outcome = self._execute_synthesize(cand)
                ch = None
            else:
                ch = next(c for c in challenges
                          if c["id"] == cand.challenge_id)
                outcome = self._execute_acquire(cand, ch)
            executed.append((cand, outcome, ch))
        record["tasks"] = []
        for cand, outcome, ch in executed:
            task_rec = {"candidate": cand.as_dict(), "outcome": outcome}
            if cand.kind == "acquire" and ch is not None:
                if outcome["success"]:
                    self.store.update_challenge(
                        self.objective_id, ch["id"], resolved=1,
                        resolved_cycle=cycle_n,
                        capability_id=outcome["capability_ref"],
                        attempts=ch["attempts"] + 1,
                        last_attempt_cycle=cycle_n,
                        train_at_last_attempt=len(ch["train"]),
                        last_classification="RESOLVED",
                        # A retry that now verifies on held-out clears the
                        # earlier held-out failure: the stored capability
                        # is no longer the quarantined wrong one.
                        heldout_failed=0)
                    for rid in outcome["reused_capability_ids"]:
                        self.store.add_causal(
                            self.objective_id, cycle_n, "prerequisite_reuse",
                            f"capability:{rid}",
                            f"challenge:{ch['id']}",
                            "parent plan references pre-existing capability")
                    # State progression: if a reused capability was produced
                    # by an earlier-resolved challenge in THIS objective,
                    # record the explicit cycle-to-cycle dependency. This
                    # is the persisted evidence that later work stands on
                    # earlier outcomes, not just on pre-existing state.
                    if outcome["reused_capability_ids"]:
                        prior = {
                            c["capability_id"]: c
                            for c in challenges
                            if c["resolved"] and c["capability_id"]
                        }
                        for rid in outcome["reused_capability_ids"]:
                            # rid is like "code:name" or "plan:name"; match
                            # against stored capability refs.
                            for capref, pc in prior.items():
                                if pc["id"] == ch["id"]:
                                    continue
                                if rid.split(":", 1)[-1] in capref:
                                    self.store.add_causal(
                                        self.objective_id, cycle_n,
                                        "state_progression",
                                        f"challenge:{pc['id']}"
                                        f"(cycle {pc['resolved_cycle']})",
                                        f"challenge:{ch['id']}",
                                        f"cycle {cycle_n} acquisition reuses "
                                        f"capability resolved in cycle "
                                        f"{pc['resolved_cycle']}")
                                    break
                    if ch["attempts"] > 0:
                        self.store.add_causal(
                            self.objective_id, cycle_n, "retry_of",
                            f"cycle<{cycle_n}:challenge:{ch['id']}",
                            f"challenge:{ch['id']}",
                            f"recovered after {outcome['classification']} "
                            f"on attempt {ch['attempts'] + 1}")
                        task_rec["recovered"] = True
                else:
                    rec = self._recover(ch, outcome, cycle_n)
                    task_rec["recovery"] = rec
            elif cand.kind == "closure_audit":
                task_rec["audit"] = True
            record["tasks"].append(task_rec)

        # 9. learning (self-improvement of the selector)
        record["learning"] = self._learn(
            [(c, o) for c, o, _ in executed])

        # 10. reassess + completion check
        challenges = self.store.load_challenges(self.objective_id)
        unresolved = [c["id"] for c in challenges
                      if not c["resolved"] and not c["deferred"]]
        deferred = [c["id"] for c in challenges if c["deferred"]]
        retryable_deferred = deferred  # alias kept for record clarity
        audit_done = any(c.kind == "closure_audit" and o.get("success")
                         for c, o, _ in executed)
        had_audit_before = self._audit_done_before(cycle_n)
        # Completion requires: no unresolved, NO deferred (a deferred
        # challenge is a measured boundary, not a resolution -- the
        # objective is not complete while one stands), and a successful
        # closure audit. The audit itself is only generated when no
        # deferred challenge remains (see generate_candidates).
        complete = (not unresolved and not deferred
                    and (audit_done or had_audit_before))
        record["state_after"] = {
            "n_capabilities": self.assess()["n_capabilities"],
            "resolved": [c["id"] for c in challenges if c["resolved"]],
            "unresolved": unresolved,
            "deferred": [{"id": c["id"], "reason": c["defer_reason"]}
                         for c in challenges if c["deferred"]],
        }
        record["objective_complete"] = complete
        record["wall_s"] = round(time.time() - t_start, 2)
        if complete:
            self.store.set_status(self.objective_id, "complete")

        self.store.save_cycle(self.objective_id, cycle_n, pid,
                              SEARCH_POLICY_VERSION, t_start, record)
        return record

    def _audit_done_before(self, cycle_n: int) -> bool:
        with self.store._conn() as conn:
            rows = conn.execute(
                "SELECT record_json FROM lh_cycle WHERE objective_id=? AND cycle_n<?",
                (self.objective_id, cycle_n)).fetchall()
        for r in rows:
            try:
                rec = json.loads(r["record_json"])
            except Exception:  # noqa: BLE001
                continue
            for t in rec.get("tasks", []):
                if (t.get("candidate", {}).get("kind") == "closure_audit"
                        and t.get("outcome", {}).get("success")):
                    return True
        return False
