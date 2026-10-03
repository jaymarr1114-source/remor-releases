"""The critique machinery (creativity paper §6).

Three judgments, three judges — kept separate on purpose:

- **Novelty** — judged by mechanism: the provenance-record check
  (``check_novelty``). Not-derivable-from-existing-records is computable;
  no taste required.
- **Value** — judged by the critique step (``critique_value``) against the
  commissioned intent: did it make what was asked, well? The initial
  criteria set is explicit, documented, and constrained (VALUE_CRITERIA_V1,
  the Q7 reviewable artifact) — no taste constants invented. It is
  fallible by design, which is why the next judgment also runs.
- **Honesty, correctness, safety, provenance** — judged by the EXISTING
  Acceptance panel (``runtime.core.acceptance_panels.build_panels()``),
  reused via ``submit_to_panels`` — never replicated (D-7's separation:
  acceptance, kill/execution-control, and attestation stay distinct).

Legality comes first and is structural: ``critique_candidate`` accepts
only a ledger-LEGAL ``CompositionVerdict`` covering exactly this
candidate. An ILLEGAL composition is refused before any novelty, value,
or panel work — not merely skipped by convention.

No-duplication audit (fresh re-map 2026-10-03, canonical @3ae9e8f):
- Composition records are the existing ``ProvenanceRecord.primitives_used``
  entries in ``runtime/governance/provenance.py`` — read, never rebuilt.
- ``runtime/acquisition/delta.py`` is the distillation experience record
  (a different domain); the novelty check does not touch it.
- ``pylib/swarm_engine`` is a symlink to ``runtime`` — one codebase, two
  import roots; imports here use the ``runtime.*`` root like the rest of
  ``runtime/creativity/``.
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

from runtime.core.acceptance_panels import (
    PanelContext,
    PanelVerdict,
    build_panels,
)
from runtime.creativity.intent import CreativeIntent
from runtime.creativity.ledger import CompositionVerdict, LedgerRefused
from runtime.governance.provenance import ProvenanceStore, TrustLevel


class CritiqueRefused(Exception):
    """The critique step refused a candidate: exact reason, never silent."""


# ---------------------------------------------------------------------------
# Candidate
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class CandidateComposition:
    """A candidate creative composition at the critique stage.

    primitive_ids: the ordered arrangement of verified primitives.
    plan: the composition in the real composer plan format
        ({"steps": [{"id","op","args"}], "output": ..., "params": ...}).
    args: plan arguments for the honesty re-execution.
    claimed_outcome: what the candidate claims the plan yields.
    held_out: [{"args":..., "expected":...}] executed by the correctness
        panel through the real composer.
    file_artifacts: cited file artifacts for the provenance panel.
    addresses: which part of the intent's outcome this candidate serves.
    constraints: structured constraints from the commission, each
        {"kind": ...} — evaluated mechanically, never parsed from prose.
    run_id: the critique-run identifier carried into panel evidence.
    """
    candidate_id: str
    primitive_ids: Tuple[str, ...]
    plan: Dict[str, Any]
    args: Dict[str, Any]
    claimed_outcome: Any
    held_out: Tuple[Dict[str, Any], ...] = ()
    file_artifacts: Tuple[str, ...] = ()
    addresses: str = ""
    constraints: Tuple[Dict[str, Any], ...] = ()
    run_id: str = ""


# ---------------------------------------------------------------------------
# Novelty — judged by mechanism
# ---------------------------------------------------------------------------

NOVEL = "novel"
RETRIEVAL = "retrieval"


def canonical_signature(primitive_ids: Sequence[str]) -> str:
    """Order-free signature over primitive identities.

    Limits (documented, not hidden):
    - Permutations of one set canonicalize identically — this is the
      paper's "trivially permuted" scope.
    - Duplicate uses collapse (set semantics): using "add" twice vs once
      is the same signature.
    - Wiring/arrangement beyond the SET is not captured: the same
      primitives wired into a genuinely different graph still match.
      Arrangement-sensitive novelty is a known BOUND of this check —
      classified, not hidden.
    - Identity is the registry name string.
    """
    normalized = sorted(set(primitive_ids))
    blob = json.dumps(normalized, sort_keys=True, separators=(",", ":"))
    return "sig_" + hashlib.sha256(blob.encode("utf-8")).hexdigest()[:24]


@dataclass(frozen=True)
class NoveltyVerdict:
    """The mechanism's novelty judgment.

    verdict: "novel" | "retrieval".
    signature: the candidate's canonical signature.
    matched_capability_id / matched_trust: the existing record on RETRIEVAL.
    prior_unverified: matching combinations whose verification is NOT
        intact (below TRUSTED or quarantined) — context, not retrieval.
    reason: the exact basis, never a bare label.
    """
    verdict: str
    signature: str
    matched_capability_id: Optional[str]
    matched_trust: Optional[str]
    prior_unverified: Tuple[Tuple[str, str], ...]
    reason: str


def check_novelty(candidate: CandidateComposition,
                  provenance_store: ProvenanceStore) -> NoveltyVerdict:
    """Is this composition derivable from existing records?

    RETRIEVAL iff a provenance record's ``primitives_used`` canonicalizes
    to the same signature AND its verification is intact (trust TRUSTED+,
    never quarantined). A matching combination without intact verification
    is recorded in ``prior_unverified`` — it is context, not a retrieval,
    because novelty is judged against the VERIFIED record (paper §4).
    """
    if provenance_store is None:
        raise CritiqueRefused(
            "novelty check needs a provenance store; got None")
    sig = canonical_signature(candidate.primitive_ids)
    prior_unverified: List[Tuple[str, str]] = []
    # QUARANTINED is the trust floor: this enumerates every record.
    for record in provenance_store.list_by_trust(TrustLevel.QUARANTINED):
        if not record.primitives_used:
            continue
        if canonical_signature(record.primitives_used) != sig:
            continue
        if record.trust >= TrustLevel.TRUSTED:
            return NoveltyVerdict(
                verdict=RETRIEVAL,
                signature=sig,
                matched_capability_id=record.capability_id,
                matched_trust=record.trust.name,
                prior_unverified=tuple(prior_unverified),
                reason=(f"retrieval: provenance record "
                        f"{record.capability_id!r} already holds this "
                        f"combination (trust {record.trust.name}, "
                        f"verification intact)"))
        prior_unverified.append((record.capability_id, record.trust.name))
    return NoveltyVerdict(
        verdict=NOVEL,
        signature=sig,
        matched_capability_id=None,
        matched_trust=None,
        prior_unverified=tuple(prior_unverified),
        reason=("novel: no provenance record holds this combination "
                "with verification intact"))


# ---------------------------------------------------------------------------
# Value — judged by critique against the commissioned intent
# ---------------------------------------------------------------------------

# Q7 initial constrained criteria set (v1, 2026-10-03). Explicit,
# documented, reviewable by James; expands only on auditable evidence of
# demonstrated judgment. No taste constants: every criterion is a
# mechanical check returning (passed, reason). What v1 deliberately does
# NOT judge: semantic "wellness" beyond the checks below — e.g. whether
# the outcome is elegant, surprising, or stylish. That latitude is earned,
# not assumed.
VALUE_CRITERIA_V1: Dict[str, str] = {
    "executes": (
        "the plan executes through the real composer without error"),
    "outcome_reproduces": (
        "the observed outcome equals the claimed outcome — the candidate "
        "makes what it claims to make"),
    "intent_addressed": (
        "the candidate names the intent outcome it serves (structural); "
        "the semantic judgment of 'well' is out of scope for v1"),
    "constraints_satisfied": (
        "every structured constraint from the commission evaluates true, "
        "each with its reason"),
}


@dataclass(frozen=True)
class CriterionResult:
    criterion: str
    passed: bool
    reason: str


@dataclass(frozen=True)
class ValueScore:
    """The critique's value judgment. satisfied is True only if every
    criterion passed. criteria carries each (passed, reason) — never bare
    numbers."""
    satisfied: bool
    criteria: Tuple[CriterionResult, ...]
    criteria_version: str = "v1"


def _eval_constraint(constraint: Dict[str, Any],
                     candidate: CandidateComposition,
                     primitives: Any,
                     observed: Any) -> Tuple[bool, str]:
    """Evaluate one structured constraint mechanically. Unknown kinds are
    refused fail-closed — an uninterpretable constraint never silently
    passes."""
    kind = constraint.get("kind")
    if kind == "requires_primitive":
        name = constraint.get("primitive")
        ok = name in candidate.primitive_ids
        return ok, (f"requires_primitive {name!r}: "
                    f"{'present' if ok else 'ABSENT from composition'}")
    if kind == "excludes_family":
        family = constraint.get("family")
        if primitives is None:
            return False, (f"excludes_family {family!r}: no primitive "
                           "registry available — refused fail-closed")
        bad = []
        for pid in candidate.primitive_ids:
            prim = primitives.get(pid)
            if prim is not None and prim.family == family:
                bad.append(pid)
        ok = not bad
        return ok, (f"excludes_family {family!r}: "
                    f"{'violated by ' + ', '.join(bad) if bad else 'no primitive from that family'}")
    if kind == "max_steps":
        try:
            n = int(constraint.get("n"))
        except (TypeError, ValueError):
            return False, f"max_steps: unparsable bound {constraint.get('n')!r}"
        steps = candidate.plan.get("steps") or []
        ok = len(steps) <= n
        return ok, (f"max_steps {n}: plan has {len(steps)} step(s) — "
                    f"{'within bound' if ok else 'EXCEEDS bound'}")
    if kind == "exact_outcome":
        expected = constraint.get("value")
        ok = observed == expected
        return ok, (f"exact_outcome: observed {observed!r} vs required "
                    f"{expected!r} — {'match' if ok else 'MISMATCH'}")
    return False, (f"unknown constraint kind {kind!r}: refused fail-closed — "
                   "an uninterpretable constraint never silently passes")


def critique_value(candidate: CandidateComposition,
                   intent: CreativeIntent,
                   composer: Any,
                   primitives: Any = None) -> ValueScore:
    """Score the candidate against the commissioned intent under
    VALUE_CRITERIA_V1. Every criterion carries its reason.

    ``primitives`` (the real registry) is optional: only the
    ``excludes_family`` constraint needs it. Without a registry that
    constraint kind fails closed with the reason stated.
    """
    if not isinstance(intent, CreativeIntent):
        raise CritiqueRefused(
            f"value critique needs a CreativeIntent, got {type(intent).__name__}")
    if composer is None:
        raise CritiqueRefused("value critique needs a composer; got None")

    results: List[CriterionResult] = []

    # executes — through the REAL composer, never asserted.
    try:
        exec_result = composer.execute_sync(candidate.plan, candidate.args)
    except Exception as e:  # surfaced, never swallowed
        exec_result = {"success": False,
                       "error": f"{type(e).__name__}: {e}"}
    executes_ok = bool(exec_result.get("success"))
    observed = exec_result.get("value")
    results.append(CriterionResult(
        "executes", executes_ok,
        ("plan executed through the real composer"
         if executes_ok else
         f"plan FAILED to execute: {exec_result.get('error')}")))

    # outcome_reproduces
    reproduces_ok = executes_ok and observed == candidate.claimed_outcome
    results.append(CriterionResult(
        "outcome_reproduces", reproduces_ok,
        (f"observed {observed!r} == claimed {candidate.claimed_outcome!r}"
         if reproduces_ok else
         f"observed {observed!r} != claimed {candidate.claimed_outcome!r} — "
         f"the candidate does not make what it claims")))

    # intent_addressed (structural for v1)
    addressed_ok = bool(candidate.addresses and candidate.addresses.strip()) \
        and bool(intent.outcome and intent.outcome.strip())
    results.append(CriterionResult(
        "intent_addressed", addressed_ok,
        (f"candidate addresses {candidate.addresses!r} against intent "
         f"outcome {intent.outcome!r}"
         if addressed_ok else
         "candidate does not name the intent outcome it serves, or the "
         "intent states no outcome")))

    # constraints_satisfied — each evaluated, each reasoned.
    constraint_results: List[Tuple[bool, str]] = []
    for constraint in candidate.constraints:
        if not isinstance(constraint, dict):
            constraint_results.append(
                (False, f"malformed constraint {constraint!r}: refused"))
            continue
        constraint_results.append(
            _eval_constraint(constraint, candidate, primitives, observed))
    constraints_ok = all(ok for ok, _ in constraint_results)
    if constraint_results:
        detail = "; ".join(reason for _, reason in constraint_results)
    else:
        detail = "no constraints stated"
    results.append(CriterionResult(
        "constraints_satisfied", constraints_ok,
        detail if constraints_ok or not constraint_results
        else f"constraint(s) FAILED: {detail}"))

    return ValueScore(
        satisfied=all(r.passed for r in results),
        criteria=tuple(results))


# ---------------------------------------------------------------------------
# Panel submission — the EXISTING Acceptance panel, reused
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class _PanelAttempt:
    """Wiring, not a stub: the shape the panels read
    (plan/args/result_summary/approach_signature)."""
    plan: Dict[str, Any]
    args: Dict[str, Any]
    result_summary: Any
    approach_signature: Tuple[str, ...]


class _PanelEngine:
    """Wiring, not a stub: holds the REAL composer and the REAL primitive
    registry. Every panel call delegates to real machinery — the honesty
    panel re-executes through this composer, the safety panel resolves
    through this registry."""

    def __init__(self, composer: Any, primitives: Any) -> None:
        if composer is None or primitives is None:
            raise CritiqueRefused(
                "panel submission needs a real composer and a real "
                "primitive registry; got "
                f"composer={composer!r} primitives={primitives!r}")
        self.composer = composer
        self.primitives = primitives


def submit_to_panels(candidate: CandidateComposition,
                     composer: Any,
                     primitives: Any,
                     repo_root: str) -> Tuple[PanelVerdict, ...]:
    """Run the EXISTING Acceptance panel against the candidate's artifact
    case and return the real verdicts, in panel order.

    The critique module calls the panel; it reimplements nothing. A panel
    failure here is the panel's verdict, attached — never converted into a
    novelty or value judgment."""
    attempt = _PanelAttempt(
        plan=candidate.plan,
        args=candidate.args,
        result_summary=candidate.claimed_outcome,
        approach_signature=tuple(candidate.primitive_ids),
    )
    result = {
        "held_out": [
            {"args": dict(h.get("args") or {}), "expected": h.get("expected")}
            for h in candidate.held_out
        ],
        "file_artifacts": list(candidate.file_artifacts),
        "run_id": candidate.run_id,
    }
    ctx = PanelContext(
        result=result,
        attempt=attempt,
        engine=_PanelEngine(composer, primitives),
        repo_root=repo_root,
        run_started_at=time.time(),
    )
    return tuple(panel.verify(ctx) for panel in build_panels())


# ---------------------------------------------------------------------------
# Entry point — legality gate first, then the three judgments
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class CritiqueReport:
    """The three kept-separate judgments on one candidate.

    legality_verdict: the ledger verdict that admitted this candidate
        (legal=True, covering exactly these primitives).
    novelty: the mechanism's judgment (novel vs retrieval).
    value: the critique's judgment against the commissioned intent.
    panels: the Acceptance panel's verdicts, attached in panel order.
    None absorbs another: a panel failure never rewrites the novelty or
    value judgments — all three are carried side by side.
    """
    candidate_id: str
    intent_outcome: str
    legality_verdict: CompositionVerdict
    novelty: NoveltyVerdict
    value: ValueScore
    panels: Tuple[PanelVerdict, ...]


def critique_candidate(*, verdict: CompositionVerdict,
                       intent: CreativeIntent,
                       candidate: CandidateComposition,
                       provenance_store: ProvenanceStore,
                       composer: Any,
                       primitives: Any,
                       repo_root: str) -> CritiqueReport:
    """Run the critique step on one candidate composition.

    The legality gate is structural: a non-LEGAL verdict — or a LEGAL
    verdict that does not cover exactly this candidate's primitives — is
    refused BEFORE any novelty, value, or panel work. The refusal is
    raised, not returned, so no downstream stage can observe a refused
    candidate by convention alone.
    """
    if not isinstance(verdict, CompositionVerdict):
        raise CritiqueRefused(
            f"critique needs a ledger CompositionVerdict, got "
            f"{type(verdict).__name__} — the legality gate cannot be skipped")
    if not verdict.legal:
        failing = "; ".join(
            f"{item.get('primitive_id')}: {item.get('reason')}"
            for item in verdict.illegal)
        raise CritiqueRefused(
            f"critique refused: composition verdict is ILLEGAL — "
            f"{failing or 'no reason recorded'}")
    if set(candidate.primitive_ids) != set(verdict.checked):
        raise CritiqueRefused(
            f"critique refused: the verdict does not cover this candidate — "
            f"verdict checked {sorted(verdict.checked)}, candidate uses "
            f"{sorted(set(candidate.primitive_ids))}")
    if not isinstance(intent, CreativeIntent):
        raise CritiqueRefused(
            f"critique needs a CreativeIntent, got {type(intent).__name__}")

    novelty = check_novelty(candidate, provenance_store)
    value = critique_value(candidate, intent, composer, primitives)
    panels = submit_to_panels(candidate, composer, primitives, repo_root)
    return CritiqueReport(
        candidate_id=candidate.candidate_id,
        intent_outcome=intent.outcome,
        legality_verdict=verdict,
        novelty=novelty,
        value=value,
        panels=panels,
    )
