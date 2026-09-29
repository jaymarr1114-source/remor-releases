"""CUR-P1D — expenditure-site metering per charter C-4.5.

C-4.5: shared-substrate spend (reasoning-model calls) is metered at the
call site by the substrate interface and attributed to the calling
controller's budget. The substrate enforces nothing and holds no
budget; it reports spend.

A31/A32: the substrate interface meters (reports); the calling
controller's budget pays.

FRM amendment §8 — monetary cost provenance is distinguished:
    measured  — observed billing fact
    declared  — explicitly declared policy constant (James's input)
    estimated — provisional cost estimate
    unpriced  — no price available at all

The MeteredSubstrate wraps any substrate callable. Every call produces
a SpendReport measured at the call site. The substrate never sees a
budget, never enforces, never refuses for money reasons.
"""
from __future__ import annotations

import hashlib
import sqlite3
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional


# ---------------------------------------------------------------------------
# records
# ---------------------------------------------------------------------------

COST_KINDS = ("measured", "declared", "estimated", "unpriced")


@dataclass
class SpendReport:
    """What the substrate interface reports for one call (C-4.5). The
    report is produced at the call site, from measurement, not from
    the caller's ledger."""
    call_id: str
    controller_id: str          # the caller whose budget pays (A32)
    inquiry_id: str             # originating inquiry/work unit (FRM §7)
    work_ref: str
    model_provider: str         # relevant model/provider (FRM §7)
    cpu_seconds: float          # measured wall-clock CPU of the call
    work_units: int             # counted reasoning units (real iterations)
    bytes_processed: int
    monetary_cost: float
    cost_kind: str              # measured|declared|estimated|unpriced (§8)
    reported_at: float = field(default_factory=time.time)

    def validate(self) -> "SpendReport":
        if self.cpu_seconds < 0 or self.work_units < 0 or self.bytes_processed < 0:
            raise ValueError("spend quantities cannot be negative")
        if self.monetary_cost < 0:
            raise ValueError("monetary_cost cannot be negative")
        if self.cost_kind not in COST_KINDS:
            raise ValueError(f"cost_kind {self.cost_kind!r} not in {COST_KINDS}")
        return self


@dataclass
class Expenditure:
    """A SpendReport persisted to the expenditure ledger — the
    expenditure link of the attribution chain."""
    expenditure_id: str
    grant_id: str
    call_id: str
    controller_id: str
    inquiry_id: str
    work_ref: str
    model_provider: str
    cpu_seconds: float
    work_units: int
    bytes_processed: int
    monetary_cost: float
    cost_kind: str
    reported_at: float


# ---------------------------------------------------------------------------
# the metered substrate interface
# ---------------------------------------------------------------------------

class MeteredSubstrate:
    """Wraps a substrate callable with call-site metering (C-4.5).

    The wrapped callable performs real work and returns a real result.
    The wrapper measures the call and reports a SpendReport; it never
    enforces, never refuses, never sees the caller's budget.

    monetary_cost is derived from a declared policy rate
    (FRM §8: an explicitly declared policy constant, honestly labeled
    ``declared`` — not an observed billing fact).
    """

    def __init__(self, work_fn: Callable[..., Any], model_provider: str,
                 declared_rate_per_cpu_s: float = 0.0):
        self._work_fn = work_fn
        self._model_provider = model_provider
        self._declared_rate = declared_rate_per_cpu_s

    @property
    def model_provider(self) -> str:
        return self._model_provider

    def call(self, controller_id: str, inquiry_id: str, work_ref: str,
             *args: Any, **kwargs: Any) -> "MeteredCall":
        """Execute the substrate call and meter it. Returns the real
        result together with its SpendReport."""
        start = time.perf_counter()
        result = self._work_fn(*args, **kwargs)
        elapsed = time.perf_counter() - start
        work_units = _count_work_units(result)
        payload = repr(args) + repr(sorted(kwargs.items()))
        bytes_processed = len(payload.encode("utf-8"))
        monetary_cost = elapsed * self._declared_rate
        report = SpendReport(
            call_id=str(uuid.uuid4()),
            controller_id=controller_id,
            inquiry_id=inquiry_id,
            work_ref=work_ref,
            model_provider=self._model_provider,
            cpu_seconds=elapsed,
            work_units=work_units,
            bytes_processed=bytes_processed,
            monetary_cost=monetary_cost,
            cost_kind="declared" if self._declared_rate > 0 else "unpriced",
        ).validate()
        return MeteredCall(result=result, report=report)


@dataclass
class MeteredCall:
    result: Any
    report: SpendReport


def _count_work_units(result: Any) -> int:
    """Count real reasoning units in the result: the number of
    iterations the substrate demonstrably performed. The test substrate
    returns (digest, iterations); anything else counts as 1 call."""
    if isinstance(result, tuple) and len(result) == 2 and isinstance(result[1], int):
        return result[1]
    return 1


def real_substrate_work(payload: bytes, iterations: int = 2000) -> tuple:
    """Real substrate work: iterated hashing — actual CPU consumption,
    actually measured. Returns (hex_digest, iterations) so the work
    units are counted, not asserted."""
    digest = hashlib.sha256(payload).digest()
    for _ in range(iterations):
        digest = hashlib.sha256(digest).digest()
    return digest.hex(), iterations


# ---------------------------------------------------------------------------
# expenditure ledger
# ---------------------------------------------------------------------------

_SCHEMA = """
CREATE TABLE IF NOT EXISTS expenditures (
    expenditure_id TEXT PRIMARY KEY,
    grant_id TEXT NOT NULL,
    call_id TEXT NOT NULL,
    controller_id TEXT NOT NULL,
    inquiry_id TEXT NOT NULL,
    work_ref TEXT NOT NULL,
    model_provider TEXT NOT NULL,
    cpu_seconds REAL NOT NULL,
    work_units INTEGER NOT NULL,
    bytes_processed INTEGER NOT NULL,
    monetary_cost REAL NOT NULL,
    cost_kind TEXT NOT NULL,
    reported_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_exp_work ON expenditures(work_ref);
CREATE INDEX IF NOT EXISTS idx_exp_inquiry ON expenditures(inquiry_id);
CREATE INDEX IF NOT EXISTS idx_exp_grant ON expenditures(grant_id);
"""


class ExpenditureLedger:
    """Append-only expenditure ledger — the expenditure link of the
    attribution chain. The ledger records what the substrate reported;
    it never invents spend."""

    def __init__(self, db_path: str):
        self.db_path = db_path
        self._conn = sqlite3.connect(db_path)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def record(self, grant_id: str, report: SpendReport) -> Expenditure:
        report.validate()
        exp = Expenditure(
            expenditure_id=str(uuid.uuid4()),
            grant_id=grant_id,
            call_id=report.call_id,
            controller_id=report.controller_id,
            inquiry_id=report.inquiry_id,
            work_ref=report.work_ref,
            model_provider=report.model_provider,
            cpu_seconds=report.cpu_seconds,
            work_units=report.work_units,
            bytes_processed=report.bytes_processed,
            monetary_cost=report.monetary_cost,
            cost_kind=report.cost_kind,
            reported_at=report.reported_at,
        )
        self._conn.execute(
            "INSERT INTO expenditures VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (exp.expenditure_id, exp.grant_id, exp.call_id,
             exp.controller_id, exp.inquiry_id, exp.work_ref,
             exp.model_provider, exp.cpu_seconds, exp.work_units,
             exp.bytes_processed, exp.monetary_cost, exp.cost_kind,
             exp.reported_at),
        )
        self._conn.commit()
        return exp

    def for_work(self, work_ref: str) -> List[Expenditure]:
        rows = self._conn.execute(
            "SELECT * FROM expenditures WHERE work_ref=? ORDER BY reported_at",
            (work_ref,)).fetchall()
        return [_exp_from_row(r) for r in rows]

    def for_inquiry(self, inquiry_id: str) -> List[Expenditure]:
        rows = self._conn.execute(
            "SELECT * FROM expenditures WHERE inquiry_id=? ORDER BY reported_at",
            (inquiry_id,)).fetchall()
        return [_exp_from_row(r) for r in rows]

    def get(self, expenditure_id: str) -> Optional[Expenditure]:
        row = self._conn.execute(
            "SELECT * FROM expenditures WHERE expenditure_id=?",
            (expenditure_id,)).fetchone()
        return _exp_from_row(row) if row else None

    def delete(self, expenditure_id: str) -> None:
        """Remove one expenditure record. Production code never calls
        this — the ledger is append-only. It exists ONLY so the causal
        gate can execute counterfactuals (remove the claimed causal
        expenditure) against an isolated snapshot copy of the ledger."""
        self._conn.execute(
            "DELETE FROM expenditures WHERE expenditure_id=?",
            (expenditure_id,))
        self._conn.commit()

    def close(self) -> None:
        self._conn.commit()
        self._conn.close()


def _exp_from_row(row: sqlite3.Row) -> Expenditure:
    return Expenditure(
        expenditure_id=row["expenditure_id"], grant_id=row["grant_id"],
        call_id=row["call_id"], controller_id=row["controller_id"],
        inquiry_id=row["inquiry_id"], work_ref=row["work_ref"],
        model_provider=row["model_provider"],
        cpu_seconds=row["cpu_seconds"], work_units=row["work_units"],
        bytes_processed=row["bytes_processed"],
        monetary_cost=row["monetary_cost"], cost_kind=row["cost_kind"],
        reported_at=row["reported_at"])
