"""Boundary presentations: what the executive is allowed to route.

A boundary is never a bare string claim ("something is broken"). It is a
BoundaryPresentation: a kind plus the STRUCTURED EVIDENCE the observing
machinery emitted, validated by this module before the executive routes it.

Validation is the anti-fabrication gate (the M7 pattern): each kind names
the real record type its evidence must carry, checked with isinstance
against the producing machinery's own classes. A fabricated dict in place
of a real GapRecord is refused here, not routed.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict

#: The six boundary classes, one per loop. The ownership map lives in
#: executive.py (BOUNDARY_OWNERSHIP); the kinds live here because
#: validation is per-kind.
BOUNDARY_RUN_WAKE = "run_wake"
BOUNDARY_ACQUISITION_GAP = "acquisition_gap"
BOUNDARY_EXECUTION_FAILURE = "execution_failure"
BOUNDARY_COMPLETION_CANDIDATE = "completion_candidate"
BOUNDARY_TECHNIQUE_DELTA = "technique_delta"
BOUNDARY_NOVEL_TASK = "novel_task"

BOUNDARY_KINDS = frozenset({
    BOUNDARY_RUN_WAKE,
    BOUNDARY_ACQUISITION_GAP,
    BOUNDARY_EXECUTION_FAILURE,
    BOUNDARY_COMPLETION_CANDIDATE,
    BOUNDARY_TECHNIQUE_DELTA,
    BOUNDARY_NOVEL_TASK,
})

_RUN_WAKE_REASONS = frozenset({"cadence_tick", "user_gap", "resume_request"})


class BoundaryRefused(Exception):
    """The presentation failed validation: not routable, fail closed."""


@dataclass
class BoundaryPresentation:
    """A boundary offered to the executive, with its evidence attached.

    kind: one of BOUNDARY_KINDS.
    evidence: the structured record the observing machinery emitted.
        Per-kind contract (validated by validate()):
          run_wake: {"wake_reason": cadence_tick|user_gap|resume_request,
                     "run_id": str} -- observed by the RunController, which
                     owns cadence (V10-P2).
          acquisition_gap: {"gap_record": GapRecord} -- a REAL M7 record,
              status open/acquiring, not a dict shaped like one.
          execution_failure: {"capability_id": str} -- the quarantine
              record itself is read by the inlet through M5's frozen
              reason API; the executive only checks the shape here.
          completion_candidate: {"run_id": str, "goal": str,
              "attempt": Attempt, "auth": AuthReport(passed=True)} --
              Q8's present() gate: a failed attempt is not presentable.
          technique_delta: {"delta": DeltaRecord} -- a REAL M2 record.
              Causal discipline (validate()) is the loop's business;
              the executive checks the record is real.
          novel_task: {"distilled_ref": {"promoted_name": str, ...},
              "novel_goal": str, "novel_examples": [(dict, output), ...]}
              -- non-empty examples; the loop's vacuity guard does the
              rest.
    observed_by: which machinery emitted the boundary (provenance).
    """

    kind: str
    evidence: Dict[str, Any]
    observed_by: str
    observed_at: float = field(default_factory=time.time)
    boundary_id: str = field(
        default_factory=lambda: f"bnd_{uuid.uuid4().hex[:12]}")

    def validate(self) -> "BoundaryPresentation":
        """Validate kind + evidence shape. Raises BoundaryRefused."""
        if self.kind not in BOUNDARY_KINDS:
            raise BoundaryRefused(
                f"unknown boundary kind {self.kind!r}: no loop owns it "
                f"(known: {sorted(BOUNDARY_KINDS)})")
        if not isinstance(self.evidence, dict):
            raise BoundaryRefused("evidence must be a dict of named fields")
        if not (self.observed_by or "").strip():
            raise BoundaryRefused(
                "observed_by is required: a boundary with no observing "
                "machinery is an ungrounded claim")
        validator = _VALIDATORS[self.kind]
        validator(self.evidence)
        return self


def _v_run_wake(ev: Dict[str, Any]) -> None:
    reason = ev.get("wake_reason")
    if reason not in _RUN_WAKE_REASONS:
        raise BoundaryRefused(
            f"run_wake needs wake_reason in {sorted(_RUN_WAKE_REASONS)}, "
            f"got {reason!r}")
    if not ev.get("run_id"):
        raise BoundaryRefused("run_wake needs run_id: whose wake is this")


def _v_acquisition_gap(ev: Dict[str, Any]) -> None:
    from swarm_engine.acquisition.gaps import GapRecord
    rec = ev.get("gap_record")
    if not isinstance(rec, GapRecord):
        raise BoundaryRefused(
            "acquisition_gap evidence must carry a real M7 GapRecord "
            f"(got {type(rec).__name__}): fabricated gap claims are not "
            "routable")
    if rec.status not in ("open", "acquiring"):
        raise BoundaryRefused(
            f"gap {rec.gap_id} has status {rec.status!r}: only open gaps "
            "are boundaries; closed gaps are history")


def _v_execution_failure(ev: Dict[str, Any]) -> None:
    cid = ev.get("capability_id")
    if not isinstance(cid, str) or not cid.strip():
        raise BoundaryRefused(
            "execution_failure needs a capability_id string: the inlet "
            "reads the real quarantine record through M5's frozen API")


def _v_completion_candidate(ev: Dict[str, Any]) -> None:
    from swarm_engine.services.acceptance import Attempt, AuthReport
    attempt = ev.get("attempt")
    auth = ev.get("auth")
    if not isinstance(attempt, Attempt):
        raise BoundaryRefused(
            "completion_candidate evidence must carry a real Attempt "
            f"(got {type(attempt).__name__})")
    if not isinstance(auth, AuthReport):
        raise BoundaryRefused(
            "completion_candidate evidence must carry a real AuthReport "
            f"(got {type(auth).__name__})")
    if not auth.passed:
        raise BoundaryRefused(
            "completion_candidate with auth.passed=False: a failed attempt "
            "is not presentable as completed (Q8's gate, enforced here too)")
    if not ev.get("run_id") or not ev.get("goal"):
        raise BoundaryRefused(
            "completion_candidate needs run_id and goal")


def _v_technique_delta(ev: Dict[str, Any]) -> None:
    from swarm_engine.acquisition.delta import DeltaRecord
    delta = ev.get("delta")
    if not isinstance(delta, DeltaRecord):
        raise BoundaryRefused(
            "technique_delta evidence must carry a real M2 DeltaRecord "
            f"(got {type(delta).__name__}): the loop's causal discipline "
            "applies to real records, not claims")


def _v_novel_task(ev: Dict[str, Any]) -> None:
    ref = ev.get("distilled_ref")
    if not isinstance(ref, dict) or not ref.get("promoted_name"):
        raise BoundaryRefused(
            "novel_task needs distilled_ref naming the source technique "
            "(promoted_name)")
    ex = ev.get("novel_examples")
    if not isinstance(ex, list) or not ex:
        raise BoundaryRefused(
            "novel_task needs a non-empty novel_examples list: "
            "generalization without novel evidence is vacuous")
    for i, pair in enumerate(ex):
        if (not isinstance(pair, (list, tuple)) or len(pair) != 2
                or not isinstance(pair[0], dict)):
            raise BoundaryRefused(
                f"novel_examples[{i}] must be (input_dict, output)")


_VALIDATORS = {
    BOUNDARY_RUN_WAKE: _v_run_wake,
    BOUNDARY_ACQUISITION_GAP: _v_acquisition_gap,
    BOUNDARY_EXECUTION_FAILURE: _v_execution_failure,
    BOUNDARY_COMPLETION_CANDIDATE: _v_completion_candidate,
    BOUNDARY_TECHNIQUE_DELTA: _v_technique_delta,
    BOUNDARY_NOVEL_TASK: _v_novel_task,
}
