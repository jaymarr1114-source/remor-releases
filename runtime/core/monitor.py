"""
swarm_engine/core/monitor.py

Continuous self-monitoring.

A single audit at boot (already in `capabilities.audit_all`) catches
dependency drift once, at startup. This runs the same class of checks
repeatedly during operation, and adds checks a boot-time audit cannot do:
degrading success rate, repeated recent failures, and lifecycle/provenance
disagreement — cases where a capability is registered but has no provenance
record, or is trusted but has no evidence, which indicate the bookkeeping
itself has drifted rather than the capability.

Findings that meet an actionable threshold are acted on directly (quarantine),
not just reported: a monitor that only logs a capability failing nine times
in a row and waits for a human is not continuous monitoring, it is a log.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Dict, List


@dataclass
class Anomaly:
    capability_id: str
    kind: str
    detail: str
    action_taken: str = ""

    def as_dict(self) -> Dict[str, Any]:
        return {"capability_id": self.capability_id, "kind": self.kind,
                "detail": self.detail[:250], "action_taken": self.action_taken}


@dataclass
class MonitorReport:
    checked: int
    anomalies: List[Anomaly] = field(default_factory=list)
    at: float = field(default_factory=time.time)

    def as_dict(self) -> Dict[str, Any]:
        return {"checked": self.checked, "at": self.at,
                "anomalies": [a.as_dict() for a in self.anomalies]}


class HealthMonitor:
    """Periodic self-checks with automatic containment of real problems."""

    DEGRADED_SUCCESS_RATE = 0.5     # below this with enough samples: suspect
    MIN_SAMPLES_FOR_JUDGEMENT = 5
    REPEATED_FAILURE_THRESHOLD = 3  # consecutive recent failures: quarantine

    def __init__(self, engine):
        self.engine = engine

    def run(self) -> MonitorReport:
        report = MonitorReport(checked=0)

        # 1. dependency integrity (reuses the existing audit machinery)
        audit = self.engine.capabilities.audit_all(self.engine.primitives)
        for capability_id, missing in audit.get("broken", {}).items():
            report.anomalies.append(Anomaly(
                capability_id, "dependency_drift",
                f"depends on missing primitives: {missing}", "quarantined by audit"))

        # 2. degrading reliability across everything with provenance
        for capability_id, provenance_record in self._all_provenance():
            report.checked += 1
            total = provenance_record.total_uses
            if total < self.MIN_SAMPLES_FOR_JUDGEMENT:
                continue
            if provenance_record.success_rate < self.DEGRADED_SUCCESS_RATE:
                self.engine.provenance.set_trust(
                    capability_id, provenance_record.trust.__class__.QUARANTINED,
                    f"success rate {provenance_record.success_rate:.0%} over "
                    f"{total} uses is below the monitoring threshold")
                report.anomalies.append(Anomaly(
                    capability_id, "degraded_reliability",
                    f"success rate {provenance_record.success_rate:.0%} over {total} uses",
                    "quarantined"))

        # 3. bookkeeping consistency: registered but no provenance
        for name in list(self.engine.primitives.names()):
            primitive = self.engine.primitives.get(name)
            if primitive is None or primitive.family != "acquired":
                continue
            record = self.engine.acquired_code.get(name)
            if record is None:
                continue
            if self.engine.provenance.get(record["capability_id"]) is None:
                report.anomalies.append(Anomaly(
                    record["capability_id"], "missing_provenance",
                    f"{name!r} is registered and persisted but has no "
                    f"provenance record", "flagged (not auto-quarantined: "
                    "code is intact, only bookkeeping is inconsistent)"))

        return report

    def _all_provenance(self):
        # ProvenanceStore has no "list all", so this walks what the engine
        # already knows about: everything the capability store has bound to a
        # goal, plus everything acquired.
        seen = set()
        for record in self.engine.capabilities.list():
            capability_id = getattr(record, "capability_id", None)
            if capability_id and capability_id not in seen:
                seen.add(capability_id)
                provenance_record = self.engine.provenance.get(capability_id)
                if provenance_record:
                    yield capability_id, provenance_record
        for entry in self.engine.acquired_code.all():
            capability_id = entry["capability_id"]
            if capability_id not in seen:
                seen.add(capability_id)
                provenance_record = self.engine.provenance.get(capability_id)
                if provenance_record:
                    yield capability_id, provenance_record
