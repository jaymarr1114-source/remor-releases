"""The creativity budget mechanism (paper T6, §11 Q3, D-6).

Q3 (James, DECIDED): creative work is budgeted, not exempt — BOTH compute
and monetary cost, governed by the FRM. No hard-coded permanent numeric
budget: the initial budget is an explicit configurable policy envelope
established from measured workload/cost data, with authorization required
for expansion beyond it.

D-6 (decided-as-replaced): resources flow through the FRM. This module
consumes the FRM's real grant path — issue_run_grant / the frozen FrmGrant
contract (GRANT-MIGRATE-1) — and adds the policy layer the FRM does not
have: the configurable envelope, the hard refusal on over-envelope
requests, the authorization-gated expansion, and the L1-analog ceiling
stop. The FRM decides the grant; this module decides admission to the
grant path. FRM logic is never reimplemented here.

Cost dimensions:
- compute_s: wall-clock seconds, MEASURED on the bench.
- monetary: the FRM CostInput provenance discipline (policy.py). The tree
  carries no price constants and no billing facts, so on the bench this
  dimension is UNPRICED (value None) — the envelope carries it as an
  explicit UNSET parameter with its interface defined, never a fabricated
  number. If James later sets a DECLARED/MEASURED price, the same check
  path enforces it.

What this module does NOT do (honest bounds):
- It does not detect one logical task split across several work_ids.
  Aggregation is per work_id; cross-work_id splitting is BOUNDED here
  (work identity is EXEC-1/run-controller territory).
- It does not integrate with the FRM epoch loop / apply_grants path.
  Per-run issuance (issue_run_grant) is the production pattern per its
  docstring ("exactly one per-run issuer, not one per caller"); the
  epoch-loop bound is inherited and disclosed, not hidden.
- The envelope's numeric values are a James gate. This module ships the
  mechanism + a measured proposal + its derivation; it never presents a
  number as decided.
"""

from __future__ import annotations

import json
import sqlite3
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from runtime.curiosity.frm.grant import FrmGrant, issue_run_grant
from runtime.curiosity.frm.policy import CostInput, CostKind


# ---------------------------------------------------------------------------
# Dimensions
# ---------------------------------------------------------------------------

COMPUTE = "compute_s"
MONETARY = "monetary"
DIMENSIONS = (COMPUTE, MONETARY)

MEASURE_WALL_CLOCK = "wall_clock"
MEASURE_FRM_COST_INPUT = "frm_cost_input"


class BudgetRefused(Exception):
    """Hard refusal: the request exceeds the envelope. Names the bound
    exceeded and the authorization required for expansion. A refusal, not
    a warning, not a flag."""


class BudgetHalted(Exception):
    """The work hit the budget ceiling mid-run. Non-punitive: the
    preservation record exists; the halt is resource-governance, not a
    fault. Raised on any further spend attempt for the halted work."""


# ---------------------------------------------------------------------------
# Envelope
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class CostBound:
    """One dimension's bound. bound=None means UNSET (monetary on the
    bench): the dimension is carried with its interface defined and is
    NOT enforced — never a fabricated number."""
    dimension: str
    unit: str
    bound: Optional[float]
    measurement: str
    provenance: str  # how derived: workload, method, date — always present

    def __post_init__(self) -> None:
        if self.dimension not in DIMENSIONS:
            raise ValueError(f"unknown dimension {self.dimension!r}")
        if not self.provenance or not self.provenance.strip():
            raise ValueError("a bound carries its provenance, always")
        if self.bound is not None and self.bound < 0:
            raise ValueError("bound must be >= 0")


@dataclass(frozen=True)
class BudgetEnvelope:
    """The configurable policy envelope: one CostBound per dimension."""
    bounds: Dict[str, CostBound]

    def __post_init__(self) -> None:
        if set(self.bounds.keys()) != set(DIMENSIONS):
            raise ValueError(
                f"envelope covers exactly {DIMENSIONS}, "
                f"got {sorted(self.bounds.keys())}")

    def bound_for(self, dimension: str) -> CostBound:
        return self.bounds[dimension]


# Derivation of the shipped proposal: measured, shown, reproducible.
ENVELOPE_HEADROOM_FACTOR = 2.0


def propose_envelope(measurements: Dict[str, float],
                     measured_at: Optional[float] = None,
                     host_note: str = "") -> BudgetEnvelope:
    """Derive a BudgetEnvelope proposal from measured wall-clock costs.

    measurements: workload name -> measured wall-clock seconds (real
        observations, e.g. Phase-1 battery replays under accounting).
    Formula (documented, reproducible): compute bound =
        ceil(max(measurements) * ENVELOPE_HEADROOM_FACTOR).
    The monetary bound is UNSET (UNPRICED on the bench) with its
    interface defined. The proposal is a proposal: James authorizes
    the numbers.
    """
    if not measurements:
        raise ValueError("envelope proposal needs measurements, not air")
    for name, seconds in measurements.items():
        if seconds < 0:
            raise ValueError(f"measurement {name!r} is negative: {seconds}")
    peak = max(measurements.values())
    when = measured_at if measured_at is not None else time.time()
    provenance = (
        f"measured {when:.0f}{(' on ' + host_note) if host_note else ''}: "
        + ", ".join(f"{n}={s:.2f}s" for n, s in sorted(measurements.items()))
        + f"; bound=ceil(max*{ENVELOPE_HEADROOM_FACTOR}) [budget.propose_envelope]"
    )
    import math
    return BudgetEnvelope(bounds={
        COMPUTE: CostBound(
            dimension=COMPUTE, unit="seconds",
            bound=float(math.ceil(peak * ENVELOPE_HEADROOM_FACTOR)),
            measurement=MEASURE_WALL_CLOCK, provenance=provenance),
        MONETARY: CostBound(
            dimension=MONETARY, unit="currency",
            bound=None,  # UNSET: UNPRICED on the bench, never fabricated
            measurement=MEASURE_FRM_COST_INPUT,
            provenance=(
                "no price constants or billing facts in the tree "
                "(frm/policy.py CostKind); monetary UNPRICED on the bench; "
                "bound UNSET until James sets a DECLARED/MEASURED price")),
    })


# ---------------------------------------------------------------------------
# Authorization
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class AuthorizationRecord:
    """Explicit authorization to expand a bound. Without one, an
    over-envelope refusal stands. No silent auto-expansion, no soft limit."""
    authorized_by: str
    dimension: str
    additional: float  # added to the envelope bound for this scope
    scope: str  # a work_id, or "*" for all work
    issued_at: float
    expires_at: float

    def __post_init__(self) -> None:
        if self.dimension not in DIMENSIONS:
            raise ValueError(f"unknown dimension {self.dimension!r}")
        if self.additional <= 0:
            raise ValueError("expansion must add a positive amount")
        if self.expires_at <= self.issued_at:
            raise ValueError("authorization must expire after it is issued")
        if not self.authorized_by or not self.authorized_by.strip():
            raise ValueError("authorization names who authorized it")

    def active(self, now: float, work_id: str) -> bool:
        return (self.issued_at <= now < self.expires_at
                and (self.scope == "*" or self.scope == work_id))


# ---------------------------------------------------------------------------
# Spend accounting (sqlite-backed, injected db_path — no hardcoded paths)
# ---------------------------------------------------------------------------

_SCHEMA = """
CREATE TABLE IF NOT EXISTS grants (
    work_id TEXT, grant_id TEXT, epoch_id INTEGER, budget_s REAL,
    estimated_compute_s REAL, margin_s REAL, issued_at REAL);
CREATE TABLE IF NOT EXISTS refusals (
    work_id TEXT, dimension TEXT, requested REAL, bound REAL,
    reason TEXT, recorded_at REAL);
CREATE TABLE IF NOT EXISTS spend (
    work_id TEXT, compute_s REAL, monetary_kind TEXT,
    monetary_value REAL, recorded_at REAL);
CREATE TABLE IF NOT EXISTS stops (
    work_id TEXT, stopped_at REAL, reason TEXT, preservation_json TEXT);
CREATE TABLE IF NOT EXISTS authorizations (
    authorized_by TEXT, dimension TEXT, additional REAL, scope TEXT,
    issued_at REAL, expires_at REAL);
"""


class SpendLedger:
    """Every grant, refusal, spend, stop, and authorization — recorded
    with the work it belongs to, so the envelope is auditable and the
    beyond-budget yield (accepted artifacts per cost, T6) is computable."""

    def __init__(self, db_path: str) -> None:
        if not db_path:
            raise ValueError("SpendLedger needs an explicit db_path")
        if db_path == ":memory:":
            raise ValueError(
                "SpendLedger refuses :memory:: one connection per operation "
                "would silently lose every record — fail closed, not silent")
        self._db_path = db_path
        conn = sqlite3.connect(db_path)
        try:
            conn.executescript(_SCHEMA)
            conn.commit()
        finally:
            conn.close()

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path)
        conn.row_factory = sqlite3.Row
        return conn

    # -- writes ---------------------------------------------------------
    def record_grant(self, work_id: str, grant: FrmGrant,
                     estimated_compute_s: float, margin_s: float) -> None:
        conn = self._conn()
        try:
            conn.execute(
                "INSERT INTO grants VALUES (?,?,?,?,?,?,?)",
                (work_id, grant.grant_id, grant.epoch_id, grant.budget_s,
                 estimated_compute_s, margin_s, time.time()))
            conn.commit()
        finally:
            conn.close()

    def record_refusal(self, work_id: str, dimension: str,
                       requested: Optional[float],
                       bound: Optional[float], reason: str) -> None:
        conn = self._conn()
        try:
            conn.execute(
                "INSERT INTO refusals VALUES (?,?,?,?,?,?)",
                (work_id, dimension, requested, bound, reason, time.time()))
            conn.commit()
        finally:
            conn.close()

    def record_spend(self, work_id: str, compute_s: float,
                     monetary: Optional[CostInput] = None) -> None:
        conn = self._conn()
        try:
            conn.execute(
                "INSERT INTO spend VALUES (?,?,?,?,?)",
                (work_id, compute_s,
                 monetary.kind if monetary else None,
                 monetary.value if monetary else None,
                 time.time()))
            conn.commit()
        finally:
            conn.close()

    def record_stop(self, work_id: str, reason: str,
                    preservation: Dict[str, Any]) -> None:
        conn = self._conn()
        try:
            conn.execute(
                "INSERT INTO stops VALUES (?,?,?,?)",
                (work_id, time.time(), reason,
                 json.dumps(preservation, sort_keys=True, default=str)))
            conn.commit()
        finally:
            conn.close()

    def record_authorization(self, record: AuthorizationRecord) -> None:
        conn = self._conn()
        try:
            conn.execute(
                "INSERT INTO authorizations VALUES (?,?,?,?,?,?)",
                (record.authorized_by, record.dimension, record.additional,
                 record.scope, record.issued_at, record.expires_at))
            conn.commit()
        finally:
            conn.close()

    # -- reads ----------------------------------------------------------
    def spent_compute(self, work_id: str) -> float:
        conn = self._conn()
        try:
            row = conn.execute(
                "SELECT COALESCE(SUM(compute_s),0) AS s FROM spend "
                "WHERE work_id=?", (work_id,)).fetchone()
            return float(row["s"])
        finally:
            conn.close()

    def granted_compute(self, work_id: str) -> float:
        """Outstanding granted compute (budget_s of issued grants)."""
        conn = self._conn()
        try:
            row = conn.execute(
                "SELECT COALESCE(SUM(budget_s),0) AS s FROM grants "
                "WHERE work_id=?", (work_id,)).fetchone()
            return float(row["s"])
        finally:
            conn.close()

    def is_halted(self, work_id: str) -> bool:
        conn = self._conn()
        try:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM stops WHERE work_id=?",
                (work_id,)).fetchone()
            return int(row["n"]) > 0
        finally:
            conn.close()

    def refusals_for(self, work_id: str) -> List[Dict[str, Any]]:
        conn = self._conn()
        try:
            return [dict(r) for r in conn.execute(
                "SELECT * FROM refusals WHERE work_id=?", (work_id,))]
        finally:
            conn.close()

    def authorizations_for(self, dimension: str) -> List[AuthorizationRecord]:
        conn = self._conn()
        try:
            return [AuthorizationRecord(
                authorized_by=r["authorized_by"], dimension=r["dimension"],
                additional=r["additional"], scope=r["scope"],
                issued_at=r["issued_at"], expires_at=r["expires_at"])
                for r in conn.execute(
                    "SELECT * FROM authorizations WHERE dimension=?",
                    (dimension,))]
        finally:
            conn.close()

    def summary(self, work_id: str) -> Dict[str, Any]:
        conn = self._conn()
        try:
            g = conn.execute(
                "SELECT COUNT(*) n, COALESCE(SUM(budget_s),0) s FROM grants "
                "WHERE work_id=?", (work_id,)).fetchone()
            r = conn.execute(
                "SELECT COUNT(*) n FROM refusals WHERE work_id=?",
                (work_id,)).fetchone()
            s = conn.execute(
                "SELECT COUNT(*) n, COALESCE(SUM(compute_s),0) s FROM spend "
                "WHERE work_id=?", (work_id,)).fetchone()
            h = conn.execute(
                "SELECT COUNT(*) n FROM stops WHERE work_id=?",
                (work_id,)).fetchone()
            return {"work_id": work_id,
                    "grants": int(g["n"]), "granted_compute_s": float(g["s"]),
                    "refusals": int(r["n"]),
                    "spend_events": int(s["n"]),
                    "spent_compute_s": float(s["s"]),
                    "halted": int(h["n"]) > 0}
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# The mechanism
# ---------------------------------------------------------------------------

@dataclass
class CeilingStop:
    """The L1-analog hard stop: non-punitive, preserves everything.
    Resource-governance, not a fault."""
    work_id: str
    stopped_at: float
    reason: str
    preservation: Dict[str, Any]  # consumption, grants, provenance refs,
    # completed evidence, checkpoints


class CreativityBudget:
    """Creative work spends compute and monetary cost through the FRM.

    request_budget() checks the request against the envelope, then issues
    through the FRM's real grant path (issue_run_grant / FrmGrant). The
    FRM decides the grant; this module decides admission to the grant
    path. FRM logic is never reimplemented here.
    """

    def __init__(self, envelope: BudgetEnvelope, *,
                 spend_db_path: str,
                 grant_issuer: Callable[..., FrmGrant] = issue_run_grant,
                 ) -> None:
        if not isinstance(envelope, BudgetEnvelope):
            raise ValueError("CreativityBudget needs a BudgetEnvelope")
        self._envelope = envelope
        self._ledger = SpendLedger(spend_db_path)
        self._issue = grant_issuer

    @property
    def envelope(self) -> BudgetEnvelope:
        return self._envelope

    @property
    def ledger(self) -> SpendLedger:
        return self._ledger

    # -- admission ------------------------------------------------------
    def _effective_bound(self, dimension: str, work_id: str,
                         now: float) -> Optional[float]:
        bound = self._envelope.bound_for(dimension).bound
        if bound is None:
            return None  # UNSET: carried, not enforced
        extra = sum(a.additional for a in
                    self._ledger.authorizations_for(dimension)
                    if a.active(now, work_id))
        return bound + extra

    def request_budget(self, *, work_id: str,
                       estimated_compute_s: float,
                       estimated_monetary: Optional[CostInput] = None,
                       margin_s: float = 0.0,
                       note: str = "") -> FrmGrant:
        """Request budget for a unit of creative work.

        The envelope is checked against the EFFECTIVE request
        (estimate + margin = the actual grant size), aggregated per
        work_id: prior granted-but-unspent + prior spent + this request.
        Within envelope -> real FrmGrant via the FRM issuance path.
        Beyond envelope -> BudgetRefused naming the bound and the
        authorization required. Monetary UNPRICED with an UNSET bound is
        carried, not enforced.
        """
        if not work_id or not work_id.strip():
            raise ValueError("request_budget needs a work_id")
        if estimated_compute_s < 0 or margin_s < 0:
            raise ValueError("cost estimates must be >= 0")
        if self._ledger.is_halted(work_id):
            raise BudgetHalted(
                f"work {work_id!r} hit the budget ceiling and is halted; "
                "no further budget will be issued (resource-governance)")
        now = time.time()
        effective = float(estimated_compute_s) + float(margin_s)
        prior = (self._ledger.granted_compute(work_id)
                 - self._ledger.spent_compute(work_id))
        cumulative = max(0.0, prior) + effective

        bound = self._effective_bound(COMPUTE, work_id, now)
        if bound is not None and cumulative > bound:
            reason = (f"compute request {effective:.2f}s (cumulative "
                      f"{cumulative:.2f}s for work {work_id!r}) exceeds the "
                      f"envelope bound {bound:.2f}s")
            self._ledger.record_refusal(work_id, COMPUTE, effective,
                                        bound, reason)
            raise BudgetRefused(
                reason + "; expansion requires an authorization record "
                "(authorize_expansion: who, dimension, amount, scope, "
                "expiry) — without it the refusal stands")

        if estimated_monetary is not None:
            mbound = self._effective_bound(MONETARY, work_id, now)
            if mbound is not None and estimated_monetary.value is not None:
                if estimated_monetary.value > mbound:
                    reason = (f"monetary request {estimated_monetary.value} "
                              f"exceeds the envelope bound {mbound}")
                    self._ledger.record_refusal(
                        work_id, MONETARY, estimated_monetary.value,
                        mbound, reason)
                    raise BudgetRefused(
                        reason + "; expansion requires an authorization "
                        "record — without it the refusal stands")
            # UNPRICED, or UNSET bound: carried in the spend record, not
            # enforced. The interface is defined; the number is not
            # fabricated.

        grant = self._issue(domain="creativity",
                            estimated_cost_s=float(estimated_compute_s),
                            margin_s=float(margin_s),
                            note=note or f"creativity work {work_id}")
        if not isinstance(grant, FrmGrant):
            raise BudgetRefused(
                f"FRM issuance did not return a FrmGrant for work "
                f"{work_id!r}: got {type(grant).__name__} — refusing rather "
                "than proceeding on a non-contract grant")
        self._ledger.record_grant(work_id, grant, float(estimated_compute_s),
                                  float(margin_s))
        return grant

    def authorize_expansion(self, record: AuthorizationRecord) -> None:
        """Register an explicit authorization to expand a bound."""
        if not isinstance(record, AuthorizationRecord):
            raise ValueError("authorize_expansion needs an AuthorizationRecord")
        self._ledger.record_authorization(record)

    # -- spend + ceiling -------------------------------------------------
    def record_spend(self, work_id: str, compute_s: float,
                     monetary: Optional[CostInput] = None,
                     checkpoints: Optional[Dict[str, Any]] = None
                     ) -> Optional[CeilingStop]:
        """Record actual spend. The ceiling is enforced on ACTUALS, not
        estimates: estimate/actual divergence is recorded and visible.
        Returns a CeilingStop when this spend hits the ceiling (the work
        is halted; further spend raises BudgetHalted). Returns None
        otherwise."""
        if compute_s < 0:
            raise ValueError("spend must be >= 0")
        if self._ledger.is_halted(work_id):
            raise BudgetHalted(
                f"work {work_id!r} is halted at the budget ceiling; no "
                "further spend accrues (non-cooperative stop)")
        self._ledger.record_spend(work_id, compute_s, monetary)
        now = time.time()
        # The ceiling is the EFFECTIVE bound: envelope + active,
        # in-scope, unexpired authorizations. An authorized expansion
        # genuinely moves the ceiling for its scope and duration — that
        # is what "authorization required for expansion" means. Without
        # an authorization, the envelope bound stands.
        ceiling = self._effective_bound(COMPUTE, work_id, now)
        if ceiling is not None and self._ledger.spent_compute(work_id) >= ceiling:
            return self._halt(work_id, checkpoints or {},
                              f"cumulative spend reached the effective bound "
                              f"{ceiling:.2f}s")
        return None

    def _halt(self, work_id: str, checkpoints: Dict[str, Any],
              reason: str) -> CeilingStop:
        summary = self._ledger.summary(work_id)
        preservation = {
            "classification": "resource-governance (not a fault)",
            "consumption_records": summary,
            "grants": summary["grants"],
            "granted_compute_s": summary["granted_compute_s"],
            "spent_compute_s": summary["spent_compute_s"],
            "refusals": summary["refusals"],
            "checkpoints": checkpoints,
            "note": ("everything preserved: consumption records, grants, "
                     "provenance refs, completed evidence, checkpoints; "
                     "accumulated knowledge not erased"),
        }
        self._ledger.record_stop(work_id, reason, preservation)
        return CeilingStop(work_id=work_id, stopped_at=time.time(),
                           reason=reason, preservation=preservation)

    # -- audit -----------------------------------------------------------
    def spend_summary(self, work_id: str) -> Dict[str, Any]:
        return self._ledger.summary(work_id)

    def yield_report(self, work_id: str,
                     admitted_artifacts: int) -> Dict[str, Any]:
        """T6: beyond-budget yield as accepted artifacts per cost, computed
        from the spend records — never asserted."""
        summary = self._ledger.summary(work_id)
        spent = summary["spent_compute_s"]
        return {"work_id": work_id,
                "admitted_artifacts": int(admitted_artifacts),
                "spent_compute_s": spent,
                "artifacts_per_compute_s":
                    (float(admitted_artifacts) / spent if spent > 0 else None),
                "refusals": summary["refusals"],
                "halted": summary["halted"]}
