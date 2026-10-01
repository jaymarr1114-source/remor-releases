"""Level-3 combiner: severe-violation fact + failed roll call + temporal overlap.

Level 3 requires BOTH a condition classified as a severe violation AND failure
to satisfy the required roll-call condition while that severe violation
remains active. A missed roll call by itself is NOT a violation (invariant 6);
a severe violation with a MET roll call is not Level 3 either. A stale
roll-call fact -- one issued outside the violation's active window -- cannot
combine.

Import-guarded like the engine: unreachable from the curiosity domain.

Status (DEAD-CODE-1, 2026-10-01): ``evaluate_l3`` is the D-3 severe-violation
combiner James decided (2026-09-29;
architecture/exec2-enforcement-three-level-kill-policy_2026-09-29.md). It is
NOT wired into the live enforcement path — the L3 branch exists on paper
only. It is exercised by proofs/cur_p1b_enforcement_proof.py (crossed with
CUR-P1B). Do not silently delete: wiring it into the live path or removing
it is James's decision.
"""

from __future__ import annotations

import time
from typing import Callable, Dict, Optional

from . import _guard as _domain_guard

_domain_guard.ensure_external_caller()

from .states import (  # noqa: E402
    DOMAIN,
    ISSUER_ENFORCEMENT,
    ISSUER_SAFETY_AUTHORITY,
    EnforcementState,
)
from ._engine import EnforcementEngine, EnforcementError  # noqa: E402


class L3Refused(EnforcementError):
    """The L3 combination preconditions are not met."""


# Classifications that count as "failed roll call" for Level 3 evaluation.
# INVALID is forensically distinct from MISSED but both are failures.
FAILED_CLASSIFICATIONS = frozenset({"MISSED", "INVALID"})


def evaluate_l3(
    engine: EnforcementEngine,
    domain: str,
    violation_fact: Optional[Dict],
    attestation: Optional[Dict],
    issuer: str = ISSUER_ENFORCEMENT,
    clock: Callable[[], float] = time.time,
):
    """Combine an independently established severe-violation fact with the GAM
    roll-call fact. On success performs RUNNING|WARNING_1|SUSPENDED_SAFETY ->
    BANNED_6M via the engine; on any missing precondition raises L3Refused.

    violation_fact: {severity, established_by, active_from, active_until|None,
                     evidence_refs}
    attestation: GAM record {check_id, domain, issued_at, responded_at,
                 classification, nonce}
    """
    now = clock()

    if violation_fact is None:
        raise L3Refused("no severe-violation fact established")
    if violation_fact.get("severity") != "severe":
        raise L3Refused("violation is not classified severe")
    if violation_fact.get("established_by") != ISSUER_SAFETY_AUTHORITY:
        raise L3Refused(
            "severe violation was not independently established by the "
            "safety authority"
        )
    if attestation is None:
        raise L3Refused("no roll-call attestation fact")
    if attestation.get("domain") != domain:
        raise L3Refused("attestation is for a different domain")
    classification = attestation.get("classification")
    if classification not in FAILED_CLASSIFICATIONS:
        raise L3Refused(
            f"roll call did not fail (classification={classification!r})"
        )

    active_from = violation_fact.get("active_from")
    active_until = violation_fact.get("active_until")  # None => still active
    issued_at = attestation.get("issued_at")
    if active_from is None or issued_at is None:
        raise L3Refused("missing timestamps for overlap check")
    if not (active_from <= issued_at <= (active_until if active_until is not None else now)):
        raise L3Refused(
            "stale roll-call fact: no temporal overlap with the active "
            "violation window"
        )

    return engine.transition(
        domain,
        EnforcementState.BANNED_6M,
        issuer,
        reason_refs={
            "violation_ref": violation_fact.get("violation_id", "unknown"),
            "evidence_refs": violation_fact.get("evidence_refs", []),
            "rollcall_check_id": attestation.get("check_id"),
            "rollcall_classification": classification,
        },
        preserved_refs={
            "violation_evidence": violation_fact.get("evidence_refs", []),
            "rollcall_records": [attestation.get("check_id")],
        },
    )
