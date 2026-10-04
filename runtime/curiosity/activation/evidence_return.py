"""C-9 evidence-return leg (Phase 4c, CUR-P4C).

The curiosity side of the return path: a Primary-requested inquiry's
terminal finding is presented to Primary Acceptance through the REAL
`RelevanceGate.decide` — the gate judges relevance, evaluates, and
records the verdict (admitted / retained / rejected, each with a
record). The curiosity side then reads the verdict back; it never
writes one.

Load-bearing prohibitions (charter C-2.1/C-2.3), enforced by what this
module does NOT contain:
  * NO verdict-writing: the only acceptance writer is the gate's own
    `_record`, reachable solely through `decide()`. This module exposes
    no `set_verdict`, no direct store access, no objective-swapping.
  * NO boundary-closing: a curiosity finding, however correct, yields at
    most an ADMITTED verdict — never a closed Primary boundary. No
    boundary-closing API exists on the curiosity side, and this module
    adds none.
  * Causal cross-check: a finding presented as Primary-requested must
    carry the ACTUAL request's bounded objective and inquiry id. A forged
    objective link is refused here, before the gate ever sees it.

The gate's provenance-link admission trusts the fenced provenance chain
(request -> inquiry -> finding). This module is where the curiosity
side keeps that chain honest: the check below ties the finding back to
the live take-up record, so a stamped objective that was never
requested cannot ride the link.
"""

from __future__ import annotations

import json
from typing import Any, Dict, Optional

from swarm_engine.core.executive.relevance import (
    Finding as GateFinding,
    FindingProvenance as GateProvenance,
)

from .request import ST_ACCEPTED
from ..evidence.store import CuriosityEvidenceStore


class ReturnRefused(Exception):
    """The return leg refuses to present: not a finding, failed the
    causal cross-check, or the presentation contract is unmet. Named,
    never silent."""


def _expected_bounded_objective(request_view: Dict[str, Any]) -> str:
    """Recompute the bounded objective the take-up stamped on the
    trigger, from the live request record (mirrors
    ActivationTakeUp._bounded_objective)."""
    ref = request_view["operational_objective_ref"]
    scope = (request_view.get("bounded_requirement") or {}).get("scope") or ""
    scope = scope.strip()
    return (f"{ref['objective_id']}: {ref['statement']}"
            + (f" [scope: {scope}]" if scope else ""))


def _payload_text(payload_ref: str) -> str:
    """Read the persisted payload envelope and extract judgeable text.
    The envelope is loop-agnostic; the text below is what the
    relevance gate scores. Unreadable payload -> refused: relevance
    cannot be judged against nothing."""
    try:
        with open(payload_ref, "r", encoding="utf-8") as fh:
            payload = json.load(fh)
    except (OSError, ValueError) as exc:
        raise ReturnRefused(
            f"payload unreadable at {payload_ref!r}: {exc}")
    parts = []
    for key in ("precise_question", "question_text", "claim"):
        val = payload.get(key)
        if isinstance(val, str) and val.strip():
            parts.append(val.strip())
    passes = payload.get("passes")
    if isinstance(passes, list):
        for p in passes:
            if isinstance(p, str) and p.strip():
                parts.append(p.strip())
            elif isinstance(p, dict):
                txt = p.get("text") or p.get("content") or p.get("answer")
                if isinstance(txt, str) and txt.strip():
                    parts.append(txt.strip())
    text = "\n".join(parts).strip()
    if not text:
        raise ReturnRefused(
            f"payload at {payload_ref!r} carries no judgeable text")
    return text


def _payload_inquiry_id(payload_ref: str) -> Optional[str]:
    try:
        with open(payload_ref, "r", encoding="utf-8") as fh:
            payload = json.load(fh)
    except (OSError, ValueError):
        return None
    val = payload.get("inquiry_id")
    return val if isinstance(val, str) else None


def present_for_acceptance(evidence_id: str, *, request_id: Optional[str],
                           takeup: Any, evidence_store: CuriosityEvidenceStore,
                           gate: Any) -> Dict[str, Any]:
    """Present one terminal finding to Primary Acceptance.

    `request_id` names the Primary activation request behind the
    finding; None means curiosity-initiated (handled distinctly —
    never stamped as Primary-requested). `gate` is the REAL
    RelevanceGate; the ONLY gate method this function touches is
    `decide()`.

    Returns the linkage record
    {evidence_id, request_id, inquiry_id, requested_by_primary,
     verdict, criterion, score, decided_at}.
    Raises ReturnRefused (named) on any contract failure.
    """
    try:
        finding = evidence_store.get(evidence_id)
    except Exception as exc:
        raise ReturnRefused(
            f"evidence store unreadable for {evidence_id!r}: {exc}")
    if finding is None:
        raise ReturnRefused(
            f"unknown evidence_id {evidence_id!r}: nothing to present")
    # Not-a-finding: KILLED terminations and other non-finding shapes
    # carry no charter terminal state / provenance block.
    terminal_state = getattr(finding, "terminal_state", None)
    provenance = getattr(finding, "provenance", None)
    if not terminal_state or provenance is None:
        raise ReturnRefused(
            f"{evidence_id!r} is not a finding: refusing presentation "
            f"(terminations are not findings)")
    # Fenced validation (charter C-7/C-2.2 admissibility).
    finding.validate()

    requested_by_primary = False
    inquiry_id: Optional[str] = None
    if request_id is not None:
        if takeup is None:
            raise ReturnRefused(
                "request_id given but no take-up handle: cannot "
                "cross-check the request")
        req_view = takeup.get_request(request_id)
        if req_view is None:
            raise ReturnRefused(
                f"unknown activation request {request_id!r}")
        if req_view.get("state") != ST_ACCEPTED or not req_view.get(
                "inquiry_id"):
            raise ReturnRefused(
                f"request {request_id!r} is {req_view.get('state')}: only "
                f"an ACCEPTED request's finding may ride the "
                f"Primary-requested path")
        if finding.origin != "PRIMARY_REQUESTED":
            raise ReturnRefused(
                f"finding {evidence_id!r} has origin "
                f"{finding.origin!r}: not a Primary-requested finding")
        expected = _expected_bounded_objective(req_view)
        if finding.bounded_objective != expected:
            raise ReturnRefused(
                f"finding {evidence_id!r} stamps bounded objective "
                f"{finding.bounded_objective!r} but request "
                f"{request_id!r} carries {expected!r}: forged "
                f"objective link refused")
        payload_inq = _payload_inquiry_id(finding.payload_ref)
        if payload_inq != req_view["inquiry_id"]:
            raise ReturnRefused(
                f"finding {evidence_id!r} payload names inquiry "
                f"{payload_inq!r} but request {request_id!r} ran "
                f"{req_view['inquiry_id']!r}: inquiry linkage broken")
        requested_by_primary = True
        inquiry_id = req_view["inquiry_id"]
        # The gate's provenance link keys on the objective ID. Take it
        # from the REQUEST record (authoritative), not the finding's
        # stamp — the stamp was already cross-checked above.
        objective_id = req_view["operational_objective_ref"]["objective_id"]
    else:
        objective_id = finding.bounded_objective

    # Adapt to the gate's Finding shape (read-only import of the
    # Primary-side record types; the gate OBJECT is supplied by the
    # caller — this module never constructs or configures gates).
    gate_finding = GateFinding(
        finding_id=finding.evidence_id,
        content=_payload_text(finding.payload_ref),
        provenance=GateProvenance(
            source_loop=finding.provenance.loop,
            bounded_objective_id=objective_id,
            requested_by_primary=requested_by_primary,
            triage=finding.provenance.triage,
        ),
        terminal_state=finding.terminal_state,
    )
    # The ONLY gate method touched: decide(). No set_objective, no
    # store access, no verdict writing exists on this path.
    decision = gate.decide(gate_finding)
    return {
        "evidence_id": evidence_id,
        "request_id": request_id,
        "inquiry_id": inquiry_id,
        "requested_by_primary": requested_by_primary,
        "verdict": decision.verdict,
        "criterion": decision.criterion,
        "score": decision.score,
        "decided_at": decision.decided_at,
    }
