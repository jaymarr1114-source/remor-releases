"""
swarm_engine/acquisition/objective_bridge.py

M+28.27 — the objective -> requirements boundary.

Forensic finding this module addresses: DefinitionSemanticIR (M+28.19-23)
tokenizes an entire piece of text as ONE flat, undifferentiated sequence —
content_links treats "readings -> calculate" as just another adjacent-word
link, identical in kind to "normalize -> collection". There is nothing in
that machinery that recognizes a multi-clause objective contains several
separate things to do. This module adds exactly one thing upstream of it:
generic structural clause segmentation, so each candidate clause can be
fed through the EXISTING, already-proven single-concept IR pipeline
unmodified.

The segmentation is deliberately dumb and purely structural: it splits on
coordination markers (commas, standalone "and"/"then"/"and then") without
ever inspecting what a clause is ABOUT. It does not know or care whether a
clause says "normalize the readings" or "bake a cake" — the same split
points fire either way. This is the difference between a phrase->
requirement table (forbidden: deciding a clause's MEANING from its
wording) and a generic clause tokenizer (a structural preprocessing step,
the same kind of thing a sentence splitter is to a document).

Each resulting requirement is deliberately NON-EXECUTABLE: it carries the
clause's own verbatim text as its description, a semantic_basis pointing
at the IR built for it, a confidence derived from how much content the IR
actually found (not from recognizing what the content means), and an
explicit status — DISCOVERED / AMBIGUOUS / UNKNOWN — so uncertain
segmentation is represented honestly rather than silently promoted to a
fact.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List

from swarm_engine.acquisition.lexical import (
    Lexicon, build_definition_ir, ir_to_semantic_structure,
)
from swarm_engine.acquisition.semantic_structure import SemanticStructureStore
from swarm_engine.acquisition.behavioral_grounding import extract_behavioral_obligation


# Generic coordination markers only — these fire on their STRUCTURAL
# position (standalone words, or commas) regardless of the vocabulary
# around them. Nothing here is specific to any domain or test objective.
_COORDINATORS = re.compile(r"\band then\b|\bthen\b|\band\b", re.IGNORECASE)


def _stable_id(prefix: str, text: str) -> str:
    h = hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
    return f"{prefix}_{h}"


class RequirementStatus(Enum):
    DISCOVERED = "discovered"
    HYPOTHESIZED = "hypothesized"
    AMBIGUOUS = "ambiguous"
    UNKNOWN = "unknown"


@dataclass
class DiscoveredRequirement:
    requirement_id: str
    semantic_basis: str            # ir_id from build_definition_ir
    # Existing non-executable SemanticStructure projected from semantic_basis.
    # It preserves inspectable lexical/unknown/cardinality structure without
    # claiming an operation, invariant, or executable meaning.
    semantic_structure_id: str
    description: str               # the clause's own verbatim text
    status: RequirementStatus
    confidence: float
    provenance: Dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> Dict[str, Any]:
        return {"requirement_id": self.requirement_id,
                "semantic_basis": self.semantic_basis,
                "semantic_structure_id": self.semantic_structure_id,
                "description": self.description,
                "status": self.status.value,
                "confidence": round(self.confidence, 3),
                "provenance": self.provenance}


@dataclass
class ObjectiveDecomposition:
    objective_id: str
    source_text: str
    requirements: List[DiscoveredRequirement]
    provenance: Dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> Dict[str, Any]:
        return {"objective_id": self.objective_id, "source_text": self.source_text,
                "requirements": [r.as_dict() for r in self.requirements],
                "provenance": self.provenance}


def _split_clauses(text: str) -> List[str]:
    """Purely structural segmentation on coordination markers. Never
    inspects clause content — the same split points fire for any
    objective, in any domain, regardless of what the clauses mean.

    Commas and coordinators inside parentheses/brackets/braces are not
    split points (parameter lists, nested clauses). This is structural
    nesting awareness, not domain semantics.
    """
    def _top_level_chunks(s: str) -> List[str]:
        """Split on commas and coordinator words only at paren-depth 0."""
        chunks: List[str] = []
        buf: List[str] = []
        depth = 0
        i = 0
        lower = s.lower()
        while i < len(s):
            ch = s[i]
            if ch in "([{":
                depth += 1
                buf.append(ch)
                i += 1
                continue
            if ch in ")]}":
                depth = max(0, depth - 1)
                buf.append(ch)
                i += 1
                continue
            if depth == 0 and ch == ",":
                piece = "".join(buf).strip().strip(".")
                if piece:
                    chunks.append(piece)
                buf = []
                i += 1
                continue
            if depth == 0:
                matched = None
                for token in (" and then ", " then ", " and "):
                    if lower[i:i + len(token)] == token:
                        matched = token
                        break
                if matched:
                    piece = "".join(buf).strip().strip(".")
                    if piece:
                        chunks.append(piece)
                    buf = []
                    i += len(matched)
                    continue
            buf.append(ch)
            i += 1
        piece = "".join(buf).strip().strip(".")
        if piece:
            chunks.append(piece)
        return chunks

    return _top_level_chunks(text or "")


def decompose_objective(lex: Lexicon, objective_text: str) -> ObjectiveDecomposition:
    """objective text -> multiple non-executable DiscoveredRequirements.

    Reuses build_definition_ir (M+28.19-23, unmodified) for each candidate
    clause rather than inventing a second NLP subsystem. Confidence and
    status come from how much actual semantic content the IR found for
    that clause (content-word count, resolved concepts) — never from
    recognizing what the clause is about.
    """
    clauses = _split_clauses(objective_text)
    requirements: List[DiscoveredRequirement] = []
    # The structure store is the existing canonical persistence path. Pure
    # in-memory lexicons still receive an inspectable identity but do not
    # silently create a separate transient database.
    structure_store = SemanticStructureStore(lex.db_path) if lex.db_path else None
    for i, clause_text in enumerate(clauses):
        concept_name = _stable_id("objclause", f"{objective_text}::{i}::{clause_text}")
        concept = lex.register([concept_name], clause_text)
        ir = build_definition_ir(lex, concept)
        structure = ir_to_semantic_structure(ir)
        if structure_store is not None:
            structure_store.store(structure)
        content_signal = len(ir.unknown_surfaces) + len(ir.resolved_concept_ids)
        # M+29.31: a real, syntactic obligation signal -- distinguishes
        # a genuine behavioral requirement ("the character must jump")
        # from a lexically-dense but obligation-free fragment (a
        # heading, a bare list) that the pre-existing content_signal
        # alone cannot tell apart, since it only counts recognized
        # words regardless of whether they impose any obligation.
        obligation = extract_behavioral_obligation(clause_text)
        # A clause with almost no real content (e.g. a stray fragment left
        # over from a coordination split that shouldn't have fired) gets
        # an honest low-confidence / ambiguous status rather than being
        # silently treated as equally real as a substantive clause.
        if content_signal == 0:
            status, confidence = RequirementStatus.UNKNOWN, 0.0
        elif content_signal <= 1:
            status, confidence = RequirementStatus.AMBIGUOUS, 0.3
        elif content_signal <= 2:
            status, confidence = RequirementStatus.HYPOTHESIZED, 0.6
        else:
            status, confidence = RequirementStatus.DISCOVERED, min(1.0, content_signal / 4.0)
        # A word-dense clause is no longer reported as confidently
        # DISCOVERED unless it also contains a genuine, syntactically
        # detected obligation -- content_signal alone was the exact
        # defect the SpellBrook audit found (a comma-split heading
        # fragment scored confidence=1.00). This never upgrades a
        # clause's status, only downgrades one that lexical density
        # alone had inflated.
        if obligation is None and status == RequirementStatus.DISCOVERED:
            status, confidence = RequirementStatus.HYPOTHESIZED, min(confidence, 0.5)
        elif obligation is not None and obligation.ambiguous and status == RequirementStatus.DISCOVERED:
            status, confidence = RequirementStatus.AMBIGUOUS, min(confidence, 0.4)
        req_provenance: Dict[str, Any] = {}
        if obligation is not None:
            req_provenance["behavioral_obligation"] = obligation.as_dict()
        requirements.append(DiscoveredRequirement(
            requirement_id=_stable_id("req", clause_text),
            semantic_basis=ir.ir_id,
            semantic_structure_id=structure.structure_id,
            description=clause_text,
            status=status,
            confidence=confidence,
            provenance={"objective_text": objective_text, "clause_index": i,
                        "split_method": "structural_coordination",
                        "content_signal": content_signal, **req_provenance}))
    return ObjectiveDecomposition(
        objective_id=_stable_id("obj", objective_text),
        source_text=objective_text,
        requirements=requirements,
        provenance={"builder": "decompose_objective.v1"})
