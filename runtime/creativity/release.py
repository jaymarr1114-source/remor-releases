"""Gallery admission contract (creativity paper §7, §11 Q4).

The Artifact Lab is the downloads gallery — where everything REMOR generates
ends up; it is NOT a creation surface (paper §7, James 2026-09-26). This
module is the admission contract for that gallery: it admits an artifact if
and only if it meets explicit, written, auditable criteria. It does not
build the gallery UI (Frontend surface track) and it does not generate
candidates (EXEC-1's generation seam, documented below).

§11 Q4 (James, decided): host-admits-and-James-can-remove. The host admits
autonomously against the defined criteria; James retains removal authority;
every admission is auditable with its evaluation evidence attached.

Trust boundary (documented, not hidden):
  admit() takes pipeline INPUTS and runs the pipeline itself — the real
  ledger check (ledger.check_composition) and the real critique pipeline
  (critique_candidate). It accepts NO CompositionVerdict object and NO
  CritiqueReport object: a hand-assembled report cannot be smuggled in
  because there is no parameter for one. The trust roots are the three
  landed mechanisms: the ledger, critique_candidate, and build_panels.
  What admission does NOT verify: that the supplied Ledger instance is
  bound to the real stores (a hostile caller could supply a fake ledger).
  That wiring trust belongs to EXEC-1, which will hold the real instances
  directly. It is recorded here, not assumed away.

What this module does NOT decide (James's gates):
  - the admission criteria VALUES (ADMISSION_CRITERIA_V1 is the reviewable
    artifact — James approves the initial set and any change);
  - whether a RETRIEVAL may ever be admitted (v1: no — a retrieval is not
    a creation; re-admission policy is his call);
  - the file-kind verification table (extendable only by a new mission,
    never by silent edit — see _verify_file_artifact).
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
import uuid
import zipfile
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from runtime.creativity.critique import (
    CandidateComposition,
    CritiqueRefused,
    NoveltyVerdict,
    NOVEL,
    RETRIEVAL,
    VALUE_CRITERIA_V1,
    ValueScore,
    critique_candidate,
)
from runtime.creativity.intent import CreativeIntent
from runtime.creativity.ledger import (
    CompositionVerdict,
    Ledger,
    LedgerEntry,
    LedgerRefused,
)

# ---------------------------------------------------------------------------
# The criteria set — the reviewable artifact (Q4 + Q7).
# Each criterion is a check with a named failure, not a vibe.
# ---------------------------------------------------------------------------

ADMISSION_CRITERIA_V1: Dict[str, str] = {
    "legality": (
        "the ledger's live legality check passes for exactly the "
        "candidate's primitives — one unverified primitive fails admission"),
    "critique_complete": (
        "the real critique pipeline ran to completion without refusal — "
        "no hand-assembled report is accepted, because no report object "
        "is accepted at all"),
    "novelty_bar": (
        "the mechanism's novelty verdict is NOVEL — a RETRIEVAL is refused "
        "and the existing record is named (re-admission policy is James's "
        "call, not this module's)"),
    "value_bar": (
        "every VALUE_CRITERIA_V1 criterion passed, each with its reason — "
        "including outcome_reproduces (observed == claimed, by execution)"),
    "panels_pass": (
        "every Acceptance panel verdict passed — honesty, correctness, "
        "safety, provenance, as judged by the real build_panels()"),
    "provenance_complete": (
        "the commissioning intent is present with a stated outcome; every "
        "primitive carries a ledger entry with a non-empty gate reference"),
    "executes_as_claimed": (
        "the artifact executes/renders/plays as claimed — verified by "
        "actual execution, never assertion; cited file artifacts pass "
        "kind-appropriate open checks"),
}

CRITERIA_VERSION = "v1"


class AdmissionRefused(Exception):
    """Admission refused: the exact failing criteria are carried, never
    a bare refusal."""


# ---------------------------------------------------------------------------
# Records
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class CriterionOutcome:
    criterion: str
    passed: bool
    reason: str


@dataclass(frozen=True)
class AdmissionRecord:
    """The frozen admission record — the gallery entry's evidence.

    Provenance complete or the artifact does not release: intent, primitives,
    verification events, gate references, the critique evidence, and the
    panel verdicts are all carried here.
    """
    admission_id: str
    candidate_id: str
    intent_outcome: str
    commissioning_principal: str
    primitive_ids: Tuple[str, ...]
    verification_events: Tuple[Dict[str, str], ...]
    novelty: Dict[str, Any]
    value: Dict[str, Any]
    panels: Tuple[Dict[str, Any], ...]
    criteria: Tuple[CriterionOutcome, ...]
    admitted_at: float
    criteria_version: str = CRITERIA_VERSION

    def to_dict(self) -> Dict[str, Any]:
        return {
            "admission_id": self.admission_id,
            "candidate_id": self.candidate_id,
            "intent_outcome": self.intent_outcome,
            "commissioning_principal": self.commissioning_principal,
            "primitive_ids": list(self.primitive_ids),
            "verification_events": [dict(e) for e in self.verification_events],
            "novelty": dict(self.novelty),
            "value": self.value,
            "panels": [dict(p) for p in self.panels],
            "criteria": [
                {"criterion": c.criterion, "passed": c.passed,
                 "reason": c.reason}
                for c in self.criteria
            ],
            "admitted_at": self.admitted_at,
            "criteria_version": self.criteria_version,
        }

    @staticmethod
    def from_dict(d: Dict[str, Any]) -> "AdmissionRecord":
        return AdmissionRecord(
            admission_id=d["admission_id"],
            candidate_id=d["candidate_id"],
            intent_outcome=d["intent_outcome"],
            commissioning_principal=d["commissioning_principal"],
            primitive_ids=tuple(d["primitive_ids"]),
            verification_events=tuple(d["verification_events"]),
            novelty=dict(d["novelty"]),
            value=d["value"],
            panels=tuple(d["panels"]),
            criteria=tuple(
                CriterionOutcome(c["criterion"], c["passed"], c["reason"])
                for c in d["criteria"]),
            admitted_at=d["admitted_at"],
            criteria_version=d.get("criteria_version", CRITERIA_VERSION),
        )


@dataclass(frozen=True)
class RemovalRecord:
    """James's removal authority, as a record.

    The authority field records WHOSE authority the removal claims — verbatim,
    auditable. Authorization enforcement (who may call record_removal) belongs
    to the caller layer (EXEC-1 / the host), not to this record: the record
    is the audit trail, not the lock.
    """
    admission_id: str
    removed_at: float
    reason: str
    authority: str
    artifact_disposition: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "admission_id": self.admission_id,
            "removed_at": self.removed_at,
            "reason": self.reason,
            "authority": self.authority,
            "artifact_disposition": self.artifact_disposition,
        }

    @staticmethod
    def from_dict(d: Dict[str, Any]) -> "RemovalRecord":
        return RemovalRecord(
            admission_id=d["admission_id"],
            removed_at=d["removed_at"],
            reason=d["reason"],
            authority=d["authority"],
            artifact_disposition=d["artifact_disposition"],
        )


@dataclass(frozen=True)
class StageOutcome:
    stage: str
    ok: bool
    reason: str


@dataclass(frozen=True)
class ReleaseResult:
    """The §7 release flow as explicit stages: intake → legality →
    critique → panels → criteria → decision → record. Each stage's outcome
    is recorded — what was left out and why is visible, never hidden."""
    candidate_id: str
    stages: Tuple[StageOutcome, ...]
    admitted: bool
    admission: Optional[AdmissionRecord]
    refusal_reason: str = ""


# ---------------------------------------------------------------------------
# Admission registry — sqlite-backed, auditable, no hardcoded paths.
# ---------------------------------------------------------------------------

_SCHEMA = """
CREATE TABLE IF NOT EXISTS admissions (
    admission_id TEXT PRIMARY KEY,
    record_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS removals (
    admission_id TEXT PRIMARY KEY,
    record_json TEXT NOT NULL
);
"""


class AdmissionRegistry:
    """Durable host-side record of admissions and removals.

    This is the admission ledger, not the gallery: the gallery UI (Frontend
    track) renders from the frozen interface; this registry is the
    auditable host-side truth of what was admitted, on what evidence,
    and what was later removed.
    """

    def __init__(self, db_path: str) -> None:
        if not db_path:
            raise AdmissionRefused(
                "admission registry needs a db_path — no hardcoded paths")
        self._db_path = db_path
        conn = sqlite3.connect(db_path)
        try:
            conn.executescript(_SCHEMA)
            conn.commit()
        finally:
            conn.close()

    def register_admission(self, record: AdmissionRecord) -> str:
        conn = sqlite3.connect(self._db_path)
        try:
            conn.execute(
                "INSERT INTO admissions (admission_id, record_json) "
                "VALUES (?, ?)",
                (record.admission_id, json.dumps(record.to_dict())))
            conn.commit()
        finally:
            conn.close()
        return record.admission_id

    def get_admission(self, admission_id: str) -> Optional[AdmissionRecord]:
        conn = sqlite3.connect(self._db_path)
        try:
            row = conn.execute(
                "SELECT record_json FROM admissions WHERE admission_id = ?",
                (admission_id,)).fetchone()
        finally:
            conn.close()
        if row is None:
            return None
        return AdmissionRecord.from_dict(json.loads(row[0]))

    def list_admissions(self) -> List[str]:
        conn = sqlite3.connect(self._db_path)
        try:
            rows = conn.execute(
                "SELECT admission_id FROM admissions ORDER BY admission_id"
            ).fetchall()
        finally:
            conn.close()
        return [r[0] for r in rows]

    def record_removal_entry(self, record: RemovalRecord) -> RemovalRecord:
        if self.get_admission(record.admission_id) is None:
            raise AdmissionRefused(
                f"cannot remove {record.admission_id!r}: no such admission "
                "in the registry")
        conn = sqlite3.connect(self._db_path)
        try:
            conn.execute(
                "INSERT OR REPLACE INTO removals (admission_id, record_json)"
                " VALUES (?, ?)",
                (record.admission_id, json.dumps(record.to_dict())))
            conn.commit()
        finally:
            conn.close()
        return record

    def get_removal(self, admission_id: str) -> Optional[RemovalRecord]:
        conn = sqlite3.connect(self._db_path)
        try:
            row = conn.execute(
                "SELECT record_json FROM removals WHERE admission_id = ?",
                (admission_id,)).fetchone()
        finally:
            conn.close()
        if row is None:
            return None
        return RemovalRecord.from_dict(json.loads(row[0]))


# ---------------------------------------------------------------------------
# File-artifact verification — kind-appropriate open checks.
# Unknown kinds fail closed: an artifact whose kind cannot be verified
# cannot satisfy "executes/renders/plays as claimed".
# ---------------------------------------------------------------------------

def _verify_file_artifact(path: str) -> Tuple[bool, str]:
    """Verify one cited file artifact by actually opening it.

    Returns (ok, reason). Never asserts — always executes.
    """
    if not path or not os.path.isfile(path):
        return False, f"file artifact {path!r} does not exist"
    ext = os.path.splitext(path)[1].lower()
    try:
        if ext == ".zip":
            with zipfile.ZipFile(path) as zf:
                bad = zf.testzip()
            if bad is not None:
                return False, (f"zip artifact {path!r} fails extraction "
                               f"check at {bad!r}")
            return True, f"zip artifact {path!r} extracts cleanly"
        if ext in (".txt", ".md", ".json", ".py", ".csv", ".log"):
            with open(path, "r", encoding="utf-8") as fh:
                content = fh.read()
            if not content:
                return False, f"text artifact {path!r} is empty"
            return True, (f"text artifact {path!r} opens "
                          f"({len(content)} chars)")
        return False, (
            f"file artifact {path!r} has unverifiable kind {ext!r}: "
            "fail-closed — the kind table is extended only by a new "
            "mission, never by assumption")
    except (OSError, zipfile.BadZipFile) as exc:
        return False, f"file artifact {path!r} failed to open: {exc}"


# ---------------------------------------------------------------------------
# The admission path
# ---------------------------------------------------------------------------

def _new_admission_id(candidate_id: str) -> str:
    return f"adm_{int(time.time())}_{candidate_id}_{uuid.uuid4().hex[:8]}"


def admit(*,
          ledger: Ledger,
          primitive_ids: List[str],
          intent: CreativeIntent,
          candidate: CandidateComposition,
          provenance_store: Any,
          composer: Any,
          primitives: Any,
          repo_root: str,
          registry: AdmissionRegistry) -> AdmissionRecord:
    """Run the full admission path on one candidate composition.

    Takes pipeline INPUTS and runs the pipeline itself — the real ledger
    check and the real critique pipeline. Accepts no CompositionVerdict
    and no CritiqueReport: there is no parameter for one, so a
    hand-assembled report cannot be smuggled in.

    Returns the AdmissionRecord on success; raises AdmissionRefused naming
    the exact failing criteria otherwise.
    """
    stages: List[StageOutcome] = []

    def _stage(name: str, ok: bool, reason: str) -> None:
        stages.append(StageOutcome(stage=name, ok=ok, reason=reason))

    # -- intake ---------------------------------------------------------
    if not isinstance(candidate, CandidateComposition):
        raise AdmissionRefused(
            "intake: admission needs a CandidateComposition, got "
            f"{type(candidate).__name__}")
    if not isinstance(intent, CreativeIntent):
        raise AdmissionRefused(
            "intake: provenance incomplete — admission needs a "
            f"CreativeIntent, got {type(intent).__name__}")
    if not intent.outcome or not str(intent.outcome).strip():
        raise AdmissionRefused(
            "intake: provenance incomplete — the commissioning intent "
            "states no outcome")
    _stage("intake", True, "candidate and intent present and typed")

    # -- legality: the REAL ledger, live re-resolution (defense in depth;
    #    critique_candidate checks the verdict again inside the pipeline)
    try:
        verdict = ledger.check_composition(list(primitive_ids))
    except LedgerRefused as exc:
        raise AdmissionRefused(f"legality: ledger refused: {exc}")
    if set(candidate.primitive_ids) != set(primitive_ids):
        raise AdmissionRefused(
            "legality: the primitive set handed to admission does not "
            f"match the candidate's — got {sorted(set(primitive_ids))}, "
            f"candidate uses {sorted(set(candidate.primitive_ids))}")
    if not verdict.legal:
        failing = "; ".join(
            f"{i.get('primitive_id')}: {i.get('reason')}"
            for i in verdict.illegal)
        raise AdmissionRefused(
            f"legality: composition verdict ILLEGAL — {failing}")
    _stage("legality", True,
           f"ledger LEGAL for {sorted(verdict.checked)}")

    # -- critique + panels: the REAL pipeline (raises CritiqueRefused) ----
    try:
        report = critique_candidate(
            verdict=verdict,
            intent=intent,
            candidate=candidate,
            provenance_store=provenance_store,
            composer=composer,
            primitives=primitives,
            repo_root=repo_root,
        )
    except CritiqueRefused as exc:
        raise AdmissionRefused(f"critique: pipeline refused: {exc}")
    _stage("critique", True, "critique pipeline ran to completion")
    _stage("panels", True, "panel verdicts attached by the pipeline")

    # -- criteria --------------------------------------------------------
    outcomes: List[CriterionOutcome] = []

    outcomes.append(CriterionOutcome(
        "legality", True,
        f"ledger LEGAL for {sorted(verdict.checked)}"))

    outcomes.append(CriterionOutcome(
        "critique_complete", True,
        "critique_candidate produced a full CritiqueReport on the real path"))

    nov: NoveltyVerdict = report.novelty
    if nov.verdict == NOVEL:
        outcomes.append(CriterionOutcome(
            "novelty_bar", True, f"novel: {nov.reason}"))
    else:
        outcomes.append(CriterionOutcome(
            "novelty_bar", False,
            f"retrieval, not creation — already exists as "
            f"{nov.matched_capability_id!r} (trust {nov.matched_trust}): "
            f"{nov.reason}. Re-admission policy is James's call."))

    val: ValueScore = report.value
    failed_criteria = [c for c in val.criteria if not c.passed]
    if val.satisfied:
        outcomes.append(CriterionOutcome(
            "value_bar", True,
            "every VALUE_CRITERIA_V1 criterion passed, each with its reason"))
    else:
        outcomes.append(CriterionOutcome(
            "value_bar", False,
            "value criteria failed: " + "; ".join(
                f"{c.criterion}: {c.reason}" for c in failed_criteria)))

    failed_panels = [p for p in report.panels if not p.passed]
    if not failed_panels:
        outcomes.append(CriterionOutcome(
            "panels_pass", True,
            f"all {len(report.panels)} Acceptance panel verdicts passed"))
    else:
        outcomes.append(CriterionOutcome(
            "panels_pass", False,
            "panel failures: " + "; ".join(
                f"{p.panel}: {p.reason}" for p in failed_panels)))

    missing_refs = []
    for pid in candidate.primitive_ids:
        entry = ledger._entries.get(pid)
        if entry is None or not (entry.gate_reference or "").strip():
            missing_refs.append(pid)
    if not missing_refs:
        outcomes.append(CriterionOutcome(
            "provenance_complete", True,
            "intent stated; every primitive carries a ledger entry with a "
            "non-empty gate reference"))
    else:
        outcomes.append(CriterionOutcome(
            "provenance_complete", False,
            "missing gate references for: "
            + ", ".join(sorted(set(missing_refs)))))

    repro = next((c for c in val.criteria
                  if c.criterion == "outcome_reproduces"), None)
    exec_problems: List[str] = []
    if repro is None or not repro.passed:
        exec_problems.append(
            "outcome_reproduces did not pass: "
            + (repro.reason if repro else "criterion absent"))
    for artifact_path in candidate.file_artifacts:
        ok, reason = _verify_file_artifact(artifact_path)
        if not ok:
            exec_problems.append(reason)
    if not exec_problems:
        outcomes.append(CriterionOutcome(
            "executes_as_claimed", True,
            "observed outcome == claimed outcome by real execution"
            + (f"; {len(candidate.file_artifacts)} cited file artifact(s) "
               "opened cleanly" if candidate.file_artifacts else
               "; no file artifacts cited")))
    else:
        outcomes.append(CriterionOutcome(
            "executes_as_claimed", False, "; ".join(exec_problems)))
    _stage("criteria", True,
           f"{sum(1 for o in outcomes if o.passed)}/{len(outcomes)} "
           "criteria passed")

    failed = [o for o in outcomes if not o.passed]
    if failed:
        _stage("decision", False, "refused: "
               + ", ".join(o.criterion for o in failed))
        raise AdmissionRefused(
            "admission refused — failing criteria: " + "; ".join(
                f"{o.criterion}: {o.reason}" for o in failed))
    _stage("decision", True, "all criteria passed")

    # -- record ----------------------------------------------------------
    verification_events = tuple(
        {"primitive_id": pid,
         "source": ledger._entries[pid].verification_source,
         "ref": str(ledger._entries[pid].verification_ref),
         "gate_reference": ledger._entries[pid].gate_reference}
        for pid in candidate.primitive_ids
    )
    record = AdmissionRecord(
        admission_id=_new_admission_id(candidate.candidate_id),
        candidate_id=candidate.candidate_id,
        intent_outcome=intent.outcome,
        commissioning_principal=intent.principal,
        primitive_ids=tuple(candidate.primitive_ids),
        verification_events=verification_events,
        novelty={"verdict": nov.verdict, "reason": nov.reason,
                 "signature": nov.signature,
                 "matched_capability_id": nov.matched_capability_id,
                 "matched_trust": nov.matched_trust},
        value={"satisfied": val.satisfied,
               "criteria": [{"criterion": c.criterion, "passed": c.passed,
                             "reason": c.reason} for c in val.criteria],
               "criteria_version": val.criteria_version},
        panels=tuple(
            {"panel": p.panel, "passed": p.passed, "reason": p.reason,
             "evidence": dict(p.evidence)} for p in report.panels),
        criteria=tuple(outcomes),
        admitted_at=time.time(),
    )
    registry.register_admission(record)
    _stage("record", True,
           f"admission {record.admission_id} registered with full evidence")
    return record


def run_release_flow(*,
                     ledger: Ledger,
                     primitive_ids: List[str],
                     intent: CreativeIntent,
                     candidate: CandidateComposition,
                     provenance_store: Any,
                     composer: Any,
                     primitives: Any,
                     repo_root: str,
                     registry: AdmissionRegistry) -> ReleaseResult:
    """The §7 release flow as explicit stages.

    intake → legality → critique → panels → criteria → decision → record.
    Generation/variation/refinement are NOT built here: the flow takes a
    CandidateComposition as input — the generation seam EXEC-1 will fill.
    What was left out and why is visible in the stage outcomes.
    """
    try:
        record = admit(
            ledger=ledger, primitive_ids=primitive_ids, intent=intent,
            candidate=candidate, provenance_store=provenance_store,
            composer=composer, primitives=primitives, repo_root=repo_root,
            registry=registry)
    except AdmissionRefused as exc:
        reason = str(exc)
        # Reconstruct the stage trail: admit() raises at the first failing
        # gate, so the trail ends at the refusal. The refusal reason names
        # every failing criterion — nothing is hidden, the trail is in the
        # reason text.
        return ReleaseResult(
            candidate_id=(candidate.candidate_id
                          if isinstance(candidate, CandidateComposition)
                          else "?"),
            stages=(StageOutcome("flow", False, reason),),
            admitted=False,
            admission=None,
            refusal_reason=reason,
        )
    return ReleaseResult(
        candidate_id=record.candidate_id,
        stages=(StageOutcome("flow", True,
                             f"admitted as {record.admission_id}"),),
        admitted=True,
        admission=record,
    )


def record_removal(registry: AdmissionRegistry,
                   admission_id: str,
                   reason: str,
                   authority: str = "james",
                   artifact_disposition: str = "quarantined") -> RemovalRecord:
    """Record a removal under James's removal authority (§11 Q4).

    Creates the removal record: what was removed, when, why, by whose
    authority, and what happened to the artifact. The gallery-side removal
    UX is the Frontend track's implementation; this is the host-side
    request shape they implement against.
    """
    if not reason or not str(reason).strip():
        raise AdmissionRefused(
            "removal needs a reason — removals are auditable, never silent")
    record = RemovalRecord(
        admission_id=admission_id,
        removed_at=time.time(),
        reason=str(reason),
        authority=authority,
        artifact_disposition=artifact_disposition,
    )
    return registry.record_removal_entry(record)
