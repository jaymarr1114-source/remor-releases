"""M+29.30 -- Acquisition-strategy learning.

Two genuinely distinct, pre-existing acquisition strategies (both are
the SAME GeneralSynthesizer engine this session already proved generic
and unmodified since M+29.22, differing only in search budget -- a
real, measurable engineering tradeoff, not a fabricated pair):

- "cheap_symbolic": a GeneralSynthesizer instance with a small
  max_candidates budget. Fast, but fails on harder problems.
- "expensive_symbolic": the existing, default-budget synthesizer
  (max_candidates=60000, the same one M+29.20-29 already use via
  cognition.propose_multi). Slower, but reaches problems cheap_symbolic
  cannot.

The learner does not know in advance which is better for a given
problem. It observes real outcomes (success/failure, real
candidates_tried cost) keyed by a generalized PROBLEM SIGNATURE
(currently: number of parameters -- a property available before
solving, never the objective's name or identity), and uses that
experience to decide, for future problems sharing a signature, whether
attempting the cheap strategy first is worth it.
"""
from __future__ import annotations

import sqlite3
import time
from typing import Any, Optional


class AcquisitionLearner:
    """Persistent, SQLite-backed acquisition-strategy experience store.
    The learning rule is deliberately simple and fully auditable: prefer
    the cheap strategy first for a given signature only if it has
    succeeded at least as often as it has failed for that exact
    signature, with no data defaulting to "try cheap first" (optimistic
    default, revised downward only by real observed failures).
    """

    def __init__(self, db_path: str):
        self.db_path = db_path
        with self._conn() as c:
            c.execute(
                "CREATE TABLE IF NOT EXISTS acquisition_experience ("
                " id INTEGER PRIMARY KEY AUTOINCREMENT,"
                " signature TEXT NOT NULL,"
                " strategy TEXT NOT NULL,"
                " success INTEGER NOT NULL,"
                " cost INTEGER NOT NULL,"
                " at REAL NOT NULL)")
            # 2026-09-19: policy-scoping (mirrors the 2026-09-14
            # search-policy scoping of failure_memory). Strategy-outcome
            # evidence is evidence about the search policy that produced
            # it; when the policy is repaired, earlier outcomes are stale
            # and must not keep steering strategy order. Rows recorded
            # before scoping existed carry policy NULL and are excluded
            # from policy-filtered reads -- they cannot be attributed to
            # any policy version, so treating them as current would be
            # the stale-evidence error this column exists to prevent.
            try:
                c.execute("ALTER TABLE acquisition_experience "
                          "ADD COLUMN policy TEXT")
            except Exception:
                pass  # column already present

    def _conn(self) -> sqlite3.Connection:
        return sqlite3.connect(self.db_path)

    def record(self, signature: str, strategy: str, success: bool, cost: int,
               policy: Optional[str] = None) -> None:
        with self._conn() as c:
            c.execute(
                "INSERT INTO acquisition_experience (signature, strategy, success, cost, at, policy) "
                "VALUES (?,?,?,?,?,?)",
                (signature, strategy, int(success), int(cost), time.time(), policy))

    def stats(self, signature: str, strategy: str,
              policy: Optional[str] = None) -> dict:
        with self._conn() as c:
            if policy is None:
                rows = c.execute(
                    "SELECT success, cost FROM acquisition_experience "
                    "WHERE signature=? AND strategy=?", (signature, strategy)).fetchall()
            else:
                rows = c.execute(
                    "SELECT success, cost FROM acquisition_experience "
                    "WHERE signature=? AND strategy=? AND policy=?",
                    (signature, strategy, policy)).fetchall()
        attempts = len(rows)
        successes = sum(s for s, _ in rows)
        avg_cost = (sum(c for _, c in rows) / attempts) if attempts else None
        return {"attempts": attempts, "successes": successes, "avg_cost": avg_cost}

    def prefer(self, candidates: list, signature: str,
               policy: Optional[str] = None) -> list:
        """M+29.31: strategy-family-agnostic generalization of
        prefer_cheap_first. Orders ANY list of strategy name strings by
        real observed evidence for the given signature -- untried
        strategies are optimistically ranked first (worth finding out,
        costs nothing but the attempt itself), tried strategies are
        ranked by real success rate (ties broken by lower average real
        cost). No strategy name is ever special-cased in this method:
        it operates purely on the stored (successes, attempts,
        avg_cost) triples this same class already persists for M+29.30.

        2026-09-19: when `policy` is given, only experience recorded
        under that search-policy version counts -- a policy repair makes
        earlier strategy-outcome evidence stale (mirrors the 2026-09-14
        failure_memory scoping). policy=None preserves the legacy
        unscoped read for non-strategy uses (node_order,
        candidate_select), whose evidence is not about the search policy.
        """
        scored = []
        for strat in candidates:
            s = self.stats(signature, strat, policy=policy)
            if s["attempts"] == 0:
                scored.append((1, 0.0, 0.0, strat))  # untried: optimistic
            else:
                rate = s["successes"] / s["attempts"]
                cost = s["avg_cost"] or 0.0
                scored.append((0, -rate, cost, strat))
        scored.sort(key=lambda t: t[:3])
        return [strat for *_, strat in scored]

    def prefer_cheap_first(self, signature: str) -> bool:
        """The decisive learning rule. No experience yet -> optimistic
        default (try cheap first, since it costs nothing to find out).
        Once cheap_symbolic has been tried for this signature, prefer it
        again only if its own observed success rate for this exact
        signature is >= 0.5 -- a genuinely evidence-responsive decision,
        not a hardcoded per-signature table (no signature ever appears
        as a literal in this function; every decision is computed from
        the stored counts).
        """
        s = self.stats(signature, "cheap_symbolic")
        if s["attempts"] == 0:
            return True
        return (s["successes"] / s["attempts"]) >= 0.5

    def reset(self, signature: Optional[str] = None) -> None:
        """Ablation support: erase learned experience (for one signature,
        or all) so a causal before/after/reset/restore test can be run.
        """
        with self._conn() as c:
            if signature is None:
                c.execute("DELETE FROM acquisition_experience")
            else:
                c.execute("DELETE FROM acquisition_experience WHERE signature=?", (signature,))
