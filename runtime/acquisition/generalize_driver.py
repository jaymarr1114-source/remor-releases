"""V10-P5: generalization driver (Test 2 of the technique-distillation charter).

The genuinely missing wiring: nothing in production called
``DistillationLoop.generalize()`` -- the bound-constant re-parameterization
machinery (M2, hardened by Q6: vacuity guard, coupled substitution, held-out
causal split, negative controls, ReviewBoard + verdict-bound promotion on the
new bytes) existed but had no driver connecting a distilled technique to a
novel task.

This driver is ONLY wiring. It reuses, never rebuilds:
- ``DistillationLoop.generalize`` (M2/Q6) -- the generalization mechanism,
  called, never edited;
- V10-P4's consumption records (kind ``distillation_consumption``) -- the
  registry of distilled techniques, read, never rewritten;
- V10-P1's ``record_experience`` / ``read_experiences`` -- the single
  read/write path for the generalization records this driver emits.

A generalization attempt is recorded whether it succeeds or fails: the
record carries the source technique, the novel task, and the outcome
(promoted name + held-out score, or the named refusal reason). Refusals
are first-class records -- a novel task outside the envelope is evidence
about where the envelope ends, not a silent skip.
"""

from typing import Any, Dict, List, Optional, Tuple

GENERALIZATION_KIND = "technique_generalization"


def find_distilled_techniques(epistemic: Any,
                              limit: int = 1000) -> List[Dict[str, Any]]:
    """Return refs to techniques the V10-P4 sweep distilled.

    Each ref carries what ``DistillationLoop.generalize`` needs to recover
    the source: ``promoted_name``, ``capability_id``, the consumption
    record's observation id (provenance), and the source delta's id.
    Only ``status == "distilled"`` outcomes are returned -- refused or
    errored deltas never produced a technique.
    """
    from swarm_engine.intellect.unified_memory import read_experiences
    from swarm_engine.acquisition.distill_driver import CONSUMPTION_KIND
    refs: List[Dict[str, Any]] = []
    for rec in read_experiences(epistemic, kind=CONSUMPTION_KIND,
                                limit=limit):
        raw = rec.get("raw") or {}
        outcome = raw.get("outcome") or {}
        if outcome.get("status") != "distilled":
            continue
        if not outcome.get("promoted_name") or not outcome.get("capability_id"):
            continue
        refs.append({
            "consumption_id": rec.get("observation_id"),
            "delta_observation_id": raw.get("delta_observation_id"),
            "promoted_name": outcome["promoted_name"],
            "capability_id": outcome["capability_id"],
            "heldout": outcome.get("heldout", ""),
        })
    return refs


def _source_stub(ref: Dict[str, Any]) -> Any:
    """Rebuild the minimal DistillationResult the generalize() call needs.

    generalize() reads ``source.promoted_name`` (naming), ``source.delta_id``
    (naming/provenance) and ``source.capability_id`` (to recover the retained
    plan via the engine's capability record). Nothing else is consulted, so
    the stub carries exactly those three -- no duplicated result state.
    """
    from swarm_engine.acquisition.distill import DistillationResult
    return DistillationResult(
        delta_id=(ref.get("delta_observation_id")
                  or f"distilled:{ref['promoted_name']}"),
        success=True,
        route="distilled",
        promoted_name=ref["promoted_name"],
        capability_id=ref["capability_id"],
    )


def generalize_for_task(engine: Any,
                        epistemic: Any,
                        distilled_ref: Dict[str, Any],
                        novel_goal: str,
                        novel_examples: List[Tuple[Dict[str, Any], Any]],
                        ) -> Any:
    """Generalize a distilled technique to a novel task. One call, one task.

    Runs the EXISTING ``DistillationLoop.generalize`` (bound-constant
    re-parameterization with Q6's vacuity guard, held-out causal split,
    negative controls, and the full ReviewBoard + verdict-bound promotion
    path on the new bytes), then records the attempt through V10-P1's
    ``record_experience`` with provenance: the causal chain names the
    consumption record the source technique came from.

    Returns the ``DistillationResult`` (success or named refusal -- the
    caller inspects ``result.success`` / ``result.reason``; refusals are
    recorded, never raised, so one out-of-envelope task cannot break a
    batch).
    """
    from swarm_engine.acquisition.distill import DistillationLoop
    from swarm_engine.intellect.unified_memory import record_experience

    loop = DistillationLoop(engine, epistemic=epistemic)
    source = _source_stub(distilled_ref)
    result = loop.generalize(source, novel_goal, novel_examples)

    if result.success:
        content = (f"technique generalization: {source.promoted_name} -> "
                   f"{result.promoted_name} "
                   f"(heldout {result.heldout_passed}/"
                   f"{result.heldout_examples})")
        raw_outcome: Dict[str, Any] = {
            "status": "generalized",
            "promoted_name": result.promoted_name,
            "heldout": (f"{result.heldout_passed}/"
                        f"{result.heldout_examples}"),
            "negative_controls_passed": result.negative_controls_passed,
            "verdict_admitted": result.verdict_admitted,
            "substitution_path": "generalization",
        }
    else:
        content = (f"technique generalization refused: "
                   f"{source.promoted_name} x {novel_goal[:60]}")
        raw_outcome = {"status": "refused", "reason": result.reason}

    record_experience(
        epistemic,
        origin_loop="acquisition",
        kind=GENERALIZATION_KIND,
        content=content,
        raw={
            "source_promoted_name": source.promoted_name,
            "source_capability_id": source.capability_id,
            "source_delta_id": source.delta_id,
            "consumption_id": distilled_ref.get("consumption_id"),
            "novel_goal": novel_goal,
            "novel_examples": len(novel_examples),
            "outcome": raw_outcome,
        },
        causal_chain=([distilled_ref["consumption_id"]]
                      if distilled_ref.get("consumption_id") else []),
        source="acquisition/generalize_driver",
    )
    return result


def find_generalization_records(epistemic: Any,
                                limit: int = 1000) -> List[Dict[str, Any]]:
    """Read back this driver's records through the unified read path."""
    from swarm_engine.intellect.unified_memory import read_experiences
    return read_experiences(epistemic, kind=GENERALIZATION_KIND, limit=limit)
