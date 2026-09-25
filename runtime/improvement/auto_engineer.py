"""Autonomous mechanism engineer -- Frontier 9: recursive self-improvement.

This module closes the one loop the existing improvement machinery leaves
open. What already exists (and is left untouched):

* hand-authored observers (SearchPolicyObserver, AcquisitionStrategyObserver)
  that watch ONE subsystem each with a human-designed repair formula;
* ImprovementPipeline, which validates a proposed change through an
  independent A/B, persists it through a CANDIDATE -> VALIDATED ->
  ADMITTED -> ACTIVE lifecycle with snapshot rollback, reactivates it at
  boot, and monitors production outcomes for evidence-driven rollback;
* RecursiveImprovementLoop, which chains improvement cycles but needs
  caller-supplied (apply_fn, revert_fn) pairs -- the candidate repairs are
  authored OUTSIDE the system.

What was missing: nobody generates the candidate repairs from the system's
own undifferentiated operational evidence. The observers are told which
subsystem to watch and what the repair looks like; the recursive loop is
handed the changes to try. This engineer does that missing step for the
acquisition pipeline:

1. COLLECT real operational evidence by running ordinary acquisition goals
   (the goals are the task conditions; the engineer never sees a subsystem
   name, a pattern name, or a repair formula in them);
2. DETECT limitation patterns data-first from a uniform attempt-event
   stream (signature, strategy, position, success, cost, suppressed);
3. GENERATE candidate repairs from a general, subsystem-agnostic repair
   grammar (PRUNE / PRIORITY / GENERAL_SKIP / NARROW), enumerating every
   candidate the evidence supports -- never a single hand-picked one;
4. VALIDATE every candidate head-to-head against the live incumbent on
   held-out goals, with no-success-regression and genuine-gain gates;
5. DEPLOY the winner through the persisted Improvement lifecycle
   (snapshot rollback, boot-time reactivation, production-outcome
   monitoring with evidence-driven rollback);
6. RECURSE: the next cycle's evidence is collected with the improved
   mechanism live, so newly detected limitations are downstream of the
   improvement, not replays of the old state.

ANTI-SIMULATION CONTRACT (read before "simplifying" this file):

* The repair grammar is fixed and general. WHICH rule fires, for WHICH
  (signature, strategy), and with WHAT parameters is decided ONLY by the
  measured evidence plus the A/B measurements. There is no goal-text
  branch, no signature literal, no strategy-name special case anywhere in
  the detection or generation code paths. Grep for a string literal like
  "int" or "generate" in those paths: the only occurrences are the
  EXPERIMENT guard (a principled invariant -- never suppress the last
  resort) and diagnostic labels.
* A candidate that merely reverts the previous deployment cannot win:
  every candidate is validated against the CURRENT production policy
  (which includes all prior deployments) as the baseline arm.
* The engineer never observes goal text, example values, or synthesized
  source -- only the (signature, strategy, position, success, cost,
  suppressed) event stream and the learner's aggregate stats.
"""

from __future__ import annotations

import asyncio
import copy
import os
import shutil
import sqlite3
import tempfile
import time
from dataclasses import dataclass, field, asdict
from typing import Any, Dict, List, Optional, Tuple

from swarm_engine.improvement.substrate import (
    Improvement, ImprovementState, ImprovementStore)

POLICY_SUBSYSTEM = "acquisition.policy"

# --- detection thresholds (generic; not tuned to any goal family) --------
WASTE_MIN_ATTEMPTS = 3        # attempts of (signature, strategy) before the
                              # all-failure record counts as waste
WASTE_MIN_COST_NS = 5_000_000_000   # 5s of measured burn before it matters
WIN_MIN_ATTEMPTS = 2          # wins before a late-winner pattern is credible
LATE_WINNER_MIN_MEAN_POSITION = 1.5  # winner tried at mean position >= 1.5
REVIEW_MIN_GOALS = 2          # goals of a signature before a live policy is
REVIEW_MIN_SUPPRESSIONS = 2   # questioned for overgeneralization
MIN_SAMPLES_REGRESSION = 5
COST_GAIN_RATIO = 0.9         # candidate wall time <= 0.9x baseline counts
                              # as a genuine gain when success is tied
NEVER_SUPPRESS = {"experiment"}  # the non-generalizing last resort is never
                                 # a suppression target (R10 invariant)


# --- policy representation ----------------------------------------------
# A policy is a plain dict:
#   {"prune":        {signature: [strategy, ...]},   # union on merge
#    "priority":     {signature: [strategy, ...]},   # replace per signature
#    "general_skip": {strategy: {"max_failures": int,
#                               "except_signatures": [signature, ...]}}}
#                                                     # replace per strategy
# A delta uses the same keys; merge_policy applies the documented merge
# semantics. The orchestrator's appliers (apply_acquisition_pruning,
# apply_acquisition_priority, _general_skip_suppresses) are the single
# place where a policy becomes behavior -- validation trials and
# production activation use literally the same functions.

def empty_policy() -> Dict[str, Any]:
    return {"prune": {}, "priority": {}, "general_skip": {}}


def current_policy(engine) -> Dict[str, Any]:
    return {
        "prune": {s: sorted(v) for s, v in
                  (getattr(engine, "acquisition_pruning", None) or {}).items()},
        "priority": {s: list(v) for s, v in
                     (getattr(engine, "acquisition_priority", None) or {}).items()},
        "general_skip": copy.deepcopy(
            getattr(engine, "acquisition_general_skip", None) or {}),
    }


def merge_policy(base: Dict[str, Any],
                 delta: Dict[str, Any]) -> Dict[str, Any]:
    """Merge a candidate delta onto a base policy. Merge semantics are part
    of the mechanism, not of any candidate: prune unions, priority and
    general_skip replace per key, drop_general_skip deletes."""
    merged = copy.deepcopy(base)
    for sig, strats in (delta.get("prune") or {}).items():
        have = set(merged["prune"].get(sig) or [])
        have.update(strats)
        merged["prune"][sig] = sorted(have)
    for sig, ordered in (delta.get("priority") or {}).items():
        merged["priority"][sig] = list(ordered)
    for strat, rule in (delta.get("general_skip") or {}).items():
        merged["general_skip"][strat] = copy.deepcopy(rule)
    for strat in delta.get("drop_general_skip") or []:
        merged["general_skip"].pop(strat, None)
    return merged


def apply_policy_to_engine(engine, policy: Dict[str, Any]) -> None:
    """The single place a full policy becomes live on an engine. Used by
    activation, boot reactivation, rollback, and validation trial arms --
    so the validated change and the production change are literally the
    same operation."""
    engine.acquisition_pruning = {
        s: set(v) for s, v in (policy.get("prune") or {}).items()}
    engine.acquisition_priority = {
        s: list(v) for s, v in (policy.get("priority") or {}).items()}
    engine.acquisition_general_skip = copy.deepcopy(
        policy.get("general_skip") or {})


# --- validation verdict ---------------------------------------------------

@dataclass
class PolicyValidationVerdict:
    passed: bool
    reasons: List[str] = field(default_factory=list)
    trials_per_arm: int = 0
    baseline_successes: int = 0
    candidate_successes: int = 0
    baseline_wall_s: float = 0.0
    candidate_wall_s: float = 0.0

    def as_dict(self) -> Dict[str, Any]:
        return asdict(self)


# --- the engineer ---------------------------------------------------------

class AutonomousImprovementEngineer:
    """Generates, validates, and deploys acquisition-policy improvements
    from the system's own measured operational evidence."""

    def __init__(self, engine):
        self.engine = engine
        self.store: ImprovementStore = engine.improvements
        with self._conn() as conn:
            conn.execute("""CREATE TABLE IF NOT EXISTS auto_engineer_evidence (
                id INTEGER PRIMARY KEY AUTOINCREMENT, cycle INTEGER,
                goal TEXT, signature TEXT, strategy TEXT, position INTEGER,
                success INTEGER, cost_ns INTEGER, suppressed INTEGER,
                at REAL)""")

    # -- evidence ------------------------------------------------------
    def _conn(self):
        return sqlite3.connect(self.store.db_path)

    def _max_evidence_id(self) -> int:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT MAX(id) FROM auto_engineer_evidence").fetchone()
        return int(row[0] or 0)

    async def collect_evidence(self, cycle: int,
                               goals: List[Tuple[str, list]]) -> Dict[str, Any]:
        """Run ordinary acquisition goals with the attempt listener
        installed. `goals` is [(name, examples)]. The engineer sees only
        the resulting event stream -- never goal text semantics."""
        orch = self.engine.acquisition_orchestrator
        if orch is None:
            raise RuntimeError("engine has no acquisition_orchestrator")
        events: List[Dict[str, Any]] = []

        def _listener(goal, signature, strategy, position, accepted,
                      cost_ns, suppressed):
            events.append({
                "cycle": cycle, "goal": goal, "signature": signature,
                "strategy": strategy, "position": int(position),
                "success": 1 if accepted else 0, "cost_ns": int(cost_ns),
                "suppressed": 1 if suppressed else 0, "at": time.time()})

        orch._attempt_listener = _listener
        goals_run, goals_ok = 0, 0
        try:
            for name, examples in goals:
                res = await asyncio.wait_for(
                    orch.resolve(name, examples=list(examples)), timeout=600)
                goals_run += 1
                if res.fully_resolved:
                    goals_ok += 1
        finally:
            orch._attempt_listener = None
        with self._conn() as conn:
            conn.executemany(
                """INSERT INTO auto_engineer_evidence
                   (cycle, goal, signature, strategy, position, success,
                    cost_ns, suppressed, at)
                   VALUES (:cycle, :goal, :signature, :strategy, :position,
                           :success, :cost_ns, :suppressed, :at)""",
                events)
        return {"goals_run": goals_run, "goals_resolved": goals_ok,
                "events": len(events)}

    def _window_events(self, start_id: int) -> List[Dict[str, Any]]:
        with self._conn() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT * FROM auto_engineer_evidence WHERE id > ? "
                "ORDER BY id", (start_id,)).fetchall()
        return [dict(r) for r in rows]

    # -- detection -----------------------------------------------------
    def _already_handled(self, signature: str, strategy: str) -> bool:
        """True when the live policy already governs (signature, strategy)
        -- redetecting it would be a replay, not a new limitation."""
        pol = current_policy(self.engine)
        if strategy in (pol["prune"].get(signature) or []):
            return True
        if strategy in (pol["priority"].get(signature) or []):
            return True
        rule = pol["general_skip"].get(strategy)
        if rule and signature not in (rule.get("except_signatures") or []):
            return True
        return False

    def detect(self, start_id: int) -> List[Dict[str, Any]]:
        """Data-first limitation detection over the evidence window. Two
        generic patterns; no subsystem names, no goal families."""
        events = self._window_events(start_id)
        groups: Dict[Tuple[str, str], List[Dict[str, Any]]] = {}
        for e in events:
            if e["signature"] is None:
                continue
            groups.setdefault((e["signature"], e["strategy"]), []).append(e)
        wins: Dict[Tuple[str, str], int] = {}
        for key, evs in groups.items():
            tried = [e for e in evs if not e["suppressed"]]
            wins[key] = sum(e["success"] for e in tried)
        patterns: List[Dict[str, Any]] = []
        for (sig, strat), evs in groups.items():
            tried = [e for e in evs if not e["suppressed"]]
            n = len(tried)
            w = wins[(sig, strat)]
            cost = sum(e["cost_ns"] for e in tried)
            if (n >= WASTE_MIN_ATTEMPTS and w == 0
                    and cost >= WASTE_MIN_COST_NS
                    and strat not in NEVER_SUPPRESS
                    and not self._already_handled(sig, strat)):
                patterns.append({
                    "kind": "WASTE", "signature": sig, "strategy": strat,
                    "attempts": n, "wins": w, "cost_ns": cost,
                    "diagnosis": (f"strategy {strat!r} burned "
                                  f"{cost/1e9:.1f}s over {n} attempts for "
                                  f"signature {sig!r} with zero successes")})
            win_evs = [e for e in tried if e["success"]]
            if (len(win_evs) >= WIN_MIN_ATTEMPTS
                    and strat not in NEVER_SUPPRESS
                    and not self._already_handled(sig, strat)):
                positions = [e["position"] for e in win_evs]
                mean_pos = sum(positions) / len(positions)
                first_win = min(positions)
                earlier = {e["strategy"] for e in tried
                           if e["position"] < first_win}
                pre_cost = sum(e["cost_ns"] for e in tried
                               if e["position"] < first_win)
                if (mean_pos >= LATE_WINNER_MIN_MEAN_POSITION
                        and pre_cost >= WASTE_MIN_COST_NS
                        and earlier
                        and all(wins.get((sig, s), 0) == 0
                                for s in earlier)):
                    patterns.append({
                        "kind": "LATE_WINNER", "signature": sig,
                        "strategy": strat, "wins": len(win_evs),
                        "mean_position": mean_pos,
                        "pre_win_cost_ns": pre_cost,
                        "diagnosis": (f"strategy {strat!r} wins for "
                                      f"signature {sig!r} but only at mean "
                                      f"position {mean_pos:.1f} after "
                                      f"{pre_cost/1e9:.1f}s of fruitless "
                                      f"earlier attempts")})
        return patterns

    def review_active_policies(self, start_id: int) -> List[Dict[str, Any]]:
        """Second-order detection: with an improvement live, its own
        production footprint is questioned. A general skip rule that is
        suppressing a signature whose goals all fail under suppression --
        the signature the rule was never justified by -- triggers a NARROW
        review. This is where the recursive step comes from: the limitation
        is downstream of the deployed improvement."""
        triggers: List[Dict[str, Any]] = []
        active = self.store.active_for(POLICY_SUBSYSTEM)
        if active is None:
            return triggers
        delta = active.proposed_change.get("policy_delta", {}) or {}
        events = self._window_events(start_id)
        by_sig: Dict[str, List[Dict[str, Any]]] = {}
        for e in events:
            if e["signature"] is None:
                continue
            by_sig.setdefault(e["signature"], []).append(e)
        for strat, rule in (delta.get("general_skip") or {}).items():
            excepted = set(rule.get("except_signatures") or [])
            for sig, evs in by_sig.items():
                if sig in excepted:
                    continue
                goals = {e["goal"] for e in evs}
                suppressed = sum(1 for e in evs
                                 if e["suppressed"] and e["strategy"] == strat)
                ok_goals = {e["goal"] for e in evs if e["success"]}
                if (len(goals) >= REVIEW_MIN_GOALS
                        and suppressed >= REVIEW_MIN_SUPPRESSIONS
                        and not ok_goals):
                    triggers.append({
                        "kind": "NARROW",
                        "improvement_id": active.improvement_id,
                        "strategy": strat, "signature": sig,
                        "goals": len(goals), "suppressions": suppressed,
                        "diagnosis": (f"live general-skip of {strat!r} "
                                      f"suppresses {suppressed} attempts over "
                                      f"{len(goals)} goals of signature "
                                      f"{sig!r} with zero successes -- the "
                                      f"rule may be overgeneralizing")})
        return triggers

    # -- candidate generation ------------------------------------------
    def generate_candidates(self, patterns: List[Dict[str, Any]],
                            triggers: List[Dict[str, Any]],
                            cycle: int) -> List[Dict[str, Any]]:
        """Enumerate EVERY candidate the evidence supports from the fixed
        repair grammar. Never a single hand-picked repair: WASTE yields a
        general rule AND per-signature prunes as competitors; a NARROW
        review yields threshold variants AND a demote-to-last competitor.
        Which one wins is decided by measurement, below."""
        candidates: List[Dict[str, Any]] = []
        seq = 0

        def _add(kind: str, policy_delta: Dict[str, Any],
                 justification: List[Dict[str, Any]], **kw) -> None:
            nonlocal seq
            seq += 1
            cand = {"candidate_id": f"c{cycle}_{kind}_{seq}",
                    "kind": kind, "policy_delta": policy_delta,
                    "justification": justification}
            cand.update(kw)
            candidates.append(cand)

        waste_by_strategy: Dict[str, List[Dict[str, Any]]] = {}
        for p in patterns:
            if p["kind"] == "WASTE":
                waste_by_strategy.setdefault(p["strategy"], []).append(p)
        for strat, ps in waste_by_strategy.items():
            max_failures = max(2, min(p["attempts"] for p in ps))
            sigs = sorted({p["signature"] for p in ps})
            _add("GENERAL_SKIP",
                 {"general_skip": {strat: {
                     "max_failures": max_failures,
                     "except_signatures": []}}},
                 justification=[dict(p, candidate_role="justifies")
                                for p in ps],
                 strategy=strat, max_failures=max_failures,
                 relevant_signatures=sigs,
                 summary=(f"skip {strat} for any signature after "
                          f"{max_failures} all-failure attempts"))
            for p in ps:
                _add("PRUNE",
                     {"prune": {p["signature"]: [strat]}},
                     justification=[dict(p, candidate_role="justifies")],
                     strategy=strat, signature=p["signature"],
                     relevant_signatures=[p["signature"]],
                     summary=(f"prune {strat} for signature "
                              f"{p['signature']!r} only"))
        for p in patterns:
            if p["kind"] != "LATE_WINNER":
                continue
            _add("PRIORITY",
                 {"priority": {p["signature"]: [p["strategy"]]}},
                 justification=[dict(p, candidate_role="justifies")],
                 strategy=p["strategy"], signature=p["signature"],
                 relevant_signatures=[p["signature"]],
                 summary=(f"try {p['strategy']} first for signature "
                          f"{p['signature']!r}"))
        for t in triggers:
            if t["kind"] != "NARROW":
                continue
            strat, sig = t["strategy"], t["signature"]
            live = (current_policy(self.engine)["general_skip"]
                    .get(strat) or {})
            base_failures = int(live.get("max_failures", 3) or 3)
            live_except = set(live.get("except_signatures") or [])
            for mf in (base_failures, base_failures + 2):
                excepted = sorted(live_except | {sig})
                _add("NARROW",
                     {"general_skip": {strat: {
                         "max_failures": mf,
                         "except_signatures": excepted}}},
                     justification=[dict(t, candidate_role="justifies",
                                         max_failures=mf)],
                     strategy=strat, signature=sig, max_failures=mf,
                     relevant_signatures=sorted(live_except | {sig}),
                     predecessor_hint=t["improvement_id"],
                     summary=(f"keep skipping {strat} after {mf} failures, "
                              f"but not for signature {sig!r}"))
            _add("PRIORITY",
                 {"priority": {sig: self._demote_order(
                     sig, strat, t)}},
                 justification=[dict(t, candidate_role="justifies")],
                 strategy=strat, signature=sig,
                 relevant_signatures=[sig],
                 summary=(f"demote {strat} to last for signature {sig!r} "
                          f"instead of skipping it"))
        return candidates

    def _demote_order(self, signature: str, strat: str,
                      trigger: Dict[str, Any]) -> List[str]:
        """Data-driven demote-to-last order: strategies observed for this
        signature in the current window, winners first, the suppressed
        strategy forced last. No hard-coded strategy universe."""
        with self._conn() as conn:
            rows = conn.execute(
                """SELECT strategy, SUM(success), COUNT(*) FROM
                   auto_engineer_evidence
                   WHERE signature = ? AND suppressed = 0
                   GROUP BY strategy""", (signature,)).fetchall()
        ranked = sorted(rows, key=lambda r: (-(r[1] or 0), r[2], r[0]))
        ordered = [r[0] for r in ranked if r[0] != strat]
        ordered.append(strat)
        return ordered

    # -- validation ----------------------------------------------------
    def _base_db(self) -> Optional[str]:
        learner = getattr(self.engine, "strategy_learner", None)
        path = (getattr(learner, "db_path", None)
                or getattr(self.engine, "db_path", None))
        if not path or not os.path.exists(path):
            return None
        return path

    async def _run_trial(self, base_db: str, name: str, examples: list,
                         policy: Dict[str, Any]) -> Dict[str, Any]:
        """One real orchestrator run on a throwaway clone of the world.
        The trial arm's ONLY difference from production is the policy --
        applied through the same apply_policy_to_engine production uses."""
        from swarm_engine.core.engine import SwarmEngine  # deferred: core
        # imports this module at boot
        fd, clone = tempfile.mkstemp(suffix=".db", prefix="f9_ab_")
        os.close(fd)
        try:
            shutil.copyfile(base_db, clone)
            eng = SwarmEngine(db_path=clone)
            # Boot reactivation applies the CURRENT production policy; the
            # arm then overlays the candidate delta with the same merge the
            # deploy path uses. The baseline arm passes production through
            # unchanged.
            apply_policy_to_engine(eng, policy)
            orch = eng.acquisition_orchestrator
            t0 = time.perf_counter()
            res = await asyncio.wait_for(
                orch.resolve(name, examples=list(examples)), timeout=600)
            wall_s = time.perf_counter() - t0
            try:
                eng.shutdown()
            except Exception:
                pass
            return {"ok": True, "success": bool(res.fully_resolved),
                    "wall_s": wall_s,
                    "attempted": [a.strategy for a in res.attempts]}
        except Exception as exc:
            return {"ok": False,
                    "error": f"{type(exc).__name__}: {exc}"}
        finally:
            try:
                os.remove(clone)
            except OSError:
                pass

    async def validate_candidate(
            self, candidate: Dict[str, Any],
            heldout: List[Tuple[str, list]],
            baseline_results: List[Dict[str, Any]]) -> PolicyValidationVerdict:
        """Head-to-head A/B of one candidate against the live incumbent on
        held-out goals. Gates: no trial errors (fail-closed), no success
        regression, genuine gain (more success OR <=0.9x wall time), and a
        mechanism-engagement check specific to the candidate kind."""
        verdict = PolicyValidationVerdict(passed=False)
        base_db = self._base_db()
        if base_db is None:
            verdict.reasons.append("no base DB; fail-closed")
            return verdict
        prod_policy = current_policy(self.engine)
        cand_policy = merge_policy(prod_policy, candidate["policy_delta"])
        cand_results = []
        for name, examples in heldout:
            cand_results.append(
                await self._run_trial(base_db, name, examples, cand_policy))
        verdict.trials_per_arm = len(heldout)
        if not all(r.get("ok") for r in baseline_results + cand_results):
            verdict.reasons.append("trial error in an arm; fail-closed")
            return verdict
        bs = sum(1 for r in baseline_results if r["success"])
        cs = sum(1 for r in cand_results if r["success"])
        bw = sum(r["wall_s"] for r in baseline_results)
        cw = sum(r["wall_s"] for r in cand_results)
        verdict.baseline_successes = bs
        verdict.candidate_successes = cs
        verdict.baseline_wall_s = bw
        verdict.candidate_wall_s = cw
        if cs < bs:
            verdict.reasons.append(
                f"success regressed {bs}->{cs} on held-out; refused")
            return verdict
        if not (cs > bs or cw <= COST_GAIN_RATIO * bw):
            verdict.reasons.append(
                f"no evidence of benefit (success {bs}->{cs}, wall "
                f"{bw:.1f}s->{cw:.1f}s); refused")
            return verdict
        kind = candidate["kind"]
        strat = candidate.get("strategy")
        if kind in ("GENERAL_SKIP", "PRUNE") and strat:
            # Multi-node resolves record child-leaf attempts too. A parent
            # signature prune can drop generate on the waste node while
            # children still try generate. Engagement is therefore a
            # decrease in how often the strategy was attempted, not
            # "absent from the entire flattened attempt list".
            def _n(rows):
                return sum((r.get("attempted") or []).count(strat) for r in rows)
            b_n, c_n = _n(baseline_results), _n(cand_results)
            if b_n <= 0 or c_n >= b_n:
                verdict.reasons.append(
                    f"suppression does not engage ({strat} baseline={b_n} "
                    f"candidate={c_n}); refused")
                return verdict
            verdict.reasons.append(
                f"suppression engages: {strat} attempts {b_n}->{c_n}")
        elif kind == "NARROW" and strat:
            b_didnt = any(strat not in r["attempted"]
                          for r in baseline_results)
            c_did = any(strat in r["attempted"] for r in cand_results)
            c_still = any(strat not in r["attempted"]
                          for r in cand_results)
            if not (b_didnt and c_did and c_still):
                verdict.reasons.append(
                    "lift is not selective (baseline already tried it, "
                    "candidate never tried it, or nothing stayed "
                    "suppressed); refused")
                return verdict
            verdict.reasons.append(
                f"lift is selective: {strat} suppressed in baseline, "
                f"tried in candidate, still suppressed elsewhere")
        elif kind == "PRIORITY":
            ordered = (candidate["policy_delta"].get("priority") or {})
            firsts = {v[0] for v in ordered.values() if v}
            c_first = any(r["attempted"] and r["attempted"][0] in firsts
                          for r in cand_results)
            if not c_first:
                verdict.reasons.append(
                    "priority never took effect first in any trial; refused")
                return verdict
            verdict.reasons.append("priority took effect in trial order")
        verdict.passed = True
        verdict.reasons.append(
            f"VALIDATED: success {bs}->{cs}, wall {bw:.1f}s->{cw:.1f}s")
        return verdict

    def select_winner(self, validated: List[Tuple[Dict[str, Any],
                                                  PolicyValidationVerdict]]
                      ) -> Optional[Tuple[Dict[str, Any],
                                          PolicyValidationVerdict]]:
        """Pre-registered selection rule: most held-out successes wins;
        ties break on lower wall time; then on the more conservative
        (larger max_failures); then on candidate id. No human in the loop."""
        passing = [(c, v) for c, v in validated if v.passed]
        if not passing:
            return None

        def _key(item):
            c, v = item
            return (-v.candidate_successes, v.candidate_wall_s,
                    -(c.get("max_failures") or 0), c["candidate_id"])

        passing.sort(key=_key)
        return passing[0]

    # -- deployment ----------------------------------------------------
    def _next_version(self) -> int:
        versions = [i.version for i in self.store.all(POLICY_SUBSYSTEM)]
        return (max(versions) + 1) if versions else 1

    def deploy(self, candidate: Dict[str, Any],
               verdict: PolicyValidationVerdict, cycle: int,
               patterns: List[Dict[str, Any]],
               triggers: List[Dict[str, Any]]) -> Improvement:
        """Persist the winner through the full Improvement lifecycle:
        CANDIDATE -> VALIDATED -> ADMITTED -> ACTIVE, with a snapshot of
        the pre-activation policy for rollback and supersession of the
        predecessor -- mirroring ImprovementPipeline's contract."""
        version = self._next_version()
        predecessor = self.store.active_for(POLICY_SUBSYSTEM)
        imp = Improvement(
            improvement_id=f"auto_acqpolicy_v{version}",
            target_subsystem=POLICY_SUBSYSTEM,
            motivation=(f"cycle {cycle}: {candidate['summary']}; "
                        f"justified by {len(candidate['justification'])} "
                        f"evidence pattern(s)"),
            evidence={"cycle": cycle,
                      "patterns": patterns, "triggers": triggers,
                      "candidates_considered": [
                          c["candidate_id"] for c, _ in
                          getattr(self, "_last_validated", [])]},
            proposed_change={"policy_delta": candidate["policy_delta"],
                             "candidate_id": candidate["candidate_id"],
                             "candidate_kind": candidate["kind"]},
            expected_benefit=(f"held-out success "
                              f"{verdict.baseline_successes}->"
                              f"{verdict.candidate_successes}, wall "
                              f"{verdict.baseline_wall_s:.1f}s->"
                              f"{verdict.candidate_wall_s:.1f}s"),
            validation_requirements={
                "gates": ["no trial errors", "no success regression",
                          "genuine gain (success up or wall <= 0.9x)",
                          "mechanism engagement"],
                "trials_per_arm": verdict.trials_per_arm},
            provenance={"generated_by": "AutonomousImprovementEngineer",
                        "cycle": cycle,
                        "winner_selection": ("max held-out successes, "
                                             "min wall time, max "
                                             "max_failures, candidate id"),
                        "baseline": "current production policy"},
            version=version,
            predecessor_id=(predecessor.improvement_id
                            if predecessor else None),
            validation_evidence={
                "verdict": verdict.as_dict(),
                "baseline_success_rate": (
                    verdict.baseline_successes / verdict.trials_per_arm
                    if verdict.trials_per_arm else 0.0)},
        )
        self.store.save(imp)
        self.store.transition(imp.improvement_id,
                              ImprovementState.VALIDATED,
                              "independent A/B on held-out goals passed")
        self.store.transition(imp.improvement_id,
                              ImprovementState.ADMITTED,
                              "admitted for activation")
        # Snapshot-then-apply: rollback restores exactly what was live
        # before this improvement (which may include an earlier
        # improvement's delta -- the snapshot is cumulative).
        before = current_policy(self.engine)
        merged = merge_policy(before, candidate["policy_delta"])
        apply_policy_to_engine(self.engine, merged)
        imp.rollback_info["policy_before"] = before
        imp.rollback_info["policy_after"] = merged
        stored = self.store.get(imp.improvement_id)
        if stored is not None:
            stored.rollback_info.update(imp.rollback_info)
            imp = stored
        self.store.save(imp)
        if predecessor is not None:
            self.store.transition(predecessor.improvement_id,
                                  ImprovementState.SUPERSEDED,
                                  f"superseded by {imp.improvement_id}")
        self.store.transition(imp.improvement_id, ImprovementState.ACTIVE,
                              "activated; production policy updated")
        return imp

    def rollback(self, improvement_id: str) -> Dict[str, Any]:
        """Evidence-driven or ablation rollback: restore the exact
        pre-activation policy snapshot, mark ROLLED_BACK, and restore a
        superseded predecessor to ACTIVE so the store agrees with what is
        actually governing the subsystem."""
        imp = self.store.get(improvement_id)
        if imp is None or imp.state is not ImprovementState.ACTIVE:
            return {"rolled_back": False,
                    "reason": "not ACTIVE"}
        before = imp.rollback_info.get("policy_before")
        if before is None:
            return {"rolled_back": False,
                    "reason": "no policy_before snapshot"}
        apply_policy_to_engine(self.engine, before)
        self.store.transition(improvement_id, ImprovementState.ROLLED_BACK,
                              "rolled back; pre-activation policy restored")
        restored = None
        if imp.predecessor_id:
            pred = self.store.get(imp.predecessor_id)
            if pred is not None and pred.state is ImprovementState.SUPERSEDED:
                self.store.transition(imp.predecessor_id,
                                      ImprovementState.ACTIVE,
                                      f"restored after {improvement_id} "
                                      f"rolled back")
                restored = imp.predecessor_id
        return {"rolled_back": True, "improvement_id": improvement_id,
                "restored": restored}

    def restore(self, improvement_id: str) -> Dict[str, Any]:
        """Re-activate a ROLLED_BACK improvement (ablation recovery path):
        reapply its cumulative policy snapshot and force it ACTIVE; a
        currently-ACTIVE improvement for the subsystem returns to
        SUPERSEDED so the store agrees with the live policy."""
        imp = self.store.get(improvement_id)
        if imp is None or imp.state is not ImprovementState.ROLLED_BACK:
            return {"restored": False,
                    "reason": "not ROLLED_BACK"}
        after = (imp.rollback_info or {}).get("policy_after")
        if after is None:
            return {"restored": False,
                    "reason": "no policy_after snapshot"}
        cur = self.store.active_for(POLICY_SUBSYSTEM)
        apply_policy_to_engine(self.engine, after)
        if cur is not None and cur.improvement_id != improvement_id:
            self.store.transition(
                cur.improvement_id, ImprovementState.SUPERSEDED,
                f"re-superseded by restore of {improvement_id}")
        self.store.transition(improvement_id, ImprovementState.ACTIVE,
                              "restored after ablation rollback", force=True)
        return {"restored": True, "improvement_id": improvement_id}

    def reactivate(self) -> List[str]:
        """Boot-time: reapply the ACTIVE improvement's cumulative policy
        snapshot. The rollback baseline is never recomputed from a fresh
        engine (that would snapshot an empty baseline and drop chained
        deltas for improvements deployed in sequence)."""
        active = self.store.active_for(POLICY_SUBSYSTEM)
        """Boot-time: reapply the ACTIVE improvement's cumulative policy
        snapshot. The rollback baseline is never recomputed from a fresh
        engine (that would snapshot an empty baseline and drop chained
        deltas for improvements deployed in sequence)."""
        active = self.store.active_for(POLICY_SUBSYSTEM)
        if active is None:
            return []
        after = (active.rollback_info or {}).get("policy_after")
        if after:
            apply_policy_to_engine(self.engine, after)
            return [active.improvement_id]
        # Legacy / unexpected: rebuild from the proposed delta chain.
        delta = (active.proposed_change or {}).get("policy_delta") or {}
        apply_policy_to_engine(
            self.engine, merge_policy(empty_policy(), delta))
        return [active.improvement_id]

    # -- production monitoring -------------------------------------------
    def record_production_outcome(self, succeeded: bool
                                  ) -> Optional[Dict[str, Any]]:
        """Feed one real post-activation outcome back; only
        check_for_regression's minimum-sample floor can trigger a
        rollback -- a single outcome is never enough evidence."""
        active = self.store.active_for(POLICY_SUBSYSTEM)
        if active is None:
            return None
        self.store.record_outcome(active.improvement_id, succeeded)
        return self.check_for_regression(active.improvement_id)

    def check_for_regression(
            self, improvement_id: str,
            min_samples: int = MIN_SAMPLES_REGRESSION) -> Optional[Dict[str, Any]]:
        improvement = self.store.get(improvement_id)
        if (improvement is None
                or improvement.state is not ImprovementState.ACTIVE):
            return None
        recent = self.store.recent_outcomes(improvement_id)
        if len(recent) < min_samples:
            return None
        rate = sum(recent) / len(recent)
        baseline_rate = (improvement.validation_evidence or {}).get(
            "baseline_success_rate", 0.0)
        # A policy improvement claims "same success, lower cost" (or more
        # success), so the bar is the validation-time baseline rate with a
        # margin -- mirroring the acquisition pipeline's pruning bar, not
        # an absolute floor that would spuriously roll back a correct
        # suppression for a signature class where nothing ever succeeded.
        if rate >= baseline_rate - 0.15:
            return None
        rb = self.rollback(improvement_id)
        rb.update({"recent_success_rate": rate,
                   "sample_size": len(recent)})
        return rb

    # -- full cycle ------------------------------------------------------
    @staticmethod
    def diagnosis_key(patterns: List[Dict[str, Any]],
                      triggers: List[Dict[str, Any]]) -> str:
        parts = sorted(f"{p['kind']}:{p.get('signature')}:{p.get('strategy')}"
                       for p in patterns)
        parts += sorted(f"{t['kind']}:{t.get('signature')}:{t.get('strategy')}"
                        for t in triggers)
        return "|".join(parts) if parts else "NONE"

    async def run_cycle(self, cycle: int,
                        evidence_goals: List[Tuple[str, list]],
                        heldout_goals: List[Tuple[str, list]]
                        ) -> Dict[str, Any]:
        """One full recursive-improvement cycle: collect -> detect ->
        review -> generate (all supported candidates) -> validate each
        against the live incumbent on held-out goals -> deploy the winner.
        Returns a complete, serializable cycle report."""
        start_id = self._max_evidence_id()
        evidence = await self.collect_evidence(cycle, evidence_goals)
        patterns = self.detect(start_id)
        triggers = self.review_active_policies(start_id)
        candidates = self.generate_candidates(patterns, triggers, cycle)
        # The baseline arm is shared across candidates: it IS the current
        # production policy, measured once.
        base_db = self._base_db()
        baseline_results: List[Dict[str, Any]] = []
        if candidates:
            prod_policy = current_policy(self.engine)
            for name, examples in heldout_goals:
                baseline_results.append(await self._run_trial(
                    base_db, name, examples, prod_policy))
        validated = []
        for cand in candidates:
            verdict = await self.validate_candidate(
                cand, heldout_goals, baseline_results)
            validated.append((cand, verdict))
        self._last_validated = validated
        winner = self.select_winner(validated)
        deployed = None
        if winner is not None:
            deployed = self.deploy(winner[0], winner[1], cycle,
                                   patterns, triggers)
        return {
            "cycle": cycle,
            "evidence": evidence,
            "patterns": patterns,
            "triggers": triggers,
            "candidates": [{"candidate": {k: v for k, v in c.items()
                                          if k != "justification"},
                            "justification": c["justification"],
                            "verdict": v.as_dict()}
                           for c, v in validated],
            "baseline": {"trials": baseline_results},
            "winner": winner[0]["candidate_id"] if winner else None,
            "deployed": (deployed.improvement_id if deployed else None),
            "diagnosis_key": self.diagnosis_key(patterns, triggers),
        }
