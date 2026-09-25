"""
Governed semantic capability representation (M+25).

A SemanticCapability is a first-class, evidence-backed mapping from a
goal-description pattern (opaque string key) to an operation/family constraint
that capability matching can consume.

This module does NOT invent natural-language meanings. Callers must supply
evidence. Hypotheses are not facts until validated and admitted.

States: hypothesis → candidate → validated → admitted | rejected | contradicted
"""
from __future__ import annotations

import hashlib
import re
import json
import sqlite3
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional, Sequence, Tuple


class SemanticState(str, Enum):
    HYPOTHESIS = "hypothesis"
    CANDIDATE = "candidate"
    VALIDATED = "validated"
    ADMITTED = "admitted"
    REJECTED = "rejected"
    CONTRADICTED = "contradicted"
    SUPERSEDED = "superseded"


@dataclass
class SemanticEvidence:
    """One independent observation supporting or opposing an interpretation."""
    kind: str  # behavioral | registry | external | analogical | ...
    source: str
    payload: Dict[str, Any] = field(default_factory=dict)
    supports: Optional[bool] = True  # None = polarity unassigned
    strength: float = 0.5
    observed_at: float = field(default_factory=time.time)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "kind": self.kind,
            "source": self.source,
            "payload": self.payload,
            "supports": self.supports,
            "strength": self.strength,
            "observed_at": self.observed_at,
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "SemanticEvidence":
        # M+28.0: preserve True / False / None distinctly.
        # None means "no polarity assigned yet" (hypothesis-free evidence);
        # False is opposing evidence; True is supporting. bool(None)==False
        # must not collapse the neutral state.
        if "supports" not in d:
            supports_val = True  # legacy default when key absent
        else:
            supports_val = d["supports"]  # may be True, False, or None
        return cls(
            kind=str(d.get("kind") or "unknown"),
            source=str(d.get("source") or ""),
            payload=dict(d.get("payload") or {}),
            supports=supports_val,
            strength=float(d.get("strength") or 0.5),
            observed_at=float(d.get("observed_at") or time.time()),
        )


@dataclass
class SemanticCapability:
    """Governed semantic interpretation object.

    interpretation is an opaque structured claim, e.g.
      {"family": "multiplicative", "k": 7.0, "role": "elementwise"}
    It must not be treated as true until state is validated/admitted.
    """
    semantic_id: str
    description_key: str  # normalized goal/description fingerprint key
    interpretation: Dict[str, Any]
    state: SemanticState = SemanticState.HYPOTHESIS
    evidence: List[SemanticEvidence] = field(default_factory=list)
    confidence: float = 0.0
    version: int = 1
    parent_id: Optional[str] = None
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    provenance: Dict[str, Any] = field(default_factory=dict)
    risk: str = "medium"

    def as_dict(self) -> Dict[str, Any]:
        return {
            "semantic_id": self.semantic_id,
            "description_key": self.description_key,
            "interpretation": self.interpretation,
            "state": self.state.value if isinstance(self.state, SemanticState) else self.state,
            "evidence": [e.as_dict() for e in self.evidence],
            "confidence": self.confidence,
            "version": self.version,
            "parent_id": self.parent_id,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "provenance": self.provenance,
            "risk": self.risk,
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "SemanticCapability":
        st = d.get("state") or "hypothesis"
        if not isinstance(st, SemanticState):
            st = SemanticState(str(st))
        return cls(
            semantic_id=str(d["semantic_id"]),
            description_key=str(d.get("description_key") or ""),
            interpretation=dict(d.get("interpretation") or {}),
            state=st,
            evidence=[SemanticEvidence.from_dict(e) for e in (d.get("evidence") or [])],
            confidence=float(d.get("confidence") or 0.0),
            version=int(d.get("version") or 1),
            parent_id=d.get("parent_id"),
            created_at=float(d.get("created_at") or time.time()),
            updated_at=float(d.get("updated_at") or time.time()),
            provenance=dict(d.get("provenance") or {}),
            risk=str(d.get("risk") or "medium"),
        )


def make_semantic_id(description_key: str, interpretation: Dict[str, Any], version: int = 1) -> str:
    blob = json.dumps({"k": description_key, "i": interpretation, "v": version},
                      sort_keys=True, separators=(",", ":"))
    return "sem_" + hashlib.sha256(blob.encode()).hexdigest()[:20]


def description_key(text: str) -> str:
    return " ".join((text or "").strip().lower().split())


class SemanticValidator:
    """Validate a semantic candidate against evidence — not against benchmarks.

    Behavioral evidence: interpretation claims family+k; examples must match
    the algebraic consequences of that claim when a composer+plan is provided,
    OR payload must contain explicit input/output pairs consistent with the claim.
    """

    def validate(self, cap: SemanticCapability,
                 behavioral_cases: Optional[Sequence[Tuple[Dict[str, Any], Any]]] = None
                 ) -> Dict[str, Any]:
        supporting = [e for e in cap.evidence if e.supports is True]
        opposing = [e for e in cap.evidence if e.supports is False]
        report: Dict[str, Any] = {
            "ok": False,
            "reasons": [],
            "supporting": len(supporting),
            "opposing": len(opposing),
            "confidence": 0.0,
        }
        if opposing:
            report["reasons"].append(
                f"{len(opposing)} opposing evidence item(s); cannot validate")
            report["ok"] = False
            return report
        if not supporting:
            report["reasons"].append("no supporting evidence")
            return report

        # Strength aggregate
        strength = sum(e.strength for e in supporting) / max(1, len(supporting))
        # Require at least one non-trivial evidence kind
        kinds = {e.kind for e in supporting}
        if kinds <= {"unknown"}:
            report["reasons"].append("evidence kinds are unknown")
            return report

        # Behavioral consistency if cases provided with multiplicative claim
        interp = cap.interpretation or {}
        if behavioral_cases and interp.get("family") == "multiplicative":
            k = interp.get("k")
            if k is None:
                report["reasons"].append("multiplicative claim missing k")
                return report
            ok_n = 0
            for args, expected in behavioral_cases:
                # elementwise if list present
                vals = None
                for v in (args or {}).values():
                    if isinstance(v, (list, tuple)):
                        vals = list(v)
                        break
                if vals is None:
                    continue
                try:
                    pred = [float(x) * float(k) for x in vals]
                    if isinstance(expected, (list, tuple)) and len(expected) == len(pred):
                        if all(abs(float(a) - float(b)) < 1e-6 for a, b in zip(pred, expected)):
                            ok_n += 1
                except Exception:
                    pass
            if ok_n == 0 and behavioral_cases:
                report["reasons"].append("behavioral cases inconsistent with claimed k")
                return report
            if ok_n:
                strength = min(1.0, strength + 0.2)
                report["reasons"].append(f"behavioral consistent on {ok_n} case(s)")

        if strength < 0.4:
            report["reasons"].append(f"aggregate strength {strength:.2f} below threshold")
            return report

        report["ok"] = True
        report["confidence"] = min(1.0, strength)
        report["reasons"].append("sufficient supporting evidence")
        return report


class SemanticCapabilityStore:
    """SQLite-backed store for semantic capabilities."""

    def __init__(self, db_path: str):
        self.db_path = db_path
        self._ensure()

    def _conn(self):
        return sqlite3.connect(self.db_path)

    def _ensure(self) -> None:
        with self._conn() as c:
            c.execute(
                """CREATE TABLE IF NOT EXISTS semantic_capabilities (
                    semantic_id TEXT PRIMARY KEY,
                    description_key TEXT,
                    data_json TEXT NOT NULL,
                    state TEXT,
                    version INTEGER,
                    updated_at REAL
                )"""
            )
            c.execute(
                """CREATE INDEX IF NOT EXISTS idx_sem_key
                   ON semantic_capabilities(description_key)"""
            )

    def store(self, cap: SemanticCapability) -> None:
        cap.updated_at = time.time()
        with self._conn() as c:
            c.execute(
                """INSERT OR REPLACE INTO semantic_capabilities
                   (semantic_id, description_key, data_json, state, version, updated_at)
                   VALUES (?,?,?,?,?,?)""",
                (cap.semantic_id, cap.description_key,
                 json.dumps(cap.as_dict()),
                 cap.state.value if isinstance(cap.state, SemanticState) else str(cap.state),
                 cap.version, cap.updated_at),
            )

    def get(self, semantic_id: str) -> Optional[SemanticCapability]:
        with self._conn() as c:
            row = c.execute(
                "SELECT data_json FROM semantic_capabilities WHERE semantic_id=?",
                (semantic_id,),
            ).fetchone()
        if not row:
            return None
        return SemanticCapability.from_dict(json.loads(row[0]))

    def list(self, state: Optional[str] = None, limit: int = 100) -> List[SemanticCapability]:
        with self._conn() as c:
            if state:
                rows = c.execute(
                    "SELECT data_json FROM semantic_capabilities WHERE state=? "
                    "ORDER BY updated_at DESC LIMIT ?",
                    (state, limit),
                ).fetchall()
            else:
                rows = c.execute(
                    "SELECT data_json FROM semantic_capabilities "
                    "ORDER BY updated_at DESC LIMIT ?",
                    (limit,),
                ).fetchall()
        return [SemanticCapability.from_dict(json.loads(r[0])) for r in rows]

    def find_by_key(self, key: str, state: str = "admitted") -> List[SemanticCapability]:
        key = description_key(key)
        with self._conn() as c:
            rows = c.execute(
                "SELECT data_json FROM semantic_capabilities WHERE description_key=? AND state=?",
                (key, state),
            ).fetchall()
        return [SemanticCapability.from_dict(json.loads(r[0])) for r in rows]


class SemanticAdmissionController:
    """Govern semantic candidates: validate → admit/reject; handle contradiction."""

    def __init__(self, store: SemanticCapabilityStore, validator: Optional[SemanticValidator] = None):
        self.store = store
        self.validator = validator or SemanticValidator()

    def submit_hypothesis(self, description: str, interpretation: Dict[str, Any],
                          evidence: Optional[List[SemanticEvidence]] = None,
                          provenance: Optional[Dict[str, Any]] = None) -> SemanticCapability:
        key = description_key(description)
        sid = make_semantic_id(key, interpretation, 1)
        cap = SemanticCapability(
            semantic_id=sid,
            description_key=key,
            interpretation=dict(interpretation),
            state=SemanticState.HYPOTHESIS,
            evidence=list(evidence or []),
            provenance=dict(provenance or {}),
        )
        if evidence:
            cap.state = SemanticState.CANDIDATE
        self.store.store(cap)
        return cap

    def validate(self, semantic_id: str,
                 behavioral_cases: Optional[Sequence[Tuple[Dict[str, Any], Any]]] = None
                 ) -> Dict[str, Any]:
        cap = self.store.get(semantic_id)
        if cap is None:
            return {"ok": False, "reasons": ["not found"]}
        report = self.validator.validate(cap, behavioral_cases=behavioral_cases)
        if report["ok"]:
            cap.state = SemanticState.VALIDATED
            cap.confidence = float(report["confidence"])
        else:
            cap.state = SemanticState.REJECTED
            cap.confidence = 0.0
        cap.updated_at = time.time()
        self.store.store(cap)
        report["state"] = cap.state.value
        report["semantic_id"] = semantic_id
        return report

    def admit(self, semantic_id: str) -> Dict[str, Any]:
        cap = self.store.get(semantic_id)
        if cap is None:
            return {"ok": False, "reasons": ["not found"]}
        if cap.state != SemanticState.VALIDATED:
            return {"ok": False, "reasons": [f"state is {cap.state.value}, need validated"]}
        # Contradiction: another admitted with same key but different interpretation
        existing = self.store.find_by_key(cap.description_key, state="admitted")
        for other in existing:
            if other.semantic_id == cap.semantic_id:
                continue
            if other.interpretation != cap.interpretation:
                other.state = SemanticState.CONTRADICTED
                self.store.store(other)
                cap.parent_id = other.semantic_id
                cap.version = other.version + 1
        cap.state = SemanticState.ADMITTED
        cap.updated_at = time.time()
        self.store.store(cap)
        return {"ok": True, "semantic_id": cap.semantic_id, "state": cap.state.value,
                "version": cap.version}


    def evaluate_attached_evidence(self, semantic_id: str) -> Dict[str, Any]:
        """Run FormalEvidenceEvaluator on neutral external evidence items.

        Replaces each neutral external evidence item with an evaluated copy
        (supports True/False/None from formal probe consistency).
        Does NOT admit. Does NOT change interpretation.
        """
        cap = self.store.get(semantic_id)
        if cap is None:
            return {"ok": False, "reasons": ["not found"], "evaluated": 0}
        evaluator = FormalEvidenceEvaluator()
        new_evidence = []
        reports = []
        changed = 0
        for e in cap.evidence:
            if e.kind == "external" and e.supports is None:
                # Promote nested probes from metadata if present
                payload = dict(e.payload or {})
                meta = payload.get("metadata") or {}
                if not payload.get("observed_probes") and meta.get("observed_probes"):
                    payload["observed_probes"] = meta["observed_probes"]
                    e = SemanticEvidence(
                        kind=e.kind, source=e.source, payload=payload,
                        supports=e.supports, strength=e.strength,
                        observed_at=e.observed_at)
                applied = evaluator.apply(cap, e)
                reports.append(applied.payload.get("evaluation") if applied.payload else {})
                new_evidence.append(applied)
                changed += 1
            else:
                new_evidence.append(e)
        cap.evidence = new_evidence
        cap.updated_at = time.time()
        self.store.store(cap)
        return {
            "ok": True,
            "evaluated": changed,
            "reports": reports,
            "semantic_id": semantic_id,
            "interpretation": dict(cap.interpretation or {}),
        }

    def validate_with_evaluation(self, semantic_id: str,
                                 behavioral_cases: Optional[Sequence[Tuple[Dict[str, Any], Any]]] = None
                                 ) -> Dict[str, Any]:
        """Evaluate neutral external evidence, then run existing validate().

        Causal path: FormalEvidenceEvaluator → polarity → SemanticValidator.
        Still does not admit.
        """
        prep = self.evaluate_attached_evidence(semantic_id)
        report = self.validate(semantic_id, behavioral_cases=behavioral_cases)
        report["evaluation_prep"] = prep
        return report

    def add_evidence(self, semantic_id: str, evidence: SemanticEvidence) -> Optional[SemanticCapability]:
        cap = self.store.get(semantic_id)
        if cap is None:
            return None
        cap.evidence.append(evidence)
        if evidence.supports is False and cap.state == SemanticState.ADMITTED:
            cap.state = SemanticState.CONTRADICTED
        elif evidence.supports is True and cap.state == SemanticState.HYPOTHESIS:
            cap.state = SemanticState.CANDIDATE
        cap.updated_at = time.time()
        self.store.store(cap)
        return cap


# ---------------------------------------------------------------------------
# M+28.1 — Hypothesis-free external evidence (no interpretation field)
# ---------------------------------------------------------------------------

@dataclass
class ExternalEvidenceRecord:
    """Raw externally sourced observation. MUST NOT contain an interpretation.

    This is Path A substrate: KnowledgeSource → RetrievedKnowledge → here.
    It is not a SemanticCapability and cannot be admitted as one.
    """
    evidence_id: str
    query: str
    content: str
    source: str
    retrieved_at: Optional[float] = None
    metadata: Dict[str, Any] = field(default_factory=dict)
    ingested_at: float = field(default_factory=time.time)
    provenance: Dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "evidence_id": self.evidence_id,
            "query": self.query,
            "content": self.content,
            "source": self.source,
            "retrieved_at": self.retrieved_at,
            "metadata": self.metadata,
            "ingested_at": self.ingested_at,
            "provenance": self.provenance,
            # intentionally no interpretation / supports / family
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "ExternalEvidenceRecord":
        return cls(
            evidence_id=str(d["evidence_id"]),
            query=str(d.get("query") or ""),
            content=str(d.get("content") or ""),
            source=str(d.get("source") or ""),
            retrieved_at=d.get("retrieved_at"),
            metadata=dict(d.get("metadata") or {}),
            ingested_at=float(d.get("ingested_at") or time.time()),
            provenance=dict(d.get("provenance") or {}),
        )

    @classmethod
    def from_retrieved(cls, rk: Any, *, provenance: Optional[Dict[str, Any]] = None
                       ) -> "ExternalEvidenceRecord":
        """Ingest RetrievedKnowledge without adding interpretation."""
        content = getattr(rk, "content", None) or (rk.get("content") if isinstance(rk, dict) else "")
        query = getattr(rk, "query", None) or (rk.get("query") if isinstance(rk, dict) else "")
        source = getattr(rk, "source", None) or (rk.get("source") if isinstance(rk, dict) else "")
        retrieved_at = getattr(rk, "retrieved_at", None)
        if isinstance(rk, dict):
            retrieved_at = rk.get("retrieved_at")
        metadata = getattr(rk, "metadata", None) or (rk.get("metadata") if isinstance(rk, dict) else {}) or {}
        blob = json.dumps({"q": query, "c": content, "s": source}, sort_keys=True)
        eid = "ext_" + hashlib.sha256(blob.encode()).hexdigest()[:20]
        return cls(
            evidence_id=eid,
            query=str(query or ""),
            content=str(content or ""),
            source=str(source or ""),
            retrieved_at=retrieved_at,
            metadata=dict(metadata),
            provenance=dict(provenance or {"ingest": "from_retrieved"}),
        )


class ExternalEvidenceStore:
    """Persist hypothesis-free external evidence. Independent of SemanticCapability."""

    def __init__(self, db_path: str):
        self.db_path = db_path
        self._ensure()

    def _conn(self):
        return sqlite3.connect(self.db_path)

    def _ensure(self) -> None:
        with self._conn() as c:
            c.execute(
                """CREATE TABLE IF NOT EXISTS external_evidence (
                    evidence_id TEXT PRIMARY KEY,
                    query TEXT,
                    source TEXT,
                    data_json TEXT NOT NULL,
                    ingested_at REAL
                )"""
            )

    def store(self, rec: ExternalEvidenceRecord) -> None:
        with self._conn() as c:
            c.execute(
                """INSERT OR REPLACE INTO external_evidence
                   (evidence_id, query, source, data_json, ingested_at)
                   VALUES (?,?,?,?,?)""",
                (rec.evidence_id, rec.query, rec.source,
                 json.dumps(rec.as_dict()), rec.ingested_at),
            )

    def get(self, evidence_id: str) -> Optional[ExternalEvidenceRecord]:
        with self._conn() as c:
            row = c.execute(
                "SELECT data_json FROM external_evidence WHERE evidence_id=?",
                (evidence_id,),
            ).fetchone()
        if not row:
            return None
        return ExternalEvidenceRecord.from_dict(json.loads(row[0]))

    def list(self, limit: int = 100) -> List[ExternalEvidenceRecord]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT data_json FROM external_evidence ORDER BY ingested_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [ExternalEvidenceRecord.from_dict(json.loads(r[0])) for r in rows]

    def list_by_query(self, query: str, limit: int = 50) -> List[ExternalEvidenceRecord]:
        q = (query or "").strip().lower()
        return [r for r in self.list(limit=limit * 2) if r.query.strip().lower() == q][:limit]


def ingest_from_knowledge_source(source, query: str, context: Optional[Dict[str, Any]] = None,
                                 store: Optional[ExternalEvidenceStore] = None
                                 ) -> List[ExternalEvidenceRecord]:
    """Path A: KnowledgeSource.research → ExternalEvidenceRecord (no interpretation)."""
    context = context or {}
    results = source.research(query, context) if source is not None else []
    out: List[ExternalEvidenceRecord] = []
    for rk in results or []:
        rec = ExternalEvidenceRecord.from_retrieved(
            rk, provenance={"pipeline": "ingest_from_knowledge_source", "query": query})
        if store is not None:
            store.store(rec)
        out.append(rec)
    return out


def external_record_to_neutral_evidence(rec: ExternalEvidenceRecord) -> SemanticEvidence:
    """Convert ExternalEvidenceRecord → SemanticEvidence with supports=None.

    Does NOT interpret content, assign family, or set polarity True/False.
    Payload preserves raw content + retrieval metadata for provenance.
    """
    return SemanticEvidence(
        kind="external",
        source=str(rec.source or ""),
        payload={
            "content": rec.content,
            "query": rec.query,
            "evidence_id": rec.evidence_id,
            "metadata": dict(rec.metadata or {}),
            "retrieved_at": rec.retrieved_at,
            "provenance": dict(rec.provenance or {}),
            "ingested_at": rec.ingested_at,
        },
        supports=None,  # neutral — attachment is not evaluation
        strength=0.0,   # unassessed until a later evaluation step
        observed_at=float(rec.ingested_at or time.time()),
    )


def attach_external_evidence_neutral(
        admission: "SemanticAdmissionController",
        semantic_id: str,
        rec: ExternalEvidenceRecord,
) -> Optional["SemanticCapability"]:
    """Attach raw external evidence to an existing hypothesis as neutral evidence.

    Preconditions: hypothesis already exists (manual control or prior step).
    Postconditions: interpretation unchanged; supports is None on new item.
    """
    evidence = external_record_to_neutral_evidence(rec)
    return admission.add_evidence(semantic_id, evidence)


# ---------------------------------------------------------------------------
# M+28.3 — Formal evidence evaluation (NOT natural-language interpretation)
# ---------------------------------------------------------------------------

class EvidencePolarity(str, Enum):
    SUPPORTS = "supports"
    OPPOSES = "opposes"
    NEUTRAL = "neutral"


@dataclass
class EvidenceEvaluation:
    """Result of evaluating one evidence item against a stated hypothesis."""
    polarity: EvidencePolarity
    reason: str
    evidence_id: Optional[str] = None
    hypothesis_key: Optional[str] = None
    mechanism: str = "formal_probe_consistency"
    confidence: float = 0.0
    provenance: Dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "polarity": self.polarity.value if isinstance(self.polarity, EvidencePolarity) else self.polarity,
            "reason": self.reason,
            "evidence_id": self.evidence_id,
            "hypothesis_key": self.hypothesis_key,
            "mechanism": self.mechanism,
            "confidence": self.confidence,
            "provenance": self.provenance,
        }


class FormalEvidenceEvaluator:
    """Evaluate external evidence against an *explicitly stated* hypothesis.

    Scope: formal/algebraic consistency only.
    - If evidence payload contains observed_probes (or probes) with
      {args|input, observed|output}, check consistency with hypothesis
      interpretation {family, k|b}.
    - No probes / empty / unstructured text → NEUTRAL.
    - Does NOT parse natural language or use synonym tables.
    """

    def evaluate(self, hypothesis: SemanticCapability,
                 evidence: SemanticEvidence) -> EvidenceEvaluation:
        interp = dict(hypothesis.interpretation or {})
        payload = dict(evidence.payload or {}) if evidence.payload else {}
        eid = payload.get("evidence_id") or evidence.source
        hyp_key = hypothesis.description_key or hypothesis.semantic_id

        content = (payload.get("content") or "").strip()
        probes = payload.get("observed_probes") or payload.get("probes") or []
        structured_obs = payload.get("structured_observations") or []

        # M+28.6: explicit structured semantic observations vs hypothesis
        if structured_obs:
            return self._eval_structured_observations(
                interp, structured_obs, eid, hyp_key)

        # Empty / no formal structure → NEUTRAL
        if not probes and not content:
            return EvidenceEvaluation(
                polarity=EvidencePolarity.NEUTRAL,
                reason="empty evidence content and no probes",
                evidence_id=str(eid) if eid else None,
                hypothesis_key=hyp_key,
                confidence=0.0,
                provenance={"mechanism": "formal_probe_consistency"},
            )

        if not probes:
            # Unstructured text alone cannot assign polarity without NL interpretation
            return EvidenceEvaluation(
                polarity=EvidencePolarity.NEUTRAL,
                reason="no structured probes; unstructured content is not formally evaluated",
                evidence_id=str(eid) if eid else None,
                hypothesis_key=hyp_key,
                confidence=0.0,
                provenance={"mechanism": "formal_probe_consistency",
                            "content_len": len(content)},
            )

        family = interp.get("family")
        if family not in ("multiplicative", "additive"):
            return EvidenceEvaluation(
                polarity=EvidencePolarity.NEUTRAL,
                reason=f"hypothesis family {family!r} has no formal probe rule",
                evidence_id=str(eid) if eid else None,
                hypothesis_key=hyp_key,
                confidence=0.0,
            )

        ok, bad, n = 0, 0, 0
        for probe in probes:
            if not isinstance(probe, dict):
                continue
            args = probe.get("args") or probe.get("input") or {}
            observed = probe.get("observed") if "observed" in probe else probe.get("output")
            if observed is None:
                continue
            predicted = self._predict(interp, args)
            if predicted is None:
                continue
            n += 1
            if self._close(predicted, observed):
                ok += 1
            else:
                bad += 1

        if n == 0:
            return EvidenceEvaluation(
                polarity=EvidencePolarity.NEUTRAL,
                reason="probes present but none usable for formal prediction",
                evidence_id=str(eid) if eid else None,
                hypothesis_key=hyp_key,
                confidence=0.0,
            )
        if bad == 0 and ok > 0:
            return EvidenceEvaluation(
                polarity=EvidencePolarity.SUPPORTS,
                reason=f"all {ok} usable probes consistent with hypothesis",
                evidence_id=str(eid) if eid else None,
                hypothesis_key=hyp_key,
                confidence=min(1.0, 0.5 + 0.1 * ok),
                provenance={"ok": ok, "bad": bad, "n": n},
            )
        if bad > 0:
            return EvidenceEvaluation(
                polarity=EvidencePolarity.OPPOSES,
                reason=f"{bad}/{n} probes inconsistent with hypothesis",
                evidence_id=str(eid) if eid else None,
                hypothesis_key=hyp_key,
                confidence=min(1.0, 0.5 + 0.1 * bad),
                provenance={"ok": ok, "bad": bad, "n": n},
            )
        return EvidenceEvaluation(
            polarity=EvidencePolarity.NEUTRAL,
            reason="no decisive probe outcome",
            evidence_id=str(eid) if eid else None,
            hypothesis_key=hyp_key,
            confidence=0.0,
        )

    def apply(self, hypothesis: SemanticCapability,
              evidence: SemanticEvidence) -> SemanticEvidence:
        """Return a *copy* of evidence with supports set from formal evaluation.

        Does not mutate the original; does not change hypothesis.interpretation.
        """
        ev = self.evaluate(hypothesis, evidence)
        supports: Optional[bool]
        if ev.polarity == EvidencePolarity.SUPPORTS:
            supports = True
        elif ev.polarity == EvidencePolarity.OPPOSES:
            supports = False
        else:
            supports = None
        payload = dict(evidence.payload or {})
        payload["evaluation"] = ev.as_dict()
        return SemanticEvidence(
            kind=evidence.kind,
            source=evidence.source,
            payload=payload,
            supports=supports,
            strength=float(ev.confidence or evidence.strength or 0.0),
            observed_at=evidence.observed_at,
        )


    def _eval_structured_observations(self, interp, structured_obs, eid, hyp_key):
        """Compare explicit schema observations to hypothesis — not NL."""
        hyp_family = interp.get("family")
        matches, conflicts = 0, 0
        for o in structured_obs:
            if not isinstance(o, dict):
                continue
            if o.get("schema") and o.get("schema") != "swarm.semantic_observation.v1":
                continue
            ofam = o.get("family")
            opar = o.get("parameter")
            if ofam is None or opar is None:
                continue
            if ofam != hyp_family:
                conflicts += 1
                continue
            # parameter vs k/b
            if hyp_family == "multiplicative":
                try:
                    if abs(float(opar) - float(interp.get("k"))) < 1e-9:
                        matches += 1
                    else:
                        conflicts += 1
                except Exception:
                    conflicts += 1
            elif hyp_family == "additive":
                try:
                    ref = interp.get("b", interp.get("k"))
                    if abs(float(opar) - float(ref)) < 1e-9:
                        matches += 1
                    else:
                        conflicts += 1
                except Exception:
                    conflicts += 1
            else:
                conflicts += 1
        if matches and not conflicts:
            return EvidenceEvaluation(
                polarity=EvidencePolarity.SUPPORTS,
                reason=f"{matches} structured observation(s) match hypothesis",
                evidence_id=str(eid) if eid else None,
                hypothesis_key=hyp_key,
                confidence=min(1.0, 0.5 + 0.1 * matches),
                provenance={"matches": matches, "conflicts": conflicts,
                            "mechanism": "structured_observation_match"},
            )
        if conflicts:
            return EvidenceEvaluation(
                polarity=EvidencePolarity.OPPOSES,
                reason=f"{conflicts} structured observation(s) conflict with hypothesis",
                evidence_id=str(eid) if eid else None,
                hypothesis_key=hyp_key,
                confidence=min(1.0, 0.5 + 0.1 * conflicts),
                provenance={"matches": matches, "conflicts": conflicts,
                            "mechanism": "structured_observation_match"},
            )
        return EvidenceEvaluation(
            polarity=EvidencePolarity.NEUTRAL,
            reason="structured observations present but not decisive",
            evidence_id=str(eid) if eid else None,
            hypothesis_key=hyp_key,
            confidence=0.0,
        )

    def _predict(self, interp: Dict[str, Any], args: Dict[str, Any]) -> Any:
        family = interp.get("family")
        vals = None
        for v in (args or {}).values():
            if isinstance(v, (list, tuple)):
                vals = list(v)
                break
        if vals is None:
            return None
        try:
            if family == "multiplicative":
                k = float(interp["k"])
                return [float(x) * k for x in vals]
            if family == "additive":
                b = float(interp.get("b", interp.get("k", 0)))
                return [float(x) + b for x in vals]
        except Exception:
            return None
        return None

    @staticmethod
    def _close(a: Any, b: Any, tol: float = 1e-6) -> bool:
        if isinstance(a, (list, tuple)) and isinstance(b, (list, tuple)):
            if len(a) != len(b):
                return False
            try:
                return all(abs(float(x) - float(y)) <= tol for x, y in zip(a, b))
            except Exception:
                return list(a) == list(b)
        try:
            return abs(float(a) - float(b)) <= tol
        except Exception:
            return a == b


# ---------------------------------------------------------------------------
# M+28.5 — Structured probe extraction (explicit formats only; no NL semantics)
# ---------------------------------------------------------------------------

_EXTRACTOR_VERSION = "structured_probe_extractor.v1"


@dataclass
class StructuredProbe:
    """One explicitly extracted input/output observation."""
    input: Any
    output: Any
    raw_fragment: str
    source: str = ""
    evidence_id: str = ""
    offset: Optional[int] = None
    mechanism: str = _EXTRACTOR_VERSION
    provenance: Dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "input": self.input,
            "output": self.output,
            "raw_fragment": self.raw_fragment,
            "source": self.source,
            "evidence_id": self.evidence_id,
            "offset": self.offset,
            "mechanism": self.mechanism,
            "provenance": self.provenance,
        }

    def as_evaluator_probe(self) -> Dict[str, Any]:
        # FormalEvidenceEvaluator expects list-valued args["values"] and list observed
        # when using multiplicative/additive formal rules.
        if isinstance(self.input, dict):
            args = self.input
            observed = self.output
        elif isinstance(self.input, list):
            args = {"values": self.input}
            observed = self.output if isinstance(self.output, list) else [self.output]
        else:
            args = {"values": [self.input]}
            observed = self.output if isinstance(self.output, list) else [self.output]
        return {"args": args, "observed": observed, "raw_fragment": self.raw_fragment}


def extract_structured_probes(evidence: Any) -> List[StructuredProbe]:
    """Extract explicit structured observations from external evidence.

    Does NOT take a hypothesis. Does NOT interpret natural language.
    Supported explicit formats only (deterministic):
      - 'Input: <num>\\nOutput: <num>' blocks
      - '<num> → <num>' or '<num> -> <num>' lines
      - JSON objects/lines with input/output or args/observed keys
      - list values: 'Input: [1, 2]\\nOutput: [7, 14]'
    Unstructured prose yields [].
    """
    content = ""
    source = ""
    evidence_id = ""
    if isinstance(evidence, ExternalEvidenceRecord):
        content = evidence.content or ""
        source = evidence.source or ""
        evidence_id = evidence.evidence_id or ""
    elif isinstance(evidence, SemanticEvidence):
        payload = evidence.payload or {}
        content = str(payload.get("content") or "")
        source = evidence.source or ""
        evidence_id = str(payload.get("evidence_id") or "")
    elif isinstance(evidence, dict):
        content = str(evidence.get("content") or "")
        source = str(evidence.get("source") or "")
        evidence_id = str(evidence.get("evidence_id") or "")
    elif isinstance(evidence, str):
        content = evidence
    else:
        return []

    probes: List[StructuredProbe] = []
    seen_offsets = set()

    def _add(inp, out, fragment, offset):
        if offset is not None and offset in seen_offsets:
            return
        if offset is not None:
            seen_offsets.add(offset)
        probes.append(StructuredProbe(
            input=inp, output=out, raw_fragment=fragment.strip(),
            source=source, evidence_id=evidence_id, offset=offset,
            provenance={"extractor": _EXTRACTOR_VERSION},
        ))

    def _parse_value(s: str):
        s = s.strip()
        if not s:
            return None
        # JSON list or number
        try:
            return json.loads(s)
        except Exception:
            pass
        try:
            if "." in s:
                return float(s)
            return int(s)
        except Exception:
            return None  # non-numeric → reject (no coercion of 'banana')

    # Pattern 1: Input: ... Output: ... (possibly multiline)
    for m in re.finditer(
        r"(?im)^\s*Input\s*:\s*(.+?)\s*$\s*^\s*Output\s*:\s*(.+?)\s*$",
        content,
    ):
        inp = _parse_value(m.group(1))
        out = _parse_value(m.group(2))
        if inp is None or out is None:
            continue  # partial/malformed skipped, not invented
        _add(inp, out, m.group(0), m.start())

    # Pattern 2: num → num or num -> num (single values or JSON lists on a line)
    for m in re.finditer(
        r"(?m)^\s*(\[[^\]]+\]|-?\d+(?:\.\d+)?)\s*(?:→|->)\s*(\[[^\]]+\]|-?\d+(?:\.\d+)?)\s*$",
        content,
    ):
        inp = _parse_value(m.group(1))
        out = _parse_value(m.group(2))
        if inp is None or out is None:
            continue
        _add(inp, out, m.group(0), m.start())

    # Pattern 3: JSON object lines
    for m in re.finditer(r"(?m)^\s*(\{.*\})\s*$", content):
        try:
            obj = json.loads(m.group(1))
        except Exception:
            continue
        if not isinstance(obj, dict):
            continue
        if "input" in obj and "output" in obj:
            _add(obj["input"], obj["output"], m.group(0), m.start())
        elif "args" in obj and ("observed" in obj or "output" in obj):
            out = obj.get("observed", obj.get("output"))
            _add(obj["args"], out, m.group(0), m.start())

    return probes


def probes_to_evidence_payload(probes: List[StructuredProbe],
                               base_payload: Optional[Dict[str, Any]] = None
                               ) -> Dict[str, Any]:
    """Attach extracted probes for FormalEvidenceEvaluator without polarity."""
    payload = dict(base_payload or {})
    payload["observed_probes"] = [p.as_evaluator_probe() for p in probes]
    payload["extracted_probes"] = [p.as_dict() for p in probes]
    payload["extraction_mechanism"] = _EXTRACTOR_VERSION
    return payload


def attach_extracted_probes_neutral(
        admission: "SemanticAdmissionController",
        semantic_id: str,
        rec: ExternalEvidenceRecord,
) -> Optional["SemanticCapability"]:
    """Extract probes from ExternalEvidenceRecord, attach as neutral external evidence.

    Extraction does not receive the hypothesis. Polarity remains None until evaluation.
    """
    probes = extract_structured_probes(rec)
    se = external_record_to_neutral_evidence(rec)
    payload = probes_to_evidence_payload(probes, se.payload)
    se = SemanticEvidence(
        kind=se.kind, source=se.source, payload=payload,
        supports=None, strength=0.0, observed_at=se.observed_at,
    )
    return admission.add_evidence(semantic_id, se)


# ---------------------------------------------------------------------------
# M+28.6 — Structured semantic observation schema (explicit only; no NL)
# ---------------------------------------------------------------------------

_SEM_OBS_SCHEMA = "swarm.semantic_observation.v1"


@dataclass
class StructuredSemanticObservation:
    """Explicit machine-readable semantic proposition from external evidence.

    MUST NOT be produced from free prose. Contains formal fields only.
    Does not include polarity or hypothesis identity.
    """
    family: str
    parameter: Any  # k or b depending on family
    schema: str = _SEM_OBS_SCHEMA
    input_domain: Optional[str] = None
    output_relation: Optional[str] = None
    raw_fragment: str = ""
    source: str = ""
    evidence_id: str = ""
    offset: Optional[int] = None
    mechanism: str = "structured_semantic_extractor.v1"
    provenance: Dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "schema": self.schema,
            "family": self.family,
            "parameter": self.parameter,
            "input_domain": self.input_domain,
            "output_relation": self.output_relation,
            "raw_fragment": self.raw_fragment,
            "source": self.source,
            "evidence_id": self.evidence_id,
            "offset": self.offset,
            "mechanism": self.mechanism,
            "provenance": self.provenance,
        }

    def as_interpretation_fragment(self) -> Dict[str, Any]:
        """Map to FormalEvidenceEvaluator-comparable interpretation fields."""
        out: Dict[str, Any] = {"family": self.family}
        if self.family == "multiplicative":
            out["k"] = self.parameter
        elif self.family == "additive":
            out["b"] = self.parameter
        else:
            out["parameter"] = self.parameter
        return out


def extract_structured_semantics(evidence: Any) -> List[StructuredSemanticObservation]:
    """Extract explicit structured semantic observations. NO hypothesis argument.

    Accepted formats (deterministic):
      1) JSON object with schema==swarm.semantic_observation.v1 and family+parameter
      2) Line-oriented:
            schema: swarm.semantic_observation.v1
            family: multiplicative
            parameter: 7
      3) Compact: family=multiplicative; parameter=7; schema=swarm.semantic_observation.v1

    Prose without schema is ignored (returns []).
    """
    content, source, evidence_id = _evidence_text_parts(evidence)
    if not content.strip():
        return []
    obs: List[StructuredSemanticObservation] = []

    def _valid_family(f: str) -> bool:
        return f in ("multiplicative", "additive")

    def _add(family, param, fragment, offset, extra=None):
        if not _valid_family(str(family)):
            return
        if param is None:
            return
        try:
            if isinstance(param, str) and param.strip():
                param = json.loads(param) if param.strip()[:1] in "[{" else (
                    float(param) if "." in param else int(param))
        except Exception:
            return  # malformed parameter — no guess
        o = StructuredSemanticObservation(
            family=str(family),
            parameter=param,
            schema=_SEM_OBS_SCHEMA,
            input_domain=(extra or {}).get("input_domain"),
            output_relation=(extra or {}).get("output_relation"),
            raw_fragment=(fragment or "").strip(),
            source=source,
            evidence_id=evidence_id,
            offset=offset,
            provenance={"extractor": "structured_semantic_extractor.v1"},
        )
        obs.append(o)

    # JSON objects (whole content or line)
    for m in re.finditer(r"\{[^{}]+\}", content):
        try:
            obj = json.loads(m.group(0))
        except Exception:
            continue
        if not isinstance(obj, dict):
            continue
        schema = obj.get("schema") or obj.get("$schema")
        if schema != _SEM_OBS_SCHEMA:
            continue
        if "family" not in obj or "parameter" not in obj:
            continue
        _add(obj["family"], obj["parameter"], m.group(0), m.start(), obj)

    # Line-oriented block: schema: ... family: ... parameter: ...
    for m in re.finditer(
        r"(?is)schema\s*:\s*" + re.escape(_SEM_OBS_SCHEMA)
        + r".*?family\s*:\s*(\w+).*?parameter\s*:\s*([^\n]+)",
        content,
    ):
        extra = {}
        block = m.group(0)
        dm = re.search(r"input_domain\s*:\s*(\S+)", block, re.I)
        if dm:
            extra["input_domain"] = dm.group(1).strip()
        rm = re.search(r"output_relation\s*:\s*(.+)$", block, re.I | re.M)
        if rm:
            extra["output_relation"] = rm.group(1).strip()
        _add(m.group(1), m.group(2).strip(), block, m.start(), extra)

    # Compact semicolon form
    for m in re.finditer(
        r"family\s*=\s*(\w+)\s*;\s*parameter\s*=\s*([^;]+)\s*;\s*schema\s*=\s*"
        + re.escape(_SEM_OBS_SCHEMA),
        content, re.I,
    ):
        _add(m.group(1), m.group(2).strip(), m.group(0), m.start())

    return obs


def _evidence_text_parts(evidence: Any) -> Tuple[str, str, str]:
    if isinstance(evidence, ExternalEvidenceRecord):
        return evidence.content or "", evidence.source or "", evidence.evidence_id or ""
    if isinstance(evidence, SemanticEvidence):
        payload = evidence.payload or {}
        return (str(payload.get("content") or ""), evidence.source or "",
                str(payload.get("evidence_id") or ""))
    if isinstance(evidence, dict):
        return (str(evidence.get("content") or ""), str(evidence.get("source") or ""),
                str(evidence.get("evidence_id") or ""))
    if isinstance(evidence, str):
        return evidence, "", ""
    return "", "", ""


def observation_to_neutral_evidence(
        rec: ExternalEvidenceRecord,
        observations: List[StructuredSemanticObservation],
) -> SemanticEvidence:
    """Attach structured observations as neutral external evidence for evaluation."""
    se = external_record_to_neutral_evidence(rec)
    payload = dict(se.payload or {})
    payload["structured_observations"] = [o.as_dict() for o in observations]
    payload["extraction_mechanism"] = "structured_semantic_extractor.v1"
    return SemanticEvidence(
        kind=se.kind, source=se.source, payload=payload,
        supports=None, strength=0.0, observed_at=se.observed_at,
    )
