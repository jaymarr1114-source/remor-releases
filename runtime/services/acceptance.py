"""M6 — the user-acceptance loop with near-miss records.

James's standing rule (2026-09-27): system "completed" is NOT terminal — it is
a candidate state. Only the user's satisfaction, or one of the five terminal
conditions, closes the loop. Dissatisfaction reopens the loop at pool
expansion, with the failed attempt + feedback persisted as a near-miss record
that steers the next search away from the characterized failure and toward
the unmet criteria.

Architecture (ownership-respecting):
  * The scheduler's run record is FROZEN — this module never edits it. It is
    read (via scheduler.get_run) for the underlying system status.
  * Acceptance state lives in an OVERLAY record keyed by run_id, persisted in
    its own sqlite DB (acceptance.db). "completed" vs "accepted" are two
    distinct statuses on the overlay:
      - completed  = scheduler status "completed" AND the authentication gate
                     passed (held-out tests, negative controls,
                     counterfactuals where applicable). Candidate state.
      - accepted   = user satisfied. Closes the loop.
  * Near-miss records are persisted as epistemic Observations (source
    "acceptance_loop", type "near_miss") through EpistemicStore.save_observation.
    NOTE (interface): the campaign's frozen epistemic API
    (record_observation()/record_hypothesis()) is owned by M1 and has not
    landed yet. This module's _record_observation is the M6-side shim that
    calls the store method directly; it switches to the frozen API the moment
    M1 lands it. This module never writes to runtime/intellect/epistemic.py.
  * Pool expansion interrogates the search itself through real machinery:
    gap_reasoner.analyze (fresh re-decomposition), the primitive registry,
    and the planner's own hint-steering — with the near-miss's excluded
    approach signature removed from the pool. Classification is observed:
    untried steered candidates remain  -> POOL_LIMITED (the pool was the
    bottleneck; first-bind-wins stopped at the failure);
    nothing remains                    -> GENUINELY_ABSENT (honest "unmet").

Nothing here is simulated: every claim the loop makes (candidate presented,
verdict recorded, near-miss persisted, expansion classified) is backed by a
real store write and real tool output. The only simulated element in the
bench demonstration is the USER VERDICT itself, explicitly labeled as such —
the mechanism is what's proven, not the verdict.
"""
from __future__ import annotations

import json
import sqlite3
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional


# ---------------------------------------------------------------------------
# vocabularies
# ---------------------------------------------------------------------------

class AcceptanceState(Enum):
    CANDIDATE = "candidate"            # system completed, awaiting the user
    ACCEPTED = "accepted"              # user satisfied — loop closed
    REJECTED = "rejected"              # user dissatisfied — expansion reopened
    CLOSED_TERMINAL = "closed_terminal"  # one of James's five terminal conditions


class PoolVerdict(Enum):
    POOL_LIMITED = "pool_limited"        # untried candidates sat in the pool
    GENUINELY_ABSENT = "genuinely_absent"  # expanded search finds nothing new


SYSTEM_COMPLETED = "completed"  # the scheduler status this loop treats as candidate


# ---------------------------------------------------------------------------
# records
# ---------------------------------------------------------------------------

@dataclass
class AuthReport:
    """The authentication gate: held-out tests the attempt was not built
    against, negative controls, counterfactuals where applicable."""
    held_out: Dict[str, Any] = field(default_factory=dict)
    negative_controls: Dict[str, Any] = field(default_factory=dict)
    counterfactuals: Optional[Dict[str, Any]] = None
    passed: bool = False

    def as_dict(self) -> Dict[str, Any]:
        return {"held_out": self.held_out,
                "negative_controls": self.negative_controls,
                "counterfactuals": self.counterfactuals,
                "passed": self.passed}

    @staticmethod
    def from_dict(d: Dict[str, Any]) -> "AuthReport":
        return AuthReport(
            held_out=d.get("held_out", {}),
            negative_controls=d.get("negative_controls", {}),
            counterfactuals=d.get("counterfactuals"),
            passed=bool(d.get("passed", False)))


@dataclass
class Attempt:
    """One executed attempt at a goal."""
    approach_signature: List[str]          # e.g. planner ops_used
    plan: Dict[str, Any]
    args: Dict[str, Any] = field(default_factory=dict)
    result_summary: Any = None
    exec_ok: bool = False

    def as_dict(self) -> Dict[str, Any]:
        return {"approach_signature": self.approach_signature,
                "plan": self.plan, "args": self.args,
                "result_summary": self.result_summary,
                "exec_ok": self.exec_ok}

    @staticmethod
    def from_dict(d: Dict[str, Any]) -> "Attempt":
        return Attempt(
            approach_signature=list(d.get("approach_signature", [])),
            plan=d.get("plan", {}), args=d.get("args", {}),
            result_summary=d.get("result_summary"),
            exec_ok=bool(d.get("exec_ok", False)))


@dataclass
class NearMiss:
    """A useful learning attempt that did not fit the criteria: the failed
    attempt plus the user's feedback, characterizing what was close but
    wrong. Gives the next pool expansion a gradient instead of thrashing."""
    near_miss_id: str
    run_id: str
    goal: str
    attempt: Dict[str, Any]
    auth: Dict[str, Any]
    feedback_text: str
    unmet_criteria: List[str]
    close_but_wrong: Dict[str, Any]   # {"matched": [...], "missed": [...]}
    steers_away_from: List[str]       # approach signatures to exclude
    steers_toward: List[str]         # unmet criteria to search toward
    at: float = field(default_factory=time.time)

    def as_dict(self) -> Dict[str, Any]:
        return {"near_miss_id": self.near_miss_id, "run_id": self.run_id,
                "goal": self.goal, "attempt": self.attempt, "auth": self.auth,
                "feedback_text": self.feedback_text,
                "unmet_criteria": self.unmet_criteria,
                "close_but_wrong": self.close_but_wrong,
                "steers_away_from": self.steers_away_from,
                "steers_toward": self.steers_toward, "at": self.at}

    @staticmethod
    def from_dict(d: Dict[str, Any]) -> "NearMiss":
        # storage envelope: drop the facade's canonical provenance block
        d = {k: v for k, v in d.items()
             if k not in ("type", "_provenance")}
        return NearMiss(**d)


@dataclass
class PoolExpansion:
    goal: str
    excluded: List[str]
    candidates: List[Dict[str, Any]]  # [{"name","family","why"}] untried
    re_decomposed_gaps: List[str]
    verdict: PoolVerdict
    reason: str

    def as_dict(self) -> Dict[str, Any]:
        return {"goal": self.goal, "excluded": self.excluded,
                "candidates": self.candidates,
                "re_decomposed_gaps": self.re_decomposed_gaps,
                "verdict": self.verdict.value, "reason": self.reason}


@dataclass
class AcceptanceRecord:
    run_id: str
    goal: str
    system_status: str
    auth: AuthReport
    state: AcceptanceState = AcceptanceState.CANDIDATE
    rounds: int = 1
    near_miss_ids: List[str] = field(default_factory=list)
    attempt: Optional[Attempt] = None
    presented_at: float = field(default_factory=time.time)
    decided_at: Optional[float] = None
    close_reason: Optional[str] = None

    def as_dict(self) -> Dict[str, Any]:
        return {"run_id": self.run_id, "goal": self.goal,
                "system_status": self.system_status,
                "auth": self.auth.as_dict(), "state": self.state.value,
                "rounds": self.rounds, "near_miss_ids": self.near_miss_ids,
                "attempt": self.attempt.as_dict() if self.attempt else None,
                "presented_at": self.presented_at,
                "decided_at": self.decided_at,
                "close_reason": self.close_reason}

    @staticmethod
    def from_dict(d: Dict[str, Any]) -> "AcceptanceRecord":
        return AcceptanceRecord(
            run_id=d["run_id"], goal=d["goal"],
            system_status=d.get("system_status", ""),
            auth=AuthReport.from_dict(d.get("auth", {})),
            state=AcceptanceState(d.get("state", "candidate")),
            rounds=int(d.get("rounds", 1)),
            near_miss_ids=list(d.get("near_miss_ids", [])),
            attempt=Attempt.from_dict(d["attempt"]) if d.get("attempt") else None,
            presented_at=d.get("presented_at", time.time()),
            decided_at=d.get("decided_at"),
            close_reason=d.get("close_reason"))


# ---------------------------------------------------------------------------
# persistence: the acceptance overlay (own DB, own table — never the scheduler)
# ---------------------------------------------------------------------------

class AcceptanceStore:
    def __init__(self, db_path: str = "acceptance.db"):
        self.db_path = db_path
        with self._conn() as conn:
            conn.execute("""CREATE TABLE IF NOT EXISTS acceptance_records (
                run_id TEXT PRIMARY KEY, data TEXT NOT NULL,
                updated_at REAL)""")

    def _conn(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def save(self, rec: AcceptanceRecord) -> AcceptanceRecord:
        with self._conn() as conn:
            conn.execute("""INSERT OR REPLACE INTO acceptance_records
                (run_id, data, updated_at) VALUES (?,?,?)""",
                (rec.run_id, json.dumps(rec.as_dict()), time.time()))
        return rec

    def get(self, run_id: str) -> Optional[AcceptanceRecord]:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT data FROM acceptance_records WHERE run_id=?",
                (run_id,)).fetchone()
        return AcceptanceRecord.from_dict(json.loads(row["data"])) if row else None

    def all(self) -> List[AcceptanceRecord]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT data FROM acceptance_records ORDER BY updated_at").fetchall()
        return [AcceptanceRecord.from_dict(json.loads(r["data"])) for r in rows]


# ---------------------------------------------------------------------------
# the loop
# ---------------------------------------------------------------------------

class AcceptanceLoop:
    """Drives attempt -> completed(candidate) -> user verdict ->
    accepted | rejected -> steered pool expansion -> next attempt.

    `engine` (injected) supplies the real search machinery for expansion:
    engine.gap_reasoner, engine.primitives, engine.planner, engine.composer.
    `scheduler` (injected, read-only) supplies the system run record.
    `epistemic` (injected EpistemicStore) receives near-miss observations.
    """

    def __init__(self, store: AcceptanceStore, epistemic: Any,
                 engine: Any = None, scheduler: Any = None):
        self.store = store
        self.epistemic = epistemic
        self.engine = engine
        self.scheduler = scheduler

    # -- system side: presenting a completed attempt ---------------------

    def system_status_of(self, run_id: str) -> Optional[str]:
        if self.scheduler is None:
            return None
        rec = self.scheduler.get_run(run_id)
        return rec.get("status") if rec else None

    def present(self, run_id: str, goal: str, attempt: Attempt,
                auth: AuthReport) -> AcceptanceRecord:
        """A run reached system-completed and passed the authentication gate:
        it becomes a CANDIDATE awaiting the user's verdict. Completed is a
        candidate state, never terminal."""
        if not auth.passed:
            raise ValueError(
                "present: authentication gate did not pass; a failed attempt "
                "is not presentable as completed")
        # PLOOP-12: a completed attempt is presented ONCE. The store is
        # INSERT OR REPLACE, so without this guard a duplicate present
        # silently clobbers the record -- including an ACCEPTED verdict,
        # the user's word as ground truth. Duplicates are refused loudly;
        # a retry after rejection goes through present_retry (which keeps
        # the evidence chain intact).
        prior = self.store.get(run_id)
        if prior is not None:
            state_name = getattr(prior.state, "name", prior.state)
            if state_name == "CANDIDATE":
                why = ("a CANDIDATE is already awaiting verdict; "
                       "a duplicate present is refused")
            elif state_name == "ACCEPTED":
                why = ("the run is already ACCEPTED; the verdict stands, "
                       "a re-present cannot clobber it")
            elif state_name == "REJECTED":
                why = ("the run was REJECTED; re-present via present_retry "
                       "so the evidence chain stays intact")
            else:
                why = (f"the run is in terminal state {state_name}; "
                       "it cannot be re-presented")
            raise ValueError(
                f"present: run {run_id!r} was already presented: {why}")
        system_status = self.system_status_of(run_id) or SYSTEM_COMPLETED
        rec = AcceptanceRecord(run_id=run_id, goal=goal,
                               system_status=system_status, auth=auth,
                               state=AcceptanceState.CANDIDATE,
                               attempt=attempt,
                               presented_at=time.time())
        return self.store.save(rec)

    def present_retry(self, run_id: str, attempt: Attempt,
                      auth: AuthReport) -> AcceptanceRecord:
        """Re-present after a rejection as the next round, with the evidence
        chain INTACT: near_miss_ids and rounds carry forward (plain
        present() would wipe them via INSERT OR REPLACE). V10-P6 repair:
        without this, dissatisfaction -> near-miss -> steered retry ->
        accept is not queryable as one chain."""
        if not auth.passed:
            raise ValueError(
                "present_retry: authentication gate did not pass; a failed "
                "attempt is not presentable as completed")
        prior = self.store.get(run_id)
        if prior is None:
            raise KeyError(
                f"present_retry: no acceptance record for {run_id!r}; "
                "a retry with no prior round is a fabricated chain")
        if prior.state is AcceptanceState.ACCEPTED:
            raise ValueError(
                f"present_retry: run {run_id!r} is already accepted; "
                "a retry is only presentable after a rejection")
        rec = AcceptanceRecord(
            run_id=run_id, goal=prior.goal,
            system_status=self.system_status_of(run_id) or SYSTEM_COMPLETED,
            auth=auth, state=AcceptanceState.CANDIDATE,
            rounds=prior.rounds,
            near_miss_ids=list(prior.near_miss_ids),
            attempt=attempt, presented_at=time.time())
        return self.store.save(rec)

    # -- user side: the verdict ------------------------------------------

    def record_verdict(self, run_id: str, satisfied: bool,
                       feedback: str = "",
                       unmet_criteria: Optional[List[str]] = None
                       ) -> AcceptanceRecord:
        """The user's verdict. satisfied=True closes the loop (ACCEPTED).
        satisfied=False persists a near-miss record and reopens the loop at
        pool expansion (REJECTED); the caller then drives expand_pool and the
        next attempt. Only ACCEPTED — or close_terminal — ends the loop."""
        rec = self.store.get(run_id)
        if rec is None:
            raise KeyError(f"record_verdict: no acceptance record for {run_id!r}")
        if rec.state is AcceptanceState.CLOSED_TERMINAL:
            raise ValueError(
                f"record_verdict: run {run_id!r} is closed by terminal "
                f"condition ({rec.close_reason}); only James reopens it")
        if rec.state is AcceptanceState.ACCEPTED:
            if satisfied:
                return rec  # idempotent: still accepted
            # The user's latest word is ground truth: dissatisfaction after
            # acceptance reopens the loop at pool expansion, as a new round.
        elif rec.state not in (AcceptanceState.CANDIDATE,
                               AcceptanceState.REJECTED):
            raise ValueError(
                f"record_verdict: run {run_id!r} is {rec.state.value}; "
                "only a candidate (or a rejected round awaiting retry) "
                "can receive a verdict")
        if satisfied:
            rec.state = AcceptanceState.ACCEPTED
            rec.decided_at = time.time()
            rec.close_reason = "user_satisfied"
            return self.store.save(rec)
        # Dissatisfaction: characterize, persist, reopen.
        near_miss = self.characterize_near_miss(
            rec, feedback_text=feedback,
            unmet_criteria=list(unmet_criteria or []))
        self.persist_near_miss(near_miss)
        rec.near_miss_ids.append(near_miss.near_miss_id)
        rec.state = AcceptanceState.REJECTED
        rec.rounds += 1
        rec.decided_at = None
        rec.close_reason = None
        return self.store.save(rec)

    def close_terminal(self, run_id: str, reason: str) -> AcceptanceRecord:
        """One of James's five terminal conditions closed the loop instead
        of the user. The reason is recorded; it stays an OPEN condition in
        the overall system, not a completed boundary."""
        rec = self.store.get(run_id)
        if rec is None:
            raise KeyError(f"close_terminal: no acceptance record for {run_id!r}")
        rec.state = AcceptanceState.CLOSED_TERMINAL
        rec.decided_at = time.time()
        rec.close_reason = reason
        return self.store.save(rec)

    # -- near-miss characterization --------------------------------------

    def characterize_near_miss(self, rec: AcceptanceRecord,
                               feedback_text: str,
                               unmet_criteria: List[str]) -> NearMiss:
        """Turn the failed attempt + feedback into a gradient for the next
        search. The characterization is structural and honest: the attempt's
        real approach signature and auth evidence, the feedback verbatim,
        and the caller-supplied unmet criteria (the loop never invents
        criteria by parsing prose — that would be confabulation)."""
        attempt = rec.attempt.as_dict() if rec.attempt else {}
        sig = attempt.get("approach_signature", [])
        auth = rec.auth.as_dict()
        matched = []
        if auth.get("passed"):
            matched.append("passed the authentication gate "
                           "(held-out + negative controls)")
        if attempt.get("exec_ok"):
            matched.append(f"executed via {sig}")
        missed = list(unmet_criteria) or (
            ["user dissatisfied; no explicit unmet criteria supplied"]
            if feedback_text else ["no feedback supplied"])
        return NearMiss(
            near_miss_id="nm_" + uuid.uuid4().hex[:12],
            run_id=rec.run_id, goal=rec.goal, attempt=attempt, auth=auth,
            feedback_text=feedback_text,
            unmet_criteria=list(unmet_criteria),
            close_but_wrong={"matched": matched, "missed": missed},
            steers_away_from=list(sig),
            steers_toward=list(unmet_criteria))

    def persist_near_miss(self, near_miss: NearMiss):
        """Persist as a first-class experience record (retrievable as
        evidence) through the unified-memory facade -- not the store's
        raw observation API. The facade stamps the canonical provenance
        block; the near-miss fields stay in the raw payload unchanged, so
        near_misses_for_goal (which reads source + raw) is unaffected."""
        from swarm_engine.intellect.unified_memory import record_experience
        content = (f"near-miss on goal '{near_miss.goal}': attempt via "
                   f"{near_miss.attempt.get('approach_signature')} was "
                   f"close but wrong ({near_miss.feedback_text!r}); "
                   f"unmet: {near_miss.unmet_criteria}")
        return record_experience(
            self.epistemic,
            origin_loop="verification",
            kind="near_miss",
            content=content,
            raw={"type": "near_miss",
                 "near_miss_id": near_miss.near_miss_id,
                 **near_miss.as_dict()},
            source="acceptance_loop")

    def near_misses_for_goal(self, goal: str) -> List[NearMiss]:
        """Retrieve near-miss records for a goal from the epistemic store —
        the next expansion cross-references these (everything known)."""
        out = []
        for obs in self.epistemic.all_observations():
            raw = obs.raw or {}
            if (obs.source == "acceptance_loop"
                    and raw.get("type") == "near_miss"
                    and raw.get("goal") == goal):
                out.append(NearMiss.from_dict(raw))
        return out

    # -- pool expansion: interrogate the search itself -------------------

    @staticmethod
    def _canonical(engine: Any, names) -> set:
        """Primitive names, alias-resolved: `computation.sum` and `sum`
        are the same approach, and excluding one must exclude both."""
        out = set()
        for n in names:
            try:
                out.add(engine.primitives.resolve(n) or n)
            except Exception:
                out.add(n)
        return out

    def expand_pool(self, goal_text: str,
                    near_miss: NearMiss) -> PoolExpansion:
        """Reopen at pool expansion, constrained by the near-miss.

        Real machinery, in order:
          1. re-decompose: gap_reasoner.analyze(goal) fresh — the gap stage
             no longer just reports "unmet".
          2. cross-reference everything known: near-miss records for this
             goal are pulled from the epistemic store; their excluded
             signatures accumulate across rounds.
          3. steer: the pool is the planner's own search space —
             registry.producing(goal.wants_type), pure-only exactly as the
             planner's _search filters — ordered by the planner's own
             _hint_steers. The failed approach signatures are EXCLUDED;
             candidates whose names echo the unmet criteria (token-boundary,
             like the planner) sort first. The search moves away from the
             characterized failure, toward the unmet criteria.
          4. classify by observation: untried steered candidates remain ->
             POOL_LIMITED; nothing remains -> GENUINELY_ABSENT.
        """
        if self.engine is None:
            raise ValueError("expand_pool: no engine injected")
        from swarm_engine.synthesis.planner import (
            parse_goal, _hint_toksets, _hint_steers)

        goal = parse_goal(goal_text)
        graph = self.engine.gap_reasoner.analyze(goal_text)
        gaps = [g.name for g in graph.gaps()]

        excluded = set(near_miss.steers_away_from)
        for nm in self.near_misses_for_goal(goal_text):
            excluded.update(nm.steers_away_from)
        # Alias-aware: excluding `computation.sum` excludes `sum` too.
        excluded_canon = self._canonical(self.engine, excluded)

        # The planner's own pool: type-directed, pure-only (same filter as
        # Planner._search with allow_effects=False).
        pool = [p for p in self.engine.primitives.producing(goal.wants_type)
                if p.pure and self._canonical(self.engine, [p.name]).isdisjoint(excluded_canon)]
        toksets = _hint_toksets(set(goal.verbs) | set(goal.nouns))
        toward_toksets = _hint_toksets(set(near_miss.steers_toward))

        def _rank(p):
            return (0 if _hint_steers(toward_toksets, p.name) else 1,
                    0 if _hint_steers(toksets, p.name) else 1,
                    p.name)

        candidates = [
            {"name": p.name, "family": p.family,
             "why": ("matches unmet-criteria steering; " if _hint_steers(toward_toksets, p.name) else "")
                    + ("planner hint-steered for this goal" if _hint_steers(toksets, p.name) else "type-compatible with the goal")}
            for p in sorted(pool, key=_rank)]

        if candidates:
            verdict = PoolVerdict.POOL_LIMITED
            reason = (f"{len(candidates)} candidate(s) sat untried in the "
                      f"planner's own pool while the search bound first-win "
                      f"on {sorted(excluded)}")
        else:
            verdict = PoolVerdict.GENUINELY_ABSENT
            reason = ("expanded search (fresh decomposition + the planner's "
                      "own type-directed pool, failed approaches excluded) "
                      "surfaces no new candidate: the gap is genuine, not "
                      "pool-limited")
        return PoolExpansion(goal=goal_text, excluded=sorted(excluded_canon),
                             candidates=candidates,
                             re_decomposed_gaps=gaps, verdict=verdict,
                             reason=reason)

    # -- attempting -------------------------------------------------------

    def attempt(self, goal_text: str, args: Dict[str, Any],
                exclude: Optional[List[str]] = None) -> Attempt:
        """Plan with the real planner and execute with the real composer.
        `exclude` enforces the near-miss constraint: a proposal whose
        ops_used intersects the excluded signatures is refused and the
        caller must use expand_pool's explicit alternative instead of
        silently retrying the failure."""
        if self.engine is None:
            raise ValueError("attempt: no engine injected")
        proposal = self.engine.planner.best(goal_text)
        if proposal is None:
            raise RuntimeError(f"attempt: planner found no proposal for {goal_text!r}")
        sig = list(proposal.ops_used or [])
        if exclude and not self._canonical(self.engine, sig).isdisjoint(
                self._canonical(self.engine, exclude)):
            raise RuntimeError(
                f"attempt: planner re-bound the excluded approach {sig}; "
                "use expand_pool's explicit alternative instead")
        res = self.engine.composer.execute_sync(proposal.plan, args)
        ok = bool(res.get("success"))
        summary = res.get("value", res.get("result", res.get("error")))
        return Attempt(approach_signature=sig, plan=proposal.plan,
                       args=dict(args), result_summary=summary, exec_ok=ok)
