"""First-pass triage (charter C-3.3): the curiosity side's advisory pass
over terminal findings.

`triage_finding()` takes a terminal finding (pre-persist, triage
unassigned) plus its payload dict, runs the mechanical rules
(`rules.assign_triage`), records the append-only ledger event, and
returns the finding with `provenance.triage` stamped. Persisting is the
caller's job, through the normal fenced write path -- this package never
persists, never admits, never promotes.

Deliberately absent from this package: admit(), promote(),
register_capability(), or any API that could turn a triage flag into an
admission. Triage is not admission (C-3.3); the package is shaped so the
prohibition is structural, not just documented.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any, Dict, Optional, Tuple

from .ledger import TriageEvent, TriageLedger
from .rules import assign_triage


def triage_finding(*, finding: Any, payload: Optional[Dict[str, Any]],
                   ledger: TriageLedger) -> Tuple[Any, TriageEvent]:
    """Run the first-pass triage over a terminal finding.

    The finding must be a validated curiosity finding whose provenance
    triage is unassigned (None). Returns (finding_with_triage, event).
    The ledger event carries cause 'first-pass' and cites the finding's
    own evidence_id.

    Raises TriageRefused if the finding already carries a triage
    (first-pass runs once; use the ledger's re-triage path with new
    evidence for updates) or if the content is untriagable.
    """
    from .rules import TriageRefused
    if finding.provenance.triage is not None:
        raise TriageRefused(
            f"refusing first-pass triage of {finding.evidence_id!r}: "
            f"provenance.triage is already {finding.provenance.triage!r} "
            f"-- first-pass runs once; re-triage requires new evidence "
            f"via the ledger")
    triage_flag, rule_id = assign_triage(
        terminal_state=finding.terminal_state, payload=payload)
    event = ledger.record_triage(
        finding_id=finding.evidence_id, triage=triage_flag, rule_id=rule_id,
        cause="first-pass", evidence_ref=finding.evidence_id)
    stamped = replace(
        finding,
        provenance=replace(finding.provenance, triage=triage_flag))
    return stamped, event


def retriage(*, finding_id: str, new_evidence_ref: str, cause: str,
             triage: str, rule_id: str,
             ledger: TriageLedger) -> TriageEvent:
    """Record a re-triage event. Requires NEW evidence (an evidence_ref
    not already cited for this finding) -- otherwise TriageRefused.
    History stays append-only; readers see latest-wins via
    `ledger.get_triage()`."""
    return ledger.record_triage(
        finding_id=finding_id, triage=triage, rule_id=rule_id,
        cause=cause, evidence_ref=new_evidence_ref)


def persist_triaged(*, finding: Any, store: Any) -> Any:
    """Persist a triaged finding through the fenced write path.

    Defined here (inside the curiosity domain) so the writer's domain
    fence admits the call -- batteries must never call the writer
    directly from __main__. The finding must already carry its triage
    assignment; un-triaged findings are refused (first-pass triage is
    not optional).
    """
    from .rules import TriageRefused
    from swarm_engine.curiosity.evidence.writer import CuriosityWriter
    if finding.provenance.triage is None:
        raise TriageRefused(
            f"refusing to persist {finding.evidence_id!r} without a "
            f"triage assignment: first-pass triage runs before persist, "
            f"never after")
    writer = CuriosityWriter(store)
    return writer.submit(finding)
