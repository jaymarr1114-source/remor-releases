"""
M+28.23 — Semantic reasoning → capability-gap bridge.

Maps non-executable semantic requirements to inventory comparison without
phrase→capability tables or benchmark answer injection.
"""
from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

from swarm_engine.acquisition.pipeline import CapabilityRequirement, CapabilityGap


@dataclass
class SemanticCapabilityRequirement:
    """Requirement derived from semantic IR / structure, not from phrase maps."""
    requirement_id: str
    semantic_basis: str  # e.g. ir_id or structure_id
    tokens: Tuple[str, ...]  # resolved surfaces + unknown tokens as probes
    numeric_values: Tuple[Any, ...] = ()
    relation_hypotheses: Tuple[str, ...] = ()
    confidence: str = "unknown"  # high | partial | unknown
    provenance: Dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "requirement_id": self.requirement_id,
            "semantic_basis": self.semantic_basis,
            "tokens": list(self.tokens),
            "numeric_values": list(self.numeric_values),
            "relation_hypotheses": list(self.relation_hypotheses),
            "confidence": self.confidence,
            "provenance": dict(self.provenance),
        }


@dataclass
class CapabilityMatchResult:
    status: str  # AVAILABLE | PARTIAL | MISSING | UNKNOWN
    requirement_id: str
    matched_capabilities: Tuple[str, ...]
    partial_capabilities: Tuple[str, ...]
    missing_tokens: Tuple[str, ...]
    evidence: Dict[str, Any] = field(default_factory=dict)
    provenance: Dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "status": self.status,
            "requirement_id": self.requirement_id,
            "matched_capabilities": list(self.matched_capabilities),
            "partial_capabilities": list(self.partial_capabilities),
            "missing_tokens": list(self.missing_tokens),
            "evidence": dict(self.evidence),
            "provenance": dict(self.provenance),
        }


def _rid(*parts: Any) -> str:
    blob = json.dumps(parts, sort_keys=True, default=str)
    return "req_" + hashlib.sha256(blob.encode()).hexdigest()[:16]


def requirement_from_ir(ir: Any, hyps: Optional[Sequence[Any]] = None) -> SemanticCapabilityRequirement:
    """Build requirement tokens from DefinitionSemanticIR + optional relation hyps.

    Tokens are surfaces/concept ids present in the IR — not a phrase→capability map.
    """
    tokens: List[str] = []
    # resolved concept surfaces from tokens
    for t in getattr(ir, "tokens", ()) or ():
        if getattr(t, "kind", None) == "RESOLVED" and t.surface:
            tokens.append(str(t.surface).lower())
        elif getattr(t, "kind", None) == "UNKNOWN" and t.surface:
            tokens.append(str(t.surface).lower())
    # also resolved_concept_ids short forms
    for cid in getattr(ir, "resolved_concept_ids", ()) or ():
        tokens.append(str(cid).lower()[:24])
    # head surface if available in provenance
    tokens = list(dict.fromkeys(tokens))
    hyp_types = tuple(
        getattr(h, "relation_type_candidate", str(h)) for h in (hyps or ())
    )
    conf = "high" if tokens else "unknown"
    if tokens and any(getattr(t, "kind", None) == "UNKNOWN" for t in getattr(ir, "tokens", ())):
        conf = "partial"
    return SemanticCapabilityRequirement(
        requirement_id=_rid(getattr(ir, "ir_id", "ir"), tokens),
        semantic_basis=str(getattr(ir, "ir_id", "")),
        tokens=tuple(tokens),
        numeric_values=tuple(getattr(ir, "numeric_values", ()) or ()),
        relation_hypotheses=hyp_types,
        confidence=conf,
        provenance={
            "builder": "requirement_from_ir.v1",
            "head_concept_id": getattr(ir, "head_concept_id", None),
            "definition_span": (getattr(ir, "definition_text", "") or "")[:120],
        },
    )


def _inventory_entries(registry: Any) -> List[Dict[str, str]]:
    """Collect name/description/keyword strings from primitive registry."""
    entries = []
    names = []
    if hasattr(registry, "names"):
        names = list(registry.names())
    elif hasattr(registry, "all"):
        names = list(registry.all())
    elif hasattr(registry, "_primitives"):
        names = list(getattr(registry, "_primitives", {}).keys())
    for name in names:
        desc = ""
        try:
            prim = registry.get(name) if hasattr(registry, "get") else None
            if prim is not None:
                desc = str(getattr(prim, "description", "") or getattr(prim, "doc", "") or "")
        except Exception:
            pass
        blob = f"{name} {desc}".lower()
        entries.append({"name": name, "blob": blob})
    return entries


def match_requirement_to_inventory(
        req: SemanticCapabilityRequirement,
        registry: Any,
) -> CapabilityMatchResult:
    """Compare semantic requirement tokens against capability inventory.

    Matching is token-in-capability-metadata (structural), not a fixed
    phrase→capability table. Empty tokens → UNKNOWN.
    """
    if not req.tokens or req.confidence == "unknown" and not req.tokens:
        return CapabilityMatchResult(
            status="UNKNOWN",
            requirement_id=req.requirement_id,
            matched_capabilities=(),
            partial_capabilities=(),
            missing_tokens=(),
            evidence={"reason": "insufficient semantic tokens"},
            provenance={"builder": "match_requirement_to_inventory.v1"},
        )

    entries = _inventory_entries(registry)
    if not entries:
        return CapabilityMatchResult(
            status="MISSING",
            requirement_id=req.requirement_id,
            matched_capabilities=(),
            partial_capabilities=(),
            missing_tokens=tuple(req.tokens),
            evidence={"reason": "empty inventory"},
            provenance={"builder": "match_requirement_to_inventory.v1"},
        )

    matched: List[str] = []
    partial: List[str] = []
    token_hits: Dict[str, List[str]] = {t: [] for t in req.tokens}

    for ent in entries:
        hit_tokens = [t for t in req.tokens if t and t in ent["blob"]]
        if not hit_tokens:
            continue
        if len(hit_tokens) >= max(1, len(req.tokens) // 2 + (1 if len(req.tokens) > 1 else 0)):
            # majority of tokens appear in this capability metadata
            if len(hit_tokens) == len(req.tokens):
                matched.append(ent["name"])
            else:
                partial.append(ent["name"])
        else:
            partial.append(ent["name"])
        for t in hit_tokens:
            token_hits[t].append(ent["name"])

    covered = {t for t, caps in token_hits.items() if caps}
    missing_tokens = tuple(t for t in req.tokens if t not in covered)

    if matched and not missing_tokens:
        status = "AVAILABLE"
    elif matched or partial:
        status = "PARTIAL" if missing_tokens or not matched else "AVAILABLE"
        if matched and not missing_tokens:
            status = "AVAILABLE"
        elif not matched and partial:
            status = "PARTIAL"
        else:
            status = "PARTIAL"
    elif not covered:
        # no token appears in any capability — missing relative to inventory
        status = "MISSING"
    else:
        status = "PARTIAL"

    return CapabilityMatchResult(
        status=status,
        requirement_id=req.requirement_id,
        matched_capabilities=tuple(dict.fromkeys(matched)),
        partial_capabilities=tuple(dict.fromkeys(partial)),
        missing_tokens=missing_tokens,
        evidence={
            "token_hits": {k: v[:5] for k, v in token_hits.items()},
            "inventory_size": len(entries),
        },
        provenance={
            "builder": "match_requirement_to_inventory.v1",
            "semantic_basis": req.semantic_basis,
        },
    )


def match_to_capability_gap(
        req: SemanticCapabilityRequirement,
        match: CapabilityMatchResult,
        goal: str = "",
) -> CapabilityGap:
    """Project match result into existing pipeline CapabilityGap."""
    missing_reqs: List[CapabilityRequirement] = []
    if match.status in ("MISSING", "PARTIAL", "UNKNOWN"):
        for tok in match.missing_tokens or req.tokens:
            missing_reqs.append(CapabilityRequirement(
                name=f"semantic:{tok}",
                description=f"Capability related to semantic token '{tok}'",
                keywords=[tok],
                origin={
                    "semantic_requirement_id": req.requirement_id,
                    "match_status": match.status,
                    "builder": "match_to_capability_gap.v1",
                },
            ))
        if not missing_reqs and match.status == "UNKNOWN":
            missing_reqs.append(CapabilityRequirement(
                name="semantic:unknown",
                description="Insufficient semantic evidence for capability match",
                keywords=[],
                origin={"semantic_requirement_id": req.requirement_id},
            ))
    satisfied = list(match.matched_capabilities)
    reason = f"semantic match status={match.status}"
    return CapabilityGap(
        goal=goal or req.semantic_basis,
        satisfied=satisfied,
        missing=missing_reqs,
        reason=reason,
    )


