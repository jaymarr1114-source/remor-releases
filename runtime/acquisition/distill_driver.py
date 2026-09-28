"""V10-P4: continuous distillation driver (charter Test 1).

Wires V10-P3's captured technique deltas into M2's DistillationLoop.
This file builds ONLY the missing wiring:

  pending-delta discovery -> charter->DeltaRecord adapter ->
  DistillationLoop.distill -> consumption marking.

Everything else already exists and is reused through frozen interfaces:

  - DistillationLoop (swarm_engine/acquisition/distill.py, M2): the loop
    itself -- Route A (near-miss adaptation), Route B (fresh synthesis +
    held-out verification in fresh processes + negative controls +
    ReviewBoard + promotion through the frozen promotion API). It never
    raises on a failed distillation; the failure is named in the result.
  - DeltaRecord (swarm_engine/acquisition/delta.py, M2): validate /
    split_evidence / mark_synthesized. Its causal discipline (minimum
    I/O examples, input-dict shape) is enforced, never bypassed.
  - DeltaSession capture + causal adjudication (V10-P3's delta_capture.py):
    the only source of technique_delta records. Called, never edited.
  - record_experience / read_experiences (V10-P1's unified_memory.py):
    the single read/write path. Called, never edited.

What "unprompted" means here: one call to run_distillation_sweep()
processes EVERY pending delta. No human in the loop per delta. (Calling
the sweep on a schedule -- cadence -- is V10-P2's mandate, not this
file's. This file is the sweep; V10-P2 is the clock.)

Anti-duplication contract (James, 2026-09-27): if you are tempted to
write a second distiller, a second capture path, a second store, or a
parallel verifier in this file -- stop. That is the defect this mission
was ordered not to commit. The loop is DistillationLoop. The store is
the epistemic store. The verifier is ReviewBoard.
"""

from __future__ import annotations

import json
import os
from typing import Any, Dict, List, Tuple

CONSUMPTION_KIND = "distillation_consumption"


class NotDistillable(Exception):
    """A captured delta the synthesis loop cannot consume.

    Named, never silent. The reason says exactly which precondition
    failed so the refusal is inspectable, not a shrug.
    """


# ---------------------------------------------------------------------------
# Pending-delta discovery (V10-P1's read path; nothing else)
# ---------------------------------------------------------------------------

def _technique_deltas(epistemic: Any, limit: int) -> List[Dict[str, Any]]:
    from swarm_engine.intellect.unified_memory import read_experiences
    return read_experiences(epistemic, origin_loop="acquisition",
                            kind="technique_delta", limit=limit)


def _consumed_observation_ids(epistemic: Any, limit: int) -> set:
    from swarm_engine.intellect.unified_memory import read_experiences
    recs = read_experiences(epistemic, origin_loop="acquisition",
                            kind=CONSUMPTION_KIND, limit=limit)
    return {(r.get("raw") or {}).get("delta_observation_id") for r in recs
            if (r.get("raw") or {}).get("delta_observation_id")}


def find_pending_deltas(epistemic: Any, limit: int = 1000) -> List[Dict[str, Any]]:
    """Every technique_delta record with no consumption record.

    Consumption records are append-only and provenance-stamped (see
    mark_consumed); a delta examined once is never distilled twice.
    """
    consumed = _consumed_observation_ids(epistemic, limit)
    return [r for r in _technique_deltas(epistemic, limit)
            if r.get("observation_id") not in consumed]


# ---------------------------------------------------------------------------
# Charter-delta -> DeltaRecord adapter
# ---------------------------------------------------------------------------

def extract_io_examples(delta_rec: Dict[str, Any]) -> List[Tuple[Dict[str, Any], Any]]:
    """Pull worked I/O examples out of a charter delta's cited evidence.

    Convention (documented, not smuggled): a cited file artifact whose
    contents parse as JSON with a top-level ``io_examples`` list of
    ``{"input": {...}, "output": ...}`` supplies the synthesis evidence.
    The session genuinely demonstrates with worked examples; the file is
    the cited evidence; this function reads what the session wrote.

    Raises NotDistillable when the delta carries behavioral/file
    artifacts only. That is a named fail-closed, not a gap in the
    adapter: the synthesis loop distills from worked examples, and a
    delta without them is not its input.
    """
    raw = delta_rec.get("raw") or {}
    delta = raw.get("delta") or {}
    checked: List[str] = []
    found: List[Tuple[Dict[str, Any], Any]] = []
    for artifact in delta.get("evidence_e") or []:
        if not isinstance(artifact, dict) or artifact.get("kind") != "file":
            continue
        path = artifact.get("path")
        checked.append(str(path))
        if not path or not os.path.isfile(path):
            continue
        try:
            with open(path, "r", encoding="utf-8") as handle:
                doc = json.load(handle)
        except Exception:
            continue
        examples = doc.get("io_examples") if isinstance(doc, dict) else None
        if not isinstance(examples, list):
            continue
        for i, example in enumerate(examples):
            if (not isinstance(example, dict)
                    or not isinstance(example.get("input"), dict)
                    or "output" not in example):
                raise NotDistillable(
                    f"io_examples[{i}] in {path} is not "
                    "{input: dict, output: ...}: malformed evidence")
            found.append((dict(example["input"]), example["output"]))
    if not found:
        raise NotDistillable(
            "no I/O examples in cited evidence: delta carries behavioral "
            f"artifacts only (checked: {checked}); the synthesis loop "
            "distills from worked input/output examples")
    return found


def charter_to_delta_record(delta_rec: Dict[str, Any]) -> Any:
    """Adapt one V10-P3 charter delta to M2's DeltaRecord.

    Field mapping is direct (the charter schema and the delta schema
    describe the same Y-Z record); evidence comes from extract_io_examples.
    Raises NotDistillable -- never returns a half-built record, never
    weakens DeltaRecord.validate().
    """
    from swarm_engine.acquisition.delta import (
        DeltaRecord, DeltaValidationError)
    raw = delta_rec.get("raw") or {}
    delta = raw.get("delta") or {}
    if not delta:
        raise NotDistillable("record carries no charter delta payload")
    examples = extract_io_examples(delta_rec)  # may raise NotDistillable
    technique = delta.get("technique_t") or {}
    record = DeltaRecord(
        objective=str(delta.get("objective_x") or ""),
        external_actions=str(delta.get("external_demo_y") or ""),
        prior_capability=str(delta.get("native_inventory_z") or ""),
        capability_gap=str(delta.get("capability_gap") or ""),
        technique=str(technique.get("name") or "") if isinstance(
            technique, dict) else "",
        evidence=[{"input": inputs, "output": output}
                  for inputs, output in examples],
        dependencies=list(delta.get("dependencies_d") or []),
        verification={"performed": str(delta.get("verification_v") or "")},
        source="v10p4-charter-adapter",
        delta_id=str(raw.get("session_id")
                     or delta_rec.get("observation_id") or ""),
    )
    try:
        record.validate()
    except DeltaValidationError as exc:
        raise NotDistillable(f"DeltaRecord discipline refused: {exc}")
    return record


# ---------------------------------------------------------------------------
# Consumption marking (append-only, provenance-preserving)
# ---------------------------------------------------------------------------

def mark_consumed(epistemic: Any, delta_rec: Dict[str, Any],
                  outcome: Dict[str, Any]) -> str:
    """Record that a delta was examined by the sweep, with its outcome.

    The record is append-only: the delta itself is never mutated, so
    V10-P3's capture stays the single writer of technique_delta records.
    outcome carries status "distilled" | "refused" | "error" plus the
    reason or the promotion identifiers -- inspectable, never silent.
    """
    from swarm_engine.intellect.unified_memory import record_experience
    obs_id = delta_rec.get("observation_id")
    return record_experience(
        epistemic,
        origin_loop="acquisition",
        kind=CONSUMPTION_KIND,
        content=(f"distillation consumption: {obs_id} -> "
                 f"{outcome.get('status')}"),
        raw={"delta_observation_id": obs_id,
             "delta": (delta_rec.get("raw") or {}).get("delta"),
             "outcome": dict(outcome)},
        causal_chain=[obs_id] if obs_id else [],
        source="acquisition/distillation_consumption")


# ---------------------------------------------------------------------------
# The unprompted sweep
# ---------------------------------------------------------------------------

def run_distillation_sweep(engine: Any, epistemic: Any,
                           limit: int = 1000) -> Dict[str, Any]:
    """Process every pending technique delta. One call, all deltas, no human.

    For each pending delta: adapt to DeltaRecord, run the EXISTING
    DistillationLoop, mark consumed with the outcome. Per-delta failures
    (refusals, errors) are recorded, never raised: one bad delta must not
    kill the sweep. Returns a summary dict.
    """
    from swarm_engine.acquisition.distill import DistillationLoop
    loop = DistillationLoop(engine, epistemic=epistemic)
    summary: Dict[str, Any] = {
        "examined": 0, "distilled": [], "refused": [], "errors": []}
    for rec in find_pending_deltas(epistemic, limit):
        obs_id = rec.get("observation_id")
        summary["examined"] += 1
        try:
            delta = charter_to_delta_record(rec)
        except NotDistillable as exc:
            mark_consumed(epistemic, rec,
                          {"status": "refused", "reason": str(exc)})
            summary["refused"].append(
                {"delta": obs_id, "reason": str(exc)})
            continue
        try:
            result = loop.distill(delta)
        except Exception as exc:  # backstop: the loop names failures itself
            reason = f"{type(exc).__name__}: {exc}"
            mark_consumed(epistemic, rec,
                          {"status": "error", "reason": reason})
            summary["errors"].append({"delta": obs_id, "reason": reason})
            continue
        if result.success:
            outcome = {"status": "distilled",
                       "route": result.route,
                       "promoted_name": result.promoted_name,
                       "capability_id": result.capability_id,
                       "heldout": (f"{result.heldout_passed}/"
                                   f"{result.heldout_examples}"),
                       "verdict_admitted": result.verdict_admitted}
            mark_consumed(epistemic, rec, outcome)
            summary["distilled"].append(
                {"delta": obs_id,
                 "promoted_name": result.promoted_name,
                 "heldout": outcome["heldout"]})
        else:
            mark_consumed(epistemic, rec,
                          {"status": "refused", "reason": result.reason})
            summary["refused"].append(
                {"delta": obs_id, "reason": result.reason})
    return summary
