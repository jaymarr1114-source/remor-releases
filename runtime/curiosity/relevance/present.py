"""Honest relevance input for Primary Acceptance (CUR-P5C).

A curiosity finding enters Primary Acceptance's relevance question through
this module. The contract, enforced by construction:

- Content is VERBATIM. The presenter takes the finding's content as given
  and passes it through unchanged. It never pads, stems, expands, or
  otherwise games the gate's token-recall scoring. Keyword stuffing to
  clear the D-4 threshold would be fabrication; this module cannot do it
  because it has no code path that mutates content.
- No score is computed or set. The gate's Finding shape has no score
  field; the score exists only in the gate's RelevanceDecision, produced
  by the gate. There is deliberately no score parameter, attribute, or
  knob anywhere in this module.
- Provenance is VERBATIM, triage advisory. source_loop,
  bounded_objective_id, requested_by_primary, and the curiosity triage
  label are carried through unchanged. Triage is recorded as advisory
  provenance per charter C-3.3 -- it never decides.
- Fail-closed. A finding with empty content or missing provenance is
  refused here, before it reaches the gate: presenting nothing (or
  nothing attributable) as "relevant" is itself dishonest.

What this module does NOT do (by absence, verified in the battery):
- No verdict API. It cannot admit, retain, reject, or flip anything.
- No threshold. The D-4 0.25 threshold lives in the gate; this module
  never names it, reads it, or depends on it.
- No Primary-side writes. It builds the input record and hands it to
  the caller's acceptance inlet. The inlet decides.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Dict, Optional


class RelevanceInputRefused(Exception):
    """The curiosity finding cannot be honestly presented: fail closed."""


@dataclass(frozen=True)
class RelevanceInput:
    """The curiosity side's relevance presentation: what is claimed.

    This is a record of the CLAIM, not a judgment. `content` is the
    finding's content verbatim; `claimed_objective_id` names the
    operational objective the curiosity side asserts relevance to. The
    gate checks the claim; this record only states it, auditably.
    """

    finding_id: str
    content: str
    source_loop: str
    bounded_objective_id: str
    requested_by_primary: bool
    terminal_state: str
    triage: Optional[str]
    claimed_objective_id: str
    presented_at: float = field(default_factory=time.time)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "finding_id": self.finding_id,
            "content": self.content,
            "source_loop": self.source_loop,
            "bounded_objective_id": self.bounded_objective_id,
            "requested_by_primary": self.requested_by_primary,
            "terminal_state": self.terminal_state,
            "triage": self.triage,
            "claimed_objective_id": self.claimed_objective_id,
            "presented_at": self.presented_at,
        }


def build_gate_finding(curiosity_finding: Any, *, content: str,
                       claimed_objective_id: str) -> Any:
    """Adapt a curiosity finding to the Primary gate's Finding shape.

    Honest by construction:
    - `content` is passed through VERBATIM (caller's responsibility to
      supply the finding's real content; this function never mutates it).
    - Provenance fields are copied VERBATIM from the curiosity finding.
    - `requested_by_primary` is derived from the curiosity origin, never
      asserted: True only for the PRIMARY_REQUESTED origin.
    - No score is computed, set, or returned. The gate judges.

    Raises RelevanceInputRefused on empty content or missing provenance:
    fail closed rather than present the unpresentable.
    """
    # Local import: this package must not import Primary-side machinery
    # at module load; the gate's shapes are imported lazily so the
    # curiosity side's input stays decoupled from the Primary side.
    from ...core.executive.relevance import Finding, FindingProvenance

    if not (content or "").strip():
        raise RelevanceInputRefused(
            "cannot present a finding with empty content: there is "
            "nothing whose relevance could be judged")
    prov = getattr(curiosity_finding, "provenance", None)
    for attr in ("loop", "bounded_objective", "model"):
        if not (getattr(prov, attr, None) or "").strip():
            raise RelevanceInputRefused(
                f"cannot present a finding with missing provenance.{attr}")
    origin = getattr(curiosity_finding, "origin", "")
    terminal_state = getattr(curiosity_finding, "terminal_state", "")
    if not (terminal_state or "").strip():
        raise RelevanceInputRefused(
            "cannot present a finding with no terminal state")
    evidence_id = getattr(curiosity_finding, "evidence_id", "")
    if not (evidence_id or "").strip():
        raise RelevanceInputRefused(
            "cannot present a finding with no evidence_id")

    return Finding(
        finding_id=evidence_id,
        content=content,  # VERBATIM -- never padded, stemmed, or expanded
        provenance=FindingProvenance(
            source_loop=prov.loop,
            bounded_objective_id=prov.bounded_objective,
            # Derived from origin, never asserted:
            requested_by_primary=(origin == "PRIMARY_REQUESTED"),
            triage=getattr(prov, "triage", None),  # advisory (C-3.3)
        ),
        terminal_state=terminal_state,
    )


def present_for_acceptance(curiosity_finding: Any, *, content: str,
                           claimed_objective_id: str) -> RelevanceInput:
    """Record the curiosity side's relevance claim, auditably.

    Returns the RelevanceInput record of what was claimed. The caller
    passes the returned record's fields to the real acceptance inlet
    (LivePath.submit_finding / ExecutiveController.submit_finding);
    the inlet -- the Primary side -- decides. This function judges
    nothing and admits nothing.
    """
    gate_finding = build_gate_finding(
        curiosity_finding, content=content,
        claimed_objective_id=claimed_objective_id)
    return RelevanceInput(
        finding_id=gate_finding.finding_id,
        content=gate_finding.content,
        source_loop=gate_finding.provenance.source_loop,
        bounded_objective_id=gate_finding.provenance.bounded_objective_id,
        requested_by_primary=gate_finding.provenance.requested_by_primary,
        terminal_state=gate_finding.terminal_state,
        triage=gate_finding.provenance.triage,
        claimed_objective_id=claimed_objective_id,
    )
