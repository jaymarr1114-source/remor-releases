"""C-9 kill terminations: named KILLED records for Primary-requested inquiries.

Design: architecture/phase0-c9-activation-design_2026-09-29.md §6 (kill),
R7 in the refusal-state table.

A termination is NOT a finding, and the type boundary holds in both
directions:
- it carries no charter terminal_state (no QUESTION_RESOLVED,
  HYPOTHESIS_SUPPORTED, ...), no provenance block, no evidence payload;
- it names the enforcement act: D-3 level, reason, issuer, checkpoint,
  and the preserved partial evidence (INCONCLUSIVE, C-9.4).

It is written exactly once, after a REAL kill executed through the run
controller, and it is cross-checked against the run controller's live
inquiry record (state == KILLED) so a fabricated kill-result dict cannot
mint a termination. The Primary side reads it back through the request's
view() — the defined return channel (P4A).
"""

from __future__ import annotations

import time
from typing import Any, Dict

from .request import R_KILLED, ST_ACCEPTED, ST_KILLED


class TerminationRefused(Exception):
    """Fail-closed: a termination that is not backed by a real kill."""


def build_termination(*, level: str, reason: str, issuer: str,
                      checkpoint_id: str | None,
                      evidence_id: str | None,
                      spent_s: float) -> Dict[str, Any]:
    """Pure builder for the termination record (no I/O, no mutation)."""
    if not (level or "").strip():
        raise TerminationRefused("D-3 level is required on a kill termination")
    if not (reason or "").strip():
        raise TerminationRefused("kill reason is required on a kill termination")
    return {
        "verdict": R_KILLED,
        "level": level,
        "reason": reason,
        "issuer": issuer,
        "killed_at": time.time(),
        "checkpoint_id": checkpoint_id,
        "partial_evidence": ({
            "evidence_id": evidence_id,
            "terminal_state": "INCONCLUSIVE",
        } if evidence_id else None),
        "spent_s": round(spent_s, 6),
        # Explicit type marker: this record is not a finding and must
        # never enter a finding inlet.
        "record_type": "kill_termination",
    }


def record_kill_termination(takeup: Any, request_id: str,
                            kill_result: Dict[str, Any], *,
                            level: str, issuer: str) -> Dict[str, Any]:
    """Write the KILLED termination onto a Primary-requested inquiry.

    Fail-closed: the request must exist and be ST_ACCEPTED with an inquiry
    id matching the kill result; the kill result must describe a real kill
    (state KILLED); and the run controller's LIVE inquiry record must show
    that inquiry KILLED (a fabricated dict alone mints nothing).
    Written exactly once: a request that already carries a termination
    refuses a second write.
    """
    req = takeup._requests.get(request_id)
    if req is None:
        raise TerminationRefused(
            f"unknown activation request {request_id!r}: nothing to terminate")
    if req.termination is not None:
        raise TerminationRefused(
            f"request {request_id!r} already carries a kill termination: "
            f"terminations are written exactly once")
    if req.state != ST_ACCEPTED or req.inquiry_id is None:
        raise TerminationRefused(
            f"request {request_id!r} is {req.state}: only a live accepted "
            f"request can be kill-terminated")
    if not isinstance(kill_result, dict):
        raise TerminationRefused("kill_result must be the run controller's "
                                 "kill record dict")
    if kill_result.get("inquiry_id") != req.inquiry_id:
        raise TerminationRefused(
            f"kill result names inquiry {kill_result.get('inquiry_id')!r}, "
            f"request names {req.inquiry_id!r}: not the same inquiry")
    if kill_result.get("state") != ST_KILLED:
        raise TerminationRefused(
            f"kill result state is {kill_result.get('state')!r}: not a kill")
    # Causal cross-check: the run controller's LIVE record must show this
    # inquiry KILLED. inquiry_views() is the public loop-level aggregate.
    live = {v["inquiry_id"]: v for v in takeup._rc.inquiry_views()}
    view = live.get(req.inquiry_id)
    if view is None or view.get("state") != ST_KILLED:
        raise TerminationRefused(
            f"run controller shows inquiry {req.inquiry_id!r} as "
            f"{view.get('state') if view else 'absent'}: no real kill "
            f"backs this termination")
    termination = build_termination(
        level=level, reason=kill_result.get("kill_reason") or "",
        issuer=issuer,
        checkpoint_id=kill_result.get("checkpoint_id"),
        evidence_id=kill_result.get("evidence_id"),
        spent_s=float(kill_result.get("spent_s") or 0.0))
    req.termination = termination
    req.state = ST_KILLED
    return req.view()
