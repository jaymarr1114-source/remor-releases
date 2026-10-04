"""C-9 take-up: Primary activation request → Curiosity Executive decision.

Design: architecture/phase0-c9-activation-design_2026-09-29.md §2
(take-up decision procedure), §4 (refusal table R1–R7), §5 (withdrawal A28).

The take-up evaluates in the executive's as-built fixed order (kill state →
loop ownership → grant → fit; the design's §2 specified kill → grant → fit
and Phase 2 built ownership before grant — every refusal is named either
way, and the deviation is recorded in the mission report, not hidden).

This module consumes the executive's REAL request_activation and the run
controller's REAL dispatch/stop paths. It never re-implements their checks.

Field ownership: this module writes ONLY the curiosity-owned fields of
ActivationRequest (state, decision, decision_basis, inquiry_id,
withdrawn_at, withdrawn_reason). Issuance fields are Primary-written.

The Primary-side ActivationRequestStore is UNBUILT (Primary-track-owned);
the decision record written here (readable via get_request) is the defined
return channel the Primary side binds.
"""

from __future__ import annotations

import re
import time
from typing import Any, Dict, Optional

from .request import (
    ST_ACCEPTED,
    ST_KILLED,
    ST_REFUSED,
    ST_REQUESTED,
    ST_WITHDRAWN,
    R_CURIOSITY_DISABLED,
    R_DUPLICATE_ACTIVE_REQUEST,
    R_REFUSED_FIT,
    R_REFUSED_GRANT,
    R_REQUEST_MALFORMED,
    R_WITHDRAWN,
    ActivationRequest,
    DuplicateActiveRequest,
    RequestMalformed,
)

from swarm_engine.curiosity.executive.boundary import (
    ORIGINS,
    CuriosityTrigger,
    new_trigger,
)
from swarm_engine.curiosity.executive.executive import (
    ABSENT_OWNERSHIP,
    LOOP_OWNERSHIP,
    ActivationRefused,
    R_ATTESTATION_INVALID,
    R_FIT,
    R_INVALID_TRIGGER,
    R_KILL_STATE,
    R_LOOP_ABSENT,
    R_NO_BUDGET,
    R_NO_SLOT,
    R_UNOWNED,
)

# Refusal-name mapping: executive reason prefix -> C-9 refusal name.
_REFUSAL_MAP = {
    R_KILL_STATE: R_CURIOSITY_DISABLED,
    R_NO_BUDGET: R_REFUSED_GRANT,
    R_NO_SLOT: R_REFUSED_GRANT,
    R_LOOP_ABSENT: R_REFUSED_FIT,
    R_UNOWNED: R_REFUSED_FIT,
    R_FIT: R_REFUSED_FIT,
    R_ATTESTATION_INVALID: R_REFUSED_FIT,
    R_INVALID_TRIGGER: R_REQUEST_MALFORMED,  # defensive; validated first
}

_KNOWN_CLASSES = (frozenset(LOOP_OWNERSHIP) | frozenset(ABSENT_OWNERSHIP))


class TakeUpError(Exception):
    """Named take-up failure that is not a C-9 refusal (unknown request)."""


def _reason_prefix(reason: str) -> str:
    return (reason or "").split(":", 1)[0].strip()


class ActivationTakeUp:
    """Curiosity-side take-up for Primary activation requests."""

    def __init__(self, *, executive: Any, run_controller: Any) -> None:
        self._ex = executive
        self._rc = run_controller
        self._requests: Dict[str, ActivationRequest] = {}

    # -- intake -----------------------------------------------------------
    def receive(self, request_dict: Dict[str, Any]) -> str:
        """Validate and index a Primary-issued request (state REQUESTED).

        Raises RequestMalformed (R1) or DuplicateActiveRequest (R2).
        """
        req = ActivationRequest.from_primary_dict(request_dict)  # R1
        if req.request_id in self._requests:
            raise DuplicateActiveRequest(
                f"{R_DUPLICATE_ACTIVE_REQUEST}: request_id "
                f"{req.request_id!r} is already indexed: re-issue under a "
                f"new id")
        self._requests[req.request_id] = req
        return req.request_id

    def get_request(self, request_id: str) -> Dict[str, Any]:
        """The record as the Primary side reads it back (return channel)."""
        req = self._requests.get(request_id)
        if req is None:
            raise TakeUpError(
                f"unknown activation request {request_id!r}: nothing to read")
        return req.view()

    # -- take-up decision (design §2) ------------------------------------
    def decide(self, request_id: str) -> Dict[str, Any]:
        """Run the take-up procedure; write exactly one decision."""
        req = self._requests.get(request_id)
        if req is None:
            raise TakeUpError(
                f"unknown activation request {request_id!r}: cannot decide")
        if req.state != ST_REQUESTED:
            raise TakeUpError(
                f"request {request_id!r} is already {req.state}: the "
                f"take-up decision is written exactly once")

        # Fit-hint candidate class (advisory; the executive judges fit
        # authoritatively). Unknown/absent hint -> REFUSED_FIT here.
        hints = (req.bounded_requirement.get("fit_hints") or {})
        candidate = (hints.get("boundary_class") or "").strip()
        if candidate not in _KNOWN_CLASSES:
            return self._refuse(
                req, R_REFUSED_FIT,
                f"no fittable boundary class: fit_hints.boundary_class "
                f"{candidate!r} is not a known curiosity boundary class",
                basis={"fit": candidate or "(no hint)"})

        trigger = CuriosityTrigger(
            trigger_id=f"act-{req.request_id[:16]}",
            boundary_class=candidate,
            question_text=req.knowledge_gap,
            bounded_objective=self._bounded_objective(req),
            origin="PRIMARY_REQUESTED",
        ).validate()
        try:
            decision = self._ex.request_activation(trigger)
        except ActivationRefused as exc:
            return self._map_refusal(req, str(exc))

        # ACCEPTED: fund and start a real inquiry through the run controller.
        inquiry_id = self._rc.dispatch(decision)
        req.inquiry_id = inquiry_id
        req.decision_basis = {
            "kill_state": decision.enforcement_state,
            "grant_available": {
                "budget_s": round(decision.grant.budget_s, 6),
                "max_concurrent": decision.grant.max_concurrent,
                "epoch_id": decision.epoch_id,
            },
            "fit": decision.loop,
        }
        req.decision = {
            "verdict": "ACCEPTED",
            "reason": (f"take-up accepted: loop={decision.loop} "
                       f"inquiry={inquiry_id}"),
            "decided_at": time.time(),
        }
        req.state = ST_ACCEPTED
        return req.view()

    def consider(self, request_dict: Dict[str, Any]) -> Dict[str, Any]:
        """Atomic convenience: receive + decide."""
        return self.decide(self.receive(request_dict))

    # -- withdrawal (A28) --------------------------------------------------
    def withdraw(self, request_id: str, reason: str) -> Dict[str, Any]:
        """Primary withdraws its request (its own act, always honored)."""
        req = self._requests.get(request_id)
        if req is None:
            raise TakeUpError(
                f"unknown activation request {request_id!r}: withdrawal "
                f"refused, nothing withdrawn")
        now = time.time()
        if req.state == ST_REQUESTED:
            # Before take-up: closed; no curiosity resources were committed.
            req.state = ST_WITHDRAWN
            req.withdrawn_at = now
            req.withdrawn_reason = reason
            return req.view()
        if req.state == ST_ACCEPTED:
            req.withdrawn_at = now
            req.withdrawn_reason = reason
            if req.inquiry_id is not None:
                # Ordinary stop (A13/A18), never a kill: the run
                # controller's stop path preserves partial evidence as
                # INCONCLUSIVE with the cause. Already-terminal inquiries
                # no-op honestly inside stop_inquiry.
                self._rc.stop_inquiry(req.inquiry_id,
                                      cause=f"{R_WITHDRAWN}: {reason}")
            req.state = ST_WITHDRAWN
            return req.view()
        # REFUSED / WITHDRAWN: already terminal; named no-op.
        # ST_KILLED: the kill termination stands — withdrawal after a kill
        # must not rewrite kill history (CUR-P4B race ordering).
        if req.state == ST_KILLED:
            return dict(req.view(), _withdrawal_note=(
                f"request already KILLED "
                f"({req.termination['reason'] if req.termination else '?'}): "
                f"withdrawal is a no-op; the kill termination stands"))
        return dict(req.view(), _withdrawal_note=(
            f"request already {req.state}: withdrawal is a no-op"))

    # -- kill termination (C-9 R7; CUR-P4B) ----------------------------------
    def record_termination(self, request_id: str,
                           kill_result: Dict[str, Any], *,
                           level: str, issuer: str) -> Dict[str, Any]:
        """Write the named KILLED termination after a REAL kill.

        Thin delegate to termination.record_kill_termination (fail-closed:
        real kill cross-checked against the run controller's live record).
        """
        from .termination import record_kill_termination
        return record_kill_termination(self, request_id, kill_result,
                                       level=level, issuer=issuer)

    # -- internals ----------------------------------------------------------
    def _bounded_objective(self, req: ActivationRequest) -> str:
        ref = req.operational_objective_ref
        scope = (req.bounded_requirement.get("scope") or "").strip()
        return (f"{ref['objective_id']}: {ref['statement']}"
                + (f" [scope: {scope}]" if scope else ""))

    def _refuse(self, req: ActivationRequest, name: str, reason: str,
                basis: Dict[str, Any]) -> Dict[str, Any]:
        req.decision_basis = {"kill_state": None, "grant_available": None,
                              "fit": None}
        req.decision_basis.update(basis)
        req.decision = {"verdict": name, "reason": reason,
                        "decided_at": time.time()}
        req.state = ST_REFUSED
        return req.view()

    def _map_refusal(self, req: ActivationRequest,
                     reason: str) -> Dict[str, Any]:
        prefix = _reason_prefix(reason)
        name = _REFUSAL_MAP.get(prefix, R_REFUSED_FIT)
        basis: Dict[str, Any] = {}
        if name == R_CURIOSITY_DISABLED:
            m = re.search(r"enforcement state is ([A-Za-z_]+)", reason)
            basis["kill_state"] = m.group(1) if m else reason
        elif name == R_REFUSED_GRANT:
            basis["grant_available"] = reason
        else:
            basis["fit"] = reason
        return self._refuse(req, name, f"{name}: {reason}", basis)
