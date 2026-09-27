#!/usr/bin/env python3
"""M7 — Unified gap registry + dispatcher (M7 owns this file).

Every gap the system registers — missing dependency, missing technique,
quarantined capability, user dissatisfaction, missing data, capability
limitation — becomes ONE gap record in ONE registry, is routed to the right
acquisition path, and closes the same way: verify -> admit -> utilize ->
observe.

Routing is by record SHAPE, never by a type label: the record carries no
``type`` field at all. Each route declares the dotted field paths it
requires (e.g. {"dependency.name", "dependency.kind"}); the dispatcher
selects the most specific satisfied route. Adding a new gap kind means
adding a route with its requirement set -- never editing a branch chain.

Everything outside this file is CALLED, never edited:
  - M5's quarantine reason API (get_quarantine_reason, diagnose_quarantine)
  - Q10's limitation record (parse_limitation_reason, _validate_reproduce_spec,
    _limitation_declared_bound, the limitation_route check)
  - M3's substrate path (parse_dependency_reason, SubstrateAcquisitionDriver)
  - M2's delta schema (DeltaRecord)
  - M1's epistemic record_observation (best-effort; the loop observing itself)
  - frozen admission / effective_status / restore_everywhere

James's refinements (binding):
  - A missing environmental dependency is registered TWICE: once as
    dependency metadata (static inventory), once as an active gap work item.
    Never one without the other (audit_dual_registration enforces it).
  - Capability limitations (scale, format, performance) are first-class gaps:
    the record characterizes attempted task, scale/format, what failed, and
    the "what's preventing it" diagnosis.
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class GapRefused(Exception):
    """Registration refused: fabricated, insufficient, or unverifiable evidence."""


class GapNotFound(Exception):
    """No such gap in the registry."""


# ---------------------------------------------------------------------------
# Shape blocks -- the record's populated blocks ARE its shape.
# There is deliberately no ``type``/``kind`` discriminator on the record.
# ---------------------------------------------------------------------------

@dataclass
class DependencyBlock:
    """A missing environmental dependency (dual-registered: inventory + gap)."""
    name: str
    kind: str  # data | package | model | docs (M3's vocabulary, parsed not asserted)
    detail: str = ""
    inventory_ref: str = ""  # id of the paired static-inventory row


@dataclass
class LimitationBlock:
    """A capability limitation at scale/format (Q10's record shape)."""
    task: str
    scale_or_format: str
    failure: str
    preventing: str
    reproduce: Optional[Dict[str, Any]] = None
    route_hint: str = ""  # technique | substrate | synthesis (from Q10's diagnosis)


@dataclass
class QuarantineBlock:
    """The quarantine this gap was observed through (M5's reason API)."""
    capability_id: str
    reason: str
    since: Optional[float] = None
    system: str = ""


@dataclass
class DissatisfactionBlock:
    """A user dissatisfaction gap (M6's near-miss shape)."""
    attempt_ref: str = ""
    feedback: str = ""
    unmet_criteria: str = ""


@dataclass
class TechniqueBlock:
    """A missing technique gap (M1/M2's delta vocabulary)."""
    objective: str = ""
    delta: Optional[Dict[str, Any]] = None  # validated DeltaRecord as dict
    note: str = ""


@dataclass
class DataBlock:
    """Missing data that is not a dependency of a capability."""
    description: str = ""
    source: str = ""  # where it could come from, if known


# Gap lifecycle. "closed" is written only by _close(), which requires verified
# utilization evidence -- routing alone can never close a gap.
STATUS_OPEN = "open"
STATUS_ROUTED = "routed"
STATUS_ACQUIRING = "acquiring"
STATUS_CLOSED = "closed"


@dataclass
class GapRecord:
    gap_id: str = ""
    registered_at: float = 0.0
    registered_by: str = ""  # what registered it: m5-diagnosis, q12-recorder, ...
    summary: str = ""
    dependency: Optional[DependencyBlock] = None
    limitation: Optional[LimitationBlock] = None
    quarantine: Optional[QuarantineBlock] = None
    dissatisfaction: Optional[DissatisfactionBlock] = None
    technique: Optional[TechniqueBlock] = None
    data: Optional[DataBlock] = None
    evidence: List[Dict[str, Any]] = field(default_factory=list)
    status: str = STATUS_OPEN
    route_name: str = ""
    route_history: List[Dict[str, Any]] = field(default_factory=list)
    closing_evidence: Optional[Dict[str, Any]] = None
    residual: str = ""  # honestly-named remainder, if the closure is partial

    def block_fields(self) -> Dict[str, Any]:
        """Flatten populated blocks to dotted field paths for shape routing."""
        out: Dict[str, Any] = {}
        for bname in ("dependency", "limitation", "quarantine",
                      "dissatisfaction", "technique", "data"):
            block = getattr(self, bname)
            if block is None:
                continue
            for k, v in asdict(block).items():
                out[f"{bname}.{k}"] = v
        return out


# ---------------------------------------------------------------------------
# Evidence validation -- a record exists only where there is real evidence.
# Checkable evidence kinds are RE-VERIFIED live; anything that does not
# check out is refused as fabricated/insufficient (the adversarial bar).
# ---------------------------------------------------------------------------

def _validate_evidence(record: GapRecord, engine: Any) -> None:
    ev = record.evidence or []
    if not ev:
        raise GapRefused("no evidence: a gap record exists only where there "
                         "is an actual observable gap with evidence")
    for i, entry in enumerate(ev):
        if not isinstance(entry, dict):
            raise GapRefused(f"evidence[{i}] is not a structured record")
        kind = entry.get("kind")
        if kind == "quarantine_record":
            _verify_quarantine_evidence(entry, engine, i)
        elif kind == "diagnosis":
            _verify_diagnosis_evidence(entry, engine, i)
        elif kind == "observation":
            if not entry.get("observed") or not entry.get("detail"):
                raise GapRefused(
                    f"evidence[{i}] observation needs 'observed' and 'detail'")
        elif kind == "measurement":
            if "metric" not in entry or "value" not in entry:
                raise GapRefused(
                    f"evidence[{i}] measurement needs 'metric' and 'value'")
        else:
            raise GapRefused(f"evidence[{i}] has unknown kind {kind!r}: "
                             "evidence must be checkable")


def _verify_quarantine_evidence(entry: Dict[str, Any], engine: Any,
                               i: int) -> None:
    cap_id = entry.get("capability_id")
    reason = entry.get("reason")
    if not cap_id or not reason:
        raise GapRefused(f"evidence[{i}] quarantine_record needs "
                         "'capability_id' and 'reason'")
    from swarm_engine.synthesis.integrity import get_quarantine_reason
    qr = get_quarantine_reason(cap_id, engine=engine)
    if not qr or not qr.get("reason"):
        raise GapRefused(
            f"evidence[{i}] fabricated: capability {cap_id!r} has no "
            "recorded quarantine reason in the live store")
    if qr["reason"] != reason:
        raise GapRefused(
            f"evidence[{i}] fabricated: claimed reason does not match the "
            f"live store (claimed {reason[:80]!r}, live "
            f"{qr['reason'][:80]!r})")


def _verify_diagnosis_evidence(entry: Dict[str, Any], engine: Any,
                              i: int) -> None:
    cap_id = entry.get("capability_id")
    verdict = entry.get("verdict")
    if not cap_id or not verdict:
        raise GapRefused(f"evidence[{i}] diagnosis needs 'capability_id' "
                         "and 'verdict'")
    from swarm_engine.synthesis.integrity import diagnose_quarantine
    diag = diagnose_quarantine(engine, cap_id)
    if diag.verdict != verdict:
        raise GapRefused(
            f"evidence[{i}] fabricated: claimed verdict {verdict!r} does not "
            f"match a fresh diagnosis ({diag.verdict!r})")


# ---------------------------------------------------------------------------
# Routes -- declared by the field paths they require. The dispatcher picks
# the most specific satisfied route. No type branches, no package names.
# ---------------------------------------------------------------------------

@dataclass
class Route:
    name: str
    requires: frozenset  # dotted field paths the record must carry
    constraint: Optional[Callable[[GapRecord], bool]] = None
    acquire: Optional[Callable[..., Tuple[str, str, Optional[Dict[str, Any]]]]] = None
    # acquire(registry, record, context) -> (outcome, detail, closing_evidence)
    # outcome: "closed" | "open"


@dataclass
class DispatchResult:
    gap_id: str
    routed: bool
    route_name: str = ""
    outcome: str = "open"  # closed | open
    detail: str = ""
    closing_evidence: Optional[Dict[str, Any]] = None

    def as_dict(self) -> Dict[str, Any]:
        return {"gap_id": self.gap_id, "routed": self.routed,
                "route_name": self.route_name, "outcome": self.outcome,
                "detail": self.detail,
                "closing_evidence": self.closing_evidence}


def _has_fields(record: GapRecord, paths: frozenset) -> bool:
    fields = record.block_fields()
    for p in paths:
        v = fields.get(p)
        if v is None or (isinstance(v, str) and not v.strip()):
            return False
    return True


def _select_route(record: GapRecord,
                 routes: List[Route]) -> Optional[Route]:
    """Most-specific satisfied route wins; registration order breaks ties."""
    best: Optional[Route] = None
    best_n = -1
    for route in routes:
        if not _has_fields(record, route.requires):
            continue
        if route.constraint is not None and not route.constraint(record):
            continue
        n = len(route.requires)
        if n > best_n:
            best, best_n = route, n
    return best


# ---------------------------------------------------------------------------
# GapRegistry
# ---------------------------------------------------------------------------

_SCHEMA = """
CREATE TABLE IF NOT EXISTS m7_gap_records (
    gap_id TEXT PRIMARY KEY,
    registered_at REAL NOT NULL,
    registered_by TEXT NOT NULL,
    summary TEXT NOT NULL,
    record_json TEXT NOT NULL,
    status TEXT NOT NULL,
    route_name TEXT NOT NULL DEFAULT '',
    closing_json TEXT
);
CREATE TABLE IF NOT EXISTS m7_dependency_inventory (
    inv_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    kind TEXT NOT NULL,
    detail TEXT NOT NULL DEFAULT '',
    declared_by TEXT NOT NULL,
    first_seen REAL NOT NULL,
    last_seen REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS m7_route_runs (
    run_id TEXT PRIMARY KEY,
    gap_id TEXT NOT NULL,
    at REAL NOT NULL,
    route_name TEXT NOT NULL,
    outcome TEXT NOT NULL,
    detail TEXT NOT NULL
);
"""


class GapRegistry:
    """One registry for every gap. Owns its own tables; the engine is only
    ever CALLED (frozen APIs), never modified."""

    def __init__(self, engine: Any, db_path: Optional[str] = None):
        self._engine = engine
        if db_path is None:
            base = getattr(engine, "db_path", None)
            if base:
                db_path = os.path.join(os.path.dirname(
                    os.path.abspath(base)), "gaps.db")
            else:
                db_path = os.path.abspath("gaps.db")
        self._db_path = db_path
        con = sqlite3.connect(self._db_path)
        try:
            con.executescript(_SCHEMA)
            con.commit()
        finally:
            con.close()
        self._routes: List[Route] = _builtin_routes()

    # -- persistence ------------------------------------------------------
    def _conn(self) -> sqlite3.Connection:
        con = sqlite3.connect(self._db_path)
        con.row_factory = sqlite3.Row
        return con

    def _save(self, record: GapRecord) -> None:
        payload = asdict(record)
        con = self._conn()
        try:
            con.execute(
                "INSERT OR REPLACE INTO m7_gap_records "
                "(gap_id, registered_at, registered_by, summary, "
                " record_json, status, route_name, closing_json) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (record.gap_id, record.registered_at, record.registered_by,
                 record.summary, json.dumps(payload),
                 record.status, record.route_name,
                 json.dumps(record.closing_evidence)
                 if record.closing_evidence else None))
            con.commit()
        finally:
            con.close()

    def _load_row(self, row: sqlite3.Row) -> GapRecord:
        payload = json.loads(row["record_json"])
        return _record_from_dict(payload)

    def get(self, gap_id: str) -> GapRecord:
        con = self._conn()
        try:
            row = con.execute(
                "SELECT * FROM m7_gap_records WHERE gap_id=?",
                (gap_id,)).fetchone()
        finally:
            con.close()
        if row is None:
            raise GapNotFound(gap_id)
        return self._load_row(row)

    def list_gaps(self, status: Optional[str] = None) -> List[GapRecord]:
        con = self._conn()
        try:
            if status:
                rows = con.execute(
                    "SELECT * FROM m7_gap_records WHERE status=? "
                    "ORDER BY registered_at", (status,)).fetchall()
            else:
                rows = con.execute(
                    "SELECT * FROM m7_gap_records ORDER BY registered_at"
                ).fetchall()
        finally:
            con.close()
        return [self._load_row(r) for r in rows]

    # -- registration -----------------------------------------------------
    def register(self, record: GapRecord) -> GapRecord:
        """Validate evidence (re-verified live), assign identity, persist."""
        if not record.summary or not record.summary.strip():
            raise GapRefused("a gap record needs a human-readable summary")
        _validate_evidence(record, self._engine)
        if record.dependency is not None:
            self._ensure_inventory_for(record.dependency,
                                      record.registered_by)
        if not record.gap_id:
            record.gap_id = "gap_" + uuid.uuid4().hex[:12]
        if not record.registered_at:
            record.registered_at = time.time()
        record.status = STATUS_OPEN
        self._save(record)
        return record

    def register_from_diagnosis(self, capability_id: str,
                               registered_by: str = "m5-diagnosis"
                               ) -> GapRecord:
        """Build a gap record from M5/Q10's REAL diagnosis output.

        The quarantine reason is read live; limitation reasons are parsed
        with Q10's parser; the route hint comes from Q10's limitation_route
        check. Nothing is asserted -- everything is read."""
        from swarm_engine.synthesis.integrity import (
            get_quarantine_reason, diagnose_quarantine,
            parse_limitation_reason)
        from swarm_engine.acquisition.substrate import parse_dependency_reason
        qr = get_quarantine_reason(capability_id, engine=self._engine)
        if not qr or not qr.get("reason"):
            raise GapRefused(f"capability {capability_id!r} has no recorded "
                             "quarantine reason: nothing to register")
        reason = qr["reason"]
        diag = diagnose_quarantine(self._engine, capability_id)
        qblock = QuarantineBlock(capability_id=capability_id, reason=reason,
                                since=qr.get("since"),
                                system=qr.get("system") or "")
        record = GapRecord(
            registered_by=registered_by,
            summary=f"gap observed through quarantine of {capability_id}: "
                    f"{reason[:120]}",
            quarantine=qblock,
            evidence=[
                {"kind": "quarantine_record", "capability_id": capability_id,
                 "reason": reason},
                {"kind": "diagnosis", "capability_id": capability_id,
                 "verdict": diag.verdict, "label": diag.label or ""},
            ])
        lim = parse_limitation_reason(reason)
        if lim is not None:
            hint = ""
            for check in (diag.checks or []):
                if check.get("name") == "limitation_route":
                    hint = check.get("outcome") or ""
            record.limitation = LimitationBlock(
                task=lim.get("task") or "", scale_or_format=lim.get(
                    "scale_or_format") or "", failure=lim.get("failure") or "",
                preventing=lim.get("preventing") or "",
                reproduce=lim.get("reproduce"), route_hint=hint)
            record.summary = (f"capability limitation: {lim.get('task')} at "
                              f"{lim.get('scale_or_format')}")
        else:
            dep = parse_dependency_reason(reason)
            if dep is not None:
                record.dependency = DependencyBlock(
                    name=dep.name, kind=dep.kind, detail=dep.detail)
                record.summary = (f"missing dependency: {dep.kind} "
                                  f"{dep.name}")
        return self.register(record)

    def register_dependency_gap(
            self, name: str, kind: str, detail: str,
            evidence: List[Dict[str, Any]],
            registered_by: str,
            summary: str = "",
            capability_id: Optional[str] = None) -> GapRecord:
        """Dual registration (James's rule): the static inventory row AND
        the active gap work item are written together, atomically. Never
        one without the other."""
        if kind not in ("data", "package", "model", "docs"):
            raise GapRefused(f"unknown dependency kind {kind!r}")
        record = GapRecord(
            registered_by=registered_by,
            summary=summary or f"missing dependency: {kind} {name}",
            dependency=DependencyBlock(name=name, kind=kind, detail=detail),
            evidence=evidence)
        if capability_id:
            from swarm_engine.synthesis.integrity import get_quarantine_reason
            qr = get_quarantine_reason(capability_id, engine=self._engine)
            record.quarantine = QuarantineBlock(
                capability_id=capability_id,
                reason=(qr.get("reason") if qr else ""),
                since=(qr.get("since") if qr else None),
                system=(qr.get("system") if qr else "") or "")
        return self.register(record)

    def _ensure_inventory_for(self, dep: DependencyBlock,
                             declared_by: str) -> None:
        """Write (or refresh) the static inventory row paired with a
        dependency gap. Called inside register(), so the pair is atomic."""
        now = time.time()
        con = self._conn()
        try:
            row = con.execute(
                "SELECT inv_id, first_seen FROM m7_dependency_inventory "
                "WHERE name=? AND kind=?", (dep.name, dep.kind)).fetchone()
            if row is None:
                inv_id = "inv_" + uuid.uuid4().hex[:12]
                con.execute(
                    "INSERT INTO m7_dependency_inventory "
                    "(inv_id, name, kind, detail, declared_by, first_seen, "
                    " last_seen) VALUES (?,?,?,?,?,?,?)",
                    (inv_id, dep.name, dep.kind, dep.detail, declared_by,
                     now, now))
                dep.inventory_ref = inv_id
            else:
                dep.inventory_ref = row["inv_id"]
                con.execute(
                    "UPDATE m7_dependency_inventory SET last_seen=?, "
                    "detail=? WHERE inv_id=?",
                    (now, dep.detail, row["inv_id"]))
            con.commit()
        finally:
            con.close()

    def audit_dual_registration(self) -> Dict[str, Any]:
        """James's invariant: a missing dep that isn't a gap is a gap the
        system is ignoring. Report inventory rows with no active gap and
        dependency gaps with no inventory row."""
        con = self._conn()
        try:
            inv = con.execute(
                "SELECT inv_id, name, kind FROM m7_dependency_inventory"
            ).fetchall()
            gaps = con.execute(
                "SELECT gap_id, record_json, status FROM m7_gap_records"
            ).fetchall()
        finally:
            con.close()
        inv_ids = {r["inv_id"]: (r["name"], r["kind"]) for r in inv}
        gap_inv_refs = set()
        orphan_gaps = []
        for g in gaps:
            try:
                payload = json.loads(g["record_json"])
            except Exception:
                continue
            dep = (payload.get("dependency") or {})
            ref = dep.get("inventory_ref")
            if dep and dep.get("name"):
                if ref:
                    gap_inv_refs.add(ref)
                else:
                    orphan_gaps.append(g["gap_id"])
        missing_gap = [ {"inv_id": i, "name": n, "kind": k}
                        for i, (n, k) in inv_ids.items()
                        if i not in gap_inv_refs ]
        return {"inventory_rows": len(inv_ids),
                "inventory_without_active_gap": missing_gap,
                "dependency_gaps_without_inventory": orphan_gaps,
                "ok": not missing_gap and not orphan_gaps}

    # -- dispatch ---------------------------------------------------------
    def dispatch(self, gap_id: str,
                context: Optional[Dict[str, Any]] = None
                ) -> DispatchResult:
        """Route by shape; run the acquisition leg; close ONLY on verified
        utilization. A failed acquisition leaves the gap OPEN with the
        reason visible in its route history."""
        record = self.get(gap_id)
        if record.status == STATUS_CLOSED:
            return DispatchResult(gap_id, routed=bool(record.route_name),
                                  route_name=record.route_name,
                                  outcome="closed",
                                  detail="already closed",
                                  closing_evidence=record.closing_evidence)
        route = _select_route(record, self._routes)
        if route is None:
            detail = ("no route satisfied by this record's shape "
                      f"(populated blocks: {sorted(record.block_fields())})")
            self._log_run(record, "", "open", detail)
            return DispatchResult(gap_id, routed=False, outcome="open",
                                  detail=detail)
        record.status = STATUS_ACQUIRING
        record.route_name = route.name
        self._save(record)
        try:
            outcome, detail, closing = route.acquire(self, record,
                                                     context or {})
        except Exception as exc:  # acquisition legs fail open, never crash
            outcome, detail, closing = (
                "open",
                f"acquisition leg raised {type(exc).__name__}: {exc}",
                None)
        if outcome == "closed" and closing:
            self._close(record, route.name, closing)
            result = DispatchResult(gap_id, True, route.name, "closed",
                                    detail, closing)
        else:
            record.status = STATUS_OPEN
            self._save(record)
            result = DispatchResult(gap_id, True, route.name, "open", detail)
        self._log_run(record, route.name, result.outcome, detail)
        return result

    def _close(self, record: GapRecord, route_name: str,
              closing_evidence: Dict[str, Any]) -> None:
        """The ONLY path to closed. Requires verified utilization evidence."""
        if not closing_evidence.get("utilization_verified"):
            raise GapRefused("refusing to close: no verified utilization")
        record.status = STATUS_CLOSED
        record.route_name = route_name
        record.closing_evidence = closing_evidence
        record.residual = closing_evidence.get("residual", "")
        self._save(record)
        # the loop observing itself: the closure is an experience record
        try:
            epi = getattr(self._engine, "epistemic_store", None)
            if epi is not None and hasattr(epi, "record_observation"):
                epi.record_observation(
                    f"gap closed: {record.summary} "
                    f"(route {route_name})",
                    source="gap-registry",
                    raw={"gap_id": record.gap_id,
                         "closing_evidence": closing_evidence})
        except Exception:
            pass

    def _log_run(self, record: GapRecord, route_name: str, outcome: str,
                detail: str) -> None:
        record.route_history.append(
            {"at": time.time(), "route": route_name, "outcome": outcome,
             "detail": detail[:2000]})
        self._save(record)
        con = self._conn()
        try:
            con.execute(
                "INSERT INTO m7_route_runs "
                "(run_id, gap_id, at, route_name, outcome, detail) "
                "VALUES (?,?,?,?,?,?)",
                ("run_" + uuid.uuid4().hex[:12], record.gap_id, time.time(),
                 route_name, outcome, detail[:4000]))
            con.commit()
        finally:
            con.close()


def _record_from_dict(payload: Dict[str, Any]) -> GapRecord:
    def block(cls, d):
        if not d:
            return None
        return cls(**{k: v for k, v in d.items()
                      if k in cls.__dataclass_fields__})
    return GapRecord(
        gap_id=payload.get("gap_id", ""),
        registered_at=payload.get("registered_at", 0.0),
        registered_by=payload.get("registered_by", ""),
        summary=payload.get("summary", ""),
        dependency=block(DependencyBlock, payload.get("dependency")),
        limitation=block(LimitationBlock, payload.get("limitation")),
        quarantine=block(QuarantineBlock, payload.get("quarantine")),
        dissatisfaction=block(DissatisfactionBlock,
                             payload.get("dissatisfaction")),
        technique=block(TechniqueBlock, payload.get("technique")),
        data=block(DataBlock, payload.get("data")),
        evidence=payload.get("evidence", []),
        status=payload.get("status", STATUS_OPEN),
        route_name=payload.get("route_name", ""),
        route_history=payload.get("route_history", []),
        closing_evidence=payload.get("closing_evidence"),
        residual=payload.get("residual", ""))


# ---------------------------------------------------------------------------
# Acquisition legs -- the real machinery each route drives.
# Each returns (outcome, detail, closing_evidence).
# "closed" requires closing_evidence with utilization_verified=True.
# ---------------------------------------------------------------------------

def _acquire_substrate(registry: GapRegistry, record: GapRecord,
                       context: Dict[str, Any]
                       ) -> Tuple[str, str, Optional[Dict[str, Any]]]:
    """Route 'substrate': drive M3's governed acquisition path for real.

    Two stages. Stage 1 always drives M3's frozen driver (classification +
    the honest verdict). Stage 2 -- only when the dispatch context carries
    a ``governed_install`` config (operator-supplied: wheel path, pinned
    sha256, import name, scoped target dir) -- performs the governed
    fetch+install that M3's driver names as its missing piece, then
    re-verifies through the frozen restore edge. The install mechanism
    lives in this leg (M7's file) per James's "build the crossing
    mechanism where the architecture permits"; M3's driver file is
    untouched and still driven as stage 1."""
    from swarm_engine.acquisition.substrate import (
        SubstrateAcquisitionDriver)
    dep = record.dependency
    cap_id = (record.quarantine.capability_id
              if record.quarantine else None)
    gov = context.get("governed_install")
    if dep.kind != "data" and gov is None:
        # M3's own honesty, mirrored: the data-only channel cannot provide
        # executable substrate. Name the missing piece; the gap stays open.
        return ("open",
                f"substrate_unavailable: dependency {dep.name!r} is kind "
                f"{dep.kind!r}: the data-only channel cannot provide "
                f"executable substrate. Missing piece: an installable "
                f"{dep.kind} artifact for {dep.name!r} from a trusted "
                f"source (M3 boundary, mirrored)",
                None)
    if dep.kind != "data" and gov is not None:
        return _acquire_substrate_governed(registry, record, context, gov)
    channel = context.get("fetch_channel")
    if channel is None:
        return ("open",
                "substrate route needs operator configuration: no "
                "'fetch_channel' in dispatch context (trusted indexes are "
                "operator config per M3; the registry never invents one)",
                None)
    if not cap_id:
        return ("open",
                "substrate route needs a quarantined capability to restore "
                "(M3's driver walks the real lifecycle); this record "
                "carries no quarantine block", None)
    driver = SubstrateAcquisitionDriver(registry._engine, channel=channel)
    res = driver.attempt(cap_id)
    if res.outcome != "readmitted":
        return ("open",
                f"M3 acquisition did not restore: {res.outcome}: "
                f"{res.detail}", None)
    # Verified utilization: the restored capability must perform. The
    # proof harness supplies the task-specific check; otherwise the
    # frozen re-verification is the bar (stated, not hidden).
    check = context.get("utilization_check")
    if check is not None:
        ok, evidence = check(cap_id)
    else:
        from swarm_engine.synthesis.integrity import verify_capability
        rep = verify_capability(registry._engine, cap_id)
        ok = not rep.failures
        evidence = {"verify_failures": rep.failures,
                    "note": "frozen re-verification (no task check "
                            "supplied in context)"}
    if not ok:
        return ("open",
                f"restored but utilization unverified: {evidence}", None)
    return ("closed",
            f"substrate acquired and utilized: {res.detail}",
            {"utilization_verified": True,
             "acquisition": {"route": "substrate",
                            "driver_outcome": res.outcome,
                            "detail": res.detail},
             "utilization": evidence,
             "residual": ""})


# -- limitation technique leg ---------------------------------------------
# The bound-aware retry: the limitation record names a declared bound the
# substrate enforces. The acquired technique re-invokes the recorded
# reproduction at that bound. Verified by a REAL artifact at the bound.

_SCALE_AXES = ("width", "height", "duration_s", "fps", "size", "scale",
              "resolution", "dim", "pixels")
_NON_SCALE_KEYS = ("seed", "prompt", "out_path", "path", "name", "id")

_BOUND_CHILD = r"""
import json, sys, traceback
spec_path, which, out_path = sys.argv[1], sys.argv[2], sys.argv[3]
with open(spec_path, encoding="utf-8") as fh:
    spec = json.load(fh)
args = spec["args"]
module_name, attr = spec["callable"].split(":")
try:
    mod = __import__(module_name, fromlist=[attr])
    fn = getattr(mod, attr)
    result = fn(**args)
    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump({"which": which, "ok": True,
                   "result": repr(result)[:2000]}, fh)
except Exception:
    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump({"which": which, "ok": False,
                   "error": traceback.format_exc(limit=5)[-2000:]}, fh)
"""


def _scan_declared_bound(failure: str, preventing: str) -> Optional[int]:
    """Generic bound extraction: Q10's patterns first, then a px-scan of
    the preventing text. No numbers are hardcoded; the bound always comes
    from the record."""
    from swarm_engine.synthesis.integrity import _limitation_declared_bound
    bound = _limitation_declared_bound(failure or "")
    if bound is not None:
        return bound
    bound = _limitation_declared_bound(preventing or "")
    if bound is not None:
        return bound
    import re
    m = re.search(r"(\d+)\s*px\b", preventing or "")
    if m:
        return int(m.group(1))
    return None


def _clamp_scale_args(args: Dict[str, Any],
                      bound: int) -> Dict[str, Any]:
    """Clamp numeric scale-axis args to the declared bound. Scale axes are
    a generic vocabulary (width/height/duration/fps/...), never gap- or
    task-specific."""
    out = dict(args)
    for key, val in args.items():
        kl = key.lower()
        if kl in _NON_SCALE_KEYS:
            continue
        if not isinstance(val, (int, float)) or isinstance(val, bool):
            continue
        if val <= bound:
            continue
        if any(ax in kl for ax in _SCALE_AXES):
            out[key] = bound if isinstance(val, int) else float(bound)
    return out


def _run_callable_child(callable_ref: str, args: Dict[str, Any],
                        timeout_s: float = 120.0,
                        workdir: Optional[str] = None
                        ) -> Tuple[bool, str, Dict[str, Any]]:
    """Run module:attr(**args) in a bounded child process (Q10's pattern:
    hard wall timeout, kill on expiry). Never raises."""
    import subprocess
    tmp = workdir or tempfile.mkdtemp(prefix="m7tech_")
    spec_path = os.path.join(tmp, "call.json")
    child_path = os.path.join(tmp, "tech_child.py")
    out_path = os.path.join(tmp, "out.json")
    try:
        with open(spec_path, "w", encoding="utf-8") as fh:
            json.dump({"callable": callable_ref, "args": args}, fh)
        with open(child_path, "w", encoding="utf-8") as fh:
            fh.write(_BOUND_CHILD)
        env = dict(os.environ)
        # realpath: this module is often imported through the
        # pylib/swarm_engine -> ../runtime symlink; resolve it first or the
        # derivation below lands in a nonexistent pylib/pylib.
        pylib = os.path.normpath(os.path.join(
            os.path.dirname(os.path.dirname(os.path.realpath(__file__))),
            "..", "pylib"))
        # Governed substrate installed by earlier gap closures (e.g. the
        # Pillow gap) must be importable in technique-retry children too.
        extra = [p for p in
                 os.environ.get("M7_SUBSTRATE_PATHS", "").split(os.pathsep)
                 if p]
        env["PYTHONPATH"] = (os.pathsep.join([pylib] + extra)
                             + os.pathsep + env.get("PYTHONPATH", ""))
        proc = subprocess.Popen(
            [sys.executable, child_path, spec_path, "bound", out_path],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            env=env, cwd=tmp)
        try:
            proc.communicate(timeout=timeout_s)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.communicate()
            return False, "technique_timeout", {}
        try:
            with open(out_path, encoding="utf-8") as fh:
                side = json.load(fh)
        except Exception as exc:
            return False, f"no child output: {exc}", {}
        if proc.returncode not in (0, None):
            side["ok"] = False
            side.setdefault("error", f"child exit {proc.returncode}")
        if side.get("ok"):
            return True, "ok", side
        return False, str(side.get("error", "unknown"))[:1000], side
    except Exception as exc:
        return False, f"harness: {type(exc).__name__}: {exc}", {}


def _verify_rendered_artifact(task: str, args: Dict[str, Any],
                             target_scale: int) -> Tuple[bool, str]:
    """Real utilization check: the artifact the bound-aware retry produced
    must exist and match the intended scale. Images get dimension checks
    via PIL; anything else gets existence + non-emptiness."""
    out = args.get("out_path") or args.get("path")
    if not out or not os.path.isfile(out):
        return False, f"no artifact at {out!r}"
    if os.path.getsize(out) == 0:
        return False, f"artifact {out!r} is empty"
    if "image" in (task or "").lower():
        w, h = args.get("width"), args.get("height")
        try:
            from PIL import Image
            with Image.open(out) as im:
                iw, ih = im.size
            if w and h and (iw, ih) != (w, h):
                return False, (f"artifact is {iw}x{ih}, expected "
                               f"{w}x{h}")
            if (w, h) != (target_scale, target_scale):
                return False, (f"render not at the intended scale: "
                               f"{w}x{h} vs target {target_scale}")
        except ImportError:
            return False, "PIL unavailable for dimension check"
        except Exception as exc:
            return False, f"artifact unreadable: {exc}"
    return True, f"artifact {out!r} verified at scale {target_scale}"


def _acquire_limitation_technique(
        registry: GapRegistry, record: GapRecord,
        context: Dict[str, Any]) -> Tuple[str, str, Optional[Dict[str, Any]]]:
    """Route 'limitation:technique' -- the bound-aware retry.

    The limitation record names a declared bound the substrate enforces.
    The acquired technique: re-invoke the recorded reproduction with scale
    args clamped to that bound, in a bounded child process, and verify the
    REAL artifact. The technique is recorded as a validated delta (the
    loop observing itself) and the closure names its residual honestly."""
    from swarm_engine.synthesis.integrity import _validate_reproduce_spec
    lim = record.limitation
    rep = lim.reproduce or {}
    spec_err = _validate_reproduce_spec(rep)
    if spec_err:
        return ("open",
                f"reproduce spec invalid ({spec_err}): bound-aware retry "
                "needs a valid reproduction", None)
    bound = _scan_declared_bound(lim.failure or "", lim.preventing or "")
    if bound is None:
        return ("open",
                "no declared bound found in the failure/preventing text: "
                "bound-aware retry cannot name its target", None)
    args = dict(rep.get("args") or {})
    # absolute out_path base: children run in a temp cwd
    out_base = None
    for key in ("out_path", "path"):
        if isinstance(args.get(key), str):
            p = args[key]
            if not os.path.isabs(p):
                p = os.path.join(tempfile.mkdtemp(prefix="m7util_"),
                                 os.path.basename(p))
            out_base = (key, p)
            break
    if out_base is None:
        return ("open",
                "reproduce spec names no artifact path: the retry has no "
                "utilization to verify", None)
    bargs = _clamp_scale_args(args, bound)
    if bargs == args:
        return ("open",
                "no scale arg exceeded the declared bound: nothing for "
                "the retry to change", None)
    # Four real renders at four in-bound scales: M2's causal discipline
    # needs >=4 evidence examples, and each one is a genuine utilization.
    key, base_path = out_base
    base, ext = os.path.splitext(base_path)
    seed0 = args.get("seed")
    scales = [bound, bound * 3 // 4, bound // 2, bound // 4]
    evidence: List[Dict[str, Any]] = []
    primary_check = ""
    for i, s in enumerate(scales):
        if s <= 0:
            continue
        sargs = _clamp_scale_args(args, s)
        sargs[key] = f"{base}_{s}px{ext}"
        if isinstance(seed0, int) and not isinstance(seed0, bool):
            sargs["seed"] = seed0 + i
        ok, detail, side = _run_callable_child(rep["callable"], sargs)
        if not ok:
            return ("open",
                    f"bound-aware retry failed at scale {s} "
                    f"(bound {bound}): {detail}", None)
        vok, vdetail = _verify_rendered_artifact(lim.task, sargs, s)
        if not vok:
            return ("open",
                    f"utilization unverified at scale {s}: {vdetail}", None)
        evidence.append({"input": {"scale": s, "bound": bound,
                                   "seed": sargs.get("seed")},
                         "output": {"artifact": vdetail}})
        if i == 0:
            primary_check = vdetail
    if len(evidence) < 4:
        return ("open",
                f"only {len(evidence)} successful in-bound renders: M2's "
                "causal discipline needs at least 4 evidence examples",
                None)
    # The technique, as a validated delta record (self-observed; stated).
    from swarm_engine.acquisition.delta import DeltaRecord
    technique = (
        "bound-aware retry: when a limitation record names a declared "
        "bound, re-invoke the recorded reproduction with scale args "
        f"clamped to that bound ({bound})")
    delta = DeltaRecord(
        objective=(f"{lim.task} at requested {lim.scale_or_format} "
                   "despite the declared substrate bound"),
        external_actions=("re-invoked the limitation record's reproduce "
                          "callable in a bounded child process with scale "
                          f"args clamped to the declared bound {bound}"),
        prior_capability=("dispatch fails at the requested scale; the "
                          "limitation is recorded but the task is not "
                          "performed"),
        capability_gap=("no automatic fallback to the declared bound: the "
                        "system records the limitation but cannot serve "
                        "the task at any scale"),
        technique=technique,
        evidence=evidence,
        verification={"reproduce_spec": "valid",
                      "artifact": primary_check,
                      "bound_source": "record",
                      "in_bound_scales": [e["input"]["scale"]
                                          for e in evidence]},
        source="limitation-repair").validate()
    closing = {
        "utilization_verified": True,
        "technique": technique,
        "delta_id": delta.delta_id,
        "bound": bound,
        "artifact_check": primary_check,
        "residual": (
            f"direct {lim.scale_or_format} invocation remains beyond the "
            f"declared {bound}px substrate bound; the capability stays "
            "quarantined with its honest limitation reason"),
    }
    return ("closed",
            f"technique acquired and utilized at bound {bound}: "
            f"{primary_check}",
            closing)
def _acquire_limitation_substrate(
        registry: GapRegistry, record: GapRecord,
        context: Dict[str, Any]) -> Tuple[str, str, Optional[Dict[str, Any]]]:
    """Route 'limitation:substrate' -- the limitation is a genuine absence
    at scale (Q10's routing). Delegates to the substrate leg."""
    return _acquire_substrate(registry, record, context)


def _acquire_limitation_synthesis(
        registry: GapRegistry, record: GapRecord,
        context: Dict[str, Any]) -> Tuple[str, str, Optional[Dict[str, Any]]]:
    """Route 'limitation:synthesis' -- the present substrate is incapable
    and no technique crosses it. The synthesis leg that could build the
    crossing mechanism does not exist yet (the synthesis substrate's
    search power is Q6/Q11's boundary). Classified, not pretended: the
    gap stays OPEN with the reason visible."""
    return ("open",
            "synthesis leg unavailable: synthesizing a crossing mechanism "
            "for this limitation needs a stronger synthesis substrate "
            "(Q6's exact next boundary; queued as Q11). The gap stays "
            "open with this reason visible -- not closed, not hidden.",
            None)


def _acquire_technique(registry: GapRegistry, record: GapRecord,
                       context: Dict[str, Any]
                       ) -> Tuple[str, str, Optional[Dict[str, Any]]]:
    """Route 'technique' -- drive M2's real distillation loop on the
    record's validated delta."""
    from swarm_engine.acquisition.delta import DeltaRecord
    from swarm_engine.acquisition.distill import DistillationLoop
    d = record.technique.delta or {}
    try:
        delta = DeltaRecord(
            objective=d.get("objective", ""),
            external_actions=d.get("external_actions", ""),
            prior_capability=d.get("prior_capability", ""),
            capability_gap=d.get("capability_gap", ""),
            technique=d.get("technique", ""),
            evidence=d.get("evidence", []),
            dependencies=d.get("dependencies", []),
            verification=d.get("verification", {}),
            source=d.get("source", "external-ingestion")).validate()
    except Exception as exc:
        return ("open",
                f"technique delta fails causal discipline: {exc}", None)
    epi = getattr(registry._engine, "epistemic_store", None)
    # 2026-09-27 (M7-R1): the import/construction below previously named
    # `Distiller`, which does not exist in this module -- the class is
    # `DistillationLoop`. That ImportError killed the whole technique leg
    # at import time (V9-GEN found it).
    distiller = DistillationLoop(registry._engine, epistemic=epi)
    result = distiller.distill(delta)
    rd = result.as_dict() if hasattr(result, "as_dict") else {}
    # 2026-09-27 (M7-R1): DistillationResult carries `success` + `verdict_admitted`,
    # never `admitted` -- `success` is only set True after promotion +
    # held-out verification pass (distill.py routes A/B), so it IS the
    # admission gate. The old `rd.get("admitted")` check was always falsy
    # and would have left the leg permanently open.
    if rd.get("success") and rd.get("verdict_admitted"):
        return ("closed",
                "technique distilled, verified and admitted via M2",
                {"utilization_verified": True,
                 "distillation": rd,
                 "residual": ""})
    return ("open",
            f"M2 distillation did not admit: {rd.get('status') or rd}",
            None)


def _acquire_dissatisfaction(
        registry: GapRegistry, record: GapRecord,
        context: Dict[str, Any]) -> Tuple[str, str, Optional[Dict[str, Any]]]:
    """Route 'dissatisfaction' -- re-enter M6's acceptance loop at pool
    expansion, steered by the feedback. Honestly bounded: the production
    inlet from run completion into present() is Q8's gate (unnamed
    scheduler/run-control owner), so the leg records the re-entry
    requirement and leaves the gap open with the reason visible."""
    return ("open",
            "dissatisfaction re-entry needs M6's acceptance driver with a "
            "production inlet (Q8's gate: scheduler/run-control owner "
            "unnamed). The near-miss requirement is recorded in the gap; "
            "the gap stays open until the loop can re-enter.",
            None)


def _acquire_data(registry: GapRegistry, record: GapRecord,
                 context: Dict[str, Any]
                 ) -> Tuple[str, str, Optional[Dict[str, Any]]]:
    """Route 'data' -- missing data with a known fetchable source goes
    through the substrate leg; otherwise the gap stays open naming what
    would be needed."""
    if not (record.data.source or "").strip():
        return ("open",
                "missing data with no known source: the gap stays open. "
                "Missing piece: a source the governed channel could "
                "fetch from.", None)
    # Shape-shift into the substrate leg via a synthetic dependency block
    # (the record itself is untouched; routing stays shape-based).
    shim = GapRecord(
        summary=record.summary, quarantine=record.quarantine,
        evidence=record.evidence,
        dependency=DependencyBlock(name=record.data.description,
                                   kind="data",
                                   detail=record.data.source))
    return _acquire_substrate(registry, shim, context)


def _independent_scale_specific(record: GapRecord) -> bool:
    """The record's own fields establish scale-specificity even when the
    diagnosis hint misses it: a valid reproduce spec, an extractable
    declared bound, and a scale number exceeding that bound. Entirely
    shape-derived -- no type branches.

    (Q10 incident, 2026-09-27: _BOUND_PATTERNS misses the "out of bounds
    [lo,hi]" failure format, and the separability probe's
    specific_confirmed never feeds _route_limitation, so route_hint can
    read "synthesis" for a genuinely scale-specific limitation. Q10's
    file is frozen; the registry reads the record's full shape instead
    of trusting the hint alone. The substrate hint is still honored:
    "cannot run in this environment" is not crossed by a retry.)"""
    from swarm_engine.synthesis.integrity import _validate_reproduce_spec
    import re
    lim = record.limitation
    if lim is None:
        return False
    if _validate_reproduce_spec(lim.reproduce or {}):
        return False
    bound = _scan_declared_bound(lim.failure or "", lim.preventing or "")
    if bound is None:
        return False
    nums = [int(n) for n in re.findall(r"\d+", lim.scale_or_format or "")]
    return any(n > bound for n in nums)


def _builtin_routes() -> List[Route]:
    """The route table. Each route declares the shape it serves; the
    dispatcher picks the most specific satisfied route. No type labels,
    no package names, no gap-type branches."""
    return [
        Route(
            name="substrate",
            requires=frozenset({"dependency.name", "dependency.kind"}),
            acquire=_acquire_substrate),
        Route(
            name="limitation_technique",
            requires=frozenset({"limitation.task",
                               "limitation.scale_or_format",
                               "limitation.reproduce",
                               "limitation.route_hint",
                               "quarantine.capability_id"}),
            constraint=lambda r: (
                r.limitation.route_hint == "technique"
                or (r.limitation.route_hint != "substrate"
                    and _independent_scale_specific(r))),
            acquire=_acquire_limitation_technique),
        Route(
            name="limitation_substrate",
            requires=frozenset({"limitation.task",
                               "limitation.scale_or_format",
                               "limitation.reproduce",
                               "limitation.route_hint",
                               "quarantine.capability_id"}),
            constraint=lambda r: (r.limitation.route_hint == "substrate"),
            acquire=_acquire_limitation_substrate),
        Route(
            name="limitation_synthesis",
            requires=frozenset({"limitation.task",
                               "limitation.scale_or_format",
                               "limitation.route_hint"}),
            constraint=lambda r: (r.limitation.route_hint == "synthesis"),
            acquire=_acquire_limitation_synthesis),
        Route(
            name="technique",
            requires=frozenset({"technique.objective",
                               "technique.delta"}),
            acquire=_acquire_technique),
        Route(
            name="dissatisfaction",
            requires=frozenset({"dissatisfaction.attempt_ref",
                               "dissatisfaction.feedback"}),
            acquire=_acquire_dissatisfaction),
        Route(
            name="data",
            requires=frozenset({"data.description", "data.source"}),
            acquire=_acquire_data),
    ]

def _governed_install(dep: DependencyBlock,
                      gov: Dict[str, Any]) -> Tuple[bool, str, Dict[str, Any]]:
    """The governed fetch+install (M3's named missing piece).

    Operator-supplied config only -- nothing is invented here:
      wheel_path:  local path of the installable artifact (the operator or
                   proof resolves it from the trusted index)
      sha256:      pinned hash the artifact must match
      import_name: the module name the artifact provides (never derived
                   from the dependency name by guessing)
      target_dir:  sandbox-scoped install dir (never system site-packages)

    Steps: verify sha256 -> pip install --target --no-deps -> verify the
    import in a FRESH child process with ONLY the scoped dir on the path
    (no ambient leakage) -> make the scoped dir visible to this engine
    process (recorded in the result; this is the acquisition completing).
    Never raises; returns (ok, detail, info).
    """
    import hashlib
    import subprocess
    wheel = gov.get("wheel_path") or ""
    pinned = (gov.get("sha256") or "").lower()
    import_name = gov.get("import_name") or ""
    target = gov.get("target_dir") or ""
    if not (wheel and pinned and import_name and target):
        return (False,
                "governed_install needs wheel_path, sha256, import_name "
                "and target_dir in context['governed_install']", {})
    if not os.path.isfile(wheel):
        return False, f"wheel not found at {wheel!r}", {}
    try:
        h = hashlib.sha256()
        with open(wheel, "rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                h.update(chunk)
    except Exception as exc:
        return False, f"hash read failed: {exc}", {}
    if h.hexdigest().lower() != pinned:
        return (False,
                f"sha256 mismatch: artifact does not match the pinned "
                f"hash (got {h.hexdigest()[:16]}..., refusing install)",
                {})
    os.makedirs(target, exist_ok=True)
    if os.path.abspath(target) == os.path.abspath(sys.prefix):
        return False, "refusing: target_dir must not be the interpreter", {}
    proc = subprocess.run(
        [sys.executable, "-m", "pip", "install", "--quiet",
         "--no-deps", "--target", os.path.abspath(target),
         os.path.abspath(wheel)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=600)
    if proc.returncode != 0:
        return (False,
                f"pip install failed (exit {proc.returncode}): "
                f"{proc.stderr.decode()[-1500:]}", {})
    # Verify in a FRESH child with ONLY the scoped dir importable: the
    # artifact must stand on its own, not on ambient installs.
    probe = ("import sys; sys.path.insert(0, %r); "
             "import %s as m; "
             "print(getattr(m, '__version__', 'unknown'))" % (
                 os.path.abspath(target), import_name))
    proc = subprocess.run(
        [sys.executable, "-I", "-c", probe],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=120,
        env={"PATH": os.environ.get("PATH", "")})
    if proc.returncode != 0:
        return (False,
                f"installed artifact failed child import check: "
                f"{proc.stderr.decode()[-1000:]}", {})
    version = proc.stdout.decode().strip()
    # Make the acquired substrate visible to this engine process. This is
    # the acquisition completing: an install no code can import is not
    # acquired. Recorded, not silent.
    abs_target = os.path.abspath(target)
    if abs_target not in sys.path:
        sys.path.insert(0, abs_target)
    os.environ["M7_SUBSTRATE_PATHS"] = (
        os.environ.get("M7_SUBSTRATE_PATHS", "")
        + (os.pathsep if os.environ.get("M7_SUBSTRATE_PATHS") else "")
        + abs_target)
    return (True,
            f"governed install ok: {dep.name!r} provides {import_name!r} "
            f"version {version} (sha256 verified, scoped to {abs_target})",
            {"import_name": import_name, "version": version,
             "target_dir": abs_target,
             "substrate_path_added": abs_target})


def _acquire_substrate_governed(
        registry: GapRegistry, record: GapRecord,
        context: Dict[str, Any], gov: Dict[str, Any]
        ) -> Tuple[str, str, Optional[Dict[str, Any]]]:
    """Stage 1: M3's real driver (honest verdict). Stage 2: governed
    install. Stage 3: re-verify + restore through the frozen lifecycle
    edge. Stage 4: verified utilization."""
    from swarm_engine.acquisition.substrate import (
        SubstrateAcquisitionDriver)
    dep = record.dependency
    cap_id = (record.quarantine.capability_id
              if record.quarantine else None)
    if not cap_id:
        return ("open",
                "governed substrate route needs a quarantined capability "
                "to restore; this record carries no quarantine block",
                None)
    channel = context.get("fetch_channel")
    driver = SubstrateAcquisitionDriver(registry._engine, channel=channel)
    first = driver.attempt(cap_id)
    first_verdict = f"{first.outcome}: {first.detail[:300]}"
    ok, detail, info = _governed_install(dep, gov)
    if not ok:
        return ("open",
                f"M3 stage-1 verdict [{first_verdict}]; governed install "
                f"failed: {detail}", None)
    # Re-verify + restore through the frozen lifecycle edge (the same edge
    # M3's driver uses for data). The engine's own authority handle.
    from swarm_engine.synthesis.integrity import (
        restore_everywhere, RestoreRefused)
    caller = getattr(registry._engine, "oracle", None)
    try:
        restored = restore_everywhere(
            registry._engine, cap_id, caller=caller,
            reason=(f"substrate acquired: {dep.name!r} via governed install "
                    f"(sha256 pinned, scoped to "
                    f"{info.get('target_dir')})"))
    except (RestoreRefused, Exception) as exc:
        return ("open",
                f"installed ({detail}) but restore refused: "
                f"{type(exc).__name__}: {exc}", None)
    # Verified utilization: the restored capability must perform.
    check = context.get("utilization_check")
    if check is not None:
        uok, evidence = check(cap_id)
    else:
        from swarm_engine.synthesis.integrity import verify_capability
        rep = verify_capability(registry._engine, cap_id)
        uok = not rep.failures
        evidence = {"verify_failures": rep.failures,
                    "note": "frozen re-verification (no task check "
                            "supplied in context)"}
    if not uok:
        return ("open",
                f"restored but utilization unverified: {evidence}", None)
    closing = {"utilization_verified": True,
               "acquisition": {"route": "substrate",
                               "m3_stage1": first_verdict,
                               "governed_install": info,
                               "restore": str(restored)[:1000]},
               "utilization": evidence,
               "residual": ""}
    return ("closed",
            f"substrate acquired via governed install and utilized: "
            f"{detail}", closing)
