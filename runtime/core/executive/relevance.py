#!/usr/bin/env python3
"""Relevance ownership for the Primary executive (PLOOP-7).

Charter C-3: "a fact can be true and still be irrelevant." The Primary side
owns the FINAL relevance decision for anything entering its loops. This
module is that owner: a RelevanceGate that admits, retains, or rejects
findings at the Primary inlet, persisting every decision.

Verdicts:
  ADMITTED  relevant to the operational objective -> may enter Primary machinery
  RETAINED  true (or truth-unestablished) but irrelevant -> kept with its
            terminal state, never promoted, never silently discarded
  REJECTED  false (refuted) or malformed -> not admitted

Decision order per finding:
  1. malformed (missing provenance / terminal state outside the charter's
     enumerated set) -> REJECTED (charter C-2.2: inadmissible as evidence)
  2. truth standing REFUTED -> REJECTED. A refutation is still knowledge:
     it is retained in the store with its terminal state, but it is not
     admitted into the loop as a candidate.
  3. provenance link: the finding's bounded objective IS the current
     operational objective or a registered sub-objective -> ADMITTED.
     (Primary-requested evidence for this objective needs no further proof
     of relevance; the request itself established it.)
  4. content score: synonym-expanded token recall of the finding's content
     against the objective statement >= threshold -> ADMITTED, with the
     overlap terms recorded in the decision.
  5. otherwise -> RETAINED: true-but-irrelevant. Persisted with terminal
     state, flagged not-promoted.

Triage is not admission (charter C-3.3): a curiosity-side triage label
("propose" / "retain" / "mark-boundary") is recorded as advisory provenance
but never decides. Only this gate admits.

The threshold is a documented, tunable constant with a recorded rationale
(cost asymmetry: a missed relevant finding stalls a loop; an over-admitted
finding is filtered again by the loop's own validation). Every decision
records the score, the threshold, and the criterion that fired, so any
decision can be re-checked later.
"""

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List, Optional, Set, Tuple

from ...acquisition.semantic import expand, tokens


# ---------------------------------------------------------------------------
# Charter C-2.2: the enumerated terminal states a finding may carry.
# ---------------------------------------------------------------------------

TERMINAL_STATES: Tuple[str, ...] = (
    "QUESTION_RESOLVED",
    "HYPOTHESIS_SUPPORTED",
    "HYPOTHESIS_REFUTED",
    "DISCOVERY_VERIFIED",
    "CANDIDATE_GENERATED",
    "MODEL_REVISED",
    "NOVELTY_CLASSIFIED",
    "BOUNDARY_ESTABLISHED",
    "INSUFFICIENT_EVIDENCE",
    "INCONCLUSIVE",
    "BLOCKED",
)

#: Terminal states whose occurrence establishes the finding's claim.
_TRUTH_SUPPORTED_STATES = frozenset({
    "QUESTION_RESOLVED",
    "HYPOTHESIS_SUPPORTED",
    "DISCOVERY_VERIFIED",
    "MODEL_REVISED",
    "NOVELTY_CLASSIFIED",
    "BOUNDARY_ESTABLISHED",
})

#: Terminal states whose occurrence refutes the finding's claim.
_TRUTH_REFUTED_STATES = frozenset({
    "HYPOTHESIS_REFUTED",
})
# Every other enumerated state leaves truth unestablished (a candidate is
# not yet verified -- cf. charter C-6.4 -- and inconclusive/blocked say
# nothing about the claim either way).


class TruthStanding(Enum):
    SUPPORTED = "supported"
    REFUTED = "refuted"
    UNESTABLISHED = "unestablished"


class FindingRefused(Exception):
    """A finding is malformed: inadmissible as evidence (charter C-2.2)."""


class RelevanceRefused(Exception):
    """The relevance decision cannot be made: no gate installed, or no
    operational objective set. Fail closed -- nothing is admitted on a
    missing decision-maker."""


# ---------------------------------------------------------------------------
# Records
# ---------------------------------------------------------------------------

ADMITTED = "admitted"
RETAINED = "retained"
REJECTED = "rejected"
VERDICTS = (ADMITTED, RETAINED, REJECTED)


@dataclass
class FindingProvenance:
    """Where the finding came from. All three fields are required by
    charter C-2.2; triage is the curiosity side's advisory first pass
    (charter C-3.3) -- recorded, never decisive."""
    source_loop: str
    bounded_objective_id: str
    requested_by_primary: bool
    triage: Optional[str] = None  # "propose" | "retain" | "mark-boundary"


@dataclass
class Finding:
    finding_id: str
    content: str
    provenance: FindingProvenance
    terminal_state: str

    def validate(self) -> "Finding":
        """Charter C-2.2 admissibility: provenance present, terminal state
        from the enumerated set. Raises FindingRefused otherwise."""
        p = self.provenance
        if not (p.source_loop or "").strip():
            raise FindingRefused(
                f"finding {self.finding_id!r}: source_loop is required "
                "(a finding with no producing loop is ungrounded)")
        if not (p.bounded_objective_id or "").strip():
            raise FindingRefused(
                f"finding {self.finding_id!r}: bounded_objective_id is "
                "required (charter C-2.2)")
        if self.terminal_state not in TERMINAL_STATES:
            raise FindingRefused(
                f"finding {self.finding_id!r}: terminal_state "
                f"{self.terminal_state!r} is not in the enumerated set")
        return self

    def truth_standing(self) -> TruthStanding:
        if self.terminal_state in _TRUTH_REFUTED_STATES:
            return TruthStanding.REFUTED
        if self.terminal_state in _TRUTH_SUPPORTED_STATES:
            return TruthStanding.SUPPORTED
        return TruthStanding.UNESTABLISHED


@dataclass
class OperationalObjective:
    """The Primary operational objective that relevance is judged against."""
    objective_id: str
    statement: str
    sub_objectives: Tuple[str, ...] = ()
    _terms: Optional[Set[str]] = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if not (self.statement or "").strip():
            raise ValueError(
                "an operational objective requires a non-empty statement: "
                "relevance cannot be judged against nothing")

    def terms(self) -> Set[str]:
        if self._terms is None:
            self._terms = expand(tokens(self.statement))
        return set(self._terms)


@dataclass
class RelevanceDecision:
    finding_id: str
    objective_id: str
    verdict: str  # admitted | retained | rejected
    criterion: str  # malformed | refuted | provenance-link | content-score | below-threshold
    score: float
    threshold: float
    reason: str
    decided_at: float = field(default_factory=time.time)

    def as_dict(self) -> Dict[str, object]:
        return {
            "finding_id": self.finding_id,
            "objective_id": self.objective_id,
            "verdict": self.verdict,
            "criterion": self.criterion,
            "score": round(self.score, 4),
            "threshold": self.threshold,
            "reason": self.reason,
            "decided_at": self.decided_at,
        }


# ---------------------------------------------------------------------------
# The gate
# ---------------------------------------------------------------------------

#: Admission threshold on synonym-expanded token recall of the finding's
#: content against the objective statement. Recall-oriented (denominator is
#: the objective's term set): the cost asymmetry is deliberate -- a missed
#: relevant finding stalls a loop, while an over-admitted finding meets the
#: loop's own validation before it can do anything. Tunable; every decision
#: records the value in force so thresholds are auditable, not silent.
DEFAULT_THRESHOLD = 0.25

_SCHEMA = """
CREATE TABLE IF NOT EXISTS findings (
  finding_id TEXT PRIMARY KEY,
  content TEXT NOT NULL,
  source_loop TEXT NOT NULL,
  bounded_objective_id TEXT NOT NULL,
  requested_by_primary INTEGER NOT NULL,
  triage TEXT,
  terminal_state TEXT NOT NULL,
  truth_standing TEXT NOT NULL,
  admission TEXT NOT NULL,
  decided_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS decisions (
  seq INTEGER PRIMARY KEY AUTOINCREMENT,
  finding_id TEXT NOT NULL,
  objective_id TEXT NOT NULL,
  verdict TEXT NOT NULL,
  criterion TEXT NOT NULL,
  score REAL NOT NULL,
  threshold REAL NOT NULL,
  reason TEXT NOT NULL,
  decided_at REAL NOT NULL
);
"""


class RelevanceGate:
    """The Primary side's relevance owner (charter C-3). Constructed with
    the current operational objective and a sqlite store path; every
    decision is persisted and re-checkable."""

    def __init__(self, *, objective: OperationalObjective,
                 store_path: str,
                 threshold: float = DEFAULT_THRESHOLD) -> None:
        self._objective = objective
        self._store_path = store_path
        self._threshold = threshold
        with sqlite3.connect(self._store_path) as conn:
            conn.executescript(_SCHEMA)

    # -- configuration -------------------------------------------------
    @property
    def objective(self) -> OperationalObjective:
        return self._objective

    @property
    def threshold(self) -> float:
        return self._threshold

    def set_objective(self, objective: OperationalObjective) -> None:
        self._objective = objective

    # -- the decision ---------------------------------------------------
    def decide(self, finding: Finding) -> RelevanceDecision:
        """Judge one finding against the current operational objective.
        Persists the finding and the decision; returns the decision."""
        obj = self._objective
        if obj is None:
            raise RelevanceRefused(
                "no operational objective set: relevance cannot be judged")
        now = time.time()

        try:
            finding.validate()
        except FindingRefused as exc:
            return self._record(
                finding, obj, REJECTED, "malformed", 0.0,
                f"rejected as inadmissible evidence (charter C-2.2): {exc}",
                now)

        truth = finding.truth_standing()
        if truth is TruthStanding.REFUTED:
            # A refutation is knowledge -- retained with its terminal
            # state -- but it is not admitted into the loop as a candidate.
            return self._record(
                finding, obj, REJECTED, "refuted", 0.0,
                f"rejected: claim refuted (terminal_state="
                f"{finding.terminal_state}); retained in store as knowledge, "
                f"not admitted", now)

        prov = finding.provenance
        if prov.bounded_objective_id in (obj.objective_id, *obj.sub_objectives):
            return self._record(
                finding, obj, ADMITTED, "provenance-link", 1.0,
                f"admitted by provenance link: finding's bounded objective "
                f"{prov.bounded_objective_id!r} is the current operational "
                f"objective (requested_by_primary={prov.requested_by_primary})",
                now)

        score, overlap = self._content_score(finding)
        if score >= self._threshold:
            shown = sorted(overlap)[:12]
            return self._record(
                finding, obj, ADMITTED, "content-score", score,
                f"admitted by content score {score:.3f} >= {self._threshold} "
                f"on overlap terms {shown}",
                now)

        triage_note = (f"; curiosity triage was {prov.triage!r} -- triage is "
                       f"advisory only (charter C-3.3)"
                       if prov.triage else "")
        return self._record(
            finding, obj, RETAINED, "below-threshold", score,
            f"retained as true-but-irrelevant: content score {score:.3f} < "
            f"{self._threshold}, no provenance link to {obj.objective_id!r}; "
            f"kept with terminal_state={finding.terminal_state}, never "
            f"promoted{triage_note}",
            now)

    def _content_score(self, finding: Finding) -> Tuple[float, Set[str]]:
        fterms = expand(tokens(finding.content))
        oterms = self._objective.terms()
        if not oterms:
            return 0.0, set()
        overlap = fterms & oterms
        return len(overlap) / len(oterms), overlap

    def _record(self, finding: Finding, obj: OperationalObjective,
                verdict: str, criterion: str, score: float,
                reason: str, now: float) -> RelevanceDecision:
        decision = RelevanceDecision(
            finding_id=finding.finding_id,
            objective_id=obj.objective_id,
            verdict=verdict,
            criterion=criterion,
            score=score,
            threshold=self._threshold,
            reason=reason,
            decided_at=now)
        p = finding.provenance
        with sqlite3.connect(self._store_path) as conn:
            conn.execute(
                "INSERT OR REPLACE INTO findings "
                "(finding_id, content, source_loop, bounded_objective_id, "
                " requested_by_primary, triage, terminal_state, "
                " truth_standing, admission, decided_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                (finding.finding_id, finding.content, p.source_loop,
                 p.bounded_objective_id, int(p.requested_by_primary),
                 p.triage, finding.terminal_state,
                 finding.truth_standing().value, verdict, now))
            conn.execute(
                "INSERT INTO decisions "
                "(finding_id, objective_id, verdict, criterion, score, "
                " threshold, reason, decided_at) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (finding.finding_id, obj.objective_id, verdict, criterion,
                 score, self._threshold, reason, now))
        return decision

    # -- re-checking -----------------------------------------------------
    def decision_count(self) -> int:
        with sqlite3.connect(self._store_path) as conn:
            return conn.execute(
                "SELECT COUNT(*) FROM decisions").fetchone()[0]

    def get_decision(self, finding_id: str) -> Optional[RelevanceDecision]:
        with sqlite3.connect(self._store_path) as conn:
            row = conn.execute(
                "SELECT finding_id, objective_id, verdict, criterion, score, "
                "threshold, reason, decided_at FROM decisions "
                "WHERE finding_id = ? ORDER BY seq DESC LIMIT 1",
                (finding_id,)).fetchone()
        if row is None:
            return None
        return RelevanceDecision(
            finding_id=row[0], objective_id=row[1], verdict=row[2],
            criterion=row[3], score=row[4], threshold=row[5],
            reason=row[6], decided_at=row[7])

    def findings_by_admission(self, admission: str) -> List[Dict[str, object]]:
        with sqlite3.connect(self._store_path) as conn:
            rows = conn.execute(
                "SELECT finding_id, terminal_state, admission FROM findings "
                "WHERE admission = ? ORDER BY finding_id",
                (admission,)).fetchall()
        return [{"finding_id": r[0], "terminal_state": r[1],
                 "admission": r[2]} for r in rows]
