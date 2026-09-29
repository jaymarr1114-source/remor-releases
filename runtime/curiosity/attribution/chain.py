"""CUR-P1D — the attribution chain.

FRM amendment §7 requires the full chain:

    resource allocation → actual expenditure → specific work →
    produced result → Primary evaluation → acceptance/admission →
    accepted capability yield

and that attribution preserve: resource expenditure; monetary
expenditure where available; originating executive; originating Run
Controller; originating inquiry/work unit; model/provider;
acceptance/admission record; and the causal relationship between
expenditure and accepted result.

FRM amendment §6 (yield): a result contributes positive yield when it
produces an acceptance or capability-admission record through the
established Primary mechanisms. Truth alone is not yield. Generation
alone is not yield. Execution alone is not yield.

Yield rules implemented here (all enforced in recompute, the function
the causal gate exercises):
    R1  unaccepted work accrues cost but zero yield
        (acceptance_record_id is null → attributed_yield = 0).
    R2  retries: a failed attempt's spend stays with the failed
        attempt's link. It is never silently folded into the
        successful attempt's yield. Attempts are separate work units.
    R3  partial results: yield is proportional to the accepted
        portion only (attributed_yield = accepted_portion ×
        admitted_yield).
    R4  reused capabilities: the acquisition's spend attributes to the
        acquiring work unit's yield ONLY. A reusing work unit's link
        bills its own marginal expenditure only — the second use
        never re-bills the first acquisition's spend. (Rule stated
        explicitly per the mandate; enforced in link assembly.)
    R5  nested microcontrollers: child spend aggregates upward into
        the parent exactly once. Aggregation walks parent_work_ref
        links and sums each descendant's own (non-aggregated)
        expenditure exactly one time.

The chain is recomputed from the ledgers — never cached — so the
causal gate's counterfactuals (remove/alter an expenditure, recompute)
measure genuine causal dependence, not ledger-ID echo.
"""
from __future__ import annotations

import sqlite3
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from .spend import Expenditure


# ---------------------------------------------------------------------------
# records
# ---------------------------------------------------------------------------

RESULT_OUTCOMES = ("success", "partial", "failed")

ADMISSION_VERDICTS = ("accepted", "retained", "rejected")


@dataclass
class WorkUnit:
    """Specific work performed under a grant allocation. attempt_no
    distinguishes retries (R2); parent_work_ref nests microcontrollers
    (R5); acquired_capability_id marks an acquisition (R4)."""
    work_ref: str
    inquiry_id: str
    grant_id: str
    controller_id: str
    attempt_no: int = 1
    parent_work_ref: Optional[str] = None
    kind: str = "execution"          # execution | acquisition | reuse
    acquired_capability_id: Optional[str] = None
    started_at: float = field(default_factory=time.time)


@dataclass
class Result:
    """The produced result of a work unit."""
    result_ref: str
    work_ref: str
    outcome: str                    # success | partial | failed
    evidence_ref: Optional[str] = None   # e.g. curiosity evidence_id (P1A)
    detail: str = ""
    produced_at: float = field(default_factory=time.time)

    def validate(self) -> "Result":
        if self.outcome not in RESULT_OUTCOMES:
            raise ValueError(f"result outcome {self.outcome!r} not in {RESULT_OUTCOMES}")
        return self


@dataclass
class AdmissionRecord:
    """Acceptance/admission record — the yield authority's verdict.

    In Phase 1 this is written by the TestAdmissionBoard test double
    (Primary Acceptance arrives Phase 4+). The record shape is the
    measurement the FRM consumes; the writer is labeled, never the
    authority itself.

    accepted_portion in (0, 1]: for partial results the fraction of
    the result the authority accepted (R3)."""
    admission_id: str
    result_ref: str
    verdict: str                    # accepted | retained | rejected
    yield_value: float              # value the authority assigns on acceptance
    accepted_portion: float = 1.0
    decided_by: str = "test_admission_board"
    decided_at: float = field(default_factory=time.time)

    def validate(self) -> "AdmissionRecord":
        if self.verdict not in ADMISSION_VERDICTS:
            raise ValueError(f"verdict {self.verdict!r} not in {ADMISSION_VERDICTS}")
        if self.verdict == "accepted":
            if not (0.0 < self.accepted_portion <= 1.0):
                raise ValueError("accepted_portion must be in (0, 1]")
            if self.yield_value < 0:
                raise ValueError("yield_value cannot be negative")
        return self


@dataclass
class AttributionLink:
    """The frozen attribution-link shape:
    {grant_id, expenditure_ids: [], work_ref, result_ref,
     acceptance_record_id: null|uuid, attributed_yield: float}
    plus the FRM §7 provenance the chain must preserve."""
    grant_id: str
    expenditure_ids: List[str]
    work_ref: str
    result_ref: str
    acceptance_record_id: Optional[str]
    attributed_yield: float
    # FRM §7 provenance, preserved with the link:
    originating_executive: str
    controller_id: str
    inquiry_id: str
    model_provider: str
    total_cpu_seconds: float
    total_monetary_cost: float
    cost_kind: str
    attempt_no: int
    computed_at: float = field(default_factory=time.time)


# ---------------------------------------------------------------------------
# chain ledger
# ---------------------------------------------------------------------------

_SCHEMA = """
CREATE TABLE IF NOT EXISTS work_units (
    work_ref TEXT PRIMARY KEY,
    inquiry_id TEXT NOT NULL,
    grant_id TEXT NOT NULL,
    controller_id TEXT NOT NULL,
    attempt_no INTEGER NOT NULL,
    parent_work_ref TEXT,
    kind TEXT NOT NULL,
    acquired_capability_id TEXT,
    started_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS results (
    result_ref TEXT PRIMARY KEY,
    work_ref TEXT NOT NULL,
    outcome TEXT NOT NULL,
    evidence_ref TEXT,
    detail TEXT NOT NULL,
    produced_at REAL NOT NULL,
    FOREIGN KEY (work_ref) REFERENCES work_units(work_ref)
);
CREATE TABLE IF NOT EXISTS admissions (
    admission_id TEXT PRIMARY KEY,
    result_ref TEXT NOT NULL,
    verdict TEXT NOT NULL,
    yield_value REAL NOT NULL,
    accepted_portion REAL NOT NULL,
    decided_by TEXT NOT NULL,
    decided_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS capability_acquisitions (
    capability_id TEXT PRIMARY KEY,
    acquiring_work_ref TEXT NOT NULL,
    acquisition_expenditure_ids TEXT NOT NULL
);
"""


class ChainLedger:
    """Holds work units, results, admissions, and capability
    acquisitions. The AttributionLink for any work_ref is recomputed
    from these ledgers plus the expenditure ledger — never cached."""

    def __init__(self, db_path: str):
        self.db_path = db_path
        self._conn = sqlite3.connect(db_path)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    # -- work -----------------------------------------------------------
    def record_work(self, work: WorkUnit) -> WorkUnit:
        self._conn.execute(
            "INSERT INTO work_units VALUES (?,?,?,?,?,?,?,?,?)",
            (work.work_ref, work.inquiry_id, work.grant_id,
             work.controller_id, work.attempt_no, work.parent_work_ref,
             work.kind, work.acquired_capability_id, work.started_at))
        self._conn.commit()
        return work

    def get_work(self, work_ref: str) -> Optional[WorkUnit]:
        row = self._conn.execute(
            "SELECT * FROM work_units WHERE work_ref=?", (work_ref,)).fetchone()
        return _work_from_row(row) if row else None

    def children_of(self, work_ref: str) -> List[WorkUnit]:
        rows = self._conn.execute(
            "SELECT * FROM work_units WHERE parent_work_ref=?", (work_ref,)).fetchall()
        return [_work_from_row(r) for r in rows]

    # -- results --------------------------------------------------------
    def record_result(self, result: Result) -> Result:
        result.validate()
        self._conn.execute(
            "INSERT INTO results VALUES (?,?,?,?,?,?)",
            (result.result_ref, result.work_ref, result.outcome,
             result.evidence_ref, result.detail, result.produced_at))
        self._conn.commit()
        return result

    def result_for_work(self, work_ref: str) -> Optional[Result]:
        row = self._conn.execute(
            "SELECT * FROM results WHERE work_ref=?", (work_ref,)).fetchone()
        return _result_from_row(row) if row else None

    # -- admissions -----------------------------------------------------
    def record_admission(self, admission: AdmissionRecord) -> AdmissionRecord:
        admission.validate()
        self._conn.execute(
            "INSERT INTO admissions VALUES (?,?,?,?,?,?,?)",
            (admission.admission_id, admission.result_ref,
             admission.verdict, admission.yield_value,
             admission.accepted_portion, admission.decided_by,
             admission.decided_at))
        self._conn.commit()
        return admission

    def admission_for_result(self, result_ref: str) -> Optional[AdmissionRecord]:
        row = self._conn.execute(
            "SELECT * FROM admissions WHERE result_ref=?", (result_ref,)).fetchone()
        return _admission_from_row(row) if row else None

    # -- capability acquisitions (R4) -----------------------------------
    def record_acquisition(self, capability_id: str, acquiring_work_ref: str,
                           acquisition_expenditure_ids: List[str]) -> None:
        import json
        self._conn.execute(
            "INSERT INTO capability_acquisitions VALUES (?,?,?)",
            (capability_id, acquiring_work_ref,
             json.dumps(sorted(acquisition_expenditure_ids))))
        self._conn.commit()

    def acquisition(self, capability_id: str) -> Optional[Dict[str, Any]]:
        import json
        row = self._conn.execute(
            "SELECT * FROM capability_acquisitions WHERE capability_id=?",
            (capability_id,)).fetchone()
        if not row:
            return None
        return {"capability_id": row["capability_id"],
                "acquiring_work_ref": row["acquiring_work_ref"],
                "acquisition_expenditure_ids": json.loads(row["acquisition_expenditure_ids"])}

    def close(self) -> None:
        self._conn.commit()
        self._conn.close()


# ---------------------------------------------------------------------------
# attribution — recomputed from ledgers, never cached
# ---------------------------------------------------------------------------

def recompute_attribution(work_ref: str, chain: ChainLedger,
                          expenditures: "ExpenditureLedgerLike",
                          originating_executive: str) -> AttributionLink:
    """Recompute the attribution link for a work unit from the current
    ledger state. Every quantity is derived from the expenditure
    records present NOW — this is what makes the causal gate's
    counterfactuals genuine: remove an expenditure, and the
    attribution it produced collapses, because the recompute has no
    memory of it.

    R5 (nesting): child spend aggregates into the parent exactly once.
    R4 (reuse): a reusing work unit's own expenditures only; the
    acquisition's spend stays with the acquiring work unit.
    R1 (yield): yield is positive only when an acceptance/admission
    record exists for this work's result.
    R3 (partial): yield scales with the accepted portion.
    """
    work = chain.get_work(work_ref)
    if work is None:
        raise KeyError(f"unknown work_ref {work_ref!r}")

    # Own expenditures (R5: NOT including descendants here — the
    # parent's aggregate is computed separately by aggregate_attribution).
    own = expenditures.for_work(work_ref)

    result = chain.result_for_work(work_ref)
    result_ref = result.result_ref if result else ""

    admission = chain.admission_for_result(result_ref) if result else None
    acceptance_record_id = admission.admission_id if (
        admission and admission.verdict == "accepted") else None

    # Causal-chain integrity: attribution is the link
    # expenditure → yield. A work unit with no recorded expenditure
    # has a broken chain — there is nothing to attribute yield to —
    # so the link is void (yield 0) even if an admission record still
    # references its result. The admission record itself is the
    # authority's fact and is untouched; the LINK carries no yield.
    chain_broken = len(own) == 0
    if chain_broken:
        acceptance_record_id = None

    # R1: no acceptance record → zero yield (cost still accrues).
    # R3: partial → proportional yield.
    attributed_yield = 0.0
    if acceptance_record_id is not None:
        attributed_yield = admission.yield_value * admission.accepted_portion

    model_provider = own[0].model_provider if own else ""
    cost_kind = _dominant_cost_kind(own)

    return AttributionLink(
        grant_id=work.grant_id,
        expenditure_ids=sorted(e.expenditure_id for e in own),
        work_ref=work_ref,
        result_ref=result_ref,
        acceptance_record_id=acceptance_record_id,
        attributed_yield=round(attributed_yield, 6),
        originating_executive=originating_executive,
        controller_id=work.controller_id,
        inquiry_id=work.inquiry_id,
        model_provider=model_provider,
        total_cpu_seconds=round(sum(e.cpu_seconds for e in own), 6),
        total_monetary_cost=round(sum(e.monetary_cost for e in own), 6),
        cost_kind=cost_kind,
        attempt_no=work.attempt_no,
    )


def aggregate_attribution(work_ref: str, chain: ChainLedger,
                          expenditures: "ExpenditureLedgerLike",
                          originating_executive: str) -> AttributionLink:
    """R5: the parent view — this work unit's own spend plus every
    descendant's spend, each exactly once. Removal of one descendant's
    expenditure drops the aggregate by exactly that expenditure's
    amount (the causal gate proves this)."""
    base = recompute_attribution(work_ref, chain, expenditures,
                                 originating_executive)
    descendant_ids: List[str] = []
    descendant_cpu = 0.0
    descendant_cost = 0.0
    stack = [c.work_ref for c in chain.children_of(work_ref)]
    seen = set()
    while stack:
        ref = stack.pop()
        if ref in seen:
            continue
        seen.add(ref)
        child = recompute_attribution(ref, chain, expenditures,
                                      originating_executive)
        descendant_ids.extend(child.expenditure_ids)
        descendant_cpu += child.total_cpu_seconds
        descendant_cost += child.total_monetary_cost
        stack.extend(c.work_ref for c in chain.children_of(ref))
    base.expenditure_ids = sorted(set(base.expenditure_ids) | set(descendant_ids))
    base.total_cpu_seconds = round(base.total_cpu_seconds + descendant_cpu, 6)
    base.total_monetary_cost = round(base.total_monetary_cost + descendant_cost, 6)
    return base


def _dominant_cost_kind(exps: List[Expenditure]) -> str:
    if not exps:
        return "unpriced"
    kinds = [e.cost_kind for e in exps]
    # measured beats declared beats estimated beats unpriced (§8:
    # preserve the best-available provenance honestly)
    for kind in ("measured", "declared", "estimated", "unpriced"):
        if kind in kinds:
            return kind
    return "unpriced"


# ---------------------------------------------------------------------------
# row helpers
# ---------------------------------------------------------------------------

def _work_from_row(row: sqlite3.Row) -> WorkUnit:
    return WorkUnit(
        work_ref=row["work_ref"], inquiry_id=row["inquiry_id"],
        grant_id=row["grant_id"], controller_id=row["controller_id"],
        attempt_no=row["attempt_no"], parent_work_ref=row["parent_work_ref"],
        kind=row["kind"], acquired_capability_id=row["acquired_capability_id"],
        started_at=row["started_at"])


def _result_from_row(row: sqlite3.Row) -> Result:
    return Result(
        result_ref=row["result_ref"], work_ref=row["work_ref"],
        outcome=row["outcome"], evidence_ref=row["evidence_ref"],
        detail=row["detail"], produced_at=row["produced_at"])


def _admission_from_row(row: sqlite3.Row) -> AdmissionRecord:
    return AdmissionRecord(
        admission_id=row["admission_id"], result_ref=row["result_ref"],
        verdict=row["verdict"], yield_value=row["yield_value"],
        accepted_portion=row["accepted_portion"],
        decided_by=row["decided_by"], decided_at=row["decided_at"])


class ExpenditureLedgerLike:
    """Structural interface the recompute needs from the expenditure
    ledger (kept import-light for the gate's snapshot copies)."""
    def for_work(self, work_ref: str) -> List[Expenditure]: ...  # pragma: no cover
