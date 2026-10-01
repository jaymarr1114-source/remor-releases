"""
M+28.16 — Lexical concepts and definition-derived compositional structures.

Non-executable. No phrase→operation maps. No LLM.

Definition → structure mechanism:
  1. Register LexicalConcept with definition text + surface forms.
  2. Tokenize definition (whitespace / simple punctuation split).
  3. Match tokens against known surface forms in the lexicon.
  4. Build SemanticStructure: center concept + MENTIONS edges to resolved
     concepts; UNKNOWN nodes for unresolved tokens (content words only).
  5. Composition: expand MENTIONS recursively (bounded) into a larger graph.

This deliberately does NOT interpret "same" as SAME-relation semantics or
"split" as a math op. Mention links are definitional co-occurrence structure only.
"""
from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

from swarm_engine.acquisition.semantic_structure import (
    SemanticStructure, SemanticNode, SemanticEdge, build_structure,
)


_STOP = {
    "a", "an", "the", "of", "to", "for", "and", "or", "in", "on", "with",
    "from", "into", "by", "as", "is", "are", "be", "being", "been", "that",
    "this", "it", "its", "at", "or", "which", "who", "whom", "whose",
}


@dataclass
class LexicalConcept:
    concept_id: str
    surface_forms: Tuple[str, ...]
    definition: str
    provenance: Dict[str, Any] = field(default_factory=dict)
    version: int = 1
    created_at: float = field(default_factory=time.time)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "concept_id": self.concept_id,
            "surface_forms": list(self.surface_forms),
            "definition": self.definition,
            "provenance": dict(self.provenance),
            "version": self.version,
            "created_at": self.created_at,
        }

    @staticmethod
    def from_dict(d: Dict[str, Any]) -> "LexicalConcept":
        return LexicalConcept(
            concept_id=str(d["concept_id"]),
            surface_forms=tuple(d.get("surface_forms") or ()),
            definition=str(d.get("definition") or ""),
            provenance=dict(d.get("provenance") or {}),
            version=int(d.get("version") or 1),
            created_at=float(d.get("created_at") or time.time()),
        )


def _tokenize(text: str) -> List[str]:
    return [t for t in re.findall(r"[A-Za-z0-9]+", text.lower()) if t]


def _cid(prefix: str, *parts: Any) -> str:
    blob = json.dumps(parts, sort_keys=True, default=str)
    return prefix + hashlib.sha256(blob.encode()).hexdigest()[:16]


class Lexicon:
    """In-memory + optional SQLite lexicon of LexicalConcept entries."""

    def __init__(self, db_path: Optional[str] = None):
        self.db_path = db_path
        self._concepts: Dict[str, LexicalConcept] = {}
        self._surface_index: Dict[str, str] = {}  # surface -> concept_id
        if db_path:
            self._init_db()
            self._load()

    def _conn(self):
        import sqlite3
        return sqlite3.connect(self.db_path)

    def _init_db(self) -> None:
        with self._conn() as c:
            c.execute(
                """CREATE TABLE IF NOT EXISTS lexical_concepts (
                    concept_id TEXT PRIMARY KEY,
                    body_json TEXT NOT NULL
                )"""
            )

    def _load(self) -> None:
        if not self.db_path:
            return
        with self._conn() as c:
            rows = c.execute("SELECT body_json FROM lexical_concepts").fetchall()
        for (body,) in rows:
            lc = LexicalConcept.from_dict(json.loads(body))
            self._index(lc)

    def _index(self, lc: LexicalConcept) -> None:
        self._concepts[lc.concept_id] = lc
        for s in lc.surface_forms:
            self._surface_index[s.lower()] = lc.concept_id

    def register(self, surface_forms: Sequence[str], definition: str,
                 concept_id: Optional[str] = None,
                 provenance: Optional[Dict[str, Any]] = None) -> LexicalConcept:
        forms = tuple(dict.fromkeys(s.lower().strip() for s in surface_forms if s.strip()))
        if not forms:
            raise ValueError("surface_forms required")
        cid = concept_id or _cid("lex_", forms, definition)
        lc = LexicalConcept(
            concept_id=cid,
            surface_forms=forms,
            definition=definition,
            provenance=dict(provenance or {"source": "register"}),
        )
        self._index(lc)
        if self.db_path:
            with self._conn() as c:
                c.execute(
                    "INSERT OR REPLACE INTO lexical_concepts (concept_id, body_json) VALUES (?,?)",
                    (lc.concept_id, json.dumps(lc.as_dict())),
                )
        return lc

    def get(self, concept_id: str) -> Optional[LexicalConcept]:
        return self._concepts.get(concept_id)

    def resolve_surface(self, token: str) -> Optional[LexicalConcept]:
        cid = self._surface_index.get(token.lower())
        return self._concepts.get(cid) if cid else None

    def all_ids(self) -> List[str]:
        return list(self._concepts.keys())


def definition_to_structure(lexicon: Lexicon, concept: LexicalConcept) -> SemanticStructure:
    """Build a non-executable SemanticStructure from a concept's definition text.

    Mechanism: tokenize definition; match known surface forms → MENTIONS edges;
    unknown content tokens → UNKNOWN nodes with deferred status.
    Does not assign executable ops or synonym-equals semantics.
    """
    tokens = _tokenize(concept.definition)
    nodes: List[Dict[str, Any]] = [
        {"node_id": "self", "role": "LEXICAL_CONCEPT",
         "attributes": {"concept_id": concept.concept_id, "surface": concept.surface_forms[0]}},
    ]
    edges: List[Dict[str, Any]] = []
    seen_resolved: Set[str] = set()
    unknown_i = 0
    for tok in tokens:
        if tok in _STOP:
            continue
        if tok in concept.surface_forms:
            continue  # self-mention
        resolved = lexicon.resolve_surface(tok)
        if resolved and resolved.concept_id != concept.concept_id:
            nid = f"ref_{resolved.concept_id[:12]}"
            if nid not in seen_resolved:
                seen_resolved.add(nid)
                nodes.append({
                    "node_id": nid,
                    "role": "LEXICAL_CONCEPT",
                    "attributes": {
                        "concept_id": resolved.concept_id,
                        "surface": resolved.surface_forms[0],
                    },
                })
                edges.append({
                    "source": "self",
                    "relation": "MENTIONS",
                    "target": nid,
                })
        else:
            # unknown content word
            if len(tok) < 2:
                continue
            nid = f"unk_{unknown_i}"
            unknown_i += 1
            nodes.append({
                "node_id": nid,
                "role": "UNKNOWN",
                "attributes": {"token": tok, "status": "unresolved"},
            })
            edges.append({
                "source": "self",
                "relation": "MENTIONS_UNKNOWN",
                "target": nid,
            })
    return build_structure(
        nodes=nodes,
        edges=edges,
        evidence_ids=[f"def:{concept.concept_id}"],
        provenance={
            "builder": "definition_to_structure.v1",
            "lexical_concept_id": concept.concept_id,
            "definition_span": concept.definition[:200],
        },
        structure_id=_cid("ds_", concept.concept_id, concept.definition),
    )


# ---------------------------------------------------------------------------
# M+28.17 — Definitional relation induction (non-executable)
# ---------------------------------------------------------------------------
# Pattern vocabulary is closed-class structural cues (same/different/part of),
# not concept→operation maps. Patterns apply to ANY head concept.
# Developer-authored pattern inventory — not autonomous invention of English.

_RELATION_PATTERNS = (
    # (trigger_tokens, relation_to_following_resolved_concept, optional_property_role)
    # Closed-class / structural cues only — never concept→executable-op maps.
    (("same",), "SAME_AS", "SAME"),
    (("identical",), "SAME_AS", "SAME"),
    (("different",), "DIFFERENT_FROM", "DIFFERENT"),
    (("distinct",), "DIFFERENT_FROM", "DIFFERENT"),
    (("part", "of"), "PART_OF", None),
    (("portion", "of"), "PART_OF", None),
    (("made", "of"), "MADE_OF", None),
    (("composed", "of"), "MADE_OF", None),
    (("used", "for"), "PURPOSE", None),
    (("intended", "for"), "PURPOSE", None),
    (("located", "in"), "LOCATED_IN", None),
    (("found", "in"), "LOCATED_IN", None),
    (("caused", "by"), "CAUSED_BY", None),
    (("consists", "of"), "CONTAINS", None),
    (("contains",), "CONTAINS", None),
    (("includes",), "CONTAINS", None),
    # Relational cues for diet / appearance (closed-class triggers; target
    # may be unresolved content — promoted below when no lexicon hit).
    (("feeds", "on"), "FEEDS_ON", None),
    (("feeds", "entirely", "on"), "FEEDS_ON", None),
    (("feed", "on"), "FEEDS_ON", None),
    (("eats",), "FEEDS_ON", None),
    (("resembling",), "RESEMBLES", None),
    (("resembles",), "RESEMBLES", None),
    (("like", "a"), "RESEMBLES", None),
    (("also", "called"), "ALSO_CALLED", None),
)


def induce_definition_structure(
        lexicon: "Lexicon",
        concept: LexicalConcept,
) -> SemanticStructure:
    """Induce non-executable semantic relations from a definition.

    1. Start from definition_to_structure (MENTIONS / UNKNOWN).
    2. Scan token windows for closed-class structural patterns.
    3. When a pattern trigger is followed by a *resolved* lexicon concept,
       emit a typed relation edge (SAME_AS, DIFFERENT_FROM, PART_OF, …).
    4. Optionally attach an abstract property node (SAME / DIFFERENT).

    Never emits arithmetic operator names or registry ops.
    Unknown following tokens do not invent concepts.
    """
    base = definition_to_structure(lexicon, concept)
    tokens = _tokenize(concept.definition)

    nodes = [n.as_dict() for n in base.nodes]
    edges = [e.as_dict() for e in base.edges]
    existing_ids = {n["node_id"] for n in nodes}

    def _ensure_ref(resolved: LexicalConcept) -> str:
        nid = f"ref_{resolved.concept_id[:12]}"
        if nid not in existing_ids:
            nodes.append({
                "node_id": nid,
                "role": "LEXICAL_CONCEPT",
                "attributes": {
                    "concept_id": resolved.concept_id,
                    "surface": resolved.surface_forms[0],
                },
            })
            existing_ids.add(nid)
        return nid

    def _ensure_property(role: str) -> str:
        nid = f"prop_{role.lower()}"
        if nid not in existing_ids:
            nodes.append({
                "node_id": nid,
                "role": "PROPERTY",
                "attributes": {"property": role},
            })
            existing_ids.add(nid)
        return nid

    i = 0
    while i < len(tokens):
        matched = False
        for trigger, rel, prop in _RELATION_PATTERNS:
            tlen = len(trigger)
            if tokens[i:i + tlen] != list(trigger):
                continue
            # look ahead for next non-stop resolved concept
            j = i + tlen
            while j < len(tokens) and tokens[j] in _STOP:
                j += 1
            if j >= len(tokens):
                matched = True
                i = j
                break
            tok = tokens[j]
            resolved = lexicon.resolve_surface(tok)
            if resolved and resolved.concept_id != concept.concept_id:
                tgt = _ensure_ref(resolved)
                edges.append({
                    "source": "self",
                    "relation": rel,
                    "target": tgt,
                })
                if prop:
                    pid = _ensure_property(prop)
                    edges.append({
                        "source": "self",
                        "relation": "HAS_PROPERTY",
                        "target": pid,
                    })
            else:
                # Promote unresolved content token as a typed relation target
                # (entity token, not a new invented lexicon concept). Enables
                # FEEDS_ON / RESEMBLES etc. when the object is not pre-registered.
                nid = f"tok_{rel.lower()}_{j}_{tok}"[:40]
                if nid not in existing_ids:
                    nodes.append({
                        "node_id": nid,
                        "role": "CONTENT",
                        "attributes": {"token": tok, "status": "unresolved"},
                    })
                    existing_ids.add(nid)
                edges.append({
                    "source": "self",
                    "relation": rel,
                    "target": nid,
                })
            matched = True
            i = j + 1
            break
        if not matched:
            i += 1

    return build_structure(
        nodes=nodes,
        edges=edges,
        evidence_ids=[f"def:{concept.concept_id}", f"induce:{concept.concept_id}"],
        provenance={
            "builder": "induce_definition_structure.v2",
            "lexical_concept_id": concept.concept_id,
            "definition_span": concept.definition[:200],
            "parent_builder": "definition_to_structure.v1",
            "pattern_inventory": "closed_class.v1",
        },
        structure_id=_cid("ind_", concept.concept_id, concept.definition),
    )


# ---------------------------------------------------------------------------
# M+28.21 — Generalized Definition Semantic IR (non-executable)
# ---------------------------------------------------------------------------
# This is NOT an expanded phrase→relation table.
# It annotates definition tokens and builds a compositional IR of:
#   HEAD, RESOLVED, UNKNOWN, NUMERIC, CONTENT_LINK, DEFINES_BODY, CARDINALITY
# Typed SAME_AS/etc. remain optional via induce_definition_structure (closed-class).

@dataclass(frozen=True)
class DefinitionToken:
    index: int
    surface: str
    kind: str  # STOP | RESOLVED | UNKNOWN | NUMERIC | PUNCT
    concept_id: Optional[str] = None
    value: Optional[Any] = None

    def as_dict(self) -> Dict[str, Any]:
        return {
            "index": self.index,
            "surface": self.surface,
            "kind": self.kind,
            "concept_id": self.concept_id,
            "value": self.value,
        }


@dataclass
class DefinitionSemanticIR:
    """General semantic IR for a definition — independent of executable ops."""
    ir_id: str
    head_concept_id: str
    tokens: Tuple[DefinitionToken, ...]
    content_links: Tuple[Tuple[int, int], ...]  # adjacent content token indices
    numeric_values: Tuple[Any, ...]
    resolved_concept_ids: Tuple[str, ...]
    unknown_surfaces: Tuple[str, ...]
    definition_text: str
    provenance: Dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "ir_id": self.ir_id,
            "head_concept_id": self.head_concept_id,
            "tokens": [t.as_dict() for t in self.tokens],
            "content_links": [list(p) for p in self.content_links],
            "numeric_values": list(self.numeric_values),
            "resolved_concept_ids": list(self.resolved_concept_ids),
            "unknown_surfaces": list(self.unknown_surfaces),
            "definition_text": self.definition_text,
            "provenance": dict(self.provenance),
        }

    def fingerprint(self) -> str:
        payload = {
            "head": self.head_concept_id,
            "tokens": [(t.surface, t.kind, t.concept_id, t.value) for t in self.tokens],
            "links": list(self.content_links),
        }
        blob = json.dumps(payload, sort_keys=True, default=str)
        return "ir_" + hashlib.sha256(blob.encode()).hexdigest()[:20]


def build_definition_ir(lexicon: "Lexicon", concept: LexicalConcept) -> DefinitionSemanticIR:
    """Build generalized IR from definition text + lexicon resolution.

    No phrase→relation typing here. Content words are RESOLVED or UNKNOWN.
    Numerics become CARDINALITY values. Adjacent content words form CONTENT_LINK.
    """
    raw_tokens = _tokenize(concept.definition)
    annotated: List[DefinitionToken] = []
    for i, tok in enumerate(raw_tokens):
        if tok in _STOP:
            annotated.append(DefinitionToken(i, tok, "STOP"))
            continue
        if tok.isdigit() or (tok.startswith("-") and tok[1:].isdigit()):
            annotated.append(DefinitionToken(i, tok, "NUMERIC", value=int(tok)))
            continue
        try:
            fv = float(tok)
            annotated.append(DefinitionToken(i, tok, "NUMERIC", value=fv))
            continue
        except ValueError:
            pass
        resolved = lexicon.resolve_surface(tok)
        if resolved and resolved.concept_id != concept.concept_id:
            annotated.append(DefinitionToken(
                i, tok, "RESOLVED", concept_id=resolved.concept_id
            ))
        elif tok in concept.surface_forms:
            annotated.append(DefinitionToken(
                i, tok, "RESOLVED", concept_id=concept.concept_id
            ))
        else:
            annotated.append(DefinitionToken(i, tok, "UNKNOWN"))

    content_idx = [t.index for t in annotated if t.kind in ("RESOLVED", "UNKNOWN", "NUMERIC")]
    links = tuple((content_idx[i], content_idx[i + 1]) for i in range(len(content_idx) - 1))
    nums = tuple(t.value for t in annotated if t.kind == "NUMERIC" and t.value is not None)
    resolved_ids = tuple(dict.fromkeys(
        t.concept_id for t in annotated if t.kind == "RESOLVED" and t.concept_id
    ))
    unknowns = tuple(t.surface for t in annotated if t.kind == "UNKNOWN")

    return DefinitionSemanticIR(
        ir_id=_cid("ir_", concept.concept_id, concept.definition),
        head_concept_id=concept.concept_id,
        tokens=tuple(annotated),
        content_links=links,
        numeric_values=nums,
        resolved_concept_ids=resolved_ids,
        unknown_surfaces=unknowns,
        definition_text=concept.definition,
        provenance={
            "builder": "build_definition_ir.v1",
            "lexical_concept_id": concept.concept_id,
            "definition_span": concept.definition[:200],
        },
    )


def ir_to_semantic_structure(ir: DefinitionSemanticIR) -> SemanticStructure:
    """Project DefinitionSemanticIR into SemanticStructure (non-executable).

    Relations used: DEFINES_BODY (head→resolved/unknown), CONTENT_LINK,
    CARDINALITY (head→numeric node). No SAME_AS/PART_OF from this path.
    """
    nodes: List[Dict[str, Any]] = [
        {"node_id": "head", "role": "LEXICAL_CONCEPT",
         "attributes": {"concept_id": ir.head_concept_id}},
    ]
    edges: List[Dict[str, Any]] = []
    # token nodes
    for t in ir.tokens:
        if t.kind == "STOP":
            continue
        nid = f"t{t.index}"
        if t.kind == "RESOLVED":
            nodes.append({
                "node_id": nid,
                "role": "LEXICAL_CONCEPT",
                "attributes": {"concept_id": t.concept_id, "surface": t.surface},
            })
            edges.append({"source": "head", "relation": "DEFINES_BODY", "target": nid})
        elif t.kind == "UNKNOWN":
            nodes.append({
                "node_id": nid,
                "role": "UNKNOWN",
                "attributes": {"token": t.surface, "status": "unresolved"},
            })
            edges.append({"source": "head", "relation": "DEFINES_BODY", "target": nid})
        elif t.kind == "NUMERIC":
            nodes.append({
                "node_id": nid,
                "role": "CARDINALITY",
                "attributes": {"n": t.value},
            })
            edges.append({"source": "head", "relation": "CARDINALITY", "target": nid})
    # content adjacency
    for a, b in ir.content_links:
        edges.append({
            "source": f"t{a}",
            "relation": "CONTENT_LINK",
            "target": f"t{b}",
        })
    # ensure edge endpoints exist
    ids = {n["node_id"] for n in nodes}
    edges = [e for e in edges if e["source"] in ids and e["target"] in ids]
    return build_structure(
        nodes=nodes,
        edges=edges,
        evidence_ids=[f"ir:{ir.ir_id}"],
        provenance={
            "builder": "ir_to_semantic_structure.v1",
            "ir_id": ir.ir_id,
            "ir_fingerprint": ir.fingerprint(),
            "lexical_concept_id": ir.head_concept_id,
            "definition_span": ir.definition_text[:200],
        },
        structure_id=_cid("irs_", ir.ir_id),
    )


# ---------------------------------------------------------------------------
# M+28.22 — Evidence-driven relation hypotheses over DefinitionSemanticIR
# ---------------------------------------------------------------------------
# Generates CANDIDATE relations from compositional IR evidence only.
# Does NOT consult _RELATION_PATTERNS / phrase tables.
# Does NOT assert typed SAME_AS/PART_OF without structural evidence of that type.
# Default candidates: DEFINES_BODY-supported RELATED_TO, CONTENT_LINK CO_OCCURS,
# CARDINALITY HAS_CARDINALITY. Evaluation checks structural support, not a label oracle.

@dataclass(frozen=True)
class SemanticRelationHypothesis:
    hypothesis_id: str
    source: str
    target: str
    relation_type_candidate: str
    supporting_structure_ids: Tuple[str, ...]
    supporting_node_ids: Tuple[str, ...]
    derivation: str
    confidence: str  # "supported" | "weak" | "unknown" | "contradicted"
    provenance: Tuple[Tuple[str, Any], ...] = ()

    def as_dict(self) -> Dict[str, Any]:
        return {
            "hypothesis_id": self.hypothesis_id,
            "source": self.source,
            "target": self.target,
            "relation_type_candidate": self.relation_type_candidate,
            "supporting_structure_ids": list(self.supporting_structure_ids),
            "supporting_node_ids": list(self.supporting_node_ids),
            "derivation": self.derivation,
            "confidence": self.confidence,
            "provenance": dict(self.provenance),
        }


def generate_relation_hypotheses(
        ir: DefinitionSemanticIR,
        ss: Optional[SemanticStructure] = None,
) -> List[SemanticRelationHypothesis]:
    """Propose relation hypotheses from IR compositional evidence.

    Evidence sources:
      - DEFINES_BODY links (head → body tokens) → RELATED_TO
      - CONTENT_LINK pairs → CO_OCCURS
      - NUMERIC/CARDINALITY → HAS_CARDINALITY
    No phrase→relation lookup. No SAME_AS from the word "same".
    """
    if ss is None:
        ss = ir_to_semantic_structure(ir)
    out: List[SemanticRelationHypothesis] = []

    def _hid(*parts) -> str:
        return _cid("rh_", *parts)

    # DEFINES_BODY → RELATED_TO
    for e in ss.edges:
        if e.relation != "DEFINES_BODY":
            continue
        tgt = next((n for n in ss.nodes if n.node_id == e.target), None)
        if not tgt:
            continue
        attrs = dict(tgt.attributes)
        target_label = attrs.get("concept_id") or attrs.get("token") or e.target
        conf = "supported" if tgt.role in ("LEXICAL_CONCEPT", "UNKNOWN") else "weak"
        out.append(SemanticRelationHypothesis(
            hypothesis_id=_hid("related", e.source, target_label, ir.ir_id),
            source=ir.head_concept_id,
            target=str(target_label),
            relation_type_candidate="RELATED_TO",
            supporting_structure_ids=(ss.structure_id, ir.ir_id),
            supporting_node_ids=(e.source, e.target),
            derivation="defines_body.v1",
            confidence=conf,
            provenance=(
                ("builder", "generate_relation_hypotheses.v1"),
                ("evidence", "DEFINES_BODY"),
            ),
        ))

    # CONTENT_LINK → CO_OCCURS
    for e in ss.edges:
        if e.relation != "CONTENT_LINK":
            continue
        out.append(SemanticRelationHypothesis(
            hypothesis_id=_hid("co", e.source, e.target, ir.ir_id),
            source=e.source,
            target=e.target,
            relation_type_candidate="CO_OCCURS",
            supporting_structure_ids=(ss.structure_id, ir.ir_id),
            supporting_node_ids=(e.source, e.target),
            derivation="content_link.v1",
            confidence="supported",
            provenance=(
                ("builder", "generate_relation_hypotheses.v1"),
                ("evidence", "CONTENT_LINK"),
            ),
        ))

    # CARDINALITY
    for e in ss.edges:
        if e.relation != "CARDINALITY":
            continue
        tgt = next((n for n in ss.nodes if n.node_id == e.target), None)
        nval = dict(tgt.attributes).get("n") if tgt else None
        out.append(SemanticRelationHypothesis(
            hypothesis_id=_hid("card", ir.head_concept_id, nval, ir.ir_id),
            source=ir.head_concept_id,
            target=str(nval),
            relation_type_candidate="HAS_CARDINALITY",
            supporting_structure_ids=(ss.structure_id, ir.ir_id),
            supporting_node_ids=(e.source, e.target),
            derivation="cardinality.v1",
            confidence="supported" if nval is not None else "unknown",
            provenance=(
                ("builder", "generate_relation_hypotheses.v1"),
                ("evidence", "CARDINALITY"),
            ),
        ))

    # Insufficient IR → no typed SAME_AS/PART_OF hypotheses (honest underdetermination)
    return out


def evaluate_relation_hypothesis(
        hyp: SemanticRelationHypothesis,
        ir: DefinitionSemanticIR,
        ss: Optional[SemanticStructure] = None,
) -> Dict[str, Any]:
    """Independent structural evaluation — does NOT use expected labels from tests.

    Accepts hypothesis if supporting edges exist in the projected structure.
    Rejects if claimed support nodes/edges are absent.
    Never invents SAME_AS acceptance from lexical cues.
    """
    if ss is None:
        ss = ir_to_semantic_structure(ir)
    edge_set = {(e.source, e.relation, e.target) for e in ss.edges}
    node_ids = {n.node_id for n in ss.nodes}

    ok = True
    reasons: List[str] = []

    for nid in hyp.supporting_node_ids:
        if nid not in node_ids:
            ok = False
            reasons.append(f"missing node {nid}")

    if hyp.derivation == "defines_body.v1":
        if not any(e.relation == "DEFINES_BODY" for e in ss.edges):
            ok = False
            reasons.append("no DEFINES_BODY evidence")
        else:
            reasons.append("DEFINES_BODY evidence present")
    elif hyp.derivation == "content_link.v1":
        if (hyp.source, "CONTENT_LINK", hyp.target) not in edge_set and \
           (hyp.target, "CONTENT_LINK", hyp.source) not in edge_set:
            ok = False
            reasons.append("CONTENT_LINK missing")
        else:
            reasons.append("CONTENT_LINK evidence present")
    elif hyp.derivation == "cardinality.v1":
        if not any(e.relation == "CARDINALITY" for e in ss.edges):
            ok = False
            reasons.append("no CARDINALITY evidence")
        else:
            reasons.append("CARDINALITY evidence present")
    else:
        reasons.append("unknown derivation — not auto-accepted")
        ok = False

    # Explicit: never accept SAME_AS/PART_OF from this evaluator without edge proof
    if hyp.relation_type_candidate in ("SAME_AS", "PART_OF", "DIFFERENT_FROM"):
        if not any(e.relation == hyp.relation_type_candidate for e in ss.edges):
            ok = False
            reasons.append(
                f"{hyp.relation_type_candidate} not evidenced in structure (underdetermined)"
            )

    return {
        "accepted": ok,
        "hypothesis_id": hyp.hypothesis_id,
        "relation_type_candidate": hyp.relation_type_candidate,
        "reasons": reasons,
        "evaluator": "evaluate_relation_hypothesis.v1",
    }
