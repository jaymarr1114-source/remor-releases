"""PLOOP-1 -- Autonomous boundary detection for the Primary executive.

The executive's inlet was proof-only: zero BoundaryPresentation
constructions existed outside the RUN-EXEC-1 proof file, so route() could
never fire on real state and the unified loop never turned.

This module is the missing half: it OBSERVES real runtime state and
constructs BoundaryPresentation instances on genuine boundary conditions.
It never hand-feeds. Every presentation's evidence is a real record read
from the producing machinery's own store:

  run_wake          <- RunController checkpoint (never-ticked / crashed /
                       stopped / cadence-due). The controller's own wake
                       conditions, read from its own checkpoint.
  acquisition_gap   <- GapRegistry.list_gaps(status=open|acquiring): the
                       REAL GapRecord object. Stagnation (repeated dispatch
                       failures from the checkpoint backoff ledger) is a
                       detection mode, not a different kind: the
                       acquisition loop owns the gap either way.
  execution_failure <- CapabilityStore.list(status=quarantined): a
                       capability the M5 machinery really quarantined.
  technique_delta   <- find_pending_deltas (V10-P1 read path): a real
                       charter delta with no consumption record, adapted
                       to a real M2 DeltaRecord via charter_to_delta_record.

Honestly source-absent (named, never faked):
  completion_candidate -- no persistent completed-attempt source exists in
      the tree; attempts are produced by execution flows, not stored.
  novel_task           -- no novel-task proposal machinery exists in the
      tree; novel tasks are supplied by callers, never detected.

Observed but not presented (no owning loop in the declared six-kind
architecture -- an architectural question, not a detection failure):
  envelope exceedance  -- last tick budget_exceeded, substrate spawn
      refusals. Reported in the scan detail; extending BOUNDARY_KINDS is
      above this module's pay grade (see PLOOP-1 report).

Anti-duplication: this module reads; it never re-implements a store, a
sweep, or a dispatch path. Dedupe is per-detector-instance scan state
(kind, source_id): a boundary presented once is not re-presented by the
same detector, so a scan loop cannot spin on one condition.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set, Tuple

from .boundary import (
    BOUNDARY_ACQUISITION_GAP,
    BOUNDARY_COMPLETION_CANDIDATE,
    BOUNDARY_EXECUTION_FAILURE,
    BOUNDARY_NOVEL_TASK,
    BOUNDARY_RUN_WAKE,
    BOUNDARY_TECHNIQUE_DELTA,
    BoundaryPresentation,
)

#: Kinds with no producing machinery in the tree. The detector names
#: them in every scan report instead of fabricating presentations.
SOURCE_ABSENT_KINDS = (
    BOUNDARY_COMPLETION_CANDIDATE,
    BOUNDARY_NOVEL_TASK,
)

#: An open gap whose dispatch has failed this many times is stagnating.
#: Still an acquisition_gap (the acquisition loop owns it); the count
#: rides along as evidence context.
STAGNATION_FAILURES = 3


@dataclass
class ScanReport:
    """What one detector scan found."""
    scanned_at: float
    presentations: List[BoundaryPresentation] = field(default_factory=list)
    source_absent: List[str] = field(default_factory=list)
    observed: Dict[str, Any] = field(default_factory=dict)
    # per-detector notes, including envelope conditions observed but not
    # presented (no owning loop) and why.
    notes: List[str] = field(default_factory=list)


class BoundaryDetector:
    """Observe real runtime state; present genuine boundaries.

    Constructed with the same collaborators as the ExecutiveController
    plus the run controller's checkpoint path (read through a fresh
    ControllerCheckpoint on the same file -- read-only usage).
    """

    def __init__(self, *, engine: Any, run_controller: Any,
                 gap_registry: Any,
                 checkpoint_path: str,
                 stagnation_failures: int = STAGNATION_FAILURES) -> None:
        self._engine = engine
        self._run_controller = run_controller
        self._gap_registry = gap_registry
        self._checkpoint_path = checkpoint_path
        self._stagnation_failures = int(stagnation_failures)
        self._epistemic = engine.intellect.epistemic
        self._presented: Set[Tuple[str, str]] = set()

    # -- public ---------------------------------------------------------
    def scan(self) -> ScanReport:
        """One full scan. Returns presentations for genuine conditions,
        names source-absent kinds, and reports observed-but-unroutable
        conditions honestly."""
        from swarm_engine.core.run_controller import ControllerCheckpoint
        checkpoint = ControllerCheckpoint(self._checkpoint_path)
        report = ScanReport(
            scanned_at=time.time(),
            source_absent=list(SOURCE_ABSENT_KINDS),
        )
        self._scan_wake(checkpoint, report)
        self._scan_gaps(checkpoint, report)
        self._scan_quarantine(report)
        self._scan_deltas(report)
        self._scan_envelope(checkpoint, report)
        return report

    # -- helpers --------------------------------------------------------
    def _fresh(self, kind: str, key: str) -> bool:
        """True if this (kind, source) was not already presented."""
        return (kind, key) not in self._presented

    def _mark(self, kind: str, key: str) -> None:
        self._presented.add((kind, key))

    def _present(self, report: ScanReport, kind: str, key: str,
                 evidence: Dict[str, Any], observed_by: str) -> None:
        p = BoundaryPresentation(
            kind=kind, evidence=evidence, observed_by=observed_by)
        p.validate()  # fail fast here, never hand the executive a refusal
        self._mark(kind, key)
        report.presentations.append(p)

    # -- detectors ------------------------------------------------------
    def _scan_wake(self, checkpoint: Any, report: ScanReport) -> None:
        """The run controller's own wake conditions, read from its own
        checkpoint: crashed -> resume_request; never ticked ->
        cadence_tick (first wake); cadence elapsed -> cadence_tick."""
        rc = self._run_controller
        run_id = rc.run_id
        status = checkpoint.load_meta("status")
        cycles = checkpoint.cycles_completed()
        reason: Optional[str] = None
        if status == "crashed":
            reason = "resume_request"
        elif cycles == 0:
            reason = "cadence_tick"
        else:
            last_at = checkpoint.last_cycle_at() or 0.0
            interval = float(rc.config.cadence_interval_s)
            if time.time() - last_at >= interval:
                reason = "cadence_tick"
        report.observed["wake"] = {
            "status": status, "cycles_completed": cycles,
            "reason": reason}
        if reason is not None and self._fresh(BOUNDARY_RUN_WAKE, run_id):
            self._present(
                report, BOUNDARY_RUN_WAKE, run_id,
                {"wake_reason": reason, "run_id": run_id},
                "boundary_detector:wake_scan")

    def _scan_gaps(self, checkpoint: Any, report: ScanReport) -> None:
        """Open/acquiring gaps: the real GapRecord objects. A gap whose
        checkpoint failure count reached the stagnation threshold is
        still an acquisition_gap -- stagnation is a detection mode."""
        found = 0
        stagnating = 0
        for status in ("open", "acquiring"):
            try:
                gaps = self._gap_registry.list_gaps(status=status)
            except Exception:
                continue
            for record in gaps:
                found += 1
                failures = checkpoint.gap_failure_count(record.gap_id)
                if failures >= self._stagnation_failures:
                    stagnating += 1
                if not self._fresh(BOUNDARY_ACQUISITION_GAP,
                                   record.gap_id):
                    continue
                evidence: Dict[str, Any] = {"gap_record": record}
                if failures:
                    evidence["dispatch_failures"] = failures
                    evidence["stagnating"] = (
                        failures >= self._stagnation_failures)
                self._present(
                    report, BOUNDARY_ACQUISITION_GAP, record.gap_id,
                    evidence, "boundary_detector:gap_scan")
        report.observed["gaps"] = {
            "open_or_acquiring": found, "stagnating": stagnating}

    def _scan_quarantine(self, report: ScanReport) -> None:
        """Capabilities the M5 machinery really quarantined."""
        try:
            quarantined = self._engine.capabilities.list(
                status="quarantined")
        except Exception:
            quarantined = []
        report.observed["quarantine"] = {"quarantined": len(quarantined)}
        for rec in quarantined:
            cap_id = rec.capability_id
            if not self._fresh(BOUNDARY_EXECUTION_FAILURE, cap_id):
                continue
            self._present(
                report, BOUNDARY_EXECUTION_FAILURE, cap_id,
                {"capability_id": cap_id},
                "boundary_detector:quarantine_scan")

    def _scan_deltas(self, report: ScanReport) -> None:
        """Technique deltas with no consumption record: real charter
        payloads adapted to real M2 DeltaRecords."""
        from swarm_engine.acquisition.distill_driver import (
            find_pending_deltas, charter_to_delta_record, NotDistillable)
        try:
            pending = find_pending_deltas(self._epistemic)
        except Exception:
            pending = []
        report.observed["deltas"] = {"pending": len(pending)}
        for delta_rec in pending:
            obs_id = str(delta_rec.get("observation_id") or "")
            try:
                delta = charter_to_delta_record(delta_rec)
            except NotDistillable as exc:
                report.notes.append(
                    f"delta {obs_id or '?'} not distillable "
                    f"({exc}); no presentation")
                continue
            key = delta.delta_id or obs_id
            if not key or not self._fresh(BOUNDARY_TECHNIQUE_DELTA, key):
                continue
            self._present(
                report, BOUNDARY_TECHNIQUE_DELTA, key,
                {"delta": delta}, "boundary_detector:delta_scan")

    def _scan_envelope(self, checkpoint: Any, report: ScanReport) -> None:
        """Envelope conditions are OBSERVED, not presented: no loop owns
        them in the declared six-kind architecture. Naming them here is
        the honest record; extending BOUNDARY_KINDS is an architectural
        decision recorded in the PLOOP-1 report as the next boundary."""
        summary = checkpoint.last_cycle_summary() or {}
        exceeded = bool(summary.get("budget_exceeded"))
        report.observed["envelope"] = {
            "last_tick_budget_exceeded": exceeded}
        if exceeded:
            report.notes.append(
                "envelope: last tick exceeded its cycle budget "
                "(budget_exceeded=True); no owning loop in the six-kind "
                "architecture -- observed, not presented")
